"""DEV-only MySQL sidecar for server-issued ticket MCP write intents.

The future HTTP adapter registers an intent only after the normal prepare and
verify flow. The future MCP adapter resolves with authenticated context and
model-visible fields, then invokes TicketApplicationService with commit_kwargs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from application.common.context import AuthContext, TraceContext
from application.ticket.service import TicketApplicationService, TicketContextSnapshot
from application.ticket.tool_intent import (
    TicketToolIntent,
    TicketToolIntentConflict,
    visible_create,
    visible_digest,
    visible_update,
)
from domain.ticket import TicketPriority, TicketVisibility
from infrastructure.security.confirmation_store import MySQLConfirmationStore


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _json(value):
    return value if isinstance(value, dict) else json.loads(value)


class MySQLTicketToolIntentStore:
    """Never accepts confirmation or idempotency from model tool arguments."""

    def __init__(self, engine: Engine, tickets: TicketApplicationService) -> None:
        self._engine = engine
        self._tickets = tickets
        self._confirmations = MySQLConfirmationStore(engine)

    def register_create(
        self,
        auth: AuthContext,
        *,
        confirmation_id: str,
        customer_id: str,
        title: str,
        description_masked: str,
        priority: TicketPriority,
        context: TicketContextSnapshot,
        trace: TraceContext,
        idempotency_key: str,
        handoff_id: str | None = None,
    ) -> None:
        # Reconstruct the exact service binding; neither a client digest nor a
        # model-provided confirmation can substitute for the verified record.
        ticket, binding, snapshot = self._tickets._create_intent(
            auth,
            customer_id,
            title,
            description_masked,
            priority,
            context,
            trace,
            idempotency_key,
            handoff_id,
        )
        self._register(
            auth,
            confirmation_id,
            idempotency_key,
            binding,
            visible_create(customer_id, title, description_masked, priority),
            ticket.ticket_id,
            context,
            snapshot,
            trace,
            handoff_id,
        )

    def register_update(
        self,
        auth: AuthContext,
        *,
        confirmation_id: str,
        ticket_id: str,
        content_masked: str,
        visibility: TicketVisibility,
        expected_revision: int,
        context: TicketContextSnapshot,
        trace: TraceContext,
        idempotency_key: str,
    ) -> None:
        binding, snapshot = self._tickets._update_binding(
            auth,
            ticket_id,
            content_masked,
            visibility,
            expected_revision,
            context,
            idempotency_key,
        )
        self._register(
            auth,
            confirmation_id,
            idempotency_key,
            binding,
            visible_update(ticket_id, content_masked, visibility),
            ticket_id,
            context,
            snapshot,
            trace,
            None,
        )

    def _register(
        self,
        auth,
        confirmation_id,
        idempotency_key,
        binding,
        visible,
        ticket_id,
        context,
        snapshot,
        trace,
        handoff_id,
    ) -> None:
        now = datetime.now(timezone.utc)
        if not auth.is_valid_at(now) or not auth.session_id or len(auth.session_id) > 128:
            raise TicketToolIntentConflict("trusted session unavailable")
        if not isinstance(trace, TraceContext) or not all(
            isinstance(value, str) and value and len(value) <= 64
            for value in (trace.request_id, trace.correlation_id, trace.trace_id, trace.thread_id)
        ):
            raise TicketToolIntentConflict("trusted trace unavailable")
        if context.as_dict(TicketPriority(snapshot["priority"])) != snapshot:
            raise TicketToolIntentConflict("ticket context changed")
        payload = {"context": snapshot, "trace": trace.__dict__, "handoff_id": handoff_id}
        serialized = json.dumps(
            {"visible": visible, "payload": payload}, ensure_ascii=False
        ).encode()
        if len(serialized) > 24_576:
            raise TicketToolIntentConflict("ticket intent exceeds size limit")
        with self._engine.begin() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT actor_subject_id,customer_id,confirmer_subject_id,"
                        "operation_type,target_id,payload_digest,expected_revision,"
                        "status,expires_at "
                        "FROM operation_confirmation WHERE tenant_id=:tenant"
                        " AND confirmation_id=:cid FOR UPDATE"
                    ),
                    {"tenant": auth.tenant_id, "cid": confirmation_id},
                )
                .mappings()
                .first()
            )
            if (
                row is None
                or row["status"] != "verified"
                or row["expires_at"] <= _naive(now)
                or row["actor_subject_id"] != auth.subject_id
                or row["confirmer_subject_id"] != auth.subject_id
                or row["customer_id"] != binding.customer_id
                or row["operation_type"] != binding.operation_type
                or row["target_id"] != binding.target_id
                or row["payload_digest"] != binding.payload_digest
                or row["expected_revision"] != binding.expected_revision
            ):
                raise TicketToolIntentConflict("verified ticket confirmation mismatch")
            digest = visible_digest(visible)
            existing = (
                connection.execute(
                    text(
                        "SELECT visible_digest,payload_digest FROM ticket_tool_intent"
                        " WHERE tenant_id=:tenant AND confirmation_id=:cid FOR UPDATE"
                    ),
                    {"tenant": auth.tenant_id, "cid": confirmation_id},
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if (
                    existing["visible_digest"] != digest
                    or existing["payload_digest"] != binding.payload_digest
                ):
                    raise TicketToolIntentConflict("ticket intent content changed")
                return
            connection.execute(
                text(
                    "INSERT INTO ticket_tool_intent (tenant_id,confirmation_id,actor_subject_id,"
                    "customer_id,session_id,operation_type,ticket_id,visible_digest,visible_fields,"
                    "payload_masked,payload_digest,idempotency_key,expected_revision,expires_at,created_at)"
                    " VALUES (:tenant,:cid,:actor,:customer,:session,:operation,:ticket,:digest,"
                    " :visible,:payload,:binding_digest,:idem,:revision,:expires,:created)"
                ),
                {
                    "tenant": auth.tenant_id,
                    "cid": confirmation_id,
                    "actor": auth.subject_id,
                    "customer": binding.customer_id,
                    "session": auth.session_id,
                    "operation": binding.operation_type,
                    "ticket": ticket_id,
                    "digest": digest,
                    "visible": json.dumps(visible, ensure_ascii=False),
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "binding_digest": binding.payload_digest,
                    "idem": idempotency_key,
                    "revision": binding.expected_revision,
                    "expires": row["expires_at"],
                    "created": _naive(now),
                },
            )

    def resolve_create(
        self,
        auth: AuthContext,
        *,
        customer_id: str,
        title: str,
        description_masked: str,
        priority: TicketPriority,
    ) -> TicketToolIntent:
        return self._resolve(
            auth,
            "ticket:create",
            visible_create(customer_id, title, description_masked, priority),
        )

    def resolve_update(
        self,
        auth: AuthContext,
        *,
        ticket_id: str,
        content_masked: str,
        visibility: TicketVisibility,
    ) -> TicketToolIntent:
        return self._resolve(
            auth,
            "ticket:update",
            visible_update(ticket_id, content_masked, visibility),
        )

    def _resolve(self, auth, operation, visible) -> TicketToolIntent:
        now = datetime.now(timezone.utc)
        if not auth.is_valid_at(now) or not auth.session_id:
            raise TicketToolIntentConflict("trusted session unavailable")
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT i.confirmation_id,i.customer_id,i.ticket_id,i.visible_fields,"
                        "i.payload_masked,i.payload_digest,i.idempotency_key,"
                        "i.expected_revision,"
                        "i.expires_at,c.actor_subject_id,"
                        "c.customer_id AS confirmed_customer,"
                        "c.confirmer_subject_id,c.operation_type,c.target_id,"
                        "c.payload_digest AS confirmed_digest,"
                        "c.expected_revision AS confirmed_revision,c.status,"
                        "c.expires_at AS confirmed_expiry,"
                        "c.consumed_by_operation_id "
                        "FROM ticket_tool_intent i JOIN operation_confirmation c "
                        "ON c.tenant_id=i.tenant_id AND c.confirmation_id=i.confirmation_id "
                        "WHERE i.tenant_id=:tenant AND i.actor_subject_id=:actor "
                        "AND i.session_id=:session AND i.operation_type=:operation "
                        "AND i.visible_digest=:digest LIMIT 3"
                    ),
                    {
                        "tenant": auth.tenant_id,
                        "actor": auth.subject_id,
                        "session": auth.session_id,
                        "operation": operation,
                        "digest": visible_digest(visible),
                    },
                )
                .mappings()
                .all()
            )
            if len(rows) != 1:
                raise TicketToolIntentConflict("ticket intent absent or ambiguous")
            row = rows[0]
            payload = _json(row["payload_masked"])
            if (
                _json(row["visible_fields"]) != visible
                or row["actor_subject_id"] != auth.subject_id
                or row["confirmer_subject_id"] != auth.subject_id
                or row["customer_id"] != row["confirmed_customer"]
                or row["operation_type"] != operation
                or row["ticket_id"] != row["target_id"]
                or row["payload_digest"] != row["confirmed_digest"]
                or row["expected_revision"] != row["confirmed_revision"]
                or row["expires_at"] != row["confirmed_expiry"]
            ):
                raise TicketToolIntentConflict("ticket intent binding mismatch")
            if row["status"] == "verified":
                if row["expires_at"] <= _naive(now):
                    raise TicketToolIntentConflict("ticket intent expired")
            elif row["status"] == "consumed":
                from application.common.context import OperationContext
                from infrastructure.ticket.mysql_confirmed_writer import _operation_id

                binding = OperationContext(
                    auth.tenant_id,
                    auth.subject_id,
                    row["customer_id"],
                    auth.subject_id,
                    operation,
                    row["ticket_id"],
                    row["payload_digest"],
                    row["expected_revision"],
                )
                replay = self._tickets._writes.replay(binding, row["idempotency_key"])
                if (
                    replay is None
                    or replay.ticket_id != row["ticket_id"]
                    or row["consumed_by_operation_id"]
                    != _operation_id(binding, row["idempotency_key"])
                ):
                    raise TicketToolIntentConflict("consumed ticket intent has no matching replay")
            else:
                raise TicketToolIntentConflict("ticket intent not verified")
        snapshot = payload["context"]
        priority = TicketPriority(snapshot["priority"])
        context = TicketContextSnapshot(
            question_masked=snapshot["question_masked"],
            summary=snapshot["summary"],
            agent_results=tuple(snapshot["agent_results"]),
            tool_result_summaries=tuple(snapshot["tool_result_summaries"]),
            failure_reason=snapshot["failure_reason"],
            business_refs=tuple(sorted(snapshot["business_refs"].items())),
            current_state=snapshot["current_state"],
            next_action=snapshot["next_action"],
            source_message_ids=tuple(snapshot["source_message_ids"]),
        )
        if context.as_dict(priority) != snapshot:
            raise TicketToolIntentConflict("ticket context mismatch")
        trace = TraceContext(**payload["trace"])
        intent = TicketToolIntent(
            operation,
            row["confirmation_id"],
            row["idempotency_key"],
            row["expected_revision"],
            row["customer_id"],
            row["ticket_id"],
            context,
            trace,
            visible,
            payload["handoff_id"],
        )
        # Recompute the digest with the ordinary service validation before the
        # caller receives any trusted write arguments.
        if operation == "ticket:create":
            _, binding, _ = self._tickets._create_intent(
                auth,
                row["customer_id"],
                visible["title"],
                visible["description_masked"],
                TicketPriority(visible["priority"]),
                context,
                trace,
                row["idempotency_key"],
                payload["handoff_id"],
            )
        else:
            binding, _ = self._tickets._update_binding(
                auth,
                row["ticket_id"],
                visible["content_masked"],
                TicketVisibility(visible["visibility"]),
                row["expected_revision"],
                context,
                row["idempotency_key"],
            )
        if binding.payload_digest != row["payload_digest"] or binding.target_id != row["ticket_id"]:
            raise TicketToolIntentConflict("ticket content changed")
        return intent
