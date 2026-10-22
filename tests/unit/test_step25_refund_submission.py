"""申请入口仅把可信报价与身份绑定到事务端口，不能由模型改金额。"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from application.common.authorization import AuthorizationDenied
from application.refund.qualification import RefundLineAllocation, RefundQuote
from application.refund.submission import RefundRequestResult, RefundSubmissionUseCase
from tests.unit.test_step10_agent_application import make_auth, make_trace


class Quotes:
    def __init__(self, quote):
        self.quote = quote

    def load(self, tenant_id, quote_id):
        if tenant_id == self.quote.tenant_id and quote_id == self.quote.quote_id:
            return self.quote
        return None


class Submission:
    def __init__(self):
        self.calls = []

    def commit(self, auth, quote, binding, confirmation_id, idempotency_key, trace):
        self.calls.append((auth, quote, binding, confirmation_id, idempotency_key, trace))
        return RefundRequestResult("refund-1", "pending_review", quote.amount_minor, quote.currency)


def _quote():
    return RefundQuote(
        quote_id="quote-1",
        tenant_id="tenant-north",
        order_id="ORDER-001",
        subject_id="customer-north-01",
        beneficiary_customer_id="CUSTOMER-01",
        reason_code="changed_mind",
        eligibility="eligible",
        amount_minor=11000,
        goods_minor=10000,
        shipping_minor=1000,
        currency="CNY",
        policy_id="MOCK-FR-07.1",
        policy_version="2026-10-v1",
        order_version="3",
        payment_version="mock-v2",
        expires_at=datetime(2026, 10, 21, tzinfo=timezone.utc) + timedelta(minutes=5),
        lines=(RefundLineAllocation("LINE-001", 1, 10000, 1000),),
    )


def _service(quote=None):
    submission = Submission()
    return RefundSubmissionUseCase(
        Quotes(quote or _quote()),
        submission,
        clock=lambda: datetime(2026, 10, 21, tzinfo=timezone.utc),
    ), submission


def test_binding_digest_changes_with_amount_and_submit_keeps_server_quote() -> None:
    auth = make_auth(scopes=("refund:request",))
    service, submission = _service()
    binding = service.binding_for_quote(auth, "quote-1")
    assert binding.expected_revision == 3
    assert binding.actor_subject_id == auth.subject_id
    assert binding.customer_id == "CUSTOMER-01"
    changed_service, _ = _service(replace(_quote(), amount_minor=12000))
    changed_binding = changed_service.binding_for_quote(auth, "quote-1")
    assert binding.payload_digest != changed_binding.payload_digest
    result = service.submit(auth, "quote-1", "confirm-1", "idem-1", make_trace())
    assert result.status == "pending_review"
    assert submission.calls[0][1].amount_minor == 11000
    assert submission.calls[0][2] == binding


def test_other_actor_and_expired_quote_never_enter_transaction() -> None:
    service, submission = _service()
    with pytest.raises(AuthorizationDenied):
        service.submit(
            make_auth("another-actor", scopes=("refund:request",)),
            "quote-1",
            "confirm-1",
            "idem-1",
            make_trace(),
        )
    expired = replace(_quote(), expires_at=datetime(2026, 10, 20, tzinfo=timezone.utc))
    service, submission = _service(expired)
    with pytest.raises(ValueError, match="过期"):
        service.submit(
            make_auth(scopes=("refund:request",)), "quote-1", "confirm-1", "idem-1", make_trace()
        )
    assert submission.calls == []
