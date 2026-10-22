"""单 Supervisor ReAct 工作流（技术详细规格 7.2/7.5，流程步骤10）。

节点：supervisor → tool → reflection → finish；一个 agent_step 最多一次
MCP 工具调用；确定性守卫先行，仅异常进入 Reflection；步骤上限与硬超时
由运行配置强制。可信数据（AuthContext、会话令牌、链路、白名单）经
per-invocation 的 WorkflowDeps 传递，不进入 State，也不进入模型上下文。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from application.agent.guard import (
    GUARD_DENIED,
    GUARD_EMPTY,
    GUARD_FACTS,
    GUARD_FAILED,
    GUARD_INVALID,
    GUARD_UNAUTHENTICATED,
    GuardVerdict,
    argument_defects,
    ask_user_prompt_for,
    evaluate_envelope,
)
from application.agent.ports.llm import ChatMessage, LLMGateway
from application.agent.ports.tools import ToolExecutionPort
from application.agent.prompts import PromptCatalog
from application.agent.reflection import reflect
from application.agent.state import NEXT_ASK_USER, NEXT_FINISH, NEXT_HANDOFF, AgentState
from application.agent.supervisor import SupervisorDecision, _intent_of, decide
from application.agent.tool_catalog import ToolCatalog
from application.common.context import ErrorDto, TraceContext
from application.common.execution.invocation_scope import ToolInvocationPending

logger = logging.getLogger("after_sales.agent")

KNOWLEDGE_ROUTES = {
    "search_knowledge_base": "rag",
    "query_knowledge_graph": "kg",
    "search_hybrid_knowledge": "hybrid",
    "get_policy_detail": "rag",
}

# 提示词 6.1 兜底话术原文（确定性路径直接使用，保证口径一致）。
FALLBACK_NOT_FOUND = "这个我暂时查不到，我帮您转接人工客服核实一下。"
FALLBACK_HANDOFF = "这个问题需要人工进一步核实，我先为您申请转接人工服务。"
FALLBACK_TIMEOUT = "系统处理超时，请稍后重试。"
FALLBACK_UNAUTHENTICATED = "登录状态已失效，请重新登录后再试。"


@dataclass
class WorkflowDeps:
    """单次请求的运行时依赖与可信数据；不进入 State 与模型上下文。"""

    llm: LLMGateway
    tools: ToolExecutionPort
    catalog: ToolCatalog
    prompts: PromptCatalog
    history: list[ChatMessage]
    session_token: str
    trace: TraceContext
    max_steps: int = 8
    deadline_monotonic: float = field(default_factory=lambda: time.monotonic() + 15.0)
    clock: Callable[[], float] = time.monotonic
    # 节点间传递的当拍决策/守卫结论（图执行为单线程顺序）。
    pending_decision: SupervisorDecision | None = None
    pending_verdict: GuardVerdict | None = None
    # finish 节点的确定性答复草稿（追问/转人工/超时等路径预先写好）。
    final_draft: str | None = None
    draft_next: str = NEXT_FINISH
    requested_field: str | None = None
    # 异常路径的错误契约（26.6：error 状态必须携带 error 对象）。
    final_status: str | None = None  # 非 None 时服务层按 error 状态返回
    final_error: ErrorDto | None = None
    final_handoff: bool = False


def _error_dto(deps: WorkflowDeps, error_code: str, message: str, *, retryable: bool) -> ErrorDto:
    return ErrorDto(
        error_code=error_code,
        message=message,
        retryable=retryable,
        request_id=deps.trace.request_id,
        correlation_id=deps.trace.correlation_id,
        trace_id=deps.trace.trace_id,
    )


def build_chat_graph(checkpointer=None, *, interrupt_before: tuple[str, ...] = ()):
    """构建编译后的对话图；依赖经 config["configurable"]["deps"] 注入。

    checkpointer 由 Bootstrap 注入（步骤12：MySQL Checkpoint 持久化与中断
    恢复）；未注入时为无持久化编译（仅限测试/显式降级场景）。
    """
    graph = StateGraph(AgentState)
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("plan_executor", plan_executor_node)
    graph.add_node("tool", tool_node)
    graph.add_node("reflection", reflection_node)
    graph.add_node("finish", finish_node)
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route_supervisor,
        {
            "tool": "tool",
            "plan_executor": "plan_executor",
            "supervisor": "supervisor",
            "finish": "finish",
        },
    )
    graph.add_conditional_edges(
        "plan_executor",
        _route_plan_executor,
        {"tool": "tool", "finish": "finish"},
    )
    graph.add_edge("tool", "reflection")
    graph.add_conditional_edges(
        "reflection",
        _route_reflection,
        {"plan_executor": "plan_executor", "finish": "finish"},
    )
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer, interrupt_before=interrupt_before or None)


def _deps(config) -> WorkflowDeps:
    return config["configurable"]["deps"]


def _deadline_exceeded(deps: WorkflowDeps) -> bool:
    return deps.clock() >= deps.deadline_monotonic


def _restore_runtime(deps: WorkflowDeps, state: AgentState) -> None:
    if deps.pending_decision is None and state.get("pending_decision"):
        deps.pending_decision = SupervisorDecision(**state["pending_decision"])
    if deps.pending_verdict is None and state.get("pending_verdict"):
        deps.pending_verdict = GuardVerdict(**state["pending_verdict"])
    if deps.final_draft is None:
        deps.final_draft = state.get("final_draft")
    if deps.final_status is None:
        deps.final_status = state.get("final_status")
    if deps.final_error is None and state.get("final_error"):
        deps.final_error = ErrorDto(**state["final_error"])
    if state.get("draft_next"):
        deps.draft_next = state["draft_next"]
    if deps.requested_field is None:
        deps.requested_field = state.get("requested_field")
    deps.final_handoff = bool(state.get("final_handoff"))


def _runtime_update(deps: WorkflowDeps) -> dict[str, Any]:
    return {
        "final_draft": deps.final_draft,
        "final_status": deps.final_status,
        "final_error": asdict(deps.final_error) if deps.final_error else None,
        "draft_next": deps.draft_next,
        "requested_field": deps.requested_field,
        "final_handoff": deps.final_handoff,
    }


def _finish_update(deps: WorkflowDeps, update: dict[str, Any]) -> dict[str, Any]:
    return {**update, **_runtime_update(deps), "final_answer": deps.final_draft}


def supervisor_node(state: AgentState, config) -> dict:
    deps = _deps(config)
    step_count = int(state.get("step_count") or 1)
    if step_count > deps.max_steps:
        # 提示词规则 11：达到上限必须停止调用并转人工。
        deps.final_draft = FALLBACK_HANDOFF
        deps.draft_next = NEXT_HANDOFF
        return {
            "next_action": NEXT_HANDOFF,
            "handoff_required": True,
            "current_agent": "supervisor",
            **_runtime_update(deps),
        }
    if _deadline_exceeded(deps):
        deps.final_draft = FALLBACK_TIMEOUT
        deps.draft_next = NEXT_FINISH
        deps.final_status = "error"
        deps.final_error = _error_dto(deps, "TIMEOUT", "处理超时，请稍后重试", retryable=True)
        return {"next_action": NEXT_FINISH, "current_agent": "supervisor", **_runtime_update(deps)}
    decision, _ = decide(
        deps.llm,
        system_prompt=deps.prompts.supervisor_system_prompt(),
        history=deps.history,
        state=state,
        tool_specs=list(deps.catalog.specs),
        catalog_is_allowed=deps.catalog.is_allowed,
    )
    if decision.next_action == "plan":
        remaining_steps = deps.max_steps - step_count + 1
        invalid_arguments = any(
            argument_defects(
                deps.catalog.entries[task["tool_name"]].get("input_schema") or {},
                task["arguments"],
            )
            for task in decision.plan
        )
        if len(decision.plan) > remaining_steps or invalid_arguments:
            decision = SupervisorDecision(
                decision_id=decision.decision_id,
                next_action=NEXT_FINISH,
                reason_code="invalid_or_unauthorized_plan",
            )
    deps.pending_decision = decision
    updates: dict[str, Any] = {
        "next_action": decision.next_action,
        "intent": decision.intent,
        "current_agent": "supervisor",
        "pending_decision": asdict(decision),
    }
    if decision.reason_code == "invalid_or_unauthorized_plan":
        retries = int(state.get("retry_count") or 0)
        if retries < 1 and step_count < deps.max_steps:
            updates["next_action"] = "replan"
            updates["retry_count"] = retries + 1
            updates["step_count"] = step_count + 1
            observations = list(state.get("observations") or [])
            observations.append(
                {
                    "tool_name": "planner",
                    "outcome": "invalid",
                    "summary": "计划包含未授权能力或任务结构无效；仅允许重新规划一次",
                }
            )
            updates["observations"] = observations
        else:
            deps.final_draft = FALLBACK_HANDOFF
            deps.draft_next = NEXT_HANDOFF
            updates["handoff_required"] = True
        return {**updates, **_runtime_update(deps)}
    if state.get("retry_count") and decision.next_action == NEXT_FINISH:
        # 无效计划后的直接文本答复没有工具证据，不能绕过计划校验。
        deps.final_draft = FALLBACK_HANDOFF
        deps.draft_next = NEXT_HANDOFF
        updates["handoff_required"] = True
        return {**updates, **_runtime_update(deps)}
    if decision.target_capability in KNOWLEDGE_ROUTES:
        updates["knowledge_route"] = KNOWLEDGE_ROUTES[decision.target_capability]
    if decision.next_action == "plan":
        updates["plan_tasks"] = list(decision.plan)
        updates["plan"] = [task["tool_name"] for task in decision.plan]
        updates["plan_cursor"] = 0
    if decision.next_action == NEXT_HANDOFF:
        deps.final_draft = FALLBACK_HANDOFF
        deps.draft_next = NEXT_HANDOFF
        updates["handoff_required"] = True
    return {**updates, **_runtime_update(deps)}


def _route_supervisor(state: AgentState) -> str:
    if state.get("next_action") == "replan":
        return "supervisor"
    if state.get("next_action") == "plan":
        return "plan_executor"
    if state.get("next_action") == "dispatch":
        return "tool"
    return "finish"


def plan_executor_node(state: AgentState, config) -> dict:
    """每周期只下发一个已授权只读任务；恢复时从持久 plan_cursor 继续。"""
    deps = _deps(config)
    tasks = state.get("plan_tasks") or []
    cursor = int(state.get("plan_cursor") or 0)
    if cursor >= len(tasks) or not tasks:
        return {"next_action": NEXT_FINISH, "current_agent": "plan_executor"}
    if int(state.get("step_count") or 1) > deps.max_steps:
        deps.final_draft = FALLBACK_HANDOFF
        deps.draft_next = NEXT_HANDOFF
        return {
            "next_action": NEXT_FINISH,
            "handoff_required": True,
            "current_agent": "plan_executor",
            **_runtime_update(deps),
        }
    if _deadline_exceeded(deps):
        deps.final_draft = FALLBACK_TIMEOUT
        deps.final_status = "error"
        deps.final_error = _error_dto(deps, "TIMEOUT", "处理超时，请稍后重试", retryable=True)
        return {
            "next_action": NEXT_FINISH,
            "current_agent": "plan_executor",
            **_runtime_update(deps),
        }
    task = tasks[cursor]
    tool_name = str(task.get("tool_name") or "")
    if not deps.catalog.is_allowed(tool_name):
        deps.final_draft = FALLBACK_HANDOFF
        deps.draft_next = NEXT_HANDOFF
        return {
            "next_action": NEXT_FINISH,
            "handoff_required": True,
            "current_agent": "plan_executor",
            **_runtime_update(deps),
        }
    dependency = task.get("depends_on") or []
    prior_id = tasks[cursor - 1].get("task_id") if cursor else None
    prior_succeeded = (
        any(
            call.get("decision_id") == f"plan-{prior_id}" and call.get("status") == "success"
            for call in state.get("tool_calls") or []
        )
        if cursor
        else True
    )
    if cursor and (dependency != [prior_id] or not prior_succeeded):
        deps.final_draft = FALLBACK_HANDOFF
        deps.draft_next = NEXT_HANDOFF
        return {
            "next_action": NEXT_FINISH,
            "handoff_required": True,
            "current_agent": "plan_executor",
            **_runtime_update(deps),
        }
    decision = SupervisorDecision(
        decision_id=f"plan-{task['task_id']}",
        next_action="dispatch",
        target_capability=tool_name,
        arguments=dict(task.get("arguments") or {}),
        intent=_intent_of(tool_name),
        reason_code="authorized_plan_step",
    )
    deps.pending_decision = decision
    update = {
        "next_action": "dispatch",
        "intent": decision.intent,
        "current_agent": "plan_executor",
        "pending_decision": asdict(decision),
    }
    if tool_name in KNOWLEDGE_ROUTES:
        update["knowledge_route"] = KNOWLEDGE_ROUTES[tool_name]
    return update


def _route_plan_executor(state: AgentState) -> str:
    return "tool" if state.get("next_action") == "dispatch" else "finish"


def tool_node(state: AgentState, config) -> dict:
    deps = _deps(config)
    _restore_runtime(deps, state)
    decision = deps.pending_decision
    if decision is None or decision.next_action != "dispatch":
        return {"next_action": NEXT_FINISH, **_runtime_update(deps)}
    if _deadline_exceeded(deps):
        deps.final_draft = FALLBACK_TIMEOUT
        deps.draft_next = NEXT_FINISH
        deps.final_status = "error"
        deps.final_error = _error_dto(deps, "TIMEOUT", "处理超时，请稍后重试", retryable=True)
        deps.pending_verdict = GuardVerdict(outcome="failed", error_code="TIMEOUT")
        return {
            "next_action": NEXT_FINISH,
            "pending_verdict": {"outcome": "failed", "error_code": "TIMEOUT"},
            **_runtime_update(deps),
        }
    tool_name = decision.target_capability or ""
    # 参数预检（注册表 Schema 唯一源）：明显缺失/多余时不发起调用，
    # 直接转入追问路径，避免消耗传输与统一重试预算（16.1/工具规则 4）。
    schema = deps.catalog.entries.get(tool_name, {}).get("input_schema") or {}
    defects = argument_defects(schema, decision.arguments)
    if defects:
        prompt = ask_user_prompt_for(defects)
        deps.pending_verdict = GuardVerdict(
            outcome=GUARD_INVALID,
            error_code="INVALID_ARGUMENT",
            message=prompt,
            summary=f"调用参数不完整（{'、'.join(defects)}），未执行工具",
        )
        deps.final_draft = prompt
        deps.draft_next = NEXT_ASK_USER
        deps.requested_field = defects[0]
        tool_calls = list(state.get("tool_calls") or [])
        tool_calls.append(
            {
                "tool_name": tool_name,
                "status": "error",
                "error_code": "INVALID_ARGUMENT",
                "decision_id": decision.decision_id,
                "reason_code": decision.reason_code,
            }
        )
        return {
            "tool_calls": tool_calls,
            "next_action": NEXT_ASK_USER,
            "current_agent": "tool",
            "pending_verdict": {
                "outcome": GUARD_INVALID,
                "error_code": "INVALID_ARGUMENT",
                "message": prompt,
                "summary": deps.pending_verdict.summary,
            },
            **_runtime_update(deps),
        }
    try:
        envelope = deps.tools.execute_tool(
            decision.target_capability or "",
            decision.arguments,
            session_token=deps.session_token,
            trace=deps.trace,
            access=deps.catalog.access_of(decision.target_capability or ""),
        )
    except ToolInvocationPending:
        raise
    except Exception as exc:  # noqa: BLE001 - 通道异常折算为失败信封语义
        logger.error("tool channel failed: %s", exc)
        envelope = {
            "status": "error",
            "error_code": "DEPENDENCY_UNAVAILABLE",
            "error": {
                "error_code": "DEPENDENCY_UNAVAILABLE",
                "message": "工具通道失败",
                "retryable": True,
            },
        }
    verdict = evaluate_envelope(decision.target_capability or "", envelope)
    deps.pending_verdict = verdict
    tool_calls = list(state.get("tool_calls") or [])
    tool_calls.append(
        {
            "tool_name": decision.target_capability,
            "status": envelope.get("status"),
            "error_code": envelope.get("error_code"),
            "decision_id": decision.decision_id,
            "reason_code": decision.reason_code,
        }
    )
    observations = list(state.get("observations") or [])
    observations.append(
        {
            "tool_name": decision.target_capability,
            "outcome": verdict.outcome,
            "summary": verdict.summary,
        }
    )
    evidence_update: dict[str, Any] = {}
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    if tool_name == "search_knowledge_base":
        evidence_update["rag_evidence"] = [
            {"evidence_id": item.get("evidence_id"), "document_id": item.get("document_id")}
            for item in data.get("results") or []
            if isinstance(item, dict)
        ]
    elif tool_name == "query_knowledge_graph":
        evidence_update["kg_evidence"] = [
            {"evidence_id": item.get("evidence_id"), "relation_type": item.get("relation_type")}
            for item in data.get("relations") or []
            if isinstance(item, dict)
        ]
    elif tool_name == "search_hybrid_knowledge":
        evidence_update["rag_evidence"] = [
            {"evidence_id": item.get("evidence_id"), "document_id": item.get("document_id")}
            for item in data.get("rag_evidence") or []
            if isinstance(item, dict)
        ]
        evidence_update["kg_evidence"] = [
            {"evidence_id": item.get("evidence_id"), "relation_type": item.get("relation_type")}
            for item in data.get("kg_evidence") or []
            if isinstance(item, dict)
        ]
    elif tool_name == "get_policy_detail":
        evidence_update["rag_evidence"] = [
            {"evidence_id": ref, "document_id": None}
            for ref in data.get("evidence_refs") or []
            if isinstance(ref, str)
        ]
    return {
        "tool_calls": tool_calls,
        "observations": observations,
        "next_action": "reflection",
        "current_agent": "tool",
        "pending_verdict": {
            "outcome": verdict.outcome,
            "error_code": verdict.error_code,
            "message": verdict.message,
            "summary": verdict.summary,
        },
        **evidence_update,
    }


def reflection_node(state: AgentState, config) -> dict:
    deps = _deps(config)
    _restore_runtime(deps, state)
    verdict = deps.pending_verdict
    if verdict is None:
        return {"next_action": NEXT_FINISH, "current_agent": "reflection", **_runtime_update(deps)}
    if verdict.outcome == GUARD_FACTS and state.get("plan_tasks"):
        cursor = int(state.get("plan_cursor") or 0) + 1
        if cursor < len(state["plan_tasks"]):
            return {
                "next_action": "plan_continue",
                "plan_cursor": cursor,
                "step_count": int(state.get("step_count") or 1) + 1,
                "current_agent": "reflection",
                **_runtime_update(deps),
            }
        return {
            "next_action": NEXT_FINISH,
            "plan_cursor": cursor,
            "current_agent": "reflection",
            **_runtime_update(deps),
        }
    if verdict.outcome in (GUARD_FACTS, GUARD_EMPTY):
        # 有校验事实：交给 finish 汇总为最终回答（不再二次询问模型意图）。
        return {"next_action": NEXT_FINISH, "current_agent": "reflection", **_runtime_update(deps)}
    if verdict.outcome == GUARD_DENIED:
        # 统一安全语义话术承诺了转人工核实：按提示词 15 归入必须转人工情形。
        deps.final_draft = FALLBACK_NOT_FOUND
        deps.draft_next = NEXT_HANDOFF
        return {
            "next_action": NEXT_HANDOFF,
            "handoff_required": True,
            "current_agent": "reflection",
            **_runtime_update(deps),
        }
    if verdict.outcome == GUARD_UNAUTHENTICATED:
        deps.final_draft = FALLBACK_UNAUTHENTICATED
        deps.draft_next = NEXT_HANDOFF
        deps.final_status = "error"
        deps.final_handoff = True
        deps.final_error = _error_dto(
            deps, "UNAUTHENTICATED", "登录状态已失效，请重新登录", retryable=False
        )
        return {
            "next_action": NEXT_HANDOFF,
            "handoff_required": True,
            "current_agent": "reflection",
            **_runtime_update(deps),
        }
    if verdict.outcome == GUARD_FAILED:
        # 统一预算已用尽的依赖失败：确定性如实收束（26.6 error 契约），
        # 不再消耗模型决策；异常类错误不重试、不编造。
        deps.final_status = "error"
        deps.final_error = _error_dto(
            deps,
            verdict.error_code or "DEPENDENCY_UNAVAILABLE",
            verdict.message or "依赖服务暂不可用，请稍后重试",
            retryable=True,
        )
        timeout_text = FALLBACK_TIMEOUT if verdict.error_code == "TIMEOUT" else None
        deps.final_draft = timeout_text or "依赖服务暂不可用，请稍后重试。"
        deps.draft_next = NEXT_FINISH
        return {"next_action": NEXT_FINISH, "current_agent": "reflection", **_runtime_update(deps)}
    # 参数不合法等异常进入 Reflection 决策（7.2：仅异常时调用）。
    outcome = reflect(
        deps.llm,
        system_prompt=deps.prompts.supervisor_nodes,
        verdict=verdict,
        tool_name=str(deps.pending_decision.target_capability if deps.pending_decision else ""),
    )
    if outcome.next_action == NEXT_ASK_USER:
        deps.final_draft = verdict.message or "请补充查询所需的信息。"
        deps.draft_next = NEXT_ASK_USER
        # 参数预检已给出具体字段时保留；其余异常场景用通用占位字段。
        deps.requested_field = deps.requested_field or "missing_info"
        return {
            "next_action": NEXT_ASK_USER,
            "current_agent": "reflection",
            **_runtime_update(deps),
        }
    if outcome.next_action == NEXT_HANDOFF:
        deps.final_draft = FALLBACK_HANDOFF
        deps.draft_next = NEXT_HANDOFF
        return {
            "next_action": NEXT_HANDOFF,
            "handoff_required": True,
            "current_agent": "reflection",
            **_runtime_update(deps),
        }
    deps.final_draft = FALLBACK_NOT_FOUND
    deps.draft_next = NEXT_FINISH
    return {"next_action": NEXT_FINISH, "current_agent": "reflection", **_runtime_update(deps)}


def _route_reflection(state: AgentState) -> str:
    if state.get("next_action") == "plan_continue":
        return "plan_executor"
    return "finish"


def finish_node(state: AgentState, config) -> dict:
    deps = _deps(config)
    _restore_runtime(deps, state)
    # 1) 确定性草稿优先（追问/转人工/超时/查不到）。
    if deps.final_draft is not None:
        return _finish_update(deps, {"next_action": deps.draft_next, "current_agent": "finish"})
    decision = deps.pending_decision
    # 2) Supervisor 直接回答（寒暄/致谢/追问/超出范围说明，工具规则 1 均不调工具）。
    # 追问与寒暄同为直接回答，形态不可靠区分（不得做入口意图硬编码）；
    # 缺参数追问的确定性 needs_input 路径由工具守卫 INVALID_ARGUMENT 承接。
    if decision is not None and decision.direct_answer:
        deps.final_draft = decision.direct_answer.strip()
        deps.draft_next = NEXT_FINISH
        return _finish_update(deps, {"next_action": NEXT_FINISH, "current_agent": "finish"})
    # 3) 有 Observation：以最终回答提示词（6.4 原文）汇总已校验事实。
    summaries = [
        str(item.get("summary") or "")
        for item in state.get("observations") or []
        if item.get("summary")
    ]
    if summaries:
        # 物流 ETA 只可来自轨迹源；直接使用守卫构造的事实摘要，避免最终
        # 生成模型补写不存在的预计时间（步骤22）。
        if decision is not None and decision.target_capability in {
            "query_logistics",
            "query_tracking",
            "check_delivery_exception",
        }:
            deps.final_draft = " ".join(summaries)
            deps.draft_next = NEXT_FINISH
            return _finish_update(deps, {"next_action": NEXT_FINISH, "current_agent": "finish"})
        try:
            result = deps.llm.complete(
                system=deps.prompts.final_answer_system_prompt(),
                messages=[
                    ChatMessage(role="user", content=state["user_question"]),
                    ChatMessage(
                        role="user",
                        content="[系统观察] " + " ".join(summaries),
                    ),
                ],
                tools=None,
            )
            answer = (result.content or "").strip()
            deps.final_draft = answer or " ".join(summaries)
        except Exception:  # noqa: BLE001 - 最终汇总失败回退确定性摘要
            logger.error("final answer generation failed")
            deps.final_draft = " ".join(summaries)
        deps.draft_next = NEXT_FINISH
        return _finish_update(deps, {"next_action": NEXT_FINISH, "current_agent": "finish"})
    # 4) 无话可说：如实收束并转人工，不编造。
    deps.final_draft = FALLBACK_HANDOFF
    deps.draft_next = NEXT_HANDOFF
    return _finish_update(
        deps, {"next_action": NEXT_HANDOFF, "handoff_required": True, "current_agent": "finish"}
    )
