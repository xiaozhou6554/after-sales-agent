"""Controlled Step27 HTTP surface and confirmation binding."""

from bootstrap.api_main import create_app
from bootstrap.settings import load_settings
from fastapi.testclient import TestClient

from application.ticket.handoff import HandoffConflict


class FakeConfirmation:
    def __init__(self, binding, confirmation_id="confirmation-1", status="prepared"):
        self.binding = binding
        self.confirmation_id = confirmation_id
        self.status = status
        from datetime import datetime, timezone

        self.expires_at = datetime.now(timezone.utc)


class FakeFlow:
    def __init__(self):
        self.record = None

    def get(self, tenant, confirmation_id):
        return (
            self.record if self.record and confirmation_id == self.record.confirmation_id else None
        )

    def verify(self, tenant, confirmation_id, challenge):
        assert challenge == "challenge-1"
        self.record.status = "verified"
        return self.record


class FakeTickets:
    def __init__(self):
        self._confirmation_flow = FakeFlow()
        self.calls = []

    def prepare_create(self, auth, **kwargs):
        from types import SimpleNamespace

        self.calls.append(("prepare", auth, kwargs))
        binding = SimpleNamespace(actor_subject_id=auth.subject_id, operation_type="ticket:create")
        record = FakeConfirmation(binding)
        self._confirmation_flow.record = record
        return SimpleNamespace(
            ticket_id="ticket-1",
            confirmation=SimpleNamespace(record=record, mock_challenge="challenge-1"),
        )

    def create(self, auth, **kwargs):
        from types import SimpleNamespace

        self.calls.append(("create", auth, kwargs))
        return SimpleNamespace(ticket_id="ticket-1", revision=1, replayed=False)


class FakeIntentStore:
    def __init__(self):
        self.registered = []

    def register_create(self, auth, **kwargs):
        self.registered.append(("ticket:create", auth, kwargs))

    def register_update(self, auth, **kwargs):
        self.registered.append(("ticket:update", auth, kwargs))


class FakeHandoff:
    def __init__(self):
        self.calls = {}

    def request(self, auth, trace, **kwargs):
        from types import SimpleNamespace

        key = kwargs["idempotency_key"]
        content = (kwargs["customer_id"], kwargs["reason_code"], kwargs["context"])
        if key in self.calls and self.calls[key] != content:
            raise HandoffConflict("same key changed content")
        replayed = key in self.calls
        self.calls[key] = content
        return SimpleNamespace(
            handoff_id="handoff-1",
            status="queued",
            revision=1,
            ticket_id=None,
            assigned_to=None,
            replayed=replayed,
        )


def _app(**kwargs):
    return create_app(settings=load_settings(), readiness=object(), chat_service=object(), **kwargs)


def _login(client):
    response = client.post("/auth/mock/session", json={"account_id": "cust-north-01"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['session_token']}"}


def test_prepare_verify_create_uses_trusted_actor_and_server_operation():
    tickets = FakeTickets()
    intents = FakeIntentStore()
    with TestClient(
        _app(
            ticket_commands=tickets,
            confirmation_use_case=tickets._confirmation_flow,
            ticket_tool_intent=intents,
        )
    ) as client:
        headers = _login(client)
        payload = {
            "customer_id": "CUSTOMER-01",
            "title": "Broken device",
            "description_masked": "Screen fails",
            "priority": "normal",
            "context": {
                "question_masked": "Screen fails",
                "summary": "Needs review",
                "current_state": "open",
                "next_action": "repair",
            },
        }
        assert client.post("/dev/tickets/prepare-create", json=payload).status_code == 401
        assert (
            client.post(
                "/dev/tickets/prepare-create",
                headers=headers,
                json={**payload, "tenant_id": "other"},
            ).status_code
            == 400
        )
        prepared = client.post("/dev/tickets/prepare-create", headers=headers, json=payload)
        assert prepared.status_code == 201
        data = prepared.json()["data"]
        assert tickets.calls[0][1].subject_id == "customer-north-01"
        verified = client.post(
            "/dev/ticket-confirmations/confirmation-1/verify",
            headers=headers,
            json={"challenge": data["challenge"]},
        )
        assert verified.status_code == 200
        created = client.post(
            "/dev/tickets/create",
            headers=headers,
            json={
                **payload,
                "operation_ref": data["operation_ref"],
                "confirmation_id": data["confirmation_id"],
            },
        )
        assert created.status_code == 201
        assert tickets.calls[-1][2]["idempotency_key"] == tickets.calls[0][2]["idempotency_key"]
        assert tickets.calls[-1][2]["trace"] == tickets.calls[0][2]["trace"]
        assert len(intents.registered) == 1
        operation, actor, registered = intents.registered[0]
        assert operation == "ticket:create"
        assert actor.subject_id == "customer-north-01"
        assert registered["confirmation_id"] == data["confirmation_id"]
        assert registered["idempotency_key"] == tickets.calls[-1][2]["idempotency_key"]


def test_unavailable_and_production_route_absent():
    with TestClient(_app()) as client:
        headers = _login(client)
        response = client.post(
            "/dev/handoffs/request",
            headers={**headers, "Idempotency-Key": "retry-1"},
            json={
                "customer_id": "CUSTOMER-01",
                "reason_code": "UNRESOLVED",
                "context": {
                    "question_masked": "help",
                    "summary": "help",
                    "current_state": "open",
                    "next_action": "human",
                },
            },
        )
        assert response.status_code == 503
        assert response.json()["error"]["error_code"] == "DEPENDENCY_UNAVAILABLE"

    settings = load_settings().model_copy(update={"app_env": "production"})
    production = create_app(settings=settings, readiness=object(), chat_service=object())
    assert all(not getattr(route, "path", "").startswith("/dev/") for route in production.routes)


def test_handoff_retry_replays_and_changed_content_conflicts():
    handoff = FakeHandoff()
    with TestClient(_app(human_handoff=handoff)) as client:
        headers = {**_login(client), "Idempotency-Key": "handoff-retry-1"}
        payload = {
            "customer_id": "CUSTOMER-01",
            "reason_code": "UNRESOLVED",
            "context": {
                "question_masked": "help",
                "summary": "help",
                "current_state": "open",
                "next_action": "human",
            },
        }
        assert (
            client.post(
                "/dev/handoffs/request",
                headers={k: v for k, v in headers.items() if k != "Idempotency-Key"},
                json=payload,
            ).status_code
            == 400
        )
        first = client.post("/dev/handoffs/request", headers=headers, json=payload)
        again = client.post("/dev/handoffs/request", headers=headers, json=payload)
        assert first.status_code == again.status_code == 201
        assert first.json()["data"]["handoff_id"] == again.json()["data"]["handoff_id"]
        assert again.json()["data"]["replayed"] is True
        changed = client.post(
            "/dev/handoffs/request", headers=headers, json={**payload, "reason_code": "DIFFERENT"}
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["error_code"] == "STATE_CONFLICT"
