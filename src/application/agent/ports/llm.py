"""LLM 端口（依赖倒置）：消息级对话补全，工具选择由模型在授权清单内进行。

端口按 OpenAI 兼容协议的最小公共形态定义（技术详细规格 5.2/5.3）：
- tool_choice=auto，模型可在提供的 tools 内选择调用或直接回答；
- 一次请求返回文本或工具调用列表，端口不做业务解释；
- 实现层必须关闭 SDK 隐式重试与 Thinking 模式（重试预算统一在工具客户端）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ChatMessage:
    """对话消息：role ∈ system/user/assistant/tool。"""

    role: str
    content: str
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"未知消息角色: {self.role!r}")


@dataclass(frozen=True)
class ToolSpec:
    """模型可见工具声明：名称/描述/参数 Schema 均来自注册表（唯一源）。"""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class LLMToolCall:
    """模型发起的一次工具调用意图；arguments 为模型产出的原始 dict。"""

    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResult:
    """一次补全结果：content 与 tool_calls 至少其一非空。"""

    content: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = field(default_factory=tuple)
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMGateway(Protocol):
    """LLM 网关端口：生产由 langchain-deepseek 适配器实现，测试用 Fake。"""

    def complete(
        self,
        *,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResult:
        """执行一次对话补全；网络/协议失败抛 LLMDependencyError。"""
        ...


class LLMDependencyError(RuntimeError):
    """LLM 通道级失败（连接/超时/协议不兼容）；调用方按瞬态依赖失败处理。"""
