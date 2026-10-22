"""本地 Mock 收货地址修改：密文落库、事务重校验、Outbox 事件与意图解析。

仅用于合成 Mock-v2；企业接入需替换为 OMS 地址指令通道，明文不得出受控边界。
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from application.common.authorization import (
    require_customer_access,
    verify_tool_context,
)
from application.common.context import AuthContext, OperationContext, TraceContext
from application.common.events import EventEnvelope, OrderShippingAddressUpdatedPayload
from application.common.operation_confirmation import content_digest
from application.common.ports.confirmation import ConfirmationConflictError
from application.order.dto.address_change import (
    AddressApplyResult,
    AddressChangeRecord,
    AddressOrderSnapshot,
)
from application.order.ports.address_change import AddressToolIntent
from application.order.use_cases.address_change import (
    ADDRESS_OPERATION_TYPE,
    address_confirmation_binding,
    payload_address,
)
from domain.order.address import ShippingAddress
from infrastructure.messaging.idempotency_store import MySQLIdempotencyStore
from infrastructure.messaging.outbox_repository import OutboxRepository
from infrastructure.security.confirmation_store import MySQLConfirmationStore


class AddressChangeConflict(ValueError):
    pass


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class AddressCrypto:
    """受限个人信息对称加密；密钥经配置注入，库中只存密文。"""

    def __init__(self, key_material: str) -> None:
        if not key_material:
            raise ValueError("收货地址加密密钥不能为空")
        digest = hashlib.sha256(key_material.encode("utf-8")).digest()
        self._fernet = _fernet_factory(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, blob: bytes) -> str:
        return self._fernet.decrypt(bytes(blob)).decode("utf-8")


def _fernet_factory(key: bytes):
    from cryptography.fernet import Fernet

    return Fernet(key)


class MySqlAddressSnapshotAdapter:
    """地址资格判定的可信快照：订单状态、支付状态与活动退款占用。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, tenant_id: str, order_id: str) -> AddressOrderSnapshot | None:
        with self._engine.connect() as connection:
            order = connection.execute(
                text(
                    "SELECT customer_id,status,version FROM mock_oms_order"
                    " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                ),
                {"tenant_id": tenant_id, "order_id": order_id},
            ).first()
            if order is None:
                return None
            payments = [
                str(row.status)
                for row in connection.execute(
                    text(
                        "SELECT status FROM mock_pay_payment"
                        " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                    ),
                    {"tenant_id": tenant_id, "order_id": order_id},
                ).all()
            ]
            active_refund = connection.execute(
                text(
                    "SELECT active_request_id FROM refund_order_guard"
                    " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                    " AND active_request_id IS NOT NULL LIMIT 1"
                ),
                {"tenant_id": tenant_id, "order_id": order_id},
            ).first()
            pending_review = connection.execute(
                text(
                    "SELECT refund_request_id FROM refund_request WHERE tenant_id=:tenant_id"
                    " AND order_id=:order_id AND review_status IN"
                    " ('pending_review','reviewing','approved') LIMIT 1"
                ),
                {"tenant_id": tenant_id, "order_id": order_id},
            ).first()
        if not payments:
            payment_status = "unpaid"
        elif all(status == "succeeded" for status in payments):
            payment_status = "succeeded"
        else:
            payment_status = "unknown"
        return AddressOrderSnapshot(
            tenant_id=tenant_id,
            order_id=order_id,
            customer_id=str(order.customer_id),
            order_status=str(order.status),
            payment_status=payment_status,
            has_active_refund=active_refund is not None or pending_review is not None,
            order_version=int(order.version),
        )


class MySqlAddressChangeStore:
    def __init__(self, engine: Engine, crypto: AddressCrypto, connection: Connection | None = None):
        self._engine = engine
        self._crypto = crypto
        self._connection = connection

    def bind(self, connection: Connection) -> "MySqlAddressChangeStore":
        return MySqlAddressChangeStore(self._engine, self._crypto, connection)

    def save_pending(self, record: AddressChangeRecord) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO mock_oms_address_change"
                    " (tenant_id,address_change_id,order_id,customer_id,actor_subject_id,"
                    " expected_order_version,status,receiver_name,contact_phone,province,"
                    " city,district,detail_address,address_digest,confirmation_id,"
                    " idempotency_key,created_at,updated_at)"
                    " VALUES (:tenant_id,:address_change_id,:order_id,:customer_id,"
                    " :actor_subject_id,:expected_order_version,'pending',:receiver_name,"
                    " :contact_phone,:province,:city,:district,:detail_address,"
                    " :address_digest,:confirmation_id,:idempotency_key,:now,:now)"
                ),
                self._params(record, now),
            )

    def _params(self, record: AddressChangeRecord, now) -> dict:
        return {
            "tenant_id": record.tenant_id,
            "address_change_id": record.address_change_id,
            "order_id": record.order_id,
            "customer_id": record.customer_id,
            "actor_subject_id": record.actor_subject_id,
            "expected_order_version": record.expected_order_version,
            "receiver_name": self._crypto.encrypt(record.address.receiver_name),
            "contact_phone": self._crypto.encrypt(record.address.contact_phone),
            "province": record.address.province,
            "city": record.address.city,
            "district": record.address.district,
            "detail_address": self._crypto.encrypt(record.address.detail_address),
            "address_digest": record.address_digest,
            "confirmation_id": record.confirmation_id,
            "idempotency_key": record.idempotency_key,
            "now": now,
        }

    def _decrypt_address(self, row) -> ShippingAddress:
        try:
            return ShippingAddress(
                receiver_name=self._crypto.decrypt(bytes(row.receiver_name)),
                contact_phone=self._crypto.decrypt(bytes(row.contact_phone)),
                province=str(row.province),
                city=str(row.city),
                district=str(row.district),
                detail_address=self._crypto.decrypt(bytes(row.detail_address)),
            )
        except Exception as exc:  # noqa: BLE001 - 密钥不匹配时如实失败，不回退明文。
            raise AddressChangeConflict("地址密文解密失败，请联系管理员") from exc

    @staticmethod
    def _select():
        return (
            "SELECT address_change_id,tenant_id,order_id,customer_id,actor_subject_id,"
            " expected_order_version,status,receiver_name,contact_phone,province,city,"
            " district,detail_address,address_digest,confirmation_id,idempotency_key,"
            " result_order_version,error_code,observed_at,created_at"
            " FROM mock_oms_address_change"
        )

    def _record(self, row) -> AddressChangeRecord:
        return AddressChangeRecord(
            address_change_id=str(row.address_change_id),
            tenant_id=str(row.tenant_id),
            order_id=str(row.order_id),
            customer_id=str(row.customer_id),
            actor_subject_id=str(row.actor_subject_id),
            expected_order_version=int(row.expected_order_version),
            status=str(row.status),
            address=self._decrypt_address(row),
            address_digest=str(row.address_digest),
            confirmation_id=str(row.confirmation_id),
            idempotency_key=str(row.idempotency_key),
            result_order_version=(
                int(row.result_order_version) if row.result_order_version is not None else None
            ),
            error_code=str(row.error_code) if row.error_code is not None else None,
            observed_at=row.observed_at,
            created_at=row.created_at,
        )

    def load(self, tenant_id: str, address_change_id: str) -> AddressChangeRecord | None:
        suffix = " FOR UPDATE" if self._connection is not None else ""
        connection = self._connection
        if connection is None:
            with self._engine.connect() as borrowed:
                row = borrowed.execute(
                    text(self._select() + " WHERE tenant_id=:t AND address_change_id=:a"),
                    {"t": tenant_id, "a": address_change_id},
                ).first()
                return self._record(row) if row is not None else None
        row = connection.execute(
            text(self._select() + " WHERE tenant_id=:t AND address_change_id=:a" + suffix),
            {"t": tenant_id, "a": address_change_id},
        ).first()
        return self._record(row) if row is not None else None

    def load_by_confirmation(
        self, tenant_id: str, confirmation_id: str
    ) -> AddressChangeRecord | None:
        connection = self._connection
        statement = text(
            self._select() + " WHERE tenant_id=:t AND confirmation_id=:c"
        )
        if connection is None:
            with self._engine.connect() as borrowed:
                row = borrowed.execute(statement, {"t": tenant_id, "c": confirmation_id}).first()
                return self._record(row) if row is not None else None
        row = connection.execute(statement, {"t": tenant_id, "c": confirmation_id}).first()
        return self._record(row) if row is not None else None


class MySqlMockAddressChangeAdapter:
    """地址修改事务端口：重核资格、版本条件更新、确认消费与事件原子提交。"""

    def __init__(
        self,
        engine: Engine,
        store: MySqlAddressChangeStore,
        outbox: OutboxRepository,
        *,
        clock=lambda: datetime.now(timezone.utc),
        connection: Connection | None = None,
    ) -> None:
        self._engine = engine
        self._store = store
        self._outbox = outbox
        self._clock = clock
        self._connection = connection

    def bind(self, connection: Connection) -> "MySqlMockAddressChangeAdapter":
        return MySqlMockAddressChangeAdapter(
            self._engine, self._store.bind(connection), self._outbox,
            clock=self._clock, connection=connection,
        )

    @contextmanager
    def _transaction(self):
        if self._connection is not None:
            with self._connection.begin_nested():
                yield self._connection
        else:
            with self._engine.begin() as connection:
                yield connection

    def commit(
        self,
        auth: AuthContext,
        record: AddressChangeRecord,
        binding: OperationContext,
        confirmation_id: str,
        idempotency_key: str,
        trace: TraceContext,
    ) -> AddressApplyResult:
        if not trace.request_id or not trace.correlation_id or not trace.trace_id:
            raise AddressChangeConflict("地址修改缺少可信链路标识")
        verify_tool_context(auth, ("order:address:update",))
        require_customer_access(auth, record.customer_id, tenant_id=record.tenant_id)
        if (
            record.tenant_id != auth.tenant_id
            or record.actor_subject_id != auth.subject_id
            or "customer" not in auth.roles
            or auth.data_scope.customer_ids != (record.customer_id,)
            or record.confirmation_id != confirmation_id
            or record.idempotency_key != idempotency_key
            or binding != address_confirmation_binding(auth, record)
            or record.address_digest != content_digest(payload_address(record.address))
        ):
            raise AddressChangeConflict("地址修改确认绑定身份或内容不一致")
        now = self._clock()
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{auth.tenant_id}:{auth.subject_id}:{ADDRESS_OPERATION_TYPE}:{idempotency_key}",
        ).hex
        try:
            with self._transaction() as conn:
                order = conn.execute(
                    text(
                        "SELECT version,status FROM mock_oms_order"
                        " WHERE tenant_id=:tenant_id AND order_id=:order_id FOR UPDATE"
                    ),
                    {"tenant_id": auth.tenant_id, "order_id": record.order_id},
                ).first()
                if order is None:
                    raise AddressChangeConflict("订单不可用或不在授权范围内")
                payments = conn.execute(
                    text(
                        "SELECT status FROM mock_pay_payment"
                        " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                    ),
                    {"tenant_id": auth.tenant_id, "order_id": record.order_id},
                ).all()
                if not payments or any(str(row.status) != "succeeded" for row in payments):
                    raise AddressChangeConflict("订单支付状态已变化，地址不可修改")
                if str(order.status) != "pending_shipment":
                    raise AddressChangeConflict("订单状态已变化，地址不可修改")
                blocking = conn.execute(
                    text(
                        "SELECT a.address_change_id FROM mock_oms_address_change a"
                        " JOIN operation_confirmation c ON c.tenant_id=a.tenant_id"
                        " AND c.confirmation_id=a.confirmation_id"
                        " WHERE a.tenant_id=:tenant_id AND a.order_id=:order_id"
                        " AND a.status='pending' AND c.status='verified'"
                        " AND c.expires_at>:now FOR UPDATE"
                    ),
                    {
                        "tenant_id": auth.tenant_id,
                        "order_id": record.order_id,
                        "now": _utc_naive(now),
                    },
                ).all()
                if any(
                    str(row.address_change_id) != record.address_change_id for row in blocking
                ):
                    raise AddressChangeConflict("订单已有待处理的地址修改，请先处理原申请")
                guard = conn.execute(
                    text(
                        "SELECT active_request_id FROM refund_order_guard"
                        " WHERE tenant_id=:tenant_id AND order_id=:order_id FOR UPDATE"
                    ),
                    {"tenant_id": auth.tenant_id, "order_id": record.order_id},
                ).first()
                if guard is not None and guard.active_request_id is not None:
                    raise AddressChangeConflict("订单退款进行中，地址不可修改")
                idempotency = MySQLIdempotencyStore(self._engine).bind(conn)
                reserve = idempotency.reserve(
                    auth.tenant_id,
                    auth.subject_id,
                    ADDRESS_OPERATION_TYPE,
                    idempotency_key,
                    binding.payload_digest,
                )
                if reserve.status == "succeeded" and reserve.result_ref:
                    return self._existing(
                        conn, auth, reserve.result_ref, operation_id,
                        replayed=True, now=now,
                    )
                if reserve.status != "pending":
                    raise AddressChangeConflict("原地址修改结果待核实，请按原标识查询")
                stored = self._store.bind(conn).load(auth.tenant_id, record.address_change_id)
                if stored is None or stored.status != "pending":
                    raise AddressChangeConflict("地址修改单状态已变化，请查询原结果")
                if int(order.version) != record.expected_order_version:
                    raise AddressChangeConflict("订单版本已变化，请重新发起地址修改")
                MySQLConfirmationStore(self._engine).bind(conn).consume(
                    confirmation_id, binding, operation_id, now
                )
                updated = conn.execute(
                    text(
                        "UPDATE mock_oms_order SET version=version+1"
                        " WHERE tenant_id=:tenant_id AND order_id=:order_id"
                        " AND version=:expected_version"
                    ),
                    {
                        "tenant_id": auth.tenant_id,
                        "order_id": record.order_id,
                        "expected_version": record.expected_order_version,
                    },
                )
                if updated.rowcount != 1:
                    raise AddressChangeConflict("订单版本并发冲突")
                new_version = int(order.version) + 1
                conn.execute(
                    text(
                        "UPDATE mock_oms_address_change SET status='succeeded',"
                        " result_order_version=:new_version,error_code=NULL,observed_at=:now,"
                        " updated_at=:now WHERE tenant_id=:tenant_id"
                        " AND address_change_id=:address_change_id AND status='pending'"
                    ),
                    {
                        "tenant_id": auth.tenant_id,
                        "address_change_id": record.address_change_id,
                        "new_version": new_version,
                        "now": _utc_naive(now),
                    },
                )
                event = EventEnvelope(
                    schema_version="1.0",
                    event_id=uuid.uuid4().hex,
                    event_type="order.shipping_address.updated",
                    event_version=1,
                    tenant_id=auth.tenant_id,
                    request_id=trace.request_id,
                    correlation_id=trace.correlation_id,
                    trace_id=trace.trace_id,
                    operation_id=operation_id,
                    causation_id=None,
                    parent_event_id=None,
                    producer="order-service",
                    occurred_at=now.isoformat(),
                    idempotency_key=f"order-shipping-address-updated:{record.address_change_id}",
                    subject_id=auth.subject_id,
                    retry_count=0,
                    payload=OrderShippingAddressUpdatedPayload(
                        order_id=record.order_id,
                        address_change_id=record.address_change_id,
                        result_order_version=new_version,
                        actor_subject_id=auth.subject_id,
                        customer_id=record.customer_id,
                    ),
                )
                self._outbox.enqueue(
                    conn,
                    event,
                    aggregate_type="order",
                    aggregate_id=record.order_id,
                    thread_id=trace.thread_id,
                )
                if not idempotency.complete(reserve, "succeeded", record.address_change_id):
                    raise AddressChangeConflict("地址修改幂等结果写入冲突")
                return AddressApplyResult(
                    operation_id,
                    record.address_change_id,
                    "succeeded",
                    new_version,
                    None,
                    now,
                )
        except IntegrityError as exc:
            raise AddressChangeConflict("地址修改冲突，请查询原结果") from exc

    @staticmethod
    def _existing(
        conn, auth: AuthContext, address_change_id: str, operation_id: str,
        *, replayed: bool, now,
    ):
        row = conn.execute(
            text(
                "SELECT status,result_order_version,error_code,observed_at"
                " FROM mock_oms_address_change WHERE tenant_id=:tenant_id"
                " AND address_change_id=:address_change_id AND actor_subject_id=:actor"
            ),
            {
                "tenant_id": auth.tenant_id,
                "address_change_id": address_change_id,
                "actor": auth.subject_id,
            },
        ).first()
        if row is None:
            raise AddressChangeConflict("原地址修改状态待核实")
        return AddressApplyResult(
            operation_id=operation_id,
            address_change_id=address_change_id,
            status=str(row.status),
            result_order_version=(
                int(row.result_order_version) if row.result_order_version is not None else None
            ),
            error_code=str(row.error_code) if row.error_code is not None else None,
            observed_at=row.observed_at or now,
            replayed=replayed,
        )


class MySqlAddressToolIntent:
    """Session-bound resolver for verified local address confirmations."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(
        self, auth: AuthContext, record: AddressChangeRecord, confirmation_id: str
    ) -> None:
        if (
            not auth.session_id
            or len(auth.session_id) > 128
            or record.tenant_id != auth.tenant_id
            or record.actor_subject_id != auth.subject_id
        ):
            raise ConfirmationConflictError("address tool intent identity mismatch")
        stored = MySQLConfirmationStore(self._engine).get(auth.tenant_id, confirmation_id)
        if (
            stored is None
            or stored.status != "prepared"
            or stored.binding != address_confirmation_binding(auth, record)
        ):
            raise ConfirmationConflictError("address confirmation is not bound to change")
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO address_tool_intent"
                    " (tenant_id,confirmation_id,actor_subject_id,session_id,"
                    " address_change_id,order_id,created_at) VALUES"
                    " (:tenant,:confirmation,:actor,:session,:change,:order,:now)"
                ),
                {
                    "tenant": auth.tenant_id,
                    "confirmation": confirmation_id,
                    "actor": auth.subject_id,
                    "session": auth.session_id,
                    "change": record.address_change_id,
                    "order": record.order_id,
                    "now": datetime.now(timezone.utc).replace(tzinfo=None),
                },
            )

    def resolve(self, auth: AuthContext, order_id: str) -> AddressToolIntent:
        if (
            not auth.session_id
            or len(auth.session_id) > 128
            or not order_id
            or "customer" not in auth.roles
            or len(auth.data_scope.customer_ids) != 1
        ):
            raise ConfirmationConflictError("address tool intent unavailable")
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT i.address_change_id,i.confirmation_id,c.status"
                    " FROM address_tool_intent i"
                    " JOIN operation_confirmation c ON c.tenant_id=i.tenant_id"
                    " AND c.confirmation_id=i.confirmation_id"
                    " JOIN mock_oms_address_change a ON a.tenant_id=i.tenant_id"
                    " AND a.address_change_id=i.address_change_id"
                    " WHERE i.tenant_id=:tenant AND i.actor_subject_id=:actor"
                    " AND i.session_id=:session AND i.order_id=:order"
                    " AND c.actor_subject_id=:actor AND c.confirmer_subject_id=:actor"
                    " AND c.customer_id=:customer"
                    " AND c.operation_type='shipping_address_change'"
                    " AND c.target_id=:order AND a.customer_id=:customer"
                    " AND a.actor_subject_id=:actor AND a.order_id=:order"
                    " AND a.status='pending' AND c.payload_digest IS NOT NULL"
                    " AND c.status IN ('verified','consumed')"
                    " AND c.expires_at>:now"
                    " ORDER BY (c.status='verified') DESC, c.created_at DESC LIMIT 3"
                ),
                {
                    "tenant": auth.tenant_id,
                    "actor": auth.subject_id,
                    "session": auth.session_id,
                    "order": order_id,
                    "customer": auth.data_scope.customer_ids[0],
                    "now": datetime.now(timezone.utc).replace(tzinfo=None),
                },
            ).all()
        verified = [row for row in rows if row.status == "verified"]
        candidates = verified if verified else rows
        if len(candidates) != 1:
            raise ConfirmationConflictError("address tool intent missing or ambiguous")
        row = candidates[0]
        return AddressToolIntent(
            address_change_id=row.address_change_id,
            confirmation_id=row.confirmation_id,
            idempotency_key=uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{auth.tenant_id}:{auth.subject_id}:{ADDRESS_OPERATION_TYPE}:"
                f"{row.confirmation_id}",
            ).hex,
        )
