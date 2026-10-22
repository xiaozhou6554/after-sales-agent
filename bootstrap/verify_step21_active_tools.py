"""本地激活后通过独立 MCP 进程调用四个知识工具；不修改发布状态。"""

from __future__ import annotations

import json

from bootstrap.settings import load_settings
from infrastructure.mcp.local_probe import MCPServerProcess, free_port, probe_trace


def main() -> None:
    settings = load_settings()
    if settings.app_env == "production" or not settings.knowledge_local_tools_enabled:
        raise RuntimeError("仅允许本地开发环境验证合成知识工具")
    process = MCPServerProcess(free_port(), knowledge_enabled=True)
    try:
        process.wait_ready()
        token = process.login("cust-north-01")
        cases = (
            ("search_knowledge_base", {"query": "退货要在签收后几天申请？"}),
            (
                "query_knowledge_graph",
                {
                    "entity_names": ["A"],
                    "relation_types": ["COMPATIBLE_WITH"],
                },
            ),
            (
                "search_hybrid_knowledge",
                {
                    "query": "净化器A能使用F-01滤芯吗？",
                    "entity_names": ["A"],
                    "relation_types": ["COMPATIBLE_WITH"],
                },
            ),
            ("get_policy_detail", {"policy_name": "退货政策"}),
        )
        results = []
        for name, arguments in cases:
            response = process.call(name, arguments, token=token, trace=probe_trace(name))
            if response["status"] not in ("success", "partial"):
                raise AssertionError(f"{name} 未返回可用知识证据: {response['error_code']}")
            data = response.get("data") or {}
            if name == "get_policy_detail" and data.get("match_status") != "found":
                raise AssertionError("唯一政策别名没有定位有效条款")
            results.append(
                {
                    "tool": name,
                    "status": response["status"],
                    "knowledge_release_id": data.get("knowledge_release_id"),
                    "evidence_count": len(data.get("evidence_refs") or []),
                }
            )
        print(json.dumps({"synthetic_only": True, "results": results}, ensure_ascii=False))
    finally:
        process.stop()


if __name__ == "__main__":
    main()
