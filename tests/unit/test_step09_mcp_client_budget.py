"""步骤09 MCP 客户端重试预算测试：故障恢复不突破预算（验收要求）。"""

from __future__ import annotations

from application.common.context import TraceContext
from infrastructure.mcp.client import MCPToolClient

TRACE = TraceContext("1.0", "req-c-1", "corr-c-1", "trace-c-1", thread_id="thread-1")


async def _fake_sleep(_seconds: float) -> None:
    return None


class FakeClient(MCPToolClient):
    """以可编程结果序列替换网络传输，统计实际尝试次数。"""

    def __init__(self, results: list[dict], transient_failures: int = 0) -> None:
        super().__init__(
            "http://mcp.test/mcp",
            clock=lambda: 0.0,
            jitter=lambda: 0.0,
            sleep=_fake_sleep,
        )
        self.results = list(results)
        self.transient_failures = transient_failures
        self.attempts = 0

    async def _call_once(self, tool_name, arguments, session_token, trace) -> dict:
        self.attempts += 1
        if self.transient_failures > 0:
            self.transient_failures -= 1
            from infrastructure.mcp.client import MCPClientError

            raise MCPClientError("connect failed")
        return self.results.pop(0)


def _error(code: str, retryable: bool) -> dict:
    return {
        "schema_version": "1.0",
        "request_id": "req-s-1",
        "correlation_id": "corr-s-1",
        "trace_id": "trace-s-1",
        "invocation_id": "inv-1",
        "tool_name": "query_order",
        "status": "error",
        "error_code": code,
        "parameter_summary": {},
        "data": None,
        "error": {"error_code": code, "message": "x", "retryable": retryable},
    }


async def test_transient_failure_retried_once_within_budget() -> None:
    client = FakeClient([_error("DEPENDENCY_UNAVAILABLE", True)], transient_failures=1)
    envelope = await client.call_tool(
        "query_order", {"order_id": "ORDER-001"}, session_token="t", trace=TRACE
    )
    assert client.attempts == 2  # 首次 + 唯一一次重试
    assert envelope["status"] == "error"
    assert envelope["error_code"] == "DEPENDENCY_UNAVAILABLE"


async def test_budget_exhaustion_stops_further_attempts() -> None:
    client = FakeClient(
        [_error("DEPENDENCY_UNAVAILABLE", True), _error("DEPENDENCY_UNAVAILABLE", True)]
    )
    envelope = await client.call_tool(
        "query_order", {"order_id": "ORDER-001"}, session_token="t", trace=TRACE
    )
    assert client.attempts == 2  # 不出现第三次尝试
    assert envelope["status"] == "error"


async def test_success_after_retry_returns_envelope() -> None:
    ok = {
        "schema_version": "1.0",
        "request_id": "req-s-2",
        "correlation_id": "corr-s-2",
        "trace_id": "trace-s-2",
        "invocation_id": "inv-2",
        "tool_name": "query_order",
        "status": "success",
        "error_code": None,
        "parameter_summary": {"order_id": "ORDER-001"},
        "data": {"order": {}, "snapshot_version": "mock-v2", "observed_at": "2026-10-20T00:00:00Z"},
        "error": None,
    }
    client = FakeClient([ok], transient_failures=1)
    envelope = await client.call_tool(
        "query_order", {"order_id": "ORDER-001"}, session_token="t", trace=TRACE
    )
    assert client.attempts == 2
    assert envelope["status"] == "success"
    assert envelope["request_id"] == "req-s-2"


async def test_authorization_error_never_retried() -> None:
    client = FakeClient([_error("RESOURCE_UNAVAILABLE", False)])
    await client.call_tool("query_order", {"order_id": "ORDER-001"}, session_token="t", trace=TRACE)
    assert client.attempts == 1


async def test_write_access_has_zero_retries() -> None:
    client = FakeClient(
        [_error("DEPENDENCY_UNAVAILABLE", True), _error("DEPENDENCY_UNAVAILABLE", True)]
    )
    await client.call_tool(
        "create_refund_request",
        {"order_id": "ORDER-001"},
        session_token="t",
        trace=TRACE,
        access="write",
    )
    assert client.attempts == 1


async def test_client_fills_missing_trace_fields() -> None:
    client = FakeClient([], transient_failures=1)
    client.transient_failures = 99  # 永远失败：末次信封来自传输错误
    envelope = await client.call_tool(
        "query_order", {"order_id": "ORDER-001"}, session_token="t", trace=TRACE
    )
    assert envelope["request_id"] == "req-c-1"
    assert envelope["correlation_id"] == "corr-c-1"
    assert envelope["trace_id"] == "trace-c-1"
    assert envelope["invocation_id"].startswith("inv-")
