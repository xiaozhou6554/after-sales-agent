"""真实 MySQL 中校验退款报价持久化的租户隔离和不可变性。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from application.refund.qualification import RefundLineAllocation, RefundQuote
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.refund_quote_repository import (
    MySqlRefundQuoteRepository,
)


def test_quote_roundtrip_is_tenant_scoped_and_duplicate_is_rejected() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        repository = MySqlRefundQuoteRepository(engine).bind(connection)
        quote = RefundQuote(
            quote_id=f"quote-test-{uuid4().hex}",
            tenant_id="tenant-north",
            order_id="ORDER-001",
            subject_id="CUSTOMER-01",
            beneficiary_customer_id="CUSTOMER-01",
            reason_code="changed_mind",
            eligibility="manual_review",
            amount_minor=800,
            goods_minor=800,
            shipping_minor=0,
            currency="CNY",
            policy_id="mock-refund",
            policy_version="1",
            order_version="3",
            payment_version="mock-v2",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            lines=(RefundLineAllocation("LINE-001", 1, 800, 0),),
        )
        repository.save(quote)
        loaded = repository.load(quote.tenant_id, quote.quote_id)
        assert loaded is not None
        assert loaded.amount_minor == quote.amount_minor
        assert loaded.lines == quote.lines
        assert loaded.expires_at == quote.expires_at
        assert repository.load("tenant-south", quote.quote_id) is None
        with pytest.raises(IntegrityError):
            repository.save(quote)
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_same_order_cannot_hold_two_active_requests_even_with_different_keys() -> None:
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        repository = MySqlRefundQuoteRepository(engine).bind(connection)
        first_quote = RefundQuote(
            quote_id=f"quote-{uuid4().hex}",
            tenant_id="tenant-north",
            order_id="ORDER-001",
            subject_id="CUSTOMER-01",
            beneficiary_customer_id="CUSTOMER-01",
            reason_code="changed_mind",
            eligibility="eligible",
            amount_minor=100,
            goods_minor=100,
            shipping_minor=0,
            currency="CNY",
            policy_id="mock-refund",
            policy_version="1",
            order_version="3",
            payment_version="mock-v2",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            lines=(RefundLineAllocation("LINE-001", 1, 100, 0),),
        )
        second_quote = replace(first_quote, quote_id=f"quote-{uuid4().hex}")
        repository.save(first_quote)
        repository.save(second_quote)
        insert = text(
            "INSERT INTO refund_request (tenant_id,refund_request_id,quote_id,order_id,"
            "actor_subject_id,customer_id,confirmation_id,idempotency_key,reason_code,"
            "eligibility,review_status,amount_minor,goods_minor,shipping_minor,currency,"
            "policy_id,policy_version,order_version,payment_version,created_at,updated_at)"
            " VALUES (:tenant_id,:request_id,:quote_id,:order_id,:subject_id,:customer_id,"
            ":confirmation_id,:idempotency_key,:reason_code,:eligibility,'pending_review',"
            ":amount_minor,:goods_minor,:shipping_minor,:currency,:policy_id,:policy_version,"
            ":order_version,:payment_version,:now,:now)"
        )

        def params(quote):
            return {
                "tenant_id": quote.tenant_id,
                "request_id": f"refund-{uuid4().hex}",
                "quote_id": quote.quote_id,
                "order_id": quote.order_id,
                "subject_id": quote.subject_id,
                "customer_id": quote.beneficiary_customer_id,
                "confirmation_id": f"confirmation-{uuid4().hex}",
                "idempotency_key": f"idem-{uuid4().hex}",
                "reason_code": quote.reason_code,
                "eligibility": quote.eligibility,
                "amount_minor": quote.amount_minor,
                "goods_minor": quote.goods_minor,
                "shipping_minor": quote.shipping_minor,
                "currency": quote.currency,
                "policy_id": quote.policy_id,
                "policy_version": quote.policy_version,
                "order_version": quote.order_version,
                "payment_version": quote.payment_version,
                "now": datetime.now(timezone.utc).replace(tzinfo=None),
            }

        first_params = params(first_quote)
        connection.execute(insert, first_params)
        with pytest.raises(IntegrityError):
            connection.execute(insert, params(second_quote))
        connection.execute(
            text(
                "UPDATE refund_request SET review_status='rejected'"
                " WHERE tenant_id=:tenant_id AND refund_request_id=:request_id"
            ),
            {"tenant_id": first_params["tenant_id"], "request_id": first_params["request_id"]},
        )
        connection.execute(insert, params(second_quote))
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
