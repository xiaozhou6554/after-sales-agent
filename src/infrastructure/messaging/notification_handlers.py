"""步骤28：通知发送消费者与业务事件的通知扇出。

模块边界：扇出消费者只写共享 Outbox（事件链），不直接写通知表；
通知表只由 notification-consumer 在自己的消费事务内写入。
通知发送失败/超时只改变通知状态，不回滚已成功的业务，也不触发重试风暴。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from application.common.events import (
    EventEnvelope,
    NotificationCreatedPayload,
)
from infrastructure.messaging.inbox_repository import InboxConflictError
from infrastructure.messaging.outbox_repository import OutboxRepository

_SYSTEM_NOTIFICATION_PRODUCER = "notification-service"


class MockNotificationSender:
    """Mock 通知渠道：默认发送成功；可注入拒绝(failed)或超时未确认(unknown)。"""

    def __init__(self, *, behavior: str = "sent") -> None:
        if behavior not in ("sent", "failed", "unknown"):
            raise ValueError("Mock 通知渠道行为必须为 sent/failed/unknown")
        self._behavior = behavior

    def send(
        self, tenant_id: str, notification_id: str, channel: str, template_id: str
    ) -> tuple[str, str | None]:
        if self._behavior == "failed":
            return "failed", "MOCK_CHANNEL_REJECTED"
        if self._behavior == "unknown":
            return "unknown", "MOCK_CHANNEL_TIMEOUT"
        return "sent", None


class NotificationSendHandler:
    """notification.created 消费者：同事务落通知记录并发送，失败只记状态。"""

    def __init__(self, sender: MockNotificationSender) -> None:
        self._sender = sender

    def __call__(self, connection: Connection, event: EventEnvelope) -> str:
        if event.event_type != "notification.created" or not isinstance(
            event.payload, NotificationCreatedPayload
        ):
            raise InboxConflictError("通知消费者收到错误事件类型")
        payload = event.payload
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        inserted = connection.execute(
            text(
                "INSERT INTO notification (tenant_id,notification_id,customer_id,channel,"
                "template_id,resource_refs,actor_subject_id,status,error,sent_at,"
                "created_at,updated_at) VALUES (:tenant_id,:notification_id,:customer_id,"
                ":channel,:template_id,:resource_refs,:actor_subject_id,'pending',NULL,NULL,"
                ":now,:now) ON DUPLICATE KEY UPDATE notification_id=notification_id"
            ),
            {
                "tenant_id": event.tenant_id,
                "notification_id": payload.notification_id,
                "customer_id": payload.customer_id,
                "channel": payload.channel,
                "template_id": payload.template_id,
                "resource_refs": _json_refs(payload.resource_refs),
                "actor_subject_id": payload.actor_subject_id,
                "now": now,
            },
        )
        if inserted.rowcount == 0:
            # 同通知ID重投：已存在记录则不再重复发送，保留原状态。
            return f"notification:{payload.notification_id}:duplicate"
        status, error = self._sender.send(
            event.tenant_id, payload.notification_id, payload.channel, payload.template_id
        )
        connection.execute(
            text(
                "UPDATE notification SET status=:status,error=:error,"
                "sent_at=:sent_at,updated_at=:now WHERE tenant_id=:tenant_id"
                " AND notification_id=:notification_id"
            ),
            {
                "status": status,
                "error": error,
                "sent_at": now if status in ("sent", "unknown") else None,
                "now": now,
                "tenant_id": event.tenant_id,
                "notification_id": payload.notification_id,
            },
        )
        return f"notification:{payload.notification_id}:{status}"


class NotificationFanoutHandler:
    """业务事件 → notification.created 事件链扇出；只写 Outbox，不写通知表。"""

    VERIFY: dict[str, tuple[str, str]] = {
        # event_type -> (存在性校验SQL, 源模块表)
        "order.shipping_address.updated": (
            "SELECT address_change_id FROM mock_oms_address_change WHERE tenant_id=:tenant_id"
            " AND address_change_id=:entity_id AND status='succeeded'",
            "mock_oms_address_change",
        ),
        "ticket.created": (
            "SELECT ticket_id FROM ticket WHERE tenant_id=:tenant_id AND ticket_id=:entity_id",
            "ticket",
        ),
        "ticket.updated": (
            "SELECT ticket_id FROM ticket WHERE tenant_id=:tenant_id AND ticket_id=:entity_id",
            "ticket",
        ),
        "human.handoff.created": (
            "SELECT handoff_id FROM human_handoff WHERE tenant_id=:tenant_id"
            " AND handoff_id=:entity_id",
            "human_handoff",
        ),
        "refund.approved": (
            "SELECT refund_request_id FROM refund_request WHERE tenant_id=:tenant_id"
            " AND refund_request_id=:entity_id AND review_status='approved'",
            "refund_request",
        ),
    }

    TEMPLATES: dict[str, tuple[str, str]] = {
        # event_type -> (template_id, channel)
        "order.shipping_address.updated": ("address_change_confirmed", "mock_sms"),
        "ticket.created": ("ticket_created", "mock_in_app"),
        "ticket.updated": ("ticket_updated", "mock_in_app"),
        "human.handoff.created": ("handoff_created", "mock_in_app"),
        "refund.approved": ("refund_approved", "mock_sms"),
    }

    def __init__(self, outbox: OutboxRepository) -> None:
        self._outbox = outbox

    def _entity_and_refs(self, event: EventEnvelope) -> tuple[str, list[str]]:
        payload = event.payload_dict()
        if event.event_type == "order.shipping_address.updated":
            refs = [payload["order_id"], payload["address_change_id"]]
            return payload["address_change_id"], refs
        if event.event_type in ("ticket.created", "ticket.updated"):
            ticket_id = payload["ticket_id"]
            if not ticket_id:
                raise InboxConflictError("工单事件缺少工单号，无法扇出通知")
            return ticket_id, [ticket_id]
        if event.event_type == "human.handoff.created":
            return payload["handoff_id"], [payload["handoff_id"]]
        return payload["refund_request_id"], [payload["refund_request_id"]]

    def __call__(self, connection: Connection, event: EventEnvelope) -> str:
        verify = self.VERIFY.get(event.event_type)
        template = self.TEMPLATES.get(event.event_type)
        if verify is None or template is None:
            raise InboxConflictError(f"事件 {event.event_type} 未登记通知扇出")
        entity_id, resource_refs = self._entity_and_refs(event)
        source = connection.execute(
            text(verify[0]),
            {"tenant_id": event.tenant_id, "entity_id": entity_id},
        ).first()
        if source is None:
            raise InboxConflictError(f"扇出通知前源记录不存在或状态不符: {verify[1]}")
        template_id, channel = template
        payload = event.payload_dict()
        notification_id = uuid.uuid5(
            uuid.NAMESPACE_OID, f"{event.tenant_id}:{event.event_type}:{event.idempotency_key}"
        ).hex
        notification_event = EventEnvelope(
            schema_version="1.0",
            event_id=uuid.uuid4().hex,
            event_type="notification.created",
            event_version=1,
            tenant_id=event.tenant_id,
            request_id=event.request_id,
            correlation_id=event.correlation_id,
            trace_id=event.trace_id,
            operation_id=event.operation_id,
            causation_id=event.event_id,
            parent_event_id=event.event_id,
            producer=_SYSTEM_NOTIFICATION_PRODUCER,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            idempotency_key=f"notification-created:{event.idempotency_key}",
            subject_id=_SYSTEM_NOTIFICATION_PRODUCER,
            retry_count=0,
            payload=NotificationCreatedPayload(
                notification_id=notification_id,
                channel=channel,
                template_id=template_id,
                resource_refs=resource_refs,
                actor_subject_id=payload["actor_subject_id"],
                customer_id=payload.get("customer_id"),
            ),
        )
        self._outbox.enqueue(
            connection,
            notification_event,
            aggregate_type="notification",
            aggregate_id=notification_id,
        )
        return f"notification_fanout:{notification_id}"


def _json_refs(refs: list[str]) -> str:
    import json

    return json.dumps(refs, ensure_ascii=False)
