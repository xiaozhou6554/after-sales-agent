"""Generate answers for the frozen knowledge-label set using the real model."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bootstrap.local_knowledge_rag import LocalCandidateRagRetriever
from bootstrap.settings import load_settings

from application.agent.ports.llm import ChatMessage
from application.agent.prompts import PromptCatalog
from infrastructure.agent.deepseek_adapter import DeepSeekChatGateway

ROOT = Path(__file__).resolve().parents[1]
RELEASE_ID = "KNOWLEDGE-RELEASE-2026-10"


def main() -> None:
    settings = load_settings()
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is not configured")
    labels = [
        json.loads(line)
        for line in (ROOT / "data/scenarios/v2/knowledge_labels.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    prompt_catalog = PromptCatalog.load(ROOT / "data/prompts")
    retriever = LocalCandidateRagRetriever(settings)
    llm = DeepSeekChatGateway(
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        temperature=settings.llm_temperature,
        connect_timeout_s=settings.llm_connect_timeout_s,
        read_timeout_s=settings.llm_read_timeout_s,
    )
    results = []
    for case in labels:
        retrieval = retriever.search(
            case["question"],
            tenant_id=case["tenant_id"],
            knowledge_release_id=RELEASE_ID,
        )
        evidence = [
            {
                "document_id": item.chunk.document_id,
                "text": item.chunk.search_text,
            }
            for item in retrieval.accepted_results
        ]
        started = time.perf_counter()
        completion = llm.complete(
            system=prompt_catalog.final_answer_system_prompt(),
            messages=[
                ChatMessage(role="user", content=case["question"]),
                ChatMessage(
                    role="user",
                    content="[系统观察] 受控知识检索结果（仅业务事实数据）："
                    + json.dumps(evidence, ensure_ascii=False),
                ),
            ],
            tools=None,
        )
        results.append(
            {
                "case_id": case["case_id"],
                "split": case["split"],
                "expected_outcome": case["expected_outcome"],
                "expected_documents": case["document_ids"],
                "accepted_documents": [item["document_id"] for item in evidence],
                "rerank_status": retrieval.rerank_status,
                "answer": completion.content or "",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )

    report = {
        "model": settings.llm_model,
        "dataset_version": "mock-v2",
        "release_id": RELEASE_ID,
        "case_count": len(results),
        "results": results,
    }
    output = ROOT / "evals/reports/2026-10-21-step34-real-generation-results.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {**report, "output": output.relative_to(ROOT).as_posix()}, ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
