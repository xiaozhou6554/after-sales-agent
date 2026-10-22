"""DeepSeek LLM 网关适配器（技术详细规格 5.2/5.3 参数契约）。

- tool_choice=auto：工具选择由模型在授权清单内进行，入口不硬编码路由；
- temperature=0.2，非 Thinking 模型；
- SDK 隐式重试必须关闭（max_retries=0），重试预算唯一归工具客户端；
- 通道失败统一折算为 LLMDependencyError，由编排层按瞬态依赖失败处理。
"""

from __future__ import annotations

from typing import Any

import httpx
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_deepseek import ChatDeepSeek

from application.agent.ports.llm import (
    ChatMessage,
    LLMDependencyError,
    LLMResult,
    LLMToolCall,
    ToolSpec,
)
from application.common.execution.circuit_breaker import CircuitBreaker
from application.common.execution.timeout_policy import remaining_seconds

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


class UnconfiguredLLMGateway:
    """未配置模型密钥时的占位网关：任何调用都如实报依赖失败，不伪装成功。"""

    def complete(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResult:
        raise LLMDependencyError("未配置 LLM_API_KEY/DEEPSEEK_API_KEY，模型网关不可用")


class DeepSeekChatGateway:
    """LLMGateway 端口的生产实现。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = DEFAULT_BASE_URL,
        temperature: float = 0.2,
        connect_timeout_s: float = 1.0,
        read_timeout_s: float = 8.0,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        self._model_name = model
        self._circuit = circuit or CircuitBreaker()
        self._client = ChatDeepSeek(
            model=model,
            api_key=api_key,
            api_base=base_url,
            temperature=temperature,
            max_retries=0,
            timeout=httpx.Timeout(
                connect=connect_timeout_s,
                read=read_timeout_s,
                write=read_timeout_s,
                pool=connect_timeout_s,
            ),
        )

    def complete(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResult:
        remaining = remaining_seconds()
        if remaining is not None and remaining <= 0:
            raise LLMDependencyError("请求时限已到，拒绝发起模型调用")
        circuit = getattr(self, "_circuit", None)
        if circuit is not None and not circuit.allow():
            raise LLMDependencyError("LLM 依赖熔断中")
        lc_messages: list[BaseMessage] = [SystemMessage(content=system)]
        duplicate_system_skipped = False
        for message in messages:
            if (
                message.role == "system"
                and message.content == system
                and not duplicate_system_skipped
            ):
                duplicate_system_skipped = True
                continue
            lc_messages.append(_to_lc_message(message))
        try:
            if tools:
                runnable = self._client.bind_tools([_tool_schema(spec) for spec in tools])
            else:
                runnable = self._client
            result = runnable.invoke(lc_messages)
        except LLMDependencyError:
            if circuit is not None:
                circuit.record(failed=True)
            raise
        except Exception as exc:  # noqa: BLE001 - SDK/网络异常统一折算通道失败
            if circuit is not None:
                circuit.record(failed=True)
            raise LLMDependencyError(f"LLM 通道失败: {exc}") from exc
        if circuit is not None:
            circuit.record(failed=False)
        return _to_result(result, model=self._model_name)


def _to_lc_message(message: ChatMessage) -> BaseMessage:
    if message.role == "user":
        return HumanMessage(content=message.content)
    if message.role == "assistant":
        return AIMessage(content=message.content)
    if message.role == "tool":
        return ToolMessage(content=message.content, tool_call_id=message.tool_call_id or "")
    return SystemMessage(content=message.content)


def _tool_schema(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def _to_result(result: BaseMessage, *, model: str) -> LLMResult:
    content = result.content if isinstance(result.content, str) else ""
    calls: list[LLMToolCall] = []
    for tc in getattr(result, "tool_calls", None) or []:
        args = tc.get("args")
        calls.append(
            LLMToolCall(
                call_id=str(tc.get("id") or ""),
                name=str(tc.get("name") or ""),
                arguments=args if isinstance(args, dict) else {},
            )
        )
    return LLMResult(content=content or None, tool_calls=tuple(calls), model=model)
