"""Measure fused Recall@10 and reranked MRR@3 on the frozen knowledge labels."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from bootstrap.knowledge_rag_config import load_development_rag_config
from bootstrap.settings import load_settings
from bootstrap.verify_knowledge_retrieval import PROJECT_ROOT, build_local_retrievers

from application.knowledge.rag import RagRetriever
from infrastructure.retrieval.reranker_adapter import LocalCrossEncoderReranker

ROOT = Path(__file__).resolve().parents[1]
RELEASE_ID = "KNOWLEDGE-RELEASE-2026-10"


def main() -> None:
    settings = load_settings()
    chunks, vector, bm25 = build_local_retrievers(settings)
    if not chunks or chunks[0].release_id != RELEASE_ID:
        raise RuntimeError("Frozen labels and local retrieval release do not match")

    config = load_development_rag_config()
    candidate_retriever = RagRetriever(
        vector,
        bm25,
        replace(config, result_top_k=10),
    )
    reranked_retriever = RagRetriever(
        vector,
        bm25,
        config,
        reranker=LocalCrossEncoderReranker(
            PROJECT_ROOT / "data/generated/models/bge-reranker-base"
        ),
    )
    labels = [
        json.loads(line)
        for line in (ROOT / "data/scenarios/v2/knowledge_labels.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    relevant_cases = [case for case in labels if case["document_ids"]]
    relevant_document_count = sum(len(set(case["document_ids"])) for case in relevant_cases)
    candidate_hits = 0
    reciprocal_rank_sum = 0.0
    results = []

    for case in relevant_cases:
        expected = set(case["document_ids"])
        candidate_result = candidate_retriever.search(
            case["question"],
            tenant_id=case["tenant_id"],
            knowledge_release_id=RELEASE_ID,
            top_k=10,
        )
        candidate_documents = [item.chunk.document_id for item in candidate_result.results]
        candidate_hits += len(expected & set(candidate_documents))

        final_result = reranked_retriever.search(
            case["question"],
            tenant_id=case["tenant_id"],
            knowledge_release_id=RELEASE_ID,
            top_k=3,
        )
        final_documents = [item.chunk.document_id for item in final_result.results]
        rank = next(
            (
                index
                for index, document_id in enumerate(final_documents, start=1)
                if document_id in expected
            ),
            None,
        )
        if rank is not None:
            reciprocal_rank_sum += 1 / rank
        results.append(
            {
                "case_id": case["case_id"],
                "split": case["split"],
                "expected_documents": sorted(expected),
                "fused_candidates_top_10": candidate_documents,
                "final_reranked_top_3": final_documents,
                "rerank_status": final_result.rerank_status,
                "first_relevant_rank": rank,
            }
        )

    report = {
        "dataset_version": "mock-v2",
        "label_sha256": hashlib.sha256(
            (ROOT / "data/scenarios/v2/knowledge_labels.jsonl").read_bytes()
        ).hexdigest(),
        "knowledge_release_id": RELEASE_ID,
        "relevant_case_count": len(relevant_cases),
        "relevant_document_count": relevant_document_count,
        "fused_candidate_document_hits": candidate_hits,
        "recall_at_10": round(candidate_hits / relevant_document_count, 6),
        "mrr_at_3_reciprocal_sum": round(reciprocal_rank_sum, 6),
        "mrr_at_3_denominator": len(relevant_cases),
        "mrr_at_3": round(reciprocal_rank_sum / len(relevant_cases), 6),
        "results": results,
    }
    output = ROOT / "evals/reports/2026-10-21-step34-full-retrieval-results.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {**report, "output": output.relative_to(ROOT).as_posix()},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
