"""Authorized ticket commands; writes require an atomic confirmation-aware port."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext, OperationContext, TraceContext
from application.common.errors import ErrorCode
from application.common.operation_confirmation import (
    OperationConfirmationUseCase,
    PreparedConfirmation,
    content_digest,
)
from application.common.ports.confirmation import ConfirmationConflictError, ConfirmationStorePort
from domain.ticket import (
    InvalidTicket as InvalidTicket,
)
from domain.ticket import (
    InvalidTicketState as InvalidTicketState,
)
from domain.ticket import (
    Ticket,
    TicketMessage,
    TicketPriority,
    TicketVersionConflict,
    TicketVisibility,
)


@dataclass(frozen=True)
class TicketContextSnapshot:
    """Only bounded, already-masked summaries may cross the persistence boundary."""

    question_masked: str
    summary: str
    agent_results: tuple[str, ...]
    tool_result_summaries: tuple[str, ...]
    failure_reason: str | None
    business_refs: tuple[tuple[str, str], ...]
    current_state: str
    next_action: str
    source_message_ids: tuple[str, ...] = ()

    def as_dict(self, priority: TicketPriority) -> dict[str, Any]:
        for name, value, limit in (
            ("question_masked", self.question_masked, 2000),
            ("summary", self.summary, 1000),
            ("current_state", self.current_state, 64),
            ("next_action", self.next_action, 512),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"{name} is invalid")
        if self.failure_reason is not None and (
            not isinstance(self.failure_reason, str) or len(self.failure_reason) > 512
        ):
            raise ValueError("failure_reason is invalid")
        for name, values, limit in (
            ("agent_results", self.agent_results, 512),
            ("tool_result_summaries", self.tool_result_summaries, 512),
            ("source_message_ids", self.source_message_ids, 64),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > 20
                or any(not isinstance(value, str) or len(value) > limit for value in values)
            ):
                raise ValueError(f"{name} is invalid")
        if (
            not isinstance(self.business_refs, tuple)
            or len(self.business_refs) > 20
            or any(
                not isinstance(pair, tuple)
                or len(pair) != 2
                or any(not isinstance(value, str) or len(value) > 64 for value in pair)
                for pair in self.business_refs
            )
        ):
            raise ValueError("business_refs is invalid")
        refs = dict(self.business_refs)
        if len(refs) != len(self.business_refs):
            raise ValueError("business_refs contains duplicate keys")
        snapshot = {
            "question_masked": self.question_masked,
            "summary": self.summary,
            "agent_results": list(self.agent_results),
            "tool_result_summaries": list(self.tool_result_summaries),
            "failure_reason": self.failure_reason,
            "business_refs": refs,
            "current_state": self.current_state,
            "priority": priority.value,
            "next_action": self.next_action,
            "source_message_ids": list(self.source_message_ids),
        }
        if len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) > 16_384:
            raise ValueError("ticket context exceeds 16 KiB")
        return snapshot


@dataclass(frozen=True)
class TicketRecord:
    ticket: Ticket
    context_snapshot: dict[str, Any]
    thread_id: str
    correlation_id: str
    trace_id: str
    updated_at: datetime | None = None


@dataclass(frozen=True)
class TicketWriteResult:
    ticket_id: str
    revision: int
    replayed: bool = False


@dataclass(frozen=True)
class PreparedTicketWrite:
    ticket_id: str
    confirmation: PreparedConfirmation


class TicketReadPort(Protocol):
    def get(
        self, tenant_id: str, ticket_id: str, *, include_internal: bool = False
    ) -> TicketRecord | None: ...


class ConfirmedTicketWritePort(Protocol):
    """Implementation MUST atomically consume confirmation, write ticket and idempotency.

    The existing SQL ticket repository and confirmation store can be composed by a
    future infrastructure transaction adapter; this port is never a two-commit shim.
    """

    def replay(
        self, binding: OperationContext, idempotency_key: str
    ) -> TicketWriteResult | None: ...

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
    ) -> TicketWriteResult: ...

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
    ) -> TicketWriteResult: ...


def _safe_ticket_id(auth: AuthContext, idempotency_key: str) -> str:
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > 64
    ):
        raise ValueError("idempotency_key is invalid")
    source = f"{auth.tenant_id}\x00{auth.subject_id}\x00ticket:create\x00{idempotency_key}"
    return uuid.uuid5(uuid.NAMESPACE_URL, source).hex


def _operation_id(binding: OperationContext, idempotency_key: str) -> str:
    source = (
        f"{binding.tenant_id}\x00{binding.actor_subject_id}\x00"
        f"{binding.operation_type}\x00{idempotency_key}"
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, source).hex


def _message_id(auth: AuthContext, ticket_id: str, idempotency_key: str) -> str:
    source = f"{auth.tenant_id}\x00{auth.subject_id}\x00{ticket_id}\x00{idempotency_key}"
    return uuid.uuid5(uuid.NAMESPACE_URL, source).hex


class TicketApplicationService:
    def __init__(
        self,
        reads: TicketReadPort,
        writes: ConfirmedTicketWritePort,
        confirmations: ConfirmationStorePort,
        *,
        confirmation_ttl_seconds: int = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._reads = reads
        self._writes = writes
        self._confirmations = confirmations
        self._confirmation_flow = OperationConfirmationUseCase(
            confirmations, confirmation_ttl_seconds
        )
        self._clock = clock

    def prepare_create(
        self,
        auth: AuthContext,
        *,
        customer_id: str,
        title: str,
        description_masked: str,
        priority: TicketPriority,
        context: TicketContextSnapshot,
        trace: TraceContext,
        idempotency_key: str,
        handoff_id: str | None = None,
    ) -> PreparedTicketWrite:
        ticket, binding, _ = self._create_intent(
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
        return PreparedTicketWrite(ticket.ticket_id, self._confirmation_flow.prepare(binding))

    def create(
        self,
        auth: AuthContext,
        *,
        customer_id: str,
        title: str,
        description_masked: str,
        priority: TicketPriority,
        context: TicketContextSnapshot,
        trace: TraceContext,
        idempotency_key: str,
        confirmation_id: str,
        handoff_id: str | None = None,
    ) -> TicketWriteResult:
        ticket, binding, snapshot = self._create_intent(
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
        operation_id = _operation_id(binding, idempotency_key)
        consumed = self._require_confirmation(binding, confirmation_id, operation_id)
        if consumed:
            return self._require_replay(binding, idempotency_key)
        return self._writes.create_confirmed(
            ticket,
            binding=binding,
            confirmation_id=confirmation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            context_snapshot=snapshot,
            trace=trace,
            handoff_id=handoff_id,
        )

    def prepare_update(
        self,
        auth: AuthContext,
        *,
        ticket_id: str,
        content_masked: str,
        visibility: TicketVisibility,
        expected_revision: int,
        context: TicketContextSnapshot,
        idempotency_key: str,
    ) -> PreparedTicketWrite:
        _, binding, _, _ = self._update_intent(
            auth,
            ticket_id,
            content_masked,
            visibility,
            expected_revision,
            context,
            idempotency_key,
        )
        return PreparedTicketWrite(ticket_id, self._confirmation_flow.prepare(binding))

    def update(
        self,
        auth: AuthContext,
        *,
        ticket_id: str,
        content_masked: str,
        visibility: TicketVisibility,
        expected_revision: int,
        context: TicketContextSnapshot,
        idempotency_key: str,
        confirmation_id: str,
        trace: TraceContext,
    ) -> TicketWriteResult:
        if (
            not isinstance(trace, TraceContext)
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > 64
                for value in (trace.request_id, trace.correlation_id, trace.trace_id)
            )
            or (
                trace.thread_id is not None
                and (
                    not isinstance(trace.thread_id, str)
                    or not trace.thread_id.strip()
                    or len(trace.thread_id) > 64
                )
            )
        ):
            raise ValueError("trusted ticket trace identifiers are invalid")
        # Bind inputs before loading the mutable aggregate, so a committed replay
        # does not need to recreate the old version from current ticket state.
        binding, snapshot = self._update_binding(
            auth,
            ticket_id,
            content_masked,
            visibility,
            expected_revision,
            context,
            idempotency_key,
        )
        operation_id = _operation_id(binding, idempotency_key)
        consumed = self._require_confirmation(binding, confirmation_id, operation_id)
        if consumed:
            return self._require_replay(binding, idempotency_key)
        record = self._authorized_record(auth, ticket_id, "ticket:update", full=True)
        if record.ticket.revision != expected_revision:
            raise TicketVersionConflict("ticket revision has changed")
        message = TicketMessage(
            message_id=_message_id(auth, ticket_id, idempotency_key),
            author_subject_id=auth.subject_id,
            content_masked=content_masked,
            visibility=visibility,
        )
        changed = record.ticket.add_message(message, expected_revision=expected_revision)
        return self._writes.update_confirmed(
            changed,
            expected_revision=expected_revision,
            binding=binding,
            confirmation_id=confirmation_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            context_snapshot=snapshot,
            trace=trace,
        )

    def query(self, auth: AuthContext, ticket_id: str) -> TicketRecord:
        return self._authorized_record(auth, ticket_id, "ticket:read", full=False)

    def _create_intent(
        self,
        auth: AuthContext,
        customer_id: str,
        title: str,
        description_masked: str,
        priority: TicketPriority,
        context: TicketContextSnapshot,
        trace: TraceContext,
        idempotency_key: str,
        handoff_id: str | None = None,
    ) -> tuple[Ticket, OperationContext, dict[str, Any]]:
        verify_tool_context(auth, ("ticket:create",))
        require_customer_access(auth, customer_id, tenant_id=auth.tenant_id)
        if not isinstance(trace, TraceContext) or any(
            not isinstance(value, str) or not value.strip() or len(value) > 64
            for value in (trace.thread_id, trace.correlation_id, trace.trace_id)
        ):
            raise ValueError("trusted ticket trace identifiers are invalid")
        if len(title) > 255 or len(description_masked) > 4000:
            raise ValueError("ticket title or description exceeds storage limit")
        if handoff_id is not None and (
            not isinstance(handoff_id, str)
            or len(handoff_id) != 32
            or any(char not in "0123456789abcdef" for char in handoff_id)
        ):
            raise ValueError("handoff_id is invalid")
        ticket = Ticket(
            tenant_id=auth.tenant_id,
            ticket_id=_safe_ticket_id(auth, idempotency_key),
            customer_id=customer_id,
            created_by=auth.subject_id,
            title=title,
            description_masked=description_masked,
            priority=priority,
        )
        snapshot = context.as_dict(priority)
        digest_content = {
            "ticket_id": ticket.ticket_id,
            "customer_id": customer_id,
            "title": title,
            "description_masked": description_masked,
            "priority": priority.value,
            "context": snapshot,
            "thread_id": trace.thread_id,
            "correlation_id": trace.correlation_id,
            "trace_id": trace.trace_id,
        }
        if handoff_id is not None:
            digest_content["handoff_id"] = handoff_id
        digest = content_digest(digest_content)
        binding = OperationContext.from_trusted_actor(
            auth,
            customer_id=customer_id,
            confirmer_subject_id=auth.subject_id,
            operation_type="ticket:create",
            target_id=ticket.ticket_id,
            payload_digest=digest,
            expected_revision=None,
        )
        return ticket, binding, snapshot

    def _update_intent(
        self,
        auth: AuthContext,
        ticket_id: str,
        content_masked: str,
        visibility: TicketVisibility,
        expected_revision: int,
        context: TicketContextSnapshot,
        idempotency_key: str,
    ) -> tuple[TicketRecord, OperationContext, dict[str, Any], TicketMessage]:
        record = self._authorized_record(auth, ticket_id, "ticket:update", full=True)
        binding, snapshot = self._update_binding(
            auth,
            ticket_id,
            content_masked,
            visibility,
            expected_revision,
            context,
            idempotency_key,
            customer_id=record.ticket.customer_id,
            priority=record.ticket.priority,
        )
        message = TicketMessage(
            message_id=_message_id(auth, ticket_id, idempotency_key),
            author_subject_id=auth.subject_id,
            content_masked=content_masked,
            visibility=visibility,
        )
        record.ticket.add_message(message, expected_revision=expected_revision)
        return record, binding, snapshot, message

    def _update_binding(
        self,
        auth: AuthContext,
        ticket_id: str,
        content_masked: str,
        visibility: TicketVisibility,
        expected_revision: int,
        context: TicketContextSnapshot,
        idempotency_key: str,
        *,
        customer_id: str | None = None,
        priority: TicketPriority | None = None,
    ) -> tuple[OperationContext, dict[str, Any]]:
        verify_tool_context(auth, ("ticket:update",))
        _safe_ticket_id(auth, idempotency_key)
        if visibility is TicketVisibility.INTERNAL and not self._is_staff(auth):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        if not isinstance(visibility, TicketVisibility):
            raise ValueError("visibility is invalid")
        if (
            not isinstance(content_masked, str)
            or not content_masked.strip()
            or len(content_masked) > 4000
        ):
            raise ValueError("content_masked is invalid")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision is invalid")
        if customer_id is None or priority is None:
            record = self._authorized_record(auth, ticket_id, "ticket:update", full=False)
            customer_id, priority = record.ticket.customer_id, record.ticket.priority
        snapshot = context.as_dict(priority)
        digest = content_digest(
            {
                "ticket_id": ticket_id,
                "customer_id": customer_id,
                "message_id": _message_id(auth, ticket_id, idempotency_key),
                "content_masked": content_masked,
                "visibility": visibility.value,
                "expected_revision": expected_revision,
                "context": snapshot,
            }
        )
        binding = OperationContext.from_trusted_actor(
            auth,
            customer_id=customer_id,
            confirmer_subject_id=auth.subject_id,
            operation_type="ticket:update",
            target_id=ticket_id,
            payload_digest=digest,
            expected_revision=expected_revision,
        )
        return binding, snapshot

    def _authorized_record(
        self,
        auth: AuthContext,
        ticket_id: str,
        scope: str,
        *,
        full: bool,
    ) -> TicketRecord:
        verify_tool_context(auth, (scope,))
        # First load customer-visible projection, authorize ownership, then load
        # internal messages only for a trusted staff workflow or update CAS.
        record = self._reads.get(auth.tenant_id, ticket_id)
        if record is None or record.ticket.tenant_id != auth.tenant_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        require_customer_access(auth, record.ticket.customer_id, tenant_id=auth.tenant_id)
        if full or self._is_staff(auth):
            complete = self._reads.get(auth.tenant_id, ticket_id, include_internal=True)
            if complete is None or complete.ticket.customer_id != record.ticket.customer_id:
                raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
            return complete
        return record

    def _require_confirmation(
        self,
        binding: OperationContext,
        confirmation_id: str,
        operation_id: str,
    ) -> bool:
        if not isinstance(confirmation_id, str) or not confirmation_id:
            raise ConfirmationConflictError("confirmation_id is required")
        record = self._confirmations.get(binding.tenant_id, confirmation_id)
        if record is None or record.binding != binding:
            raise ConfirmationConflictError("confirmation is not bound to this ticket content")
        if record.status == "consumed" and record.consumed_by_operation_id == operation_id:
            return True
        now = self._clock()
        if record.status != "verified" or record.expires_at <= now:
            raise ConfirmationConflictError("confirmation is not verified or has expired")
        return False

    def _require_replay(
        self,
        binding: OperationContext,
        idempotency_key: str,
    ) -> TicketWriteResult:
        result = self._writes.replay(binding, idempotency_key)
        if result is None:
            raise ConfirmationConflictError("consumed confirmation has no committed ticket result")
        return result

    @staticmethod
    def _is_staff(auth: AuthContext) -> bool:
        return bool(
            set(auth.roles) & {"support", "human_agent", "supervisor", "operations", "admin"}
        )
