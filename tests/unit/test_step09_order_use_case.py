"""步骤09 订单/支付查询用例单元测试：归属校验与统一安全语义。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from application.common.authorization import GENERIC_DENIED_MESSAGE, AuthorizationDenied
from application.common.context import AuthContext, DataScope
from application.common.errors import ErrorCode
from application.order.dto.order import PAYMENT_OUTCOME_UNKNOWN, payment_result
from application.order.ports.order_query import OrderDependencyError
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from domain.order.snapshots import OrderSnapshot, PaymentSnapshot

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)


def make_snapshot(order_id: str = "ORDER-001", owner: str = "CUSTOMER-01") -> OrderSnapshot:
    return OrderSnapshot(
        tenant_id="tenant-north",
        order_id=order_id,
        owner_customer_id=owner,
        status="delivered",
        payment_status="succeeded",
        fulfillment_status="delivered",
        currency="CNY",
        amount_minor=100000,
        version=3,
        snapshot_version="mock-v2",
        observed_at=NOW,
    )


def make_auth(
    customer_ids: tuple[str, ...] = ("CUSTOMER-01",),
    scopes: tuple[str, ...] = ("order:read", "payment:read"),
    tenant_id: str = "tenant-north",
    expires_at: datetime | None = None,
) -> AuthContext:
    return AuthContext(
        tenant_id=tenant_id,
        subject_id="subject-1",
        roles=("customer",),
        scopes=scopes,
        data_scope=DataScope(customer_ids=customer_ids),
        session_id="s-1",
        issued_at=NOW - timedelta(minutes=5),
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(days=1),
    )


class FakeOrderPort:
    def __init__(
        self, orders: dict[str, OrderSnapshot], payments: dict[str, PaymentSnapshot | None]
    ) -> None:
        self.orders = orders
        self.payments = payments
        self.order_calls: list[tuple[str, str]] = []
        self.payment_calls: list[tuple[str, str]] = []
        self.fail = False

    def query_order(self, tenant_id: str, order_id: str) -> OrderSnapshot | None:
        if self.fail:
            raise OrderDependencyError("db down")
        self.order_calls.append((tenant_id, order_id))
        return self.orders.get((tenant_id, order_id))

    def query_payment(self, tenant_id: str, order_id: str) -> PaymentSnapshot | None:
        if self.fail:
            raise OrderDependencyError("db down")
        self.payment_calls.append((tenant_id, order_id))
        return self.payments.get((tenant_id, order_id))


def test_self_order_query_passes_ownership() -> None:
    port = FakeOrderPort({("tenant-north", "ORDER-001"): make_snapshot()}, {})
    snapshot = QueryOrderUseCase(port).execute(make_auth(), "ORDER-001")
    assert snapshot.owner_customer_id == "CUSTOMER-01"
    assert port.order_calls == [("tenant-north", "ORDER-001")]


def test_other_users_order_denied_with_generic_message() -> None:
    port = FakeOrderPort(
        {("tenant-north", "ORDER-002"): make_snapshot("ORDER-002", "CUSTOMER-02")}, {}
    )
    with pytest.raises(AuthorizationDenied) as excinfo:
        QueryOrderUseCase(port).execute(make_auth(), "ORDER-002")
    assert excinfo.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE
    assert excinfo.value.message == GENERIC_DENIED_MESSAGE


def test_absent_order_indistinguishable_from_denied() -> None:
    """不存在订单与越权订单同一错误码、同一泛化文案（不泄露存在性）。"""
    port = FakeOrderPort({}, {})
    with pytest.raises(AuthorizationDenied) as absent:
        QueryOrderUseCase(port).execute(make_auth(), "ORDER-404")
    assert absent.value.error_code is ErrorCode.RESOURCE_UNAVAILABLE
    assert absent.value.message == GENERIC_DENIED_MESSAGE


def test_tenant_scoped_query_uses_auth_tenant() -> None:
    port = FakeOrderPort({}, {})
    with pytest.raises(AuthorizationDenied):
        QueryOrderUseCase(port).execute(make_auth(), "ORDER-001")
    assert port.order_calls == [("tenant-north", "ORDER-001")]


def test_expired_identity_denied_as_unauthenticated() -> None:
    port = FakeOrderPort({("tenant-north", "ORDER-001"): make_snapshot()}, {})
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(AuthorizationDenied) as excinfo:
        QueryOrderUseCase(port).execute(make_auth(expires_at=expired), "ORDER-001")
    assert excinfo.value.error_code is ErrorCode.UNAUTHENTICATED


def test_dependency_error_propagates_for_retry_budget() -> None:
    port = FakeOrderPort({}, {})
    port.fail = True
    with pytest.raises(OrderDependencyError):
        QueryOrderUseCase(port).execute(make_auth(), "ORDER-001")


def test_payment_unknown_outcome_for_order_without_payment() -> None:
    port = FakeOrderPort(
        {("tenant-north", "ORDER-001"): make_snapshot()}, {("tenant-north", "ORDER-001"): None}
    )
    result = QueryPaymentUseCase(port).execute(make_auth(), "ORDER-001")
    assert result.outcome == PAYMENT_OUTCOME_UNKNOWN
    assert result.snapshot is None
    assert result.order is not None


def test_payment_result_builder() -> None:
    snapshot = PaymentSnapshot(
        tenant_id="tenant-north",
        order_id="ORDER-001",
        payment_status="succeeded",
        paid_minor=100000,
        refunded_minor=10000,
        currency="CNY",
        payment_ids=("PAY-001",),
        snapshot_version="mock-v2",
        observed_at=NOW,
    )
    found = payment_result(snapshot, make_snapshot())
    assert found.found and found.snapshot is snapshot
