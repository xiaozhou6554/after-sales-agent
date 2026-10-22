"""MySQL 操作幂等记录；本地业务写入可通过 bind 共用事务。"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from application.common.ports.idempotency import IdempotencyConflictError, IdempotencyRecord


class MySQLIdempotencyStore:
    def __init__(self, engine: Engine, connection: Connection | None = None) -> None:
        self._engine = engine
        self._connection = connection

    def bind(self, connection: Connection) -> "MySQLIdempotencyStore":
        return MySQLIdempotencyStore(self._engine, connection)

    @contextmanager
    def _transaction(self):
        if self._connection is not None:
            yield self._connection
        else:
            with self._engine.begin() as connection:
                yield connection

    @staticmethod
    def _record(row) -> IdempotencyRecord:
        return IdempotencyRecord(
            row.record_id,
            row.tenant_id,
            row.actor_subject_id,
            row.operation_type,
            row.idempotency_key,
            row.request_digest,
            row.status,
            row.result_ref,
            int(row.revision),
        )

    @staticmethod
    def _get(connection, tenant_id, actor_subject_id, operation_type, idempotency_key, lock=False):
        suffix = " FOR UPDATE" if lock else ""
        return connection.execute(
            text(
                "SELECT * FROM operation_idempotency WHERE tenant_id=:tenant_id "
                "AND actor_subject_id=:actor_subject_id AND operation_type=:operation_type "
                "AND idempotency_key=:idempotency_key" + suffix
            ),
            {
                "tenant_id": tenant_id,
                "actor_subject_id": actor_subject_id,
                "operation_type": operation_type,
                "idempotency_key": idempotency_key,
            },
        ).first()

    def reserve(
        self,
        tenant_id: str,
        actor_subject_id: str,
        operation_type: str,
        idempotency_key: str,
        request_digest: str,
    ) -> IdempotencyRecord:
        if not idempotency_key or len(idempotency_key) > 64 or len(request_digest) != 64:
            raise ValueError("幂等键或请求摘要无效")
        with self._transaction() as connection:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            connection.execute(
                text(
                    "INSERT IGNORE INTO operation_idempotency (tenant_id, record_id,"
                    " actor_subject_id, operation_type, idempotency_key, request_digest,"
                    " status, created_at, updated_at) VALUES (:tenant_id, :record_id,"
                    " :actor_subject_id, :operation_type, :idempotency_key, :request_digest,"
                    " 'pending', :created_at, :updated_at)"
                ),
                {
                    "tenant_id": tenant_id,
                    "record_id": uuid.uuid4().hex,
                    "actor_subject_id": actor_subject_id,
                    "operation_type": operation_type,
                    "idempotency_key": idempotency_key,
                    "request_digest": request_digest,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = self._get(
                connection, tenant_id, actor_subject_id, operation_type, idempotency_key, lock=True
            )
            if row.request_digest != request_digest:
                raise IdempotencyConflictError("同一幂等键绑定了不同请求")
            return self._record(row)

    def get(
        self, tenant_id: str, actor_subject_id: str, operation_type: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        with self._transaction() as connection:
            row = self._get(
                connection, tenant_id, actor_subject_id, operation_type, idempotency_key
            )
            return self._record(row) if row is not None else None

    def complete(self, record: IdempotencyRecord, status: str, result_ref: str | None) -> bool:
        if status not in ("succeeded", "failed", "unknown"):
            raise ValueError("幂等结果状态无效")
        with self._transaction() as connection:
            updated = connection.execute(
                text(
                    "UPDATE operation_idempotency SET status=:status, result_ref=:result_ref,"
                    " revision=revision+1, updated_at=:updated_at WHERE tenant_id=:tenant_id"
                    " AND record_id=:record_id AND revision=:revision"
                    " AND status IN ('pending','unknown')"
                ),
                {
                    "status": status,
                    "result_ref": result_ref,
                    "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
                    "tenant_id": record.tenant_id,
                    "record_id": record.record_id,
                    "revision": record.revision,
                },
            )
            return updated.rowcount == 1
