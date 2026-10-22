# Step 34 retrieval, generation, Agent, and performance evaluation

## Execution identity

- Date: 2026-10-21
- Python: Conda `model`, Python 3.10.21
- Dataset: `mock-v2`, `data/scenarios/v2/knowledge_labels.jsonl`
- Retrieval probe split: development, 4 answerable cases (K16-01, K16-02, K16-03, K16-07)
- Requested knowledge release: `KNOWLEDGE-RELEASE-2026-10`
- Static knowledge manifest status: `draft`; runtime query tools were scoped to the requested release and tenant
- LLM setting: `deepseek-chat` (credential value intentionally not recorded)
- Prompt files SHA256: `1db98d238ac30cf1fa371396c68c9c49cc01b0478492f40c6dd8f919c4a0efa7` (SHA256 over the four prompt files concatenated in `PROMPT_FILES` order)
- MCP tool contract: schema version `1.0`, SHA256 `fcbf5e0777324c0707a8c7a09674be8d18b83fb2dca4aa7dcfb270edcc0b1b4e`

## Retrieval measurements

The repeatable probe is `evals/run_step34_retrieval_probe.py`. Per technical specification 17.1, it records vector and BM25 branches independently, final accepted evidence, KG evidence, and hybrid state.

| Metric | Numerator / denominator | Measured result | Target / interpretation |
| --- | ---: | ---: | --- |
| Development expected-document hits in the vector/BM25 candidate union | 4 / 4 relevant documents | 1.00 | Preliminary candidate coverage only; not the formal Recall@10 result because the 4-case subset is too small and the probe does not use the full frozen labeled set |
| Final accepted-document MRR@3 | reciprocal-rank sum 0 / 4 cases | 0.00 | Failed: the reranker rejected all four calls as unavailable, leaving no generation-accepted evidence |
| Reranker unavailable responses | 4 / 4 cases | 100% | Failed: `RERANK_UNAVAILABLE`; vector and BM25 branches themselves returned successfully |
| KG compatibility probe | 1 / 1 known relation | 1 relation, 1 evidence record | One sample only; not the ≥95% graph-accuracy evaluation |
| KG wrong-release isolation probe | 0 / 1 relation | 0 returned | Passed this negative control |
| Hybrid probe | 1 / 1 question | `partial`, `generation_ready=true`, RAG `degraded`, KG `success`, no conflict | Partial evidence was available through KG; RAG did not provide accepted evidence |

The first retrieval call took 19,204 ms in the combined probe (cold local initialization); the next three took 1,339, 1,286, and 1,379 ms. This is not a P95 latency measurement: `n=4`, the cold start is included, and all four final retrievals were degraded. The standalone single KG query took 97.585 ms and the one hybrid query 1,391.573 ms; neither is a P95 sample.

The reranker failure is traced to the local model integrity check: `README.md` differs from the fixed download manifest although the model weights and tokenizer file hashes match and the MIT marker remains. The manifest and verifier were not changed to hide that mismatch. Branch evidence still contained every labeled expected document (candidate hits 4/4), but final accepted evidence was empty (MRR@3 0/4).

## Generation and Agent evidence

- Browser E2E with the configured DeepSeek model completed a customer order query through MCP and MySQL; the matching audit entry recorded a successful `query_order` invocation. The browser script also completed refund, address-change, and staff-workbench flows with zero page errors.
- A real hybrid-tool attempt with an irrelevant model-selected KG entity returned degraded/empty evidence. A controlled known-entity KG probe returned one compatible relation. Both outcomes are retained; the empty result is not reclassified as a pass.
- The labeled set contains 7 knowledge cases total: 4 answerable development cases, 1 holdout answerable case, 1 holdout no-answer case, and 1 holdout version-sensitive case. The holdout cases were not used to tune this probe.
- No independently human-scored generation set was executed. Factual consistency, relevance, automatic answer rate, and unsupported-answer refusal rate remain unmeasured.

## Performance and completion status

The requirement target is 100 concurrent users for a continuous 30-minute window, with ordinary read-only P95 ≤5 s/P99 ≤10 s, retrieval P95 ≤800 ms, graph P95 ≤500 ms, and availability ≥99.5%. That load window was not run. The report has no numerator/denominator for those service-level targets; they remain unverified and must not be reported as met.

The measurements above are the initial run, before the pinned README was restored. That initial run failed the reranker integrity check. Follow-up evidence and corrected status are recorded below; the historical result is retained for audit.

## Follow-up after pinned model restoration

The local `README.md` was backed up to `evals/reports/2026-10-21-step34-local-README-before-restoration.md`. The replacement was downloaded from the [official README at fixed revision `2cfc18c9415c912f9d8155881c133215df768a70`](https://huggingface.co/BAAI/bge-reranker-base/blob/2cfc18c9415c912f9d8155881c133215df768a70/README.md); its SHA256 equals the existing manifest value `b4223c8c1e95538f8c79168afc501f8b2da06c63221f013b79b28af78b59366d`. The manifest and verification rules were not changed. `bootstrap.verify_knowledge_reranker` loaded the pinned model and all three positive-vs-negative ranking checks passed.

The development probe was rerun with the restored model:

| Metric | Numerator / denominator | Result | Interpretation |
| --- | ---: | ---: | --- |
| Expected-document candidate recall | 4 / 4 | 1.00 | All four development labels were present in the vector/BM25 candidate union; this small slice is not the frozen-set Recall@10 acceptance result. |
| Final MRR@3 | reciprocal-rank sum 3.333333 / 4 cases | 0.833333 | Above the 0.75 target on this four-case development sample. Correct documents appeared at ranks 1, 3, 1, and 1. |
| Reranker status | 4 / 4 | success | No `RERANK_UNAVAILABLE` responses. |
| Final retrieval latency | n=4 | cold 27,000.696 ms; warm 675.624, 466.514, 607.992 ms | Warm-only n=3 is descriptive, not a P95 acceptance sample. |
| KG / hybrid probe | 1 / 1 each | relation evidence present; hybrid complete | Single probes only; not quality or latency acceptance samples. |

The repeatable real-generation evaluator `evals/run_step34_real_generation_eval.py` used all seven frozen `mock-v2` labels and the configured real model. Machine-readable answers and evidence are in `evals/reports/2026-10-21-step34-real-generation-results.json`. Manual review found: development answerable cases 4/4 received evidence-supported answers; the no-answer holdout was refused (1/1); the version-sensitive holdout did not invent the missing 2025 policy (1/1); the South-region answerable holdout was refused because its source document contains only tenant applicability text and no policy rules (0/1 answer coverage). This is a gold-label/data-content conflict in K16-06, which was preserved for adjudication instead of retuned. Unsupported-refusal on the one no-answer example was 100%, while answerable coverage across the five labels marked answerable was 4/5 (80%). The seven-example set is too small for the 90% quality target and remains a preliminary measurement, not acceptance.

## Frozen-set retrieval and performance follow-up

`evals/run_step34_full_retrieval_eval.py` measures fused, pre-rerank Recall@10 and final reranked MRR@3 against all frozen labels that contain relevant documents. On the 6 relevant questions (7 expected relevant documents), it found 6/7 expected documents in the fused top-10, giving Recall@10 **0.857143**; final reranked MRR@3 was **5.333333 / 6 = 0.888889**. Both meet the respective 0.85 and 0.75 thresholds on this frozen set. The only missed expected document was `DOC-RETURN-LEGACY` for K16-05; `DOC-RETURN-CURRENT` was ranked first, so that case's reciprocal rank is 1. Its per-case output, metric denominators, and label SHA256 are in `evals/reports/2026-10-21-step34-full-retrieval-results.json`. The four-case development probe above remains a separate development result.

The 100-user run used authenticated read-only `GET /notifications?limit=20` requests from the synthetic customer against the local development API. It completed the full 1,800-second measured window after a 120-second warmup:

| Metric | Numerator / denominator | Result | Target | Status |
| --- | ---: | ---: | ---: | --- |
| Warmup requests | 400 / 400 | 0 failures | n/a | Passed |
| Measured requests | 6,000 / 6,000 | 0 failures | n/a | Passed |
| Measured duration | 1,799.998 s | 100 virtual users | 1,800 s | Passed |
| Ordinary read P95 | n=6,000 | 140.452 ms | ≤5,000 ms | Passed |
| Ordinary read P99 | n=6,000 | 272.976 ms | ≤10,000 ms | Passed |
| Availability | 6,000 / 6,000 | 100% | ≥99.5% | Passed |

The machine-readable run is `evals/reports/2026-10-21-step34-read-load.json`. It validates this local development API's authenticated notification read path only; it does not establish production capacity or the separate retrieval (≤800 ms) and graph-query (≤500 ms) latency targets. The preliminary generation outcome remains 4/5 answerable-label coverage because K16-06's gold label conflicts with the source text. Independent human factual-consistency and relevance scoring is still required by the specification; no model-as-judge result is substituted for it.
