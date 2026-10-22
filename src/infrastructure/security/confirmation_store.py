"""MySQL 一次性确认存储；bind 复用业务更新的同一事务连接。"""

from __future__ import annotations

import hmac
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from application.common.context import OperationContext
from application.common.ports.confirmation import (
    ConfirmationConflictError,
    ConfirmationRecord,
)


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc)


class MySQLConfirmationStore:
    def __init__(self, engine: Engine, connection: Connection | None = None) -> None:
        self._engine = engine
        self._connection = connection

    def bind(self, connection: Connection) -> "MySQLConfirmationStore":
        """未来业务用例在本地事务内消费确认时复用连接。"""
        return MySQLConfirmationStore(self._engine, connection)

    @contextmanager
    def _transaction(self):
        if self._connection is not None:
            yield self._connection
        else:
            with self._engine.begin() as connection:
                yield connection

    @staticmethod
    def _record(row) -> ConfirmationRecord:
        return ConfirmationRecord(
            confirmation_id=row.confirmation_id,
            binding=OperationContext(
                tenant_id=row.tenant_id,
                actor_subject_id=row.actor_subject_id,
                customer_id=row.customer_id,
                confirmer_subject_id=row.confirmer_subject_id,
                operation_type=row.operation_type,
                target_id=row.target_id,
                payload_digest=row.payload_digest,
                expected_revision=row.expected_revision,
            ),
            status=row.status,
            expires_at=_utc_aware(row.expires_at),
            consumed_by_operation_id=row.consumed_by_operation_id,
        )

    def _get(self, connection: Connection, tenant_id: str, confirmation_id: str, lock=False):
        suffix = " FOR UPDATE" if lock else ""
        return connection.execute(
            text(
                "SELECT * FROM operation_confirmation WHERE tenant_id=:tenant_id "
                "AND confirmation_id=:confirmation_id" + suffix
            ),
            {"tenant_id": tenant_id, "confirmation_id": confirmation_id},
        ).first()

    def issue(
        self, binding: OperationContext, challenge_digest: str, expires_at: datetime
    ) -> ConfirmationRecord:
        confirmation_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            connection.execute(
                text(
                    "INSERT INTO operation_confirmation"
                    " (tenant_id, confirmation_id, actor_subject_id,"
                    " customer_id, confirmer_subject_id, operation_type, target_id, payload_digest,"
                    " expected_revision, challenge_digest, status, expires_at, created_at) VALUES"
                    " (:tenant_id, :confirmation_id, :actor_subject_id, :customer_id,"
                    " :confirmer_subject_id, :operation_type, :target_id, :payload_digest,"
                    " :expected_revision, :challenge_digest, 'prepared', :expires_at, :created_at)"
                ),
                {
                    **binding.__dict__,
                    "confirmation_id": confirmation_id,
                    "challenge_digest": challenge_digest,
                    "expires_at": _utc_naive(expires_at),
                    "created_at": _utc_naive(now),
                },
            )
        return ConfirmationRecord(confirmation_id, binding, "prepared", expires_at, None)

    def verify(
        self, tenant_id: str, confirmation_id: str, challenge_digest: str, now: datetime
    ) -> ConfirmationRecord:
        with self._transaction() as connection:
            row = self._get(connection, tenant_id, confirmation_id, lock=True)
            if (
                row is None
                or row.status != "prepared"
                or row.expires_at <= _utc_naive(now)
                or not hmac.compare_digest(row.challenge_digest, challenge_digest)
            ):
                raise ConfirmationConflictError("确认无效或已过期")
            connection.execute(
                text(
                    "UPDATE operation_confirmation SET status='verified' "
                    "WHERE tenant_id=:tenant_id AND confirmation_id=:confirmation_id"
                ),
                {"tenant_id": tenant_id, "confirmation_id": confirmation_id},
            )
            record = self._record(row)
            return ConfirmationRecord(
                record.confirmation_id, record.binding, "verified", record.expires_at, None
            )

    def consume(
        self,
        confirmation_id: str,
        binding: OperationContext,
        operation_id: str,
        now: datetime,
    ) -> ConfirmationRecord:
        with self._transaction() as connection:
            row = self._get(connection, binding.tenant_id, confirmation_id, lock=True)
            if (
                row is None
                or row.status != "verified"
                or row.expires_at <= _utc_naive(now)
                or self._record(row).binding != binding
            ):
                raise ConfirmationConflictError("确认无效、已消费或内容已变化")
            connection.execute(
                text(
                    "UPDATE operation_confirmation SET status='consumed', consumed_at=:consumed_at,"
                    " consumed_by_operation_id=:operation_id WHERE tenant_id=:tenant_id"
                    " AND confirmation_id=:confirmation_id AND status='verified'"
                ),
                {
                    "consumed_at": _utc_naive(now),
                    "operation_id": operation_id,
                    "tenant_id": binding.tenant_id,
                    "confirmation_id": confirmation_id,
                },
            )
            return ConfirmationRecord(
                confirmation_id, binding, "consumed", _utc_aware(row.expires_at), operation_id
            )

    def get(self, tenant_id: str, confirmation_id: str) -> ConfirmationRecord | None:
        with self._transaction() as connection:
            row = self._get(connection, tenant_id, confirmation_id)
            return self._record(row) if row is not None else None
