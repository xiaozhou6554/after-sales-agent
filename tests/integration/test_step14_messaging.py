"""步骤14：真实 MySQL/RabbitMQ 的发布、幂等、租约与死信故障恢复。"""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import replace

import pika
import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.events import parse_event
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.inbox_repository import InboxRepository
from infrastructure.messaging.outbox_publisher import OutboxPublisher
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.messaging.rabbitmq import RabbitMQTransport
from infrastructure.messaging.retry import InboxConsumer
from infrastructure.persistence.mysql.connection import create_mysql_engine
from tests.contract.samples import EVENT_ENVELOPE_TICKET_CREATED


@pytest.fixture()
def messaging_case():
    settings = load_settings()
    marker = uuid.uuid4().hex[:12]
    tenant = f"step14-{marker}"
    table = f"step14_test_effect_{marker}"
    queue = f"step14.audit.{marker}"
    catalog = EventCatalog("data/contracts/event-catalog.json", "data/contracts/events.schema.json")
    catalog.exchange = f"step14.events.{marker}"
    catalog.dead_letter_queue = f"step14.dlq.{marker}"
    catalog.entries = {
        "audit.record.created": {**catalog.entries["audit.record.created"], "queue": queue}
    }
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                f"CREATE TABLE {table} (tenant_id VARCHAR(64) NOT NULL, "
                "business_key VARCHAR(128) NOT NULL, "
                "PRIMARY KEY(tenant_id,business_key)) ENGINE=InnoDB"
            )
        )
    transport = RabbitMQTransport(settings.rabbitmq_url, catalog, retry_delay_ms=40)
    try:
        yield engine, transport, catalog, tenant, table, queue
    finally:
        transport.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM consumer_inbox WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
            connection.execute(
                text("DELETE FROM consumer_business_guard WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            connection.execute(
                text("DELETE FROM outbox_event WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
            connection.execute(text(f"DROP TABLE {table}"))
        engine.dispose()
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


def make_event(tenant: str, *, event_id: str = "evt-a", key: str = "step14-business-key-001"):
    raw = copy.deepcopy(EVENT_ENVELOPE_TICKET_CREATED)
    raw.update(
        {
            "tenant_id": tenant,
            "event_id": event_id,
            "event_type": "audit.record.created",
            "producer": "audit-service",
            "idempotency_key": key,
            "payload": {
                "audit_id": "audit-step14",
                "action": "test",
                "resource_refs": [],
                "status": "pending",
                "actor_subject_id": "step14-test",
                "customer_id": None,
            },
        }
    )
    return parse_event(raw)


def count_effect(engine, table: str, tenant: str) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id=:tenant"), {"tenant": tenant}
        ).scalar_one()


def test_outbox_republish_and_inbox_dual_dedup(messaging_case) -> None:
    engine, transport, catalog, tenant, table, queue = messaging_case
    event = make_event(tenant)
    outbox = OutboxRepository(engine, catalog)
    with engine.begin() as connection:
        outbox.enqueue(connection, event, aggregate_type="audit", aggregate_id="audit-step14")
    assert outbox.pending_tenants("audit-service") == [tenant]
    assert outbox.claim("audit-service", "other-tenant", "publisher-a") == []
    lease = outbox.claim("audit-service", tenant, "publisher-a", lease_s=0)[0]
    transport.publish_event(lease.event)
    time.sleep(0.01)
    republished = OutboxPublisher(outbox, transport, catalog).publish_pending(
        "audit-service", tenant, "publisher-b"
    )
    assert republished == 1
    with pytest.raises(RuntimeError, match="租约"):
        outbox.mark_published(lease)

    def handler(connection, received):
        connection.execute(
            text(f"INSERT INTO {table}(tenant_id,business_key) VALUES (:tenant,:key)"),
            {"tenant": received.tenant_id, "key": received.idempotency_key},
        )
        return "effect-1"

    consumer = InboxConsumer(
        InboxRepository(engine), transport, catalog, {event.event_type: handler}
    )
    assert consumer.poll_once(queue)
    assert consumer.poll_once(queue)
    assert count_effect(engine, table, tenant) == 1
    transport.publish_event(make_event(tenant, event_id="evt-b"))
    assert consumer.poll_once(queue)
    assert count_effect(engine, table, tenant) == 1
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT status,attempts FROM consumer_inbox WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        ).all()
    assert sorted(rows) == [("succeeded", 1), ("succeeded", 1)]


def test_commit_then_ack_lost_and_worker_recreation(messaging_case) -> None:
    engine, transport, catalog, tenant, table, queue = messaging_case
    event = make_event(tenant)
    transport.publish_event(event)
    delivery = transport.get(queue)
    assert delivery is not None
    repository = InboxRepository(engine)
    outcome, lease = repository.claim(event, "audit-archive-consumer", "worker-a")
    assert outcome == "claimed"

    def handler(connection, received):
        connection.execute(
            text(f"INSERT INTO {table}(tenant_id,business_key) VALUES (:tenant,:key)"),
            {"tenant": received.tenant_id, "key": received.idempotency_key},
        )
        return "effect-1"

    repository.complete(lease, handler)
    transport.nack(delivery)
    restarted = InboxConsumer(
        InboxRepository(engine), transport, catalog, {event.event_type: handler}
    )
    assert restarted.poll_once(queue)
    assert count_effect(engine, table, tenant) == 1


def test_four_failed_attempts_go_to_dead_letter(messaging_case) -> None:
    engine, transport, catalog, tenant, _table, queue = messaging_case
    event = make_event(tenant)
    transport.publish_event(event)

    def failing_handler(_connection, _event):
        raise RuntimeError("test failure")

    consumer = InboxConsumer(
        InboxRepository(engine, max_retries=catalog.max_retries),
        transport,
        catalog,
        {event.event_type: failing_handler},
    )
    for _ in range(4):
        deadline = time.monotonic() + 3
        while not consumer.poll_once(queue):
            assert time.monotonic() < deadline
            time.sleep(0.02)
    dead = transport.get(catalog.dead_letter_queue)
    assert dead is not None
    assert parse_event(json.loads(dead.body)).event_id == event.event_id
    assert dead.headers["original_event_id"] == event.event_id
    assert dead.headers["idempotency_key"] == event.idempotency_key
    assert dead.headers["failure_reason"]
    transport.ack(dead)
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status,attempts,error FROM consumer_inbox WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        ).one()
    assert row.status == "dead_letter"
    assert row.attempts == 4
    assert row.error == "RuntimeError"


def test_atomic_rollback_and_failed_publish_remain_retryable(messaging_case) -> None:
    engine, transport, catalog, tenant, table, _queue = messaging_case
    event = make_event(tenant)
    outbox = OutboxRepository(engine, catalog)
    with pytest.raises(RuntimeError, match="rollback"):
        with engine.begin() as connection:
            outbox.enqueue(connection, event, aggregate_type="audit", aggregate_id="audit-step14")
            raise RuntimeError("rollback")
    assert outbox.pending_tenants("audit-service") == []

    with engine.begin() as connection:
        outbox.enqueue(connection, event, aggregate_type="audit", aggregate_id="audit-step14")

    class FailingTransport:
        def publish_event(self, _event):
            raise OSError("broker unavailable")

    assert (
        OutboxPublisher(outbox, FailingTransport(), catalog).publish_pending(
            "audit-service", tenant, "publisher-a"
        )
        == 0
    )
    with engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM outbox_event WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        ).scalar_one()
    assert status == "pending"
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE outbox_event SET next_retry_at=UTC_TIMESTAMP(6) WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
    assert (
        OutboxPublisher(outbox, transport, catalog).publish_pending(
            "audit-service", tenant, "publisher-b"
        )
        == 1
    )

    inbox = InboxRepository(engine)
    outcome, lease = inbox.claim(event, "audit-archive-consumer", "worker-a")
    assert outcome == "claimed"

    def handler(connection, received):
        connection.execute(
            text(f"INSERT INTO {table}(tenant_id,business_key) VALUES (:tenant,:key)"),
            {"tenant": received.tenant_id, "key": received.idempotency_key},
        )
        raise RuntimeError("handler rollback")

    with pytest.raises(RuntimeError, match="handler rollback"):
        inbox.complete(lease, handler)
    assert count_effect(engine, table, tenant) == 0
    assert inbox.fail(lease, "RuntimeError") == "failed"
    outcome, lease2 = inbox.claim(
        replace(event, retry_count=1), "audit-archive-consumer", "worker-b"
    )
    assert outcome == "claimed"

    def recovering_handler(connection, received):
        connection.execute(
            text(f"INSERT INTO {table}(tenant_id,business_key) VALUES (:tenant,:key)"),
            {"tenant": received.tenant_id, "key": received.idempotency_key},
        )
        return "effect-1"

    inbox.complete(lease2, recovering_handler)
    assert count_effect(engine, table, tenant) == 1
