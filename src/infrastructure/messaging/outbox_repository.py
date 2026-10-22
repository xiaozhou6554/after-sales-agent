"""所属模块事务内写入、租约领取与发布确认后的 Outbox 状态更新。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from application.common.events import EventEnvelope, parse_event
from infrastructure.messaging.event_contract import EventCatalog


@dataclass(frozen=True)
class OutboxLease:
    event: EventEnvelope
    owner_module: str
    lease_owner: str
    attempt: int


class OutboxRepository:
    def __init__(self, engine: Engine, catalog: EventCatalog) -> None:
        self._engine = engine
        self._catalog = catalog

    def enqueue(
        self,
        connection: Connection,
        event: EventEnvelope,
        *,
        aggregate_type: str,
        aggregate_id: str,
        thread_id: str | None = None,
    ) -> None:
        self._catalog.validate(event)
        actor_id = event.subject_id or event.payload_dict().get("actor_subject_id")
        if not actor_id or not aggregate_type or not aggregate_id:
            raise ValueError("Outbox 缺少操作者或聚合引用")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        connection.execute(
            text("""INSERT INTO outbox_event (
                tenant_id,event_id,owner_module,event_type,event_version,schema_version,
                request_id,correlation_id,trace_id,thread_id,actor_subject_id,customer_id,
                aggregate_type,aggregate_id,idempotency_key,payload,status,attempts,
                next_retry_at,lease_owner,lease_until,published_at,created_at
            ) VALUES (
                :tenant_id,:event_id,:owner_module,:event_type,:event_version,:schema_version,
                :request_id,:correlation_id,:trace_id,:thread_id,:actor_subject_id,:customer_id,
                :aggregate_type,:aggregate_id,:idempotency_key,:payload,'pending',0,
                :now,NULL,NULL,NULL,:now
            )"""),
            {
                "tenant_id": event.tenant_id,
                "event_id": event.event_id,
                "owner_module": event.producer,
                "event_type": event.event_type,
                "event_version": event.event_version,
                "schema_version": event.schema_version,
                "request_id": event.request_id,
                "correlation_id": event.correlation_id,
                "trace_id": event.trace_id,
                "thread_id": thread_id,
                "actor_subject_id": actor_id,
                "customer_id": event.payload_dict().get("customer_id"),
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "idempotency_key": event.idempotency_key,
                "payload": json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True),
                "now": now,
            },
        )

    def pending_tenants(self, owner_module: str) -> list[str]:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.connect() as connection:
            rows = connection.execute(
                text("""SELECT DISTINCT tenant_id FROM outbox_event
                    WHERE owner_module=:owner_module AND
                    ((status='pending' AND next_retry_at<=:now) OR
                     (status='publishing' AND lease_until<:now))"""),
                {"owner_module": owner_module, "now": now},
            )
            return [str(row[0]) for row in rows]

    def claim(
        self,
        owner_module: str,
        tenant_id: str,
        lease_owner: str,
        *,
        limit: int = 20,
        lease_s: int = 30,
    ) -> list[OutboxLease]:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        until = now + timedelta(seconds=lease_s)
        with self._engine.begin() as connection:
            rows = (
                connection.execute(
                    text("""SELECT tenant_id,event_id,payload,attempts FROM outbox_event
                    WHERE owner_module=:owner_module AND tenant_id=:tenant_id AND
                    ((status='pending' AND next_retry_at<=:now) OR
                     (status='publishing' AND lease_until<:now))
                    ORDER BY next_retry_at,event_id LIMIT :limit FOR UPDATE SKIP LOCKED"""),
                    {
                        "owner_module": owner_module,
                        "tenant_id": tenant_id,
                        "now": now,
                        "limit": limit,
                    },
                )
                .mappings()
                .all()
            )
            leases = []
            for row in rows:
                attempt = int(row["attempts"]) + 1
                connection.execute(
                    text("""UPDATE outbox_event SET status='publishing',attempts=:attempt,
                        lease_owner=:lease_owner,lease_until=:until
                        WHERE tenant_id=:tenant_id AND event_id=:event_id"""),
                    {
                        "attempt": attempt,
                        "lease_owner": lease_owner,
                        "until": until,
                        "tenant_id": row["tenant_id"],
                        "event_id": row["event_id"],
                    },
                )
                body = row["payload"]
                if isinstance(body, str):
                    body = json.loads(body)
                leases.append(OutboxLease(parse_event(body), owner_module, lease_owner, attempt))
            return leases

    def mark_published(self, lease: OutboxLease) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(
                text("""UPDATE outbox_event SET status='published',published_at=:now,
                    lease_owner=NULL,lease_until=NULL
                    WHERE tenant_id=:tenant_id AND event_id=:event_id AND status='publishing'
                    AND lease_owner=:lease_owner AND lease_until>=:now
                    AND attempts=:attempt"""),
                {
                    "now": datetime.now(timezone.utc).replace(tzinfo=None),
                    "tenant_id": lease.event.tenant_id,
                    "event_id": lease.event.event_id,
                    "lease_owner": lease.lease_owner,
                    "attempt": lease.attempt,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("Outbox 发布租约已过期或被其他 Worker 领取")

    def mark_retry(self, lease: OutboxLease, *, delay_s: int = 5) -> None:
        next_retry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=delay_s)
        with self._engine.begin() as connection:
            result = connection.execute(
                text("""UPDATE outbox_event SET status='pending',next_retry_at=:next_retry,
                    lease_owner=NULL,lease_until=NULL
                    WHERE tenant_id=:tenant_id AND event_id=:event_id AND status='publishing'
                    AND lease_owner=:lease_owner AND attempts=:attempt"""),
                {
                    "next_retry": next_retry,
                    "tenant_id": lease.event.tenant_id,
                    "event_id": lease.event.event_id,
                    "lease_owner": lease.lease_owner,
                    "attempt": lease.attempt,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("Outbox 发布租约已过期或被其他 Worker 领取")
