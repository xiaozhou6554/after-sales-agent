"""步骤17候选：manifest 一致性、关键词召回与向量端口契约。"""

from __future__ import annotations

import hashlib
import json
import sys
import types
from dataclasses import replace
from pathlib import Path

import chromadb
import pytest
from bootstrap.settings import load_settings

from application.knowledge.retrieval.branch import run_search_branch
from application.knowledge.retrieval.ports import RetrievalUnavailable
from infrastructure.document.knowledge_loader import load_manifest_chunks
from infrastructure.retrieval.bm25_adapter import BM25Adapter
from infrastructure.retrieval.embedding_adapter import LocalSentenceEmbedding
from infrastructure.retrieval.vector_adapter import ChromaVectorAdapter

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "data" / "knowledge" / "v2" / "corpus.json"
MANIFEST = ROOT / "data" / "manifests" / "knowledge-v2.json"


class FakeEmbedding:
    """仅测 Chroma 契约；不代表真实语义召回。"""

    model_id = "fake-embedding-512"

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for item in texts:
            digest = hashlib.sha256(item.encode("utf-8")).digest()
            vectors.append([float(digest[index % len(digest)] + 1) for index in range(512)])
        return vectors


def test_manifest_locked_chunks_and_tamper_rejection(tmp_path: Path) -> None:
    chunks = load_manifest_chunks(CORPUS, MANIFEST)
    assert len(chunks) == 10
    assert len({chunk.chunk_id for chunk in chunks}) == 10
    assert {chunk.release_id for chunk in chunks} == {"KNOWLEDGE-RELEASE-2026-10"}

    tampered = json.loads(CORPUS.read_text(encoding="utf-8"))
    tampered["documents"][0]["content"] = "tampered"
    changed = tmp_path / "corpus.json"
    changed.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="内容或状态不匹配"):
        load_manifest_chunks(changed, MANIFEST)


def test_bm25_exact_code_scope_and_empty_vs_unavailable() -> None:
    chunks = load_manifest_chunks(CORPUS, MANIFEST)
    bm25 = BM25Adapter()
    with pytest.raises(RetrievalUnavailable):
        bm25.search("F23", "tenant-north", chunks[0].release_id, 20)
    bm25.build(chunks)
    hits = bm25.search("F23 故障", "tenant-north", chunks[0].release_id, 20)
    assert hits and hits[0].chunk.document_id == "DOC-FAULT-F23"
    assert all(hit.chunk.tenant_id == "tenant-north" for hit in hits)
    assert bm25.search("F23", "tenant-unknown", chunks[0].release_id, 20) == []
    assert bm25.search("", "tenant-north", chunks[0].release_id, 20) == []
    with pytest.raises(ValueError, match="发布版本"):
        bm25.search("F23", "tenant-north", "wrong-release", 20)


def test_chroma_dimension_release_and_tenant_contract(tmp_path: Path) -> None:
    chunks = load_manifest_chunks(CORPUS, MANIFEST)
    settings = load_settings()
    vector = ChromaVectorAdapter(
        chromadb.PersistentClient(path=str(tmp_path)),
        FakeEmbedding(),
        settings.embedding_dimension,
        hnsw_m=settings.hnsw_m,
        ef_construction=settings.hnsw_ef_construction,
        ef_search=settings.hnsw_ef_search,
    )
    with pytest.raises(RetrievalUnavailable):
        vector.search("F23", "tenant-north", chunks[0].release_id, 20)
    vector.build(chunks)
    hits = vector.search("F23", "tenant-north", chunks[0].release_id, 20)
    assert hits and all(hit.chunk.tenant_id == "tenant-north" for hit in hits)
    assert all(hit.chunk.status == "published" and hit.source == "vector" for hit in hits)
    assert vector.search("F23", "tenant-unknown", chunks[0].release_id, 20) == []
    with pytest.raises(ValueError, match="发布版本"):
        vector.search("F23", "tenant-north", "wrong-release", 20)
    with pytest.raises(ValueError, match="不匹配"):
        vector.build([replace(chunks[0], content="tampered"), *chunks[1:]])


def test_chroma_rejects_wrong_embedding_dimension(tmp_path: Path) -> None:
    class BadEmbedding(FakeEmbedding):
        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] * 511 for _ in texts]

    settings = load_settings()
    vector = ChromaVectorAdapter(
        chromadb.PersistentClient(path=str(tmp_path)),
        BadEmbedding(),
        settings.embedding_dimension,
        hnsw_m=settings.hnsw_m,
        ef_construction=settings.hnsw_ef_construction,
        ef_search=settings.hnsw_ef_search,
    )
    with pytest.raises(ValueError, match="维度"):
        vector.build(load_manifest_chunks(CORPUS, MANIFEST))


def test_local_embedding_missing_model_is_dependency_failure(monkeypatch) -> None:
    def missing_model(model_id: str, **kwargs):
        assert model_id == "BAAI/bge-small-zh-v1.5"
        assert kwargs["local_files_only"] is True
        assert kwargs["trust_remote_code"] is False
        raise FileNotFoundError("model not installed")

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=missing_model),
    )
    with pytest.raises(RetrievalUnavailable, match="本地 Embedding"):
        LocalSentenceEmbedding("BAAI/bge-small-zh-v1.5").encode(["退货政策"])


def test_branch_observation_distinguishes_empty_and_failure() -> None:
    times = iter([1.0, 1.01, 2.0, 2.02])
    empty = run_search_branch("bm25", lambda: [], clock=lambda: next(times))
    assert empty.status == "empty" and empty.error_code is None
    assert empty.duration_ms == pytest.approx(10.0)

    def unavailable():
        raise RetrievalUnavailable("index offline")

    failed = run_search_branch("vector", unavailable, clock=lambda: next(times))
    assert failed.status == "error" and failed.error_code == "DEPENDENCY_UNAVAILABLE"
    assert failed.hits == () and failed.duration_ms == pytest.approx(20.0)
