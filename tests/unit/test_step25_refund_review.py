"""审核通过不等于退款已支付，自动审核只处理确定性 Mock 场景。"""

from dataclasses import replace

import pytest

from domain.refund.review import RefundReview, ReviewConflict, decide_review, start_review


def _review(**kwargs):
    review = RefundReview("refund-1", "pending_review", 0, "eligible", "pending_shipment")
    return replace(review, **kwargs)


def test_mock_consumer_may_approve_only_explicit_pre_shipment_case() -> None:
    started = start_review(_review(), expected_revision=0)
    assert started.status == "reviewing" and started.revision == 1
    approved = decide_review(
        started,
        expected_revision=1,
        reviewer_kind="mock_consumer",
        reviewer_subject_id="mock-review-service",
        approve=True,
        reason_code="MOCK_PRE_SHIPMENT_CLEAR",
    )
    assert approved.status == "approved"
    assert approved.reason_code == "MOCK_PRE_SHIPMENT_CLEAR"
    assert not hasattr(approved, "payment_status")
    with pytest.raises(ReviewConflict):
        decide_review(
            approved,
            expected_revision=2,
            reviewer_kind="mock_consumer",
            reviewer_subject_id="mock-review-service",
            approve=True,
            reason_code="REPLAY",
        )


@pytest.mark.parametrize(
    "eligibility,fulfillment",
    [
        ("manual_review", "pending_shipment"),
        ("eligible", "delivered"),
        ("manual_review", "delivered"),
    ],
)
def test_mock_consumer_cannot_approve_manual_or_fulfilled_case(eligibility, fulfillment) -> None:
    started = start_review(
        _review(eligibility=eligibility, fulfillment_status=fulfillment), expected_revision=0
    )
    with pytest.raises(ReviewConflict):
        decide_review(
            started,
            expected_revision=1,
            reviewer_kind="mock_consumer",
            reviewer_subject_id="mock-review-service",
            approve=True,
            reason_code="AUTO",
        )


def test_authorized_human_can_reject_with_reason_and_revision_guard() -> None:
    started = start_review(_review(eligibility="manual_review"), expected_revision=0)
    with pytest.raises(ReviewConflict):
        decide_review(
            started,
            expected_revision=0,
            reviewer_kind="authorized_human",
            reviewer_subject_id="support-1",
            approve=False,
            reason_code="INSPECTION_FAILED",
        )
    rejected = decide_review(
        started,
        expected_revision=1,
        reviewer_kind="authorized_human",
        reviewer_subject_id="support-1",
        approve=False,
        reason_code="INSPECTION_FAILED",
    )
    assert rejected.status == "rejected" and rejected.revision == 2
