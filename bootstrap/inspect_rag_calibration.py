"""仅分析冻结 development 标签的原始双路和重排分数；不读取 holdout。"""

from __future__ import annotations

import json
from pathlib import Path

from bootstrap.settings import load_settings
from bootstrap.verify_knowledge_retrieval import build_local_retrievers
from infrastructure.retrieval.reranker_adapter import LocalCrossEncoderReranker

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    settings = load_settings()
    chunks, vector, bm25 = build_local_retrievers(settings)
    release_id = chunks[0].release_id
    reranker = LocalCrossEncoderReranker(ROOT / "data/generated/models/bge-reranker-base")
    cases = [
        json.loads(line)
        for line in (ROOT / "data/scenarios/v2/knowledge_labels.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    output = []
    for case in cases:
        if case["split"] != "development":
            continue
        query, tenant = case["question"], case["tenant_id"]
        vector_hits = vector.search(query, tenant, release_id, settings.vector_top_k)
        bm25_hits = bm25.search(query, tenant, release_id, settings.bm25_top_k)
        candidates = {hit.chunk.chunk_id: hit.chunk for hit in vector_hits + bm25_hits}
        passages = list(candidates.values())
        rerank = reranker.score(query, [chunk.search_text for chunk in passages])
        by_id = {chunk.chunk_id: score for chunk, score in zip(passages, rerank)}
        output.append(
            {
                "case_id": case["case_id"],
                "expected_documents": case["document_ids"],
                "vector": [[hit.chunk.document_id, round(hit.score, 4)] for hit in vector_hits],
                "bm25": [[hit.chunk.document_id, round(hit.score, 4)] for hit in bm25_hits],
                "rerank": sorted(
                    [[chunk.document_id, round(by_id[chunk.chunk_id], 4)] for chunk in passages],
                    key=lambda item: -item[1],
                ),
            }
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
