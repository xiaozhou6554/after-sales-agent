"""只读资格/报价：服务端身份与单一可信快照，未确认不占用余额。"""

from datetime import datetime, timezone

import pytest

from application.common.authorization import AuthorizationDenied
from application.refund.qualification import (
    RefundLineAllocation,
    RefundLineSelection,
    RefundQualificationUseCase,
    RefundSnapshot,
)
from tests.unit.test_step10_agent_application import make_auth
from tests.unit.test_step25_refund_policy import _facts


class Snapshots:
    def __init__(self, owner="CUSTOMER-01"):
        self.owner = owner
        self.calls = []

    def load(self, tenant_id, order_id):
        self.calls.append((tenant_id, order_id))
        return RefundSnapshot(
            tenant_id,
            self.owner,
            _facts(),
            (RefundLineAllocation("LINE-001", 1, 10000, 1000),),
        )


class Quotes:
    def __init__(self):
        self.saved = []

    def save(self, quote):
        self.saved.append(quote)


def _use_case(snapshots, quotes):
    return RefundQualificationUseCase(
        snapshots,
        quotes,
        clock=lambda: datetime(2026, 10, 21, tzinfo=timezone.utc),
        new_id=lambda: "fixed",
    )


def test_check_policy_does_not_create_quote_or_reserve() -> None:
    snapshots, quotes = Snapshots(), Quotes()
    result = _use_case(snapshots, quotes).check_policy(
        make_auth(scopes=("refund:read",)), "ORDER-001", "no_longer_needed"
    )
    assert result.eligibility == "eligible" and result.amount_minor == 11000
    assert quotes.saved == []
    assert snapshots.calls == [("tenant-north", "ORDER-001")]


def test_quote_uses_one_snapshot_and_server_chosen_amount_and_expiry() -> None:
    snapshots, quotes = Snapshots(), Quotes()
    assessment, quote = _use_case(snapshots, quotes).calculate_quote(
        make_auth(scopes=("refund:read",)), "ORDER-001", "no_longer_needed"
    )
    assert assessment.can_submit and quote is not None
    assert quote.quote_id == "quote-fixed"
    assert (quote.goods_minor, quote.shipping_minor, quote.amount_minor) == (10000, 1000, 11000)
    assert quote.expires_at == datetime(2026, 10, 21, 0, 5, tzinfo=timezone.utc)
    assert quote.order_version == "order-v3" and quote.payment_version == "payment-v2"
    assert quotes.saved == [quote]
    assert snapshots.calls == [("tenant-north", "ORDER-001")]


def test_missing_reason_and_unauthorized_customer_never_create_quote() -> None:
    quotes = Quotes()
    assessment, quote = _use_case(Snapshots(), quotes).calculate_quote(
        make_auth(scopes=("refund:read",)), "ORDER-001", None
    )
    assert assessment.reason_code == "REASON_REQUIRED" and quote is None
    with pytest.raises(AuthorizationDenied) as exc:
        _use_case(Snapshots("CUSTOMER-OTHER"), quotes).calculate_quote(
            make_auth(scopes=("refund:read",)), "ORDER-001", "no_longer_needed"
        )
    assert exc.value.error_code.value == "RESOURCE_UNAVAILABLE"
    assert quotes.saved == []


def test_missing_or_mismatched_line_allocation_fails_closed() -> None:
    class NoLines(Snapshots):
        def load(self, tenant_id, order_id):
            return RefundSnapshot(tenant_id, self.owner, _facts())

    quotes = Quotes()
    assessment, quote = _use_case(NoLines(), quotes).calculate_quote(
        make_auth(scopes=("refund:read",)), "ORDER-001", "changed_mind"
    )
    assert assessment.eligibility == "unknown"
    assert assessment.reason_code == "LINE_ALLOCATION_INCONSISTENT"
    assert quote is None and quotes.saved == []


def test_full_line_subset_quotes_partial_amount_and_partial_quantity_fails_closed() -> None:
    class TwoLines(Snapshots):
        def load(self, tenant_id, order_id):
            return RefundSnapshot(
                tenant_id,
                self.owner,
                _facts(),
                (
                    RefundLineAllocation("LINE-A", 2, 6000, 600),
                    RefundLineAllocation("LINE-B", 1, 4000, 400),
                ),
            )

    quotes = Quotes()
    service = _use_case(TwoLines(), quotes)
    auth = make_auth(scopes=("refund:read",))
    assessment, quote = service.calculate_quote(
        auth, "ORDER-001", "changed_mind", (RefundLineSelection("LINE-A", 2),)
    )
    assert assessment.can_submit and quote is not None
    assert (quote.goods_minor, quote.shipping_minor, quote.amount_minor) == (6000, 600, 6600)
    assert quote.lines == (RefundLineAllocation("LINE-A", 2, 6000, 600),)
    unsupported, absent = service.calculate_quote(
        auth, "ORDER-001", "changed_mind", (RefundLineSelection("LINE-A", 1),)
    )
    assert unsupported.reason_code == "LINE_SELECTION_UNSUPPORTED"
    assert absent is None and quotes.saved == [quote]
