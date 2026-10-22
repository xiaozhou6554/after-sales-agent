"""重排必须在最终相关性接受前完成；本测试不测模型质量。"""

from __future__ import annotations

import hashlib
import json
import sys
import types

import pytest

from application.knowledge.rag import CalibrationCurve, RagRetriever, RerankUnavailable
from application.knowledge.retrieval.ports import RetrievalHit
from infrastructure.retrieval import reranker_download as download
from infrastructure.retrieval.reranker_adapter import LocalCrossEncoderReranker
from tests.unit.test_step18_rag import SearchPort, chunk, config


class FakeReranker:
    model_id = "test-cross-encoder"
    model_revision = "fixed-revision"

    def __init__(self, scores=(), *, failure=False):
        self.scores = scores
        self.failure = failure
        self.calls = []

    def score(self, query, passages):
        self.calls.append((query, tuple(passages)))
        if self.failure:
            raise RerankUnavailable("model absent")
        if len(passages) == 2 * len(self.scores):
            return tuple(self.scores) + tuple(self.scores)
        return self.scores


def rerank_config(**changes):
    return config(
        rerank_calibration=CalibrationCurve("test-rerank-v1", ((-5.0, 0.0), (5.0, 1.0))),
        rerank_threshold=0.5,
        **changes,
    )


def test_rerank_before_top_k_and_relevance_acceptance() -> None:
    first, second = chunk("first"), chunk("second")
    vector = SearchPort(
        [
            RetrievalHit(first, 0.9, "vector"),
            RetrievalHit(second, 0.4, "vector"),
        ]
    )
    reranker = FakeReranker((-2.0, 3.0))
    result = RagRetriever(vector, SearchPort(), rerank_config(), reranker=reranker).search(
        "查询", tenant_id="t1", knowledge_release_id="r1", top_k=1
    )
    assert reranker.calls == [
        (
            "查询",
            ("content-first", "content-second", "search-first", "search-second"),
        )
    ]
    assert [item.chunk.chunk_id for item in result.results] == ["second"]
    assert result.results[0].final_score == pytest.approx(0.7)
    assert result.results[0].rerank_score == 3.0
    assert result.results[0].rerank_relevance == pytest.approx(0.8)
    assert result.accepted_results == result.results
    assert result.generation_ready
    assert result.retrieval_status == "complete"
    assert result.ranking_method == "cross_encoder"
    assert result.rerank_status == "success"
    assert result.rerank_model_id == "test-cross-encoder"
    assert result.rerank_model_revision == "fixed-revision"
    assert result.rerank_calibration_version == "test-rerank-v1"


def test_cross_encoder_order_differs_from_fused_order() -> None:
    first, second = chunk("first"), chunk("second")
    result = RagRetriever(
        SearchPort(
            [
                RetrievalHit(first, 0.9, "vector"),
                RetrievalHit(second, 0.4, "vector"),
            ]
        ),
        SearchPort(),
        rerank_config(),
        reranker=FakeReranker((1.0, 3.0)),
    ).search("查询", tenant_id="t1", knowledge_release_id="r1")
    assert [item.chunk.chunk_id for item in result.accepted_results] == ["second", "first"]
    assert result.results[0].final_score < result.results[1].final_score
    assert result.results[0].rerank_score > result.results[1].rerank_score


def test_either_content_or_search_metadata_can_supply_relevant_view() -> None:
    result = RagRetriever(
        SearchPort([RetrievalHit(chunk("candidate"), 0.9, "vector")]),
        SearchPort(),
        rerank_config(),
        reranker=FakeReranker((-4.0, 3.0)),
    ).search("查询", tenant_id="t1", knowledge_release_id="r1")
    assert result.generation_ready
    assert result.results[0].rerank_score == 3.0


def test_no_reranker_candidates_are_not_generation_ready() -> None:
    hit = RetrievalHit(chunk("candidate"), 0.9, "vector")
    result = RagRetriever(SearchPort([hit]), SearchPort(), config()).search(
        "查询", tenant_id="t1", knowledge_release_id="r1"
    )
    assert len(result.results) == 1  # 兼容旧观测 DTO 的初排候选
    assert result.accepted_results == ()
    assert not result.generation_ready
    assert result.rerank_status == "not_configured"


@pytest.mark.parametrize("scores", [(-4.0,), (float("nan"),), ()])
def test_rejects_low_or_invalid_rerank_results(scores) -> None:
    reranker = FakeReranker(scores)
    result = RagRetriever(
        SearchPort([RetrievalHit(chunk("candidate"), 0.9, "vector")]),
        SearchPort(),
        rerank_config(),
        reranker=reranker,
    ).search("查询", tenant_id="t1", knowledge_release_id="r1")
    assert result.results == ()
    assert not result.generation_ready
    if scores == (-4.0,):
        assert result.retrieval_status == "empty"
        assert result.rerank_status == "success"
        assert result.rerank_error_code is None
    else:
        assert result.retrieval_status == "degraded"
        assert result.rerank_status == "error"
        assert result.rerank_error_code == "RERANK_ERROR"


def test_reranker_failure_does_not_leak_unaccepted_candidates() -> None:
    result = RagRetriever(
        SearchPort([RetrievalHit(chunk("candidate"), 0.9, "vector")]),
        SearchPort(),
        rerank_config(),
        reranker=FakeReranker(failure=True),
    ).search("查询", tenant_id="t1", knowledge_release_id="r1")
    assert result.retrieval_status == "degraded"
    assert result.rerank_error_code == "RERANK_UNAVAILABLE"
    assert result.results == result.accepted_results == ()


def test_partial_branch_can_only_offer_rerank_accepted_evidence() -> None:
    result = RagRetriever(
        SearchPort([RetrievalHit(chunk("candidate"), 0.9, "vector")]),
        SearchPort(failure=True),
        rerank_config(),
        reranker=FakeReranker((2.0,)),
    ).search("查询", tenant_id="t1", knowledge_release_id="r1")
    assert result.retrieval_status == "partial"
    assert result.bm25_status == "failed"
    assert result.generation_ready
    assert result.results[0].rerank_score == 2.0


def test_reranker_configuration_required_and_empty_skips_inference() -> None:
    with pytest.raises(ValueError):
        RagRetriever(SearchPort(), SearchPort(), config(), reranker=FakeReranker())
    with pytest.raises(ValueError):
        config(rerank_threshold=0.5)
    reranker = FakeReranker()
    result = RagRetriever(SearchPort(), SearchPort(), rerank_config(), reranker=reranker).search(
        "查询", tenant_id="t1", knowledge_release_id="r1"
    )
    assert result.retrieval_status == "empty"
    assert result.rerank_status == "skipped_empty"
    assert reranker.calls == []


def _fake_model_dir(root, monkeypatch):
    target = root / "data" / "generated" / "models" / "bge-reranker-base"
    target.mkdir(parents=True)
    for name in download.MODEL_FILES:
        content = b"---\nlicense: mit\n---\n" if name == "README.md" else name.encode()
        (target / name).write_bytes(content)
    monkeypatch.setattr(
        download, "WEIGHTS_SHA256", hashlib.sha256(b"model.safetensors").hexdigest()
    )
    manifest = {
        "source": f"https://huggingface.co/{download.MODEL_ID}",
        "revision": download.MODEL_REVISION,
        "license": download.MODEL_LICENSE,
        "files_sha256": {
            name: hashlib.sha256((target / name).read_bytes()).hexdigest()
            for name in download.MODEL_FILES
        },
    }
    (target / download.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return target


def test_offline_adapter_verifies_files_and_uses_safe_local_loading(tmp_path, monkeypatch) -> None:
    model_dir = _fake_model_dir(tmp_path, monkeypatch)
    created = []

    class FakeCrossEncoder:
        def __init__(self, path, **kwargs):
            created.append((path, kwargs))

        def predict(self, pairs, **kwargs):
            assert pairs == [("查询", "第一段"), ("查询", "第二段")]
            assert kwargs["batch_size"] == 2
            assert kwargs["activation_fn"](-3.0) == -3.0
            return [2.0, -3.0]

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.SimpleNamespace(CrossEncoder=FakeCrossEncoder)
    )
    adapter = LocalCrossEncoderReranker(model_dir, batch_size=2)
    assert adapter.score("查询", ("第一段", "第二段")) == (2.0, -3.0)
    assert created[0][0] == str(model_dir)
    assert created[0][1]["local_files_only"] is True
    assert created[0][1]["trust_remote_code"] is False
    assert created[0][1]["model_kwargs"] == {"use_safetensors": True}


def test_tampered_model_or_pickle_file_is_rejected_before_load(tmp_path, monkeypatch) -> None:
    model_dir = _fake_model_dir(tmp_path, monkeypatch)
    (model_dir / "config.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(RerankUnavailable):
        LocalCrossEncoderReranker(model_dir).score("查询", ("证据",))
    (model_dir / "config.json").write_bytes(b"config.json")
    (model_dir / "pytorch_model.bin").write_bytes(b"pickle")
    with pytest.raises(RerankUnavailable):
        LocalCrossEncoderReranker(model_dir).score("查询", ("证据",))


def test_downloader_pins_revision_and_safe_allowlist(tmp_path, monkeypatch) -> None:
    expected_target = tmp_path / "data" / "generated" / "models" / "bge-reranker-base"
    expected_target.mkdir(parents=True)
    observed = []

    def fake_snapshot_download(**kwargs):
        observed.append(kwargs)
        for name in download.MODEL_FILES:
            content = b"---\nlicense: mit\n---\n" if name == "README.md" else name.encode()
            (expected_target / name).write_bytes(content)

    monkeypatch.setattr(
        download, "WEIGHTS_SHA256", hashlib.sha256(b"model.safetensors").hexdigest()
    )
    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(snapshot_download=fake_snapshot_download),
    )
    assert download.download_reranker(tmp_path) == expected_target
    assert observed[0]["repo_id"] == download.MODEL_ID
    assert observed[0]["revision"] == download.MODEL_REVISION
    assert observed[0]["allow_patterns"] == list(download.MODEL_FILES)
    assert "*.bin" in observed[0]["ignore_patterns"]
    assert download.download_reranker(tmp_path) == expected_target
    assert len(observed) == 1
