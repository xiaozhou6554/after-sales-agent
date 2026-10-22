"""步骤10 应用层单元测试：受限路由、确定性守卫、Reflection、工作流闭环。

LLM 与工具端口全部使用 Fake（真实模型验证单独记录于开发运行指南）；
覆盖验收口径：可寒暄直答、可派发订单工具、白名单外拒绝、缺参数追问、
失败如实收束、超时/步数上限、代客越权拒绝、会话记忆。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from application.agent.guard import (
    GUARD_DENIED,
    GUARD_EMPTY,
    GUARD_FACTS,
    GUARD_FAILED,
    GUARD_INVALID,
    GUARD_UNAUTHENTICATED,
    argument_defects,
    ask_user_prompt_for,
    evaluate_envelope,
    format_money,
)
from application.agent.ports.llm import LLMResult, LLMToolCall
from application.agent.prompts import PromptCatalog
from application.agent.reflection import reflect
from application.agent.service import ChatService
from application.agent.state import initial_state
from application.agent.supervisor import decide, supervisor_messages
from application.agent.tool_catalog import ToolCatalog
from application.agent.workflow.graph import (
    FALLBACK_HANDOFF,
    FALLBACK_TIMEOUT,
    WorkflowDeps,
    supervisor_node,
)
from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, DataScope, TraceContext
from application.common.dto import ChatRequestDto
from application.common.errors import ErrorCode
from infrastructure.agent.memory_adapter import InMemorySessionRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = PROJECT_ROOT / "data" / "prompts"
REGISTRY_PATH = PROJECT_ROOT / "data" / "contracts" / "mcp-tools.json"

PROMPTS = PromptCatalog.load(PROMPTS_DIR)


class ScriptedLLM:
    """按脚本依次返回结果；记录每次调用的 system/messages/tools 供断言。"""

    def __init__(self, *results: LLMResult) -> None:
        self.results: list[LLMResult] = list(results)
        self.calls: list[dict] = []

    def complete(self, *, system, messages, tools=None):
        self.calls.append(
            {"system": system, "messages": list(messages), "tools": list(tools or ())}
        )
        if not self.results:
            raise AssertionError("FakeLLM 脚本已耗尽")
        return self.results.pop(0)


class StaticTools:
    """恒定返回同一信封的 Fake 工具端口；记录调用入参。"""

    def __init__(self, envelope: dict) -> None:
        self.envelope = envelope
        self.calls: list[tuple] = []

    def execute_tool(self, tool_name, arguments, *, session_token, trace, access="read"):
        self.calls.append((tool_name, dict(arguments), session_token, access))
        return self.envelope


def make_auth(
    subject_id: str = "customer-north-01",
    scopes: tuple[str, ...] = ("order:read", "payment:read"),
    customers: tuple[str, ...] = ("CUSTOMER-01",),
) -> AuthContext:
    return AuthContext(
        tenant_id="tenant-north",
        subject_id=subject_id,
        roles=("customer",),
        scopes=scopes,
        data_scope=DataScope(customer_ids=customers),
        session_id="sess-1",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


def make_trace(request_id: str = "req-unit") -> TraceContext:
    return TraceContext(
        schema_version="1.0",
        request_id=request_id,
        correlation_id="corr-unit",
        trace_id="trace-unit",
    )


def order_envelope(status: str = "success", *, owner: str = "CUSTOMER-01") -> dict:
    return {
        "schema_version": "1.0",
        "request_id": "req-unit",
        "correlation_id": "corr-unit",
        "trace_id": "trace-unit",
        "invocation_id": "inv-1",
        "tool_name": "query_order",
        "status": status,
        "error_code": None,
        "parameter_summary": {"order_id": "ORDER-001"},
        "data": {
            "order": {
                "order_id": "ORDER-001",
                "owner_customer_id": owner,
                "status": "delivered",
                "payment_status": "succeeded",
                "fulfillment_status": "succeeded",
                "currency": "CNY",
                "amount_minor": 100000,
                "snapshot_version": "3",
                "observed_at": "2026-01-01T00:00:00+00:00",
            }
        },
        "error": None,
    }


def error_envelope(code: str, message: str, *, retryable: bool = False) -> dict:
    envelope = order_envelope("error")
    envelope.update(
        {
            "status": "error",
            "error_code": code,
            "data": None,
            "error": {"error_code": code, "message": message, "retryable": retryable},
        }
    )
    return envelope


def make_service(llm, tools, *, timeout_s: float = 15.0, auth: AuthContext | None = None):
    return ChatService(
        llm=llm,
        tools=tools,
        memory=InMemorySessionRepository(),
        prompts=PROMPTS,
        registry_path=REGISTRY_PATH,
        timeout_s=timeout_s,
    )


def run_chat(service: ChatService, message: str, *, auth=None, thread_id: str | None = None):
    auth = auth or make_auth()
    request = ChatRequestDto(trace=make_trace(), message=message, thread_id=thread_id)
    return service.execute(request, auth, session_token="session-token-1"), auth


def order_dispatch(order_id: str, call_id: str = "c") -> LLMResult:
    call = LLMToolCall(call_id=call_id, name="query_order", arguments={"order_id": order_id})
    return LLMResult(tool_calls=(call,))


def payment_dispatch(order_id: str, call_id: str = "c") -> LLMResult:
    call = LLMToolCall(call_id=call_id, name="query_payment", arguments={"order_id": order_id})
    return LLMResult(tool_calls=(call,))


# ---------- State / 目录 / Supervisor ----------


def test_initial_state_matches_spec_74_fields() -> None:
    state = initial_state(user_id="u1", thread_id="t1", trace_id="trace-1", user_question="问题")
    assert state["step_count"] == 1
    assert state["retry_count"] == 0
    assert state["tool_calls"] == [] and state["observations"] == []
    assert state["next_action"] == "dispatch"
    assert state["handoff_required"] is False


def test_catalog_filters_by_scope_and_read_access() -> None:
    catalog = ToolCatalog.load(REGISTRY_PATH, make_auth())
    names = {spec.name for spec in catalog.specs}
    assert "query_order" in names and "query_payment" in names
    assert catalog.is_allowed("query_order") and not catalog.is_allowed("query_unknown")
    assert catalog.access_of("query_order") == "read"
    admin = ToolCatalog.load(REGISTRY_PATH, make_auth(scopes=("audit:read",)))
    assert admin.specs == () and not admin.is_allowed("query_order")


def test_supervisor_dispatches_allowed_tool_with_intent() -> None:
    llm = ScriptedLLM(order_dispatch("ORDER-001", call_id="c1"))
    catalog = ToolCatalog.load(REGISTRY_PATH, make_auth())
    decision, result = decide(
        llm,
        system_prompt=PROMPTS.supervisor_system_prompt(),
        history=[],
        state=initial_state(user_id="u", thread_id="t", trace_id="x", user_question="查订单"),
        tool_specs=list(catalog.specs),
        catalog_is_allowed=catalog.is_allowed,
    )
    assert decision.next_action == "dispatch"
    assert decision.target_capability == "query_order"
    assert decision.arguments == {"order_id": "ORDER-001"}
    assert decision.intent == "order_query"
    assert llm.calls[0]["tools"] and [s.name for s in llm.calls[0]["tools"]] == [
        spec.name for spec in catalog.specs
    ]
    assert result.has_tool_calls


def test_supervisor_rejects_tool_outside_catalog() -> None:
    llm = ScriptedLLM(
        LLMResult(tool_calls=(LLMToolCall(call_id="c", name="refund_request", arguments={}),))
    )
    decision, _ = decide(
        llm,
        system_prompt="s",
        history=[],
        state=initial_state(user_id="u", thread_id="t", trace_id="x", user_question="退款"),
        tool_specs=[],
        catalog_is_allowed=lambda name: False,
    )
    assert decision.next_action == "finish"
    assert decision.reason_code == "tool_not_allowed"
    assert decision.direct_answer is None


def test_supervisor_direct_answer_without_tools() -> None:
    llm = ScriptedLLM(LLMResult(content="您好，请问有什么可以帮您？", model="fake"))
    decision, _ = decide(
        llm,
        system_prompt="s",
        history=[],
        state=initial_state(user_id="u", thread_id="t", trace_id="x", user_question="你好"),
        tool_specs=[],
        catalog_is_allowed=lambda name: False,
    )
    assert decision.next_action == "finish"
    assert decision.direct_answer == "您好，请问有什么可以帮您？"


def test_supervisor_messages_include_history_and_observation_summary() -> None:
    state = initial_state(user_id="u", thread_id="t", trace_id="x", user_question="那支付呢")
    state["observations"] = [
        {"tool_name": "query_order", "outcome": "facts", "summary": "订单摘要"}
    ]
    messages = supervisor_messages(
        "SYS",
        [_chat("user", "帮我查订单")],
        state,
        [],
    )
    assert messages[0].content == "SYS"
    assert messages[-1].content == "那支付呢"
    assert any("[系统观察1] 订单摘要" in m.content for m in messages)
    assert any(m.content == "帮我查订单" for m in messages)


def _chat(role: str, content: str):
    from application.agent.ports.llm import ChatMessage

    return ChatMessage(role=role, content=content)


# ---------- 确定性守卫 ----------


def test_guard_success_produces_facts_and_summary() -> None:
    verdict = evaluate_envelope("query_order", order_envelope())
    assert verdict.outcome == GUARD_FACTS
    assert verdict.data["order"]["order_id"] == "ORDER-001"
    assert "1000.00 CNY" in verdict.summary
    assert not verdict.needs_reflection


def test_guard_empty_payment_unknown() -> None:
    envelope = order_envelope("empty")
    envelope["data"] = {"order_id": "ORDER-001", "payment_status": "unknown"}
    verdict = evaluate_envelope("query_payment", envelope)
    assert verdict.outcome == GUARD_EMPTY
    assert verdict.summary == "该订单暂无支付记录"


def test_guard_denied_invalid_failed_unauthenticated_branches() -> None:
    denied = evaluate_envelope(
        "query_order", error_envelope("RESOURCE_UNAVAILABLE", "资源不存在或不在授权范围内")
    )
    assert denied.outcome == GUARD_DENIED and not denied.needs_reflection
    invalid = evaluate_envelope("query_order", error_envelope("INVALID_ARGUMENT", "参数不合法"))
    assert invalid.outcome == GUARD_INVALID and invalid.needs_reflection
    failed = evaluate_envelope(
        "query_order", error_envelope("DEPENDENCY_UNAVAILABLE", "数据库不可用", retryable=True)
    )
    assert failed.outcome == GUARD_FAILED and failed.needs_reflection
    unauth = evaluate_envelope("query_order", error_envelope("UNAUTHENTICATED", "会话已过期"))
    assert unauth.outcome == GUARD_UNAUTHENTICATED and not unauth.needs_reflection


def test_guard_unknown_status_and_missing_data_fail_closed() -> None:
    unknown = evaluate_envelope("query_order", {"status": "degraded"})
    assert unknown.outcome == GUARD_FAILED
    empty_data = order_envelope("success")
    empty_data["data"] = {}
    assert evaluate_envelope("query_order", empty_data).outcome == GUARD_FAILED


def test_format_money_server_side() -> None:
    assert format_money(100000, "CNY") == "1000.00 CNY"
    assert format_money(0, "USD") == "0.00 USD"


# ---------- Reflection ----------


def test_reflection_invalid_arguments_asks_user_deterministically() -> None:
    verdict = evaluate_envelope("query_order", error_envelope("INVALID_ARGUMENT", "参数不合法"))
    outcome = reflect(ScriptedLLM(), system_prompt="s", verdict=verdict, tool_name="query_order")
    assert outcome.next_action == "ask_user"
    assert outcome.reason_code == "invalid_arguments"


def test_reflection_timeout_finishes_without_llm() -> None:
    verdict = evaluate_envelope(
        "query_order", error_envelope("TIMEOUT", "处理超时", retryable=True)
    )
    llm = ScriptedLLM()
    outcome = reflect(llm, system_prompt="s", verdict=verdict, tool_name="query_order")
    assert outcome.next_action == "finish" and llm.calls == []


def test_reflection_parses_llm_choice_or_falls_back_to_handoff() -> None:
    verdict = evaluate_envelope("query_order", error_envelope("INTERNAL_ERROR", "内部错误"))
    parsed = reflect(
        ScriptedLLM(LLMResult(content='{"next_action": "ask_user", "reason": "缺参数"}')),
        system_prompt="s",
        verdict=verdict,
        tool_name="query_order",
    )
    assert parsed.next_action == "ask_user"
    unparsed = reflect(
        ScriptedLLM(LLMResult(content="我不知如何回答")),
        system_prompt="s",
        verdict=verdict,
        tool_name="query_order",
    )
    assert unparsed.next_action == "handoff"
    raised = reflect(
        ScriptedLLM(),  # 脚本为空 → complete 抛异常
        system_prompt="s",
        verdict=verdict,
        tool_name="query_order",
    )
    assert raised.next_action == "handoff"


# ---------- 工作流闭环（ChatService + Fake 依赖） ----------


def test_chitchat_direct_answer_success_and_memory() -> None:
    llm = ScriptedLLM(LLMResult(content="您好，请问有什么可以帮您？", model="fake"))
    service = make_service(llm, StaticTools(order_envelope()))
    response, auth = run_chat(service, "你好", thread_id="thread-a")
    assert response.status == "success" and response.next_action == "finish"
    assert response.answer == "您好，请问有什么可以帮您？"
    assert response.trace.thread_id == "thread-a"
    history = service._memory.load(auth.tenant_id, "thread-a", subject_id=auth.subject_id)
    assert [(m.role, m.content) for m in history] == [
        ("user", "你好"),
        ("assistant", "您好，请问有什么可以帮您？"),
    ]


def test_order_query_facts_summarized_by_final_answer_prompt() -> None:
    llm = ScriptedLLM(
        LLMResult(
            tool_calls=(
                LLMToolCall(call_id="c1", name="query_order", arguments={"order_id": "ORDER-001"}),
            )
        ),
        LLMResult(content="订单已送达，金额 1000.00 CNY。", model="fake"),
    )
    tools = StaticTools(order_envelope())
    service = make_service(llm, tools)
    response, _ = run_chat(service, "帮我查订单 ORDER-001")
    assert response.status == "success" and response.answer == "订单已送达，金额 1000.00 CNY。"
    assert tools.calls == [("query_order", {"order_id": "ORDER-001"}, "session-token-1", "read")]
    assert len(llm.calls) == 2
    assert llm.calls[1]["system"] == PROMPTS.final_answer
    joined = " ".join(m.content for m in llm.calls[1]["messages"])
    assert "[系统观察]" in joined and "1000.00 CNY" in joined


def test_final_answer_llm_failure_falls_back_to_deterministic_summary() -> None:
    llm = ScriptedLLM(
        payment_dispatch("ORDER-001"),
        LLMResult(content="   ", model="fake"),  # 空回答 → 回退确定性摘要
    )
    envelope = order_envelope("empty")
    envelope["data"] = {"order_id": "ORDER-001", "payment_status": "unknown"}
    envelope["tool_name"] = "query_payment"
    service = make_service(llm, StaticTools(envelope))
    response, _ = run_chat(service, "查支付")
    assert response.status == "success"
    assert response.answer == "该订单暂无支付记录"


def test_invalid_arguments_maps_to_needs_input() -> None:
    llm = ScriptedLLM(
        LLMResult(tool_calls=(LLMToolCall(call_id="c", name="query_order", arguments={}),))
    )
    tools = StaticTools(order_envelope())
    service = make_service(llm, tools)
    response, _ = run_chat(service, "帮我查订单")
    assert response.status == "needs_input" and response.next_action == "ask_user"
    assert len(response.requested_inputs) == 1
    assert response.requested_inputs[0].field == "order_id"
    assert response.answer == "请提供订单号。"
    assert tools.calls == []  # 明知缺参不发起调用，不消耗传输与重试预算
    assert len(llm.calls) == 1  # Reflection 确定性路径未再调用模型


def test_argument_defects_by_registry_schema() -> None:
    schema = {
        "type": "object",
        "required": ["order_id"],
        "properties": {"order_id": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    }
    assert argument_defects(schema, {}) == ["order_id"]
    assert argument_defects(schema, {"order_id": "   "}) == ["order_id"]
    assert argument_defects(schema, {"order_id": "ORDER-1", "extra": 1}) == ["extra"]
    assert argument_defects(schema, {"order_id": "ORDER-1"}) == []
    assert ask_user_prompt_for(["order_id"]) == "请提供订单号。"
    assert ask_user_prompt_for([]) == "请补充查询所需的信息。"


def test_dependency_failure_returns_error_envelope() -> None:
    llm = ScriptedLLM(order_dispatch("ORDER-001"))
    failed_env = error_envelope("DEPENDENCY_UNAVAILABLE", "数据库不可用", retryable=True)
    service = make_service(llm, StaticTools(failed_env))
    response, _ = run_chat(service, "查订单 ORDER-001")
    assert response.status == "error"
    assert response.next_action == "finish"
    assert response.error.error_code == "DEPENDENCY_UNAVAILABLE"
    assert response.error.retryable is True
    assert response.answer == "依赖服务暂不可用，请稍后重试。"


def test_session_expired_maps_to_error_with_handoff() -> None:
    llm = ScriptedLLM(order_dispatch("ORDER-001"))
    service = make_service(llm, StaticTools(error_envelope("UNAUTHENTICATED", "会话已过期")))
    response, _ = run_chat(service, "查订单 ORDER-001")
    assert response.status == "error"
    assert response.error.error_code == "UNAUTHENTICATED"
    assert response.next_action == "handoff" and response.handoff_required is True
    assert response.answer == "登录状态已失效，请重新登录后再试。"


def test_resource_unavailable_keeps_generic_semantics_with_handoff() -> None:
    llm = ScriptedLLM(order_dispatch("ORDER-XYZ"))
    denied_env = error_envelope("RESOURCE_UNAVAILABLE", "资源不存在或不在授权范围内")
    service = make_service(llm, StaticTools(denied_env))
    response, _ = run_chat(service, "查订单 ORDER-XYZ")
    assert response.status == "success"
    assert response.answer == "这个我暂时查不到，我帮您转接人工客服核实一下。"
    assert response.handoff_required is True and response.next_action == "handoff"
    assert "ORDER-XYZ" not in response.answer  # 不泄露资源存在性


def test_hard_timeout_short_circuits_before_llm() -> None:
    llm = ScriptedLLM()
    service = make_service(llm, StaticTools(order_envelope()), timeout_s=0.0)
    response, _ = run_chat(service, "查订单 ORDER-001")
    assert response.status == "error"
    assert response.error.error_code == "TIMEOUT"
    assert response.answer == FALLBACK_TIMEOUT
    assert llm.calls == []  # 超时后不再进入模型决策


def test_tool_outside_catalog_is_never_executed() -> None:
    llm = ScriptedLLM(
        LLMResult(tool_calls=(LLMToolCall(call_id="c", name="refund_request", arguments={}),))
    )
    tools = StaticTools(order_envelope())
    service = make_service(llm, tools, auth=make_auth(scopes=("audit:read",)))
    response, _ = run_chat(service, "帮我退款")
    assert tools.calls == []  # 白名单外不执行
    assert response.status == "success"
    assert response.answer == FALLBACK_HANDOFF
    assert response.handoff_required is True


def test_proxy_customer_outside_data_scope_denied() -> None:
    service = make_service(ScriptedLLM(), StaticTools(order_envelope()))
    request = ChatRequestDto(trace=make_trace(), message="查订单", customer_id="CUSTOMER-OTHER")
    with pytest.raises(AuthorizationDenied) as excinfo:
        service.execute(request, make_auth(), session_token="tok")
    assert excinfo.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE


def test_supervisor_step_limit_forces_handoff() -> None:
    deps = WorkflowDeps(
        llm=ScriptedLLM(),
        tools=StaticTools(order_envelope()),
        catalog=ToolCatalog.load(REGISTRY_PATH, make_auth()),
        prompts=PROMPTS,
        history=[],
        session_token="tok",
        trace=make_trace(),
        max_steps=0,
    )
    state = initial_state(user_id="u", thread_id="t", trace_id="x", user_question="查订单")
    updates = supervisor_node(state, config={"configurable": {"deps": deps}})
    assert updates["next_action"] == "handoff" and updates["handoff_required"] is True
    assert deps.final_draft == FALLBACK_HANDOFF


def test_memory_history_feeds_next_supervisor_turn() -> None:
    llm = ScriptedLLM(
        LLMResult(content="您好，请问有什么可以帮您？", model="fake"),
        LLMResult(content="好的，我明白了。", model="fake"),
    )
    service = make_service(llm, StaticTools(order_envelope()))
    run_chat(service, "第一句", thread_id="thread-m")
    run_chat(service, "第二句", thread_id="thread-m")
    second = llm.calls[1]["messages"]
    assert any(m.role == "user" and m.content == "第一句" for m in second)
    assert any(m.role == "assistant" and m.content == "您好，请问有什么可以帮您？" for m in second)
