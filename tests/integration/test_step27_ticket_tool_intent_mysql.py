"""Local MySQL proof for the DEV-only server-trusted ticket intent sidecar."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bootstrap.settings import load_settings
from migrations.versions.v0017_ticket_tool_intent import statements
from sqlalchemy import text

from application.common.context import AuthContext, DataScope, TraceContext
from application.common.operation_confirmation import OperationConfirmationUseCase
from application.ticket.service import TicketApplicationService, TicketContextSnapshot
from application.ticket.tool_intent import TicketToolIntentConflict
from domain.ticket import TicketPriority, TicketVisibility
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.repositories.ticket_repository import MySQLTicketRepository
from infrastructure.security.confirmation_store import MySQLConfirmationStore
from infrastructure.ticket.mysql_confirmed_writer import MySQLConfirmedTicketWriter
from infrastructure.ticket.mysql_tool_intent import MySQLTicketToolIntentStore


@pytest.fixture
def ticket_case():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    with engine.begin() as connection:
        for statement in statements():
            connection.execute(text(statement))
    tenant = f"step27-intent-{uuid.uuid4().hex[:10]}"
    confirmations = MySQLConfirmationStore(engine)
    service = TicketApplicationService(
        MySQLTicketRepository(engine), MySQLConfirmedTicketWriter(engine), confirmations
    )
    store = MySQLTicketToolIntentStore(engine, service)
    try:
        yield tenant, engine, confirmations, service, store
    finally:
        with engine.begin() as connection:
            for table in (
                "outbox_event",
                "ticket_write_idempotency",
                "ticket_message",
                "ticket_tool_intent",
                "ticket",
                "operation_confirmation",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id=:tenant"), {"tenant": tenant}
                )
        engine.dispose()


def _auth(tenant, *, session="session-a", customer="customer-1"):
    now = datetime.now(timezone.utc)
    return AuthContext(
        tenant,
        customer,
        ("customer",),
        ("ticket:create", "ticket:update", "ticket:read"),
        DataScope(customer_ids=(customer,)),
        session,
        now - timedelta(minutes=1),
        now + timedelta(hours=1),
    )


def _context():
    return TicketContextSnapshot(
        "masked issue",
        "masked summary",
        ("agent summary",),
        ("tool summary",),
        None,
        (("order_id", "ORDER-1"),),
        "open",
        "contact support",
    )


def _trace():
    return TraceContext("1.0", "request-intent", "corr-intent", "trace-intent", "thread-intent")


def _prepare_create(case, auth, *, title="device failure"):
    _, _, confirmations, service, store = case
    args = dict(
        customer_id="customer-1",
        title=title,
        description_masked="masked description",
        priority=TicketPriority.NORMAL,
        context=_context(),
        trace=_trace(),
        idempotency_key=uuid.uuid4().hex,
    )
    prepared = service.prepare_create(auth, **args)
    cid = prepared.confirmation.record.confirmation_id
    OperationConfirmationUseCase(confirmations, 300).verify(
        auth.tenant_id, cid, prepared.confirmation.mock_challenge
    )
    return args, cid, store


def test_create_resolve_and_consumed_replay(ticket_case):
    tenant, _, _, service, store = ticket_case
    auth = _auth(tenant)
    args, cid, _ = _prepare_create(ticket_case, auth)
    store.register_create(auth, confirmation_id=cid, **args)
    visible = {key: args[key] for key in ("customer_id", "title", "description_masked", "priority")}
    intent = store.resolve_create(auth, **visible)
    assert intent.confirmation_id == cid
    assert intent.idempotency_key == args["idempotency_key"]
    assert set(visible).isdisjoint({"confirmation_id", "idempotency_key"})
    created = service.create(auth, **intent.commit_kwargs())
    assert created.revision == 1 and not created.replayed
    replay_intent = store.resolve_create(auth, **visible)
    assert service.create(auth, **replay_intent.commit_kwargs()).replayed
    with pytest.raises(TicketToolIntentConflict):
        store.resolve_create(_auth(tenant, session="other-session"), **visible)
    with pytest.raises(TicketToolIntentConflict):
        store.resolve_create(_auth("other-tenant"), **visible)
    with pytest.raises(TicketToolIntentConflict):
        store.resolve_create(auth, **{**visible, "title": "changed"})


def test_update_resolve_and_replay(ticket_case):
    tenant, _, confirmations, service, store = ticket_case
    auth = _auth(tenant)
    create_args, cid, _ = _prepare_create(ticket_case, auth)
    store.register_create(auth, confirmation_id=cid, **create_args)
    created = service.create(
        auth,
        **store.resolve_create(
            auth,
            customer_id="customer-1",
            title=create_args["title"],
            description_masked=create_args["description_masked"],
            priority=TicketPriority.NORMAL,
        ).commit_kwargs(),
    )
    args = dict(
        ticket_id=created.ticket_id,
        content_masked="masked follow-up",
        visibility=TicketVisibility.CUSTOMER,
        expected_revision=1,
        context=_context(),
        idempotency_key=uuid.uuid4().hex,
    )
    prepared = service.prepare_update(auth, **args)
    update_cid = prepared.confirmation.record.confirmation_id
    OperationConfirmationUseCase(confirmations, 300).verify(
        tenant, update_cid, prepared.confirmation.mock_challenge
    )
    store.register_update(auth, confirmation_id=update_cid, trace=_trace(), **args)
    visible = {key: args[key] for key in ("ticket_id", "content_masked", "visibility")}
    intent = store.resolve_update(auth, **visible)
    assert intent.expected_revision == 1
    assert service.update(auth, **intent.commit_kwargs()).revision == 2
    assert service.update(auth, **store.resolve_update(auth, **visible).commit_kwargs()).replayed


def test_unverified_ambiguous_and_expired_fail_closed(ticket_case):
    tenant, engine, confirmations, service, store = ticket_case
    auth = _auth(tenant)
    args, cid, _ = _prepare_create(ticket_case, auth)
    visible = {key: args[key] for key in ("customer_id", "title", "description_masked", "priority")}
    other = service.prepare_create(auth, **{**args, "idempotency_key": uuid.uuid4().hex})
    with pytest.raises(TicketToolIntentConflict):
        store.register_create(
            auth,
            confirmation_id=other.confirmation.record.confirmation_id,
            **args,
        )
    store.register_create(auth, confirmation_id=cid, **args)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE operation_confirmation SET expires_at=:past "
                "WHERE tenant_id=:tenant AND confirmation_id=:cid"
            ),
            {
                "past": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1),
                "tenant": tenant,
                "cid": cid,
            },
        )
    with pytest.raises(TicketToolIntentConflict):
        store.resolve_create(auth, **visible)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE operation_confirmation SET expires_at=:future "
                "WHERE tenant_id=:tenant AND confirmation_id=:cid"
            ),
            {
                "future": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=2),
                "tenant": tenant,
                "cid": cid,
            },
        )
        connection.execute(
            text(
                "UPDATE ticket_tool_intent SET expires_at=:future "
                "WHERE tenant_id=:tenant AND confirmation_id=:cid"
            ),
            {
                "future": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=2),
                "tenant": tenant,
                "cid": cid,
            },
        )
    args2, cid2, _ = _prepare_create(ticket_case, auth)
    args2["title"] = args["title"]
    store.register_create(auth, confirmation_id=cid2, **args2)
    with pytest.raises(TicketToolIntentConflict):
        store.resolve_create(auth, **visible)
