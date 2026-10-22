"""把知识召回结果投影为严格、可校验的 MCP 证据 DTO。"""

from __future__ import annotations

from application.knowledge.graph.ports import GraphResult
from application.knowledge.hybrid import HybridResult
from application.knowledge.policy import PolicyResult
from application.knowledge.rag.retriever import RagResult


def rag_tool_data(result: RagResult) -> dict:
    """只投影已通过检索层租户/版本检查的证据，不生成答案。"""
    accepted = result.accepted_results
    retrieval_status = (
        "degraded"
        if result.results and result.rerank_status == "not_configured"
        else result.retrieval_status
    )
    return {
        "route": "rag",
        "query": result.query,
        "knowledge_release_id": result.knowledge_release_id,
        "retrieval_status": retrieval_status,
        "vector_status": result.vector_status,
        "bm25_status": result.bm25_status,
        "vector_error": result.vector_branch.error_code,
        "bm25_error": result.bm25_branch.error_code,
        "vector_hit_count": len(result.vector_branch.hits),
        "bm25_hit_count": len(result.bm25_branch.hits),
        "vector_duration_ms": result.vector_branch.duration_ms,
        "bm25_duration_ms": result.bm25_branch.duration_ms,
        "config_version": result.config_version,
        "vector_calibration_version": result.vector_calibration_version,
        "bm25_calibration_version": result.bm25_calibration_version,
        "ranking_method": result.ranking_method,
        "rerank_status": result.rerank_status,
        "rerank_error": result.rerank_error_code,
        "rerank_model_id": result.rerank_model_id,
        "rerank_model_revision": result.rerank_model_revision,
        "rerank_calibration_version": result.rerank_calibration_version,
        "generation_ready": result.generation_ready,
        "results": [
            {
                "document_id": item.chunk.document_id,
                "chunk_id": item.chunk.chunk_id,
                "title": item.chunk.title,
                "snippet": item.chunk.content[:4000],
                "score": item.final_score,
                "vector_score": item.vector_score,
                "bm25_score": item.bm25_score,
                "rerank_score": item.rerank_score,
                "rerank_relevance": item.rerank_relevance,
                "retrieval_source": list(item.retrieval_source),
                "version": str(item.chunk.version),
                "evidence_id": item.evidence_id,
            }
            for item in accepted
        ],
        "evidence_refs": list(dict.fromkeys(item.evidence_id for item in accepted)),
        "conflicts": list(result.conflicts),
    }


def graph_tool_data(result: GraphResult) -> dict:
    """图谱关系必须保留原始证据引用和发布版本。"""
    return {
        "route": "kg",
        "knowledge_release_id": result.knowledge_release_id,
        "graph_status": "success" if result.relations else "empty",
        "nodes": [
            {
                "node_id": item.node_id,
                "node_type": item.node_type,
                "label": item.label,
                "version": item.version,
            }
            for item in result.nodes
        ],
        "relations": [
            {
                "from": item.from_id,
                "relation_type": item.relation_type,
                "to": item.to_id,
                "version": item.version,
                "evidence_id": item.evidence_id,
            }
            for item in result.relations
        ],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "document_id": item.document_id,
                "source": item.source,
                "version": item.version,
            }
            for item in result.evidence
        ],
        "evidence_refs": list(result.evidence_refs),
    }


def hybrid_tool_data(result: HybridResult) -> dict:
    """文档与关系保留独立证据类型；冲突时不暴露可供生成的候选。"""
    rag_evidence = []
    if result.rag is not None:
        for item in result.rag.accepted_results:
            rag_evidence.append(
                {
                    "document_id": item.chunk.document_id,
                    "chunk_id": item.chunk.chunk_id,
                    "title": item.chunk.title,
                    "snippet": item.chunk.content[:4000],
                    "score": item.final_score,
                    "retrieval_source": list(item.retrieval_source),
                    "version": str(item.chunk.version),
                    "evidence_id": item.evidence_id,
                }
            )
    kg_evidence = []
    if result.graph is not None:
        for item in result.graph.relations:
            kg_evidence.append(
                {
                    "from": item.from_id,
                    "relation_type": item.relation_type,
                    "to": item.to_id,
                    "evidence_id": item.evidence_id,
                    "version": item.version,
                }
            )
    merged = [{"kind": "document", **item} for item in rag_evidence] + [
        {"kind": "relation", **item} for item in kg_evidence
    ]
    return {
        "route": "hybrid",
        "knowledge_release_id": result.knowledge_release_id,
        "retrieval_status": result.retrieval_status,
        "rag_status": result.rag_status,
        "kg_status": result.kg_status,
        "rag_evidence": rag_evidence,
        "kg_evidence": kg_evidence,
        "merged_evidence": merged,
        "conflict_status": result.conflict_status,
        "conflicts": list(result.conflicts),
        "generation_ready": result.generation_ready,
        "evidence_refs": list(dict.fromkeys(item["evidence_id"] for item in merged)),
    }


def _date_time(value) -> str:
    return value.isoformat() + "T00:00:00+00:00"


def policy_tool_data(result: PolicyResult) -> dict:
    policy = result.policy
    return {
        "route": "policy",
        "knowledge_release_id": result.knowledge_release_id,
        "match_status": result.match_status,
        "policy": (
            {
                "policy_id": policy.policy_id,
                "policy_name": policy.policy_name,
                "clauses": list(policy.clauses),
                "applicability": list(policy.applicability),
                "valid_from": _date_time(policy.valid_from),
                "valid_to": _date_time(policy.valid_to) if policy.valid_to else None,
                "version": policy.version,
                "evidence_refs": list(policy.evidence_refs),
            }
            if policy
            else None
        ),
        "candidates": [
            {
                "policy_id": item.policy_id,
                "policy_name": item.policy_name,
                "evidence_id": item.evidence_id,
            }
            for item in result.candidates
        ],
        "evidence_refs": list(policy.evidence_refs) if policy else [],
    }
