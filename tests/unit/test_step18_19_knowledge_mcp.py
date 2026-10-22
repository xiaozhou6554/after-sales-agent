"""知识 MCP：可信发布版本、授权、参数化查询和证据信封。"""

from dataclasses import replace

from application.knowledge.graph.ports import GraphEvidence, GraphNode, GraphRelation, GraphResult
from application.knowledge.rag.retriever import RagEvidence, RagResult
from application.knowledge.retrieval.branch import BranchResult
from application.knowledge.retrieval.ports import KnowledgeChunk
from interface.mcp.tools import execute_tool_call
from tests.unit.test_step09_order_use_case import make_auth
from tests.unit.test_step09_tool_execution import (
    HEADERS_SELF,
    FakeGateway,
    RecordingSink,
    build_deps,
)


class ActiveRelease:
    def __init__(self, release_id="release-v2"):
        self.release_id = release_id
        self.tenants = []

    def get_active_release_id(self, tenant_id):
        self.tenants.append(tenant_id)
        return self.release_id


class FakeRag:
    def __init__(self):
        self.calls = []

    def search(self, query, *, tenant_id, knowledge_release_id, top_k):
        self.calls.append((query, tenant_id, knowledge_release_id, top_k))
        chunk = KnowledgeChunk(
            tenant_id,
            knowledge_release_id,
            "chunk-1",
            "doc-1",
            "ev-1",
            2,
            "退货政策",
            "签收后七日内",
            "退货",
            "published",
        )
        return RagResult(
            query,
            tenant_id,
            knowledge_release_id,
            "partial",
            (RagEvidence(chunk, 0.8, None, 0.9, None, 0.9, ("vector",), 3.0, 0.8),),
            (
                BranchResult("vector", "success", (), 1.0),
                BranchResult("bm25", "error", (), 2.0, "DEPENDENCY_UNAVAILABLE"),
            ),
            "candidate-v1",
            "vector-v1",
            "bm25-v1",
            ranking_method="cross_encoder",
            rerank_status="success",
            rerank_model_id="test-reranker",
            rerank_model_revision="fixed-revision",
            rerank_calibration_version="rerank-v1",
        )


class FakeGraph:
    def __init__(self):
        self.calls = []

    def search(self, query, scope):
        self.calls.append((query, scope))
        return GraphResult(
            scope.knowledge_release_id,
            (GraphNode("p1", "Product", "A", "1"), GraphNode("a1", "Accessory", "F-01", "1")),
            (GraphRelation("p1", "COMPATIBLE_WITH", "a1", "doc-1:v2", "ev-1"),),
            (GraphEvidence("ev-1", "doc-1", "corpus", "2"),),
        )


def _deps(release_id="release-v2"):
    from tests.unit.test_step09_tool_execution import FakeGuard

    sink = RecordingSink()
    rag, graph, active = FakeRag(), FakeGraph(), ActiveRelease(release_id)
    deps = build_deps(
        {},
        {},
        gateway=FakeGateway({"token-self": make_auth(scopes=("knowledge:read",))}),
        sink=sink,
        scopes={
            "search_knowledge_base": ("knowledge:read",),
            "query_knowledge_graph": ("knowledge:read",),
        },
    )
    assert isinstance(deps.guard, FakeGuard)
    return (
        replace(deps, active_knowledge_release=active, rag_retriever=rag, graph_search=graph),
        rag,
        graph,
        active,
        sink,
    )


def test_rag_partial_preserves_one_route_evidence_and_audit() -> None:
    deps, rag, _, active, sink = _deps()
    response = execute_tool_call(
        "search_knowledge_base",
        {"query": "退货期限", "release_id": "release-v2"},
        deps=deps,
        headers=HEADERS_SELF,
    )
    assert response["status"] == "partial"
    assert response["data"]["bm25_error"] == "DEPENDENCY_UNAVAILABLE"
    assert response["data"]["evidence_refs"] == ["ev-1"]
    assert rag.calls == [("退货期限", "tenant-north", "release-v2", None)]
    assert active.tenants == ["tenant-north"]
    assert len(sink.intents) == len(sink.completions) == 1


def test_inactive_or_wrong_release_never_reaches_retrieval() -> None:
    deps, rag, graph, _, _ = _deps(None)
    denied = execute_tool_call(
        "search_knowledge_base", {"query": "退货期限"}, deps=deps, headers=HEADERS_SELF
    )
    assert denied["error_code"] == "RESOURCE_UNAVAILABLE"
    assert not rag.calls and not graph.calls

    deps, rag, _, _, _ = _deps()
    denied = execute_tool_call(
        "search_knowledge_base",
        {"query": "退货期限", "release_id": "draft-v3"},
        deps=deps,
        headers=HEADERS_SELF,
    )
    assert denied["error_code"] == "RESOURCE_UNAVAILABLE"
    assert not rag.calls


def test_graph_receives_trusted_tenant_release_and_rejects_raw_cypher() -> None:
    deps, _, graph, _, _ = _deps()
    response = execute_tool_call(
        "query_knowledge_graph",
        {"entity_names": ["A"], "relation_types": ["COMPATIBLE_WITH"]},
        deps=deps,
        headers=HEADERS_SELF,
    )
    assert response["status"] == "success"
    assert response["data"]["evidence_refs"] == ["ev-1"]
    assert graph.calls[0][1].tenant_id == "tenant-north"
    assert graph.calls[0][1].knowledge_release_id == "release-v2"

    rejected = execute_tool_call(
        "query_knowledge_graph",
        {"entity_names": ["A"], "relation_types": ["COMPATIBLE_WITH"], "cypher": "MATCH (n)"},
        deps=deps,
        headers=HEADERS_SELF,
    )
    assert rejected["error_code"] == "INVALID_ARGUMENT"
    assert len(graph.calls) == 1
