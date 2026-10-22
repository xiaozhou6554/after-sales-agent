"""消费失败后的持久化重试与死信投递。"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace

from sqlalchemy.engine import Connection

from application.common.events import EventEnvelope, parse_event
from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.inbox_repository import (
    InboxConflictError,
    InboxRepository,
)
from infrastructure.messaging.rabbitmq import RabbitMQTransport

logger = logging.getLogger("after_sales.messaging")
EventHandler = Callable[[Connection, EventEnvelope], str | None]


class InboxConsumer:
    def __init__(
        self,
        repository: InboxRepository,
        transport: RabbitMQTransport,
        catalog: EventCatalog,
        handlers: dict[str, EventHandler],
        *,
        worker_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._transport = transport
        self._catalog = catalog
        self._handlers = handlers
        self._worker_id = worker_id or uuid.uuid4().hex

    def poll_once(self, queue: str) -> bool:
        delivery = self._transport.get(queue)
        if delivery is None:
            return False
        try:
            event = parse_event(json.loads(delivery.body))
            entry = self._catalog.validate(event)
            if entry["queue"] != queue or event.event_type not in self._handlers:
                raise ValueError("消息队列与已注册消费者不一致")
            consumer_name = entry["consumer"]
            try:
                outcome, lease = self._repository.claim(event, consumer_name, self._worker_id)
            except InboxConflictError:
                self._transport.publish_dead_letter(event, "相同事件标识的内容冲突")
                self._transport.ack(delivery)
                return True
            if outcome == "busy":
                self._transport.nack(delivery)
                return True
            if outcome == "duplicate":
                self._transport.ack(delivery)
                return True
            if outcome == "dead_letter":
                self._transport.publish_dead_letter(event, "消息处理次数已耗尽")
                self._transport.ack(delivery)
                return True
            if outcome == "retry_handoff":
                self._transport.publish_retry(replace(event, retry_count=event.retry_count + 1))
                self._transport.ack(delivery)
                return True
            assert lease is not None
            try:
                self._repository.complete(lease, self._handlers[event.event_type])
            except InboxConflictError as exc:
                status = self._repository.fail(lease, str(exc), fatal=True)
                logger.warning("消息业务键冲突: event_id=%s", event.event_id)
            except Exception as exc:  # noqa: BLE001 - 业务失败须落库并进入重试或死信
                status = self._repository.fail(lease, type(exc).__name__)
                logger.exception("消息消费失败: event_id=%s", event.event_id)
            else:
                self._transport.ack(delivery)
                return True
            if status == "dead_letter":
                self._transport.publish_dead_letter(event, "消费失败或业务键冲突")
            else:
                self._transport.publish_retry(replace(event, retry_count=lease.attempt))
            self._transport.ack(delivery)
            return True
        except Exception:
            self._transport.nack(delivery)
            raise
