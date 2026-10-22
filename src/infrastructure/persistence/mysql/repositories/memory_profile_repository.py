"""MySQL 长期记忆仓储；只按可信租户和客户键读取。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from application.memory.ports.profile import ProfileUnavailableError
from domain.memory.aggregates.memory_profile import MemoryProfile
from domain.memory.entities.memory_fact import MemoryFact


class MySQLMemoryProfileRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, tenant_id: str, user_id: str) -> MemoryProfile:
        try:
            with self._engine.connect() as connection:
                rows = (
                    connection.execute(
                        text("""SELECT fact_type, fact_key, fact_value, importance,
                            source, updated_at FROM user_memory_fact
                            WHERE tenant_id=:tenant_id AND user_id=:user_id ORDER BY id"""),
                        {"tenant_id": tenant_id, "user_id": user_id},
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise ProfileUnavailableError("长期记忆读取失败") from exc
        profile = MemoryProfile(tenant_id, user_id)
        for row in rows:
            value = row["fact_value"]
            if isinstance(value, str):
                value = json.loads(value)
            moment = row["updated_at"].replace(tzinfo=timezone.utc)
            profile.remember(
                MemoryFact(
                    fact_type=row["fact_type"],
                    fact_key=row["fact_key"],
                    fact_value=str(value["value"]),
                    importance=float(row["importance"]),
                    source=row["source"],
                    updated_at=moment,
                )
            )
        return profile

    def upsert(self, tenant_id: str, user_id: str, fact: MemoryFact) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    text("""INSERT INTO user_profile
                    (tenant_id,user_id,role,created_at,updated_at)
                    VALUES (:tenant_id,:user_id,'customer',:now,:now)
                    ON DUPLICATE KEY UPDATE updated_at=VALUES(updated_at)"""),
                    {"tenant_id": tenant_id, "user_id": user_id, "now": now},
                )
                connection.execute(
                    text("""INSERT INTO user_memory_fact
                    (tenant_id,user_id,fact_type,fact_key,fact_value,importance,source,created_at,updated_at)
                    VALUES (:tenant_id,:user_id,:fact_type,:fact_key,:fact_value,
                            :importance,:source,:now,:now)
                    ON DUPLICATE KEY UPDATE fact_value=VALUES(fact_value),
                    importance=VALUES(importance),source=VALUES(source),updated_at=VALUES(updated_at)"""),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "fact_type": fact.fact_type,
                        "fact_key": fact.fact_key,
                        "fact_value": json.dumps({"value": fact.fact_value}, ensure_ascii=False),
                        "importance": round(fact.importance, 2),
                        "source": fact.source,
                        "now": now,
                    },
                )
        except SQLAlchemyError as exc:
            raise ProfileUnavailableError("长期记忆写入失败") from exc

    def cleanup_candidates(self, tenant_id: str, user_id: str) -> list[MemoryFact]:
        now = datetime.now(timezone.utc)
        return [fact for fact in self.load(tenant_id, user_id).facts if fact.cleanup_candidate(now)]

    def delete_cleanup_candidates(
        self, now: datetime, limit: int, tenant_id: str | None = None
    ) -> int:
        if limit < 1:
            raise ValueError("limit 必须为正数")
        moment = now.astimezone(timezone.utc).replace(tzinfo=None)
        try:
            with self._engine.begin() as connection:
                query = """DELETE FROM user_memory_fact
                        WHERE importance < 0.8
                          AND importance * EXP(
                              -GREATEST(TIMESTAMPDIFF(SECOND, updated_at, :now), 0)
                              / 15552000.0
                          ) < 0.2"""
                params = {"now": moment, "limit": limit}
                if tenant_id is not None:
                    query += " AND tenant_id = :tenant_id"
                    params["tenant_id"] = tenant_id
                query += " ORDER BY updated_at, id LIMIT :limit"
                result = connection.execute(text(query), params)
                return result.rowcount
        except SQLAlchemyError as exc:
            raise ProfileUnavailableError("长期记忆清理失败") from exc
