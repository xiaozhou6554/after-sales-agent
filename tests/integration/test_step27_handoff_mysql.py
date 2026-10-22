"""Real MySQL queue capacity, replay, association and competing claims."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from bootstrap.settings import load_settings
from migrations.versions.v0016_handoff_queue import statements
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from application.common.context import AuthContext, DataScope, TraceContext
from application.ticket.handoff import HandoffConflict, MockHumanHandoff
from application.ticket.service import TicketContextSnapshot
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.persistence.mysql.migration_runner import MySQLMigrationRunner
from infrastructure.ticket.mysql_handoff_queue import MySQLHandoffQueue


@pytest.fixture
def case():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"local MySQL unavailable: {exc.orig.args[0]}")
    MySQLMigrationRunner(engine).run_statements(statements())
    tenant = f"handoff-{uuid.uuid4().hex[:12]}"
    try:
        yield tenant, engine, MockHumanHandoff(MySQLHandoffQueue(engine), capacity=1)
    finally:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM outbox_event WHERE tenant_id=:tenant "
                    "AND aggregate_type='human_handoff'"
                ),
                {"tenant": tenant},
            )
            for table in (
                "human_handoff_claim",
                "human_handoff_idempotency",
                "human_handoff",
                "human_handoff_capacity",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id=:tenant"), {"tenant": tenant}
                )
        engine.dispose()


def auth(tenant, subject="customer-1", *, agent=False, customers=("customer-1",)):
    now = datetime.now(timezone.utc)
    return AuthContext(
        tenant,
        subject,
        ("human_agent",) if agent else ("customer",),
        ("handoff:accept",) if agent else ("ticket:create",),
        DataScope(customer_ids=customers),
        "session",
        now - timedelta(minutes=1),
        now + timedelta(hours=1),
    )


def context():
    return TicketContextSnapshot(
        "masked question", "masked summary", (), (), "unresolved", (), "open", "human review"
    )


def request(service, tenant, key, *, available=True, ticket_id=None):
    return service.request(
        auth(tenant),
        TraceContext("1.0", "request", "corr", "trace", "thread"),
        customer_id="customer-1",
        reason_code="unresolved",
        context=context(),
        idempotency_key=key,
        available=available,
        ticket_id=ticket_id,
    )


def test_queue_capacity_replay_and_unavailable(case):
    tenant, engine, service = case
    first = request(service, tenant, "one")
    assert first.status == "queued"
    assert request(service, tenant, "one").handoff_id == first.handoff_id
    assert request(service, tenant, "one").replayed
    assert request(service, tenant, "two").status == "unavailable"
    assert request(service, tenant, "three", available=False).status == "unavailable"
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT event_type,aggregate_id,payload FROM outbox_event "
                    "WHERE tenant_id=:tenant AND event_type='human.handoff.created'"
                ),
                {"tenant": tenant},
            )
            .mappings()
            .all()
        )
    assert len(rows) == 3
    assert any(row["aggregate_id"] == first.handoff_id for row in rows)
    assert all("masked summary" not in row["payload"] for row in rows)


def test_concurrent_claim_one_winner_and_repeat(case):
    tenant, _, service = case
    first = request(service, tenant, "one")

    def claim(subject):
        try:
            return service.claim(
                auth(tenant, subject, agent=True),
                handoff_id=first.handoff_id,
                expected_revision=1,
                idempotency_key="click",
            )
        except HandoffConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("human-1", "human-2")))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].status == "accepted"
    assert claim(winners[0].assigned_to).replayed
    assert request(service, tenant, "next").status == "queued"


def test_claim_scope_and_ticket_association(case):
    tenant, engine, service = case
    first = request(service, tenant, "one")
    from application.common.authorization import AuthorizationDenied

    with pytest.raises(AuthorizationDenied):
        service.claim(
            auth(tenant, "human-1", agent=True, customers=()),
            handoff_id=first.handoff_id,
            expected_revision=1,
            idempotency_key="click",
        )
    with pytest.raises(HandoffConflict):
        service.claim(
            auth(tenant, "human-1", agent=True),
            handoff_id=first.handoff_id,
            expected_revision=1,
            idempotency_key="click",
            ticket_id="missing",
        )
    assert MySQLHandoffQueue(engine).get(tenant, first.handoff_id)[1].status == "queued"


def test_handoff_outbox_failure_rolls_back_queue_and_idempotency(case):
    tenant, engine, _ = case

    class FailingOutbox:
        def enqueue(self, *_args, **_kwargs):
            raise RuntimeError("synthetic outbox failure")

    broken = MockHumanHandoff(MySQLHandoffQueue(engine, FailingOutbox()), capacity=1)
    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        request(broken, tenant, "failed-event")
    with engine.connect() as conn:
        for table in ("human_handoff", "human_handoff_idempotency"):
            assert (
                conn.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id=:tenant"),
                    {"tenant": tenant},
                ).scalar_one()
                == 0
            )
        assert (
            conn.execute(
                text("SELECT active_count FROM human_handoff_capacity WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            ).scalar_one_or_none()
            is None
        )
