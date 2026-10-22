"""步骤23：授权复杂只读计划按依赖顺序执行，越权计划不执行。"""

from application.agent.planning import validate_tool_plan
from application.agent.ports.llm import LLMResult, LLMToolCall
from application.agent.state import initial_state
from application.agent.tool_catalog import ToolCatalog
from application.agent.workflow.graph import WorkflowDeps, build_chat_graph, plan_executor_node
from tests.unit.test_step10_agent_application import (
    PROMPTS,
    REGISTRY_PATH,
    ScriptedLLM,
    make_auth,
    make_service,
    make_trace,
    order_envelope,
    run_chat,
)


class PlannedTools:
    def __init__(self):
        self.calls = []

    def execute_tool(self, tool_name, arguments, *, session_token, trace, access="read"):
        self.calls.append((tool_name, dict(arguments), access))
        if tool_name == "query_order":
            return order_envelope()
        if tool_name == "query_payment":
            return {
                "status": "success",
                "error_code": None,
                "error": None,
                "data": {
                    "order_id": "ORDER-001",
                    "payment_status": "succeeded",
                    "paid_minor": 100000,
                    "refunded_minor": 0,
                    "currency": "CNY",
                    "payment_ids": ["PAY-001"],
                },
            }
        if tool_name == "query_logistics":
            return {
                "status": "success",
                "error_code": None,
                "error": None,
                "data": {"order_id": "ORDER-001", "shipments": []},
            }
        raise AssertionError("计划调用了未知工具")


def _plan(*names):
    return LLMResult(
        tool_calls=tuple(
            LLMToolCall(f"task-{index}", name, {"order_id": "ORDER-001"})
            for index, name in enumerate(names)
        )
    )


def test_two_authorized_read_tasks_run_in_order_then_summarize() -> None:
    llm = ScriptedLLM(
        _plan("query_order", "query_payment"), LLMResult(content="订单已送达，支付成功。")
    )
    tools = PlannedTools()
    response, _ = run_chat(
        make_service(llm, tools),
        "查询 ORDER-001 的订单和支付状态",
        thread_id="step23-plan-order-payment",
    )
    assert response.status == "success"
    assert [call[0] for call in tools.calls] == ["query_order", "query_payment"]
    assert len(llm.calls) == 2  # Planner/Supervisor 一次，最终总结一次


def test_plan_cannot_add_unauthorized_write_tool() -> None:
    llm = ScriptedLLM(
        _plan("query_order", "refund_request"),
        _plan("query_order", "refund_request"),
    )
    tools = PlannedTools()
    response, _ = run_chat(
        make_service(llm, tools),
        "查订单并发起退款",
        thread_id="step23-plan-write-rejected",
    )
    assert response.handoff_required
    assert tools.calls == []
    assert len(llm.calls) == 2


def test_invalid_plan_can_replan_once_to_authorized_read_tasks() -> None:
    llm = ScriptedLLM(
        _plan("query_order", "refund_request"),
        _plan("query_order", "query_payment"),
        LLMResult(content="订单已送达，支付成功。"),
    )
    tools = PlannedTools()
    response, _ = run_chat(
        make_service(llm, tools),
        "查询订单与支付",
        thread_id="step23-one-replan",
    )
    assert response.status == "success"
    assert [call[0] for call in tools.calls] == ["query_order", "query_payment"]


def test_plan_stops_at_shared_agent_step_budget() -> None:
    llm = ScriptedLLM(_plan("query_order", "query_payment"))
    tools = PlannedTools()
    service = make_service(llm, tools)
    service._max_steps = 1
    response, _ = run_chat(service, "查询订单和支付", thread_id="step23-plan-budget")
    assert response.handoff_required
    assert tools.calls == []


def test_invalid_later_task_arguments_cannot_partially_execute_plan() -> None:
    first = LLMToolCall("task-0", "query_order", {"order_id": "ORDER-001"})
    second = LLMToolCall("task-1", "query_payment", {})
    llm = ScriptedLLM(
        LLMResult(tool_calls=(first, second)),
        LLMResult(tool_calls=(first, second)),
    )
    tools = PlannedTools()
    response, _ = run_chat(
        make_service(llm, tools), "查订单和支付", thread_id="step23-plan-bad-args"
    )
    assert response.handoff_required
    assert tools.calls == []


def test_order_then_logistics_uses_authorized_short_plan() -> None:
    llm = ScriptedLLM(_plan("query_order", "query_logistics"))
    tools = PlannedTools()
    response, _ = run_chat(
        make_service(llm, tools),
        "查 ORDER-001 的订单和物流",
        auth=make_auth(scopes=("order:read", "logistics:read")),
        thread_id="step23-order-logistics",
    )
    assert response.status == "success"
    assert [call[0] for call in tools.calls] == ["query_order", "query_logistics"]
    assert len(llm.calls) == 1  # 物流事实走确定性摘要，不再额外生成 ETA


def test_plan_is_bounded_and_has_explicit_sequential_dependencies() -> None:
    calls = _plan("query_order", "query_payment").tool_calls
    tasks = validate_tool_plan(calls, is_allowed=lambda name: True, max_tasks=8)
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == [tasks[0]["task_id"]]


def test_plan_and_decisions_are_checkpoint_state_auditable() -> None:
    llm = ScriptedLLM(
        _plan("query_order", "query_payment"),
        LLMResult(content="订单已送达，支付成功。"),
    )
    deps = WorkflowDeps(
        llm=llm,
        tools=PlannedTools(),
        catalog=ToolCatalog.load(REGISTRY_PATH, make_auth()),
        prompts=PROMPTS,
        history=[],
        session_token="session-token-1",
        trace=make_trace(),
    )
    result = build_chat_graph().invoke(
        initial_state(
            user_id="customer-north-01",
            thread_id="plan-audit",
            trace_id="trace-unit",
            user_question="查询订单和支付",
        ),
        config={"configurable": {"deps": deps}, "recursion_limit": 50},
    )
    assert result["plan"] == ["query_order", "query_payment"]
    assert result["plan_cursor"] == 2
    assert result["step_count"] == 2
    assert all(call["reason_code"] == "authorized_plan_step" for call in result["tool_calls"])
    assert len({call["decision_id"] for call in result["tool_calls"]}) == 2


def test_resumed_plan_cannot_skip_unverified_dependency() -> None:
    tasks = validate_tool_plan(
        _plan("query_order", "query_payment").tool_calls,
        is_allowed=lambda name: True,
        max_tasks=8,
    )
    state = initial_state(
        user_id="u1", thread_id="t1", trace_id="tr1", user_question="查询订单和支付"
    )
    state["plan_tasks"] = list(tasks)
    state["plan_cursor"] = 1
    state["step_count"] = 2
    deps = WorkflowDeps(
        llm=ScriptedLLM(),
        tools=PlannedTools(),
        catalog=ToolCatalog.load(REGISTRY_PATH, make_auth()),
        prompts=PROMPTS,
        history=[],
        session_token="session-token-1",
        trace=make_trace(),
    )
    update = plan_executor_node(state, {"configurable": {"deps": deps}})
    assert update["handoff_required"] is True
    assert update["next_action"] == "finish"
