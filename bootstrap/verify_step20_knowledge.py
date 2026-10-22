"""步骤20合成草稿的三种知识路由本地实测；不激活发布。"""

from __future__ import annotations

import json
from datetime import date

from application.knowledge.graph.ports import GraphQuery, GraphScope
from application.knowledge.hybrid import HybridRetriever
from application.knowledge.policy import PolicyReader
from bootstrap.local_knowledge_rag import LocalCandidateRagRetriever
from bootstrap.settings import load_settings
from bootstrap.verify_knowledge_graph_reader import RELEASE_ID
from bootstrap.verify_knowledge_retrieval import PROJECT_ROOT
from infrastructure.document.policy_catalog import LocalManifestPolicySource
from infrastructure.graph.neo4j_adapter import Neo4jGraphAdapter
from infrastructure.persistence.neo4j.connection import create_neo4j_driver


def main() -> None:
    settings = load_settings()
    if settings.app_env == "production" or not settings.neo4j_query_password:
        raise RuntimeError("仅可在本地已配置只读图谱账号的开发环境执行")
    rag = LocalCandidateRagRetriever(settings)
    driver = create_neo4j_driver(
        settings.neo4j_uri,
        settings.neo4j_query_username,
        settings.neo4j_query_password,
        settings.neo4j_connection_timeout_s,
    )
    try:
        graph = Neo4jGraphAdapter(driver, settings.neo4j_database)
        policy = PolicyReader(
            LocalManifestPolicySource(
                PROJECT_ROOT / "data/knowledge/v2/corpus.json",
                PROJECT_ROOT / "data/manifests/knowledge-v2.json",
            )
        )
        rag_result = rag.search(
            "退货要在签收后几天申请？",
            tenant_id="tenant-north",
            knowledge_release_id=RELEASE_ID,
        )
        kg_result = graph.search(
            GraphQuery(("A",), ("COMPATIBLE_WITH",)),
            GraphScope("tenant-north", RELEASE_ID),
        )
        hybrid_result = HybridRetriever(rag, graph).search(
            "净化器A能使用F-01滤芯吗？",
            GraphQuery(("A",), ("COMPATIBLE_WITH",)),
            tenant_id="tenant-north",
            knowledge_release_id=RELEASE_ID,
        )
        policy_result = policy.get(
            "退货政策",
            tenant_id="tenant-north",
            knowledge_release_id=RELEASE_ID,
            as_of=date(2026, 10, 21),
        )
        rag_docs = [item.chunk.document_id for item in rag_result.accepted_results]
        if "DOC-RETURN-CURRENT" not in rag_docs:
            raise AssertionError("RAG政策文本未命中预期文档")
        if not any(item.relation_type == "COMPATIBLE_WITH" for item in kg_result.relations):
            raise AssertionError("KG兼容关系未命中")
        if (
            not hybrid_result.generation_ready
            or not hybrid_result.rag
            or not hybrid_result.rag.accepted_results
            or not hybrid_result.graph
            or not hybrid_result.graph.relations
        ):
            raise AssertionError("混合检索未同时提供已验证文档与关系")
        if policy_result.match_status != "found" or policy_result.policy is None:
            raise AssertionError("政策详情未命中唯一有效版本")
        print(
            json.dumps(
                {
                    "release_status": "draft_not_activated",
                    "rag_documents": rag_docs,
                    "kg_relations": len(kg_result.relations),
                    "hybrid_status": hybrid_result.retrieval_status,
                    "hybrid_rag_documents": [
                        item.chunk.document_id for item in hybrid_result.rag.accepted_results
                    ],
                    "hybrid_kg_relations": len(hybrid_result.graph.relations),
                    "hybrid_conflict_status": hybrid_result.conflict_status,
                    "policy_id": policy_result.policy.policy_id,
                    "policy_version": policy_result.policy.version,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
