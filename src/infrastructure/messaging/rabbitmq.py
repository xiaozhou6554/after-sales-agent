"""RabbitMQ 连接参数与就绪探测。

超时来自《技术详细规格》1.4（连接1秒/读取2秒）；探测使用短连接，不在就绪检查
中保持长连接占用。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import pika

from application.common.events import EventEnvelope
from application.common.ports.dependency_probe import ProbeResult
from infrastructure.messaging.event_contract import EventCatalog


def parse_rabbitmq_url(url: str) -> dict[str, object]:
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5672,
        "virtual_host": (parsed.path[1:] if len(parsed.path) > 1 else "/") or "/",
        "username": parsed.username or "guest",
        "password": parsed.password or "guest",
    }


class RabbitMQDependencyProbe:
    name = "rabbitmq"

    def __init__(self, url: str, connect_timeout_s: float, read_timeout_s: float) -> None:
        params = parse_rabbitmq_url(url)
        self._credentials = pika.PlainCredentials(str(params["username"]), str(params["password"]))
        self._host = str(params["host"])
        self._port = int(str(params["port"]))
        self._virtual_host = str(params["virtual_host"])
        self._connect_timeout_s = connect_timeout_s
        self._read_timeout_s = read_timeout_s

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        connection = pika.BlockingConnection(
            pika.ConnectionParameters(
                host=self._host,
                port=self._port,
                virtual_host=self._virtual_host,
                credentials=self._credentials,
                socket_timeout=self._read_timeout_s,
                stack_timeout=self._connect_timeout_s * 2,
                connection_attempts=1,
            )
        )
        try:
            connection.close()
        except pika.exceptions.AMQPError:
            pass
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(name=self.name, ok=True, latency_ms=latency_ms)


@dataclass(frozen=True)
class Delivery:
    tag: int
    body: bytes
    headers: dict | None = None


class RabbitMQTransport:
    """持久拓扑和带 confirm 的发布；消费 ACK 由调用方在事务提交后执行。"""

    def __init__(self, url: str, catalog: EventCatalog, *, retry_delay_ms: int = 5000) -> None:
        self._catalog = catalog
        parameters = pika.URLParameters(url)
        parameters.connection_attempts = 1
        parameters.socket_timeout = 2
        parameters.blocked_connection_timeout = 2
        self._connection = pika.BlockingConnection(parameters)
        self._channel = self._connection.channel()
        self._channel.confirm_delivery()
        self._declare_topology(retry_delay_ms)

    def _declare_topology(self, retry_delay_ms: int) -> None:
        exchange = self._catalog.exchange
        self._channel.exchange_declare(exchange=exchange, exchange_type="direct", durable=True)
        self._channel.exchange_declare(
            exchange=f"{exchange}.retry", exchange_type="direct", durable=True
        )
        self._channel.queue_declare(queue=self._catalog.dead_letter_queue, durable=True)
        for queue in self._catalog.queues:
            self._channel.queue_declare(
                queue=queue,
                durable=True,
                arguments={
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": self._catalog.dead_letter_queue,
                },
            )
            self._channel.queue_bind(exchange=exchange, queue=queue, routing_key=queue)
            retry_queue = f"{queue}.retry"
            self._channel.queue_declare(
                queue=retry_queue,
                durable=True,
                arguments={
                    "x-message-ttl": retry_delay_ms,
                    "x-dead-letter-exchange": exchange,
                    "x-dead-letter-routing-key": queue,
                },
            )
            self._channel.queue_bind(
                exchange=f"{exchange}.retry", queue=retry_queue, routing_key=queue
            )
        for event_type, entry in self._catalog.entries.items():
            self._channel.queue_bind(
                exchange=exchange, queue=entry["queue"], routing_key=event_type
            )

    def publish_event(self, event: EventEnvelope) -> None:
        self._catalog.validate(event)
        self._publish(
            exchange=self._catalog.exchange,
            routing_key=event.event_type,
            event=event,
        )

    def publish_retry(self, event: EventEnvelope) -> None:
        entry = self._catalog.validate(event)
        self._publish(
            exchange=f"{self._catalog.exchange}.retry",
            routing_key=entry["queue"],
            event=event,
        )

    def publish_dead_letter(self, event: EventEnvelope, reason: str) -> None:
        self._publish(
            exchange="",
            routing_key=self._catalog.dead_letter_queue,
            event=event,
            headers={
                "failure_reason": reason[:256],
                "original_event_id": event.event_id,
                "idempotency_key": event.idempotency_key,
            },
        )

    def _publish(
        self,
        *,
        exchange: str,
        routing_key: str,
        event: EventEnvelope,
        headers: dict | None = None,
    ) -> None:
        self._channel.basic_publish(
            exchange=exchange,
            routing_key=routing_key,
            body=json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"),
            mandatory=True,
            properties=pika.BasicProperties(
                delivery_mode=2,
                content_type="application/json",
                message_id=event.event_id,
                headers=headers or {},
            ),
        )

    def get(self, queue: str) -> Delivery | None:
        method, properties, body = self._channel.basic_get(queue=queue, auto_ack=False)
        if method is None:
            return None
        return Delivery(method.delivery_tag, body, properties.headers or {})

    def ack(self, delivery: Delivery) -> None:
        self._channel.basic_ack(delivery_tag=delivery.tag)

    def nack(self, delivery: Delivery) -> None:
        self._channel.basic_nack(delivery_tag=delivery.tag, requeue=True)

    def close(self) -> None:
        if self._connection.is_open:
            self._connection.close()
