"""Redis 会话仓储（技术详细规格 11.2，流程步骤11）。

- Key 统一为 ``session:{tenant_id}:{thread_id}``，全库按租户隔离；
- Hash 字段：subject_id/thread_id/messages/tool_summaries/updated_at/last_agent；
- 内容经脱敏写入（联系方式/卡号/证件号等定值掩码）；工具摘要只保留
  名称/状态/错误码，不含参数与原文；
- 每次读写成功后刷新 TTL（259200 秒，1.4 全局参数表）；
- 主体归属校验：他人会话读取视同无历史，写入直接拒绝；
- redis 传输异常统一折算为 MemoryUnavailableError，语义拒绝保持
  MemoryPersistenceError，供降级包装器区分处理。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import redis

from application.agent.ports.llm import ChatMessage
from application.agent.ports.memory import (
    MemoryPersistenceError,
    MemoryUnavailableError,
)
from application.common.masking import mask_mapping, mask_text
from application.memory.dto.session import SessionSnapshot, SummaryConflictError

logger = logging.getLogger("after_sales.memory")

SESSION_TTL_S = 259200  # 3 天（技术详细规格 1.4 业务约束）


def session_key(tenant_id: str, thread_id: str) -> str:
    return f"session:{tenant_id}:{thread_id}"


def _task_marker_key(tenant_id: str, thread_id: str) -> str:
    return f"session_task:{tenant_id}:{thread_id}"


def _decode(raw) -> str:
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


class RedisSessionRepository:
    """SessionRepositoryPort 的 Redis 实现；生产主仓储。"""

    def __init__(
        self,
        client: redis.Redis,
        *,
        session_ttl_s: int = SESSION_TTL_S,
        max_messages: int = 20,
    ) -> None:
        self._client = client
        self._ttl_s = session_ttl_s
        self._max_messages = max_messages

    def append(
        self,
        tenant_id: str,
        thread_id: str,
        subject_id: str,
        message: ChatMessage,
        *,
        tool_summaries: tuple[dict, ...] = (),
    ) -> None:
        key = session_key(tenant_id, thread_id)
        try:
            existing_subject = self._client.hget(key, "subject_id")
            if existing_subject is not None and _decode(existing_subject) != subject_id:
                # 语义拒绝：不覆盖他人会话，也不落入降级存储。
                raise MemoryPersistenceError("会话主体不匹配，拒绝写入他人会话")
            messages_raw = self._client.hget(key, "messages")
            summaries_raw = self._client.hget(key, "tool_summaries")
            offset_raw = self._client.hget(key, "message_offset")
            offset = int(_decode(offset_raw)) if offset_raw else 0
            messages = json.loads(_decode(messages_raw)) if messages_raw else []
            summaries = json.loads(_decode(summaries_raw)) if summaries_raw else []
            messages.append({"role": message.role, "content": mask_text(message.content)})
            summaries.append(mask_mapping([dict(item) for item in tool_summaries]))
            if len(messages) > self._max_messages:
                overflow = len(messages) - self._max_messages
                messages = messages[overflow:]
                summaries = summaries[overflow:]
                offset += overflow
            now = datetime.now(timezone.utc).isoformat()
            self._client.hset(
                key,
                mapping={
                    "subject_id": subject_id,
                    "thread_id": thread_id,
                    "messages": json.dumps(messages, ensure_ascii=False),
                    "tool_summaries": json.dumps(summaries, ensure_ascii=False),
                    "updated_at": now,
                    "last_agent": "supervisor",
                    "message_offset": str(offset),
                },
            )
            self._client.expire(key, self._ttl_s)
        except redis.RedisError as exc:
            raise MemoryUnavailableError(f"Redis 会话写入失败: {exc}") from exc

    def append_turn_once(
        self,
        tenant_id: str,
        thread_id: str,
        subject_id: str,
        task_id: str,
        question: str,
        answer: str,
        *,
        tool_summaries=(),
    ) -> bool:
        key = session_key(tenant_id, thread_id)
        marker = _task_marker_key(tenant_id, thread_id)
        messages = json.dumps(
            [
                {"role": "user", "content": mask_text(question)},
                {"role": "assistant", "content": mask_text(answer)},
            ],
            ensure_ascii=False,
        )
        summaries = json.dumps(
            [[], mask_mapping([dict(item) for item in tool_summaries])], ensure_ascii=False
        )
        script = """
        local subject = redis.call('HGET', KEYS[1], 'subject_id')
        if subject and subject ~= ARGV[1] then return -1 end
        if redis.call('SISMEMBER', KEYS[2], ARGV[2]) == 1 then return 0 end
        local messages = {}
        local summaries = {}
        local old_messages = redis.call('HGET', KEYS[1], 'messages')
        local old_summaries = redis.call('HGET', KEYS[1], 'tool_summaries')
        if old_messages then messages = cjson.decode(old_messages) end
        if old_summaries then summaries = cjson.decode(old_summaries) end
        for _, item in ipairs(cjson.decode(ARGV[3])) do table.insert(messages, item) end
        for _, item in ipairs(cjson.decode(ARGV[4])) do table.insert(summaries, item) end
        local offset = tonumber(redis.call('HGET', KEYS[1], 'message_offset') or '0')
        while #messages > tonumber(ARGV[5]) do
            table.remove(messages, 1)
            table.remove(summaries, 1)
            offset = offset + 1
        end
        redis.call('HSET', KEYS[1], 'subject_id', ARGV[1], 'thread_id', ARGV[6],
            'messages', cjson.encode(messages), 'tool_summaries', cjson.encode(summaries),
            'message_offset', tostring(offset), 'updated_at', ARGV[7],
            'last_agent', 'supervisor')
        redis.call('SADD', KEYS[2], ARGV[2])
        redis.call('EXPIRE', KEYS[1], tonumber(ARGV[8]))
        redis.call('EXPIRE', KEYS[2], tonumber(ARGV[8]))
        return 1
        """
        try:
            result = self._client.eval(
                script,
                2,
                key,
                marker,
                subject_id,
                task_id,
                messages,
                summaries,
                self._max_messages,
                thread_id,
                datetime.now(timezone.utc).isoformat(),
                self._ttl_s,
            )
        except redis.RedisError as exc:
            raise MemoryUnavailableError("Redis 后台对话轮写入失败") from exc
        if result == -1:
            raise MemoryPersistenceError("会话主体不匹配，拒绝写入他人会话")
        return result == 1

    def load(self, tenant_id: str, thread_id: str, *, subject_id: str) -> list[ChatMessage]:
        key = session_key(tenant_id, thread_id)
        try:
            stored_subject = self._client.hget(key, "subject_id")
            if stored_subject is None:
                return []
            if _decode(stored_subject) != subject_id:
                logger.warning(
                    "session subject mismatch: tenant_id=%s thread_id=%s", tenant_id, thread_id
                )
                return []  # 他人会话视同无历史：跨主体不串上下文
            messages_raw = self._client.hget(key, "messages")
            if not messages_raw:
                return []
            items = json.loads(_decode(messages_raw))
            self._client.expire(key, self._ttl_s)  # 读成功刷新 TTL
            return [
                ChatMessage(role=str(item["role"]), content=str(item["content"])) for item in items
            ]
        except redis.RedisError as exc:
            raise MemoryUnavailableError(f"Redis 会话读取失败: {exc}") from exc

    def load_snapshot(self, tenant_id: str, thread_id: str, *, subject_id: str) -> SessionSnapshot:
        messages = self.load(tenant_id, thread_id, subject_id=subject_id)
        key = session_key(tenant_id, thread_id)
        try:
            if _decode(self._client.hget(key, "subject_id")) != subject_id:
                return SessionSnapshot((), 0, "", 0)
            offset_raw = self._client.hget(key, "message_offset")
            summary_raw = self._client.hget(key, "state_summary")
            summary = json.loads(_decode(summary_raw)) if summary_raw else {}
            return SessionSnapshot(
                tuple(messages),
                int(_decode(offset_raw)) if offset_raw else 0,
                str(summary.get("content", "")),
                int(summary.get("covered_until", 0)),
            )
        except redis.RedisError as exc:
            raise MemoryUnavailableError("Redis 会话摘要读取失败") from exc

    def save_summary(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        subject_id: str,
        expected_covered_until: int,
        covered_until: int,
        summary: str,
    ) -> None:
        key = session_key(tenant_id, thread_id)
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(
            {
                "content": mask_text(summary, 1000),
                "covered_until": covered_until,
                "updated_at": now,
            },
            ensure_ascii=False,
        )
        script = """
        if redis.call('HGET', KEYS[1], 'subject_id') ~= ARGV[1] then return -1 end
        local old = redis.call('HGET', KEYS[1], 'state_summary')
        local covered = 0
        if old then covered = tonumber(cjson.decode(old)['covered_until']) or 0 end
        if covered ~= tonumber(ARGV[2]) then return 0 end
        redis.call('HSET', KEYS[1], 'state_summary', ARGV[4])
        redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
        return 1
        """
        try:
            result = self._client.eval(
                script,
                1,
                key,
                subject_id,
                expected_covered_until,
                covered_until,
                payload,
                self._ttl_s,
            )
        except redis.RedisError as exc:
            raise MemoryUnavailableError("Redis 会话摘要写入失败") from exc
        if result == -1:
            raise MemoryPersistenceError("会话主体不匹配")
        if result != 1:
            raise SummaryConflictError("摘要覆盖位置发生并发变化")

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - 关闭阶段不再向外抛出
            pass


class FallbackSessionRepository:
    """显式降级包装：仅当配置允许且 Redis 传输故障时退回进程内仓储。

    语义拒绝（主体不匹配等）不吸收；降级数据进程私有，健康状态由
    ReadinessCheckUseCase 如实标注 degraded，不宣称持久化。
    """

    def __init__(self, primary, fallback, *, fallback_allowed: bool) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_allowed = fallback_allowed

    def append(
        self,
        tenant_id: str,
        thread_id: str,
        subject_id: str,
        message: ChatMessage,
        *,
        tool_summaries: tuple[dict, ...] = (),
    ) -> None:
        try:
            self._primary.append(
                tenant_id, thread_id, subject_id, message, tool_summaries=tool_summaries
            )
        except MemoryUnavailableError:
            if not self._fallback_allowed:
                raise
            logger.warning("redis unavailable, session degraded to in-process memory")
            self._fallback.append(
                tenant_id, thread_id, subject_id, message, tool_summaries=tool_summaries
            )

    def append_turn_once(
        self,
        tenant_id: str,
        thread_id: str,
        subject_id: str,
        task_id: str,
        question: str,
        answer: str,
        *,
        tool_summaries=(),
    ) -> bool:
        try:
            return self._primary.append_turn_once(
                tenant_id,
                thread_id,
                subject_id,
                task_id,
                question,
                answer,
                tool_summaries=tool_summaries,
            )
        except MemoryUnavailableError:
            if not self._fallback_allowed:
                raise
            return self._fallback.append_turn_once(
                tenant_id,
                thread_id,
                subject_id,
                task_id,
                question,
                answer,
                tool_summaries=tool_summaries,
            )

    def load(self, tenant_id: str, thread_id: str, *, subject_id: str) -> list[ChatMessage]:
        try:
            return self._primary.load(tenant_id, thread_id, subject_id=subject_id)
        except MemoryUnavailableError:
            if not self._fallback_allowed:
                raise
            logger.warning("redis unavailable, session degraded to in-process memory")
            return self._fallback.load(tenant_id, thread_id, subject_id=subject_id)

    def close(self) -> None:
        close = getattr(self._primary, "close", None)
        if close is not None:
            close()

    def load_snapshot(self, tenant_id: str, thread_id: str, *, subject_id: str) -> SessionSnapshot:
        try:
            return self._primary.load_snapshot(tenant_id, thread_id, subject_id=subject_id)
        except MemoryUnavailableError:
            if not self._fallback_allowed:
                raise
            return self._fallback.load_snapshot(tenant_id, thread_id, subject_id=subject_id)

    def save_summary(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        subject_id: str,
        expected_covered_until: int,
        covered_until: int,
        summary: str,
    ) -> None:
        try:
            self._primary.save_summary(
                tenant_id,
                thread_id,
                subject_id=subject_id,
                expected_covered_until=expected_covered_until,
                covered_until=covered_until,
                summary=summary,
            )
        except MemoryUnavailableError:
            if not self._fallback_allowed:
                raise
            self._fallback.save_summary(
                tenant_id,
                thread_id,
                subject_id=subject_id,
                expected_covered_until=expected_covered_until,
                covered_until=covered_until,
                summary=summary,
            )
