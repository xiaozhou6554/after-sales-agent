"""步骤17本地索引构建和开发样例验证；不激活知识发布版本。"""

from __future__ import annotations

import json
from pathlib import Path

from application.knowledge.retrieval.branch import run_search_branch
from bootstrap.settings import RuntimeSettings, load_settings
from infrastructure.document.knowledge_loader import load_manifest_chunks
from infrastructure.retrieval.bm25_adapter import BM25Adapter
from infrastructure.retrieval.embedding_adapter import LocalSentenceEmbedding
from infrastructure.retrieval.vector_adapter import ChromaVectorAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_local_retrievers(settings: RuntimeSettings):
    chunks = load_manifest_chunks(
        PROJECT_ROOT / "data/knowledge/v2/corpus.json",
        PROJECT_ROOT / "data/manifests/knowledge-v2.json",
    )
    model_dir = Path(settings.embedding_model_path)
    if not model_dir.is_absolute():
        model_dir = PROJECT_ROOT / model_dir
    persist_dir = Path(settings.chroma_persist_dir)
    if not persist_dir.is_absolute():
        persist_dir = PROJECT_ROOT / persist_dir
    embedder = LocalSentenceEmbedding(
        settings.embedding_model, model_dir, settings.embedding_model_revision
    )
    bm25 = BM25Adapter()
    bm25.build(chunks)
    vector = ChromaVectorAdapter.persistent(
        persist_dir,
        embedder,
        settings.embedding_dimension,
        hnsw_m=settings.hnsw_m,
        ef_construction=settings.hnsw_ef_construction,
        ef_search=settings.hnsw_ef_search,
    )
    vector.build(chunks)
    return chunks, vector, bm25


def main() -> None:
    settings = load_settings()
    chunks, vector, bm25 = build_local_retrievers(settings)
    release_id = chunks[0].release_id
    cases = (
        ("F23 故障如何处理？", "DOC-FAULT-F23"),
        ("净化器A能使用F-01滤芯吗？", "DOC-FAQ-FILTER-01"),
        ("退货要在签收后几天申请？", "DOC-RETURN-CURRENT"),
        ("退回商品的申请期限是多少？", "DOC-RETURN-CURRENT"),
    )
    results = []
    for query, expected in cases:
        branches = [
            run_search_branch(
                "vector",
                lambda q=query: vector.search(q, "tenant-north", release_id, settings.vector_top_k),
            ),
            run_search_branch(
                "bm25",
                lambda q=query: bm25.search(q, "tenant-north", release_id, settings.bm25_top_k),
            ),
        ]
        results.append(
            {
                "query": query,
                "expected_document_id": expected,
                "branches": [
                    {
                        "source": branch.source,
                        "status": branch.status,
                        "duration_ms": round(branch.duration_ms, 2),
                        "top_documents": [hit.chunk.document_id for hit in branch.hits[:3]],
                        "expected_in_top_3": any(
                            hit.chunk.document_id == expected for hit in branch.hits[:3]
                        ),
                    }
                    for branch in branches
                ],
            }
        )
    print(json.dumps({"release_id": release_id, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
