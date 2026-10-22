"""Handoff request and claim authorization without a database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from migrations.versions.v0016_handoff_queue import statements

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, DataScope, TraceContext
from application.ticket.handoff import HandoffResult, MockHumanHandoff
from application.ticket.service import TicketContextSnapshot


class Store:
    def __init__(self):
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return HandoffResult(kwargs["handoff_id"], "queued", 1, kwargs["ticket_id"], None)

    def get(self, tenant_id, handoff_id):
        return "customer-1", HandoffResult(handoff_id, "queued", 1, None, None)

    def claim(self, **kwargs):
        self.calls.append(kwargs)
        return HandoffResult(
            kwargs["handoff_id"], "accepted", 2, kwargs["ticket_id"], kwargs["actor_id"]
        )


def auth(*, agent=False, customers=("customer-1",)):
    now = datetime.now(timezone.utc)
    return AuthContext(
        "tenant",
        "human-1" if agent else "customer-1",
        ("human_agent",) if agent else ("customer",),
        ("handoff:accept",) if agent else ("ticket:create",),
        DataScope(customer_ids=customers),
        "session",
        now - timedelta(minutes=1),
        now + timedelta(hours=1),
    )


def test_request_bounded_context_and_stable_identity():
    store = Store()
    service = MockHumanHandoff(store, capacity=2)
    trace = TraceContext("1.0", "req", "corr", "trace", "thread")
    context = TicketContextSnapshot("masked", "summary", (), (), "failed", (), "open", "review")
    first = service.request(
        auth(),
        trace,
        customer_id="customer-1",
        reason_code="failed",
        context=context,
        idempotency_key="key",
        available=True,
    )
    second = service.request(
        auth(),
        trace,
        customer_id="customer-1",
        reason_code="failed",
        context=context,
        idempotency_key="key",
        available=False,
    )
    assert first.handoff_id == second.handoff_id
    assert store.calls[0]["capacity"] == 2
    assert store.calls[0]["context_snapshot"]["failure_reason"] == "failed"
    with pytest.raises(ValueError):
        service.request(
            auth(),
            trace,
            customer_id="customer-1",
            reason_code="failed",
            context=context,
            idempotency_key="",
            available=True,
        )
    with pytest.raises(AuthorizationDenied):
        service.request(
            auth(customers=()),
            trace,
            customer_id="customer-1",
            reason_code="failed",
            context=context,
            idempotency_key="key",
            available=True,
        )


def test_claim_requires_authorized_human_and_positive_revision():
    store = Store()
    service = MockHumanHandoff(store)
    with pytest.raises(AuthorizationDenied):
        service.claim(auth(), handoff_id="h", expected_revision=1, idempotency_key="key")
    with pytest.raises(AuthorizationDenied):
        service.claim(
            auth(agent=True, customers=()),
            handoff_id="h",
            expected_revision=1,
            idempotency_key="key",
        )
    with pytest.raises(ValueError):
        service.claim(
            auth(agent=True), handoff_id="h", expected_revision=True, idempotency_key="key"
        )
    assert (
        service.claim(
            auth(agent=True), handoff_id="h", expected_revision=1, idempotency_key="key"
        ).status
        == "accepted"
    )


def test_migration_uses_tenant_scoped_capacity_and_idempotency():
    sql = " ".join(statements())
    assert "PRIMARY KEY (tenant_id)" in sql
    assert "PRIMARY KEY (tenant_id, requested_by, idempotency_key)" in sql
