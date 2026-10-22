"""合成 Mock-v2 退款事实适配器；真实企业必须替换权威来源与政策。"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from application.refund.qualification import RefundLineAllocation, RefundSnapshot
from domain.refund import RefundFacts

MOCK_REFUND_POLICY_ID = "MOCK-FR-07.1"
MOCK_REFUND_POLICY_VERSION = "2026-10-v1"


class MockRefundSnapshotAdapter:
    def __init__(self, engine, connection: Connection | None = None, *, as_of: date | None = None):
        self._engine = engine
        self._connection = connection
        self._as_of = as_of

    def bind(self, connection: Connection) -> "MockRefundSnapshotAdapter":
        return MockRefundSnapshotAdapter(self._engine, connection, as_of=self._as_of)

    def load(self, tenant_id: str, order_id: str) -> RefundSnapshot | None:
        if self._connection is not None:
            return self._load(self._connection, tenant_id, order_id)
        with self._engine.connect() as connection:
            return self._load(connection, tenant_id, order_id)

    def _load(self, connection: Connection, tenant_id: str, order_id: str) -> RefundSnapshot | None:
        params = {"tenant_id": tenant_id, "order_id": order_id}
        order = (
            connection.execute(
                text(
                    "SELECT customer_id, status, currency, paid_minor, version FROM mock_oms_order"
                    " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                ),
                params,
            )
            .mappings()
            .first()
        )
        if order is None:
            return None
        payments = (
            connection.execute(
                text(
                    "SELECT payment_id, paid_minor, currency, status FROM mock_pay_payment"
                    " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                ),
                params,
            )
            .mappings()
            .all()
        )
        details = (
            connection.execute(
                text(
                    "SELECT d.payment_id, d.line_id, d.paid_goods_minor,"
                    " d.paid_shipping_minor, d.paid_minor"
                    " FROM mock_pay_payment_detail d WHERE d.tenant_id=:tenant_id"
                    " AND d.order_id=:order_id"
                ),
                params,
            )
            .mappings()
            .all()
        )
        refund_rows = (
            connection.execute(
                text(
                    "SELECT x.refund_id, x.amount_minor, x.currency, x.status,"
                    " COALESCE(SUM(d.refunded_goods_minor),0) AS goods_minor"
                    " FROM mock_pay_refund_execution x LEFT JOIN mock_pay_refund_detail d"
                    " ON d.tenant_id=x.tenant_id AND d.refund_id=x.refund_id"
                    " WHERE x.tenant_id=:tenant_id AND x.order_id=:order_id"
                    " GROUP BY x.refund_id,x.amount_minor,x.currency,x.status"
                ),
                params,
            )
            .mappings()
            .all()
        )
        refund_details = (
            connection.execute(
                text(
                    "SELECT d.refund_id, d.refunded_goods_minor, d.status, d.currency,"
                    " p.line_id, p.order_id AS detail_order_id,"
                    " p.payment_id AS detail_payment_id,"
                    " x.payment_id AS execution_payment_id"
                    " FROM mock_pay_refund_detail d"
                    " JOIN mock_pay_refund_execution x"
                    " ON x.tenant_id=d.tenant_id AND x.refund_id=d.refund_id"
                    " JOIN mock_pay_payment_detail p"
                    " ON p.tenant_id=d.tenant_id AND p.payment_detail_id=d.payment_detail_id"
                    " WHERE x.tenant_id=:tenant_id AND x.order_id=:order_id"
                ),
                params,
            )
            .mappings()
            .all()
        )
        requests = (
            connection.execute(
                text(
                    "SELECT refund_request_id, review_status, goods_minor, shipping_minor"
                    " FROM refund_request WHERE tenant_id=:tenant_id AND order_id=:order_id"
                    " AND review_status IN ('pending_review','reviewing','approved')"
                ),
                params,
            )
            .mappings()
            .all()
        )
        batches = (
            connection.execute(
                text(
                    "SELECT quantity, signed_at FROM mock_oms_shipment_batch"
                    " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                ),
                params,
            )
            .mappings()
            .all()
        )
        order_lines = (
            connection.execute(
                text(
                    "SELECT line_id,quantity FROM mock_oms_order_line"
                    " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                ),
                params,
            )
            .mappings()
            .all()
        )
        line_qty = sum(int(row["quantity"]) for row in order_lines)

        payment_statuses = {str(row["status"]) for row in payments}
        if not payments:
            payment_status = "unknown"
        elif payment_statuses == {"succeeded"}:
            payment_status = "succeeded"
        elif payment_statuses in ({"failed"}, {"unpaid"}):
            payment_status = next(iter(payment_statuses))
        else:
            payment_status = "unknown"

        paid_total = sum(int(row["paid_minor"]) for row in payments)
        paid_goods = sum(int(row["paid_goods_minor"]) for row in details)
        paid_shipping = sum(int(row["paid_shipping_minor"]) for row in details)
        detail_total = sum(int(row["paid_minor"]) for row in details)
        refunded_goods = sum(
            int(row["goods_minor"]) for row in refund_rows if row["status"] == "succeeded"
        )
        refunded_shipping = sum(
            int(row["amount_minor"]) - int(row["goods_minor"])
            for row in refund_rows
            if row["status"] == "succeeded"
        )
        reserved_goods = sum(int(row["goods_minor"]) for row in requests)
        reserved_shipping = sum(int(row["shipping_minor"]) for row in requests)
        currencies = {str(row["currency"]) for row in payments + refund_rows}
        status_conflict = bool(payments) and (
            paid_total != int(order["paid_minor"])
            or detail_total != paid_total
            or paid_goods + paid_shipping != detail_total
            or currencies != {str(order["currency"])}
        )
        succeeded_refunds = {
            str(row["refund_id"]): row for row in refund_rows if row["status"] == "succeeded"
        }
        refunded_by_line: dict[str, int] = {}
        detail_goods_by_refund: dict[str, int] = {}
        for row in refund_details:
            refund_id = str(row["refund_id"])
            if refund_id not in succeeded_refunds:
                continue
            if (
                row["status"] != "succeeded"
                or row["currency"] != order["currency"]
                or row["detail_order_id"] != order_id
                or row["detail_payment_id"] != row["execution_payment_id"]
            ):
                status_conflict = True
            line_id = str(row["line_id"])
            amount = int(row["refunded_goods_minor"])
            refunded_by_line[line_id] = refunded_by_line.get(line_id, 0) + amount
            detail_goods_by_refund[refund_id] = detail_goods_by_refund.get(refund_id, 0) + amount
        if any(
            detail_goods_by_refund.get(refund_id, 0) != int(row["goods_minor"])
            or detail_goods_by_refund.get(refund_id, 0) > int(row["amount_minor"])
            or (int(row["amount_minor"]) > 0 and not detail_goods_by_refund.get(refund_id, 0))
            for refund_id, row in succeeded_refunds.items()
        ):
            status_conflict = True
        signed_qty = sum(int(row["quantity"]) for row in batches)
        if order["status"] == "delivered" and (not batches or signed_qty != int(line_qty)):
            status_conflict = True
        if order["status"] != "delivered" and batches:
            status_conflict = True
        delivered_on = max(row["signed_at"] for row in batches).date() if batches else None
        facts = RefundFacts(
            order_id=order_id,
            reason_code=None,
            reason_category=None,
            currency=str(order["currency"]),
            payment_status=payment_status,
            fulfillment_status=str(order["status"]),
            fulfillment_locked=order["status"] != "pending_shipment",
            status_conflict=status_conflict,
            paid_goods_minor=paid_goods if details else None,
            paid_shipping_minor=paid_shipping if details else None,
            paid_total_minor=paid_total if payments else None,
            refunded_goods_minor=refunded_goods,
            refunded_shipping_minor=refunded_shipping,
            reserved_goods_minor=reserved_goods,
            reserved_shipping_minor=reserved_shipping,
            active_request_id=str(requests[0]["refund_request_id"]) if requests else None,
            policy_id=MOCK_REFUND_POLICY_ID,
            policy_version=MOCK_REFUND_POLICY_VERSION,
            policy_valid=True,
            manual_submission_allowed=True,
            order_version=str(order["version"]),
            payment_version="mock-v2",
            delivered_on=delivered_on,
            as_of=self._as_of or datetime.now(timezone.utc).date(),
        )
        # A historical goods refund is quotable only when its payment-detail link
        # proves the line and its quantity is one; never infer quantity from money.
        allocations: tuple[RefundLineAllocation, ...] = ()
        if (
            payment_status == "succeeded"
            and not status_conflict
            and not requests
            and order_lines
            and details
            and (not succeeded_refunds or order["status"] == "delivered")
        ):
            paid_by_line: dict[str, tuple[int, int]] = {}
            for row in details:
                line_id = str(row["line_id"])
                goods, shipping = paid_by_line.get(line_id, (0, 0))
                paid_by_line[line_id] = (
                    goods + int(row["paid_goods_minor"]),
                    shipping + int(row["paid_shipping_minor"]),
                )
            quantities = {str(row["line_id"]): int(row["quantity"]) for row in order_lines}
            if (
                set(paid_by_line) == set(quantities)
                and set(refunded_by_line) <= set(quantities)
                and all(
                    amount <= paid_by_line[line_id][0] and (amount == 0 or quantities[line_id] == 1)
                    for line_id, amount in refunded_by_line.items()
                )
            ):
                allocations = tuple(
                    RefundLineAllocation(
                        str(row["line_id"]),
                        int(row["quantity"]),
                        paid_by_line[str(row["line_id"])][0]
                        - refunded_by_line.get(str(row["line_id"]), 0),
                        paid_by_line[str(row["line_id"])][1] if not succeeded_refunds else 0,
                    )
                    for row in order_lines
                    if int(row["quantity"]) > 0
                    and (
                        paid_by_line[str(row["line_id"])][0]
                        - refunded_by_line.get(str(row["line_id"]), 0)
                        > 0
                        or (not succeeded_refunds and paid_by_line[str(row["line_id"])][1] > 0)
                    )
                )
        return RefundSnapshot(tenant_id, str(order["customer_id"]), facts, allocations)
