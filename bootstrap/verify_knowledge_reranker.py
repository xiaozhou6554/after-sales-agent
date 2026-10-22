"""冻结开发样例的真实 Cross-Encoder 分数观测；不激活发布版本。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from infrastructure.document.knowledge_loader import load_manifest_chunks
from infrastructure.retrieval.reranker_adapter import LocalCrossEncoderReranker

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ("退回商品的申请期限是多少？", "DOC-RETURN-CURRENT", "DOC-FAULT-F23"),
    ("F23 故障如何处理？", "DOC-FAULT-F23", "DOC-RETURN-CURRENT"),
    ("净化器A能使用F-01滤芯吗？", "DOC-FAQ-FILTER-01", "DOC-RETURN-CURRENT"),
)


def main() -> None:
    chunks = load_manifest_chunks(
        ROOT / "data/knowledge/v2/corpus.json", ROOT / "data/manifests/knowledge-v2.json"
    )
    documents = {
        item.document_id: item
        for item in chunks
        if item.tenant_id == "tenant-north" and item.status == "published"
    }
    reranker = LocalCrossEncoderReranker(ROOT / "data/generated/models/bge-reranker-base")
    observations = []
    for query, positive, negative in CASES:
        started = time.perf_counter()
        scores = reranker.score(query, [documents[positive].content, documents[negative].content])
        observations.append(
            {
                "query": query,
                "positive_document_id": positive,
                "negative_document_id": negative,
                "positive_raw_score": scores[0],
                "negative_raw_score": scores[1],
                "positive_above_negative": scores[0] > scores[1],
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        )
    print(
        json.dumps(
            {"release_status": "draft_not_activated", "observations": observations},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
