"""退款申请授权查询；审核、退货、资金三态不得混淆。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext
from application.common.errors import ErrorCode


@dataclass(frozen=True)
class RefundRequestSnapshot:
    tenant_id: str
    refund_request_id: str
    order_id: str
    customer_id: str
    actor_subject_id: str
    review_status: str
    return_status: str
    funds_status: str
    amount_minor: int
    currency: str
    revision: int
    updated_at: datetime


class RefundRequestReadPort(Protocol):
    def load(self, tenant_id: str, refund_request_id: str) -> RefundRequestSnapshot | None: ...

    def load_by_operation(
        self, tenant_id: str, actor_subject_id: str, idempotency_key: str
    ) -> RefundRequestSnapshot | None: ...


class RefundRequestQueryUseCase:
    def __init__(self, requests: RefundRequestReadPort) -> None:
        self._requests = requests

    def by_id(self, auth: AuthContext, refund_request_id: str) -> RefundRequestSnapshot:
        verify_tool_context(auth, ("refund:read",))
        if not refund_request_id:
            raise ValueError("退款申请标识不能为空")
        return self._authorized(auth, self._requests.load(auth.tenant_id, refund_request_id))

    def by_original_key(self, auth: AuthContext, idempotency_key: str) -> RefundRequestSnapshot:
        verify_tool_context(auth, ("refund:read",))
        if not idempotency_key:
            raise ValueError("原幂等键不能为空")
        return self._authorized(
            auth,
            self._requests.load_by_operation(auth.tenant_id, auth.subject_id, idempotency_key),
        )

    @staticmethod
    def _authorized(auth: AuthContext, snapshot: RefundRequestSnapshot | None):
        if snapshot is None or snapshot.tenant_id != auth.tenant_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        require_customer_access(auth, snapshot.customer_id, tenant_id=snapshot.tenant_id)
        return snapshot
