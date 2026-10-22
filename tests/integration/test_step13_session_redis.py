"""步骤13：真实 Redis 的摘要覆盖、并发保护与实例恢复。"""

from __future__ import annotations

import uuid

import pytest
import redis
from bootstrap.settings import load_settings

from application.agent.ports.llm import ChatMessage, LLMResult
from application.memory.dto.session import SummaryConflictError
from application.memory.use_cases.session_context import SessionContextUseCase
from infrastructure.agent.redis_session_repository import RedisSessionRepository, session_key


class SummaryLLM:
    def complete(self, *, system, messages, tools=None):
        return LLMResult(content="用户偏好黑色产品")


@pytest.fixture()
def session_case():
    settings = load_settings()
    client = redis.Redis.from_url(settings.redis_url)
    client.ping()
    thread_id = f"thread-step13-{uuid.uuid4().hex}"
    yield RedisSessionRepository(client), thread_id, client
    client.delete(session_key("tenant-step13", thread_id))
    client.close()


def test_summary_persists_across_instances_and_rejects_stale_coverage(session_case) -> None:
    repository, thread_id, client = session_case
    for number in range(7):
        repository.append(
            "tenant-step13", thread_id, "customer-a", ChatMessage("user", f"问题{number}")
        )
        repository.append(
            "tenant-step13", thread_id, "customer-a", ChatMessage("assistant", f"回答{number}")
        )
    use_case = SessionContextUseCase(repository, SummaryLLM())
    history = use_case.history("tenant-step13", thread_id, "customer-a")
    assert len(history) == 9
    restored = RedisSessionRepository(client).load_snapshot(
        "tenant-step13", thread_id, subject_id="customer-a"
    )
    assert restored.covered_until == 6
    assert restored.summary == "用户偏好黑色产品"
    with pytest.raises(SummaryConflictError):
        repository.save_summary(
            "tenant-step13",
            thread_id,
            subject_id="customer-a",
            expected_covered_until=0,
            covered_until=8,
            summary="过期摘要",
        )
    assert (
        repository.load_snapshot("tenant-step13", thread_id, subject_id="customer-b").summary == ""
    )
