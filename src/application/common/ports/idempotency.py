"""租户、真实操作者、操作类型与请求摘要绑定的幂等契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class IdempotencyRecord:
    record_id: str
    tenant_id: str
    actor_subject_id: str
    operation_type: str
    idempotency_key: str
    request_digest: str
    status: str
    result_ref: str | None
    revision: int


class IdempotencyConflictError(RuntimeError):
    """同一键绑定不同请求。"""


class IdempotencyStorePort(Protocol):
    def reserve(
        self,
        tenant_id: str,
        actor_subject_id: str,
        operation_type: str,
        idempotency_key: str,
        request_digest: str,
    ) -> IdempotencyRecord: ...

    def get(
        self, tenant_id: str, actor_subject_id: str, operation_type: str, idempotency_key: str
    ) -> IdempotencyRecord | None: ...
