from threading import Barrier

import pytest

from application.knowledge.graph.ports import (
    GraphEvidence,
    GraphIntegrityError,
    GraphQuery,
    GraphRelation,
    GraphResult,
)
from application.knowledge.hybrid import HybridRetriever
from application.knowledge.rag.retriever import RagEvidence, RagResult
from application.knowledge.retrieval.branch import BranchResult
from application.knowledge.retrieval.ports import KnowledgeChunk


def rag_result(*, release="r1", evidence_id="ev-1", ready=True):
    chunk = KnowledgeChunk(
        "tenant-1",
        release,
        "chunk-1",
        "doc-1",
        evidence_id,
        2,
        "title",
        "content",
        "search text",
        "published",
    )
    evidence = RagEvidence(chunk, 0.8, 4.0, 0.8, 0.6, 0.75, ("vector", "bm25"), 3.0, 0.9)
    return RagResult(
        "question",
        "tenant-1",
        release,
        "complete",
        (evidence,),
        (BranchResult("vector", "success", (), 1), BranchResult("bm25", "success", (), 1)),
        "config-v1",
        "vector-v1",
        "bm25-v1",
        rerank_status="success" if ready else "not_configured",
    )


def graph_result(*, release="r1", evidence_id="ev-1", document_id="doc-1", version="2"):
    return GraphResult(
        release,
        (),
        (GraphRelation("product", "HAS_POLICY", "policy", version, evidence_id),),
        (GraphEvidence(evidence_id, document_id, "source", version),),
    )


class RagPort:
    def __init__(self, result, *, gate=None, error=None):
        self.result = result
        self.gate = gate
        self.error = error
        self.calls = []

    def search(self, query, *, tenant_id, knowledge_release_id):
        self.calls.append((query, tenant_id, knowledge_release_id))
        if self.gate:
            self.gate.wait(timeout=2)
        if self.error:
            raise self.error
        return self.result


class GraphPort:
    def __init__(self, result, *, gate=None, error=None):
        self.result = result
        self.gate = gate
        self.error = error
        self.calls = []

    def search(self, query, scope):
        self.calls.append((query, scope))
        if self.gate:
            self.gate.wait(timeout=2)
        if self.error:
            raise self.error
        return self.result


QUERY = GraphQuery(("product",), ("HAS_POLICY",))


def test_parallel_same_release_and_independent_scores() -> None:
    gate = Barrier(2)
    rag, graph = RagPort(rag_result(), gate=gate), GraphPort(graph_result(), gate=gate)
    result = HybridRetriever(rag, graph).search(
        "question", QUERY, tenant_id="tenant-1", knowledge_release_id="r1"
    )
    assert rag.calls == [("question", "tenant-1", "r1")]
    assert graph.calls[0][1].tenant_id == "tenant-1"
    assert graph.calls[0][1].knowledge_release_id == "r1"
    assert result.retrieval_status == "complete"
    assert result.generation_ready
    assert result.rag.accepted_results[0].final_score == 0.75
    assert result.graph.relations[0].evidence_id == "ev-1"


def test_kg_failure_leaves_only_rag_and_does_not_verify_relation() -> None:
    result = HybridRetriever(
        RagPort(rag_result()), GraphPort(None, error=RuntimeError("kg offline"))
    ).search("question", QUERY, tenant_id="tenant-1", knowledge_release_id="r1")
    assert result.retrieval_status == "partial"
    assert result.kg_status == "error"
    assert result.graph is None
    assert result.rag.accepted_results


def test_integrity_error_and_cross_route_evidence_conflict_fail_closed() -> None:
    corrupt = HybridRetriever(
        RagPort(rag_result()), GraphPort(None, error=GraphIntegrityError("bad edge"))
    ).search("question", QUERY, tenant_id="tenant-1", knowledge_release_id="r1")
    assert corrupt.conflict_status == "indexing_failed"
    assert not corrupt.generation_ready
    assert corrupt.rag is None and corrupt.graph is None

    mismatch = HybridRetriever(
        RagPort(rag_result()), GraphPort(graph_result(document_id="other"))
    ).search("question", QUERY, tenant_id="tenant-1", knowledge_release_id="r1")
    assert mismatch.conflict_status == "indexing_failed"
    assert mismatch.conflicts == ("ev-1",)
    assert not mismatch.generation_ready


def test_mismatched_release_and_unreranked_rag_do_not_leak_evidence() -> None:
    release_mismatch = HybridRetriever(
        RagPort(rag_result()), GraphPort(graph_result(release="r2"))
    ).search("question", QUERY, tenant_id="tenant-1", knowledge_release_id="r1")
    assert release_mismatch.conflicts == ("kg_release_mismatch",)
    assert release_mismatch.rag is None

    no_rerank = HybridRetriever(
        RagPort(rag_result(ready=False)), GraphPort(GraphResult("r1", (), (), ()))
    ).search("question", QUERY, tenant_id="tenant-1", knowledge_release_id="r1")
    assert no_rerank.retrieval_status == "degraded"
    assert not no_rerank.generation_ready

    kg_only = HybridRetriever(RagPort(rag_result(ready=False)), GraphPort(graph_result())).search(
        "question", QUERY, tenant_id="tenant-1", knowledge_release_id="r1"
    )
    assert kg_only.retrieval_status == "partial"
    assert kg_only.generation_ready


def test_invalid_graph_query_is_not_treated_as_backend_failure() -> None:
    with pytest.raises(ValueError):
        HybridRetriever(
            RagPort(rag_result()), GraphPort(None, error=ValueError("invalid relation"))
        ).search("question", QUERY, tenant_id="tenant-1", knowledge_release_id="r1")
