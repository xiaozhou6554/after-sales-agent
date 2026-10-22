"""MCP 客户端：连接独立 MCP HTTP 进程并执行工具调用（9.3、9.4）。

- 统一只读重试预算是唯一重试入口（1.4）：只读瞬态失败最多重试 1 次，
  退避 100ms+抖动且不超剩余时限；SDK/HTTP 层不叠加隐式重试；
- 高风险写操作（access="write"）重试次数为 0；
- 链路三元组与 Bearer 令牌经受控传输请求头携带，不进入模型可见参数；
- 返回值始终是工具响应信封 dict（status=success/empty/partial/degraded/error）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections.abc import Callable
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError

from application.common.context import TraceContext
from application.common.execution.circuit_breaker import CircuitBreaker
from application.common.execution.invocation_scope import (
    ToolInvocationPending,
    current_tool_tenant,
)
from application.common.execution.retry_policy import RetryBudget
from application.common.execution.timeout_policy import remaining_seconds
from infrastructure.persistence.mysql.repositories.tool_invocation_repository import (
    MySQLToolInvocationRepository,
)

CONNECT_TIMEOUT_DEFAULT_S = 1.0
READ_TIMEOUT_DEFAULT_S = 3.0
TOTAL_DEADLINE_DEFAULT_S = 10.0


class MCPClientError(RuntimeError):
    """MCP 通道级失败（连接/协议/超时）；由调用方决定是否进入重试预算。"""


class MCPToolClient:
    def __init__(
        self,
        server_url: str,
        *,
        connect_timeout_s: float = CONNECT_TIMEOUT_DEFAULT_S,
        read_timeout_s: float = READ_TIMEOUT_DEFAULT_S,
        total_deadline_s: float = TOTAL_DEADLINE_DEFAULT_S,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        sleep: Callable[[float], object] = asyncio.sleep,
        circuit: CircuitBreaker | None = None,
        invocations: MySQLToolInvocationRepository | None = None,
    ) -> None:
        self._url = server_url
        self._connect_timeout_s = connect_timeout_s
        self._read_timeout_s = read_timeout_s
        self._total_deadline_s = total_deadline_s
        self._clock = clock
        self._jitter = jitter
        self._sleep = sleep
        self._circuit = circuit or CircuitBreaker(clock)
        self._invocations = invocations

    def _headers(self, session_token: str, trace: TraceContext) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {session_token}",
            "X-Request-Id": trace.request_id,
            "X-Correlation-Id": trace.correlation_id,
            "X-Trace-Id": trace.trace_id,
            **({"X-Thread-Id": trace.thread_id} if trace.thread_id else {}),
        }

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        *,
        session_token: str,
        trace: TraceContext,
        access: str = "read",
    ) -> dict:
        """执行一次逻辑工具调用；错误信封按预算决定是否重试，最后返回末次信封。"""
        tenant_id = current_tool_tenant()
        digest = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        if self._invocations is not None and tenant_id is not None:
            previous = self._invocations.result_for(tenant_id, trace.request_id, tool_name, digest)
            if previous is not None and previous[0] == "succeeded" and previous[1]:
                return self._with_trace(trace, previous[1])
        if not self._circuit.allow():
            return self._with_trace(
                trace,
                self._plain_envelope(
                    tool_name, "DEPENDENCY_UNAVAILABLE", "MCP 依赖熔断中", retryable=True
                ),
            )
        parent_remaining = remaining_seconds()
        deadline_s = min(
            self._total_deadline_s,
            parent_remaining if parent_remaining is not None else self._total_deadline_s,
        )
        budget = RetryBudget(
            max_retries=1 if access == "read" else 0,
            deadline_s=deadline_s,
            clock=self._clock,
            jitter=self._jitter,
        )
        while True:
            if budget.remaining_s() <= 0:
                self._circuit.record(failed=True)
                return self._with_trace(
                    trace,
                    self._plain_envelope(tool_name, "TIMEOUT", "请求时限已到", retryable=True),
                )
            lease = None
            if self._invocations is not None and tenant_id is not None:
                lease = self._invocations.reserve(
                    tenant_id=tenant_id,
                    request_id=trace.request_id,
                    agent_id="supervisor",
                    tool_name=tool_name,
                    normalized_args_digest=digest,
                    args_summary=arguments,
                    access_mode=access,
                    worker_id=f"tool-{uuid.uuid4().hex}",
                )
                if lease is None:
                    previous = self._invocations.result_for(
                        tenant_id, trace.request_id, tool_name, digest
                    )
                    if (
                        previous is not None
                        and previous[0] in ("succeeded", "failed")
                        and previous[1]
                    ):
                        return self._with_trace(trace, previous[1])
                    raise ToolInvocationPending("工具调用仍被持有或写入结果未知")
            budget.begin_attempt()
            try:
                envelope = await asyncio.wait_for(
                    self._call_once(tool_name, arguments, session_token, trace),
                    timeout=budget.remaining_s(),
                )
            except (TimeoutError, asyncio.TimeoutError):
                envelope = self._plain_envelope(
                    tool_name, "TIMEOUT", "MCP 调用超时", retryable=True
                )
            except MCPClientError as exc:
                # 传输/超时也是瞬态失败：转错误信封后统一进入预算判定。
                envelope = self._plain_envelope(
                    tool_name, "DEPENDENCY_UNAVAILABLE", str(exc), retryable=True
                )
            envelope = self._with_trace(trace, envelope)
            if lease is not None:
                error_code = str(envelope.get("error_code") or "")
                status = "succeeded" if envelope.get("status") != "error" else "failed"
                if access == "write" and error_code in ("TIMEOUT", "DEPENDENCY_UNAVAILABLE"):
                    status = "unknown"
                self._invocations.finish(
                    lease,
                    status=status,
                    result_summary=envelope,
                    error=error_code if status != "succeeded" else None,
                )
            if envelope.get("status") != "error":
                self._circuit.record(failed=False)
                return envelope
            if budget.should_retry(str(envelope.get("error_code"))):
                await self._sleep(budget.record_retry())
                continue
            self._circuit.record(
                failed=envelope.get("error_code") in ("DEPENDENCY_UNAVAILABLE", "TIMEOUT")
            )
            return envelope

    async def _call_once(
        self, tool_name: str, arguments: dict, session_token: str, trace: TraceContext
    ) -> dict:
        # mcp SDK 的 streamablehttp_client 以 timedelta 表达超时（HTTP 总时限）；
        # 单步读超时由 wait_for 强制，整体不超出剩余时限语义。
        http_total = timedelta(seconds=self._total_deadline_s)
        try:
            async with streamablehttp_client(
                self._url, headers=self._headers(session_token, trace), timeout=http_total
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=self._read_timeout_s)
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, arguments),
                        timeout=self._read_timeout_s,
                    )
        except McpError as exc:
            # 协议级错误（如参数不符合工具 Schema）：统一按参数错误返回信封。
            return self._protocol_error_envelope(tool_name, exc)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise MCPClientError(f"MCP 调用超时: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 传输/连接失败统一转瞬态错误
            raise MCPClientError(f"MCP 通道失败: {exc}") from exc
        return self._result_to_envelope(tool_name, result)

    def _plain_envelope(
        self, tool_name: str, error_code: str, message: str, *, retryable: bool
    ) -> dict:
        return {
            "schema_version": "1.0",
            "request_id": "",
            "correlation_id": "",
            "trace_id": "",
            "invocation_id": "",
            "tool_name": tool_name,
            "status": "error",
            "error_code": error_code,
            "parameter_summary": {},
            "data": None,
            "error": {
                "error_code": error_code,
                "message": (message or error_code)[:500],
                "retryable": retryable,
            },
        }

    def _with_trace(self, trace: TraceContext, envelope: dict) -> dict:
        """客户端补齐信封链路字段：服务端缺失时以本次调用的链路为准。"""
        filled = dict(envelope)
        filled.setdefault("tool_name", envelope.get("tool_name") or "")
        if not filled.get("request_id"):
            filled["request_id"] = trace.request_id
        if not filled.get("correlation_id"):
            filled["correlation_id"] = trace.correlation_id
        if not filled.get("trace_id"):
            filled["trace_id"] = trace.trace_id
        if not filled.get("invocation_id"):
            filled["invocation_id"] = f"inv-{uuid.uuid4().hex}"
        return filled

    def _protocol_error_envelope(self, tool_name: str, exc: McpError) -> dict:
        message = str(getattr(exc, "error", None) and exc.error.message or exc)
        return self._plain_envelope(tool_name, "INVALID_ARGUMENT", message, retryable=False)

    def _result_to_envelope(self, tool_name: str, result) -> dict:
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict) and "status" in structured:
            return structured
        if getattr(result, "isError", False):
            text = self._text_of(result)
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "status" in parsed:
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            return self._plain_envelope(
                tool_name, "DEPENDENCY_UNAVAILABLE", text or "工具执行失败", retryable=True
            )
        text = self._text_of(result)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        raise MCPClientError("MCP 响应缺少结构化信封")

    @staticmethod
    def _text_of(result) -> str:
        parts = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)
