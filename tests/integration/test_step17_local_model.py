"""步骤17真实本地 BGE + Chroma + BM25；无模型的环境只跳过此资源型验收。"""

from __future__ import annotations

from pathlib import Path

import pytest
from bootstrap.settings import load_settings
from bootstrap.verify_knowledge_retrieval import build_local_retrievers

from application.knowledge.retrieval.branch import run_search_branch
from application.knowledge.retrieval.ports import RetrievalUnavailable
from infrastructure.retrieval.bm25_adapter import BM25Adapter

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = load_settings()
MODEL_DIR = ROOT / SETTINGS.embedding_model_path
pytestmark = pytest.mark.skipif(
    not (MODEL_DIR / "download-manifest.json").is_file(),
    reason="需先运行 python -m bootstrap.download_embedding_model 获取固定提交的本地BGE模型",
)


@pytest.fixture(scope="module")
def indices():
    return build_local_retrievers(SETTINGS)


def test_real_vector_synonym_and_bm25_error_code(indices) -> None:
    chunks, vector, bm25 = indices
    release = chunks[0].release_id
    semantic = vector.search("退回商品的申请期限是多少？", "tenant-north", release, 20)
    assert semantic and semantic[0].chunk.document_id == "DOC-RETURN-CURRENT"
    exact = bm25.search("F23 故障如何处理？", "tenant-north", release, 20)
    assert exact and exact[0].chunk.document_id == "DOC-FAULT-F23"
    for hit in semantic + exact:
        assert hit.chunk.tenant_id == "tenant-north"
        assert hit.chunk.release_id == release
        assert hit.chunk.status == "published"
        assert hit.chunk.document_id != "DOC-RETURN-DRAFT"


def test_real_indices_reject_wrong_tenant_and_release(indices) -> None:
    chunks, vector, bm25 = indices
    release = chunks[0].release_id
    assert vector.search("F23", "tenant-unknown", release, 20) == []
    assert bm25.search("F23", "tenant-unknown", release, 20) == []
    with pytest.raises(ValueError, match="发布版本"):
        vector.search("F23", "tenant-north", "wrong-release", 20)
    with pytest.raises(ValueError, match="发布版本"):
        bm25.search("F23", "tenant-north", "wrong-release", 20)


def test_branch_empty_is_not_dependency_failure(indices) -> None:
    chunks, vector, _ = indices
    release = chunks[0].release_id
    empty = run_search_branch("vector", lambda: vector.search("F23", "tenant-unknown", release, 20))
    assert empty.status == "empty" and empty.error_code is None
    unbuilt = BM25Adapter()
    failed = run_search_branch("bm25", lambda: unbuilt.search("F23", "tenant-north", release, 20))
    assert failed.status == "error" and failed.error_code == "DEPENDENCY_UNAVAILABLE"
    with pytest.raises(RetrievalUnavailable):
        unbuilt.search("F23", "tenant-north", release, 20)
