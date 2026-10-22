"""退款表在数据库层守住金额与同订单活动申请唯一性。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from migrations.versions import v0012_refund_application

from application.refund.qualification import RefundQuote
from infrastructure.persistence.mysql.repositories.refund_quote_repository import (
    MySqlRefundQuoteRepository,
)


def test_refund_migration_has_active_order_and_immutable_quote_guards() -> None:
    statements = v0012_refund_application.statements()
    assert v0012_refund_application.VERSION == "0012"
    assert len(statements) == 3
    assert "uq_refund_active_order" in statements[1]
    assert "review_status IN ('pending_review','reviewing','approved')" in statements[1]
    assert "uq_refund_request_idem" in statements[1]
    assert "amount_minor = goods_minor + shipping_minor" in statements[0]
    assert "amount_minor = goods_minor + shipping_minor" in statements[1]
    assert "fk_refund_line_request" in statements[2]


def test_quote_repository_rejects_inconsistent_amount_before_database() -> None:
    quote = RefundQuote(
        quote_id="quote-test",
        tenant_id="tenant-north",
        order_id="ORDER-001",
        subject_id="CUSTOMER-01",
        beneficiary_customer_id="CUSTOMER-01",
        reason_code="changed_mind",
        eligibility="eligible",
        amount_minor=100,
        goods_minor=80,
        shipping_minor=20,
        currency="CNY",
        policy_id="mock-refund",
        policy_version="1",
        order_version="1",
        payment_version="1",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    repository = MySqlRefundQuoteRepository(None)
    with pytest.raises(ValueError, match="金额组成"):
        repository.save(replace(quote, amount_minor=99))
    with pytest.raises(ValueError, match="金额组成"):
        repository.save(replace(quote, amount_minor=0))
