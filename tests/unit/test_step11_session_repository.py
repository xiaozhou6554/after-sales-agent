"""步骤11 会话仓储单元测试：隔离、TTL、脱敏、降级与失败语义。

Redis 仓储用进程内 Fake 双向测试（行为等价 hget/hset/hgetall/expire）；
与真实 Redis 的连通行为由集成测试覆盖。
"""

from __future__ import annotations

import json

import pytest
import redis as redis_lib

from application.agent.ports.llm import ChatMessage
from application.agent.ports.memory import (
    MemoryPersistenceError,
    MemoryUnavailableError,
)
from infrastructure.agent.memory_adapter import InMemorySessionRepository
from infrastructure.agent.redis_session_repository import (
    SESSION_TTL_S,
    FallbackSessionRepository,
    RedisSessionRepository,
    session_key,
)

TENANT = "tenant-north"
THREAD = "thread-u1"
SUBJECT = "customer-north-01"


class FakeRedis:
    """最小 Hash 语义双向：记录 expire 调用以验证 TTL 刷新规则。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: list[tuple[str, int]] = []
        self.fail = False

    def hget(self, key, field):
        if self.fail:
            raise redis_lib.RedisError("connection lost")
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, mapping=None):
        if self.fail:
            raise redis_lib.RedisError("connection lost")
        self.hashes.setdefault(key, {}).update(
            {k: v if isinstance(v, str) else v.decode() for k, v in (mapping or {}).items()}
        )

    def hgetall(self, key):
        if self.fail:
            raise redis_lib.RedisError("connection lost")
        return dict(self.hashes.get(key, {}))

    def expire(self, key, ttl):
        if self.fail:
            raise redis_lib.RedisError("connection lost")
        self.expires.append((key, ttl))
        return True


# ---------- InMemorySessionRepository（显式降级实现） ----------


def test_memory_repository_roundtrip_and_isolation() -> None:
    repo = InMemorySessionRepository()
    repo.append(TENANT, THREAD, SUBJECT, ChatMessage(role="user", content="你好"))
    repo.append(TENANT, THREAD, SUBJECT, ChatMessage(role="assistant", content="在的"))
    assert [(m.role, m.content) for m in repo.load(TENANT, THREAD, subject_id=SUBJECT)] == [
        ("user", "你好"),
        ("assistant", "在的"),
    ]
    # 跨主体/跨租户不串历史
    assert repo.load(TENANT, THREAD, subject_id="someone-else") == []
    assert repo.load("tenant-south", THREAD, subject_id=SUBJECT) == []


def test_memory_repository_rejects_foreign_subject_append() -> None:
    repo = InMemorySessionRepository()
    repo.append(TENANT, THREAD, SUBJECT, ChatMessage(role="user", content="你好"))
    with pytest.raises(MemoryPersistenceError):
        repo.append(TENANT, THREAD, "intruder", ChatMessage(role="user", content="插话"))


def test_memory_repository_trims_to_capacity() -> None:
    repo = InMemorySessionRepository(max_messages=4)
    for index in range(6):
        repo.append(TENANT, THREAD, SUBJECT, ChatMessage(role="user", content=f"m{index}"))
    history = repo.load(TENANT, THREAD, subject_id=SUBJECT)
    assert [m.content for m in history] == ["m2", "m3", "m4", "m5"]


def test_memory_repository_masks_sensitive_content() -> None:
    repo = InMemorySessionRepository()
    repo.append(
        TENANT, THREAD, SUBJECT, ChatMessage(role="user", content="手机号 13812345678 找人")
    )
    history = repo.load(TENANT, THREAD, subject_id=SUBJECT)
    assert "13812345678" not in history[0].content
    assert "***" in history[0].content


# ---------- RedisSessionRepository（Key/TTL/脱敏/摘要） ----------


def test_redis_repository_key_format_ttl_refresh_and_roundtrip() -> None:
    fake = FakeRedis()
    repo = RedisSessionRepository(fake)
    repo.append(TENANT, THREAD, SUBJECT, ChatMessage(role="user", content="你好"))
    assert session_key(TENANT, THREAD) in fake.hashes
    assert fake.expires[-1] == (session_key(TENANT, THREAD), SESSION_TTL_S)
    repo.append(
        TENANT,
        THREAD,
        SUBJECT,
        ChatMessage(role="assistant", content="已查到"),
        tool_summaries=({"tool_name": "query_order", "status": "success"},),
    )
    repo.load(TENANT, THREAD, subject_id=SUBJECT)
    # 读写成功都刷新 TTL（11.2：每次读写成功后刷新）
    assert [key for key, _ in fake.expires].count(session_key(TENANT, THREAD)) == 3
    assert all(ttl == SESSION_TTL_S for _, ttl in fake.expires)


def test_redis_repository_stores_masked_text_and_bounded_summaries() -> None:
    fake = FakeRedis()
    repo = RedisSessionRepository(fake)
    repo.append(
        TENANT,
        THREAD,
        SUBJECT,
        ChatMessage(role="user", content="回拨 13998887766"),
        tool_summaries=({"tool_name": "query_order", "status": "error", "error_code": "TIMEOUT"},),
    )
    stored = fake.hashes[session_key(TENANT, THREAD)]
    messages = json.loads(stored["messages"])
    assert "13998887766" not in messages[0]["content"]
    summaries = json.loads(stored["tool_summaries"])
    assert summaries[0][0]["tool_name"] == "query_order"
    assert "arguments" not in summaries[0][0]
    assert stored["subject_id"] == SUBJECT and stored["thread_id"] == THREAD
    assert "updated_at" in stored and stored["last_agent"] == "supervisor"


def test_redis_repository_subject_isolation_and_semantic_rejection() -> None:
    fake = FakeRedis()
    repo = RedisSessionRepository(fake)
    repo.append(TENANT, THREAD, SUBJECT, ChatMessage(role="user", content="你好"))
    # 他人读取视同无历史；写入语义拒绝（不是传输失败，降级不吸收）。
    assert repo.load(TENANT, THREAD, subject_id="other-subject") == []
    with pytest.raises(MemoryPersistenceError) as exc:
        repo.append(TENANT, THREAD, "other-subject", ChatMessage(role="user", content="插话"))
    assert not isinstance(exc.value, MemoryUnavailableError)


def test_redis_repository_trims_oldest_beyond_capacity() -> None:
    fake = FakeRedis()
    repo = RedisSessionRepository(fake, max_messages=4)
    for index in range(6):
        repo.append(TENANT, THREAD, SUBJECT, ChatMessage(role="user", content=f"m{index}"))
    history = repo.load(TENANT, THREAD, subject_id=SUBJECT)
    assert [m.content for m in history] == ["m2", "m3", "m4", "m5"]


def test_redis_repository_transport_failure_maps_to_unavailable() -> None:
    fake = FakeRedis()
    repo = RedisSessionRepository(fake)
    fake.fail = True
    with pytest.raises(MemoryUnavailableError):
        repo.load(TENANT, THREAD, subject_id=SUBJECT)
    with pytest.raises(MemoryUnavailableError):
        repo.append(TENANT, THREAD, SUBJECT, ChatMessage(role="user", content="你好"))


# ---------- FallbackSessionRepository（显式降级开关语义） ----------


def test_fallback_absorbs_transport_failure_only_when_allowed() -> None:
    broken = FakeRedis()
    broken.fail = True
    message = ChatMessage(role="user", content="你好")

    allowed = FallbackSessionRepository(
        RedisSessionRepository(broken), InMemorySessionRepository(), fallback_allowed=True
    )
    allowed.append(TENANT, THREAD, SUBJECT, message)
    assert [m.content for m in allowed.load(TENANT, THREAD, subject_id=SUBJECT)] == ["你好"]

    strict = FallbackSessionRepository(
        RedisSessionRepository(broken), InMemorySessionRepository(), fallback_allowed=False
    )
    with pytest.raises(MemoryUnavailableError):
        strict.append(TENANT, THREAD, SUBJECT, message)
    with pytest.raises(MemoryUnavailableError):
        strict.load(TENANT, THREAD, subject_id=SUBJECT)


def test_fallback_does_not_absorb_semantic_rejection() -> None:
    repo = InMemorySessionRepository()
    repo.append(TENANT, THREAD, SUBJECT, ChatMessage(role="user", content="你好"))
    wrapper = FallbackSessionRepository(repo, InMemorySessionRepository(), fallback_allowed=True)
    with pytest.raises(MemoryPersistenceError):
        wrapper.append(TENANT, THREAD, "intruder", ChatMessage(role="user", content="插话"))
    # 降级存储未被污染：拒绝的写入不得落入兜底仓储
    assert wrapper.load(TENANT, THREAD, subject_id="intruder") == []


# ---------- 容器装配（bootstrap 只组装，不承载规则） ----------


def test_build_session_repository_wiring_follows_config() -> None:
    from bootstrap.container import build_session_repository
    from bootstrap.settings import RuntimeSettings

    default_repo = build_session_repository(RuntimeSettings())
    assert isinstance(default_repo, RedisSessionRepository)
    default_repo.close()

    degraded_repo = build_session_repository(RuntimeSettings(memory_fallback_enabled=True))
    assert isinstance(degraded_repo, FallbackSessionRepository)
    assert isinstance(degraded_repo._fallback, InMemorySessionRepository)
    degraded_repo.close()


# ---------- ChatService 记忆失败收束（对话上下文不得静默丢弃） ----------


class _ConflictingMemory:
    """语义冲突：会话已归属其他主体。"""

    def append(self, tenant_id, thread_id, subject_id, message, *, tool_summaries=()):
        raise MemoryPersistenceError("会话主体不匹配，拒绝写入他人会话")

    def load(self, tenant_id, thread_id, *, subject_id):
        return []


class _BrokenMemory:
    """传输不可用：仓储连接失败。"""

    def append(self, tenant_id, thread_id, subject_id, message, *, tool_summaries=()):
        raise MemoryUnavailableError("Redis 会话写入失败")

    def load(self, tenant_id, thread_id, *, subject_id):
        raise MemoryUnavailableError("Redis 会话读取失败")


def _chat_service_with_memory(memory):
    from pathlib import Path

    from application.agent.prompts import PromptCatalog
    from application.agent.service import ChatService

    project_root = Path(__file__).resolve().parents[2]

    class _FixedLLM:
        def complete(self, *, system, messages, tools=None):
            from application.agent.ports.llm import LLMResult

            return LLMResult(content="好的。", model="fake")

    return ChatService(
        llm=_FixedLLM(),
        tools=object(),
        memory=memory,
        prompts=PromptCatalog.load(project_root / "data" / "prompts"),
        registry_path=project_root / "data" / "contracts" / "mcp-tools.json",
    )


def _execute_once(service):
    from datetime import datetime, timezone

    from application.common.context import AuthContext, DataScope, TraceContext
    from application.common.dto import ChatRequestDto

    auth = AuthContext(
        tenant_id=TENANT,
        subject_id=SUBJECT,
        roles=("customer",),
        scopes=("order:read",),
        data_scope=DataScope(customer_ids=("CUSTOMER-01",)),
        session_id="sess-1",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    trace = TraceContext(
        schema_version="1.0",
        request_id="req-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        thread_id=None,
    )
    return service.execute(
        ChatRequestDto(trace=trace, message="你好", thread_id=THREAD), auth, session_token="t"
    )


def test_chat_subject_conflict_maps_to_non_retryable_conflict() -> None:
    response = _execute_once(_chat_service_with_memory(_ConflictingMemory()))
    assert response.status == "error"
    assert response.error.error_code == "RESOURCE_UNAVAILABLE"
    assert response.error.retryable is False


def test_chat_memory_transport_failure_maps_to_retryable_dependency_error() -> None:
    response = _execute_once(_chat_service_with_memory(_BrokenMemory()))
    assert response.status == "error"
    assert response.error.error_code == "DEPENDENCY_UNAVAILABLE"
    assert response.error.retryable is True
