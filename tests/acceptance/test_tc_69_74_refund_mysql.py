"""Rollback-only synthetic acceptance: pre-shipment quote and review are not payment."""

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.context import AuthContext, DataScope, TraceContext
from application.common.events import parse_event
from application.refund.confirmation import RefundConfirmationUseCase
from application.refund.qualification import RefundQualificationUseCase
from application.refund.submission import RefundSubmissionUseCase
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.inbox_repository import InboxRepository
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.refund_quote_repository import (
    MySqlRefundQuoteRepository,
)
from infrastructure.refund.review_handler import MockRefundReviewHandler
from infrastructure.refund.submission_adapter import MySqlMockRefundSubmissionAdapter
from infrastructure.security.confirmation_store import MySQLConfirmationStore


def _insert_pre_shipment_facts(connection) -> None:
    common = {"tenant_id": "tenant-north", "order_id": "AT-TC-69-ORDER"}
    connection.execute(
        text(
            "INSERT INTO mock_oms_order"
            " (tenant_id,order_id,customer_id,status,currency,paid_minor,version)"
            " VALUES (:tenant_id,:order_id,'CUSTOMER-01','pending_shipment','CNY',11000,1)"
        ),
        common,
    )
    connection.execute(
        text(
            "INSERT INTO mock_oms_order_line"
            " (tenant_id,order_id,line_id,source_product_id,quantity,goods_minor,shipping_minor)"
            " VALUES (:tenant_id,:order_id,'AT-TC-69-LINE','P-100',1,10000,1000)"
        ),
        common,
    )
    connection.execute(
        text(
            "INSERT INTO mock_pay_payment"
            " (tenant_id,payment_id,order_id,paid_minor,currency,status)"
            " VALUES (:tenant_id,'AT-TC-69-PAY',:order_id,11000,'CNY','succeeded')"
        ),
        common,
    )
    connection.execute(
        text(
            "INSERT INTO mock_pay_payment_detail"
            " (tenant_id,payment_detail_id,payment_id,order_id,line_id,product_id,"
            " paid_goods_minor,paid_shipping_minor,paid_minor,paid_at,detail_sequence)"
            " VALUES (:tenant_id,'AT-TC-69-DETAIL','AT-TC-69-PAY',:order_id,"
            " 'AT-TC-69-LINE','product-air-01',10000,1000,11000,"
            " '2026-10-19 04:00:00',1)"
        ),
        common,
    )


@pytest.mark.parametrize("refund_detail_status", ["succeeded", "pending"])
def test_tc70_delivered_partial_refund_quotes_only_remaining_goods(
    refund_detail_status: str,
) -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        _insert_pre_shipment_facts(connection)
        now = datetime.now(timezone.utc)
        params = {"t": "tenant-north", "o": "AT-TC-69-ORDER"}
        connection.execute(
            text("UPDATE mock_oms_order SET status='delivered' WHERE tenant_id=:t AND order_id=:o"),
            params,
        )
        connection.execute(
            text(
                "INSERT INTO mock_oms_shipment_batch"
                " (tenant_id,order_id,batch_id,quantity,signed_at)"
                " VALUES (:t,:o,'AT-TC-70-SHIP',1,:signed_at)"
            ),
            {**params, "signed_at": now.replace(tzinfo=None)},
        )
        connection.execute(
            text(
                "INSERT INTO mock_pay_refund_execution"
                " (tenant_id,refund_id,order_id,payment_id,amount_minor,currency,status,"
                "idempotency_key) VALUES (:t,'AT-TC-70-REFUND',:o,'AT-TC-69-PAY',"
                "2000,'CNY','succeeded','AT-TC-70-REFUND-KEY')"
            ),
            params,
        )
        connection.execute(
            text(
                "INSERT INTO mock_pay_refund_detail"
                " (tenant_id,refund_detail_id,payment_detail_id,refund_id,status,"
                "refunded_goods_minor,currency) VALUES (:t,'AT-TC-70-REFUND-DETAIL',"
                "'AT-TC-69-DETAIL','AT-TC-70-REFUND',:detail_status,2000,'CNY')"
            ),
            {**params, "detail_status": refund_detail_status},
        )
        auth = AuthContext(
            tenant_id="tenant-north",
            subject_id="at-tc70-customer",
            roles=("customer",),
            scopes=("refund:read", "refund:request"),
            data_scope=DataScope(customer_ids=("CUSTOMER-01",)),
            session_id="at-tc70",
            issued_at=now,
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        qualification = RefundQualificationUseCase(
            MockRefundSnapshotAdapter(engine, connection, as_of=now.date()),
            MySqlRefundQuoteRepository(engine).bind(connection),
        )
        assessment, quote = qualification.calculate_quote(auth, "AT-TC-69-ORDER", "changed_mind")
        if refund_detail_status == "succeeded":
            assert assessment.eligibility == "manual_review"
            assert quote is not None
            assert (quote.goods_minor, quote.shipping_minor, quote.amount_minor) == (8000, 0, 8000)
            assert [
                (line.line_id, line.quantity, line.goods_minor, line.shipping_minor)
                for line in quote.lines
            ] == [("AT-TC-69-LINE", 1, 8000, 0)]
        else:
            assert assessment.eligibility == "unknown"
            assert quote is None
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM refund_request WHERE tenant_id=:t AND order_id=:o"),
                params,
            ).scalar_one()
            == 0
        )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.mark.parametrize(
    "source_change,expected_status,expected_revision",
    [("none", "approved", 2), ("version", "reviewing", 1), ("amount", "reviewing", 1)],
)
def test_tc69_and_tc74_pre_shipment_review_is_not_a_refund_execution(
    source_change: str, expected_status: str, expected_revision: int
) -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        _insert_pre_shipment_facts(connection)
        now = datetime.now(timezone.utc)
        auth = AuthContext(
            tenant_id="tenant-north",
            subject_id="at-tc69-customer",
            roles=("customer",),
            scopes=("refund:read", "refund:request"),
            data_scope=DataScope(customer_ids=("CUSTOMER-01",)),
            session_id="at-tc69",
            issued_at=now,
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        trace = TraceContext(
            "1.0", "at-tc69-request", "at-tc69-correlation", "at-tc69-trace", "at-tc69-thread"
        )
        snapshots = MockRefundSnapshotAdapter(engine).bind(connection)
        quotes = MySqlRefundQuoteRepository(engine).bind(connection)
        qualification = RefundQualificationUseCase(snapshots, quotes)
        missing_reason, missing_quote = qualification.calculate_quote(auth, "AT-TC-69-ORDER", None)
        assert missing_reason.reason_code == "REASON_REQUIRED" and missing_quote is None
        quality, quality_quote = qualification.calculate_quote(
            auth, "AT-TC-69-ORDER", "quality_issue"
        )
        assert not quality.can_submit and quality_quote is None
        connection.execute(
            text("UPDATE mock_oms_order SET paid_minor=10999 WHERE tenant_id=:t AND order_id=:o"),
            {"t": auth.tenant_id, "o": "AT-TC-69-ORDER"},
        )
        inconsistent, inconsistent_quote = qualification.calculate_quote(
            auth, "AT-TC-69-ORDER", "changed_mind"
        )
        assert not inconsistent.can_submit and inconsistent_quote is None
        connection.execute(
            text("UPDATE mock_oms_order SET paid_minor=11000 WHERE tenant_id=:t AND order_id=:o"),
            {"t": auth.tenant_id, "o": "AT-TC-69-ORDER"},
        )
        assessment, quote = qualification.calculate_quote(auth, "AT-TC-69-ORDER", "changed_mind")
        assert assessment.eligibility == "eligible" and assessment.can_submit
        assert quote is not None
        assert (quote.goods_minor, quote.shipping_minor, quote.amount_minor) == (10000, 1000, 11000)
        assert len(quote.lines) == 1 and quote.lines[0].quantity == 1
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM refund_request WHERE tenant_id=:t AND order_id=:o"),
                {"t": auth.tenant_id, "o": quote.order_id},
            ).scalar_one()
            == 0
        )
        catalog = EventCatalog(
            "data/contracts/event-catalog.json", "data/contracts/events.schema.json"
        )
        service = RefundSubmissionUseCase(
            quotes,
            MySqlMockRefundSubmissionAdapter(
                engine, snapshots, OutboxRepository(engine, catalog)
            ).bind(connection),
        )
        confirmations = MySQLConfirmationStore(engine).bind(connection)
        flow = RefundConfirmationUseCase(service, confirmations)
        prepared = flow.prepare(auth, quote.quote_id)
        flow.verify(auth, prepared.record.confirmation_id, prepared.mock_challenge)
        created = service.submit(
            auth, quote.quote_id, prepared.record.confirmation_id, "at-tc69-idem", trace
        )
        assert created.status == "pending_review"
        if source_change == "version":
            connection.execute(
                text(
                    "UPDATE mock_oms_order SET version=version+1 WHERE tenant_id=:t AND order_id=:o"
                ),
                {"t": auth.tenant_id, "o": quote.order_id},
            )
        if source_change == "amount":
            connection.execute(
                text(
                    "UPDATE mock_oms_order SET paid_minor=12000 WHERE tenant_id=:t AND order_id=:o"
                ),
                {"t": auth.tenant_id, "o": quote.order_id},
            )
            connection.execute(
                text(
                    "UPDATE mock_pay_payment SET paid_minor=12000"
                    " WHERE tenant_id=:t AND order_id=:o"
                ),
                {"t": auth.tenant_id, "o": quote.order_id},
            )
            connection.execute(
                text(
                    "UPDATE mock_pay_payment_detail SET paid_goods_minor=11000,"
                    " paid_minor=12000 WHERE tenant_id=:t AND order_id=:o"
                ),
                {"t": auth.tenant_id, "o": quote.order_id},
            )
        payload = connection.execute(
            text(
                "SELECT payload FROM outbox_event WHERE tenant_id=:t"
                " AND aggregate_type='refund_request' AND aggregate_id=:r"
            ),
            {"t": auth.tenant_id, "r": created.refund_request_id},
        ).scalar_one()
        event = parse_event(json.loads(payload))
        review = MockRefundReviewHandler(snapshots, OutboxRepository(engine, catalog))
        if source_change == "none":

            class NestedTransactionEngine:
                @contextmanager
                def begin(self):
                    with connection.begin_nested():
                        yield connection

            inbox = InboxRepository(NestedTransactionEngine())
            consumer_name = catalog.entries["refund.confirmed"]["consumer"]
            outcome, lease = inbox.claim(event, consumer_name, "worker-first")
            assert outcome == "claimed" and lease is not None

            def crash_after_review(conn, received):
                review(conn, received)
                raise RuntimeError("synthetic worker crash after review")

            with pytest.raises(RuntimeError, match="synthetic worker crash"):
                inbox.complete(lease, crash_after_review)
            still_pending = connection.execute(
                text(
                    "SELECT review_status,review_revision FROM refund_request"
                    " WHERE tenant_id=:t AND refund_request_id=:r"
                ),
                {"t": auth.tenant_id, "r": created.refund_request_id},
            ).one()
            assert (still_pending.review_status, still_pending.review_revision) == (
                "pending_review",
                0,
            )
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM outbox_event WHERE tenant_id=:t"
                        " AND aggregate_type='refund_request' AND aggregate_id=:r"
                        " AND event_type='refund.approved'"
                    ),
                    {"t": auth.tenant_id, "r": created.refund_request_id},
                ).scalar_one()
                == 0
            )
            assert inbox.fail(lease, "synthetic worker crash") == "failed"
            retry_event = replace(event, retry_count=1)
            retry_outcome, retry_lease = inbox.claim(retry_event, consumer_name, "worker-second")
            assert retry_outcome == "claimed" and retry_lease is not None
            assert inbox.complete(retry_lease, review) == created.refund_request_id
            duplicate_outcome, duplicate_lease = inbox.claim(
                retry_event, consumer_name, "worker-third"
            )
            assert duplicate_outcome == "duplicate" and duplicate_lease is None
        assert review(connection, event) == created.refund_request_id
        assert review(connection, event) == created.refund_request_id
        row = connection.execute(
            text(
                "SELECT review_status,review_revision FROM refund_request"
                " WHERE tenant_id=:t AND refund_request_id=:r"
            ),
            {"t": auth.tenant_id, "r": created.refund_request_id},
        ).one()
        assert (row.review_status, row.review_revision) == (expected_status, expected_revision)
        approved_events = connection.execute(
            text(
                "SELECT COUNT(*) FROM outbox_event WHERE tenant_id=:t"
                " AND aggregate_type='refund_request' AND aggregate_id=:r"
                " AND event_type='refund.approved'"
            ),
            {"t": auth.tenant_id, "r": created.refund_request_id},
        ).scalar_one()
        assert approved_events == (1 if expected_status == "approved" else 0)
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM mock_pay_refund_execution"
                    " WHERE tenant_id=:t AND order_id=:o"
                ),
                {"t": auth.tenant_id, "o": quote.order_id},
            ).scalar_one()
            == 0
        )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
