"""模型可见只读工具必须同时满足运行注册与主体权限。"""

from types import SimpleNamespace

from bootstrap.container import registered_read_tool_names

from application.agent.tool_catalog import ToolCatalog
from tests.unit.test_step10_agent_application import REGISTRY_PATH, make_auth


def _settings(*, local: bool, graph: bool):
    return SimpleNamespace(
        app_env="development",
        knowledge_local_tools_enabled=local,
        vector_backend="chroma",
        neo4j_read_only=graph,
        neo4j_query_password="configured" if graph else "",
    )


def test_runtime_registration_excludes_unimplemented_contract_only_tools() -> None:
    names = registered_read_tool_names(_settings(local=False, graph=False))
    assert names == frozenset(
        {
            "query_order",
            "query_payment",
            "query_tracking",
            "query_logistics",
            "check_delivery_exception",
            "check_refund_policy",
            "calculate_refund",
            "create_refund_request",
            "query_refund_request",
            "query_ticket",
            "create_ticket",
            "update_ticket",
            "handoff_to_human",
            "query_return_progress",
            "query_refund_execution",
        }
    )
    catalog = ToolCatalog.load(
        REGISTRY_PATH,
        make_auth(scopes=("order:read", "payment:read", "logistics:read", "refund:read")),
        registered_names=names,
    )
    assert set(catalog.entries) <= names
    assert "check_refund_policy" in catalog.entries
    assert "calculate_refund" in catalog.entries
    # 契约存在但未实现的工具仍不得进入模型清单。
    assert {
        "check_shipping_address_change",
        "query_after_sales_metrics",
        "explain_anomaly",
        "update_shipping_address",
    }.isdisjoint(names)


def test_local_knowledge_visibility_tracks_actual_graph_and_rag_registration() -> None:
    rag_only = registered_read_tool_names(_settings(local=True, graph=False))
    assert "search_knowledge_base" in rag_only
    assert "query_knowledge_graph" not in rag_only
    assert "search_hybrid_knowledge" not in rag_only
    all_names = registered_read_tool_names(_settings(local=True, graph=True))
    assert {
        "search_knowledge_base",
        "query_knowledge_graph",
        "search_hybrid_knowledge",
        "get_policy_detail",
    } <= all_names
