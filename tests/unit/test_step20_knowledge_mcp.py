from dataclasses import replace
from datetime import date

from application.knowledge.hybrid import HybridResult
from application.knowledge.policy.reader import PolicyCandidate, PolicyDetail, PolicyResult
from interface.mcp.tools import execute_tool_call
from tests.unit.test_step09_order_use_case import make_auth
from tests.unit.test_step09_tool_execution import (
    HEADERS_SELF,
    FakeGateway,
    RecordingSink,
    build_deps,
)
from tests.unit.test_step18_19_knowledge_mcp import ActiveRelease, FakeGraph, FakeRag


class FakeHybrid:
    def __init__(self):
        self.calls = []

    def search(self, query, graph_query, *, tenant_id, knowledge_release_id):
        self.calls.append((query, graph_query, tenant_id, knowledge_release_id))
        rag = FakeRag().search(
            query, tenant_id=tenant_id, knowledge_release_id=knowledge_release_id, top_k=None
        )
        from application.knowledge.graph.ports import GraphScope

        graph = FakeGraph().search(graph_query, GraphScope(tenant_id, knowledge_release_id))
        return HybridResult(
            knowledge_release_id, "partial", "none", rag, graph, "partial", "success"
        )


class FakePolicy:
    def __init__(self, status="found"):
        self.status = status
        self.calls = []

    def get(self, identifier, *, tenant_id, knowledge_release_id, as_of):
        self.calls.append((identifier, tenant_id, knowledge_release_id, as_of))
        detail = PolicyDetail(
            "POL-RETURN-01",
            "退货政策",
            ("七日内",),
            (),
            date(2026, 1, 1),
            None,
            "2",
            ("ev-1",),
        )
        if self.status == "found":
            return PolicyResult("found", detail, (), knowledge_release_id)
        if self.status == "ambiguous":
            return PolicyResult(
                "ambiguous",
                None,
                (PolicyCandidate("POL-RETURN-01", "退货政策", "ev-1"),),
                knowledge_release_id,
            )
        return PolicyResult("not_found", None, (), knowledge_release_id)


def deps(*, release="release-v2", policy_status="found"):
    sink = RecordingSink()
    base = build_deps(
        {},
        {},
        gateway=FakeGateway({"token-self": make_auth(scopes=("knowledge:read",))}),
        sink=sink,
        scopes={
            "search_hybrid_knowledge": ("knowledge:read",),
            "get_policy_detail": ("knowledge:read",),
        },
    )
    hybrid, policy = FakeHybrid(), FakePolicy(policy_status)
    return (
        replace(
            base,
            active_knowledge_release=ActiveRelease(release),
            hybrid_retriever=hybrid,
            policy_reader=policy,
        ),
        hybrid,
        policy,
        sink,
    )


def test_hybrid_uses_one_trusted_release_and_preserves_two_kinds_of_evidence() -> None:
    dependencies, hybrid, _, sink = deps()
    response = execute_tool_call(
        "search_hybrid_knowledge",
        {"query": "A政策", "entity_names": ["A"], "relation_types": ["HAS_POLICY"]},
        deps=dependencies,
        headers=HEADERS_SELF,
    )
    assert response["status"] == "partial"
    assert response["data"]["knowledge_release_id"] == "release-v2"
    assert {item["kind"] for item in response["data"]["merged_evidence"]} == {
        "document",
        "relation",
    }
    assert hybrid.calls[0][2:] == ("tenant-north", "release-v2")
    assert len(sink.intents) == len(sink.completions) == 1


def test_hybrid_rejects_draft_release_before_search() -> None:
    dependencies, hybrid, _, _ = deps()
    response = execute_tool_call(
        "search_hybrid_knowledge",
        {
            "query": "A政策",
            "entity_names": ["A"],
            "relation_types": ["HAS_POLICY"],
            "release_id": "draft",
        },
        deps=dependencies,
        headers=HEADERS_SELF,
    )
    assert response["error_code"] == "RESOURCE_UNAVAILABLE"
    assert hybrid.calls == []


def test_policy_name_id_and_date_use_release_gate() -> None:
    dependencies, _, policy, _ = deps()
    response = execute_tool_call(
        "get_policy_detail",
        {"policy_name": "退货政策", "as_of": "2026-06-01T12:00:00+00:00"},
        deps=dependencies,
        headers=HEADERS_SELF,
    )
    assert response["status"] == "success"
    assert response["data"]["policy"]["evidence_refs"] == ["ev-1"]
    assert policy.calls == [("退货政策", "tenant-north", "release-v2", date(2026, 6, 1))]

    invalid = execute_tool_call(
        "get_policy_detail",
        {"policy_name": "退货政策", "policy_id": "POL-RETURN-01"},
        deps=dependencies,
        headers=HEADERS_SELF,
    )
    assert invalid["error_code"] == "INVALID_ARGUMENT"
    assert len(policy.calls) == 1


def test_policy_ambiguity_is_partial_and_inactive_release_is_denied() -> None:
    dependencies, _, policy, _ = deps(policy_status="ambiguous")
    ambiguous = execute_tool_call(
        "get_policy_detail",
        {"policy_id": "POL-RETURN-01"},
        deps=dependencies,
        headers=HEADERS_SELF,
    )
    assert ambiguous["status"] == "partial"
    assert ambiguous["data"]["policy"] is None
    assert ambiguous["data"]["candidates"][0]["policy_id"] == "POL-RETURN-01"

    inactive, _, _, _ = deps(release=None)
    denied = execute_tool_call(
        "get_policy_detail",
        {"policy_name": "退货政策"},
        deps=inactive,
        headers=HEADERS_SELF,
    )
    assert denied["error_code"] == "RESOURCE_UNAVAILABLE"
