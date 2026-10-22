from datetime import datetime, timedelta, timezone

import pytest

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, DataScope
from application.common.operation_confirmation import PreparedConfirmation
from application.common.ports.confirmation import ConfirmationRecord
from application.workbench.commands import WorkbenchTicketCommands
from application.workbench.queries import WorkbenchPage, WorkbenchQueries


def _auth(
    *,
    subject="support-1",
    scopes=("customer:read", "ticket:read", "handoff:read", "refund:review"),
    customers=("CUSTOMER-01", "CUSTOMER-02"),
    roles=("support",),
    session="session-1",
):
    now = datetime.now(timezone.utc)
    return AuthContext(
        "tenant-north",
        subject,
        roles,
        scopes,
        DataScope(customers, ("team-north",)),
        session,
        now,
        now + timedelta(hours=1),
    )


class Reads:
    def customers(self, tenant, customer_ids, query, offset, limit):
        return WorkbenchPage(
            [
                {"customer_id": item, "display_name_masked": "masked"}
                for item in customer_ids[offset : offset + limit]
            ],
            None,
        )

    def tickets(self, tenant, customer_ids, filters, offset, limit):
        return WorkbenchPage([{"ticket_id": "ticket-1", "customer_id": "CUSTOMER-01"}], None)

    def handoffs(self, tenant, customer_ids, status, offset, limit):
        return WorkbenchPage([], None)

    def refunds(self, tenant, customer_ids, status, customer_id, offset, limit):
        return WorkbenchPage([], None)

    def ticket_detail(self, tenant, ticket_id):
        return {
            "ticket_id": ticket_id,
            "customer_id": "CUSTOMER-01",
            "messages": [{"visibility": "internal"}],
        }


def test_customer_scope_and_signed_cursor_are_bound_to_filters_and_session():
    queries = WorkbenchQueries(Reads())
    first = queries.customers(_auth(), limit=1)
    assert first["items"][0]["customer_id"] == "CUSTOMER-01"
    assert first["next_cursor"]
    assert (
        queries.customers(_auth(), limit=1, cursor=first["next_cursor"])["items"][0]["customer_id"]
        == "CUSTOMER-02"
    )
    with pytest.raises(AuthorizationDenied):
        queries.customers(_auth(session="another-session"), limit=1, cursor=first["next_cursor"])
    with pytest.raises(AuthorizationDenied):
        queries.tickets(_auth(), customer_id="CUSTOMER-03")


def test_customer_detail_is_authorized_and_staff_projection_keeps_visibility():
    result = WorkbenchQueries(Reads()).ticket_detail(_auth(), "ticket-1")
    assert result["messages"][0]["visibility"] == "internal"
    with pytest.raises(AuthorizationDenied):
        WorkbenchQueries(Reads()).ticket_detail(_auth(customers=("CUSTOMER-02",)), "ticket-1")


def test_admin_does_not_inherit_customer_or_ticket_business_scope():
    admin = _auth(scopes=("audit:read",), customers=("*",), roles=("admin",))
    queries = WorkbenchQueries(Reads())
    with pytest.raises(AuthorizationDenied):
        queries.tickets(admin)
    with pytest.raises(AuthorizationDenied):
        queries.refunds(admin)


def test_customer_role_cannot_enter_workbench_even_with_customer_ticket_read_scope():
    customer = _auth(
        subject="customer-1",
        scopes=("ticket:read", "customer:read"),
        customers=("CUSTOMER-01",),
        roles=("customer",),
    )
    queries = WorkbenchQueries(Reads())
    with pytest.raises(AuthorizationDenied):
        queries.tickets(customer)
    with pytest.raises(AuthorizationDenied):
        queries.ticket_detail(customer, "ticket-1")


class Confirmations:
    def __init__(self):
        self.record = None

    def prepare(self, binding):
        self.record = ConfirmationRecord(
            "confirm-1",
            binding,
            "prepared",
            datetime.now(timezone.utc) + timedelta(minutes=5),
            None,
        )
        return PreparedConfirmation(self.record, "mock-code")

    def get(self, tenant_id, confirmation_id):
        return self.record

    def verify(self, tenant_id, confirmation_id, challenge):
        return self.record


class Staff:
    def belongs_to_any_team(self, tenant_id, subject_id, team_ids):
        return subject_id == "agent-1" and "team-north" in team_ids


class Writes:
    def apply(self, detail, **kwargs):
        self.detail = detail
        self.kwargs = kwargs
        return type("WriteResult", (), {"revision": 3, "replayed": False})()


def test_staff_ticket_write_is_content_version_and_team_bound():
    auth = _auth(scopes=("ticket:read", "ticket:assign", "ticket:status"))
    confirmations = Confirmations()
    writes = Writes()
    service = WorkbenchTicketCommands(WorkbenchQueries(Reads()), confirmations, writes, Staff())
    prepared = service.prepare_assignment(auth, "ticket-1", "agent-1", 2)
    assert prepared.record.binding.operation_type == "assign"
    assert prepared.record.binding.target_id == "ticket-1"
    assert prepared.record.binding.expected_revision == 2
    assert prepared.record.binding.payload_digest
    with pytest.raises(AuthorizationDenied):
        service.prepare_assignment(auth, "ticket-1", "agent-other", 2)

    service.verify(auth, "confirm-1", "mock-code")
    result = service.assign(auth, "ticket-1", "agent-1", 2, "confirm-1", "same-key")
    assert result["revision"] == 3
    assert writes.kwargs["binding"] == prepared.record.binding
