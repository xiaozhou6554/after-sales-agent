"""步骤15：真实 Redis 后台会话轮按任务标识幂等追加。"""

from __future__ import annotations

import uuid

import pytest
import redis
from bootstrap.settings import load_settings

from application.agent.ports.memory import MemoryPersistenceError
from infrastructure.agent.redis_session_repository import (
    RedisSessionRepository,
    _task_marker_key,
    session_key,
)


def test_background_turn_is_atomic_and_idempotent_across_instances() -> None:
    client = redis.Redis.from_url(load_settings().redis_url)
    client.ping()
    tenant = f"step15-redis-{uuid.uuid4().hex[:12]}"
    thread = f"thread-{uuid.uuid4().hex}"
    task = f"task-{uuid.uuid4().hex}"
    try:
        first = RedisSessionRepository(client)
        assert first.append_turn_once(tenant, thread, "customer-a", task, "查询订单", "订单已发货")
        restarted = RedisSessionRepository(client)
        assert not restarted.append_turn_once(
            tenant, thread, "customer-a", task, "查询订单", "订单已发货"
        )
        history = restarted.load(tenant, thread, subject_id="customer-a")
        assert [(item.role, item.content) for item in history] == [
            ("user", "查询订单"),
            ("assistant", "订单已发货"),
        ]
        with pytest.raises(MemoryPersistenceError):
            restarted.append_turn_once(tenant, thread, "customer-b", task, "越权", "越权")
        assert client.ttl(session_key(tenant, thread)) > 0
        assert client.ttl(_task_marker_key(tenant, thread)) > 0
    finally:
        client.delete(session_key(tenant, thread), _task_marker_key(tenant, thread))
        client.close()
