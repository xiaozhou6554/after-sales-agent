"""Agent 公共 State（《技术详细规格》7.4 核心字段，纯 Python）。

字段名与规格 7.4 一一对应；State 只承载服务端工作流数据，
AuthContext 与会话令牌等可信信息不进入 State（经运行时配置单独传递）。
"""

from __future__ import annotations

from typing import Any, TypedDict

# next_action 取值（7.4 示例为 finish；完整枚举见 26.6 SupervisorDecision）。
NEXT_DISPATCH = "dispatch"
NEXT_ASK_USER = "ask_user"
NEXT_HANDOFF = "handoff"
NEXT_FINISH = "finish"


class AgentState(TypedDict, total=False):
    """7.4 Agent State 核心字段；LangGraph 节点间以该结构传递。"""

    user_id: str
    thread_id: str
    trace_id: str
    user_question: str
    intent: str | None
    current_agent: str
    # 已发生的工具调用摘要 [{tool_name, status, error_code}]（不含参数明文）。
    tool_calls: list[dict[str, Any]]
    # 工具 Observation（已脱敏的摘要事实），供最终回答引用。
    observations: list[dict[str, Any]]
    plan: list[str]
    plan_tasks: list[dict[str, Any]]
    plan_cursor: int
    knowledge_route: str | None
    rag_evidence: list[dict[str, Any]]
    kg_evidence: list[dict[str, Any]]
    next_action: str
    step_count: int
    retry_count: int
    handoff_required: bool
    # 步骤15：跨 Worker 恢复所需的决策与守卫摘要，不含令牌和可信身份。
    pending_decision: dict[str, Any]
    pending_verdict: dict[str, Any]
    final_draft: str | None
    final_status: str | None
    final_error: dict[str, Any] | None
    final_answer: str | None
    submission_digest: str | None
    draft_next: str | None
    requested_field: str | None
    final_handoff: bool


def initial_state(
    *,
    user_id: str,
    thread_id: str,
    trace_id: str,
    user_question: str,
) -> AgentState:
    """按 7.4 构造初始 State：显式空集合，计数从 1 开始（首个 Supervisor 周期）。"""
    return AgentState(
        user_id=user_id,
        thread_id=thread_id,
        trace_id=trace_id,
        user_question=user_question,
        intent=None,
        current_agent="supervisor",
        tool_calls=[],
        observations=[],
        plan=[],
        plan_tasks=[],
        plan_cursor=0,
        knowledge_route=None,
        rag_evidence=[],
        kg_evidence=[],
        next_action="dispatch",
        step_count=1,
        retry_count=0,
        handoff_required=False,
        pending_decision={},
        pending_verdict={},
        final_draft=None,
        final_status=None,
        final_error=None,
        final_answer=None,
        submission_digest=None,
        draft_next=None,
        requested_field=None,
        final_handoff=False,
    )
