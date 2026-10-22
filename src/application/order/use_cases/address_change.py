"""收货地址修改用例（FR-18/步骤26）：资格、受控确认、会话意图与提交边界。

- 明文地址只在受控表单入参与服务端内部流转；对模型与查询侧只暴露脱敏摘要；
- 确认绑定 租户+主体+订单+版本+地址内容，提交时由事务端口条件重校验版本；
- 幂等键由服务端从 confirmation_id 派生，模型不可注入。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from application.common.authorization import (
    GENERIC_DENIED_MESSAGE,
    AuthorizationDenied,
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext, OperationContext, TraceContext
from application.common.errors import ErrorCode
from application.common.operation_confirmation import (
    PreparedConfirmation,
    content_digest,
)
from application.order.dto.address_change import (
    AddressApplyResult,
    AddressChangeRecord,
    AddressOrderSnapshot,
)
from application.order.ports.address_change import (
    AddressApplyPort,
    AddressChangeStorePort,
    AddressSnapshotPort,
    AddressToolIntentPort,
)
from domain.order.address import ShippingAddress, address_denial_reasons

ADDRESS_OPERATION_TYPE = "shipping_address_change"


class AddressChangeDenied(ValueError):
    """资格或校验不通过；reasons 为稳定 denial_reasons 编码。"""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("、".join(reasons) or "ADDRESS_CHANGE_DENIED")
        self.reasons = list(reasons)


@dataclass(frozen=True)
class PreparedAddressChange:
    record: AddressChangeRecord
    confirmation: PreparedConfirmation


def _customer_only(auth: AuthContext, customer_id: str, tenant_id: str) -> None:
    require_customer_access(auth, customer_id, tenant_id=tenant_id)
    if "customer" not in auth.roles or auth.data_scope.customer_ids != (customer_id,):
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)


def address_confirmation_binding(
    auth: AuthContext, record: AddressChangeRecord
) -> OperationContext:
    payload = {
        "order_id": record.order_id,
        "address_change_id": record.address_change_id,
        "expected_order_version": record.expected_order_version,
        "address": {
            "receiver_name": record.address.receiver_name,
            "contact_phone": record.address.contact_phone,
            "province": record.address.province,
            "city": record.address.city,
            "district": record.address.district,
            "detail_address": record.address.detail_address,
        },
    }
    return OperationContext.from_trusted_actor(
        auth,
        customer_id=record.customer_id,
        confirmer_subject_id=auth.subject_id,
        operation_type=ADDRESS_OPERATION_TYPE,
        target_id=record.order_id,
        payload_digest=content_digest(payload),
        expected_revision=record.expected_order_version,
    )


def server_address_idempotency_key(auth: AuthContext, confirmation_id: str) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{auth.tenant_id}:{auth.subject_id}:{ADDRESS_OPERATION_TYPE}:{confirmation_id}",
    ).hex


class AddressChangeUseCase:
    def __init__(
        self,
        snapshots: AddressSnapshotPort,
        store: AddressChangeStorePort,
        confirmation,
        intents: AddressToolIntentPort,
        *,
        ttl_seconds: int = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("确认有效期必须大于零")
        self._snapshots = snapshots
        self._store = store
        self._confirmation = confirmation
        self._intents = intents
        self._ttl = ttl_seconds
        self._clock = clock

    def _authorized_snapshot(
        self, auth: AuthContext, order_id: str, scope: str
    ) -> AddressOrderSnapshot:
        verify_tool_context(auth, (scope,), now=self._clock())
        if not order_id:
            raise ValueError("订单标识不能为空")
        snapshot = self._snapshots.load(auth.tenant_id, order_id)
        if snapshot is None or snapshot.tenant_id != auth.tenant_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        _customer_only(auth, snapshot.customer_id, snapshot.tenant_id)
        return snapshot

    def check(self, auth: AuthContext, order_id: str) -> dict:
        snapshot = self._authorized_snapshot(auth, order_id, "order:read")
        reasons = address_denial_reasons(
            order_status=snapshot.order_status,
            payment_status=snapshot.payment_status,
            has_active_refund=snapshot.has_active_refund,
        )
        eligible = not reasons
        return {
            "eligible": eligible,
            "order_id": snapshot.order_id,
            "order_version": str(snapshot.order_version),
            "denial_reasons": reasons,
            "confirmation_required": eligible,
            "evidence_refs": [
                f"mock_oms_order:{snapshot.order_id}@v{snapshot.order_version}",
                f"mock_pay_payment:{snapshot.order_id}:{snapshot.payment_status}",
            ],
        }

    def prepare(
        self, auth: AuthContext, order_id: str, address: ShippingAddress
    ) -> PreparedAddressChange:
        snapshot = self._authorized_snapshot(auth, order_id, "order:address:update")
        address.validate()
        reasons = address_denial_reasons(
            order_status=snapshot.order_status,
            payment_status=snapshot.payment_status,
            has_active_refund=snapshot.has_active_refund,
        )
        if not address.delivery_in_range():
            reasons.append("ADDRESS_OUT_OF_DELIVERY_RANGE")
        if reasons:
            raise AddressChangeDenied(reasons)
        record = AddressChangeRecord(
            address_change_id=f"addr-{uuid.uuid4().hex}",
            tenant_id=auth.tenant_id,
            order_id=snapshot.order_id,
            customer_id=snapshot.customer_id,
            actor_subject_id=auth.subject_id,
            expected_order_version=snapshot.order_version,
            status="pending",
            address=address,
            address_digest="",
            confirmation_id="",
            idempotency_key="",
        )
        binding = address_confirmation_binding(auth, record)
        prepared = self._confirmation.prepare(binding)
        record = AddressChangeRecord(
            **{
                **record.__dict__,
                "confirmation_id": prepared.record.confirmation_id,
                "idempotency_key": server_address_idempotency_key(
                    auth, prepared.record.confirmation_id
                ),
                "address_digest": content_digest(payload_address(address)),
            }
        )
        self._store.save_pending(record)
        self._intents.register(auth, record, prepared.record.confirmation_id)
        return PreparedAddressChange(record, prepared)

    def prepare_from_fields(
        self, auth: AuthContext, order_id: str, fields: dict[str, str]
    ) -> PreparedAddressChange:
        """入口适配器提交的字段由用例转换为领域值对象。"""
        return self.prepare(auth, order_id, ShippingAddress(**fields))

    def verify(self, auth: AuthContext, confirmation_id: str, challenge: str) -> object:
        verify_tool_context(auth, ("order:address:update",), now=self._clock())
        stored = self._confirmation.get(auth.tenant_id, confirmation_id)
        binding = stored.binding if stored is not None else None
        if (
            stored is None
            or stored.status != "prepared"
            or binding is None
            or binding.tenant_id != auth.tenant_id
            or binding.actor_subject_id != auth.subject_id
            or binding.confirmer_subject_id != auth.subject_id
            or binding.operation_type != ADDRESS_OPERATION_TYPE
            or auth.data_scope.customer_ids != (binding.customer_id,)
            or "customer" not in auth.roles
        ):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        return self._confirmation.verify(auth.tenant_id, confirmation_id, challenge)

    def query(self, auth: AuthContext, address_change_id: str) -> dict:
        verify_tool_context(auth, ("order:read",), now=self._clock())
        if not address_change_id:
            raise ValueError("地址修改单标识不能为空")
        record = self._store.load(auth.tenant_id, address_change_id)
        if record is None or record.tenant_id != auth.tenant_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        _customer_only(auth, record.customer_id, record.tenant_id)
        observed = record.observed_at or record.created_at or self._clock()
        return {
            "address_change_id": record.address_change_id,
            "order_id": record.order_id,
            "status": record.status,
            "expected_order_version": record.expected_order_version,
            "result_order_version": record.result_order_version,
            "observed_at": observed,
        }

    def by_confirmation(self, auth: AuthContext, confirmation_id: str) -> dict:
        verify_tool_context(auth, ("order:read",), now=self._clock())
        record = self._store.load_by_confirmation(auth.tenant_id, confirmation_id)
        if record is None or record.tenant_id != auth.tenant_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        _customer_only(auth, record.customer_id, record.tenant_id)
        observed = record.observed_at or record.created_at or self._clock()
        return {
            "address_change_id": record.address_change_id,
            "order_id": record.order_id,
            "status": record.status,
            "expected_order_version": record.expected_order_version,
            "result_order_version": record.result_order_version,
            "observed_at": observed,
        }


def payload_address(address: ShippingAddress) -> dict:
    return {
        "receiver_name": address.receiver_name,
        "contact_phone": address.contact_phone,
        "province": address.province,
        "city": address.city,
        "district": address.district,
        "detail_address": address.detail_address,
    }


class AddressChangeApplyUseCase:
    def __init__(
        self,
        store: AddressChangeStorePort,
        apply_port: AddressApplyPort,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._store = store
        self._port = apply_port
        self._clock = clock

    def apply(
        self,
        auth: AuthContext,
        order_id: str,
        address_change_id: str,
        confirmation_id: str,
        idempotency_key: str,
        trace: TraceContext,
    ) -> AddressApplyResult:
        verify_tool_context(auth, ("order:address:update",), now=self._clock())
        if not confirmation_id or not idempotency_key or len(idempotency_key) > 64:
            raise ValueError("地址修改确认或幂等标识缺失")
        record = self._store.load(auth.tenant_id, address_change_id)
        if (
            record is None
            or record.tenant_id != auth.tenant_id
            or record.order_id != order_id
        ):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        _customer_only(auth, record.customer_id, record.tenant_id)
        if record.actor_subject_id != auth.subject_id:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        binding = address_confirmation_binding(auth, record)
        return self._port.commit(
            auth, record, binding, confirmation_id, idempotency_key, trace
        )
