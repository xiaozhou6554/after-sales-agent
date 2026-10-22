"""Rollback-only refund review across real RabbitMQ delivery and Inbox retry."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pika
from bootstrap.settings import load_settings
from sqlalchemy import text
from tests.acceptance.test_tc_69_74_refund_mysql import _insert_pre_shipment_facts

from application.common.context import AuthContext, DataScope, TraceContext
from application.common.events import parse_event
from application.refund.confirmation import RefundConfirmationUseCase
from application.refund.qualification import RefundQualificationUseCase
from application.refund.submission import RefundSubmissionUseCase
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.inbox_repository import InboxRepository
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.messaging.rabbitmq import RabbitMQTransport
from infrastructure.messaging.retry import InboxConsumer
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.refund_quote_repository import (
    MySqlRefundQuoteRepository,
)
from infrastructure.refund.review_handler import MockRefundReviewHandler
from infrastructure.refund.submission_adapter import MySqlMockRefundSubmissionAdapter
from infrastructure.security.confirmation_store import MySQLConfirmationStore


def test_tc75_broker_retry_rolls_back_crashed_review_and_approves_once() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    connection = engine.connect()
    transaction = connection.begin()
    marker = uuid.uuid4().hex[:12]
    queue = f"test.refund.review.{marker}"
    catalog = EventCatalog("data/contracts/event-catalog.json", "data/contracts/events.schema.json")
    business_catalog = EventCatalog(
        "data/contracts/event-catalog.json", "data/contracts/events.schema.json"
    )
    catalog.exchange = f"test.refund.events.{marker}"
    catalog.dead_letter_queue = f"test.refund.dlq.{marker}"
    catalog.entries = {"refund.confirmed": {**catalog.entries["refund.confirmed"], "queue": queue}}
    transport = None
    try:
        _insert_pre_shipment_facts(connection)
        transport = RabbitMQTransport(settings.rabbitmq_url, catalog, retry_delay_ms=40)
        now = datetime.now(timezone.utc)
        auth = AuthContext(
            tenant_id="tenant-north",
            subject_id="tc75-customer",
            roles=("customer",),
            scopes=("refund:read", "refund:request"),
            data_scope=DataScope(customer_ids=("CUSTOMER-01",)),
            session_id="tc75",
            issued_at=now,
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        trace = TraceContext("1.0", "tc75-request", "tc75-correlation", "tc75-trace", "tc75-thread")
        snapshots = MockRefundSnapshotAdapter(engine).bind(connection)
        quotes = MySqlRefundQuoteRepository(engine).bind(connection)
        _, quote = RefundQualificationUseCase(snapshots, quotes).calculate_quote(
            auth, "AT-TC-69-ORDER", "changed_mind"
        )
        assert quote is not None
        service = RefundSubmissionUseCase(
            quotes,
            MySqlMockRefundSubmissionAdapter(
                engine, snapshots, OutboxRepository(engine, business_catalog)
            ).bind(connection),
        )
        flow = RefundConfirmationUseCase(service, MySQLConfirmationStore(engine).bind(connection))
        prepared = flow.prepare(auth, quote.quote_id)
        flow.verify(auth, prepared.record.confirmation_id, prepared.mock_challenge)
        created = service.submit(
            auth, quote.quote_id, prepared.record.confirmation_id, "tc75-idem", trace
        )
        raw = connection.execute(
            text(
                "SELECT payload FROM outbox_event WHERE tenant_id=:tenant "
                "AND aggregate_type='refund_request' AND aggregate_id=:request "
                "AND event_type='refund.confirmed'"
            ),
            {"tenant": auth.tenant_id, "request": created.refund_request_id},
        ).scalar_one()
        event = parse_event(json.loads(raw))
        transport.publish_event(event)

        class NestedEngine:
            @contextmanager
            def begin(self):
                with connection.begin_nested():
                    yield connection

        real_handler = MockRefundReviewHandler(
            snapshots, OutboxRepository(engine, business_catalog)
        )
        attempts = 0

        def crash_once(conn, received):
            nonlocal attempts
            attempts += 1
            real_handler(conn, received)
            if attempts == 1:
                raise RuntimeError("synthetic crash after business review")
            return created.refund_request_id

        consumer = InboxConsumer(
            InboxRepository(NestedEngine()),
            transport,
            catalog,
            {"refund.confirmed": crash_once},
            worker_id="tc75-worker",
        )
        assert consumer.poll_once(queue)
        assert (
            connection.execute(
                text(
                    "SELECT review_status FROM refund_request WHERE tenant_id=:tenant "
                    "AND refund_request_id=:request"
                ),
                {"tenant": auth.tenant_id, "request": created.refund_request_id},
            ).scalar_one()
            == "pending_review"
        )
        deadline = time.monotonic() + 3
        while attempts < 2:
            assert time.monotonic() < deadline
            consumer.poll_once(queue)
            time.sleep(0.02)
        assert (
            connection.execute(
                text(
                    "SELECT review_status FROM refund_request WHERE tenant_id=:tenant "
                    "AND refund_request_id=:request"
                ),
                {"tenant": auth.tenant_id, "request": created.refund_request_id},
            ).scalar_one()
            == "approved"
        )
        transport.publish_event(event)
        assert consumer.poll_once(queue)
        assert attempts == 2
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_event WHERE tenant_id=:tenant "
                    "AND aggregate_id=:request AND event_type='refund.approved'"
                ),
                {"tenant": auth.tenant_id, "request": created.refund_request_id},
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM mock_pay_refund_execution "
                    "WHERE tenant_id=:tenant AND order_id=:order"
                ),
                {"tenant": auth.tenant_id, "order": quote.order_id},
            ).scalar_one()
            == 0
        )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
        if transport is not None:
            transport.close()
            broker = pika.BlockingConnection(pika.URLParameters(settings.rabbitmq_url))
            try:
                channel = broker.channel()
                channel.queue_delete(queue=queue)
                channel.queue_delete(queue=f"{queue}.retry")
                channel.queue_delete(queue=catalog.dead_letter_queue)
                channel.exchange_delete(exchange=catalog.exchange)
                channel.exchange_delete(exchange=f"{catalog.exchange}.retry")
            finally:
                broker.close()
