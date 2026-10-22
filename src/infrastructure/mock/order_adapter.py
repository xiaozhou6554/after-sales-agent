"""MockOrderAdapter：基于 mock_oms_* / mock_pay_* 表实现 OrderQueryPort。

- 生产接入仅替换本适配器为 EnterpriseOrderAdapter，端口与快照契约不变；
- 全部查询携带 tenant_id 条件；依赖失败统一包装为 OrderDependencyError；
- 支付未知：订单存在但 mock_pay_payment 无记录时 query_payment 返回 None；
- refunded_minor 只累计已成功执行的退款（refund_executions.status='succeeded'）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from application.order.ports.order_query import OrderDependencyError, OrderQueryPort
from domain.order.snapshots import OrderSnapshot, PaymentSnapshot

# 与 data/mock/v2 systems.json 的 dataset_version 一致；契约测试锁定该值。
MOCK_SNAPSHOT_VERSION = "mock-v2"

_ORDER_SQL = text(
    "SELECT order_id, customer_id, status, currency, paid_minor, version "
    "FROM mock_oms_order WHERE tenant_id = :tenant_id AND order_id = :order_id"
)
_PAYMENTS_SQL = text(
    "SELECT payment_id, paid_minor, currency, status "
    "FROM mock_pay_payment WHERE tenant_id = :tenant_id AND order_id = :order_id"
)
_REFUND_SQL = text(
    "SELECT amount_minor, status FROM mock_pay_refund_execution "
    "WHERE tenant_id = :tenant_id AND order_id = :order_id"
)


def derive_payment_status(statuses: tuple[str, ...]) -> str:
    """汇总支付状态：无记录 unknown；状态一致取该值；不一致无法归一取 unknown。"""
    if not statuses:
        return "unknown"
    unique = set(statuses)
    if len(unique) == 1:
        return statuses[0]
    return "unknown"


def sum_refunded_minor(rows: list[tuple[int, str]]) -> int:
    """只累计已成功执行的退款金额；pending/unknown/failed 不计入已退金额。"""
    return sum(amount for amount, status in rows if status == "succeeded")


class MockOrderAdapter(OrderQueryPort):
    def __init__(self, engine) -> None:
        self._engine = engine

    def query_order(self, tenant_id: str, order_id: str) -> OrderSnapshot | None:
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    _ORDER_SQL, {"tenant_id": tenant_id, "order_id": order_id}
                ).first()
                if row is None:
                    return None
                pay_rows = conn.execute(
                    _PAYMENTS_SQL, {"tenant_id": tenant_id, "order_id": order_id}
                ).all()
            statuses = tuple(r.status for r in pay_rows)
            return OrderSnapshot(
                tenant_id=tenant_id,
                order_id=row.order_id,
                owner_customer_id=row.customer_id,
                status=row.status,
                payment_status=derive_payment_status(statuses),
                fulfillment_status=row.status,
                currency=row.currency,
                amount_minor=int(row.paid_minor),
                version=int(row.version),
                snapshot_version=MOCK_SNAPSHOT_VERSION,
                observed_at=datetime.now(timezone.utc),
            )
        except OrderDependencyError:
            raise
        except Exception as exc:  # sqlalchemy 异常统一转瞬态依赖错误
            raise OrderDependencyError(f"订单查询失败: {exc}") from exc

    def query_payment(self, tenant_id: str, order_id: str) -> PaymentSnapshot | None:
        try:
            with self._engine.connect() as conn:
                pay_rows = conn.execute(
                    _PAYMENTS_SQL, {"tenant_id": tenant_id, "order_id": order_id}
                ).all()
                if not pay_rows:
                    return None
                refund_rows = conn.execute(
                    _REFUND_SQL, {"tenant_id": tenant_id, "order_id": order_id}
                ).all()
            return PaymentSnapshot(
                tenant_id=tenant_id,
                order_id=order_id,
                payment_status=derive_payment_status(tuple(r.status for r in pay_rows)),
                paid_minor=sum(int(r.paid_minor) for r in pay_rows),
                refunded_minor=sum_refunded_minor(
                    [(int(r.amount_minor), r.status) for r in refund_rows]
                ),
                currency=pay_rows[0].currency,
                payment_ids=tuple(r.payment_id for r in pay_rows),
                snapshot_version=MOCK_SNAPSHOT_VERSION,
                observed_at=datetime.now(timezone.utc),
            )
        except OrderDependencyError:
            raise
        except Exception as exc:  # sqlalchemy 异常统一转瞬态依赖错误
            raise OrderDependencyError(f"支付查询失败: {exc}") from exc
