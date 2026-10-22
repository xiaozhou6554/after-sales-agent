"""步骤12 中断恢复集成测试：真实 MySQL 保存器＋真实 LangGraph 引擎。

验收口径（流程步骤12）：中途重启能恢复；未提交节点写入可恢复，不因重启
重复执行已有结果；服务级经 ChatService＋Bootstrap 注入路径闭环。
需要本地 docker MySQL（开发环境已启动）；使用 tenant-step12 隔离数据。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

import pytest
from bootstrap.migrate import build_executor, run_apply
from bootstrap.settings import load_settings
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from application.agent.ports.checkpoint import CheckpointRepositoryPort
from application.agent.ports.llm import LLMResult, LLMToolCall
from application.agent.prompts import PromptCatalog
from application.agent.service import ChatService
from application.common.context import AuthContext, DataScope, TraceContext
from application.common.dto import ChatRequestDto
from infrastructure.agent.memory_adapter import InMemorySessionRepository
from infrastructure.agent.mysql_checkpoint_saver import MySQLCheckpointSaver
from infrastructure.persistence.mysql.connection import create_mysql_engine

TEST_TENANT = "tenant-step12"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPTS = PromptCatalog.load(PROJECT_ROOT / "data" / "prompts")
REGISTRY = PROJECT_ROOT / "data" / "contracts" / "mcp-tools.json"


@pytest.fixture()
def recovery_case():
    settings = load_settings()
    run_apply(build_executor(settings))
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_checkpoint WHERE tenant_id LIKE 'tenant-step12%'"))
    engine.dispose()


# ---------- 引擎级：已完成节点写入恢复后不重跑 ----------


class FlowState(TypedDict, total=False):
    payload: str
    delivered: bool


class ExternalLedger:
    """记录节点实际执行次数，等价于不可重复的外部写。"""

    def __init__(self) -> None:
        self.prepared = 0
        self.delivered = 0
        self.fail_delivery = True

    def reset_attempt(self) -> None:
        self.fail_delivery = False


def build_flow_graph(checkpointer):
    ledger = ExternalLedger()

    def prepare_node(state: FlowState) -> dict:
        ledger.prepared += 1
        return {"payload": "准备完成 13812345678"}

    def deliver_node(state: FlowState) -> dict:
        if ledger.fail_delivery:
            raise RuntimeError("模拟进程中断：投递节点未完成")
        ledger.delivered += 1
        return {"delivered": True}

    builder = StateGraph(FlowState)
    builder.add_node("prepare", prepare_node)
    builder.add_node("deliver", deliver_node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "deliver")
    builder.add_edge("deliver", END)
    graph = builder.compile(checkpointer=checkpointer)
    return graph, ledger


def flow_config(thread: str) -> dict:
    return {
        "configurable": {
            "tenant_id": TEST_TENANT,
            "thread_id": thread,
            "checkpoint_ns": "",
        },
        "recursion_limit": 20,
    }


def test_completed_node_writes_survive_restart_without_reexecution(recovery_case) -> None:
    engine = recovery_case
    graph, ledger = build_flow_graph(MySQLCheckpointSaver(engine))
    with pytest.raises(RuntimeError, match="模拟进程中断"):
        graph.invoke({"payload": "开始"}, flow_config("thread-recover-1"))
    # 中断点：prepare 已完成并持久化，deliver 未完成。
    assert ledger.prepared == 1
    assert ledger.delivered == 0

    # 进程重启：新建保存器与图实例，仅共享同一 MySQL；故障已修复。
    restarted_graph, restarted_ledger = build_flow_graph(MySQLCheckpointSaver(engine))
    assert restarted_ledger is not ledger
    restarted_ledger.reset_attempt()
    final = restarted_graph.invoke(None, flow_config("thread-recover-1"))
    assert final["delivered"] is True
    # 恢复核对：已完成节点写入经 pending_writes 回放，不重复执行外部写。
    assert restarted_ledger.prepared == 0
    assert restarted_ledger.delivered == 1

    saver = MySQLCheckpointSaver(engine)
    summary = saver.latest(TEST_TENANT, "thread-recover-1")
    assert summary is not None
    assert summary.revision >= 1


# ---------- 服务级：经 ChatService 注入路径的中断恢复 ----------


class FailingLLM:
    """首轮模型网关故障：模拟编排中断。"""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system, messages, tools=None):
        self.calls += 1
        raise RuntimeError("模拟模型网关中断")


class ScriptedLLM:
    def __init__(self, *results: LLMResult) -> None:
        self.results = list(results)

    def complete(self, *, system, messages, tools=None):
        if not self.results:
            raise AssertionError("脚本已耗尽")
        return self.results.pop(0)


class CountingTools:
    def __init__(self, envelope: dict) -> None:
        self.envelope = envelope
        self.calls = 0

    def execute_tool(self, tool_name, arguments, *, session_token, trace, access="read"):
        self.calls += 1
        return self.envelope


def make_auth() -> AuthContext:
    return AuthContext(
        tenant_id=TEST_TENANT,
        subject_id="customer-recover-01",
        roles=("customer",),
        scopes=("order:read", "payment:read"),
        data_scope=DataScope(customer_ids=("CUSTOMER-01",)),
        session_id="sess-recover",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )


def order_envelope() -> dict:
    return {
        "schema_version": "1.0",
        "request_id": "req-recover",
        "correlation_id": "corr-recover",
        "trace_id": "trace-recover",
        "invocation_id": "inv-recover",
        "tool_name": "query_order",
        "status": "success",
        "error_code": None,
        "parameter_summary": {"order_id": "ORDER-001"},
        "data": {
            "order": {
                "order_id": "ORDER-001",
                "owner_customer_id": "CUSTOMER-01",
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


def build_chat_service(engine, llm, tools) -> ChatService:
    saver = MySQLCheckpointSaver(engine)
    return ChatService(
        llm=llm,
        tools=tools,
        memory=InMemorySessionRepository(),
        prompts=PROMPTS,
        registry_path=REGISTRY,
        checkpointer=saver,
    )


def run_turn(service: ChatService, message: str, thread_id: str):
    request = ChatRequestDto(
        trace=TraceContext(
            schema_version="1.0",
            request_id=f"req-{thread_id}",
            correlation_id="corr-recover",
            trace_id="trace-recover",
        ),
        message=message,
        thread_id=thread_id,
    )
    return service.execute(request, make_auth(), session_token="session-token-recover")


def test_service_crash_recovery_keeps_tool_executed_once(recovery_case) -> None:
    engine = recovery_case
    thread = "thread-svc-recover"
    failing = FailingLLM()
    tools = CountingTools(order_envelope())
    crashed_service = build_chat_service(engine, failing, tools)

    first = run_turn(crashed_service, "帮我查一下订单 ORDER-001", thread)
    assert first.status == "error"
    assert first.error is not None
    assert first.error.error_code == "DEPENDENCY_UNAVAILABLE"
    assert tools.calls == 0  # 中断发生在模型决策前，工具未执行

    # 重启后（新服务实例＋新保存器实例）恢复：决策重做、工具只执行一次。
    healthy = ScriptedLLM(
        LLMResult(
            tool_calls=(
                LLMToolCall(call_id="c1", name="query_order", arguments={"order_id": "ORDER-001"}),
            )
        ),
        LLMResult(content="您的订单 ORDER-001 已送达。"),
    )
    recovered_tools = CountingTools(order_envelope())
    recovered_service = build_chat_service(engine, healthy, recovered_tools)

    second = run_turn(recovered_service, "帮我查一下订单 ORDER-001", thread)
    assert second.status == "success"
    assert recovered_tools.calls == 1

    saver = MySQLCheckpointSaver(engine)
    summary = saver.latest(TEST_TENANT, thread)
    assert summary is not None
    assert summary.revision >= 1
    # 库内 State 不含明文敏感原文：工具摘要只存名称/状态/错误码（channel_values 层）。
    channel_values = summary.state.get("channel_values") or {}
    tool_calls: list[dict[str, Any]] = channel_values.get("tool_calls") or []
    assert any(item.get("tool_name") == "query_order" for item in tool_calls)


def test_multi_turn_chain_continues_across_service_instances(recovery_case) -> None:
    engine = recovery_case
    thread = "thread-multiturn"

    def fresh_service() -> tuple[ChatService, CountingTools]:
        llm = ScriptedLLM(
            LLMResult(content="您好，有什么可以帮您？"),
        )
        tools = CountingTools(order_envelope())
        return build_chat_service(engine, llm, tools), tools

    service_a, tools_a = fresh_service()
    first = run_turn(service_a, "你好", thread)
    assert first.status == "success"
    assert tools_a.calls == 0  # 寒暄直答不调工具

    service_b, tools_b = fresh_service()
    second = run_turn(service_b, "你好", thread)
    assert second.status == "success"

    saver = MySQLCheckpointSaver(engine)
    summary = saver.latest(TEST_TENANT, thread)
    assert summary is not None
    # 第二轮在新实例上沿同一链继续：版本严格增长，不产生分叉冲突。
    assert summary.revision >= 2
    port: CheckpointRepositoryPort = saver
    port.delete_checkpoints(TEST_TENANT, thread)
    assert saver.latest(TEST_TENANT, thread) is None
