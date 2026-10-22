"""Step 27 application authorization and confirmation, with atomic fake ports."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, DataScope, OperationContext, TraceContext
from application.common.operation_confirmation import OperationConfirmationUseCase
from application.common.ports.confirmation import ConfirmationConflictError, ConfirmationRecord
from application.ticket.service import (
    TicketApplicationService,
    TicketContextSnapshot,
    TicketRecord,
    TicketWriteResult,
)
from domain.ticket import TicketMessage, TicketPriority, TicketVersionConflict, TicketVisibility

NOW = datetime.now(timezone.utc)


class FakeConfirmations:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], ConfirmationRecord] = {}
        self.challenges: dict[tuple[str, str], str] = {}

    def issue(
        self, binding: OperationContext, challenge_digest: str, expires_at: datetime
    ) -> ConfirmationRecord:
        confirmation_id = uuid.uuid4().hex
        record = ConfirmationRecord(confirmation_id, binding, "prepared", expires_at, None)
        key = (binding.tenant_id, confirmation_id)
        self.records[key] = record
        self.challenges[key] = challenge_digest
        return record

    def verify(
        self, tenant_id: str, confirmation_id: str, challenge_digest: str, now: datetime
    ) -> ConfirmationRecord:
        key = (tenant_id, confirmation_id)
        record = self.records.get(key)
        if (
            record is None
            or record.status != "prepared"
            or record.expires_at <= now
            or self.challenges[key] != challenge_digest
        ):
            raise ConfirmationConflictError("invalid challenge")
        record = replace(record, status="verified")
        self.records[key] = record
        return record

    def consume(
        self, confirmation_id: str, binding: OperationContext, operation_id: str, now: datetime
    ) -> ConfirmationRecord:
        key = (binding.tenant_id, confirmation_id)
        record = self.records.get(key)
        if (
            record is None
            or record.status != "verified"
            or record.binding != binding
            or record.expires_at <= now
        ):
            raise ConfirmationConflictError("confirmation changed")
        record = replace(record, status="consumed", consumed_by_operation_id=operation_id)
        self.records[key] = record
        return record

    def get(self, tenant_id: str, confirmation_id: str) -> ConfirmationRecord | None:
        return self.records.get((tenant_id, confirmation_id))


class FakeTickets:
    def __init__(self, confirmations: FakeConfirmations) -> None:
        self.confirmations = confirmations
        self.rows: dict[tuple[str, str], TicketRecord] = {}
        self.idempotency: dict[tuple[str, str, str, str], tuple[str, TicketWriteResult]] = {}
        self.create_calls = 0
        self.update_calls = 0

    def get(self, tenant_id: str, ticket_id: str, *, include_internal: bool = False):
        record = self.rows.get((tenant_id, ticket_id))
        if record is None or include_internal:
            return record
        visible = tuple(
            m for m in record.ticket.messages if m.visibility is TicketVisibility.CUSTOMER
        )
        return replace(record, ticket=replace(record.ticket, messages=visible))

    def replay(self, binding: OperationContext, idempotency_key: str):
        key = (binding.tenant_id, binding.actor_subject_id, binding.operation_type, idempotency_key)
        existing = self.idempotency.get(key)
        if existing is None:
            return None
        if existing[0] != binding.payload_digest:
            raise ValueError("idempotency content changed")
        return replace(existing[1], replayed=True)

    def _commit(
        self,
        ticket,
        binding,
        confirmation_id,
        operation_id,
        idempotency_key,
        context_snapshot,
        trace=None,
    ):
        replay = self.replay(binding, idempotency_key)
        if replay is not None:
            return replay
        key = (binding.tenant_id, ticket.ticket_id)
        existing = self.rows.get(key)
        if binding.operation_type == "ticket:create":
            if existing is not None:
                raise ValueError("ticket exists")
        elif existing is None or existing.ticket.revision != binding.expected_revision:
            raise TicketVersionConflict("revision changed")
        # Fake models one atomic commit boundary: all guards run before consuming.
        self.confirmations.consume(
            confirmation_id, binding, operation_id, datetime.now(timezone.utc)
        )
        if trace is None:
            assert existing is not None
            trace_values = (existing.thread_id, existing.correlation_id, existing.trace_id)
            self.update_calls += 1
        else:
            trace_values = (trace.thread_id, trace.correlation_id, trace.trace_id)
            self.create_calls += 1
        self.rows[key] = TicketRecord(ticket, context_snapshot, *trace_values)
        result = TicketWriteResult(ticket.ticket_id, ticket.revision)
        idem_key = (
            binding.tenant_id,
            binding.actor_subject_id,
            binding.operation_type,
            idempotency_key,
        )
        self.idempotency[idem_key] = (binding.payload_digest, result)
        return result

    def create_confirmed(
        self,
        ticket,
        *,
        binding,
        confirmation_id,
        operation_id,
        idempotency_key,
        context_snapshot,
        trace,
        handoff_id=None,
    ):
        return self._commit(
            ticket, binding, confirmation_id, operation_id, idempotency_key, context_snapshot, trace
        )

    def update_confirmed(
        self,
        ticket,
        *,
        expected_revision,
        binding,
        confirmation_id,
        operation_id,
        idempotency_key,
        context_snapshot,
        trace,
    ):
        assert ticket.revision == expected_revision + 1
        assert isinstance(trace, TraceContext)
        return self._commit(
            ticket, binding, confirmation_id, operation_id, idempotency_key, context_snapshot
        )


def auth(
    tenant: str = "tenant-a",
    *,
    customer: str = "customer-1",
    subject: str = "customer-1",
    scopes=("ticket:create", "ticket:read", "ticket:update"),
    roles=("customer",),
) -> AuthContext:
    return AuthContext(
        tenant_id=tenant,
        subject_id=subject,
        roles=roles,
        scopes=scopes,
        data_scope=DataScope(customer_ids=(customer,)),
        session_id="session-1",
        issued_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
    )


def trace() -> TraceContext:
    return TraceContext("1.0", "request-1", "corr-1", "trace-1", "thread-1")


def context() -> TicketContextSnapshot:
    return TicketContextSnapshot(
        question_masked="设备无法启动",
        summary="设备故障待核实",
        agent_results=("已检索保修政策",),
        tool_result_summaries=("订单状态待核实",),
        failure_reason=None,
        business_refs=(("order_id", "ORDER-1"),),
        current_state="open",
        next_action="人工核实",
        source_message_ids=("source-1",),
    )


@pytest.fixture
def harness():
    confirmations = FakeConfirmations()
    tickets = FakeTickets(confirmations)
    service = TicketApplicationService(tickets, tickets, confirmations)
    return service, tickets, confirmations


def _verified(confirmations: FakeConfirmations, prepared) -> str:
    OperationConfirmationUseCase(confirmations, 300).verify(
        prepared.confirmation.record.binding.tenant_id,
        prepared.confirmation.record.confirmation_id,
        prepared.confirmation.mock_challenge,
    )
    return prepared.confirmation.record.confirmation_id


def _create(harness, actor: AuthContext | None = None):
    service, _, confirmations = harness
    actor = actor or auth()
    args = dict(
        customer_id="customer-1",
        title="设备故障",
        description_masked="设备无法启动",
        priority=TicketPriority.NORMAL,
        context=context(),
        trace=trace(),
        idempotency_key="create-1",
    )
    prepared = service.prepare_create(actor, **args)
    result = service.create(actor, **args, confirmation_id=_verified(confirmations, prepared))
    return args, result, prepared


def test_create_query_and_idempotent_replay(harness) -> None:
    service, tickets, _ = harness
    args, result, prepared = _create(harness)
    assert result.revision == 1 and tickets.create_calls == 1
    replay = service.create(
        auth(), **args, confirmation_id=prepared.confirmation.record.confirmation_id
    )
    assert replay.replayed and replay.ticket_id == result.ticket_id
    assert tickets.create_calls == 1
    queried = service.query(auth(), result.ticket_id)
    assert queried.context_snapshot["summary"] == "设备故障待核实"
    assert queried.correlation_id == "corr-1"


def test_cross_tenant_and_out_of_scope_cannot_query_or_update(harness) -> None:
    service, tickets, _ = harness
    _, result, _ = _create(harness)
    with pytest.raises(AuthorizationDenied):
        service.query(auth("tenant-b"), result.ticket_id)
    with pytest.raises(AuthorizationDenied):
        service.query(auth(customer="customer-other"), result.ticket_id)
    with pytest.raises(AuthorizationDenied):
        service.prepare_update(
            auth(customer="customer-other"),
            ticket_id=result.ticket_id,
            content_masked="补充信息",
            visibility=TicketVisibility.CUSTOMER,
            expected_revision=1,
            context=context(),
            idempotency_key="update-1",
        )
    assert tickets.update_calls == 0


def test_create_requires_verified_confirmation_and_exact_content(harness) -> None:
    service, tickets, confirmations = harness
    args = dict(
        customer_id="customer-1",
        title="设备故障",
        description_masked="设备无法启动",
        priority=TicketPriority.NORMAL,
        context=context(),
        trace=trace(),
        idempotency_key="create-1",
    )
    prepared = service.prepare_create(auth(), **args)
    confirmation_id = prepared.confirmation.record.confirmation_id
    with pytest.raises(ConfirmationConflictError):
        service.create(auth(), **args, confirmation_id=confirmation_id)
    _verified(confirmations, prepared)
    with pytest.raises(ConfirmationConflictError):
        service.create(
            auth(), **{**args, "description_masked": "内容已变更"}, confirmation_id=confirmation_id
        )
    assert tickets.create_calls == 0
    assert confirmations.get("tenant-a", confirmation_id).status == "verified"


def test_update_revision_and_idempotency(harness) -> None:
    service, tickets, confirmations = harness
    _, result, _ = _create(harness)
    args = dict(
        ticket_id=result.ticket_id,
        content_masked="补充设备型号",
        visibility=TicketVisibility.CUSTOMER,
        expected_revision=1,
        context=context(),
        idempotency_key="update-1",
    )
    prepared = service.prepare_update(auth(), **args)
    confirmation_id = _verified(confirmations, prepared)
    changed = service.update(auth(), **args, confirmation_id=confirmation_id, trace=trace())
    assert changed.revision == 2 and tickets.update_calls == 1
    assert service.update(auth(), **args, confirmation_id=confirmation_id, trace=trace()).replayed
    assert tickets.update_calls == 1
    assert len(service.query(auth(), result.ticket_id).ticket.messages) == 1


def test_update_content_change_and_stale_revision_fail_closed(harness) -> None:
    service, tickets, confirmations = harness
    _, result, _ = _create(harness)
    args = dict(
        ticket_id=result.ticket_id,
        content_masked="补充设备型号",
        visibility=TicketVisibility.CUSTOMER,
        expected_revision=1,
        context=context(),
        idempotency_key="update-1",
    )
    prepared = service.prepare_update(auth(), **args)
    confirmation_id = _verified(confirmations, prepared)
    with pytest.raises(ConfirmationConflictError):
        service.update(
            auth(),
            **{**args, "content_masked": "改变后的内容"},
            confirmation_id=confirmation_id,
            trace=trace(),
        )
    key = ("tenant-a", result.ticket_id)
    tickets.rows[key] = replace(
        tickets.rows[key], ticket=replace(tickets.rows[key].ticket, revision=2)
    )
    with pytest.raises(TicketVersionConflict):
        service.update(auth(), **args, confirmation_id=confirmation_id, trace=trace())
    assert tickets.update_calls == 0
    assert confirmations.get("tenant-a", confirmation_id).status == "verified"


def test_customer_cannot_write_internal_or_read_staff_message(harness) -> None:
    service, tickets, _ = harness
    _, result, _ = _create(harness)
    with pytest.raises(AuthorizationDenied):
        service.prepare_update(
            auth(),
            ticket_id=result.ticket_id,
            content_masked="内部备注",
            visibility=TicketVisibility.INTERNAL,
            expected_revision=1,
            context=context(),
            idempotency_key="internal",
        )
    key = ("tenant-a", result.ticket_id)
    internal = tickets.rows[key].ticket.add_message(
        TicketMessage("staff-message", "staff-1", "仅员工可见", TicketVisibility.INTERNAL),
        expected_revision=1,
    )
    tickets.rows[key] = replace(tickets.rows[key], ticket=internal)
    assert service.query(auth(), result.ticket_id).ticket.messages == ()
    staff = auth(subject="staff-1", roles=("support",))
    assert len(service.query(staff, result.ticket_id).ticket.messages) == 1


def test_missing_scope_and_context_bounds(harness) -> None:
    service, _, _ = harness
    with pytest.raises(AuthorizationDenied):
        service.prepare_create(
            auth(scopes=("ticket:read",)),
            customer_id="customer-1",
            title="设备故障",
            description_masked="设备无法启动",
            priority=TicketPriority.NORMAL,
            context=context(),
            trace=trace(),
            idempotency_key="create-1",
        )
    too_long = replace(context(), summary="x" * 1001)
    with pytest.raises(ValueError):
        service.prepare_create(
            auth(),
            customer_id="customer-1",
            title="设备故障",
            description_masked="设备无法启动",
            priority=TicketPriority.NORMAL,
            context=too_long,
            trace=trace(),
            idempotency_key="create-1",
        )
