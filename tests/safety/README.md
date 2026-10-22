# Step 33 security regression map

This directory contains targeted security regressions for controls shared across
the customer and staff flows. Existing unit, HTTP, contract, and MySQL scenarios
remain the source of truth for their original assertions.

| Security case | Existing evidence |
| --- | --- |
| Prompt and tool-result instructions cannot replace system policy | `test_step33_prompt_injection.py`; prompt byte-for-byte contract in `tests/contract/test_step10_prompt_contract.py` |
| Hostile graph values stay parameterized and relationship names are allowlisted | `tests/unit/test_step19_graph.py::test_untrusted_values_never_enter_cypher_and_relation_whitelist_rejects_unknown` |
| Forged identity, role, tenant, scopes, expired token, and production Mock login | `tests/integration/test_step07_auth_http.py`; `tests/unit/test_step07_authorization.py`; `tests/unit/test_step07_mock_auth_adapter.py` |
| Cross-customer and cross-tenant reads fail closed | `tests/integration/test_step10_chat_http.py`; `tests/integration/test_step09_order_adapter_mysql.py`; `tests/unit/test_step31_workbench_queries.py` |
| Confirmation binding, expiry, content/version mismatch, and idempotent replay | `tests/integration/test_step24_confirmation_http.py`; refund/address/ticket/workbench unit and MySQL tests |
| Team-scoped assignment and illegal ticket transitions | `tests/unit/test_step27_ticket_application.py`; `tests/integration/test_step27_ticket_mysql.py`; `tests/integration/test_step31_workbench_mysql.py` |
| Production configuration forbids memory fallback and requires independent secrets | `tests/unit/test_step05_settings.py`; production HTTP cases in `tests/integration/test_step07_auth_http.py` |

The tests in this folder add explicit prompt-injection wording coverage. They do
not claim to replace hostile-model evaluation; that evidence is recorded
separately when model-based safety cases run.
