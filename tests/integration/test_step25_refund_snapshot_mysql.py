"""从真实 Mock-v2 表读取资格事实，不把知识文档当金额来源。"""

from datetime import date, datetime, timezone

from bootstrap.settings import load_settings
from sqlalchemy import text

from application.common.context import AuthContext, DataScope
from application.refund.qualification import RefundQualificationUseCase
from domain.refund import assess_refund
from infrastructure.mock.refund_snapshot_adapter import MockRefundSnapshotAdapter
from infrastructure.persistence.mysql.connection import create_mysql_engine


def test_mock_refund_snapshot_uses_payment_detail_and_tenant_scope() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    try:
        adapter = MockRefundSnapshotAdapter(engine, as_of=date(2026, 10, 19))
        north = adapter.load("tenant-north", "ORDER-001")
        south = adapter.load("tenant-south", "ORDER-001")
        assert north is not None and south is not None
        assert north.customer_id == "CUSTOMER-01"
        assert south.customer_id == "CUSTOMER-91"
        assert north.facts.paid_total_minor == 100000
        assert north.facts.paid_goods_minor is None  # 基础订单缺少分行支付组成，必须拒绝推算
        detailed = adapter.load("tenant-north", "ORDER-003")
        assert detailed is not None
        assert detailed.facts.paid_goods_minor is not None
        assert detailed.facts.paid_shipping_minor is not None
        assert (
            detailed.facts.paid_goods_minor + detailed.facts.paid_shipping_minor
            == detailed.facts.paid_total_minor
        )
        fresh = MockRefundSnapshotAdapter(engine, as_of=date(2026, 9, 3)).load(
            "tenant-north", "SYN-ORDER-N-01-001"
        )
        assert fresh is not None and not fresh.facts.status_conflict
        assert len(fresh.lines) == 1
        assert fresh.lines[0].goods_minor == 39402
        assert fresh.lines[0].shipping_minor == 848
        assert fresh.lines[0].quantity == 3

        class CapturedQuotes:
            def __init__(self):
                self.saved = []

            def save(self, quote):
                self.saved.append(quote)

        quotes = CapturedQuotes()
        auth = AuthContext(
            tenant_id="tenant-north",
            subject_id="synthetic-customer-1",
            roles=("customer",),
            scopes=("refund:read",),
            data_scope=DataScope(customer_ids=(fresh.customer_id,)),
            session_id="test-session",
            issued_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
            expires_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
        )
        assessment, quote = RefundQualificationUseCase(
            MockRefundSnapshotAdapter(engine, as_of=date(2026, 9, 3)),
            quotes,
            clock=lambda: datetime(2026, 9, 3, tzinfo=timezone.utc),
            new_id=lambda: "synthetic-replay",
        ).calculate_quote(auth, "SYN-ORDER-N-01-001", "changed_mind")
        assert assessment.eligibility == "manual_review" and assessment.can_submit
        assert quote is not None and quote.amount_minor == 39402
        assert quote.lines[0].shipping_minor == 0
        assert quotes.saved == [quote]
        assert adapter.load("tenant-south", "ORDER-002") is None
        assert assess_refund(north.facts).reason_code == "REASON_REQUIRED"
    finally:
        engine.dispose()


def test_pending_shipment_with_existing_signed_batch_is_source_conflict() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            text(
                "UPDATE mock_oms_order SET status='pending_shipment'"
                " WHERE tenant_id='tenant-north' AND order_id='SYN-ORDER-N-01-001'"
            )
        )
        snapshot = (
            MockRefundSnapshotAdapter(engine)
            .bind(connection)
            .load("tenant-north", "SYN-ORDER-N-01-001")
        )
        assert snapshot is not None and snapshot.facts.status_conflict
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
