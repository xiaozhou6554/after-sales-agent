"""步骤15：任务入队前持久暂停，重建图实例后从同一 Checkpoint 继续。"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bootstrap.settings import load_settings
from langgraph.graph import END, START, StateGraph
from sqlalchemy import text

from application.agent.ports.llm import LLMResult
from application.agent.prompts import PromptCatalog
from application.agent.service import ChatService
from application.common.context import AuthContext, DataScope, TraceContext
from application.common.dto import ChatRequestDto
from application.common.tasks.use_cases import TaskUseCases
from infrastructure.agent.memory_adapter import InMemorySessionRepository
from infrastructure.agent.mysql_checkpoint_saver import MySQLCheckpointSaver
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.task_repository import MySQLTaskRepository
from infrastructure.resilience.task_dispatcher import TaskDispatcher, TaskOutcome


def test_paused_graph_continues_after_new_worker_instance() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    tenant = f"step15-pause-{uuid.uuid4().hex[:12]}"
    thread = f"thread-{uuid.uuid4().hex}"
    calls: list[str] = []

    def graph(*, pause: bool):
        builder = StateGraph(dict)

        def execute(state):
            calls.append("executed")
            return {"result": state["question"] + " 已处理"}

        builder.add_node("execute", execute)
        builder.add_edge(START, "execute")
        builder.add_edge("execute", END)
        return builder.compile(
            checkpointer=MySQLCheckpointSaver(engine),
            interrupt_before=["execute"] if pause else None,
        )

    config = {"configurable": {"tenant_id": tenant, "thread_id": thread, "checkpoint_ns": ""}}
    try:
        graph(pause=True).invoke({"question": "订单查询"}, config, durability="sync")
        assert calls == []
        summary = MySQLCheckpointSaver(engine).latest(tenant, thread)
        assert summary is not None
        resumed = graph(pause=False).invoke(None, config, durability="sync")
        assert resumed["result"] == "订单查询 已处理"
        assert calls == ["executed"]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM agent_checkpoint WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
        engine.dispose()


class DirectAnswerLLM:
    def complete(self, *, system, messages, tools=None):
        return LLMResult(content="后台查询已完成。")


class NoTools:
    def execute_tool(self, *args, **kwargs):
        raise AssertionError("直接回答不应调用工具")


def test_chat_task_uses_prepared_checkpoint_and_persistent_lease() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    tenant = f"step15-chat-{uuid.uuid4().hex[:12]}"
    thread = f"thread-{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    auth = AuthContext(
        tenant_id=tenant,
        subject_id="customer-step15",
        roles=("customer",),
        scopes=("order:read",),
        data_scope=DataScope(customer_ids=("CUSTOMER-01",)),
        session_id="session-step15",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    trace = TraceContext("1.0", f"req-{uuid.uuid4().hex}", "corr-step15", "trace-step15")
    request = ChatRequestDto(
        trace=trace, message="你好", thread_id=thread, customer_id="CUSTOMER-01"
    )
    root = Path(__file__).resolve().parents[2]

    def service():
        return ChatService(
            llm=DirectAnswerLLM(),
            tools=NoTools(),
            memory=InMemorySessionRepository(),
            prompts=PromptCatalog.load(root / "data/prompts"),
            registry_path=root / "data/contracts/mcp-tools.json",
            checkpointer=MySQLCheckpointSaver(engine),
        )

    catalog = EventCatalog(
        root / "data/contracts/event-catalog.json", root / "data/contracts/events.schema.json"
    )
    repository = MySQLTaskRepository(engine, OutboxRepository(engine, catalog))
    use_cases = TaskUseCases(repository)
    key = f"idem-{uuid.uuid4().hex}"
    input_digest = {"message_digest": hashlib.sha256(request.message.encode()).hexdigest()}
    task = None
    try:
        checkpoint_ref = service().prepare_async(
            request, auth, thread_id=thread, idempotency_key=key
        )
        assert checkpoint_ref == service().prepare_async(
            request, auth, thread_id=thread, idempotency_key=key
        )
        task = use_cases.create(
            auth=auth,
            trace=trace,
            task_type="agent.chat",
            input_masked=input_digest,
            thread_id=thread,
            idempotency_key=key,
            customer_id="CUSTOMER-01",
            checkpoint_ref=checkpoint_ref,
        )
        existing = use_cases.find_existing(
            auth=auth,
            task_type="agent.chat",
            input_masked=input_digest,
            thread_id=thread,
            idempotency_key=key,
            customer_id="CUSTOMER-01",
        )
        assert existing is not None and existing.task_id == task.task_id
        dispatcher = TaskDispatcher(
            repository,
            {
                "agent.chat": lambda record: TaskOutcome(
                    service().resume_task(record, auth, "token")
                )
            },
        )
        assert dispatcher.run_one(tenant, "agent.chat", "worker-step15")
        done = use_cases.get(auth, task.task_id)
        assert done.status == "succeeded"
        assert done.result_ref
        summary = MySQLCheckpointSaver(engine).latest(tenant, thread)
        assert summary is not None
        assert summary.state["channel_values"]["final_answer"] == "后台查询已完成。"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outbox_event WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
            connection.execute(
                text("DELETE FROM agent_task WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
            connection.execute(
                text("DELETE FROM agent_checkpoint WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
        engine.dispose()
