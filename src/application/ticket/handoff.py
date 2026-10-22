"""Authorized Mock human queue commands."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from application.common.authorization import require_customer_access, verify_tool_context
from application.common.context import AuthContext, TraceContext
from application.common.operation_confirmation import content_digest
from application.ticket.service import TicketContextSnapshot
from domain.ticket import TicketPriority


class HandoffConflict(ValueError):
    pass


@dataclass(frozen=True)
class HandoffResult:
    handoff_id: str
    status: str
    revision: int
    ticket_id: str | None
    assigned_to: str | None
    replayed: bool = False


class HandoffStore(Protocol):
    def request(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        customer_id: str,
        requested_by: str,
        reason_code: str,
        summary_masked: str,
        context_snapshot: dict[str, Any],
        trace: TraceContext,
        idempotency_key: str,
        request_digest: str,
        available: bool,
        capacity: int,
        ticket_id: str | None,
    ) -> HandoffResult: ...

    def claim(
        self,
        *,
        tenant_id: str,
        handoff_id: str,
        actor_id: str,
        expected_revision: int,
        idempotency_key: str,
        ticket_id: str | None,
    ) -> HandoffResult: ...

    def get(self, tenant_id: str, handoff_id: str) -> tuple[str, HandoffResult] | None: ...


class MockHumanHandoff:
    def __init__(self, store: HandoffStore, *, capacity: int = 32) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be positive")
        self._store = store
        self._capacity = capacity

    def request(
        self,
        auth: AuthContext,
        trace: TraceContext,
        *,
        customer_id: str,
        reason_code: str,
        context: TicketContextSnapshot,
        idempotency_key: str,
        available: bool,
        ticket_id: str | None = None,
    ) -> HandoffResult:
        verify_tool_context(auth, ("ticket:create",))
        require_customer_access(auth, customer_id, tenant_id=auth.tenant_id)
        if not isinstance(available, bool):
            raise ValueError("available must be a trusted boolean")
        if not isinstance(trace, TraceContext):
            raise ValueError("trusted handoff trace is required")
        for value, limit, name in (
            (reason_code, 64, "reason_code"),
            (idempotency_key, 64, "idempotency_key"),
            (trace.thread_id, 64, "thread_id"),
            (trace.request_id, 64, "request_id"),
            (trace.correlation_id, 64, "correlation_id"),
            (trace.trace_id, 64, "trace_id"),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"{name} is invalid")
        snapshot = context.as_dict(TicketPriority.NORMAL)
        digest = content_digest(
            {
                "customer_id": customer_id,
                "reason_code": reason_code,
                "context": snapshot,
                "ticket_id": ticket_id,
            }
        )
        handoff_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{auth.tenant_id}\x00{auth.subject_id}\x00handoff\x00{idempotency_key}",
        ).hex
        return self._store.request(
            tenant_id=auth.tenant_id,
            handoff_id=handoff_id,
            customer_id=customer_id,
            requested_by=auth.subject_id,
            reason_code=reason_code,
            summary_masked=context.summary,
            context_snapshot=snapshot,
            trace=trace,
            idempotency_key=idempotency_key,
            request_digest=digest,
            available=available,
            capacity=self._capacity,
            ticket_id=ticket_id,
        )

    def claim(
        self,
        auth: AuthContext,
        *,
        handoff_id: str,
        expected_revision: int,
        idempotency_key: str,
        ticket_id: str | None = None,
    ) -> HandoffResult:
        verify_tool_context(auth, ("handoff:accept",))
        if "human_agent" not in auth.roles:
            raise HandoffConflict("human agent role required")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 64
        ):
            raise ValueError("idempotency_key is invalid")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision is invalid")
        record = self._store.get(auth.tenant_id, handoff_id)
        if record is None:
            raise HandoffConflict("handoff unavailable")
        customer_id, _ = record
        require_customer_access(auth, customer_id, tenant_id=auth.tenant_id)
        return self._store.claim(
            tenant_id=auth.tenant_id,
            handoff_id=handoff_id,
            actor_id=auth.subject_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            ticket_id=ticket_id,
        )
