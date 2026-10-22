"""Step18 双路用例合同；分数锚点为测试配置，不代表检索质量校准。"""

from __future__ import annotations

from dataclasses import replace
from threading import Barrier

import pytest

from application.knowledge.rag import CalibrationCurve, RagConfig, RagRetriever
from application.knowledge.retrieval.ports import KnowledgeChunk, RetrievalHit, RetrievalUnavailable


def chunk(chunk_id: str, *, tenant: str = "t1", release: str = "r1") -> KnowledgeChunk:
    return KnowledgeChunk(
        tenant_id=tenant,
        release_id=release,
        chunk_id=chunk_id,
        document_id="same-document",
        evidence_id=f"ev-{chunk_id}",
        version=2,
        title="政策",
        content=f"content-{chunk_id}",
        search_text=f"search-{chunk_id}",
        status="published",
    )


def config(**changes) -> RagConfig:
    base = RagConfig(
        version="test-params-v1",
        vector_calibration=CalibrationCurve("test-vector-v1", ((-1.0, 0.0), (1.0, 1.0))),
        bm25_calibration=CalibrationCurve("test-bm25-v1", ((0.0, 0.0), (10.0, 1.0))),
        vector_weight=3.0,
        bm25_weight=1.0,
        vector_threshold=0.4,
        bm25_threshold=0.3,
        fused_threshold=0.5,
        vector_top_k=20,
        bm25_top_k=20,
        result_top_k=3,
    )
    return replace(base, **changes)


class SearchPort:
    def __init__(self, hits=(), *, failure=False, barrier=None):
        self.hits = list(hits)
        self.failure = failure
        self.barrier = barrier
        self.calls = []

    def search(self, query, tenant_id, release_id, top_k):
        self.calls.append((query, tenant_id, release_id, top_k))
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        if self.failure:
            raise RetrievalUnavailable("secret backend detail")
        return list(self.hits)


def test_parallel_dedup_scores_sources_and_distinct_chunks() -> None:
    gate = Barrier(2)
    first, second = chunk("c1"), chunk("c2")
    vector = SearchPort(
        [RetrievalHit(first, 0.8, "vector"), RetrievalHit(second, 0.2, "vector")],
        barrier=gate,
    )
    bm25 = SearchPort(
        [RetrievalHit(first, 6.0, "bm25"), RetrievalHit(second, 8.0, "bm25")],
        barrier=gate,
    )
    result = RagRetriever(vector, bm25, config()).search(
        "查询", tenant_id="t1", knowledge_release_id="r1"
    )
    assert vector.calls == [("查询", "t1", "r1", 20)]
    assert bm25.calls == [("查询", "t1", "r1", 20)]
    assert result.retrieval_status == "complete"
    assert [item.chunk.chunk_id for item in result.results] == ["c1", "c2"]
    assert result.evidence_refs == ("ev-c1", "ev-c2")
    assert result.results[0].retrieval_source == ("vector", "bm25")
    assert result.results[0].vector_score == 0.8
    assert result.results[0].bm25_score == 6.0
    assert result.results[0].final_score == pytest.approx((3 * 0.9 + 0.6) / 4)
    assert result.results[1].final_score == pytest.approx((3 * 0.6 + 0.8) / 4)
    assert result.ranking_method == "fused_score_only"
    assert result.rerank_status == "not_configured"
    assert result.config_version == "test-params-v1"
    assert result.vector_calibration_version == "test-vector-v1"
    assert result.bm25_calibration_version == "test-bm25-v1"
    assert [branch.source for branch in result.branches] == ["vector", "bm25"]
    assert all(branch.status == "success" and branch.duration_ms >= 0 for branch in result.branches)


def test_single_route_weight_renormalization_and_independent_thresholds() -> None:
    vector = SearchPort([RetrievalHit(chunk("vector-only"), 0.4, "vector")])
    bm25 = SearchPort([RetrievalHit(chunk("keyword-only"), 8.0, "bm25")])
    result = RagRetriever(vector, bm25, config()).search(
        "查询", tenant_id="t1", knowledge_release_id="r1"
    )
    assert [(item.chunk.chunk_id, item.final_score) for item in result.results] == [
        ("keyword-only", pytest.approx(0.8)),
        ("vector-only", pytest.approx(0.7)),
    ]
    assert result.results[0].vector_score is None
    assert result.results[0].retrieval_source == ("bm25",)
    assert result.results[1].bm25_score is None

    # 分路门槛先于融合门槛；低分 BM25 不参与高分 vector 的加权。
    both = chunk("both")
    result = RagRetriever(
        SearchPort([RetrievalHit(both, 0.8, "vector")]),
        SearchPort([RetrievalHit(both, 2.0, "bm25")]),
        config(),
    ).search("查询", tenant_id="t1", knowledge_release_id="r1")
    assert result.results[0].final_score == pytest.approx(0.9)
    assert result.results[0].retrieval_source == ("vector",)


def test_fixed_calibration_does_not_rescale_single_query_hit() -> None:
    result = RagRetriever(
        SearchPort(),
        SearchPort([RetrievalHit(chunk("only"), 4.0, "bm25")]),
        config(fused_threshold=0.0),
    ).search("查询", tenant_id="t1", knowledge_release_id="r1")
    assert result.results[0].final_score == pytest.approx(0.4)
    assert result.results[0].bm25_score == 4.0


@pytest.mark.parametrize(
    ("vector_hits", "keyword_hits", "vector_failure", "keyword_failure", "expected"),
    [
        ((), (), False, False, "empty"),
        ((0.8,), (), False, False, "complete"),
        ((0.8,), (), False, True, "partial"),
        ((), (), True, False, "degraded"),
        ((), (), True, True, "degraded"),
        ((-0.9,), (), False, False, "empty"),
    ],
)
def test_empty_partial_and_degraded_are_distinct(
    vector_hits, keyword_hits, vector_failure, keyword_failure, expected
) -> None:
    vector = SearchPort(
        [RetrievalHit(chunk("v"), score, "vector") for score in vector_hits],
        failure=vector_failure,
    )
    keyword = SearchPort(
        [RetrievalHit(chunk("b"), score, "bm25") for score in keyword_hits],
        failure=keyword_failure,
    )
    result = RagRetriever(vector, keyword, config()).search(
        "查询", tenant_id="t1", knowledge_release_id="r1"
    )
    assert result.retrieval_status == expected
    assert len(vector.calls) == len(keyword.calls) == 1
    expected_vector = "failed" if vector_failure else "success" if vector_hits else "empty"
    expected_bm25 = "failed" if keyword_failure else "success" if keyword_hits else "empty"
    assert result.vector_status == expected_vector
    assert result.bm25_status == expected_bm25
    if vector_failure:
        assert result.branches[0].error_code == "DEPENDENCY_UNAVAILABLE"
    if keyword_failure:
        assert result.branches[1].error_code == "DEPENDENCY_UNAVAILABLE"
    if expected in ("empty", "degraded"):
        assert result.results == ()


def test_scope_error_and_conflicting_chunk_evidence_fail_closed() -> None:
    wrong_tenant = RetrievalHit(chunk("bad", tenant="other"), 0.9, "vector")
    valid = RetrievalHit(chunk("good"), 8.0, "bm25")
    result = RagRetriever(SearchPort([wrong_tenant]), SearchPort([valid]), config()).search(
        "查询", tenant_id="t1", knowledge_release_id="r1"
    )
    assert result.retrieval_status == "partial"
    assert result.branches[0].error_code == "INVALID_RETRIEVAL_RESULT"
    assert [item.chunk.chunk_id for item in result.results] == ["good"]

    first = chunk("same")
    changed = replace(first, evidence_id="different")
    result = RagRetriever(
        SearchPort([RetrievalHit(first, 0.8, "vector")]),
        SearchPort([RetrievalHit(changed, 8.0, "bm25")]),
        config(),
    ).search("查询", tenant_id="t1", knowledge_release_id="r1")
    assert result.retrieval_status == "degraded"
    assert result.conflicts == ("same",)
    assert result.results == ()


def test_release_mismatch_and_unexpected_adapter_error_are_separate_branch_failures() -> None:
    class BrokenPort(SearchPort):
        def search(self, query, tenant_id, release_id, top_k):
            self.calls.append((query, tenant_id, release_id, top_k))
            raise ValueError("internal adapter detail")

    result = RagRetriever(
        SearchPort([RetrievalHit(chunk("wrong-release", release="old"), 0.9, "vector")]),
        BrokenPort(),
        config(),
    ).search("查询", tenant_id="t1", knowledge_release_id="r1")
    assert result.retrieval_status == "degraded"
    assert result.results == ()
    assert result.branches[0].error_code == "INVALID_RETRIEVAL_RESULT"
    assert result.branches[1].error_code == "RETRIEVAL_ERROR"
    assert all(branch.hits == () for branch in result.branches)


def test_invalid_configuration_and_top_k_rejected_before_search() -> None:
    with pytest.raises(ValueError):
        CalibrationCurve("v", ((0.0, 0.0), (0.0, 1.0)))
    with pytest.raises(ValueError):
        config(vector_weight=0.0)
    with pytest.raises(ValueError):
        config(fused_threshold=1.1)
    vector, keyword = SearchPort(), SearchPort()
    retriever = RagRetriever(vector, keyword, config())
    with pytest.raises(ValueError):
        retriever.search("查询", tenant_id="t1", knowledge_release_id="r1", top_k=4)
    with pytest.raises(ValueError):
        retriever.search(" ", tenant_id="t1", knowledge_release_id="r1")
    assert vector.calls == keyword.calls == []

    result = retriever.search("查询", tenant_id="t1", knowledge_release_id="r1", top_k=1)
    assert result.results == ()
    assert vector.calls[0][-1] == keyword.calls[0][-1] == 20
