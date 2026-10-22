"""退款创建的服务端边界：绑定报价、身份、确认和幂等，提交由事务端口完成。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext, OperationContext, TraceContext
from application.common.errors import ErrorCode
from application.common.operation_confirmation import content_digest
from application.refund.qualification import RefundQuote


class RefundQuoteLookup(Protocol):
    def load(self, tenant_id: str, quote_id: str) -> RefundQuote | None: ...


@dataclass(frozen=True)
class RefundRequestResult:
    refund_request_id: str
    status: str
    amount_minor: int
    currency: str
    replayed: bool = False


class RefundSubmissionPort(Protocol):
    def commit(
        self,
        auth: AuthContext,
        quote: RefundQuote,
        binding: OperationContext,
        confirmation_id: str,
        idempotency_key: str,
        trace: TraceContext,
    ) -> RefundRequestResult: ...


def refund_confirmation_binding(auth: AuthContext, quote: RefundQuote) -> OperationContext:
    payload = {
        "quote_id": quote.quote_id,
        "order_id": quote.order_id,
        "reason_code": quote.reason_code,
        "amount_minor": quote.amount_minor,
        "goods_minor": quote.goods_minor,
        "shipping_minor": quote.shipping_minor,
        "currency": quote.currency,
        "policy_id": quote.policy_id,
        "policy_version": quote.policy_version,
        "order_version": quote.order_version,
        "payment_version": quote.payment_version,
        "lines": [line.__dict__ for line in quote.lines],
    }
    try:
        expected_revision = int(quote.order_version)
    except ValueError:
        expected_revision = None
    return OperationContext.from_trusted_actor(
        auth,
        customer_id=quote.beneficiary_customer_id,
        confirmer_subject_id=auth.subject_id,
        operation_type="refund_request",
        target_id=quote.order_id,
        payload_digest=content_digest(payload),
        expected_revision=expected_revision,
    )


class RefundSubmissionUseCase:
    def __init__(
        self,
        quotes: RefundQuoteLookup,
        submission: RefundSubmissionPort,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._quotes = quotes
        self._submission = submission
        self._clock = clock

    def binding_for_quote(self, auth: AuthContext, quote_id: str) -> OperationContext:
        quote = self.authorized_quote(auth, quote_id)
        return refund_confirmation_binding(auth, quote)

    def submit(
        self,
        auth: AuthContext,
        quote_id: str,
        confirmation_id: str,
        idempotency_key: str,
        trace: TraceContext,
    ) -> RefundRequestResult:
        quote = self.authorized_quote(auth, quote_id)
        if not confirmation_id or not idempotency_key or len(idempotency_key) > 64:
            raise ValueError("退款确认或幂等标识缺失")
        binding = refund_confirmation_binding(auth, quote)
        return self._submission.commit(
            auth, quote, binding, confirmation_id, idempotency_key, trace
        )

    def authorized_quote(self, auth: AuthContext, quote_id: str) -> RefundQuote:
        verify_tool_context(auth, ("refund:request",))
        if not quote_id:
            raise ValueError("退款报价标识不能为空")
        quote = self._quotes.load(auth.tenant_id, quote_id)
        if quote is None or quote.tenant_id != auth.tenant_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        require_customer_access(auth, quote.beneficiary_customer_id, tenant_id=quote.tenant_id)
        if "customer" not in auth.roles or auth.data_scope.customer_ids != (
            quote.beneficiary_customer_id,
        ):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        if quote.subject_id != auth.subject_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        if quote.expires_at <= self._clock():
            raise ValueError("退款报价已过期，请重新报价")
        if (
            not quote.lines
            or sum(line.goods_minor for line in quote.lines) != quote.goods_minor
            or sum(line.shipping_minor for line in quote.lines) != quote.shipping_minor
        ):
            raise ValueError("退款报价缺少可信逐行组成，请重新报价")
        return quote
