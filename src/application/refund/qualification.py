"""读取可信订单/支付/政策快照后确定性判断资格；报价不占用余额。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext
from application.common.errors import ErrorCode
from domain.refund import RefundAssessment, RefundFacts, assess_refund

REASON_CATEGORIES = {
    "no_longer_needed": "ordinary_return",
    "changed_mind": "ordinary_return",
    "quality_issue": "quality_dispute",
}


@dataclass(frozen=True)
class RefundSnapshot:
    tenant_id: str
    customer_id: str
    facts: RefundFacts
    lines: tuple["RefundLineAllocation", ...] = ()


@dataclass(frozen=True)
class RefundLineAllocation:
    line_id: str
    quantity: int
    goods_minor: int
    shipping_minor: int


@dataclass(frozen=True)
class RefundLineSelection:
    order_line_id: str
    quantity: int


class RefundSnapshotPort(Protocol):
    def load(self, tenant_id: str, order_id: str) -> RefundSnapshot | None: ...


@dataclass(frozen=True)
class RefundQuote:
    quote_id: str
    tenant_id: str
    order_id: str
    subject_id: str
    beneficiary_customer_id: str
    reason_code: str
    eligibility: str
    amount_minor: int
    goods_minor: int
    shipping_minor: int
    currency: str
    policy_id: str
    policy_version: str
    order_version: str
    payment_version: str
    expires_at: datetime
    lines: tuple[RefundLineAllocation, ...] = ()


class RefundQuotePort(Protocol):
    def save(self, quote: RefundQuote) -> None: ...


class RefundQualificationUseCase:
    def __init__(
        self,
        snapshots: RefundSnapshotPort,
        quotes: RefundQuotePort,
        *,
        ttl_seconds: int = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("报价有效期必须大于零")
        self._snapshots = snapshots
        self._quotes = quotes
        self._ttl = ttl_seconds
        self._clock = clock
        self._new_id = new_id

    def check_policy(
        self, auth: AuthContext, order_id: str, reason_code: str | None
    ) -> RefundAssessment:
        _, assessment = self._assess_snapshot(auth, order_id, reason_code)
        return assessment

    def _assess_snapshot(
        self, auth: AuthContext, order_id: str, reason_code: str | None
    ) -> tuple[RefundSnapshot, RefundAssessment]:
        verify_tool_context(auth, ("refund:read",))
        if not order_id:
            raise ValueError("订单标识不能为空")
        snapshot = self._snapshots.load(auth.tenant_id, order_id)
        if (
            snapshot is None
            or snapshot.tenant_id != auth.tenant_id
            or snapshot.facts.order_id != order_id
        ):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        require_customer_access(auth, snapshot.customer_id, tenant_id=snapshot.tenant_id)
        facts = replace(
            snapshot.facts,
            reason_code=reason_code,
            reason_category=REASON_CATEGORIES.get(reason_code or ""),
        )
        return snapshot, assess_refund(facts)

    def calculate_quote(
        self,
        auth: AuthContext,
        order_id: str,
        reason_code: str | None,
        lines: tuple[RefundLineSelection, ...] | None = None,
    ) -> tuple[RefundAssessment, RefundQuote | None]:
        snapshot, assessment = self._assess_snapshot(auth, order_id, reason_code)
        if not assessment.can_submit or assessment.amount_minor is None:
            return assessment, None
        assert assessment.goods_minor is not None and assessment.shipping_minor is not None
        assert assessment.policy_id is not None and assessment.policy_version is not None
        available_lines = tuple(
            RefundLineAllocation(
                line.line_id,
                line.quantity,
                line.goods_minor,
                line.shipping_minor if assessment.shipping_minor else 0,
            )
            for line in snapshot.lines
        )
        if not available_lines or (
            sum(line.goods_minor for line in available_lines) != assessment.goods_minor
            or sum(line.shipping_minor for line in available_lines) != assessment.shipping_minor
        ):
            return replace(
                assessment,
                eligibility="unknown",
                can_submit=False,
                reason_code="LINE_ALLOCATION_INCONSISTENT",
                amount_minor=None,
                goods_minor=None,
                shipping_minor=None,
            ), None
        if lines is not None:
            by_id = {line.line_id: line for line in available_lines}
            if (
                not lines
                or len({line.order_line_id for line in lines}) != len(lines)
                or any(
                    not isinstance(line.order_line_id, str)
                    or line.order_line_id not in by_id
                    or type(line.quantity) is not int
                    or line.quantity != by_id[line.order_line_id].quantity
                    for line in lines
                )
            ):
                return replace(
                    assessment,
                    eligibility="unknown",
                    can_submit=False,
                    reason_code="LINE_SELECTION_UNSUPPORTED",
                    amount_minor=None,
                    goods_minor=None,
                    shipping_minor=None,
                ), None
            selected = {line.order_line_id for line in lines}
            quoted_lines = tuple(line for line in available_lines if line.line_id in selected)
        else:
            quoted_lines = available_lines
        goods_minor = sum(line.goods_minor for line in quoted_lines)
        shipping_minor = sum(line.shipping_minor for line in quoted_lines)
        amount_minor = goods_minor + shipping_minor
        if amount_minor <= 0:
            return replace(
                assessment,
                eligibility="ineligible",
                can_submit=False,
                reason_code="NO_SELECTED_BALANCE",
                amount_minor=None,
                goods_minor=None,
                shipping_minor=None,
            ), None
        assessment = replace(
            assessment,
            amount_minor=amount_minor,
            goods_minor=goods_minor,
            shipping_minor=shipping_minor,
        )
        quote = RefundQuote(
            quote_id=f"quote-{self._new_id()}",
            tenant_id=auth.tenant_id,
            order_id=order_id,
            subject_id=auth.subject_id,
            beneficiary_customer_id=snapshot.customer_id,
            reason_code=reason_code or "",
            eligibility=assessment.eligibility,
            amount_minor=assessment.amount_minor,
            goods_minor=assessment.goods_minor,
            shipping_minor=assessment.shipping_minor,
            currency=assessment.currency,
            policy_id=assessment.policy_id,
            policy_version=assessment.policy_version,
            order_version=assessment.order_version,
            payment_version=assessment.payment_version,
            expires_at=self._clock() + timedelta(seconds=self._ttl),
            lines=quoted_lines,
        )
        self._quotes.save(quote)
        return assessment, quote
