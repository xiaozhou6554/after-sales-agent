"""步骤09 MockOrderAdapter 集成测试：真实 MySQL 上的租户隔离查询与快照契约。

需要本地 docker MySQL（开发环境已启动）；只读查询，不写入任何数据。
数据事实以 data/generated/reports/mock-v2-seeding.json 的种子清单为准：
tenant-north/ORDER-001 有 PAY-001（100000 分，已成功）与 10000 分成功退款；
tenant-south/ORDER-001 无支付记录（支付未知语义）。
"""

from __future__ import annotations

import pytest
from bootstrap.settings import load_settings

from application.order.ports.order_query import OrderDependencyError
from infrastructure.mock.order_adapter import MOCK_SNAPSHOT_VERSION, MockOrderAdapter
from infrastructure.persistence.mysql.connection import create_mysql_engine


@pytest.fixture(scope="module")
def adapter():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    yield MockOrderAdapter(engine)
    engine.dispose()


def test_order_snapshot_fields_from_real_mock_tables(adapter) -> None:
    snapshot = adapter.query_order("tenant-north", "ORDER-001")
    assert snapshot is not None
    assert snapshot.tenant_id == "tenant-north"
    assert snapshot.order_id == "ORDER-001"
    assert snapshot.owner_customer_id == "CUSTOMER-01"
    assert snapshot.status == "delivered"
    assert snapshot.payment_status == "succeeded"
    assert snapshot.fulfillment_status == "delivered"
    assert snapshot.currency == "CNY"
    assert snapshot.amount_minor == 100000
    assert snapshot.version == 3
    assert snapshot.snapshot_version == MOCK_SNAPSHOT_VERSION
    assert snapshot.observed_at is not None


def test_payment_snapshot_aggregates_payments_and_successful_refunds(adapter) -> None:
    snapshot = adapter.query_payment("tenant-north", "ORDER-001")
    assert snapshot is not None
    assert snapshot.payment_status == "succeeded"
    assert snapshot.paid_minor == 100000
    # 只有 status='succeeded' 的退款计入已退金额（该订单仅一笔 10000 分成功退款）。
    assert snapshot.refunded_minor == 10000
    assert snapshot.payment_ids == ("PAY-001",)
    assert snapshot.currency == "CNY"
    assert snapshot.snapshot_version == MOCK_SNAPSHOT_VERSION


def test_order_without_payment_yields_none_for_unknown_payment(adapter) -> None:
    """tenant-south/ORDER-001 有订单无支付：query_payment 返回 None（支付未知）。"""
    order = adapter.query_order("tenant-south", "ORDER-001")
    assert order is not None
    assert order.owner_customer_id == "CUSTOMER-91"
    assert order.payment_status == "unknown"
    assert adapter.query_payment("tenant-south", "ORDER-001") is None


def test_absent_order_returns_none(adapter) -> None:
    assert adapter.query_order("tenant-north", "ORDER-404") is None
    assert adapter.query_payment("tenant-north", "ORDER-404") is None


def test_tenant_isolation_same_order_id(adapter) -> None:
    """同一 order_id 在不同租户下互不可见：租户条件由 SQL 强制。"""
    north = adapter.query_order("tenant-north", "ORDER-001")
    south = adapter.query_order("tenant-south", "ORDER-001")
    assert north.owner_customer_id == "CUSTOMER-01"
    assert south.owner_customer_id == "CUSTOMER-91"
    assert south.version == 1 and north.version == 3


def test_dependency_failure_wrapped_as_transient_error(adapter) -> None:
    class BrokenEngine:
        def connect(self):
            raise RuntimeError("connection refused")

    broken = MockOrderAdapter(BrokenEngine())
    with pytest.raises(OrderDependencyError):
        broken.query_order("tenant-north", "ORDER-001")
    with pytest.raises(OrderDependencyError):
        broken.query_payment("tenant-north", "ORDER-001")
    # 健康引擎不受影响（守卫不掩盖真实故障）。
    assert adapter.query_order("tenant-north", "ORDER-001") is not None
