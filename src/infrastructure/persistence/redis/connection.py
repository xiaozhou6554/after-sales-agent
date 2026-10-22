"""Redis 连接配置与就绪探测。

超时来自《技术详细规格》1.4（连接0.5秒/读取1秒）；Redis 仅保存短期会话，
不可用时的降级开关语义由应用层健康用例判定。
"""

from __future__ import annotations

import time

import redis

from application.common.ports.dependency_probe import ProbeResult


def create_redis_client(
    redis_url: str, connect_timeout_s: float, read_timeout_s: float
) -> redis.Redis:
    return redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=connect_timeout_s,
        socket_timeout=read_timeout_s,
        health_check_interval=30,
    )


class RedisDependencyProbe:
    name = "redis"

    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        self._client.ping()
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(name=self.name, ok=True, latency_ms=latency_ms)
