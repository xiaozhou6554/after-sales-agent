"""Opt-in live contract check for a local WooCommerce test store.

Refund creation is disabled unless WOO_ENABLE_REFUND_WRITE=true. The write uses
WooCommerce's non-gateway refund-record path and is replay-safe by test metadata.
"""

from __future__ import annotations

import hashlib

import pytest
from bootstrap.settings import load_settings

from infrastructure.order.woocommerce_adapter import WooCommerceOrderAdapter


def test_local_woocommerce_order_and_optional_refund_record() -> None:
    settings = load_settings()
    required = (
        settings.woo_store_url,
        settings.woo_consumer_key,
        settings.woo_consumer_secret,
        settings.woo_test_order_id,
        settings.woo_test_tenant_id,
    )
    if not all(required):
        pytest.skip("Configure WOO_* test-store settings to enable this live integration check")

    adapter = WooCommerceOrderAdapter(
        settings.woo_store_url,
        settings.woo_consumer_key,
        settings.woo_consumer_secret,
    )
    try:
        order = adapter.query_order(settings.woo_test_tenant_id, settings.woo_test_order_id)
        assert order is not None, "Test order is absent or lacks matching tenant metadata"
        assert order.order_id == settings.woo_test_order_id
        assert order.tenant_id == settings.woo_test_tenant_id

        if settings.woo_enable_refund_write:
            assert settings.woo_test_refund_amount, (
                "Set WOO_TEST_REFUND_AMOUNT for the opted-in write"
            )
            key_material = (
                f"local-acceptance:{settings.woo_test_order_id}:"
                f"{settings.woo_test_refund_amount}"
            )
            idempotency_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
            refund = adapter.create_refund_record(
                settings.woo_test_order_id,
                settings.woo_test_refund_amount,
                "After-sales local integration acceptance",
                idempotency_key=idempotency_key,
            )
            assert refund.refund_id
            assert refund.refunded_payment is False
            replay = adapter.create_refund_record(
                settings.woo_test_order_id,
                settings.woo_test_refund_amount,
                "After-sales local integration acceptance",
                idempotency_key=idempotency_key,
            )
            assert replay == refund
    finally:
        adapter.close()
