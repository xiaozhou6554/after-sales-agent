"""MySQL 连接配置、连接池与就绪探测。

超时来自《技术详细规格》1.4（连接1秒/读取2秒）；探测结果不携带凭据。
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from application.common.ports.dependency_probe import ProbeResult


def create_mysql_engine(mysql_url: str, connect_timeout_s: float, read_timeout_s: float) -> Engine:
    return create_engine(
        mysql_url,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "connect_timeout": connect_timeout_s,
            "read_timeout": read_timeout_s,
            "write_timeout": read_timeout_s,
        },
    )


class MySQLDependencyProbe:
    name = "mysql"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        with self._engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(name=self.name, ok=True, latency_ms=latency_ms)


def describe_engine(engine: Engine) -> str:
    """输出脱敏后的引擎描述，避免日志泄露连接凭据。"""
    url: Any = engine.url
    return url.render_as_string(hide_password=True)
