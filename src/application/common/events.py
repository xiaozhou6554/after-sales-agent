"""集成事件信封与载荷契约（《技术详细规格》26.8 与 8.3，纯 Python）。

唯一权威定义是 data/contracts/events.schema.json 与 event-catalog.json；
本模块是实现侧的强类型绑定：信封字段、event_type 枚举、按类型的严格载荷
均与该 Schema 一致，任何一侧变更必须同步并通过契约测试。

- 信封 subject_id 表示真实操作者（可空：系统知识发布等使用受信任服务主体）；
- 载荷统一携带 actor_subject_id 与 customer_id（除特别说明）；
  客户业务事件的 customer_id 不得以 null 代表全租户访问；
- 跨进程校验由 events.schema.json 承担；此处构造/解析同样 fail-closed。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any, ClassVar

EVENT_SCHEMA_VERSION = "1.0"

EVENT_TYPES: tuple[str, ...] = (
    "ticket.created",
    "order.shipping_address.updated",
    "ticket.updated",
    "human.handoff.created",
    "notification.created",
    "refund.confirmed",
    "knowledge.updated",
    "agent.task.created",
    "tool.call.failed",
    "audit.record.created",
    "return.created",
    "warehouse.return.received",
    "warehouse.return.inspected",
    "refund.approved",
    "refund.execution.requested",
    "refund.execution.succeeded",
    "refund.execution.failed",
    "ontology.release.activated",
)


class EventContractViolation(ValueError):
    """事件信封或载荷违反契约。"""


class StrictPayload:
    """载荷基类：按 dataclass 字段注解做类型与非空校验。

    约定：str 字段必须非空；str|None 可空；int 必须 >= 0；
    list[str] 元素必须非空字符串；bool/datetime 按类型校验。
    """

    # 子类不声明则允许 customer_id 为 null 的场景由业务侧控制；
    # 客户业务事件发布前必须显式赋值（见 spec 26.8）。
    REQUIRED_ACTOR: ClassVar[bool] = True

    def __post_init__(self) -> None:
        for f in fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            annotation = f.type
            if annotation == "str" and (not isinstance(value, str) or not value):
                raise EventContractViolation(f"{type(self).__name__}.{f.name} 必须为非空字符串")
            if annotation == "int" and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise EventContractViolation(f"{type(self).__name__}.{f.name} 必须为非负整数")
            if annotation.startswith("list") and not isinstance(value, list):
                raise EventContractViolation(f"{type(self).__name__}.{f.name} 必须为列表")
            if (
                annotation.startswith("list")
                and isinstance(value, list)
                and not all(isinstance(item, str) and item for item in value)
            ):
                raise EventContractViolation(f"{type(self).__name__}.{f.name} 元素必须为非空字符串")


@dataclass(frozen=True)
class TicketCreatedPayload(StrictPayload):
    ticket_id: str | None
    revision: int
    status: str
    priority: str
    context_ref: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class TicketUpdatedPayload(TicketCreatedPayload):
    pass


@dataclass(frozen=True)
class OrderShippingAddressUpdatedPayload(StrictPayload):
    order_id: str
    address_change_id: str
    result_order_version: int
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class HumanHandoffCreatedPayload(StrictPayload):
    handoff_id: str
    ticket_id: str | None
    status: str
    summary_ref: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class NotificationCreatedPayload(StrictPayload):
    notification_id: str
    channel: str
    template_id: str
    resource_refs: list[str]
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class RefundConfirmedPayload(StrictPayload):
    refund_request_id: str
    order_id: str
    amount_minor: int
    currency: str
    revision: int
    requires_manual_review: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class KnowledgeUpdatedPayload(StrictPayload):
    knowledge_release_id: str
    document_id: str
    version: str
    change_type: str
    manifest_digest: str
    actor_subject_id: str
    customer_id: str | None

    REQUIRED_ACTOR = False  # 系统知识发布使用受信任服务主体，customer_id 明确为 null

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.change_type not in ("upsert", "delete"):
            raise EventContractViolation("知识变更类型必须为 upsert/delete")
        if len(self.manifest_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.manifest_digest
        ):
            raise EventContractViolation("知识发布清单摘要必须为小写 SHA256")


@dataclass(frozen=True)
class AgentTaskCreatedPayload(StrictPayload):
    task_id: str
    task_type: str
    checkpoint_ref: str | None
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class ToolCallFailedPayload(StrictPayload):
    invocation_id: str
    tool_name: str
    args_digest: str
    attempt_count: int
    retry_count: int
    retry_allowed: str
    error_code: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class AuditRecordCreatedPayload(StrictPayload):
    audit_id: str
    action: str
    resource_refs: list[str]
    status: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class ReturnCreatedPayload(StrictPayload):
    return_id: str
    refund_request_id: str
    order_id: str
    request_line_refs: list[str]
    revision: int
    environment: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class WarehouseReturnReceivedPayload(StrictPayload):
    return_id: str
    receipt_id: str
    receipt_line_refs: list[str]
    received_qty: int
    source_version: str
    evidence_refs: list[str]
    environment: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class WarehouseReturnInspectedPayload(StrictPayload):
    return_id: str
    inspection_id: str
    receipt_line_refs: list[str]
    accepted_qty: int
    rejected_qty: int
    reason_codes: list[str]
    source_version: str
    evidence_refs: list[str]
    environment: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class RefundApprovedPayload(StrictPayload):
    refund_request_id: str
    revision: int
    approved_amount_minor: int
    currency: str
    return_status: str
    review_subject_id: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class RefundExecutionRequestedPayload(StrictPayload):
    execution_id: str
    refund_request_id: str
    original_payment_id: str
    allocation_refs: list[str]
    amount_minor: int
    currency: str
    environment: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class RefundExecutionSucceededPayload(StrictPayload):
    execution_id: str
    refund_request_id: str
    channel_transaction_ref: str
    amount_minor: int
    currency: str
    confirmed_at: str
    source_version: str
    environment: str
    actor_subject_id: str
    customer_id: str | None

    def __post_init__(self) -> None:
        super().__post_init__()
        _parse_utc_datetime(self.confirmed_at, f"{type(self).__name__}.confirmed_at")


@dataclass(frozen=True)
class RefundExecutionFailedPayload(StrictPayload):
    execution_id: str
    refund_request_id: str
    failure_code: str
    retry_decision_ref: str
    source_version: str
    environment: str
    actor_subject_id: str
    customer_id: str | None


@dataclass(frozen=True)
class OntologyReleaseActivatedPayload(StrictPayload):
    release_id: str
    manifest_digest: str
    mapping_version: str
    metric_version: str
    activated_at: str
    actor_subject_id: str
    customer_id: str | None

    def __post_init__(self) -> None:
        super().__post_init__()
        _parse_utc_datetime(self.activated_at, f"{type(self).__name__}.activated_at")


# event_type → 载荷类型注册表；与 events.schema.json 的 allOf 分支一一对应。
EVENT_PAYLOAD_REGISTRY: dict[str, type[StrictPayload]] = {
    "ticket.created": TicketCreatedPayload,
    "ticket.updated": TicketUpdatedPayload,
    "order.shipping_address.updated": OrderShippingAddressUpdatedPayload,
    "human.handoff.created": HumanHandoffCreatedPayload,
    "notification.created": NotificationCreatedPayload,
    "refund.confirmed": RefundConfirmedPayload,
    "knowledge.updated": KnowledgeUpdatedPayload,
    "agent.task.created": AgentTaskCreatedPayload,
    "tool.call.failed": ToolCallFailedPayload,
    "audit.record.created": AuditRecordCreatedPayload,
    "return.created": ReturnCreatedPayload,
    "warehouse.return.received": WarehouseReturnReceivedPayload,
    "warehouse.return.inspected": WarehouseReturnInspectedPayload,
    "refund.approved": RefundApprovedPayload,
    "refund.execution.requested": RefundExecutionRequestedPayload,
    "refund.execution.succeeded": RefundExecutionSucceededPayload,
    "refund.execution.failed": RefundExecutionFailedPayload,
    "ontology.release.activated": OntologyReleaseActivatedPayload,
}


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: str
    event_id: str
    event_type: str
    event_version: int
    tenant_id: str
    request_id: str
    correlation_id: str
    trace_id: str
    operation_id: str | None
    causation_id: str | None
    parent_event_id: str | None
    producer: str
    occurred_at: str
    idempotency_key: str
    subject_id: str | None
    retry_count: int
    payload: StrictPayload

    def payload_dict(self) -> dict[str, Any]:
        return dict(vars(self.payload))

    def to_dict(self) -> dict[str, Any]:
        """生成消息传输与持久化共用的严格信封字典。"""
        return asdict(self)


def _parse_utc_datetime(value: str, label: str) -> datetime:
    """解析 ISO-8601 date-time（兼容 Z 后缀，Python 3.10 fromisoformat 不识别 Z）。"""
    if not isinstance(value, str) or not value:
        raise EventContractViolation(f"{label} 必须为 ISO-8601 时间字符串")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EventContractViolation(f"{label} 不是合法 ISO-8601 时间: {value!r}") from exc


def _require_str(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise EventContractViolation(f"信封字段 {key} 必须为非空字符串")
    return value


def parse_event(raw: dict) -> EventEnvelope:
    """从反序列化后的字典严格解析事件；任何缺失/多余/非法字段都拒绝。"""
    if not isinstance(raw, dict):
        raise EventContractViolation("事件必须为对象")
    known = {
        "schema_version",
        "event_id",
        "event_type",
        "event_version",
        "tenant_id",
        "subject_id",
        "request_id",
        "correlation_id",
        "trace_id",
        "operation_id",
        "causation_id",
        "parent_event_id",
        "producer",
        "occurred_at",
        "retry_count",
        "idempotency_key",
        "payload",
    }
    unknown = set(raw) - known
    if unknown:
        raise EventContractViolation(f"信封存在未知字段: {sorted(unknown)}")
    if raw.get("schema_version") != EVENT_SCHEMA_VERSION:
        raise EventContractViolation(f"schema_version 必须为 {EVENT_SCHEMA_VERSION!r}")
    event_type = _require_str(raw, "event_type")
    payload_cls = EVENT_PAYLOAD_REGISTRY.get(event_type)
    if payload_cls is None:
        raise EventContractViolation(f"未知 event_type: {event_type!r}")
    event_version = raw.get("event_version")
    if not isinstance(event_version, int) or isinstance(event_version, bool) or event_version < 1:
        raise EventContractViolation("event_version 必须为 >= 1 的整数")
    idempotency_key = _require_str(raw, "idempotency_key")
    if not 16 <= len(idempotency_key) <= 128:
        raise EventContractViolation("idempotency_key 长度必须为 16..128")
    _parse_utc_datetime(_require_str(raw, "occurred_at"), "occurred_at")
    retry_count = raw.get("retry_count", 0)
    if not isinstance(retry_count, int) or isinstance(retry_count, bool) or retry_count < 0:
        raise EventContractViolation("retry_count 必须为非负整数")
    payload_raw = raw.get("payload")
    if not isinstance(payload_raw, dict):
        raise EventContractViolation("payload 必须为对象")
    payload_fields = {f.name for f in fields(payload_cls)}  # type: ignore[arg-type]
    unknown_payload = set(payload_raw) - payload_fields
    if unknown_payload:
        raise EventContractViolation(f"{event_type} 载荷存在未知字段: {sorted(unknown_payload)}")
    # 载荷字段全部为必填位置参数：缺失字段在构造时抛 TypeError，统一转为契约违规。
    try:
        payload = payload_cls(**payload_raw)  # type: ignore[call-arg]
    except TypeError as exc:
        raise EventContractViolation(f"{event_type} 载荷构造失败: {exc}") from exc
    return EventEnvelope(
        schema_version=raw["schema_version"],
        event_id=_require_str(raw, "event_id"),
        event_type=event_type,
        event_version=event_version,
        tenant_id=_require_str(raw, "tenant_id"),
        request_id=_require_str(raw, "request_id"),
        correlation_id=_require_str(raw, "correlation_id"),
        trace_id=_require_str(raw, "trace_id"),
        operation_id=raw.get("operation_id"),
        causation_id=raw.get("causation_id"),
        parent_event_id=raw.get("parent_event_id"),
        producer=_require_str(raw, "producer"),
        occurred_at=raw["occurred_at"],
        idempotency_key=idempotency_key,
        subject_id=raw.get("subject_id"),
        retry_count=retry_count,
        payload=payload,
    )
