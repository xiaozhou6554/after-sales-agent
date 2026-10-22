"""发布所属模块 Outbox；收到 RabbitMQ confirm 后才标记已发布。"""

from __future__ import annotations

import logging

from infrastructure.messaging.event_contract import EventCatalog
from infrastructure.messaging.outbox_repository import OutboxRepository

logger = logging.getLogger("after_sales.messaging")


class OutboxPublisher:
    def __init__(self, repository: OutboxRepository, transport, catalog: EventCatalog) -> None:
        self._repository = repository
        self._transport = transport
        self._catalog = catalog

    def publish_pending(
        self, owner_module: str, tenant_id: str, worker_id: str, *, limit: int = 20
    ) -> int:
        published = 0
        for lease in self._repository.claim(owner_module, tenant_id, worker_id, limit=limit):
            try:
                self._catalog.validate(lease.event)
                self._transport.publish_event(lease.event)
            except Exception:  # noqa: BLE001 - 发布确认未知时保留待重试记录
                logger.exception("Outbox 发布失败: event_id=%s", lease.event.event_id)
                self._repository.mark_retry(lease)
                continue
            self._repository.mark_published(lease)
            published += 1
        return published
