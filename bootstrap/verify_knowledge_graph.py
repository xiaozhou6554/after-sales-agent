"""开发环境固定语料图谱验证；仅写入合成草稿索引，不激活知识发布。"""

from __future__ import annotations

import json

from application.knowledge.graph.ports import GraphQuery, GraphScope
from bootstrap.settings import load_settings
from infrastructure.graph.neo4j_adapter import Neo4jGraphAdapter
from infrastructure.graph.seed import seed_reviewed_graph
from infrastructure.persistence.neo4j.connection import create_neo4j_driver

RELEASE_ID = "KNOWLEDGE-RELEASE-2026-10"


def main() -> None:
    settings = load_settings()
    if settings.app_env == "production":
        raise RuntimeError("生产环境不得执行开发语料图谱导入")
    driver = create_neo4j_driver(
        settings.neo4j_uri,
        settings.neo4j_username,
        settings.neo4j_password,
        settings.neo4j_connection_timeout_s,
    )
    try:
        seeded = {
            tenant: seed_reviewed_graph(driver, settings.neo4j_database, tenant, RELEASE_ID)
            for tenant in ("tenant-north", "tenant-south")
        }
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
            GraphScope("tenant-north", "not-active-or-indexed"),
        )
        if not north.relations or not south.relations or wrong_release.relations:
            raise AssertionError("图谱关系或发布版本隔离验证未通过")
        if set(north.evidence_refs) & set(south.evidence_refs):
            raise AssertionError("跨租户证据引用混用")
        print(
            json.dumps(
                {
                    "release_id": RELEASE_ID,
                    "release_status": "draft_not_activated",
                    "seeded_nodes_edges": seeded,
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
