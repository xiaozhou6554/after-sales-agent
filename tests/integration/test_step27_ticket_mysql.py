"""Real local MySQL proof of ticket confirmation, writes and replay safety."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from bootstrap.settings import load_settings
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from application.common.context import AuthContext, DataScope, TraceContext
from application.common.operation_confirmation import OperationConfirmationUseCase
from application.common.ports.confirmation import ConfirmationConflictError
from application.ticket.service import TicketApplicationService, TicketContextSnapshot
from domain.ticket import TicketPriority, TicketVersionConflict, TicketVisibility
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.ticket_repository import (
    MySQLTicketRepository,
    TicketPersistenceConflict,
)
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from infrastructure.ticket.mysql_confirmed_writer import MySQLConfirmedTicketWriter, _operation_id


@pytest.fixture()
def ticket_case():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    tenant = f"step27-{uuid.uuid4().hex[:12]}"
    confirmations = MySQLConfirmationStore(engine)
    repository = MySQLTicketRepository(engine)
    writer = MySQLConfirmedTicketWriter(engine)
    service = TicketApplicationService(repository, writer, confirmations)
    try:
        yield tenant, engine, service, repository, writer, confirmations
    finally:
        with engine.begin() as connection:
            for table in (
                "outbox_event",
                "ticket_write_idempotency",
                "ticket_message",
                "human_handoff",
                "ticket_write_confirmation",
                "ticket",
                "operation_confirmation",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id=:tenant_id"),
                    {"tenant_id": tenant},
                )
        engine.dispose()


def _auth(tenant: str) -> AuthContext:
    now = datetime.now(timezone.utc)
    return AuthContext(
        tenant_id=tenant,
        subject_id="customer-1",
        roles=("customer",),
        scopes=("ticket:create", "ticket:read", "ticket:update"),
        data_scope=DataScope(customer_ids=("customer-1",)),
        session_id="session-step27",
        issued_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
    )


def _context() -> TicketContextSnapshot:
    return TicketContextSnapshot(
        question_masked="设备无法启动",
        summary="设备故障待处理",
        agent_results=("已查询知识库",),
        tool_result_summaries=("需要人工核实",),
        failure_reason=None,
        business_refs=(("order_id", "ORDER-1"),),
        current_state="open",
        next_action="客服核实",
        source_message_ids=("source-1",),
    )


def _trace() -> TraceContext:
    return TraceContext("1.0", "request-step27", "corr-step27", "trace-step27", "thread-step27")


def _verified(confirmations: MySQLConfirmationStore, prepared) -> str:
    confirmation_id = prepared.confirmation.record.confirmation_id
    OperationConfirmationUseCase(confirmations, 300).verify(
        prepared.confirmation.record.binding.tenant_id,
        confirmation_id,
        prepared.confirmation.mock_challenge,
    )
    return confirmation_id


def _create(ticket_case):
    tenant, _, service, _, _, confirmations = ticket_case
    auth = _auth(tenant)
    args = dict(
        customer_id="customer-1",
        title="设备故障",
        description_masked="设备无法启动",
        priority=TicketPriority.NORMAL,
        context=_context(),
        trace=_trace(),
        idempotency_key="step27-create-" + uuid.uuid4().hex,
    )
    prepared = service.prepare_create(auth, **args)
    confirmation_id = _verified(confirmations, prepared)
    result = service.create(auth, **args, confirmation_id=confirmation_id)
    return auth, args, prepared, result


def test_unavailable_handoff_fallback_links_ticket_in_same_commit(ticket_case):
    tenant, engine, service, _, _, confirmations = ticket_case
    auth = _auth(tenant)
    handoff_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO human_handoff"
                " (tenant_id,handoff_id,ticket_id,customer_id,requested_by,reason_code,"
                "summary_masked,context_snapshot,status,assigned_to,revision,thread_id,"
                "correlation_id,created_at,updated_at) VALUES"
                " (:tenant,:handoff,NULL,:customer,:actor,'HUMAN_UNAVAILABLE',"
                "'人工暂不可用','{}','unavailable',NULL,1,:thread,:correlation,:now,:now)"
            ),
            {
                "tenant": tenant,
                "handoff": handoff_id,
                "customer": "customer-1",
                "actor": auth.subject_id,
                "thread": "thread-step27",
                "correlation": "corr-step27",
                "now": now,
            },
        )
    args = dict(
        customer_id="customer-1",
        title="人工不可用工单",
        description_masked="设备故障待核实",
        priority=TicketPriority.NORMAL,
        context=_context(),
        trace=_trace(),
        idempotency_key="fallback-" + uuid.uuid4().hex,
    )
    prepared = service.prepare_create(auth, **args, handoff_id=handoff_id)
    confirmation_id = _verified(confirmations, prepared)
    with pytest.raises(ConfirmationConflictError):
        service.create(auth, **args, confirmation_id=confirmation_id)

    def fail_event(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "INSERT INTO outbox_event" in statement:
            raise RuntimeError("synthetic ticket outbox outage")

    event.listen(engine, "before_cursor_execute", fail_event)
    try:
        with pytest.raises(RuntimeError, match="synthetic ticket outbox outage"):
            service.create(auth, **args, confirmation_id=confirmation_id, handoff_id=handoff_id)
    finally:
        event.remove(engine, "before_cursor_execute", fail_event)
    assert confirmations.get(tenant, confirmation_id).status == "verified"
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT ticket_id,revision FROM human_handoff"
                " WHERE tenant_id=:tenant AND handoff_id=:handoff"
            ),
            {"tenant": tenant, "handoff": handoff_id},
        ).one() == (None, 1)
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM ticket WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            ).scalar_one()
            == 0
        )
    created = service.create(auth, **args, confirmation_id=confirmation_id, handoff_id=handoff_id)
    assert created.ticket_id == prepared.ticket_id and not created.replayed
    with engine.connect() as connection:
        linked = connection.execute(
            text(
                "SELECT ticket_id,revision FROM human_handoff"
                " WHERE tenant_id=:tenant AND handoff_id=:handoff"
            ),
            {"tenant": tenant, "handoff": handoff_id},
        ).one()
    assert (linked.ticket_id, linked.revision) == (created.ticket_id, 2)
    replayed = service.create(auth, **args, confirmation_id=confirmation_id, handoff_id=handoff_id)
    assert replayed.ticket_id == created.ticket_id and replayed.replayed


def _block_outbox_event(engine, tenant: str, event_type: str, operation_id: str) -> None:
    """Cause a real primary-key failure at the final Outbox insert."""
    event_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{event_type}:{operation_id}").hex
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO outbox_event (
                tenant_id,event_id,owner_module,event_type,event_version,schema_version,
                request_id,correlation_id,trace_id,thread_id,actor_subject_id,customer_id,
                aggregate_type,aggregate_id,idempotency_key,payload,status,attempts,
                next_retry_at,created_at
            ) VALUES (
                :tenant,:event_id,'ticket-service',:event_type,1,'1.0',
                'blocked-request','blocked-corr','blocked-trace',NULL,'customer-1','customer-1',
                'ticket','blocked-ticket','blocked-event-key-01','{}','pending',0,:now,:now
            )"""),
            {
                "tenant": tenant,
                "event_id": event_id,
                "event_type": event_type,
                "now": datetime.now(timezone.utc).replace(tzinfo=None),
            },
        )


def test_outbox_failure_rolls_back_confirmed_create(ticket_case) -> None:
    tenant, engine, service, repository, _, confirmations = ticket_case
    auth = _auth(tenant)
    args = dict(
        customer_id="customer-1",
        title="device failure",
        description_masked="masked issue",
        priority=TicketPriority.NORMAL,
        context=_context(),
        trace=_trace(),
        idempotency_key="rollback-create-" + uuid.uuid4().hex,
    )
    prepared = service.prepare_create(auth, **args)
    confirmation_id = _verified(confirmations, prepared)
    operation_id = _operation_id(prepared.confirmation.record.binding, args["idempotency_key"])
    _block_outbox_event(engine, tenant, "ticket.created", operation_id)
    with pytest.raises(IntegrityError):
        service.create(auth, **args, confirmation_id=confirmation_id)
    assert confirmations.get(tenant, confirmation_id).status == "verified"
    assert repository.get(tenant, prepared.ticket_id) is None
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM ticket_write_idempotency WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        ).scalar_one()
    assert count == 0


def test_outbox_failure_rolls_back_confirmed_update(ticket_case) -> None:
    tenant, engine, service, repository, _, confirmations = ticket_case
    auth, _, _, created = _create(ticket_case)
    args = dict(
        ticket_id=created.ticket_id,
        content_masked="new masked message",
        visibility=TicketVisibility.CUSTOMER,
        expected_revision=1,
        context=_context(),
        idempotency_key="rollback-update-" + uuid.uuid4().hex,
    )
    prepared = service.prepare_update(auth, **args)
    confirmation_id = _verified(confirmations, prepared)
    operation_id = _operation_id(prepared.confirmation.record.binding, args["idempotency_key"])
    _block_outbox_event(engine, tenant, "ticket.updated", operation_id)
    with pytest.raises(IntegrityError):
        service.update(auth, **args, confirmation_id=confirmation_id, trace=_trace())
    assert confirmations.get(tenant, confirmation_id).status == "verified"
    stored = repository.get(tenant, created.ticket_id)
    assert stored.ticket.revision == 1
    assert stored.ticket.messages == ()
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text("SELECT operation_type FROM ticket_write_idempotency WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            .scalars()
            .all()
        )
    assert rows == ["create"]


def test_create_update_replay_and_visibility_in_real_mysql(ticket_case) -> None:
    tenant, engine, service, repository, _, confirmations = ticket_case
    auth, args, prepared, created = _create(ticket_case)
    assert created.ticket_id == prepared.ticket_id and created.revision == 1
    create_confirmation = prepared.confirmation.record.confirmation_id
    assert confirmations.get(tenant, create_confirmation).status == "consumed"
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT event_type,request_id,correlation_id,trace_id,thread_id,payload "
                    "FROM outbox_event WHERE tenant_id=:tenant AND aggregate_id=:ticket_id"
                ),
                {"tenant": tenant, "ticket_id": created.ticket_id},
            )
            .mappings()
            .one()
        )
    event = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    assert row["event_type"] == "ticket.created"
    assert (row["request_id"], row["correlation_id"], row["trace_id"], row["thread_id"]) == (
        "request-step27",
        "corr-step27",
        "trace-step27",
        "thread-step27",
    )
    assert event["payload"] == {
        "ticket_id": created.ticket_id,
        "revision": 1,
        "status": "open",
        "priority": "normal",
        "context_ref": f"ticket:{created.ticket_id}:context:1",
        "actor_subject_id": auth.subject_id,
        "customer_id": "customer-1",
    }
    assert "question_masked" not in json.dumps(event)
    replay = service.create(auth, **args, confirmation_id=create_confirmation)
    assert replay.replayed and replay.ticket_id == created.ticket_id
    assert repository.get("other-tenant", created.ticket_id) is None

    update_args = dict(
        ticket_id=created.ticket_id,
        content_masked="补充型号 A",
        visibility=TicketVisibility.CUSTOMER,
        expected_revision=1,
        context=_context(),
        idempotency_key="step27-update-" + uuid.uuid4().hex,
    )
    update_prepared = service.prepare_update(auth, **update_args)
    update_confirmation = _verified(confirmations, update_prepared)
    update_trace = TraceContext(
        "1.0",
        "request-update-step27",
        "corr-update-step27",
        "trace-update-step27",
        "thread-step27",
    )
    changed = service.update(
        auth, **update_args, confirmation_id=update_confirmation, trace=update_trace
    )
    assert changed.revision == 2
    assert service.update(
        auth, **update_args, confirmation_id=update_confirmation, trace=update_trace
    ).replayed
    assert len(service.query(auth, created.ticket_id).ticket.messages) == 1
    with engine.connect() as connection:
        counts = (
            connection.execute(
                text("""SELECT
            (SELECT COUNT(*) FROM ticket WHERE tenant_id=:tenant) AS tickets,
            (SELECT COUNT(*) FROM ticket_message WHERE tenant_id=:tenant) AS messages,
            (SELECT COUNT(*) FROM ticket_write_idempotency WHERE tenant_id=:tenant) AS writes
        """),
                {"tenant": tenant},
            )
            .mappings()
            .one()
        )
    assert counts == {"tickets": 1, "messages": 1, "writes": 2}
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT event_type,request_id,correlation_id,trace_id,thread_id,payload "
                    "FROM outbox_event WHERE tenant_id=:tenant AND aggregate_id=:ticket_id "
                    "ORDER BY event_type"
                ),
                {"tenant": tenant, "ticket_id": created.ticket_id},
            )
            .mappings()
            .all()
        )
    assert [row["event_type"] for row in rows] == ["ticket.created", "ticket.updated"]
    updated = rows[1]
    assert (
        updated["request_id"],
        updated["correlation_id"],
        updated["trace_id"],
        updated["thread_id"],
    ) == ("request-update-step27", "corr-update-step27", "trace-update-step27", "thread-step27")
    payload = (
        updated["payload"]
        if isinstance(updated["payload"], dict)
        else json.loads(updated["payload"])
    )
    assert payload["payload"] == {
        "ticket_id": created.ticket_id,
        "revision": 2,
        "status": "open",
        "priority": "normal",
        "context_ref": f"ticket:{created.ticket_id}:context:2",
        "actor_subject_id": auth.subject_id,
        "customer_id": "customer-1",
    }
    assert "content_masked" not in json.dumps(payload)


def test_changed_content_and_stale_revision_do_not_consume_confirmation(ticket_case) -> None:
    tenant, _, service, _, _, confirmations = ticket_case
    auth, _, _, created = _create(ticket_case)
    args = dict(
        ticket_id=created.ticket_id,
        content_masked="补充型号 A",
        visibility=TicketVisibility.CUSTOMER,
        expected_revision=1,
        context=_context(),
        idempotency_key="step27-update-" + uuid.uuid4().hex,
    )
    prepared = service.prepare_update(auth, **args)
    confirmation_id = _verified(confirmations, prepared)
    with pytest.raises(ConfirmationConflictError):
        service.update(
            auth,
            **{**args, "content_masked": "变更后的内容"},
            confirmation_id=confirmation_id,
            trace=_trace(),
        )
    # A separate confirmed operation advances revision before the prepared write.
    other = {**args, "content_masked": "另一条补充", "idempotency_key": "other-" + uuid.uuid4().hex}
    other_prepared = service.prepare_update(auth, **other)
    service.update(
        auth, **other, confirmation_id=_verified(confirmations, other_prepared), trace=_trace()
    )
    with pytest.raises(TicketVersionConflict):
        service.update(auth, **args, confirmation_id=confirmation_id, trace=_trace())
    assert confirmations.get(tenant, confirmation_id).status == "verified"


def test_duplicate_ticket_insert_rolls_back_consumed_confirmation(ticket_case) -> None:
    tenant, engine, _, repository, writer, confirmations = ticket_case
    _, _, prepared, created = _create(ticket_case)
    alternate_key = "alternate-" + uuid.uuid4().hex
    # A second valid confirmation intentionally targets the same ID but a new
    # idempotency key. The INSERT fails; its confirmation must remain reusable.
    alternate_binding = replace(
        prepared.confirmation.record.binding,
        payload_digest=prepared.confirmation.record.binding.payload_digest,
    )
    alternate = OperationConfirmationUseCase(confirmations, 300).prepare(alternate_binding)
    alternate_id = _verified(confirmations, replace(prepared, confirmation=alternate))
    from infrastructure.ticket.mysql_confirmed_writer import _operation_id

    with pytest.raises(TicketPersistenceConflict):
        writer.create_confirmed(
            repository.get(tenant, created.ticket_id).ticket,
            binding=alternate_binding,
            confirmation_id=alternate_id,
            operation_id=_operation_id(alternate_binding, alternate_key),
            idempotency_key=alternate_key,
            context_snapshot=_context().as_dict(TicketPriority.NORMAL),
            trace=_trace(),
        )
    assert confirmations.get(tenant, alternate_id).status == "verified"
    with engine.connect() as connection:
        count = connection.execute(
            text("""SELECT COUNT(*) FROM ticket_write_idempotency
            WHERE tenant_id=:tenant"""),
            {"tenant": tenant},
        ).scalar_one()
    assert count == 1


def test_concurrent_same_confirmation_creates_only_one_ticket(ticket_case) -> None:
    tenant, engine, service, _, _, confirmations = ticket_case
    auth = _auth(tenant)
    args = dict(
        customer_id="customer-1",
        title="设备故障",
        description_masked="设备无法启动",
        priority=TicketPriority.NORMAL,
        context=_context(),
        trace=_trace(),
        idempotency_key="step27-race-" + uuid.uuid4().hex,
    )
    prepared = service.prepare_create(auth, **args)
    confirmation_id = _verified(confirmations, prepared)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        return service.create(auth, **args, confirmation_id=confirmation_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert {result.ticket_id for result in results} == {prepared.ticket_id}
    assert sorted(result.replayed for result in results) == [False, True]
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM ticket WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant},
        ).scalar_one()
    assert count == 1
