"""步骤11 会话仓储集成测试：真实 Redis（docker after-sales-redis:6379）。

验收口径（项目开发流程 步骤11）：相同授权会话连续对话；跨租户/跨客户
不串历史；实例切换（两个独立仓储实例）读同一 Redis 数据；显式降级不
宣称持久化。需要本地 docker Redis。
"""

from __future__ import annotations

import uuid

import pytest
import redis as redis_lib
from bootstrap.settings import load_settings

from application.agent.ports.llm import ChatMessage, LLMResult
from application.agent.ports.memory import MemoryUnavailableError
from infrastructure.agent.memory_adapter import InMemorySessionRepository
from infrastructure.agent.redis_session_repository import (
    SESSION_TTL_S,
    FallbackSessionRepository,
    RedisSessionRepository,
    session_key,
)

TENANT = "tenant-north"
SUBJECT = "customer-north-01"
OTHER_SUBJECT = "customer-north-02"
KEY_NAMESPACE = f"thread-it11-{uuid.uuid4().hex[:8]}"


def _thread(tag: str) -> str:
    return f"{KEY_NAMESPACE}-{tag}"


@pytest.fixture(scope="module")
def redis_client():
    settings = load_settings()
    client = redis_lib.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_connect_timeout_s,
        socket_timeout=settings.redis_read_timeout_s,
    )
    client.ping()
    yield client
    for key in client.scan_iter(f"session:{TENANT}:{KEY_NAMESPACE}*"):
        client.delete(key)
    client.close()


def test_roundtrip_ttl_and_instance_switch(redis_client) -> None:
    first = RedisSessionRepository(redis_client)
    thread = _thread("roundtrip")
    first.append(TENANT, thread, SUBJECT, ChatMessage(role="user", content="你好"))
    first.append(
        TENANT,
        thread,
        SUBJECT,
        ChatMessage(role="assistant", content="您好，请问有什么可以帮您？"),
        tool_summaries=({"tool_name": "query_order", "status": "success"},),
    )
    # 实例切换：另一个独立仓储实例读同一 Redis 数据。
    second = RedisSessionRepository(redis_client)
    history = second.load(TENANT, thread, subject_id=SUBJECT)
    assert [(m.role, m.content) for m in history] == [
        ("user", "你好"),
        ("assistant", "您好，请问有什么可以帮您？"),
    ]
    ttl = redis_client.ttl(session_key(TENANT, thread))
    assert SESSION_TTL_S - 5 <= ttl <= SESSION_TTL_S


def test_cross_tenant_and_cross_subject_isolation(redis_client) -> None:
    repo = RedisSessionRepository(redis_client)
    thread = _thread("iso")
    repo.append(TENANT, thread, SUBJECT, ChatMessage(role="user", content="我的历史"))
    # 同 thread_id 不同租户：完全隔离（Key 含 tenant_id）。
    assert repo.load("tenant-south", thread, subject_id="customer-south-91") == []
    # 同租户不同主体：视同无历史，不泄露他人上下文。
    assert repo.load(TENANT, thread, subject_id=OTHER_SUBJECT) == []


def test_sensitive_content_masked_in_real_redis(redis_client) -> None:
    repo = RedisSessionRepository(redis_client)
    thread = _thread("mask")
    repo.append(TENANT, thread, SUBJECT, ChatMessage(role="user", content="联系电话 13812345678"))
    raw = redis_client.hget(session_key(TENANT, thread), "messages").decode("utf-8")
    assert "13812345678" not in raw and "***" in raw


def test_unreachable_redis_fails_closed_unless_fallback_enabled() -> None:
    broken = redis_lib.Redis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2
    )
    strict = RedisSessionRepository(broken)
    with pytest.raises(MemoryUnavailableError):
        strict.load(TENANT, _thread("dead"), subject_id=SUBJECT)
    wrapped = FallbackSessionRepository(strict, InMemorySessionRepository(), fallback_allowed=True)
    wrapped.append(TENANT, _thread("dead"), SUBJECT, ChatMessage(role="user", content="降级"))
    assert [m.content for m in wrapped.load(TENANT, _thread("dead"), subject_id=SUBJECT)] == [
        "降级"
    ]
    broken.close()


def test_chat_service_continuity_through_real_redis(redis_client) -> None:
    from datetime import datetime, timezone
    from pathlib import Path

    from application.agent.prompts import PromptCatalog
    from application.agent.service import ChatService
    from application.common.context import AuthContext, DataScope, TraceContext
    from application.common.dto import ChatRequestDto

    project_root = Path(__file__).resolve().parents[2]

    class ScriptedLLM:
        def __init__(self, results) -> None:
            self._results = list(results)
            self.calls: list[list[ChatMessage]] = []

        def complete(self, *, system, messages, tools=None):
            self.calls.append(list(messages))
            return self._results.pop(0)

    llm = ScriptedLLM(
        [
            LLMResult(content="您好，请问有什么可以帮您？", model="fake"),
            LLMResult(content="好的，我明白了。", model="fake"),
        ]
    )

    class NoTools:
        def execute_tool(self, *args, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("寒暄不应调用工具")

    service = ChatService(
        llm=llm,
        tools=NoTools(),
        memory=RedisSessionRepository(redis_client),
        prompts=PromptCatalog.load(project_root / "data" / "prompts"),
        registry_path=project_root / "data" / "contracts" / "mcp-tools.json",
    )
    auth = AuthContext(
        tenant_id=TENANT,
        subject_id=SUBJECT,
        roles=("customer",),
        scopes=("order:read",),
        data_scope=DataScope(customer_ids=("CUSTOMER-01",)),
        session_id="sess-it11",
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    trace = TraceContext(
        schema_version="1.0",
        request_id="req-it11",
        correlation_id="corr-it11",
        trace_id="trace-it11",
        thread_id=None,
    )
    thread = _thread("chat")
    first = service.execute(
        ChatRequestDto(trace=trace, message="第一句", thread_id=thread),
        auth,
        session_token="token-it11",
    )
    assert first.status == "success"
    second = service.execute(
        ChatRequestDto(trace=trace, message="第二句", thread_id=thread),
        auth,
        session_token="token-it11",
    )
    assert second.status == "success"
    second_turn_messages = llm.calls[1]
    assert any(m.role == "user" and m.content == "第一句" for m in second_turn_messages)
    assert any(
        m.role == "assistant" and m.content == "您好，请问有什么可以帮您？"
        for m in second_turn_messages
    )
