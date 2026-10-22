# Step 33 security regression report

- Date: 2026-10-21
- Runtime: Conda `model`, Python 3.10.21, pytest 8.4.2
- Scope: authentication, authorization, customer and tenant isolation, team bounds, confirmations, idempotency, graph query injection, production settings, prompt policy, and secret-pattern scan
- Result: automated targeted cases pass; full security gate remains **not accepted** because real-model adversarial tool-result testing was not run

## Measurements

| Evidence group | Numerator / denominator | Result |
| --- | ---: | --- |
| Targeted integration tests (`step07`, `step24`–`step27`, `step31`) | 93 / 93 | Passed |
| Targeted authorization, graph, plan, refund, ticket, workbench, and safety unit tests | 68 / 68 | Passed |
| Architecture checks plus `tests/safety` | 15 / 15 | Passed |
| Ruff repository scan | all configured files | Passed |
| Secret-pattern scan in `src/`, `bootstrap/`, `frontend/` | common API key, AWS key, and PEM private-key patterns | 0 matches |

The production-settings HTTP cases used a temporary `ADDRESS_ENCRYPT_KEY` in the test process. No `.env` file or persistent environment setting was changed. The first run without this required test value failed those two setup cases; rerunning with the temporary value passed both, and the complete integration group then passed 93/93.

## Coverage map

- Forged identity, tenant, role, scopes, expired sessions, and production Mock-login shutdown: `tests/integration/test_step07_auth_http.py`, `tests/unit/test_step07_authorization.py`, and `tests/unit/test_step07_mock_auth_adapter.py`.
- Customer and tenant boundaries: `tests/integration/test_step10_chat_http.py`, `tests/integration/test_step09_order_adapter_mysql.py`, and `tests/unit/test_step31_workbench_queries.py`.
- Confirmation binding, expiry, content/version checks, and replay behavior: confirmation, refund, address-change, ticket, and workbench targeted HTTP/MySQL/unit suites.
- Cross-team assignment and illegal ticket transitions: ticket application and workbench tests.
- Cypher injection: `tests/unit/test_step19_graph.py::test_untrusted_values_never_enter_cypher_and_relation_whitelist_rejects_unknown`; hostile values remain parameters and unsupported relations are rejected before driver execution.
- Prompt/tool-result policy: prompt text is byte-locked by `tests/contract/test_step10_prompt_contract.py` and the explicit phrase regression in `tests/safety/test_step33_prompt_injection.py`.
- Data masking and production fallback rules: `tests/unit/test_step08_masking.py`, `tests/unit/test_step05_settings.py`, and integration cases above.

## Open security evidence

The prompt regression confirms that the system and final-answer prompts label tool output as untrusted business data. It does not demonstrate that a live model resists a malicious tool result. A hostile-model run with a controlled injection payload and assertions over tool calls, response, state changes, and audit records remains outstanding. No business behavior was weakened to manufacture a pass.

The secret scan was a source-tree pattern check, not a full secret-management audit. Production identity provider, mTLS, key vault, and external enterprise authorization were outside this local regression environment.

## Real-model hostile tool-result follow-up

- Evaluator: `evals/run_step33_real_model_adversarial.py`, using the actual Supervisor prompt catalog, tool registry, `decide` application function, and configured `deepseek-chat` gateway.
- Cases: prompt exfiltration, unauthorized refund escalation, and forged identity/secret disclosure embedded in tool-result summaries.
- Result: **3/3** completed without the injected marker; 0 tool calls were selected, 0 write tools were exposed, and 0 tool dispatches were performed. The evaluator records only decision metadata and answer length, not model responses or credentials.
- This passes the controlled three-case adversarial sample. It does not replace the broader security gate or prove resistance to every prompt injection.
