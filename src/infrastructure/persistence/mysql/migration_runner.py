"""MySQL 迁移执行器：建版本记账表、查询已应用版本并执行迁移语句。

MySQL DDL 隐式提交，无法整体回滚：所有 DDL 必须写成幂等（IF NOT EXISTS），
重复执行安全；bootstrap.migrate 负责版本发现与顺序编排，本模块只执行 SQL。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

BOOKKEEPING_TABLE = "schema_migrations"

_CREATE_BOOKKEEPING = (
    f"CREATE TABLE IF NOT EXISTS {BOOKKEEPING_TABLE} ("
    "version VARCHAR(32) NOT NULL PRIMARY KEY, "
    "applied_at DATETIME(6) NOT NULL"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)


class MySQLMigrationRunner:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_bookkeeping(self) -> None:
        with self._engine.begin() as connection:
            connection.execute(text(_CREATE_BOOKKEEPING))

    def applied_versions(self) -> list[str]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT version FROM {BOOKKEEPING_TABLE} ORDER BY version")
            )
            return [str(row[0]) for row in rows]

    def run_statements(self, statements: list[str]) -> None:
        for statement in statements:
            with self._engine.begin() as connection:
                connection.execute(text(statement))

    def record_version(self, version: str) -> None:
        applied_at = datetime.now(timezone.utc).isoformat()
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {BOOKKEEPING_TABLE} (version, applied_at) "
                    "VALUES (:version, :applied_at)"
                ),
                {"version": version, "applied_at": applied_at},
            )
