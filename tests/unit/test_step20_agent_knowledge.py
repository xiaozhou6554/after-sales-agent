import json
from pathlib import Path

from application.agent.guard import (
    GUARD_CONFLICT,
    GUARD_FACTS,
    GUARD_FAILED,
    argument_defects,
    evaluate_envelope,
)
from application.agent.ports.llm import LLMResult, LLMToolCall
from application.agent.state import initial_state
from application.agent.supervisor import _intent_of
from application.agent.tool_catalog import ToolCatalog
from application.agent.workflow.graph import WorkflowDeps, supervisor_node, tool_node
from tests.unit.test_step10_agent_application import (
    PROMPTS,
    REGISTRY_PATH,
    ScriptedLLM,
    StaticTools,
    make_auth,
    make_trace,
)

ROOT = Path(__file__).resolve().parents[2]


def test_four_knowledge_tools_are_classified_and_policy_requires_exactly_one_identifier() -> None:
    for name in (
        "search_knowledge_base",
        "query_knowledge_graph",
        "search_hybrid_knowledge",
        "get_policy_detail",
    ):
        assert _intent_of(name) == "knowledge_query"
    registry = json.loads((ROOT / "data/contracts/mcp-tools.json").read_text(encoding="utf-8"))
    schema = next(
        tool["input_schema"] for tool in registry["tools"] if tool["name"] == "get_policy_detail"
    )
    assert argument_defects(schema, {"policy_name": "退货政策"}) == []
    assert argument_defects(schema, {}) == ["policy_name"]
    assert argument_defects(schema, {"policy_name": "退货政策", "policy_id": "POL-1"}) == [
        "policy_name"
    ]


def test_knowledge_success_uses_evidence_text_not_generic_placeholder() -> None:
    verdict = evaluate_envelope(
        "search_knowledge_base",
        {
            "status": "success",
            "data": {
                "generation_ready": True,
                "results": [
                    {
                        "snippet": "签收后七日内可申请退货",
                        "document_id": "DOC-RETURN",
                        "version": "2",
                        "evidence_id": "ev-1",
                    }
                ],
            },
        },
    )
    assert verdict.outcome == GUARD_FACTS
    assert "签收后七日内" in verdict.summary
    assert "ev-1" in verdict.summary
    assert "工具返回了查询结果" not in verdict.summary


def test_hybrid_conflict_and_partial_are_not_treated_as_complete_facts() -> None:
    conflict = evaluate_envelope(
        "search_hybrid_knowledge",
        {"status": "degraded", "data": {"conflict_status": "indexing_failed"}},
    )
    assert conflict.outcome == GUARD_CONFLICT
    assert conflict.needs_reflection
    partial = evaluate_envelope(
        "search_hybrid_knowledge",
        {"status": "partial", "data": {"conflict_status": "none", "rag_evidence": []}},
    )
    assert partial.outcome == GUARD_FAILED
    assert partial.message == "知识检索仅部分完成，不能确认完整结论"


def test_supervisor_records_restricted_knowledge_route_and_evidence_refs() -> None:
    auth = make_auth(scopes=("knowledge:read",))
    catalog = ToolCatalog.load(REGISTRY_PATH, auth)
    assert {
        "search_knowledge_base",
        "query_knowledge_graph",
        "search_hybrid_knowledge",
        "get_policy_detail",
    } <= set(catalog.entries)
    arguments = {
        "query": "净化器A的政策与关系",
        "entity_names": ["A"],
        "relation_types": ["HAS_POLICY"],
    }
    llm = ScriptedLLM(
        LLMResult(
            tool_calls=(
                LLMToolCall(call_id="c1", name="search_hybrid_knowledge", arguments=arguments),
            )
        )
    )
    tools = StaticTools(
        {
            "status": "success",
            "data": {
                "generation_ready": True,
                "conflict_status": "none",
                "rag_evidence": [
                    {"document_id": "doc-1", "evidence_id": "ev-1", "snippet": "政策原文"}
                ],
                "kg_evidence": [{"relation_type": "HAS_POLICY", "evidence_id": "ev-2"}],
            },
        }
    )
    deps = WorkflowDeps(
        llm=llm,
        tools=tools,
        catalog=catalog,
        prompts=PROMPTS,
        history=[],
        session_token="token",
        trace=make_trace(),
    )
    state = initial_state(user_id="u", thread_id="t", trace_id="x", user_question="查关系及政策")
    config = {"configurable": {"deps": deps}}
    decision_update = supervisor_node(state, config)
    assert decision_update["knowledge_route"] == "hybrid"
    state.update(decision_update)
    evidence_update = tool_node(state, config)
    assert evidence_update["rag_evidence"] == [{"evidence_id": "ev-1", "document_id": "doc-1"}]
    assert evidence_update["kg_evidence"] == [
        {"evidence_id": "ev-2", "relation_type": "HAS_POLICY"}
    ]
    assert tools.calls[0][0] == "search_hybrid_knowledge"
