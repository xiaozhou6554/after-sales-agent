"""知识工具签名和参数转发不得绕开 MCP 注册表。"""

from pathlib import Path

from mcp.server.fastmcp.tools.base import Tool

from infrastructure.mcp.server_factory import build_knowledge_tool_handlers, load_tool_registry

REGISTRY = load_tool_registry(Path(__file__).resolve().parents[2] / "data/contracts/mcp-tools.json")


def test_knowledge_signatures_match_registry_structure() -> None:
    handlers = build_knowledge_tool_handlers(object(), lambda *args, **kwargs: {})
    assert set(handlers) == {
        "search_knowledge_base",
        "query_knowledge_graph",
        "search_hybrid_knowledge",
        "get_policy_detail",
    }
    for name, handler in handlers.items():
        generated = Tool.from_function(handler, name=name).parameters
        expected = REGISTRY[name]["input_schema"]
        assert set(generated["properties"]) == set(expected["properties"]), name
        if name != "get_policy_detail":
            assert set(generated["required"]) == set(expected["required"]), name
        else:
            assert generated.get("required", []) == []
            assert len(expected["oneOf"]) == 2
        assert expected["additionalProperties"] is False


def test_knowledge_handlers_forward_only_supplied_arguments() -> None:
    seen = []

    def execute(name, arguments, **kwargs):
        seen.append((name, arguments, kwargs))
        return {"ok": True}

    deps = object()
    handlers = build_knowledge_tool_handlers(deps, execute)
    handlers["search_knowledge_base"]("退货期限")
    handlers["search_knowledge_base"]("F23", top_k=3, release_id="release-v2")
    handlers["query_knowledge_graph"](["X1"], ["COMPATIBLE_WITH"])
    handlers["query_knowledge_graph"](["X1"], ["HAS_POLICY"], limit=2)
    handlers["search_hybrid_knowledge"]("A关系及政策", ["A"], ["HAS_POLICY"])
    handlers["get_policy_detail"](policy_name="退货政策")
    assert [item[1] for item in seen] == [
        {"query": "退货期限"},
        {"query": "F23", "top_k": 3, "release_id": "release-v2"},
        {"entity_names": ["X1"], "relation_types": ["COMPATIBLE_WITH"]},
        {"entity_names": ["X1"], "relation_types": ["HAS_POLICY"], "limit": 2},
        {"query": "A关系及政策", "entity_names": ["A"], "relation_types": ["HAS_POLICY"]},
        {"policy_name": "退货政策"},
    ]
    assert all(item[2] == {"deps": deps, "headers": {}} for item in seen)
