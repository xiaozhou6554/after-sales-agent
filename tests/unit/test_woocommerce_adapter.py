from __future__ import annotations

import httpx
from bootstrap.settings import RuntimeSettings

from infrastructure.order.woocommerce_adapter import (
    WooCommerceOrderAdapter,
    amount_to_minor,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="http://woocommerce.test/wp-json/wc/v3/",
        transport=httpx.MockTransport(handler),
    )


def _order(**overrides):
    result = {
        "id": 42,
        "status": "completed",
        "currency": "CNY",
        "total": "123.45",
        "customer_id": 7,
        "date_paid": "2026-10-21T10:00:00",
        "date_modified_gmt": "2026-10-21T10:30:00",
        "transaction_id": "txn-42",
        "refunds": [],
        "meta_data": [
            {"key": "after_sales_tenant_id", "value": "tenant-a"},
            {"key": "after_sales_customer_id", "value": "customer-a"},
        ],
    }
    result.update(overrides)
    return result


def test_query_order_maps_tenant_scoped_snapshot() -> None:
    client = _client(lambda request: httpx.Response(200, json=_order()))
    adapter = WooCommerceOrderAdapter("http://woocommerce.test", "ck", "cs", client=client)

    snapshot = adapter.query_order("tenant-a", "42")

    assert snapshot is not None
    assert snapshot.owner_customer_id == "customer-a"
    assert snapshot.status == "delivered"
    assert snapshot.payment_status == "succeeded"
    assert snapshot.amount_minor == 12345
    assert snapshot.snapshot_version == "woocommerce-rest-v3"


def test_query_order_hides_orders_from_other_tenants_and_rejects_non_numeric_ids() -> None:
    calls = []
    client = _client(lambda request: calls.append(request) or httpx.Response(200, json=_order()))
    adapter = WooCommerceOrderAdapter("http://woocommerce.test", "ck", "cs", client=client)

    assert adapter.query_order("tenant-b", "42") is None
    assert adapter.query_order("tenant-a", "../42") is None
    assert len(calls) == 1


def test_query_payment_counts_only_refunds_reported_as_gateway_refunded() -> None:
    order = _order(
        refunds=[
            {"amount": "10.00", "refunded_payment": False},
            {"amount": "5.25", "refunded_payment": True},
        ]
    )
    client = _client(lambda request: httpx.Response(200, json=order))
    adapter = WooCommerceOrderAdapter("http://woocommerce.test", "ck", "cs", client=client)

    payment = adapter.query_payment("tenant-a", "42")

    assert payment is not None
    assert payment.paid_minor == 12345
    assert payment.refunded_minor == 525
    assert payment.payment_ids == ("txn-42",)


def test_refund_creation_never_calls_payment_gateway() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(
            201,
            json={"id": 91, "amount": "2.50", "refunded_payment": False},
        )

    client = _client(handler)
    adapter = WooCommerceOrderAdapter("http://woocommerce.test", "ck", "cs", client=client)

    result = adapter.create_refund_record(
        "42", "2.50", "local acceptance", idempotency_key="test-refund-1"
    )

    assert result.refund_id == "91"
    assert result.refunded_payment is False
    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[1].url.path.endswith("/orders/42/refunds/")
    assert requests[1].read().decode() == (
        '{"amount":"2.50","reason":"local acceptance",'
        '"api_refund":false,"api_restock":false,"meta_data":['
        '{"key":"after_sales_idempotency_key","value":"test-refund-1"}]}'
    )


def test_refund_record_replays_by_metadata_key_without_duplicate_post() -> None:
    calls = []
    existing = [{
        "id": 91,
        "amount": "2.50",
        "refunded_payment": False,
        "meta_data": [{"key": "after_sales_idempotency_key", "value": "test-refund-1"}],
    }]
    client = _client(
        lambda request: calls.append(request)
        or httpx.Response(200, json=existing)
    )
    adapter = WooCommerceOrderAdapter("http://woocommerce.test", "ck", "cs", client=client)

    result = adapter.create_refund_record(
        "42", "2.50", "local acceptance", idempotency_key="test-refund-1"
    )

    assert result.refund_id == "91"
    assert len(calls) == 1
    assert calls[0].method == "GET"


def test_currency_minor_unit_precision() -> None:
    assert amount_to_minor("123", "JPY") == 123
    assert amount_to_minor("1.234", "BHD") == 1234
    assert amount_to_minor("12.34", "CNY") == 1234


def test_woocommerce_credentials_are_redacted_from_safe_settings() -> None:
    settings = RuntimeSettings(
        _env_file=None,
        woo_consumer_key="ck_test_secret",
        woo_consumer_secret="cs_test_secret",
    )

    assert settings.mask_text("ck_test_secret cs_test_secret") == "*** ***"
    safe = settings.to_safe_dict()
    assert safe["woo_consumer_key"] == "***"
    assert safe["woo_consumer_secret"] == "***"


def test_http_uses_oauth_signature_without_transmitting_secret_directly() -> None:
    headers = []
    client = _client(
        lambda request: headers.append(request.headers["Authorization"])
        or httpx.Response(200, json=_order())
    )
    adapter = WooCommerceOrderAdapter(
        "http://woocommerce.test", "ck_test", "cs_sensitive", client=client
    )

    assert adapter.query_order("tenant-a", "42") is not None

    assert headers[0].startswith("OAuth ")
    assert "oauth_signature_method=\"HMAC-SHA256\"" in headers[0]
    assert "cs_sensitive" not in headers[0]


def test_https_uses_basic_auth() -> None:
    client = httpx.Client(
        base_url="https://woocommerce.test/wp-json/wc/v3/",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_order())
        ),
    )
    adapter = WooCommerceOrderAdapter(
        "https://woocommerce.test", "ck_test", "cs_test", client=client
    )

    assert adapter.query_order("tenant-a", "42") is not None
