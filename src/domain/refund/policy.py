"""FR-07.1 Mock 规则：只依据可信快照评估，不调用模型、网络或数据库。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

Eligibility = Literal["eligible", "manual_review", "ineligible", "unknown"]


@dataclass(frozen=True)
class RefundFacts:
    order_id: str
    reason_code: str | None
    reason_category: str | None
    currency: str
    payment_status: str
    fulfillment_status: str
    fulfillment_locked: bool
    status_conflict: bool
    paid_goods_minor: int | None
    paid_shipping_minor: int | None
    paid_total_minor: int | None
    refunded_goods_minor: int | None
    refunded_shipping_minor: int | None
    reserved_goods_minor: int | None
    reserved_shipping_minor: int | None
    active_request_id: str | None
    policy_id: str | None
    policy_version: str | None
    policy_valid: bool
    manual_submission_allowed: bool
    order_version: str
    payment_version: str
    delivered_on: date | None
    as_of: date


@dataclass(frozen=True)
class RefundAssessment:
    eligibility: Eligibility
    can_submit: bool
    reason_code: str
    amount_minor: int | None
    goods_minor: int | None
    shipping_minor: int | None
    currency: str
    policy_id: str | None
    policy_version: str | None
    order_version: str
    payment_version: str
    active_request_id: str | None = None
    missing_fields: tuple[str, ...] = ()


def _result(
    facts: RefundFacts,
    eligibility: Eligibility,
    code: str,
    *,
    can_submit: bool = False,
    goods: int | None = None,
    shipping: int | None = None,
    missing: tuple[str, ...] = (),
) -> RefundAssessment:
    amount = goods + shipping if goods is not None and shipping is not None else None
    return RefundAssessment(
        eligibility,
        can_submit,
        code,
        amount,
        goods,
        shipping,
        facts.currency,
        facts.policy_id,
        facts.policy_version,
        facts.order_version,
        facts.payment_version,
        facts.active_request_id,
        missing,
    )


def assess_refund(facts: RefundFacts) -> RefundAssessment:
    if not facts.order_id or not facts.order_version or not facts.payment_version:
        return _result(facts, "unknown", "SOURCE_VERSION_MISSING")
    if not facts.reason_code or not facts.reason_category:
        return _result(facts, "unknown", "REASON_REQUIRED", missing=("reason",))
    if facts.reason_category not in ("ordinary_return", "quality_dispute"):
        return _result(facts, "unknown", "REASON_UNRECOGNIZED", missing=("reason",))
    if not facts.policy_id or not facts.policy_version or not facts.policy_valid:
        return _result(facts, "unknown", "REFUND_POLICY_UNAVAILABLE")
    if facts.currency != "CNY":
        return _result(facts, "unknown", "CURRENCY_UNSUPPORTED")
    if facts.payment_status in ("unpaid", "failed"):
        return _result(facts, "ineligible", "NOT_PAID")
    if facts.payment_status != "succeeded":
        return _result(facts, "unknown", "PAYMENT_UNVERIFIED")
    if facts.active_request_id:
        return _result(facts, "ineligible", "ACTIVE_REQUEST_EXISTS")
    amounts = (
        facts.paid_goods_minor,
        facts.paid_shipping_minor,
        facts.paid_total_minor,
        facts.refunded_goods_minor,
        facts.refunded_shipping_minor,
        facts.reserved_goods_minor,
        facts.reserved_shipping_minor,
    )
    if any(type(value) is not int or value < 0 for value in amounts):
        return _result(facts, "unknown", "AMOUNT_INCONSISTENT")
    assert all(isinstance(value, int) for value in amounts)
    (
        paid_goods,
        paid_ship,
        paid_total,
        refunded_goods,
        refunded_ship,
        reserved_goods,
        reserved_ship,
    ) = amounts
    if paid_goods + paid_ship != paid_total:
        return _result(facts, "unknown", "AMOUNT_INCONSISTENT")
    goods = paid_goods - refunded_goods - reserved_goods
    shipping = paid_ship - refunded_ship - reserved_ship
    if goods < 0 or shipping < 0:
        return _result(facts, "unknown", "AMOUNT_INCONSISTENT")
    if goods + shipping == 0:
        return _result(facts, "ineligible", "NO_REFUNDABLE_BALANCE")
    if facts.status_conflict:
        return _result(facts, "unknown", "SOURCE_STATUS_CONFLICT")
    if facts.reason_category == "quality_dispute":
        return _result(facts, "manual_review", "QUALITY_DISPUTE_REVIEW")
    if facts.fulfillment_status == "pending_shipment" and not facts.fulfillment_locked:
        return _result(
            facts,
            "eligible",
            "PRE_SHIPMENT_ELIGIBLE",
            can_submit=True,
            goods=goods,
            shipping=shipping,
        )
    if facts.fulfillment_status == "delivered":
        if facts.delivered_on is None:
            return _result(facts, "unknown", "DELIVERY_TIME_MISSING")
        elapsed_days = (facts.as_of - facts.delivered_on).days
        if elapsed_days < 0:
            return _result(facts, "unknown", "DELIVERY_TIME_CONFLICT")
        if elapsed_days > 7:
            return _result(facts, "manual_review", "RETURN_WINDOW_NEEDS_REVIEW")
        if goods <= 0:
            return _result(facts, "ineligible", "NO_REFUNDABLE_GOODS")
        return _result(
            facts,
            "manual_review",
            "DELIVERED_REVIEW",
            can_submit=facts.manual_submission_allowed,
            goods=goods if facts.manual_submission_allowed else None,
            shipping=0 if facts.manual_submission_allowed else None,
        )
    if facts.fulfillment_status in ("shipped", "pending_shipment"):
        if goods <= 0:
            return _result(facts, "ineligible", "NO_REFUNDABLE_GOODS")
        return _result(
            facts,
            "manual_review",
            "FULFILLMENT_REVIEW",
            can_submit=facts.manual_submission_allowed,
            goods=goods if facts.manual_submission_allowed else None,
            shipping=0 if facts.manual_submission_allowed else None,
        )
    return _result(facts, "unknown", "FULFILLMENT_UNVERIFIED")
