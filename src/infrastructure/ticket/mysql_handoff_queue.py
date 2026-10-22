"""Transactional, tenant-bounded Mock human queue."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from application.common.context import TraceContext
from application.common.events import EventEnvelope, HumanHandoffCreatedPayload
from application.common.ports.outbox import OutboxPort
from application.ticket.handoff import HandoffConflict, HandoffResult
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository


class MySQLHandoffQueue:
    def __init__(self, engine: Engine, outbox: OutboxPort | None = None) -> None:
        self._engine = engine
        if outbox is None:
            contracts = Path(__file__).resolve().parents[3] / "data" / "contracts"
            catalog = EventCatalog(
                contracts / "event-catalog.json", contracts / "events.schema.json"
            )
            outbox = OutboxRepository(engine, catalog)
        self._outbox = outbox

    def get(self, tenant_id: str, handoff_id: str) -> tuple[str, HandoffResult] | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT customer_id,handoff_id,status,revision,ticket_id,assigned_to "
                        "FROM human_handoff WHERE tenant_id=:tenant AND handoff_id=:id"
                    ),
                    {"tenant": tenant_id, "id": handoff_id},
                )
                .mappings()
                .first()
            )
        return (row["customer_id"], self._result(row)) if row else None

    @staticmethod
    def _result(row, *, replayed: bool = False) -> HandoffResult:
        return HandoffResult(
            row["handoff_id"],
            row["status"],
            row["revision"],
            row["ticket_id"],
            row["assigned_to"],
            replayed,
        )

    @staticmethod
    def _ticket(conn, tenant_id: str, customer_id: str, ticket_id: str | None) -> None:
        if ticket_id is None:
            return
        row = conn.execute(
            text("SELECT customer_id FROM ticket WHERE tenant_id=:tenant AND ticket_id=:ticket"),
            {"tenant": tenant_id, "ticket": ticket_id},
        ).first()
        if row is None or row[0] != customer_id:
            raise HandoffConflict("linked ticket unavailable")

    def request(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        customer_id: str,
        requested_by: str,
        reason_code: str,
        summary_masked: str,
        context_snapshot: dict,
        trace: TraceContext,
        idempotency_key: str,
        request_digest: str,
        available: bool,
        capacity: int,
        ticket_id: str | None,
    ) -> HandoffResult:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            with self._engine.begin() as conn:
                # The capacity row serializes all requests for this tenant.
                conn.execute(
                    text("INSERT IGNORE INTO human_handoff_capacity (tenant_id) VALUES (:tenant)"),
                    {"tenant": tenant_id},
                )
                active = conn.execute(
                    text(
                        "SELECT active_count FROM human_handoff_capacity "
                        "WHERE tenant_id=:tenant FOR UPDATE"
                    ),
                    {"tenant": tenant_id},
                ).scalar_one()
                prior = (
                    conn.execute(
                        text(
                            "SELECT request_digest,handoff_id FROM human_handoff_idempotency "
                            "WHERE tenant_id=:tenant AND requested_by=:actor "
                            "AND idempotency_key=:key"
                        ),
                        {"tenant": tenant_id, "actor": requested_by, "key": idempotency_key},
                    )
                    .mappings()
                    .first()
                )
                if prior:
                    if prior["request_digest"] != request_digest:
                        raise HandoffConflict("idempotency key reused for different content")
                    row = (
                        conn.execute(
                            text(
                                "SELECT handoff_id,status,revision,ticket_id,assigned_to "
                                "FROM human_handoff WHERE tenant_id=:tenant AND handoff_id=:id"
                            ),
                            {"tenant": tenant_id, "id": prior["handoff_id"]},
                        )
                        .mappings()
                        .one()
                    )
                    return self._result(row, replayed=True)
                self._ticket(conn, tenant_id, customer_id, ticket_id)
                status = "queued" if available and active < capacity else "unavailable"
                conn.execute(
                    text(
                        "INSERT INTO human_handoff (tenant_id,handoff_id,ticket_id,customer_id,"
                        "requested_by,reason_code,summary_masked,context_snapshot,status,assigned_to,revision,"
                        "thread_id,correlation_id,created_at,updated_at) "
                        "VALUES (:tenant,:id,:ticket,:customer,"
                        ":actor,:reason,:summary,:context,:status,NULL,1,:thread,:correlation,:now,:now)"
                    ),
                    {
                        "tenant": tenant_id,
                        "id": handoff_id,
                        "ticket": ticket_id,
                        "customer": customer_id,
                        "actor": requested_by,
                        "reason": reason_code,
                        "summary": summary_masked,
                        "context": json.dumps(context_snapshot, ensure_ascii=False),
                        "status": status,
                        "thread": trace.thread_id,
                        "correlation": trace.correlation_id,
                        "now": now,
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO human_handoff_idempotency "
                        "(tenant_id,requested_by,idempotency_key,request_digest,"
                        "handoff_id,created_at) "
                        "VALUES (:tenant,:actor,:key,:digest,:id,:now)"
                    ),
                    {
                        "tenant": tenant_id,
                        "actor": requested_by,
                        "key": idempotency_key,
                        "digest": request_digest,
                        "id": handoff_id,
                        "now": now,
                    },
                )
                if status == "queued":
                    conn.execute(
                        text(
                            "UPDATE human_handoff_capacity SET active_count=active_count+1 "
                            "WHERE tenant_id=:tenant"
                        ),
                        {"tenant": tenant_id},
                    )
                event = EventEnvelope(
                    schema_version="1.0",
                    event_id=uuid.uuid5(
                        uuid.NAMESPACE_URL, f"human.handoff.created:{handoff_id}"
                    ).hex,
                    event_type="human.handoff.created",
                    event_version=1,
                    tenant_id=tenant_id,
                    request_id=trace.request_id,
                    correlation_id=trace.correlation_id,
                    trace_id=trace.trace_id,
                    operation_id=handoff_id,
                    causation_id=None,
                    parent_event_id=None,
                    producer="handoff-service",
                    occurred_at=datetime.now(timezone.utc).isoformat(),
                    idempotency_key=f"human.handoff.created:{handoff_id}",
                    subject_id=requested_by,
                    retry_count=0,
                    payload=HumanHandoffCreatedPayload(
                        handoff_id=handoff_id,
                        ticket_id=ticket_id,
                        status=status,
                        summary_ref=f"handoff:{handoff_id}:summary",
                        actor_subject_id=requested_by,
                        customer_id=customer_id,
                    ),
                )
                self._outbox.enqueue(
                    conn,
                    event,
                    aggregate_type="human_handoff",
                    aggregate_id=handoff_id,
                    thread_id=trace.thread_id,
                )
                return HandoffResult(handoff_id, status, 1, ticket_id, None)
        except IntegrityError as exc:
            raise HandoffConflict("handoff identity conflict") from exc

    def claim(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        actor_id: str,
        expected_revision: int,
        idempotency_key: str,
        ticket_id: str | None,
    ) -> HandoffResult:
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT customer_id,handoff_id,status,revision,ticket_id,assigned_to "
                        "FROM human_handoff WHERE tenant_id=:tenant AND handoff_id=:id FOR UPDATE"
                    ),
                    {"tenant": tenant_id, "id": handoff_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise HandoffConflict("handoff unavailable")
            claimed = (
                conn.execute(
                    text(
                        "SELECT actor_id,idempotency_key FROM human_handoff_claim "
                        "WHERE tenant_id=:tenant AND handoff_id=:id"
                    ),
                    {"tenant": tenant_id, "id": handoff_id},
                )
                .mappings()
                .first()
            )
            if (
                row["status"] == "accepted"
                and row["assigned_to"] == actor_id
                and claimed is not None
                and claimed["idempotency_key"] == idempotency_key
            ):
                if ticket_id is not None and ticket_id != row["ticket_id"]:
                    raise HandoffConflict("ticket association changed")
                return self._result(row, replayed=True)
            if row["status"] != "queued" or row["revision"] != expected_revision:
                raise HandoffConflict("handoff already claimed or revision changed")
            linked = row["ticket_id"]
            if linked is not None and ticket_id is not None and linked != ticket_id:
                raise HandoffConflict("ticket association changed")
            linked = linked or ticket_id
            self._ticket(conn, tenant_id, row["customer_id"], linked)
            count = conn.execute(
                text(
                    "UPDATE human_handoff SET status='accepted',assigned_to=:actor,"
                    "ticket_id=:ticket,revision=revision+1,updated_at=:now WHERE tenant_id=:tenant "
                    "AND handoff_id=:id AND status='queued' AND revision=:revision"
                ),
                {
                    "actor": actor_id,
                    "ticket": linked,
                    "now": datetime.now(timezone.utc).replace(tzinfo=None),
                    "tenant": tenant_id,
                    "id": handoff_id,
                    "revision": expected_revision,
                },
            ).rowcount
            if count != 1:
                raise HandoffConflict("handoff claim conflict")
            conn.execute(
                text(
                    "INSERT INTO human_handoff_claim "
                    "(tenant_id,handoff_id,actor_id,idempotency_key) "
                    "VALUES (:tenant,:id,:actor,:key)"
                ),
                {"tenant": tenant_id, "id": handoff_id, "actor": actor_id, "key": idempotency_key},
            )
            conn.execute(
                text(
                    "UPDATE human_handoff_capacity SET active_count=active_count-1 "
                    "WHERE tenant_id=:tenant AND active_count>0"
                ),
                {"tenant": tenant_id},
            )
            return HandoffResult(handoff_id, "accepted", expected_revision + 1, linked, actor_id)
