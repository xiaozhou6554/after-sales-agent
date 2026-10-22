"""MySQL 客户通知只读适配器。"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Engine


class MySQLNotificationReader:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _row(row) -> dict:
        return {
            "notification_id": row["notification_id"],
            "channel": row["channel"],
            "template_id": row["template_id"],
            "resource_refs": json.loads(row["resource_refs"]),
            "status": row["status"],
            "sent_at": row["sent_at"].isoformat() if row["sent_at"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }

    def list_for_customer(
        self, tenant_id: str, customer_ids: tuple[str, ...], status: str | None, limit: int
    ) -> list[dict]:
        placeholders = ", ".join(f":c{i}" for i in range(len(customer_ids)))
        sql = (
            "SELECT notification_id,channel,template_id,resource_refs,status,sent_at,created_at"
            f" FROM notification WHERE tenant_id=:tenant_id AND customer_id IN ({placeholders})"
        )
        params: dict = {"tenant_id": tenant_id, "limit": limit}
        params.update({f"c{i}": cid for i, cid in enumerate(customer_ids)})
        if status is not None:
            sql += " AND status=:status"
            params["status"] = status
        sql += " ORDER BY created_at DESC, notification_id DESC LIMIT :limit"
        with self._engine.connect() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
        return [self._row(row) for row in rows]

    def get_for_customer(
        self, tenant_id: str, customer_ids: tuple[str, ...], notification_id: str
    ) -> dict | None:
        placeholders = ", ".join(f":c{i}" for i in range(len(customer_ids)))
        params = {
            "tenant_id": tenant_id,
            "notification_id": notification_id,
            **{f"c{i}": cid for i, cid in enumerate(customer_ids)},
        }
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT notification_id,channel,template_id,resource_refs,status,"
                    "sent_at,created_at FROM notification WHERE tenant_id=:tenant_id"
                    f" AND notification_id=:notification_id AND customer_id IN ({placeholders})"
                ),
                params,
            ).mappings().first()
        return self._row(row) if row is not None else None
