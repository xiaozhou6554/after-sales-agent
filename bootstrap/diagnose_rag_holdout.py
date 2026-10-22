"""只读定位 K16-06 从召回到重排的断点；不得用输出直接调参。"""

from __future__ import annotations

import json
from pathlib import Path

from bootstrap.settings import load_settings
from bootstrap.verify_knowledge_retrieval import build_local_retrievers
from infrastructure.retrieval.reranker_adapter import LocalCrossEncoderReranker

ROOT = Path(__file__).resolve().parents[1]
QUERY = "南区退货政策是什么？"


def main() -> None:
    settings = load_settings()
    chunks, vector, bm25 = build_local_retrievers(settings)
    release_id = chunks[0].release_id
    tenant_id = "tenant-south"
    vector_hits = vector.search(QUERY, tenant_id, release_id, 20)
    bm25_hits = bm25.search(QUERY, tenant_id, release_id, 20)
    candidates = {hit.chunk.chunk_id: hit.chunk for hit in vector_hits + bm25_hits}
    reranker = LocalCrossEncoderReranker(ROOT / "data/generated/models/bge-reranker-base")
    raw_content = reranker.score(QUERY, [chunk.content for chunk in candidates.values()])
    raw_search = reranker.score(QUERY, [chunk.search_text for chunk in candidates.values()])
    print(
        json.dumps(
            {
                "vector": [[hit.chunk.document_id, hit.score] for hit in vector_hits],
                "bm25": [[hit.chunk.document_id, hit.score] for hit in bm25_hits],
                "candidates": [
                    {
                        "document_id": chunk.document_id,
                        "title": chunk.title,
                        "content_raw": content_score,
                        "search_text_raw": search_score,
                    }
                    for chunk, content_score, search_score in zip(
                        candidates.values(), raw_content, raw_search
                    )
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
