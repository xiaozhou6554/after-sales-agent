"""本地客户自助退款的绑定确认；生产需独立一次性验证通道。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    verify_tool_context,
)
from application.common.context import AuthContext
from application.common.errors import ErrorCode
from application.common.operation_confirmation import PreparedConfirmation
from application.common.ports.confirmation import ConfirmationRecord, ConfirmationStorePort
from application.refund.submission import RefundSubmissionUseCase


class RefundConfirmationUseCase:
    def __init__(
        self,
        submissions: RefundSubmissionUseCase,
        store: ConfirmationStorePort,
        *,
        ttl_seconds: int = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("退款确认有效期必须大于零")
        self._submissions = submissions
        self._store = store
        self._ttl = ttl_seconds
        self._clock = clock

    def prepare(self, auth: AuthContext, quote_id: str) -> PreparedConfirmation:
        quote = self._submissions.authorized_quote(auth, quote_id)
        binding = self._submissions.binding_for_quote(auth, quote_id)
        remaining = int((quote.expires_at - self._clock()).total_seconds())
        if remaining <= 0:
            raise ValueError("退款报价已过期，请重新报价")
        challenge = secrets.token_urlsafe(24)
        expires_at = self._clock() + timedelta(seconds=min(remaining, self._ttl))
        record = self._store.issue(
            binding, hashlib.sha256(challenge.encode("utf-8")).hexdigest(), expires_at
        )
        return PreparedConfirmation(record, challenge)

    def verify(self, auth: AuthContext, confirmation_id: str, challenge: str) -> ConfirmationRecord:
        verify_tool_context(auth, ("refund:request",), now=self._clock())
        record = self._store.get(auth.tenant_id, confirmation_id)
        if (
            record is None
            or record.binding.tenant_id != auth.tenant_id
            or record.binding.actor_subject_id != auth.subject_id
            or record.binding.confirmer_subject_id != auth.subject_id
            or record.binding.operation_type != "refund_request"
            or auth.data_scope.customer_ids != (record.binding.customer_id,)
            or "customer" not in auth.roles
        ):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        return self._store.verify(
            auth.tenant_id,
            confirmation_id,
            hashlib.sha256(challenge.encode("utf-8")).hexdigest(),
            self._clock(),
        )
