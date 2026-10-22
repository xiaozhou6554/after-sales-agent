"""退款申请只读投影；WMS/资金尚未接入时显式返回 unknown。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from application.refund.query import RefundRequestSnapshot


class MySqlRefundRequestReadRepository:
    def __init__(self, engine, connection: Connection | None = None) -> None:
        self._engine = engine
        self._connection = connection

    def bind(self, connection: Connection) -> "MySqlRefundRequestReadRepository":
        return MySqlRefundRequestReadRepository(self._engine, connection)

    @contextmanager
    def _read(self):
        if self._connection is not None:
            yield self._connection
        else:
            with self._engine.connect() as connection:
                yield connection

    @staticmethod
    def _project(row) -> RefundRequestSnapshot | None:
        if row is None:
            return None
        return RefundRequestSnapshot(
            tenant_id=row.tenant_id,
            refund_request_id=row.refund_request_id,
            order_id=row.order_id,
            customer_id=row.customer_id,
            actor_subject_id=row.actor_subject_id,
            review_status=row.review_status,
            return_status="unknown",  # 25A 尚未接入，不能宣称未退货或已验收。
            funds_status="unknown",  # 25B 尚未接入，不能宣称未执行或已退款。
            amount_minor=int(row.amount_minor),
            currency=row.currency,
            revision=int(row.review_revision) + 1,
            updated_at=row.updated_at.replace(tzinfo=timezone.utc),
        )

    def load(self, tenant_id: str, refund_request_id: str) -> RefundRequestSnapshot | None:
        with self._read() as connection:
            row = connection.execute(
                text(
                    "SELECT tenant_id,refund_request_id,order_id,customer_id,"
                    "actor_subject_id,review_status,review_revision,amount_minor,currency,"
                    "updated_at FROM refund_request WHERE tenant_id=:tenant_id"
                    " AND refund_request_id=:refund_request_id"
                ),
                {"tenant_id": tenant_id, "refund_request_id": refund_request_id},
            ).first()
        return self._project(row)

    def load_by_operation(
        self, tenant_id: str, actor_subject_id: str, idempotency_key: str
    ) -> RefundRequestSnapshot | None:
        with self._read() as connection:
            row = connection.execute(
                text(
                    "SELECT tenant_id,refund_request_id,order_id,customer_id,"
                    "actor_subject_id,review_status,review_revision,amount_minor,currency,"
                    "updated_at FROM refund_request WHERE tenant_id=:tenant_id"
                    " AND actor_subject_id=:actor_subject_id"
                    " AND idempotency_key=:idempotency_key"
                ),
                {
                    "tenant_id": tenant_id,
                    "actor_subject_id": actor_subject_id,
                    "idempotency_key": idempotency_key,
                },
            ).first()
        return self._project(row)
