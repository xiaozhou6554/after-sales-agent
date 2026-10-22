"""固定参数验证完整双路融合+Cross-Encoder；holdout 只验收不调参。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from application.knowledge.rag import RagRetriever
from bootstrap.knowledge_rag_config import load_development_rag_config
from bootstrap.settings import load_settings
from bootstrap.verify_knowledge_retrieval import build_local_retrievers
from infrastructure.retrieval.reranker_adapter import LocalCrossEncoderReranker

ROOT = Path(__file__).resolve().parents[1]


def assess_case(case: dict, documents: list[str], generation_ready: bool) -> str:
    outcome = case["expected_outcome"]
    if outcome == "answerable":
        return (
            "passed"
            if generation_ready and set(case["document_ids"]) & set(documents)
            else "failed"
        )
    if outcome == "no_answer":
        return "passed" if not documents and not generation_ready else "failed"
    if outcome == "version_sensitive":
        # 当前发布只检索有效版本；历史比较须另有历史版本读取能力和人工核验。
        return "manual_version_review_required"
    raise ValueError(f"未知验收标签: {outcome}")


def main() -> None:
    settings = load_settings()
    if settings.app_env == "production":
        raise RuntimeError("开发 Chroma 校准验证不可用于生产验收")
    chunks, vector, bm25 = build_local_retrievers(settings)
    reranker = LocalCrossEncoderReranker(ROOT / "data/generated/models/bge-reranker-base")
    config = load_development_rag_config()
    retriever = RagRetriever(vector, bm25, config, reranker=reranker)
    # 模型加载和线程池预热不计入稳态查询延迟；单独记录冷启动。
    warm_start = time.perf_counter()
    reranker.score("预热", ["预热文本"])
    cold_start_ms = round((time.perf_counter() - warm_start) * 1000, 2)
    cases = [
        json.loads(line)
        for line in (ROOT / "data/scenarios/v2/knowledge_labels.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    observations = []
    for case in cases:
        started = time.perf_counter()
        result = retriever.search(
            case["question"],
            tenant_id=case["tenant_id"],
            knowledge_release_id=chunks[0].release_id,
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        documents = [item.chunk.document_id for item in result.accepted_results]
        observations.append(
            {
                "case_id": case["case_id"],
                "split": case["split"],
                "expected_outcome": case["expected_outcome"],
                "expected_documents": case["document_ids"],
                "returned_documents": documents,
                "expected_in_top_3": bool(set(case["document_ids"]) & set(documents)),
                "acceptance": assess_case(case, documents, result.generation_ready),
                "generation_ready": result.generation_ready,
                "retrieval_status": result.retrieval_status,
                "rerank_status": result.rerank_status,
                "duration_ms": duration_ms,
            }
        )
    failures = [item["case_id"] for item in observations if item["acceptance"] == "failed"]
    print(
        json.dumps(
            {
                "release_status": "draft_not_activated",
                "config_version": config.version,
                "automated_gate": "failed" if failures else "passed_with_version_review_pending",
                "failed_cases": failures,
                "cold_start_ms": cold_start_ms,
                "observations": observations,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
