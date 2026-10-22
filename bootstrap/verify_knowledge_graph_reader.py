"""用独立只读账号核对本地草稿图谱的实际查询路径；不写入或激活。"""

from __future__ import annotations

import json

from application.knowledge.graph.ports import GraphQuery, GraphScope
from bootstrap.settings import load_settings
from infrastructure.graph.neo4j_adapter import Neo4jGraphAdapter
from infrastructure.persistence.neo4j.connection import create_neo4j_driver

RELEASE_ID = "KNOWLEDGE-RELEASE-2026-10"


def main() -> None:
    settings = load_settings()
    if settings.app_env == "production" or not settings.neo4j_query_password:
        raise RuntimeError("仅对已配置独立只读账号的本地开发环境执行")
    driver = create_neo4j_driver(
        settings.neo4j_uri,
        settings.neo4j_query_username,
        settings.neo4j_query_password,
        settings.neo4j_connection_timeout_s,
    )
    try:
        adapter = Neo4jGraphAdapter(driver, settings.neo4j_database)
        north = adapter.search(
            GraphQuery(("A",), ("COMPATIBLE_WITH",)),
            GraphScope("tenant-north", RELEASE_ID),
        )
        south = adapter.search(
            GraphQuery(("A",), ("HAS_POLICY",)),
            GraphScope("tenant-south", RELEASE_ID),
        )
        wrong_release = adapter.search(
            GraphQuery(("A",), ("COMPATIBLE_WITH",)),
            GraphScope("tenant-north", "not-indexed"),
        )
        if not north.relations or not south.relations or wrong_release.relations:
            raise AssertionError("只读账号下关系命中或版本隔离验证失败")
        print(
            json.dumps(
                {
                    "account": settings.neo4j_query_username,
                    "release_status": "draft_not_activated",
                    "north_compatible_relations": len(north.relations),
                    "south_policy_relations": len(south.relations),
                    "wrong_release_relations": len(wrong_release.relations),
                },
                ensure_ascii=False,
            )
        )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
