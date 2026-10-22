"""FR-07.2 退款申请审核状态机；审核与资金执行严格分离。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

ReviewStatus = Literal["pending_review", "reviewing", "approved", "rejected"]
ReviewerKind = Literal["mock_consumer", "authorized_human"]


class ReviewConflict(ValueError):
    pass


@dataclass(frozen=True)
class RefundReview:
    refund_request_id: str
    status: ReviewStatus
    revision: int
    eligibility: str
    fulfillment_status: str
    reviewer_subject_id: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not self.refund_request_id or self.revision < 0:
            raise ValueError("退款审核标识或修订号无效")


def start_review(review: RefundReview, *, expected_revision: int) -> RefundReview:
    if review.revision != expected_revision or review.status != "pending_review":
        raise ReviewConflict("退款审核状态或版本冲突")
    return replace(review, status="reviewing", revision=review.revision + 1)


def decide_review(
    review: RefundReview,
    *,
    expected_revision: int,
    reviewer_kind: ReviewerKind,
    reviewer_subject_id: str,
    approve: bool,
    reason_code: str,
) -> RefundReview:
    if review.revision != expected_revision or review.status != "reviewing":
        raise ReviewConflict("退款审核状态或版本冲突")
    if not reviewer_subject_id or not reason_code:
        raise ValueError("审核主体和依据不能为空")
    if reviewer_kind not in ("mock_consumer", "authorized_human"):
        raise ValueError("审核主体类型不可信")
    if reviewer_kind == "mock_consumer":
        if not approve:
            raise ReviewConflict("自动审核不得凭猜测拒绝申请")
        if review.eligibility != "eligible" or review.fulfillment_status != "pending_shipment":
            raise ReviewConflict("自动审核仅可通过明确符合条件的未发货申请")
    return replace(
        review,
        status="approved" if approve else "rejected",
        revision=review.revision + 1,
        reviewer_subject_id=reviewer_subject_id,
        reason_code=reason_code,
    )
