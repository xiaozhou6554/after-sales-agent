"""Development-only unavailable-human ticket fallback interaction."""

import uuid
from types import SimpleNamespace

from bootstrap.api_main import create_app
from bootstrap.settings import load_settings
from fastapi.testclient import TestClient

from application.common.operation_confirmation import OperationConfirmationUseCase
from application.ticket.service import TicketApplicationService
from tests.unit.test_step27_ticket_application import FakeConfirmations, FakeTickets


class FakeIntentStore:
    def __init__(self):
        self.registered = []

    def register_create(self, auth, **kwargs):
        self.registered.append(("ticket:create", auth, kwargs))

    def register_update(self, auth, **kwargs):
        self.registered.append(("ticket:update", auth, kwargs))


HANDOFF_REQUEST_KEY = "fallback-request-1"
HANDOFF_REQUEST_REF = uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"tenant-north:customer-north-01:handoff:request:{HANDOFF_REQUEST_KEY}",
).hex
HANDOFF_ID = uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"tenant-north\x00customer-north-01\x00handoff\x00{HANDOFF_REQUEST_REF}",
).hex


def _headers(client):
    return {**_login(client), "Handoff-Request-Key": HANDOFF_REQUEST_KEY}


def _payload(customer_id="CUSTOMER-01"):
    return {
        "customer_id": customer_id,
        "title": "Device needs support",
        "description_masked": "Screen fails",
        "priority": "normal",
        "context": {
            "question_masked": "Screen fails",
            "summary": "Needs support",
            "failure_reason": "human_unavailable",
            "current_state": "unavailable",
            "next_action": "create ticket after confirmation",
        },
    }


def _client(*, status="unavailable", ticket_id=None, writer=None):
    confirmations = FakeConfirmations()

    class LinkedTickets(FakeTickets):
        def create_confirmed(self, *args, **kwargs):
            result = super().create_confirmed(*args, **kwargs)
            if kwargs.get("handoff_id") is not None:
                self.handoff_state["ticket_id"] = result.ticket_id
            return result

    tickets = writer or LinkedTickets(confirmations)
    flow = OperationConfirmationUseCase(confirmations, 300)
    handoff_state = {"status": status, "ticket_id": ticket_id}
    tickets.handoff_state = handoff_state

    def lookup(tenant_id, handoff_id):
        if handoff_id != HANDOFF_ID:
            return None
        return "CUSTOMER-01", SimpleNamespace(**handoff_state)

    intents = FakeIntentStore()
    app = create_app(
        settings=load_settings(),
        readiness=object(),
        chat_service=object(),
        ticket_commands=TicketApplicationService(tickets, tickets, confirmations),
        confirmation_use_case=flow,
        handoff_fallback_lookup=lookup,
        ticket_tool_intent=intents,
    )
    return TestClient(app), tickets, intents


def _login(client):
    response = client.post("/auth/mock/session", json={"account_id": "cust-north-01"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['session_token']}"}


def test_customer_confirmation_then_replay_has_one_real_ticket():
    client, tickets, intents = _client()
    with client:
        headers = _headers(client)
        path = f"/dev/handoffs/{HANDOFF_ID}/fallback"
        first = client.post(f"{path}/prepare", headers=headers, json=_payload())
        assert first.status_code == 201
        prepared = first.json()["data"]
        assert "ticket_id" not in prepared
        assert prepared["operation"] == "confirmation_prepared"
        assert not tickets.rows
        repeated_prepare = client.post(f"{path}/prepare", headers=headers, json=_payload())
        assert repeated_prepare.status_code == 201
        assert repeated_prepare.json()["data"]["operation_ref"] == prepared["operation_ref"]

        unverified = client.post(
            f"{path}/commit",
            headers=headers,
            json={
                **_payload(),
                "operation_ref": prepared["operation_ref"],
                "confirmation_id": prepared["confirmation_id"],
            },
        )
        assert unverified.status_code == 409
        assert not tickets.rows

        verified = client.post(
            f"/dev/ticket-confirmations/{prepared['confirmation_id']}/verify",
            headers=headers,
            json={"challenge": prepared["challenge"]},
        )
        assert verified.status_code == 200
        committed = {
            **_payload(),
            "operation_ref": prepared["operation_ref"],
            "confirmation_id": prepared["confirmation_id"],
        }
        first_commit = client.post(f"{path}/commit", headers=headers, json=committed)
        retry = client.post(f"{path}/commit", headers=headers, json=committed)
        assert first_commit.status_code == 201
        assert tickets.handoff_state["ticket_id"] == first_commit.json()["data"]["ticket_id"]
        assert retry.status_code == 200
        assert retry.json()["data"]["replayed"] is True
        assert first_commit.json()["data"]["ticket_id"] == retry.json()["data"]["ticket_id"]
        assert tickets.create_calls == len(tickets.rows) == 1
        assert client.post(f"{path}/prepare", headers=headers, json=_payload()).status_code == 409

        second_confirmation = repeated_prepare.json()["data"]
        assert (
            client.post(
                f"/dev/ticket-confirmations/{second_confirmation['confirmation_id']}/verify",
                headers=headers,
                json={"challenge": second_confirmation["challenge"]},
            ).status_code
            == 200
        )
        second_commit = client.post(
            f"{path}/commit",
            headers=headers,
            json={**committed, "confirmation_id": second_confirmation["confirmation_id"]},
        )
        assert second_commit.status_code == 200
        assert second_commit.json()["data"]["ticket_id"] == retry.json()["data"]["ticket_id"]
        assert tickets.create_calls == 1

        changed = client.post(
            f"{path}/commit",
            headers=headers,
            json={**committed, "title": "Different content"},
        )
        assert changed.status_code == 409
        assert tickets.create_calls == 1


def test_fallback_requires_unavailable_unlinked_handoff_and_matching_customer():
    for status, ticket_id in (
        ("queued", None),
        ("accepted", None),
        ("unavailable", "existing-ticket"),
    ):
        client, tickets, intents = _client(status=status, ticket_id=ticket_id)
        with client:
            response = client.post(
                f"/dev/handoffs/{HANDOFF_ID}/fallback/prepare",
                headers=_headers(client),
                json=_payload(),
            )
            assert response.status_code == 409
            assert not tickets.rows

    client, tickets, intents = _client()
    with client:
        headers = _headers(client)
        assert (
            client.post(
                "/dev/handoffs/unknown/fallback/prepare", headers=headers, json=_payload()
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/dev/handoffs/{HANDOFF_ID}/fallback/prepare",
                headers=headers,
                json=_payload("OTHER-CUSTOMER"),
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/dev/handoffs/{HANDOFF_ID}/fallback/prepare",
                headers={**headers, "Handoff-Request-Key": "different"},
                json=_payload(),
            ).status_code
            == 404
        )
        assert not tickets.rows


def test_write_failure_does_not_claim_success_or_return_ticket_id():
    confirmations = FakeConfirmations()

    class FailingTickets(FakeTickets):
        def create_confirmed(self, *args, **kwargs):
            raise RuntimeError("storage unavailable")

    tickets = FailingTickets(confirmations)
    flow = OperationConfirmationUseCase(confirmations, 300)
    app = create_app(
        settings=load_settings(),
        readiness=object(),
        chat_service=object(),
        ticket_commands=TicketApplicationService(tickets, tickets, confirmations),
        confirmation_use_case=flow,
        handoff_fallback_lookup=lambda tenant, handoff: (
            "CUSTOMER-01",
            SimpleNamespace(status="unavailable", ticket_id=None),
        ),
        ticket_tool_intent=FakeIntentStore(),
    )
    with TestClient(app) as client:
        headers = _headers(client)
        path = f"/dev/handoffs/{HANDOFF_ID}/fallback"
        prepared = client.post(f"{path}/prepare", headers=headers, json=_payload()).json()["data"]
        client.post(
            f"/dev/ticket-confirmations/{prepared['confirmation_id']}/verify",
            headers=headers,
            json={"challenge": prepared["challenge"]},
        )
        response = client.post(
            f"{path}/commit",
            headers=headers,
            json={
                **_payload(),
                "operation_ref": prepared["operation_ref"],
                "confirmation_id": prepared["confirmation_id"],
            },
        )
        assert response.status_code == 503
        assert response.json()["error"]["error_code"] == "DEPENDENCY_UNAVAILABLE"
        assert "ticket_id" not in response.json().get("data", {})
        assert not tickets.rows
