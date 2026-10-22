"""Neo4j 连接配置与就绪探测。

超时来自《技术详细规格》1.4（连接1秒/读取2秒）；图谱默认只读（NEO4J_READ_ONLY），
探测执行的是只读查询。
"""

from __future__ import annotations

import time

from neo4j import READ_ACCESS, Driver, GraphDatabase

from application.common.ports.dependency_probe import ProbeResult


def create_neo4j_driver(
    uri: str, username: str, password: str, connection_timeout_s: float
) -> Driver:
    return GraphDatabase.driver(
        uri,
        auth=(username, password),
        connection_timeout=connection_timeout_s,
        max_connection_pool_size=10,
    )


class Neo4jDependencyProbe:
    name = "neo4j"

    def __init__(self, driver: Driver, database: str) -> None:
        self._driver = driver
        self._database = database

    def probe(self) -> ProbeResult:
        started = time.perf_counter()
        # 在目标数据库执行只读查询：同时验证服务连通与数据库已创建。
        with self._driver.session(
            database=self._database, default_access_mode=READ_ACCESS
        ) as session:
            session.run("RETURN 1").consume()
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProbeResult(name=self.name, ok=True, latency_ms=latency_ms)
