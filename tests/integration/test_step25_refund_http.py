"""受控退款表单只接收报价/确认引用，认证身份和幂等键由服务端决定。"""

from datetime import datetime, timezone
from types import SimpleNamespace

from bootstrap.api_main import create_app
from bootstrap.settings import load_settings
from fastapi.testclient import TestClient

from application.common.authorization import verify_tool_context


class FakeQualification:
    def calculate_quote(self, auth, order_id, reason_code, lines):
        assert auth.subject_id == "customer-north-01"
        assert order_id == "ORDER-001" and reason_code == "changed_mind"
        assert lines[0].order_line_id == "LINE-001" and lines[0].quantity == 1
        quote = SimpleNamespace(
            quote_id="quote-test",
            order_id=order_id,
            amount_minor=10000,
            goods_minor=10000,
            shipping_minor=0,
            currency="CNY",
            reason_code=reason_code,
            eligibility="manual_review",
            policy_version="mock-v1",
            order_version="3",
            payment_version="mock-v2",
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            lines=(
                SimpleNamespace(
                    line_id="LINE-001", quantity=1, goods_minor=10000, shipping_minor=0
                ),
            ),
        )
        return SimpleNamespace(eligibility="manual_review", can_submit=True), quote


class FakeSubmission:
    def __init__(self):
        self.keys = []

    def authorized_quote(self, auth, quote_id):
        assert auth.subject_id == "customer-north-01" and quote_id == "quote-test"
        return SimpleNamespace(order_id="ORDER-001")

    def submit(self, auth, quote_id, confirmation_id, idempotency_key, trace):
        assert auth.subject_id == "customer-north-01"
        assert (quote_id, confirmation_id) == ("quote-test", "confirmation-test")
        assert trace.request_id and trace.trace_id
        self.keys.append(idempotency_key)
        return SimpleNamespace(
            refund_request_id="refund-test",
            status="pending_review",
            amount_minor=10000,
            currency="CNY",
            replayed=len(self.keys) > 1,
        )


class FakeConfirmation:
    def prepare(self, auth, quote_id):
        assert auth.subject_id == "customer-north-01" and quote_id == "quote-test"
        record = SimpleNamespace(
            confirmation_id="confirmation-test",
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        return SimpleNamespace(record=record, mock_challenge="local-mock-challenge")

    def verify(self, auth, confirmation_id, challenge):
        assert auth.subject_id == "customer-north-01"
        assert (confirmation_id, challenge) == ("confirmation-test", "local-mock-challenge")
        return SimpleNamespace(
            confirmation_id=confirmation_id,
            status="verified",
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )


class FakeQuery:
    def by_id(self, auth, request_id):
        assert auth.subject_id == "customer-north-01" and request_id == "refund-test"
        return self._record()

    def by_original_key(self, auth, key):
        assert auth.subject_id == "customer-north-01" and len(key) == 32
        return self._record()

    @staticmethod
    def _record():
        return SimpleNamespace(
            refund_request_id="refund-test",
            order_id="ORDER-001",
            review_status="pending_review",
            return_status="unknown",
            funds_status="unknown",
            amount_minor=10000,
            currency="CNY",
            revision=1,
            updated_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        )


def test_mock_refund_form_flow_keeps_idempotency_server_side() -> None:
    submission = FakeSubmission()
    app = create_app(
        settings=load_settings().model_copy(update={"app_env": "test"}),
        chat_service=object(),
        readiness=object(),
        refund_qualification=FakeQualification(),
        refund_submission=submission,
        refund_confirmation=FakeConfirmation(),
        refund_query=FakeQuery(),
    )
    with TestClient(app) as client:
        login = client.post("/auth/mock/session", json={"account_id": "cust-north-01"})
        assert login.status_code == 200
        token = login.json()["data"]["session_token"]
        headers = {"Authorization": f"Bearer {token}"}
        quote = client.post(
            "/orders/ORDER-001/refund-quotes",
            headers=headers,
            json={
                "schema_version": "1.0",
                "reason_code": "changed_mind",
                "lines": [{"order_line_id": "LINE-001", "quantity": 1}],
            },
        )
        assert quote.status_code == 201 and quote.json()["data"]["quote_id"] == "quote-test"
        prepared = client.post(
            "/orders/ORDER-001/refund-quotes/quote-test/confirm", headers=headers
        )
        assert prepared.status_code == 201
        assert prepared.json()["data"]["synthetic_only"] is True
        verified = client.post(
            "/refund-confirmations/confirmation-test/verify",
            headers=headers,
            json={"schema_version": "1.0", "challenge": "local-mock-challenge"},
        )
        assert verified.status_code == 200
        body = {
            "schema_version": "1.0",
            "quote_id": "quote-test",
            "confirmation_id": "confirmation-test",
        }
        assert (
            client.post(
                "/refund-requests",
                headers=headers,
                json={**body, "idempotency_key": "client-choice"},
            ).status_code
            == 400
        )
        first = client.post("/refund-requests", headers=headers, json=body)
        second = client.post("/refund-requests", headers=headers, json=body)
        assert first.status_code == 201 and second.status_code == 200
        assert first.json()["data"]["funds_status"] == "not_executed"
        assert submission.keys[0] == submission.keys[1]
        assert client.get("/refund-requests/refund-test", headers=headers).status_code == 200
        recovered = client.get(
            "/refund-requests/by-confirmation/confirmation-test", headers=headers
        )
        assert recovered.status_code == 200
        assert recovered.json()["data"]["funds_status"] == "unknown"


def test_refund_form_routes_are_absent_in_production() -> None:
    app = create_app(
        settings=load_settings().model_copy(update={"app_env": "production"}),
        chat_service=object(),
    )
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/refund-requests" not in paths
    assert "/orders/{order_id}/refund-quotes" not in paths
    assert "/workbench/refund-requests/{refund_request_id}/review" not in paths


def test_mock_staff_review_route_uses_trusted_staff_identity_and_idempotency() -> None:
    class FakeReviewer:
        def decide(
            self,
            auth,
            refund_request_id,
            *,
            expected_revision,
            approve,
            reason_code,
            idempotency_key,
            trace,
        ):
            verify_tool_context(auth, ("refund:review",))
            assert auth.subject_id == "supervisor-north-01"
            assert refund_request_id == "refund-test"
            assert (expected_revision, approve, reason_code) == (1, False, "INSPECTION_FAILED")
            assert idempotency_key == "review-key-01" and trace.trace_id
            return SimpleNamespace(
                refund_request_id=refund_request_id, status="rejected", revision=2
            )

    app = create_app(
        settings=load_settings().model_copy(update={"app_env": "test"}),
        chat_service=object(),
        readiness=object(),
        refund_reviewer=FakeReviewer(),
    )
    with TestClient(app) as client:
        customer_login = client.post("/auth/mock/session", json={"account_id": "cust-north-01"})
        customer_headers = {
            "Authorization": f"Bearer {customer_login.json()['data']['session_token']}",
            "Idempotency-Key": "review-key-01",
        }
        body = {
            "schema_version": "1.0",
            "decision": "rejected",
            "expected_revision": 1,
            "review_reason": "INSPECTION_FAILED",
        }
        path = "/workbench/refund-requests/refund-test/review"
        forbidden = client.post(path, headers=customer_headers, json=body)
        assert forbidden.status_code == 404
        missing_key = client.post(
            path, headers={"Authorization": customer_headers["Authorization"]}, json=body
        )
        assert missing_key.status_code == 400
        supervisor_login = client.post("/auth/mock/session", json={"account_id": "sup-north-01"})
        headers = {
            "Authorization": f"Bearer {supervisor_login.json()['data']['session_token']}",
            "Idempotency-Key": "review-key-01",
        }
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 200
        assert response.json()["data"] == {
            "refund_request_id": "refund-test",
            "review_status": "rejected",
            "revision": 2,
            "funds_status": "unknown",
        }
