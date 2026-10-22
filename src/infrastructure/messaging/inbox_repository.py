"""Inbox 尝试计数、租约与业务键守卫；业务结果和成功状态同事务提交。"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from application.common.events import EventEnvelope


class InboxConflictError(RuntimeError):
    """同一事件或业务键携带不同请求内容。"""


class InboxLeaseLostError(RuntimeError):
    """旧 Worker 已失去消费提交权。"""


@dataclass(frozen=True)
class InboxLease:
    event: EventEnvelope
    consumer_name: str
    worker_id: str
    fencing_token: int
    attempt: int
    digest: str


def request_digest(event: EventEnvelope) -> str:
    content = json.dumps(
        [event.event_type, event.idempotency_key, event.payload_dict()],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class InboxRepository:
    def __init__(self, engine: Engine, *, max_retries: int = 3) -> None:
        self._engine = engine
        self._max_attempts = max_retries + 1

    def claim(
        self, event: EventEnvelope, consumer_name: str, worker_id: str, *, lease_s: int = 30
    ) -> tuple[str, InboxLease | None]:
        digest = request_digest(event)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        until = now + timedelta(seconds=lease_s)
        with self._engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO consumer_inbox (
                    tenant_id,inbox_id,consumer_name,event_id,event_type,idempotency_key,
                    request_digest,status,attempts,lease_owner,lease_until,fencing_token,
                    processed_at,result_ref,error
                ) VALUES (
                    :tenant_id,:inbox_id,:consumer_name,:event_id,:event_type,:idempotency_key,
                    :digest,'failed',0,NULL,NULL,0,NULL,NULL,NULL
                ) ON DUPLICATE KEY UPDATE inbox_id=inbox_id"""),
                {
                    "tenant_id": event.tenant_id,
                    "inbox_id": uuid.uuid4().hex,
                    "consumer_name": consumer_name,
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "idempotency_key": event.idempotency_key,
                    "digest": digest,
                },
            )
            row = (
                connection.execute(
                    text("""SELECT request_digest,status,attempts,lease_owner,lease_until,
                    fencing_token FROM consumer_inbox WHERE tenant_id=:tenant_id
                    AND consumer_name=:consumer_name AND event_id=:event_id FOR UPDATE"""),
                    {
                        "tenant_id": event.tenant_id,
                        "consumer_name": consumer_name,
                        "event_id": event.event_id,
                    },
                )
                .mappings()
                .one()
            )
            if row["request_digest"] != digest:
                raise InboxConflictError("相同 event_id 的内容摘要不一致")
            if row["status"] == "succeeded":
                return "duplicate", None
            if row["status"] == "dead_letter":
                return "dead_letter", None
            if row["status"] == "failed" and row["attempts"] > event.retry_count:
                return "retry_handoff", None
            if row["status"] == "processing" and row["lease_until"] > now:
                return "busy", None
            if row["attempts"] >= self._max_attempts:
                connection.execute(
                    text("""UPDATE consumer_inbox SET status='dead_letter',lease_owner=NULL,
                        lease_until=NULL WHERE tenant_id=:tenant_id AND consumer_name=:consumer_name
                        AND event_id=:event_id"""),
                    {
                        "tenant_id": event.tenant_id,
                        "consumer_name": consumer_name,
                        "event_id": event.event_id,
                    },
                )
                return "dead_letter", None
            attempt = int(row["attempts"]) + 1
            fencing_token = int(row["fencing_token"]) + 1
            connection.execute(
                text("""UPDATE consumer_inbox SET status='processing',attempts=:attempt,
                    lease_owner=:worker_id,lease_until=:until,fencing_token=:fencing_token,
                    error=NULL WHERE tenant_id=:tenant_id AND consumer_name=:consumer_name
                    AND event_id=:event_id"""),
                {
                    "attempt": attempt,
                    "worker_id": worker_id,
                    "until": until,
                    "fencing_token": fencing_token,
                    "tenant_id": event.tenant_id,
                    "consumer_name": consumer_name,
                    "event_id": event.event_id,
                },
            )
            return "claimed", InboxLease(
                event, consumer_name, worker_id, fencing_token, attempt, digest
            )

    def complete(
        self,
        lease: InboxLease,
        handler: Callable[[Connection, EventEnvelope], str | None],
    ) -> str | None:
        event = lease.event
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    text("""SELECT status,lease_owner,fencing_token FROM consumer_inbox
                    WHERE tenant_id=:tenant_id AND consumer_name=:consumer_name
                    AND event_id=:event_id FOR UPDATE"""),
                    {
                        "tenant_id": event.tenant_id,
                        "consumer_name": lease.consumer_name,
                        "event_id": event.event_id,
                    },
                )
                .mappings()
                .one()
            )
            if (
                row["status"] != "processing"
                or row["lease_owner"] != lease.worker_id
                or row["fencing_token"] != lease.fencing_token
            ):
                raise InboxLeaseLostError("消费租约已被其他 Worker 接管")
            connection.execute(
                text("""INSERT INTO consumer_business_guard (
                    tenant_id,guard_id,consumer_name,event_type,idempotency_key,
                    request_digest,status,result_ref,revision
                ) VALUES (
                    :tenant_id,:guard_id,:consumer_name,:event_type,:idempotency_key,
                    :digest,'processing',NULL,0
                ) ON DUPLICATE KEY UPDATE guard_id=guard_id"""),
                {
                    "tenant_id": event.tenant_id,
                    "guard_id": uuid.uuid4().hex,
                    "consumer_name": lease.consumer_name,
                    "event_type": event.event_type,
                    "idempotency_key": event.idempotency_key,
                    "digest": lease.digest,
                },
            )
            guard = (
                connection.execute(
                    text("""SELECT request_digest,status,result_ref FROM consumer_business_guard
                    WHERE tenant_id=:tenant_id AND consumer_name=:consumer_name
                    AND event_type=:event_type AND idempotency_key=:idempotency_key
                    FOR UPDATE"""),
                    {
                        "tenant_id": event.tenant_id,
                        "consumer_name": lease.consumer_name,
                        "event_type": event.event_type,
                        "idempotency_key": event.idempotency_key,
                    },
                )
                .mappings()
                .one()
            )
            if guard["request_digest"] != lease.digest:
                raise InboxConflictError("相同业务键的内容摘要不一致")
            if guard["status"] == "succeeded":
                result_ref = guard["result_ref"]
            else:
                result_ref = handler(connection, event)
                connection.execute(
                    text("""UPDATE consumer_business_guard SET status='succeeded',
                        result_ref=:result_ref,revision=revision+1 WHERE tenant_id=:tenant_id
                        AND consumer_name=:consumer_name AND event_type=:event_type
                        AND idempotency_key=:idempotency_key"""),
                    {
                        "result_ref": result_ref,
                        "tenant_id": event.tenant_id,
                        "consumer_name": lease.consumer_name,
                        "event_type": event.event_type,
                        "idempotency_key": event.idempotency_key,
                    },
                )
            connection.execute(
                text("""UPDATE consumer_inbox SET status='succeeded',processed_at=:now,
                    result_ref=:result_ref,lease_owner=NULL,lease_until=NULL,error=NULL
                    WHERE tenant_id=:tenant_id AND consumer_name=:consumer_name
                    AND event_id=:event_id"""),
                {
                    "now": datetime.now(timezone.utc).replace(tzinfo=None),
                    "result_ref": result_ref,
                    "tenant_id": event.tenant_id,
                    "consumer_name": lease.consumer_name,
                    "event_id": event.event_id,
                },
            )
            return result_ref

    def fail(self, lease: InboxLease, reason: str, *, fatal: bool = False) -> str:
        status = "dead_letter" if fatal or lease.attempt >= self._max_attempts else "failed"
        with self._engine.begin() as connection:
            result = connection.execute(
                text("""UPDATE consumer_inbox SET status=:status,error=:reason,
                    lease_owner=NULL,lease_until=NULL
                    WHERE tenant_id=:tenant_id AND consumer_name=:consumer_name
                    AND event_id=:event_id AND status='processing'
                    AND lease_owner=:worker_id AND fencing_token=:fencing_token"""),
                {
                    "status": status,
                    "reason": reason[:512],
                    "tenant_id": lease.event.tenant_id,
                    "consumer_name": lease.consumer_name,
                    "event_id": lease.event.event_id,
                    "worker_id": lease.worker_id,
                    "fencing_token": lease.fencing_token,
                },
            )
            if result.rowcount != 1:
                raise InboxLeaseLostError("失败记录提交时消费租约已丢失")
        return status
