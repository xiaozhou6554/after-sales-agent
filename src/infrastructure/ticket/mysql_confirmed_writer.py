"""Atomic MySQL ticket writes bound to one verified confirmation and idempotency key."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from application.common.context import OperationContext, TraceContext
from application.common.events import EventEnvelope, TicketCreatedPayload, TicketUpdatedPayload
from application.common.operation_confirmation import content_digest
from application.common.ports.confirmation import ConfirmationConflictError
from application.common.ports.outbox import OutboxPort
from domain.ticket import Ticket, TicketVersionConflict
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.persistence.mysql.repositories.ticket_repository import (
    MySQLTicketRepository,
    TicketPersistenceConflict,
    TicketWriteResult,
)
from infrastructure.security.confirmation_store import MySQLConfirmationStore


def _operation_id(binding: OperationContext, key: str) -> str:
    source = (
        f"{binding.tenant_id}\x00{binding.actor_subject_id}\x00{binding.operation_type}\x00{key}"
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, source).hex


def _operation_kind(binding: OperationContext) -> str:
    if binding.operation_type == "ticket:create":
        return "create"
    if binding.operation_type == "ticket:update":
        return "update"
    raise ValueError("unsupported ticket operation")


class MySQLConfirmedTicketWriter:
    """Atomically persist confirmation, ticket, idempotency and ticket Outbox event."""

    def __init__(self, engine: Engine, outbox: OutboxPort | None = None) -> None:
        self._engine = engine
        self._tickets = MySQLTicketRepository(engine)
        self._confirmations = MySQLConfirmationStore(engine)
        if outbox is None:
            contracts = Path(__file__).resolve().parents[3] / "data" / "contracts"
            catalog = EventCatalog(
                contracts / "event-catalog.json", contracts / "events.schema.json"
            )
            outbox = OutboxRepository(engine, catalog)
        self._outbox = outbox

    def replay(self, binding: OperationContext, idempotency_key: str) -> TicketWriteResult | None:
        result = self._tickets.replay_result(
            binding.tenant_id,
            binding.actor_subject_id,
            _operation_kind(binding),
            idempotency_key,
            binding.payload_digest,
        )
        if result is not None and result.ticket_id != binding.target_id:
            raise TicketPersistenceConflict("idempotency result targets another ticket")
        return result

    def create_confirmed(
        self,
        ticket: Ticket,
        *,
        binding: OperationContext,
        confirmation_id: str,
        operation_id: str,
        idempotency_key: str,
        context_snapshot: dict[str, Any],
        trace: TraceContext,
        handoff_id: str | None = None,
    ) -> TicketWriteResult:
        self._validate_common(ticket, binding, operation_id, idempotency_key, "ticket:create")
        if binding.expected_revision is not None:
            raise ConfirmationConflictError("create confirmation must not bind a revision")
        if not trace.thread_id:
            raise ValueError("ticket trace requires thread_id")
        digest_content = {
            "ticket_id": ticket.ticket_id,
            "customer_id": ticket.customer_id,
            "title": ticket.title,
            "description_masked": ticket.description_masked,
            "priority": ticket.priority.value,
            "context": context_snapshot,
            "thread_id": trace.thread_id,
            "correlation_id": trace.correlation_id,
            "trace_id": trace.trace_id,
        }
        if handoff_id is not None:
            digest_content["handoff_id"] = handoff_id
        digest = content_digest(digest_content)
        if digest != binding.payload_digest:
            raise ConfirmationConflictError("create content changed after confirmation")
        try:
            with self._engine.begin() as connection:
                tickets = self._tickets.bind(connection)
                prior = tickets.replay_result(
                    binding.tenant_id,
                    binding.actor_subject_id,
                    "create",
                    idempotency_key,
                    binding.payload_digest,
                )
                if prior is not None:
                    return self._checked_result(binding, prior)
                if handoff_id is not None:
                    handoff = connection.execute(
                        text(
                            "SELECT customer_id,requested_by,status,ticket_id FROM human_handoff"
                            " WHERE tenant_id=:tenant AND handoff_id=:handoff FOR UPDATE"
                        ),
                        {"tenant": binding.tenant_id, "handoff": handoff_id},
                    ).first()
                    if (
                        handoff is None
                        or handoff.customer_id != ticket.customer_id
                        or handoff.requested_by != binding.actor_subject_id
                        or handoff.status != "unavailable"
                        or handoff.ticket_id is not None
                    ):
                        raise TicketPersistenceConflict("fallback handoff unavailable")
                self._confirmations.bind(connection).consume(
                    confirmation_id, binding, operation_id, datetime.now(timezone.utc)
                )
                result = tickets.create(
                    ticket,
                    context_snapshot=context_snapshot,
                    thread_id=trace.thread_id,
                    correlation_id=trace.correlation_id,
                    trace_id=trace.trace_id,
                    idempotency_key=idempotency_key,
                    request_digest=binding.payload_digest,
                )
                if handoff_id is not None:
                    linked = connection.execute(
                        text(
                            "UPDATE human_handoff SET ticket_id=:ticket,revision=revision+1,"
                            "updated_at=:now WHERE tenant_id=:tenant AND handoff_id=:handoff"
                            " AND status='unavailable' AND ticket_id IS NULL"
                        ),
                        {
                            "ticket": ticket.ticket_id,
                            "now": datetime.now(timezone.utc).replace(tzinfo=None),
                            "tenant": binding.tenant_id,
                            "handoff": handoff_id,
                        },
                    ).rowcount
                    if linked != 1:
                        raise TicketPersistenceConflict("fallback handoff changed")
                if not result.replayed:
                    self._enqueue_ticket_event(
                        connection,
                        ticket,
                        binding,
                        operation_id,
                        trace,
                        "ticket.created",
                    )
                return self._checked_result(binding, result)
        except (ConfirmationConflictError, TicketPersistenceConflict):
            recovered = self._recover_replay(
                binding, confirmation_id, operation_id, idempotency_key
            )
            if recovered is not None:
                return recovered
            raise

    def update_confirmed(
        self,
        ticket: Ticket,
        *,
        expected_revision: int,
        binding: OperationContext,
        confirmation_id: str,
        operation_id: str,
        idempotency_key: str,
        context_snapshot: dict[str, Any],
        trace: TraceContext,
    ) -> TicketWriteResult:
        self._validate_common(ticket, binding, operation_id, idempotency_key, "ticket:update")
        if (
            binding.expected_revision != expected_revision
            or ticket.revision != expected_revision + 1
        ):
            raise TicketVersionConflict("confirmed ticket revision changed")
        try:
            with self._engine.begin() as connection:
                tickets = self._tickets.bind(connection)
                prior = tickets.replay_result(
                    binding.tenant_id,
                    binding.actor_subject_id,
                    "update",
                    idempotency_key,
                    binding.payload_digest,
                )
                if prior is not None:
                    return self._checked_result(binding, prior)
                stored = tickets.get(binding.tenant_id, ticket.ticket_id, include_internal=True)
                if stored is None or stored.ticket.revision != expected_revision:
                    raise TicketVersionConflict("ticket revision has changed")
                original = stored.ticket
                if (
                    original.customer_id != ticket.customer_id
                    or original.created_by != ticket.created_by
                    or original.title != ticket.title
                    or original.description_masked != ticket.description_masked
                    or original.priority != ticket.priority
                    or original.status != ticket.status
                    or original.assigned_to != ticket.assigned_to
                ):
                    raise TicketPersistenceConflict(
                        "content update changed protected ticket fields"
                    )
                old_messages = {message.message_id: message for message in original.messages}
                new_messages = {message.message_id: message for message in ticket.messages}
                if any(new_messages.get(key) != value for key, value in old_messages.items()):
                    raise TicketPersistenceConflict("existing ticket message changed")
                added = [
                    message for message in ticket.messages if message.message_id not in old_messages
                ]
                if len(added) != 1 or added[0].author_subject_id != binding.actor_subject_id:
                    raise TicketPersistenceConflict(
                        "content update requires one actor-authored message"
                    )
                message = added[0]
                digest = content_digest(
                    {
                        "ticket_id": ticket.ticket_id,
                        "customer_id": ticket.customer_id,
                        "message_id": message.message_id,
                        "content_masked": message.content_masked,
                        "visibility": message.visibility.value,
                        "expected_revision": expected_revision,
                        "context": context_snapshot,
                    }
                )
                if digest != binding.payload_digest:
                    raise ConfirmationConflictError("update content changed after confirmation")
                self._confirmations.bind(connection).consume(
                    confirmation_id, binding, operation_id, datetime.now(timezone.utc)
                )
                result = tickets.save(
                    ticket,
                    expected_revision=expected_revision,
                    actor_subject_id=binding.actor_subject_id,
                    context_snapshot=context_snapshot,
                    idempotency_key=idempotency_key,
                    request_digest=binding.payload_digest,
                )
                if not result.replayed:
                    self._enqueue_ticket_event(
                        connection,
                        ticket,
                        binding,
                        operation_id,
                        trace,
                        "ticket.updated",
                    )
                return self._checked_result(binding, result)
        except (ConfirmationConflictError, TicketPersistenceConflict, TicketVersionConflict):
            recovered = self._recover_replay(
                binding, confirmation_id, operation_id, idempotency_key
            )
            if recovered is not None:
                return recovered
            raise

    def _enqueue_ticket_event(
        self,
        connection,
        ticket: Ticket,
        binding: OperationContext,
        operation_id: str,
        trace: TraceContext,
        event_type: str,
    ) -> None:
        payload_type = (
            TicketCreatedPayload if event_type == "ticket.created" else TicketUpdatedPayload
        )
        event = EventEnvelope(
            schema_version="1.0",
            event_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{event_type}:{operation_id}").hex,
            event_type=event_type,
            event_version=1,
            tenant_id=binding.tenant_id,
            request_id=trace.request_id,
            correlation_id=trace.correlation_id,
            trace_id=trace.trace_id,
            operation_id=operation_id,
            causation_id=None,
            parent_event_id=None,
            producer="ticket-service",
            occurred_at=datetime.now(timezone.utc).isoformat(),
            idempotency_key=f"{event_type}:{ticket.ticket_id}:{ticket.revision}",
            subject_id=binding.actor_subject_id,
            retry_count=0,
            payload=payload_type(
                ticket_id=ticket.ticket_id,
                revision=ticket.revision,
                status=ticket.status.value,
                priority=ticket.priority.value,
                context_ref=f"ticket:{ticket.ticket_id}:context:{ticket.revision}",
                actor_subject_id=binding.actor_subject_id,
                customer_id=ticket.customer_id,
            ),
        )
        self._outbox.enqueue(
            connection,
            event,
            aggregate_type="ticket",
            aggregate_id=ticket.ticket_id,
            thread_id=trace.thread_id,
        )

    @staticmethod
    def _validate_common(
        ticket: Ticket,
        binding: OperationContext,
        operation_id: str,
        idempotency_key: str,
        operation_type: str,
    ) -> None:
        if (
            binding.operation_type != operation_type
            or binding.tenant_id != ticket.tenant_id
            or binding.target_id != ticket.ticket_id
            or binding.customer_id != ticket.customer_id
            or binding.actor_subject_id != binding.confirmer_subject_id
            or operation_id != _operation_id(binding, idempotency_key)
        ):
            raise ConfirmationConflictError("ticket write does not match trusted confirmation")
        if operation_type == "ticket:create" and ticket.created_by != binding.actor_subject_id:
            raise ConfirmationConflictError("ticket creator differs from confirmed actor")

    @staticmethod
    def _checked_result(binding: OperationContext, result: TicketWriteResult) -> TicketWriteResult:
        if result.ticket_id != binding.target_id:
            raise TicketPersistenceConflict("idempotency result targets another ticket")
        return result

    def _recover_replay(
        self,
        binding: OperationContext,
        confirmation_id: str,
        operation_id: str,
        idempotency_key: str,
    ) -> TicketWriteResult | None:
        # The failed transaction has rolled back. A concurrent same-key commit may
        # now be visible; never spend the confirmation again or create a new ID.
        record = self._confirmations.get(binding.tenant_id, confirmation_id)
        if (
            record is None
            or record.binding != binding
            or record.status not in ("verified", "consumed")
        ):
            return None
        if record.status == "consumed" and record.consumed_by_operation_id != operation_id:
            return None
        return self.replay(binding, idempotency_key)
