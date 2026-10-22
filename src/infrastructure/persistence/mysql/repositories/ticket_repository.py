"""Tenant-scoped ticket persistence; callers own authorization and confirmation."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from domain.ticket.model import (
    Ticket,
    TicketMessage,
    TicketPriority,
    TicketStatus,
    TicketVersionConflict,
    TicketVisibility,
)


class TicketPersistenceConflict(ValueError):
    """Existing ID or idempotency key is bound to another request."""


@dataclass(frozen=True)
class TicketWriteResult:
    ticket_id: str
    revision: int
    replayed: bool = False


@dataclass(frozen=True)
class StoredTicket:
    ticket: Ticket
    context_snapshot: dict[str, Any]
    thread_id: str
    correlation_id: str
    trace_id: str
    updated_at: datetime


_CONTEXT_FIELDS = frozenset(
    {
        "question_masked",
        "summary",
        "agent_results",
        "tool_result_summaries",
        "failure_reason",
        "business_refs",
        "current_state",
        "priority",
        "next_action",
        "source_message_ids",
    }
)
_REQUIRED_CONTEXT = _CONTEXT_FIELDS - {"failure_reason", "source_message_ids"}


def _identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ValueError(f"{name} must be a nonblank string of at most 64 characters")
    return value


def _context(snapshot: Mapping[str, Any], ticket: Ticket) -> str:
    if not isinstance(snapshot, Mapping) or not _REQUIRED_CONTEXT <= snapshot.keys():
        raise ValueError("ticket context is missing required structured fields")
    if snapshot.keys() - _CONTEXT_FIELDS:
        raise ValueError("ticket context has unsupported fields")
    for name, limit in (
        ("question_masked", 2000),
        ("summary", 1000),
        ("current_state", 64),
        ("next_action", 512),
    ):
        value = snapshot[name]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"ticket context {name} is invalid")
    reason = snapshot.get("failure_reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 512):
        raise ValueError("ticket context failure_reason is invalid")
    if snapshot["priority"] != ticket.priority.value:
        raise ValueError("ticket context priority differs from aggregate")
    for name in ("agent_results", "tool_result_summaries", "source_message_ids"):
        values = snapshot.get(name, [])
        if (
            not isinstance(values, list)
            or len(values) > 20
            or any(
                not isinstance(value, str)
                or len(value) > (64 if name == "source_message_ids" else 512)
                for value in values
            )
        ):
            raise ValueError(f"ticket context {name} is invalid")
    refs = snapshot["business_refs"]
    if (
        not isinstance(refs, dict)
        or len(refs) > 20
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or len(key) > 64
            or len(value) > 64
            for key, value in refs.items()
        )
    ):
        raise ValueError("ticket context business_refs is invalid")
    payload = json.dumps(dict(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 16_384:
        raise ValueError("ticket context exceeds 16 KiB")
    return payload


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validated_digest(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError("request_digest must be a lowercase SHA-256 value")
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _message_values(ticket: Ticket, message: TicketMessage, now: datetime) -> dict[str, Any]:
    _identity(message.message_id, "message_id")
    _identity(message.author_subject_id, "author_subject_id")
    if len(message.content_masked) > 4000:
        raise ValueError("ticket message exceeds 4000 characters")
    return {
        "tenant_id": ticket.tenant_id,
        "message_id": message.message_id,
        "ticket_id": ticket.ticket_id,
        "author_subject_id": message.author_subject_id,
        "content_masked": message.content_masked,
        "visibility": message.visibility.value,
        "now": now,
    }


def _ticket_payload(ticket: Ticket) -> dict[str, Any]:
    return {
        "tenant_id": ticket.tenant_id,
        "ticket_id": ticket.ticket_id,
        "customer_id": ticket.customer_id,
        "created_by": ticket.created_by,
        "title": ticket.title,
        "description_masked": ticket.description_masked,
        "priority": ticket.priority.value,
        "status": ticket.status.value,
        "assigned_to": ticket.assigned_to,
        "revision": ticket.revision,
        "messages": [
            (m.message_id, m.author_subject_id, m.content_masked, m.visibility.value)
            for m in ticket.messages
        ],
    }


class MySQLTicketRepository:
    def __init__(self, engine: Engine, connection: Connection | None = None) -> None:
        self._engine = engine
        self._connection = connection

    def bind(self, connection: Connection) -> MySQLTicketRepository:
        """Use the caller's transaction for confirmation, ticket and idempotency."""
        return MySQLTicketRepository(self._engine, connection)

    @contextmanager
    def _connection_scope(self, *, write: bool = False):
        if self._connection is not None:
            yield self._connection
        elif write:
            with self._engine.begin() as connection:
                yield connection
        else:
            with self._engine.connect() as connection:
                yield connection

    def create(
        self,
        ticket: Ticket,
        *,
        context_snapshot: Mapping[str, Any],
        thread_id: str,
        correlation_id: str,
        trace_id: str,
        idempotency_key: str,
        request_digest: str | None = None,
    ) -> TicketWriteResult:
        if ticket.revision != 1 or ticket.status is not TicketStatus.OPEN or ticket.assigned_to:
            raise ValueError("new ticket must be open at revision 1 and unassigned")
        for name in ("tenant_id", "ticket_id", "customer_id", "created_by"):
            _identity(getattr(ticket, name), name)
        for name, value in (
            ("thread_id", thread_id),
            ("correlation_id", correlation_id),
            ("trace_id", trace_id),
            ("idempotency_key", idempotency_key),
        ):
            _identity(value, name)
        if len(ticket.title) > 255 or len(ticket.description_masked) > 4000:
            raise ValueError("ticket title or description exceeds storage limit")
        context_json = _context(context_snapshot, ticket)
        computed_digest = _digest(
            {
                "ticket": _ticket_payload(ticket),
                "context": json.loads(context_json),
                "thread_id": thread_id,
                "correlation_id": correlation_id,
                "trace_id": trace_id,
            }
        )
        digest = (
            _validated_digest(request_digest) if request_digest is not None else computed_digest
        )
        operation = "create"
        existing = self._replay(
            ticket.tenant_id, ticket.created_by, operation, idempotency_key, digest
        )
        if existing is not None:
            return existing
        now = _now()
        try:
            with self._connection_scope(write=True) as connection:
                connection.execute(
                    text("""INSERT INTO ticket (
                    tenant_id,ticket_id,customer_id,created_by,title,description_masked,
                    priority,status,assigned_to,revision,context_snapshot,thread_id,
                    correlation_id,trace_id,created_at,updated_at
                ) VALUES (:tenant_id,:ticket_id,:customer_id,:created_by,:title,
                    :description_masked,:priority,:status,NULL,1,:context_json,
                    :thread_id,:correlation_id,:trace_id,:now,:now)"""),
                    {
                        **_ticket_payload(ticket),
                        "context_json": context_json,
                        "thread_id": thread_id,
                        "correlation_id": correlation_id,
                        "trace_id": trace_id,
                        "now": now,
                    },
                )
                for message in ticket.messages:
                    self._insert_message(connection, ticket, message, now)
                self._record_idempotency(
                    connection,
                    ticket.tenant_id,
                    ticket.created_by,
                    operation,
                    idempotency_key,
                    digest,
                    ticket.ticket_id,
                    ticket.revision,
                    now,
                )
        except IntegrityError as exc:
            replay = self._replay(
                ticket.tenant_id, ticket.created_by, operation, idempotency_key, digest
            )
            if replay is not None:
                return replay
            raise TicketPersistenceConflict("ticket ID or message ID already exists") from exc
        return TicketWriteResult(ticket.ticket_id, ticket.revision)

    def save(
        self,
        ticket: Ticket,
        *,
        expected_revision: int,
        actor_subject_id: str,
        context_snapshot: Mapping[str, Any],
        idempotency_key: str,
        request_digest: str | None = None,
    ) -> TicketWriteResult:
        _identity(ticket.tenant_id, "tenant_id")
        _identity(ticket.ticket_id, "ticket_id")
        _identity(actor_subject_id, "actor_subject_id")
        _identity(idempotency_key, "idempotency_key")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        if ticket.revision != expected_revision + 1:
            raise TicketVersionConflict("next revision must increment exactly once")
        context_json = _context(context_snapshot, ticket)
        computed_digest = _digest(
            {
                "ticket": _ticket_payload(ticket),
                "context": json.loads(context_json),
                "expected_revision": expected_revision,
            }
        )
        digest = (
            _validated_digest(request_digest) if request_digest is not None else computed_digest
        )
        operation = "update"
        replay = self._replay(
            ticket.tenant_id, actor_subject_id, operation, idempotency_key, digest
        )
        if replay is not None:
            return replay
        now = _now()
        try:
            with self._connection_scope(write=True) as connection:
                result = connection.execute(
                    text("""UPDATE ticket SET
                    priority=:priority,status=:status,assigned_to=:assigned_to,
                    context_snapshot=:context_json,revision=:revision,updated_at=:now
                    WHERE tenant_id=:tenant_id AND ticket_id=:ticket_id
                    AND revision=:expected_revision AND customer_id=:customer_id
                    AND created_by=:created_by AND title=:title
                    AND description_masked=:description_masked"""),
                    {
                        **_ticket_payload(ticket),
                        "context_json": context_json,
                        "expected_revision": expected_revision,
                        "now": now,
                    },
                )
                if result.rowcount != 1:
                    raise TicketVersionConflict("ticket revision or immutable fields changed")
                rows = (
                    connection.execute(
                        text("""SELECT message_id,author_subject_id,
                    content_masked,visibility FROM ticket_message
                    WHERE tenant_id=:tenant_id AND ticket_id=:ticket_id"""),
                        {
                            "tenant_id": ticket.tenant_id,
                            "ticket_id": ticket.ticket_id,
                        },
                    )
                    .mappings()
                    .all()
                )
                old = {
                    row["message_id"]: (
                        row["author_subject_id"],
                        row["content_masked"],
                        row["visibility"],
                    )
                    for row in rows
                }
                new = {
                    m.message_id: (m.author_subject_id, m.content_masked, m.visibility.value)
                    for m in ticket.messages
                }
                if (
                    any(new.get(key) != value for key, value in old.items())
                    or len(new) - len(old) > 1
                ):
                    raise TicketPersistenceConflict(
                        "existing messages cannot be altered or removed"
                    )
                for message in ticket.messages:
                    if message.message_id not in old:
                        self._insert_message(connection, ticket, message, now)
                self._record_idempotency(
                    connection,
                    ticket.tenant_id,
                    actor_subject_id,
                    operation,
                    idempotency_key,
                    digest,
                    ticket.ticket_id,
                    ticket.revision,
                    now,
                )
        except TicketVersionConflict:
            replay = self._replay(
                ticket.tenant_id, actor_subject_id, operation, idempotency_key, digest
            )
            if replay is not None:
                return replay
            raise
        except IntegrityError as exc:
            replay = self._replay(
                ticket.tenant_id, actor_subject_id, operation, idempotency_key, digest
            )
            if replay is not None:
                return replay
            raise TicketPersistenceConflict("ticket message ID already exists") from exc
        return TicketWriteResult(ticket.ticket_id, ticket.revision)

    def get(
        self, tenant_id: str, ticket_id: str, *, include_internal: bool = False
    ) -> StoredTicket | None:
        """Return customer-visible messages by default; caller must authorize ticket ownership."""
        _identity(tenant_id, "tenant_id")
        _identity(ticket_id, "ticket_id")
        with self._connection_scope() as connection:
            row = (
                connection.execute(
                    text("""SELECT tenant_id,ticket_id,customer_id,
                created_by,title,description_masked,priority,status,assigned_to,revision,
                context_snapshot,thread_id,correlation_id,trace_id,updated_at FROM ticket
                WHERE tenant_id=:tenant_id AND ticket_id=:ticket_id"""),
                    {
                        "tenant_id": tenant_id,
                        "ticket_id": ticket_id,
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            query = """SELECT message_id,author_subject_id,content_masked,visibility
                FROM ticket_message WHERE tenant_id=:tenant_id AND ticket_id=:ticket_id"""
            if not include_internal:
                query += " AND visibility='customer'"
            query += " ORDER BY created_at,message_id"
            message_rows = (
                connection.execute(
                    text(query),
                    {
                        "tenant_id": tenant_id,
                        "ticket_id": ticket_id,
                    },
                )
                .mappings()
                .all()
            )
        snapshot = row["context_snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        ticket = Ticket(
            tenant_id=row["tenant_id"],
            ticket_id=row["ticket_id"],
            customer_id=row["customer_id"],
            created_by=row["created_by"],
            title=row["title"],
            description_masked=row["description_masked"],
            priority=TicketPriority(row["priority"]),
            status=TicketStatus(row["status"]),
            assigned_to=row["assigned_to"],
            revision=int(row["revision"]),
            messages=tuple(
                TicketMessage(
                    message_id=m["message_id"],
                    author_subject_id=m["author_subject_id"],
                    content_masked=m["content_masked"],
                    visibility=TicketVisibility(m["visibility"]),
                )
                for m in message_rows
            ),
        )
        updated_at = row["updated_at"]
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        else:
            updated_at = updated_at.astimezone(timezone.utc)
        return StoredTicket(
            ticket,
            snapshot,
            row["thread_id"],
            row["correlation_id"],
            row["trace_id"],
            updated_at,
        )

    def _replay(
        self, tenant_id: str, actor: str, operation: str, key: str, digest: str
    ) -> TicketWriteResult | None:
        with self._connection_scope() as connection:
            row = (
                connection.execute(
                    text("""SELECT request_digest,ticket_id,result_revision
                FROM ticket_write_idempotency WHERE tenant_id=:tenant_id
                AND actor_subject_id=:actor AND operation_type=:operation
                AND idempotency_key=:key"""),
                    {
                        "tenant_id": tenant_id,
                        "actor": actor,
                        "operation": operation,
                        "key": key,
                    },
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        if row["request_digest"] != digest:
            raise TicketPersistenceConflict("idempotency key is bound to different content")
        return TicketWriteResult(row["ticket_id"], int(row["result_revision"]), replayed=True)

    def replay_result(
        self,
        tenant_id: str,
        actor_subject_id: str,
        operation_type: str,
        idempotency_key: str,
        request_digest: str,
    ) -> TicketWriteResult | None:
        if operation_type not in ("create", "update"):
            raise ValueError("unsupported ticket operation")
        for name, value in (
            ("tenant_id", tenant_id),
            ("actor_subject_id", actor_subject_id),
            ("idempotency_key", idempotency_key),
        ):
            _identity(value, name)
        return self._replay(
            tenant_id,
            actor_subject_id,
            operation_type,
            idempotency_key,
            _validated_digest(request_digest),
        )

    @staticmethod
    def _insert_message(
        connection: Connection, ticket: Ticket, message: TicketMessage, now: datetime
    ) -> None:
        connection.execute(
            text("""INSERT INTO ticket_message (
            tenant_id,message_id,ticket_id,author_subject_id,content_masked,visibility,created_at
        ) VALUES (:tenant_id,:message_id,:ticket_id,:author_subject_id,
            :content_masked,:visibility,:now)"""),
            _message_values(ticket, message, now),
        )

    @staticmethod
    def _record_idempotency(
        connection: Connection,
        tenant_id: str,
        actor: str,
        operation: str,
        key: str,
        digest: str,
        ticket_id: str,
        revision: int,
        now: datetime,
    ) -> None:
        connection.execute(
            text("""INSERT INTO ticket_write_idempotency (
            tenant_id,actor_subject_id,operation_type,idempotency_key,request_digest,
            ticket_id,result_revision,created_at
        ) VALUES (:tenant_id,:actor,:operation,:key,:digest,:ticket_id,:revision,:now)"""),
            {
                "tenant_id": tenant_id,
                "actor": actor,
                "operation": operation,
                "key": key,
                "digest": digest,
                "ticket_id": ticket_id,
                "revision": revision,
                "now": now,
            },
        )
