"""RAG 证据 DTO 与 MCP 响应契约对齐。"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from application.knowledge.graph.ports import GraphEvidence, GraphNode, GraphRelation, GraphResult
from application.knowledge.rag.retriever import RagEvidence, RagResult
from application.knowledge.retrieval.branch import BranchResult
from application.knowledge.retrieval.ports import KnowledgeChunk
from interface.mcp.knowledge_projection import rag_tool_data


def _result() -> RagResult:
    chunk = KnowledgeChunk(
        tenant_id="tenant-north",
        release_id="release-v2",
        chunk_id="chunk-1",
        document_id="doc-1",
        evidence_id="evidence-1",
        version=2,
        title="退货政策",
        content="政策原文",
        search_text="退货政策",
        status="published",
    )
    return RagResult(
        query="退货期限",
        tenant_id="tenant-north",
        knowledge_release_id="release-v2",
        retrieval_status="partial",
        results=(RagEvidence(chunk, 0.8, None, 0.9, None, 0.9, ("vector",), 3.0, 0.8),),
        branches=(
            BranchResult("vector", "success", (), 1.0),
            BranchResult("bm25", "error", (), 2.0, "RETRIEVAL_ERROR"),
        ),
        config_version="candidate-v1",
        vector_calibration_version="vector-v1",
        bm25_calibration_version="bm25-v1",
        ranking_method="cross_encoder",
        rerank_status="success",
        rerank_model_id="test-reranker",
        rerank_model_revision="fixed-revision",
        rerank_calibration_version="rerank-v1",
    )


def test_projection_preserves_evidence_scores_and_degradation() -> None:
    data = rag_tool_data(_result())
    assert data["retrieval_status"] == "partial"
    assert data["vector_status"] == "success"
    assert data["bm25_status"] == "failed"
    assert data["evidence_refs"] == ["evidence-1"]
    assert data["results"][0]["vector_score"] == 0.8
    assert data["results"][0]["bm25_score"] is None
    assert data["results"][0]["retrieval_source"] == ["vector"]
    assert data["rerank_status"] == "success"
    assert data["generation_ready"] is True
    assert data["results"][0]["rerank_score"] == 3.0

    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "data/contracts/mcp-response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema["$defs"]["tool_data_search_knowledge_base"]).validate(data)


def test_unreranked_candidates_never_escape_as_evidence() -> None:
    from dataclasses import replace

    candidate = replace(
        _result(),
        ranking_method="fused_score_only",
        rerank_status="not_configured",
        rerank_model_id=None,
        rerank_model_revision=None,
        rerank_calibration_version=None,
    )
    data = rag_tool_data(candidate)
    assert data["retrieval_status"] == "degraded"
    assert data["generation_ready"] is False
    assert data["results"] == []
    assert data["evidence_refs"] == []


def test_graph_projection_preserves_relation_evidence() -> None:
    from interface.mcp.knowledge_projection import graph_tool_data

    data = graph_tool_data(
        GraphResult(
            "release-v2",
            (GraphNode("p1", "Product", "A", "1"), GraphNode("a1", "Accessory", "F-01", "1")),
            (GraphRelation("p1", "COMPATIBLE_WITH", "a1", "doc-1:v2", "ev-1"),),
            (GraphEvidence("ev-1", "doc-1", "corpus", "2"),),
        )
    )
    assert data["route"] == "kg"
    assert data["relations"][0]["evidence_id"] == "ev-1"
    assert data["evidence_refs"] == ["ev-1"]
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "data/contracts/mcp-response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema["$defs"]["tool_data_query_knowledge_graph"]).validate(data)


def test_hybrid_projection_keeps_document_and_relation_distinct() -> None:
    from application.knowledge.hybrid import HybridResult
    from interface.mcp.knowledge_projection import hybrid_tool_data

    rag = _result()
    graph = GraphResult(
        "release-v2",
        (),
        (GraphRelation("p1", "HAS_POLICY", "policy1", "2", "evidence-1"),),
        (GraphEvidence("evidence-1", "doc-1", "corpus", "2"),),
    )
    data = hybrid_tool_data(
        HybridResult("release-v2", "partial", "none", rag, graph, "partial", "success")
    )
    assert {item["kind"] for item in data["merged_evidence"]} == {"document", "relation"}
    assert data["evidence_refs"] == ["evidence-1"]
    assert data["generation_ready"] is True
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "data/contracts/mcp-response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema["$defs"]["tool_data_search_hybrid_knowledge"]).validate(data)

    conflict = hybrid_tool_data(
        HybridResult(
            "release-v2",
            "degraded",
            "indexing_failed",
            None,
            None,
            "complete",
            "success",
            ("evidence-1",),
        )
    )
    assert conflict["merged_evidence"] == []
    assert conflict["generation_ready"] is False
    Draft202012Validator(schema["$defs"]["tool_data_search_hybrid_knowledge"]).validate(conflict)


def test_policy_projection_matches_versioned_schema() -> None:
    from datetime import date

    from application.knowledge.policy.reader import PolicyDetail, PolicyResult
    from interface.mcp.knowledge_projection import policy_tool_data

    data = policy_tool_data(
        PolicyResult(
            "found",
            PolicyDetail(
                "POL-1", "退货政策", ("七日内",), (), date(2026, 1, 1), None, "2", ("ev-1",)
            ),
            (),
            "release-v2",
        )
    )
    assert data["policy"]["valid_from"] == "2026-01-01T00:00:00+00:00"
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "data/contracts/mcp-response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema["$defs"]["tool_data_get_policy_detail"]).validate(data)
