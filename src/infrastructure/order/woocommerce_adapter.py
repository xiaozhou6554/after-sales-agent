"""WooCommerce REST v3 adapter for order and payment snapshots.

The adapter keeps WooCommerce HTTP details in Infrastructure. Refund records
created here are explicitly non-gateway records and do not represent settled
money movement.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, quote, urlparse

import httpx

from application.order.ports.order_query import OrderDependencyError, OrderQueryPort
from domain.order.snapshots import OrderSnapshot, PaymentSnapshot

_ZERO_DECIMAL_CURRENCIES = {
    "BIF",
    "CLP",
    "DJF",
    "GNF",
    "ISK",
    "JPY",
    "KMF",
    "KRW",
    "PYG",
    "RWF",
    "UGX",
    "VND",
    "VUV",
    "XAF",
    "XOF",
    "XPF",
}
_THREE_DECIMAL_CURRENCIES = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
_PAID_STATUSES = {"processing", "completed", "refunded"}
_ORDER_STATUS = {
    "pending": "pending_payment",
    "processing": "processing",
    "on-hold": "on_hold",
    "completed": "delivered",
    "cancelled": "cancelled",
    "refunded": "refunded",
    "failed": "failed",
    "trash": "cancelled",
}


class WooCommerceAPIError(OrderDependencyError):
    """WooCommerce request failed; messages intentionally omit response bodies."""


@dataclass(frozen=True)
class WooRefundRecord:
    refund_id: str
    amount: str
    refunded_payment: bool


def amount_to_minor(amount: str | int | Decimal, currency: str) -> int:
    """Convert a Woo decimal amount into the ISO currency's minor units."""
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError("WooCommerce returned an invalid monetary amount") from exc
    exponent = (
        0
        if currency in _ZERO_DECIMAL_CURRENCIES
        else 3
        if currency in _THREE_DECIMAL_CURRENCIES
        else 2
    )
    scaled = value * (10**exponent)
    if scaled != scaled.to_integral_value():
        raise ValueError("WooCommerce amount has unsupported currency precision")
    return int(scaled)


def _metadata(order: dict[str, Any]) -> dict[str, Any]:
    return {
        str(item.get("key")): item.get("value")
        for item in order.get("meta_data", [])
        if isinstance(item, dict) and item.get("key") is not None
    }


def _observed_at(order: dict[str, Any]) -> datetime:
    value = order.get("date_modified_gmt") or order.get("date_created_gmt")
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _percent_encode(value: str) -> str:
    return quote(value, safe="~-._")


def _oauth_authorization_header(
    method: str,
    url: str,
    consumer_key: str,
    consumer_secret: str,
) -> str:
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    query = parse_qsl(urlparse(url).query, keep_blank_values=True)
    parameters = [(key, value) for key, value in [*query, *oauth.items()]]
    normalized = "&".join(
        f"{_percent_encode(key)}={_percent_encode(value)}"
        for key, value in sorted(parameters)
    )
    base_string = "&".join(
        (method.upper(), _percent_encode(url.split("?", 1)[0]), _percent_encode(normalized))
    )
    signing_key = f"{_percent_encode(consumer_secret)}&".encode("utf-8")
    digest = hmac.new(signing_key, base_string.encode("utf-8"), hashlib.sha256).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    fields = ", ".join(
        f'{_percent_encode(key)}="{_percent_encode(value)}"'
        for key, value in sorted(oauth.items())
    )
    return f"OAuth {fields}"


class WooCommerceOrderAdapter(OrderQueryPort):
    """Read WooCommerce orders and explicitly non-gateway refund records.

    Orders must carry ``after_sales_tenant_id`` metadata. Customer identity uses
    ``after_sales_customer_id`` when present, otherwise WooCommerce's customer ID.
    The supplied ``order_id`` is the numeric WooCommerce order resource ID.
    """

    snapshot_version = "woocommerce-rest-v3"

    def __init__(
        self,
        store_url: str,
        consumer_key: str,
        consumer_secret: str,
        *,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(store_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("WooCommerce store URL must be an absolute HTTP(S) URL")
        if not consumer_key or not consumer_secret:
            raise ValueError("WooCommerce REST API credentials are required")
        api_root = store_url.rstrip("/") + "/wp-json/wc/v3/"
        self._scheme = parsed.scheme
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._client = client or httpx.Client(base_url=api_root, timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            request = self._client.build_request(method, path, **kwargs)
            if self._scheme == "https":
                credentials = f"{self._consumer_key}:{self._consumer_secret}".encode("utf-8")
                token = base64.b64encode(credentials).decode("ascii")
                request.headers["Authorization"] = f"Basic {token}"
            else:
                request.headers["Authorization"] = _oauth_authorization_header(
                    method,
                    str(request.url),
                    self._consumer_key,
                    self._consumer_secret,
                )
            response = self._client.send(request)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status is not None else ""
            raise WooCommerceAPIError(f"WooCommerce {method} request failed{suffix}") from exc

    def _read_order(self, tenant_id: str, order_id: str) -> dict[str, Any] | None:
        if not order_id.isdecimal():
            return None
        order = self._request("GET", f"orders/{quote(order_id, safe='')}/")
        if (
            not isinstance(order, dict)
            or str(_metadata(order).get("after_sales_tenant_id", "")) != tenant_id
        ):
            return None
        return order

    def query_order(self, tenant_id: str, order_id: str) -> OrderSnapshot | None:
        order = self._read_order(tenant_id, order_id)
        if order is None:
            return None
        metadata = _metadata(order)
        currency = str(order.get("currency", "")).upper()
        status = str(order.get("status", ""))
        if not currency or status not in _ORDER_STATUS:
            raise WooCommerceAPIError("WooCommerce order has unsupported status or currency")
        modified = _observed_at(order)
        customer_id = metadata.get("after_sales_customer_id", order.get("customer_id"))
        return OrderSnapshot(
            tenant_id=tenant_id,
            order_id=order_id,
            owner_customer_id=str(customer_id),
            status=_ORDER_STATUS[status],
            payment_status="succeeded" if order.get("date_paid") else "unknown",
            fulfillment_status=_ORDER_STATUS[status],
            currency=currency,
            amount_minor=amount_to_minor(order.get("total", "0"), currency),
            version=int(modified.timestamp()),
            snapshot_version=self.snapshot_version,
            observed_at=modified,
        )

    def query_payment(self, tenant_id: str, order_id: str) -> PaymentSnapshot | None:
        order = self._read_order(tenant_id, order_id)
        if order is None or not order.get("date_paid"):
            return None
        currency = str(order.get("currency", "")).upper()
        if not currency:
            raise WooCommerceAPIError("WooCommerce order is missing currency")
        paid_minor = amount_to_minor(order.get("total", "0"), currency)
        actual_refunded_minor = sum(
            amount_to_minor(refund.get("amount", "0"), currency)
            for refund in order.get("refunds", [])
            if isinstance(refund, dict) and refund.get("refunded_payment") is True
        )
        transaction_id = str(order.get("transaction_id", ""))
        return PaymentSnapshot(
            tenant_id=tenant_id,
            order_id=order_id,
            payment_status="succeeded" if str(order.get("status")) in _PAID_STATUSES else "unknown",
            paid_minor=paid_minor,
            refunded_minor=actual_refunded_minor,
            currency=currency,
            payment_ids=(transaction_id,) if transaction_id else (),
            snapshot_version=self.snapshot_version,
            observed_at=_observed_at(order),
        )

    def create_refund_record(
        self,
        order_id: str,
        amount: str,
        reason: str,
        *,
        idempotency_key: str,
    ) -> WooRefundRecord:
        """Create a store refund record without invoking a payment gateway."""
        if not order_id.isdecimal():
            raise ValueError("WooCommerce order ID must be numeric")
        if not idempotency_key:
            raise ValueError("Refund record idempotency key is required")
        value = Decimal(amount)
        if value <= 0:
            raise ValueError("Refund amount must be positive")
        encoded_order_id = quote(order_id, safe="")
        records = self._request("GET", f"orders/{encoded_order_id}/refunds/")
        if not isinstance(records, list):
            raise WooCommerceAPIError("WooCommerce returned an invalid refund list")
        for record in records:
            if not isinstance(record, dict):
                continue
            metadata = _metadata(record)
            if metadata.get("after_sales_idempotency_key") == idempotency_key:
                if Decimal(str(record.get("amount", "0"))) != value:
                    raise ValueError("Idempotency key was reused with a different refund amount")
                return WooRefundRecord(
                    refund_id=str(record["id"]),
                    amount=str(record.get("amount", format(value, "f"))),
                    refunded_payment=record.get("refunded_payment") is True,
                )
        result = self._request(
            "POST",
            f"orders/{encoded_order_id}/refunds/",
            json={
                "amount": format(value, "f"),
                "reason": reason,
                "api_refund": False,
                "api_restock": False,
                "meta_data": [{"key": "after_sales_idempotency_key", "value": idempotency_key}],
            },
        )
        if not isinstance(result, dict) or result.get("id") is None:
            raise WooCommerceAPIError("WooCommerce returned an invalid refund response")
        return WooRefundRecord(
            refund_id=str(result["id"]),
            amount=str(result.get("amount", format(value, "f"))),
            refunded_payment=result.get("refunded_payment") is True,
        )
