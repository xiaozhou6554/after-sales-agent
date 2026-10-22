"""确认凭证端口及与业务事务共享的消费契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from application.common.context import OperationContext


@dataclass(frozen=True)
class ConfirmationRecord:
    confirmation_id: str
    binding: OperationContext
    status: str
    expires_at: datetime
    consumed_by_operation_id: str | None


class ConfirmationConflictError(RuntimeError):
    """确认已失效、已消费或绑定内容变化。"""


class ConfirmationStorePort(Protocol):
    def issue(
        self, binding: OperationContext, challenge_digest: str, expires_at: datetime
    ) -> ConfirmationRecord: ...

    def verify(
        self, tenant_id: str, confirmation_id: str, challenge_digest: str, now: datetime
    ) -> ConfirmationRecord: ...

    def consume(
        self,
        confirmation_id: str,
        binding: OperationContext,
        operation_id: str,
        now: datetime,
    ) -> ConfirmationRecord: ...

    def get(self, tenant_id: str, confirmation_id: str) -> ConfirmationRecord | None: ...
