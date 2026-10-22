"""Reflection 节点：仅在确定性守卫判定异常时触发（7.2，流程步骤10）。

权限、业务规则、参数错误不重试（26.6 ReflectionDecision）；统一只读重试
预算由 MCP 客户端唯一持有，本节点不再发起工具调用，只在
询问用户/转人工/如实完成 之间决策。
"""

from __future__ import annotations

from dataclasses import dataclass

from application.agent.guard import GUARD_FAILED, GUARD_INVALID, GuardVerdict
from application.agent.ports.llm import ChatMessage, LLMGateway
from application.agent.state import NEXT_ASK_USER, NEXT_FINISH, NEXT_HANDOFF

REFLECTION_ALLOWED = (NEXT_ASK_USER, NEXT_HANDOFF, NEXT_FINISH)

# 异常场景的确定性收束口径：守卫已给出安全语义文案，Reflection 只选去向。
_REFLECTION_INSTRUCTION = (
    "Reflection：工具调用出现异常。统一预算已用尽，不得再次调用工具。"
    "请在 ask_user / handoff / finish 中选择一个去向，"
    "并给出一句话理由（reason）。输出 JSON："
    '{"next_action": "ask_user|handoff|finish", "reason": "..."}'
)


@dataclass(frozen=True)
class ReflectionOutcome:
    next_action: str  # ask_user / handoff / finish
    reason_code: str

    def __post_init__(self) -> None:
        if self.next_action not in REFLECTION_ALLOWED:
            raise ValueError(f"未知 Reflection next_action: {self.next_action!r}")


def reflect(
    llm: LLMGateway,
    *,
    system_prompt: str,
    verdict: GuardVerdict,
    tool_name: str,
) -> ReflectionOutcome:
    """异常场景的 Reflection：参数类异常默认追问，其余默认转人工；
    模型输出仅用于微调去向，解析失败按确定性默认收束（不因反射失败放大故障）。
    """
    if verdict.outcome == GUARD_INVALID:
        return ReflectionOutcome(next_action=NEXT_ASK_USER, reason_code="invalid_arguments")
    if verdict.outcome == GUARD_FAILED and verdict.error_code == "TIMEOUT":
        return ReflectionOutcome(next_action=NEXT_FINISH, reason_code="timeout")
    try:
        result = llm.complete(
            system=system_prompt,
            messages=[
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(
                    role="user",
                    content=(
                        f"{_REFLECTION_INSTRUCTION}\n"
                        f"工具: {tool_name}\n结果: {verdict.summary}"
                        f"（错误码 {verdict.error_code or '无'}）"
                    ),
                ),
            ],
            tools=None,
        )
    except Exception:  # noqa: BLE001 - 反射失败按默认收束，不外抛
        return ReflectionOutcome(next_action=NEXT_HANDOFF, reason_code="reflection_failed")
    choice = _parse_choice(result.content or "")
    if choice is not None:
        return ReflectionOutcome(next_action=choice, reason_code="reflection_llm")
    return ReflectionOutcome(next_action=NEXT_HANDOFF, reason_code="reflection_unparsed")


def _parse_choice(content: str) -> str | None:
    text = content.strip()
    for action in REFLECTION_ALLOWED:
        if f'"{action}"' in text:
            return action
    return None
