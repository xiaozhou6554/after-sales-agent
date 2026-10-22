"""Supervisor 节点：识别意图、在受限白名单内选择工具或直接回答（7.1/26.6）。

一个 agent_step 最多产生一次工具调用；模型点明白名单外工具或参数明显缺失
（如查订单但未给出订单号且历史无补齐）时不执行调用，转追问或如实收束。
模型是否缺少参数由模型依据工具 Schema 与对话判断，本节点不做业务猜测。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from application.agent.planning import validate_tool_plan
from application.agent.ports.llm import ChatMessage, LLMGateway, LLMResult, ToolSpec
from application.agent.state import (
    NEXT_ASK_USER,
    NEXT_DISPATCH,
    NEXT_FINISH,
    NEXT_HANDOFF,
    AgentState,
)


@dataclass(frozen=True)
class SupervisorDecision:
    """26.6 SupervisorDecision 的应用层形态（不含内部思维链）。"""

    decision_id: str
    next_action: str  # dispatch / plan / ask_user / handoff / finish
    target_capability: str | None = None
    arguments: dict = field(default_factory=dict)
    intent: str | None = None
    direct_answer: str | None = None
    reason_code: str = ""
    plan: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        allowed = {"dispatch", "plan", "ask_user", "handoff", "finish"}
        if self.next_action not in allowed:
            raise ValueError(f"未知 Supervisor next_action: {self.next_action!r}")
        if self.next_action == "dispatch" and not self.target_capability:
            raise ValueError("dispatch 必须指明 target_capability")


def supervisor_messages(
    system_prompt: str,
    history: list[ChatMessage],
    state: AgentState,
    tool_specs: list[ToolSpec],
) -> list[ChatMessage]:
    """组装 Supervisor 消息：系统提示词＋历史＋观察摘要＋当前问题。

    Observation 只以业务事实摘要进入上下文（不含工具原始信封与参数明文）。
    """
    messages: list[ChatMessage] = [ChatMessage(role="system", content=system_prompt)]
    messages.extend(history)
    for index, observation in enumerate(state.get("observations") or [], start=1):
        summary = observation.get("summary") or ""
        if summary:
            messages.append(
                ChatMessage(
                    role="user",
                    content=f"[系统观察{index}] {summary}",
                )
            )
    messages.append(ChatMessage(role="user", content=state["user_question"]))
    return messages


def decide(
    llm: LLMGateway,
    *,
    system_prompt: str,
    history: list[ChatMessage],
    state: AgentState,
    tool_specs: list[ToolSpec],
    catalog_is_allowed: Callable[[str], bool],
) -> tuple[SupervisorDecision, LLMResult]:
    """调用模型并映射为受限决策；白名单外工具名一律不进入 dispatch。

    catalog_is_allowed 为可调用对象（ToolCatalog.is_allowed），以参数注入
    避免应用层直接耦合目录实现。
    """
    messages = supervisor_messages(system_prompt, history, state, tool_specs)
    result = llm.complete(system=system_prompt, messages=messages, tools=tool_specs or None)
    decision_id = f"dec-{uuid.uuid4().hex}"
    if result.has_tool_calls:
        if len(result.tool_calls) > 1:
            try:
                plan = validate_tool_plan(
                    result.tool_calls, is_allowed=catalog_is_allowed, max_tasks=8
                )
            except ValueError:
                return (
                    SupervisorDecision(
                        decision_id=decision_id,
                        next_action=NEXT_FINISH,
                        reason_code="invalid_or_unauthorized_plan",
                    ),
                    result,
                )
            return (
                SupervisorDecision(
                    decision_id=decision_id,
                    next_action="plan",
                    plan=plan,
                    intent="multi_step",
                    reason_code="multiple_authorized_tools",
                ),
                result,
            )
        call = result.tool_calls[0]  # 一个 agent_step 最多一次工具调用
        if not catalog_is_allowed(call.name):
            return (
                SupervisorDecision(
                    decision_id=decision_id,
                    next_action=NEXT_FINISH,
                    reason_code="tool_not_allowed",
                    direct_answer=None,
                ),
                result,
            )
        return (
            SupervisorDecision(
                decision_id=decision_id,
                next_action=NEXT_DISPATCH,
                target_capability=call.name,
                arguments=dict(call.arguments or {}),
                intent=_intent_of(call.name),
            ),
            result,
        )
    # 模型直接回答：寒暄/致谢/可直答内容不再调用工具（提示词 6.2 规则 1）。
    return (
        SupervisorDecision(
            decision_id=decision_id,
            next_action=NEXT_FINISH,
            direct_answer=result.content,
            intent=None,
        ),
        result,
    )


def _intent_of(tool_name: str) -> str | None:
    if tool_name in {
        "search_knowledge_base",
        "query_knowledge_graph",
        "search_hybrid_knowledge",
        "get_policy_detail",
    }:
        return "knowledge_query"
    if tool_name.startswith("query_order"):
        return "order_query"
    if tool_name.startswith("query_payment"):
        return "payment_query"
    if tool_name.startswith("query_logistics"):
        return "logistics_query"
    return None


__all__ = [
    "SupervisorDecision",
    "decide",
    "supervisor_messages",
    NEXT_DISPATCH,
    NEXT_ASK_USER,
    NEXT_HANDOFF,
    NEXT_FINISH,
]
