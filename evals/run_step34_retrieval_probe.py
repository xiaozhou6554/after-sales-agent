"""Run the development-split RAG evidence probe against local configured indexes."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bootstrap.local_knowledge_rag import LocalCandidateRagRetriever
from bootstrap.settings import load_settings

from application.knowledge.graph.ports import GraphQuery, GraphScope
from application.knowledge.hybrid import HybridRetriever
from infrastructure.graph.neo4j_adapter import Neo4jGraphAdapter
from infrastructure.persistence.neo4j.connection import create_neo4j_driver

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELEASE_ID = "KNOWLEDGE-RELEASE-2026-10"


def main() -> None:
    labels_path = PROJECT_ROOT / "data/scenarios/v2/knowledge_labels.jsonl"
    labels = [
        json.loads(line)
        for line in labels_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = [
        case
        for case in labels
        if case["split"] == "development" and case["expected_outcome"] == "answerable"
    ]
    if not cases:
        raise RuntimeError("No answerable development cases found in the frozen label set")

    settings = load_settings()
    retriever = LocalCandidateRagRetriever(settings)
    results = []
    relevant_documents = 0
    candidate_document_hits = 0
    final_reciprocal_rank_sum = 0.0
    for case in cases:
        started = time.perf_counter()
        result = retriever.search(
            case["question"],
            tenant_id=case["tenant_id"],
            knowledge_release_id=RELEASE_ID,
        )
        duration_ms = (time.perf_counter() - started) * 1000
        expected = set(case["document_ids"])
        vector_documents = [hit.chunk.document_id for hit in result.vector_branch.hits]
        bm25_documents = [hit.chunk.document_id for hit in result.bm25_branch.hits]
        candidate_documents = set(vector_documents + bm25_documents)
        accepted_documents = [item.chunk.document_id for item in result.accepted_results]
        relevant_documents += len(expected)
        candidate_document_hits += len(expected & candidate_documents)
        rank = next(
            (index for index, document_id in enumerate(accepted_documents[:3], start=1)
             if document_id in expected),
            None,
        )
        if rank is not None:
            final_reciprocal_rank_sum += 1 / rank
        results.append(
            {
                "case_id": case["case_id"],
                "expected_documents": sorted(expected),
                "vector_documents": vector_documents,
                "bm25_documents": bm25_documents,
                "accepted_documents": accepted_documents,
                "retrieval_status": result.retrieval_status,
                "rerank_status": result.rerank_status,
                "rerank_error_code": result.rerank_error_code,
                "vector_status": result.vector_status,
                "bm25_status": result.bm25_status,
                "elapsed_ms": round(duration_ms, 3),
            }
        )

    if settings.app_env == "production" or not settings.neo4j_query_password:
        raise RuntimeError("知识评测仅可连接本地开发只读图谱账号")
    driver = create_neo4j_driver(
        settings.neo4j_uri,
        settings.neo4j_query_username,
        settings.neo4j_query_password,
        settings.neo4j_connection_timeout_s,
    )
    try:
        graph = Neo4jGraphAdapter(driver, settings.neo4j_database)
        graph_query = GraphQuery(("A",), ("COMPATIBLE_WITH",))
        scope = GraphScope("tenant-north", RELEASE_ID)
        graph_started = time.perf_counter()
        graph_result = graph.search(graph_query, scope)
        graph_elapsed_ms = (time.perf_counter() - graph_started) * 1000
        hybrid_started = time.perf_counter()
        hybrid_result = HybridRetriever(retriever, graph).search(
            "净化器A能使用F-01滤芯吗？",
            graph_query,
            tenant_id="tenant-north",
            knowledge_release_id=RELEASE_ID,
        )
        hybrid_elapsed_ms = (time.perf_counter() - hybrid_started) * 1000
        report = {
            "dataset_version": "mock-v2",
            "split": "development",
            "knowledge_release_id": RELEASE_ID,
            "case_count": len(cases),
            "relevant_document_count": relevant_documents,
            "candidate_document_hits": candidate_document_hits,
            "candidate_recall": (
                candidate_document_hits / relevant_documents if relevant_documents else 0
            ),
            "mrr_at_3_sum": round(final_reciprocal_rank_sum, 6),
            "mrr_at_3_denominator": len(cases),
            "mrr_at_3": round(final_reciprocal_rank_sum / len(cases), 6),
            "results": results,
            "graph_probe": {
                "entity": "A",
                "relation": "COMPATIBLE_WITH",
                "relation_count": len(graph_result.relations),
                "evidence_count": len(graph_result.evidence),
                "elapsed_ms": round(graph_elapsed_ms, 3),
            },
            "hybrid_probe": {
                "retrieval_status": hybrid_result.retrieval_status,
                "generation_ready": hybrid_result.generation_ready,
                "rag_status": hybrid_result.rag_status,
                "kg_status": hybrid_result.kg_status,
                "conflict_status": hybrid_result.conflict_status,
                "elapsed_ms": round(hybrid_elapsed_ms, 3),
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
