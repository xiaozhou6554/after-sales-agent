"""步骤15：任务与Outbox原子写入、幂等、租约接管及受权查询。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pika
import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, DataScope, TraceContext
from application.common.events import parse_event
from application.common.tasks.models import TaskConflictError, TaskLeaseLostError
from application.common.tasks.use_cases import TaskUseCases
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.inbox_repository import InboxRepository
from infrastructure.messaging.outbox_publisher import OutboxPublisher
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.messaging.rabbitmq import RabbitMQTransport
from infrastructure.messaging.retry import InboxConsumer
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.task_repository import MySQLTaskRepository
from infrastructure.resilience.task_dispatcher import (
    TaskDispatcher,
    TaskOutcome,
    task_created_handler,
)


@pytest.fixture()
def task_case():
    settings = load_settings()
    tenant = f"step15-{uuid.uuid4().hex[:12]}"
    table = f"step15_test_effect_{uuid.uuid4().hex[:12]}"
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    catalog = EventCatalog("data/contracts/event-catalog.json", "data/contracts/events.schema.json")
    repository = MySQLTaskRepository(engine, OutboxRepository(engine, catalog))
    with engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE TABLE {table} (tenant_id VARCHAR(64) NOT NULL, "
                "task_id VARCHAR(64) NOT NULL, PRIMARY KEY(tenant_id,task_id)) ENGINE=InnoDB"
            )
        )
    try:
        yield tenant, table, engine, repository, TaskUseCases(repository)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outbox_event WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
            connection.execute(
                text("DELETE FROM agent_task WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
            connection.execute(text(f"DROP TABLE {table}"))
        engine.dispose()


def auth_for(tenant: str, subject: str = "actor-a", customer: str = "customer-a"):
    now = datetime.now(timezone.utc)
    return AuthContext(
        tenant,
        subject,
        ("support",),
        ("customer:read",),
        DataScope(customer_ids=(customer,)),
        "session-step15",
        now - timedelta(seconds=1),
        now + timedelta(hours=1),
    )


def submit(use_cases, auth, *, key="step15-idempotency-0001", note="safe", due=None):
    return use_cases.create(
        auth=auth,
        trace=TraceContext("1.0", "req-step15", "corr-step15", "trace-step15"),
        task_type="agent.chat",
        input_masked={"note": note, "phone": "13812345678"},
        thread_id="thread-step15",
        idempotency_key=key,
        customer_id="customer-a",
        checkpoint_ref="checkpoint-step15",
        deadline_at=due,
    )


def test_task_and_outbox_atomic_idempotent_and_authorized(task_case) -> None:
    tenant, _table, engine, repository, use_cases = task_case
    auth = auth_for(tenant)
    first = submit(use_cases, auth)
    second = submit(use_cases, auth)
    assert first.task_id == second.task_id
    assert first.status == "pending"
    assert first.input_masked == {
        "checkpoint_ref": "checkpoint-step15",
        "request_digest": first.input_masked["request_digest"],
        "request_id": "req-step15",
        "correlation_id": "corr-step15",
        "trace_id": "trace-step15",
    }
    with pytest.raises(TaskConflictError):
        submit(use_cases, auth, note="other")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM agent_task WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM outbox_event WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            ).scalar_one()
            == 1
        )
    assert use_cases.get(auth, first.task_id).task_id == first.task_id
    for denied in (
        auth_for(tenant, subject="actor-b"),
        auth_for(tenant, customer="customer-b"),
        auth_for("other-tenant"),
    ):
        with pytest.raises(AuthorizationDenied):
            use_cases.get(denied, first.task_id)
    assert repository.get("other-tenant", first.task_id) is None


def test_expired_lease_reclaimed_and_old_worker_cannot_commit(task_case) -> None:
    tenant, table, engine, repository, use_cases = task_case
    task = submit(use_cases, auth_for(tenant))
    first = repository.claim(tenant, "agent.chat", "worker-a", lease_s=0)
    assert first is not None
    assert repository.claim("other-tenant", "agent.chat", "worker-b") is None
    catalog = EventCatalog("data/contracts/event-catalog.json", "data/contracts/events.schema.json")
    restarted = MySQLTaskRepository(engine, OutboxRepository(engine, catalog))
    second = restarted.claim(tenant, "agent.chat", "worker-b")
    assert second is not None
    assert second.task.task_id == task.task_id
    assert second.fencing_token > first.fencing_token
    with pytest.raises(TaskLeaseLostError):
        repository.complete(first, "stale")

    def effect(connection, current):
        connection.execute(
            text(f"INSERT INTO {table}(tenant_id,task_id) VALUES (:tenant,:task_id)"),
            {"tenant": current.tenant_id, "task_id": current.task_id},
        )

    completed = restarted.complete(second, "result-step15", effect)
    assert completed.status == "succeeded"
    assert completed.result_ref == "result-step15"
    assert restarted.claim(tenant, "agent.chat", "worker-c") is None
    with engine.connect() as connection:
        assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 1


def test_expired_task_fails_without_claiming(task_case) -> None:
    tenant, _table, _engine, repository, use_cases = task_case
    task = submit(
        use_cases,
        auth_for(tenant),
        key="step15-idempotency-expired",
        due=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    assert repository.claim(tenant, "agent.chat", "worker-a") is None
    stored = repository.get(tenant, task.task_id)
    assert stored.status == "failed"
    assert stored.error == "TASK_DEADLINE_EXCEEDED"


def test_dispatcher_recovers_same_task_after_instance_recreation(task_case) -> None:
    tenant, table, engine, repository, use_cases = task_case
    task = submit(use_cases, auth_for(tenant))
    old_lease = repository.claim(tenant, "agent.chat", "worker-a", lease_s=0)
    assert old_lease is not None
    catalog = EventCatalog("data/contracts/event-catalog.json", "data/contracts/events.schema.json")
    restarted = MySQLTaskRepository(engine, OutboxRepository(engine, catalog))

    def handler(record):
        assert record.task_id == task.task_id

        def effect(connection, current):
            connection.execute(
                text(f"INSERT INTO {table}(tenant_id,task_id) VALUES (:tenant,:task_id)"),
                {"tenant": current.tenant_id, "task_id": current.task_id},
            )

        return TaskOutcome("result-step15", effect)

    dispatcher = TaskDispatcher(restarted, {"agent.chat": handler})
    assert dispatcher.run_one(tenant, "agent.chat", "worker-b")
    assert not dispatcher.run_one(tenant, "agent.chat", "worker-b")
    assert use_cases.get(auth_for(tenant), task.task_id).status == "succeeded"
    with engine.connect() as connection:
        assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 1


def test_task_event_replay_runs_single_task_with_real_broker(task_case) -> None:
    tenant, table, engine, _repository, use_cases = task_case
    task = submit(use_cases, auth_for(tenant))
    suffix = tenant.split("-")[-1]
    queue = f"step15.task.{suffix}"
    settings = load_settings()
    catalog = EventCatalog("data/contracts/event-catalog.json", "data/contracts/events.schema.json")
    catalog.exchange = f"step15.events.{suffix}"
    catalog.dead_letter_queue = f"step15.dlq.{suffix}"
    catalog.entries = {
        "agent.task.created": {**catalog.entries["agent.task.created"], "queue": queue}
    }
    transport = RabbitMQTransport(settings.rabbitmq_url, catalog, retry_delay_ms=40)
    outbox = OutboxRepository(engine, catalog)
    task_repo = MySQLTaskRepository(engine, outbox)
    try:
        assert (
            OutboxPublisher(outbox, transport, catalog).publish_pending(
                "agent-service", tenant, "publisher-step15"
            )
            == 1
        )
        consumer = InboxConsumer(
            InboxRepository(engine),
            transport,
            catalog,
            {"agent.task.created": task_created_handler},
        )
        assert consumer.poll_once(queue)

        def handler(record):
            def effect(connection, current):
                connection.execute(
                    text(f"INSERT INTO {table}(tenant_id,task_id) VALUES (:tenant,:task_id)"),
                    {"tenant": current.tenant_id, "task_id": current.task_id},
                )

            return TaskOutcome("result-step15", effect)

        dispatcher = TaskDispatcher(task_repo, {"agent.chat": handler})
        assert dispatcher.run_one(tenant, "agent.chat", "worker-step15")
        with engine.connect() as connection:
            raw = connection.execute(
                text("SELECT payload FROM outbox_event WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            ).scalar_one()
        transport.publish_event(parse_event(json.loads(raw)))
        assert consumer.poll_once(queue)
        assert not dispatcher.run_one(tenant, "agent.chat", "worker-step15")
        assert use_cases.get(auth_for(tenant), task.task_id).status == "succeeded"
        with engine.connect() as connection:
            assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 1
    finally:
        transport.close()
        connection = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
        try:
            channel = connection.channel()
            channel.queue_delete(queue=queue)
            channel.queue_delete(queue=f"{queue}.retry")
            channel.queue_delete(queue=catalog.dead_letter_queue)
            channel.exchange_delete(exchange=catalog.exchange)
            channel.exchange_delete(exchange=f"{catalog.exchange}.retry")
        finally:
            connection.close()
