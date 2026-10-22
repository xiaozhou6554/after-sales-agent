"""FR-07.1 / TC-69～71：可信快照资格矩阵与整数分组成。"""

from dataclasses import replace
from datetime import date

from domain.refund import RefundFacts, assess_refund


def _facts(**changes) -> RefundFacts:
    baseline = RefundFacts(
        order_id="ORDER-001",
        reason_code="no_longer_needed",
        reason_category="ordinary_return",
        currency="CNY",
        payment_status="succeeded",
        fulfillment_status="pending_shipment",
        fulfillment_locked=False,
        status_conflict=False,
        paid_goods_minor=10000,
        paid_shipping_minor=1000,
        paid_total_minor=11000,
        refunded_goods_minor=0,
        refunded_shipping_minor=0,
        reserved_goods_minor=0,
        reserved_shipping_minor=0,
        active_request_id=None,
        policy_id="POL-RETURN-01",
        policy_version="2",
        policy_valid=True,
        manual_submission_allowed=True,
        order_version="order-v3",
        payment_version="payment-v2",
        delivered_on=None,
        as_of=date(2026, 10, 21),
    )
    return replace(baseline, **changes)


def test_tc69_unfulfilled_paid_order_includes_remaining_shipping() -> None:
    result = assess_refund(_facts())
    assert (result.eligibility, result.can_submit, result.amount_minor) == ("eligible", True, 11000)
    assert (result.goods_minor, result.shipping_minor) == (10000, 1000)


def test_tc70_delivered_partial_refund_excludes_original_shipping() -> None:
    result = assess_refund(
        _facts(
            fulfillment_status="delivered",
            delivered_on=date(2026, 10, 19),
            refunded_goods_minor=2000,
        )
    )
    assert (result.eligibility, result.can_submit, result.amount_minor) == (
        "manual_review",
        True,
        8000,
    )
    assert result.shipping_minor == 0


def test_tc71_missing_reason_unpaid_and_fully_refunded_do_not_quote() -> None:
    assert assess_refund(_facts(reason_code=None)).reason_code == "REASON_REQUIRED"
    assert assess_refund(_facts(payment_status="unpaid")).eligibility == "ineligible"
    full = assess_refund(
        _facts(
            refunded_goods_minor=10000,
            refunded_shipping_minor=1000,
        )
    )
    assert full.eligibility == "ineligible" and full.amount_minor is None


def test_active_request_and_unverified_facts_fail_closed() -> None:
    assert assess_refund(_facts(active_request_id="REFUND-1")).reason_code == (
        "ACTIVE_REQUEST_EXISTS"
    )
    for change in (
        {"currency": "USD"},
        {"policy_valid": False},
        {"paid_total_minor": 9999},
        {"reserved_goods_minor": 11000},
        {"paid_goods_minor": True},
        {"payment_status": "unknown"},
        {"status_conflict": True},
    ):
        result = assess_refund(_facts(**change))
        assert not result.can_submit and result.amount_minor is None


def test_manual_paths_never_infer_approval_or_shipping() -> None:
    shipped = assess_refund(
        _facts(
            fulfillment_status="shipped",
            manual_submission_allowed=True,
        )
    )
    assert shipped.eligibility == "manual_review" and shipped.amount_minor == 10000
    disallowed = assess_refund(
        _facts(
            fulfillment_status="shipped",
            manual_submission_allowed=False,
        )
    )
    assert not disallowed.can_submit and disallowed.amount_minor is None
    late = assess_refund(
        _facts(
            fulfillment_status="delivered",
            delivered_on=date(2026, 10, 1),
        )
    )
    assert late.eligibility == "manual_review" and not late.can_submit
    quality = assess_refund(_facts(reason_category="quality_dispute"))
    assert quality.eligibility == "manual_review" and not quality.can_submit
