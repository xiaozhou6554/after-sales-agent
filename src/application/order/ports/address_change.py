"""收货地址修改的端口：快照读取、待办单存取、事务提交与会话意图。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from application.common.context import AuthContext, OperationContext, TraceContext
from application.order.dto.address_change import (
    AddressApplyResult,
    AddressChangeRecord,
    AddressOrderSnapshot,
)


@dataclass(frozen=True)
class AddressToolIntent:
    address_change_id: str
    confirmation_id: str
    idempotency_key: str


class AddressSnapshotPort(Protocol):
    def load(self, tenant_id: str, order_id: str) -> AddressOrderSnapshot | None: ...


class AddressChangeStorePort(Protocol):
    def save_pending(self, record: AddressChangeRecord) -> None: ...

    def load(self, tenant_id: str, address_change_id: str) -> AddressChangeRecord | None: ...

    def load_by_confirmation(
        self, tenant_id: str, confirmation_id: str
    ) -> AddressChangeRecord | None: ...


class AddressApplyPort(Protocol):
    def commit(
        self,
        auth: AuthContext,
        record: AddressChangeRecord,
        binding: OperationContext,
        confirmation_id: str,
        idempotency_key: str,
        trace: TraceContext,
    ) -> AddressApplyResult: ...


class AddressToolIntentPort(Protocol):
    def register(
        self, auth: AuthContext, record: AddressChangeRecord, confirmation_id: str
    ) -> None: ...

    def resolve(self, auth: AuthContext, order_id: str) -> AddressToolIntent: ...
