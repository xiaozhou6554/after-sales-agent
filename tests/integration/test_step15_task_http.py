"""步骤15：真实 MySQL 任务状态经 HTTP 可信授权查询。"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import jsonschema
import yaml
from bootstrap.api_main import create_app
from bootstrap.settings import load_settings
from bootstrap.worker_main import build_chat_task_handler
from fastapi.testclient import TestClient
from sqlalchemy import text

from application.agent.ports.llm import LLMResult
from application.agent.prompts import PromptCatalog
from application.agent.service import ChatService
from application.common.context import TraceContext
from application.common.tasks.use_cases import TaskUseCases
from infrastructure.agent.memory_adapter import InMemorySessionRepository
from infrastructure.agent.mysql_checkpoint_saver import MySQLCheckpointSaver
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.task_repository import MySQLTaskRepository
from infrastructure.resilience.task_dispatcher import TaskDispatcher


class _CountingLLM:
    def __init__(self):
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        return LLMResult(content="后台答复已完成。")


class _UnusedTools:
    def execute_tool(self, **kwargs):
        raise AssertionError("异步受理不得执行工具")


def test_authorized_task_query_uses_api_response_and_hides_other_actor() -> None:
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "data/contracts/openapi.yaml").read_text(
            encoding="utf-8"
        )
    )
    response_schema = deepcopy(contract["components"]["schemas"]["GetTaskResponse"])
    response_schema["properties"]["data"]["properties"]["task"] = contract["components"]["schemas"][
        "TaskStatus"
    ]
    validator = jsonschema.Draft202012Validator(response_schema)
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    catalog = EventCatalog("data/contracts/event-catalog.json", "data/contracts/events.schema.json")
    repository = MySQLTaskRepository(engine, OutboxRepository(engine, catalog))
    use_cases = TaskUseCases(repository)
    app = create_app(settings, readiness=object(), chat_service=object(), task_service=use_cases)
    owner = app.state.auth.issue_mock_session("cust-north-01")
    other = app.state.auth.issue_mock_session("cust-north-02")
    trace = TraceContext("1.0", f"req-{uuid.uuid4().hex}", "corr-step15", "trace-step15")
    task = use_cases.create(
        auth=owner.auth,
        trace=trace,
        task_type="agent.chat",
        input_masked={"note": "safe"},
        thread_id=f"thread-{uuid.uuid4().hex}",
        idempotency_key=f"idem-{uuid.uuid4().hex}",
        customer_id="CUSTOMER-01",
        checkpoint_ref="checkpoint-step15-http",
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {owner.session_token}"}
            pending = client.get(f"/tasks/{task.task_id}", headers=headers)
            assert pending.status_code == 200
            body = pending.json()
            validator.validate(body)
            assert body["status"] == "pending"
            assert body["next_action"] == "wait"
            assert body["async_task_id"] == task.task_id
            assert body["data"]["task"] == {
                "task_id": task.task_id,
                "status": "pending",
                "result_ref": None,
                "error": None,
                "updated_at": task.updated_at.isoformat(),
            }
            assert client.get(f"/tasks/{task.task_id}").status_code == 401
            hidden = client.get(
                f"/tasks/{task.task_id}",
                headers={"Authorization": f"Bearer {other.session_token}"},
            )
            assert hidden.status_code == 404
            assert hidden.json()["error"]["error_code"] == "RESOURCE_UNAVAILABLE"
            missing = client.get("/tasks/task-does-not-exist", headers=headers)
            assert missing.status_code == 404
            assert missing.json()["error"]["error_code"] == "RESOURCE_UNAVAILABLE"

            lease = repository.claim(task.tenant_id, "agent.chat", "worker-step15-http")
            assert lease is not None
            running = client.get(f"/tasks/{task.task_id}", headers=headers)
            assert running.status_code == 200
            assert running.json()["status"] == "pending"
            assert running.json()["data"]["task"]["status"] == "running"
            repository.complete(lease, "result-step15-http")
            done = client.get(f"/tasks/{task.task_id}", headers=headers)
            assert done.status_code == 200
            validator.validate(done.json())
            assert done.json()["status"] == "success"
            assert done.json()["data"]["task"]["status"] == "succeeded"
            assert done.json()["data"]["task"]["result_ref"] == "result-step15-http"
            assert done.json()["async_task_id"] is None
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outbox_event WHERE tenant_id=:tenant AND aggregate_id=:task"),
                {"tenant": task.tenant_id, "task": task.task_id},
            )
            connection.execute(
                text("DELETE FROM agent_task WHERE tenant_id=:tenant AND task_id=:task"),
                {"tenant": task.tenant_id, "task": task.task_id},
            )
        engine.dispose()


def test_async_chat_accepts_once_and_rejects_changed_request() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    root = Path(__file__).resolve().parents[2]
    catalog = EventCatalog(
        root / "data/contracts/event-catalog.json", root / "data/contracts/events.schema.json"
    )
    repository = MySQLTaskRepository(engine, OutboxRepository(engine, catalog))
    llm = _CountingLLM()
    memory = InMemorySessionRepository()
    service = ChatService(
        llm=llm,
        tools=_UnusedTools(),
        memory=memory,
        prompts=PromptCatalog.load(root / "data/prompts"),
        registry_path=root / "data/contracts/mcp-tools.json",
        checkpointer=MySQLCheckpointSaver(engine),
    )
    app = create_app(
        settings, readiness=object(), chat_service=service, task_service=TaskUseCases(repository)
    )
    owner = app.state.auth.issue_mock_session("cust-north-01")
    headers = {
        "Authorization": f"Bearer {owner.session_token}",
        "Prefer": "respond-async",
        "Idempotency-Key": f"idem-{uuid.uuid4().hex}",
    }
    contract = yaml.safe_load((root / "data/contracts/openapi.yaml").read_text(encoding="utf-8"))
    response_schema = deepcopy(contract["components"]["schemas"]["GetTaskResponse"])
    response_schema["properties"]["data"]["properties"]["task"] = contract["components"]["schemas"][
        "TaskStatus"
    ]
    validator = jsonschema.Draft202012Validator(response_schema)
    try:
        with TestClient(app) as client:
            first = client.post("/chat", json={"message": "我的订单在哪里？"}, headers=headers)
            assert first.status_code == 202
            assert llm.calls == 0
            body = first.json()
            validator.validate(body)
            assert body["status"] == "pending"
            task_id = body["async_task_id"]
            assert task_id
            checkpoint = MySQLCheckpointSaver(engine).latest(
                owner.auth.tenant_id, body["thread_id"]
            )
            assert checkpoint is not None
            assert checkpoint.state["channel_values"]["user_question"] == "我的订单在哪里？"
            repeated = client.post("/chat", json={"message": "我的订单在哪里？"}, headers=headers)
            assert repeated.status_code == 202
            assert repeated.json()["async_task_id"] == task_id
            assert (
                MySQLCheckpointSaver(engine)
                .latest(owner.auth.tenant_id, body["thread_id"])
                .checkpoint_id
                == checkpoint.checkpoint_id
            )
            changed = client.post("/chat", json={"message": "换一个问题"}, headers=headers)
            assert changed.status_code == 409
            assert changed.json()["error"]["error_code"] == "IDEMPOTENCY_CONFLICT"
            missing = client.post(
                "/chat",
                json={"message": "缺少幂等键"},
                headers={k: v for k, v in headers.items() if k != "Idempotency-Key"},
            )
            assert missing.status_code == 400
            assert (
                client.get(f"/tasks/{task_id}", headers=headers).json()["data"]["task"]["status"]
                == "pending"
            )
            worker_memory = InMemorySessionRepository()
            worker_service = ChatService(
                llm=llm,
                tools=_UnusedTools(),
                memory=worker_memory,
                prompts=PromptCatalog.load(root / "data/prompts"),
                registry_path=root / "data/contracts/mcp-tools.json",
                checkpointer=MySQLCheckpointSaver(engine),
            )
            dispatcher = TaskDispatcher(
                repository,
                {"agent.chat": build_chat_task_handler(worker_service, app.state.auth)},
            )
            assert dispatcher.run_one(owner.auth.tenant_id, "agent.chat", "worker-step15-http")
            completed = client.get(f"/tasks/{task_id}", headers=headers)
            assert completed.status_code == 200
            validator.validate(completed.json())
            assert completed.json()["data"]["task"]["status"] == "succeeded"
            assert completed.json()["data"]["task"]["answer"] == "后台答复已完成。"
            assert llm.calls == 1
            assert [
                item.role
                for item in worker_memory.load(
                    owner.auth.tenant_id, body["thread_id"], subject_id=owner.auth.subject_id
                )
            ] == ["user", "assistant"]
            finished_duplicate = client.post(
                "/chat", json={"message": "我的订单在哪里？"}, headers=headers
            )
            assert finished_duplicate.status_code == 200
            assert finished_duplicate.json()["data"]["task"]["task_id"] == task_id
            assert llm.calls == 1

            retry_headers = {**headers, "Idempotency-Key": f"idem-{uuid.uuid4().hex}"}
            retry_thread_id = f"thread-{uuid.uuid4().hex}"
            retry_payload = {"message": "网络恢复后查询", "thread_id": retry_thread_id}
            with patch.object(
                repository._outbox, "enqueue", side_effect=RuntimeError("outbox unavailable")
            ):
                rejected = client.post("/chat", json=retry_payload, headers=retry_headers)
            assert rejected.status_code == 503
            assert rejected.json()["error"]["error_code"] == "DEPENDENCY_UNAVAILABLE"
            assert (
                repository.get_by_idempotency(
                    owner.auth.tenant_id,
                    owner.auth.subject_id,
                    "agent.chat",
                    retry_headers["Idempotency-Key"],
                )
                is None
            )
            resubmitted = client.post("/chat", json=retry_payload, headers=retry_headers)
            assert resubmitted.status_code == 202
            retry_task_id = resubmitted.json()["async_task_id"]
            assert resubmitted.json()["thread_id"] == retry_thread_id
    finally:
        if "task_id" in locals():
            with engine.begin() as connection:
                if "retry_task_id" in locals():
                    connection.execute(
                        text(
                            "DELETE FROM outbox_event WHERE tenant_id=:tenant "
                            "AND aggregate_id=:task"
                        ),
                        {"tenant": owner.auth.tenant_id, "task": retry_task_id},
                    )
                    connection.execute(
                        text("DELETE FROM agent_task WHERE tenant_id=:tenant AND task_id=:task"),
                        {"tenant": owner.auth.tenant_id, "task": retry_task_id},
                    )
                if "retry_thread_id" in locals():
                    connection.execute(
                        text(
                            "DELETE FROM agent_checkpoint WHERE tenant_id=:tenant "
                            "AND thread_id=:thread"
                        ),
                        {"tenant": owner.auth.tenant_id, "thread": retry_thread_id},
                    )
                connection.execute(
                    text("DELETE FROM outbox_event WHERE tenant_id=:tenant AND aggregate_id=:task"),
                    {"tenant": owner.auth.tenant_id, "task": task_id},
                )
                connection.execute(
                    text("DELETE FROM agent_task WHERE tenant_id=:tenant AND task_id=:task"),
                    {"tenant": owner.auth.tenant_id, "task": task_id},
                )
                connection.execute(
                    text(
                        "DELETE FROM agent_checkpoint WHERE tenant_id=:tenant AND thread_id=:thread"
                    ),
                    {"tenant": owner.auth.tenant_id, "thread": body["thread_id"]},
                )
        engine.dispose()
