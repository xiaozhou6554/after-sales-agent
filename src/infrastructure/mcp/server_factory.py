"""在同一 MCP 服务注册五类工具组并绑定处理器（9.4、134 行注册口径）。

- 工具名、描述与 scope 以 data/contracts/mcp-tools.json 为唯一事实源：
  只注册注册表内且已实现的工具，不伪装其他工具就绪；
- 处理器函数签名（Context 由 SDK 注入）决定 tools/list 输入 Schema；
  执行语义（鉴权/审计/信封）经 bootstrap 注入的 execute 回调来自接口层，
  本模块不反向依赖接口层；
- 同步处理器在线程池执行，与同步 SQLAlchemy 适配器匹配。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context


def load_tool_registry(registry_path: Path) -> dict[str, dict[str, Any]]:
    """加载 mcp-tools.json 为 {工具名: 注册项}；文件缺失即失败，不用空表兜底。"""
    manifest = json.loads(registry_path.read_text(encoding="utf-8"))
    return {tool["name"]: tool for tool in manifest["tools"]}


def _headers_of(ctx: Context | None):
    """从 SDK Context 取 Starlette Request 头；非 HTTP 传输返回空映射。"""
    if ctx is None:
        return {}
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        return {}
    return request.headers


def build_order_tool_handlers(deps: Any, execute: Callable[..., dict]) -> dict[str, Callable]:
    """订单工具组处理器：签名决定输入 Schema；执行语义全部委托 execute。"""

    def query_order(order_id: str, ctx: Context = None) -> dict:
        return execute("query_order", {"order_id": order_id}, deps=deps, headers=_headers_of(ctx))

    def query_payment(order_id: str, ctx: Context = None) -> dict:
        return execute("query_payment", {"order_id": order_id}, deps=deps, headers=_headers_of(ctx))

    return {"query_order": query_order, "query_payment": query_payment}


def build_logistics_tool_handlers(deps: Any, execute: Callable[..., dict]) -> dict[str, Callable]:
    """物流只读工具由同一服务端授权、审计和信封处理链执行。"""

    def query_tracking(tracking_number: str, ctx: Context = None) -> dict:
        return execute(
            "query_tracking",
            {"tracking_number": tracking_number},
            deps=deps,
            headers=_headers_of(ctx),
        )

    def query_logistics(
        order_id: str | None = None,
        tracking_number: str | None = None,
        ctx: Context = None,
    ) -> dict:
        arguments = {}
        if order_id is not None:
            arguments["order_id"] = order_id
        if tracking_number is not None:
            arguments["tracking_number"] = tracking_number
        return execute("query_logistics", arguments, deps=deps, headers=_headers_of(ctx))

    def check_delivery_exception(order_id: str, ctx: Context = None) -> dict:
        return execute(
            "check_delivery_exception", {"order_id": order_id}, deps=deps, headers=_headers_of(ctx)
        )

    return {
        "query_tracking": query_tracking,
        "query_logistics": query_logistics,
        "check_delivery_exception": check_delivery_exception,
    }


def build_knowledge_tool_handlers(deps: Any, execute: Callable[..., dict]) -> dict[str, Callable]:
    """知识检索只转发注册表允许的参数；身份和发布版本由服务端校验。"""

    def search_knowledge_base(
        query: str,
        top_k: int | None = None,
        release_id: str | None = None,
        ctx: Context = None,
    ) -> dict:
        arguments: dict[str, Any] = {"query": query}
        if top_k is not None:
            arguments["top_k"] = top_k
        if release_id is not None:
            arguments["release_id"] = release_id
        return execute("search_knowledge_base", arguments, deps=deps, headers=_headers_of(ctx))

    def query_knowledge_graph(
        entity_names: list[str],
        relation_types: list[str],
        limit: int | None = None,
        ctx: Context = None,
    ) -> dict:
        arguments: dict[str, Any] = {
            "entity_names": entity_names,
            "relation_types": relation_types,
        }
        if limit is not None:
            arguments["limit"] = limit
        return execute("query_knowledge_graph", arguments, deps=deps, headers=_headers_of(ctx))

    def search_hybrid_knowledge(
        query: str,
        entity_names: list[str],
        relation_types: list[str],
        release_id: str | None = None,
        ctx: Context = None,
    ) -> dict:
        arguments: dict[str, Any] = {
            "query": query,
            "entity_names": entity_names,
            "relation_types": relation_types,
        }
        if release_id is not None:
            arguments["release_id"] = release_id
        return execute("search_hybrid_knowledge", arguments, deps=deps, headers=_headers_of(ctx))

    def get_policy_detail(
        policy_name: str | None = None,
        policy_id: str | None = None,
        as_of: str | None = None,
        ctx: Context = None,
    ) -> dict:
        arguments: dict[str, Any] = {}
        if policy_name is not None:
            arguments["policy_name"] = policy_name
        if policy_id is not None:
            arguments["policy_id"] = policy_id
        if as_of is not None:
            arguments["as_of"] = as_of
        return execute("get_policy_detail", arguments, deps=deps, headers=_headers_of(ctx))

    return {
        "search_knowledge_base": search_knowledge_base,
        "query_knowledge_graph": query_knowledge_graph,
        "search_hybrid_knowledge": search_hybrid_knowledge,
        "get_policy_detail": get_policy_detail,
    }


def build_refund_read_tool_handlers(deps: Any, execute: Callable[..., dict]) -> dict[str, Callable]:
    """退款只读工具；创建申请必须走独立确认上下文，不能在此注册。"""

    def check_refund_policy(order_id: str, reason_code: str, ctx: Context = None) -> dict:
        return execute(
            "check_refund_policy",
            {"order_id": order_id, "reason_code": reason_code},
            deps=deps,
            headers=_headers_of(ctx),
        )

    def calculate_refund(
        order_id: str, reason_code: str, lines: list[dict], ctx: Context = None
    ) -> dict:
        return execute(
            "calculate_refund",
            {"order_id": order_id, "reason_code": reason_code, "lines": lines},
            deps=deps,
            headers=_headers_of(ctx),
        )

    def query_refund_request(refund_request_id: str, ctx: Context = None) -> dict:
        return execute(
            "query_refund_request",
            {"refund_request_id": refund_request_id},
            deps=deps,
            headers=_headers_of(ctx),
        )

    return {
        "check_refund_policy": check_refund_policy,
        "calculate_refund": calculate_refund,
        "query_refund_request": query_refund_request,
    }


def build_refund_write_tool_handlers(
    deps: Any, execute: Callable[..., dict]
) -> dict[str, Callable]:
    """Model fields only; confirmation and idempotency resolve from trusted server state."""

    def create_refund_request(order_id: str, reason_code: str, ctx: Context = None) -> dict:
        return execute(
            "create_refund_request",
            {"order_id": order_id, "reason_code": reason_code},
            deps=deps,
            headers=_headers_of(ctx),
        )

    return {"create_refund_request": create_refund_request}


def build_ticket_read_tool_handlers(deps: Any, execute: Callable[..., dict]) -> dict[str, Callable]:
    """Only expose the authorized ticket read; writes and handoff have no handlers."""

    def query_ticket(ticket_id: str, ctx: Context = None) -> dict:
        return execute(
            "query_ticket", {"ticket_id": ticket_id}, deps=deps, headers=_headers_of(ctx)
        )

    return {"query_ticket": query_ticket}


def build_warehouse_read_tool_handlers(
    deps: Any, execute: Callable[..., dict]
) -> dict[str, Callable]:
    """Return progress is read-only for the model; WMS writes stay staff-only."""

    def query_return_progress(return_id: str, ctx: Context = None) -> dict:
        return execute(
            "query_return_progress", {"return_id": return_id},
            deps=deps, headers=_headers_of(ctx),
        )

    return {"query_return_progress": query_return_progress}


def build_payment_read_tool_handlers(
    deps: Any, execute: Callable[..., dict]
) -> dict[str, Callable]:
    """Funds status is read-only for the model; execution stays a service identity."""

    def query_refund_execution(refund_request_id: str, ctx: Context = None) -> dict:
        return execute(
            "query_refund_execution", {"refund_request_id": refund_request_id},
            deps=deps, headers=_headers_of(ctx),
        )

    return {"query_refund_execution": query_refund_execution}


def build_analytics_read_tool_handlers(
    deps: Any, execute: Callable[..., dict]
) -> dict[str, Callable]:
    """Versioned metrics are read-only; quality gates and watermarks stay server-side."""

    def query_after_sales_metrics(
        metric_id: str,
        cohort_start: str,
        cohort_end: str,
        dimensions: list[str] | None = None,
        ctx: Context = None,
    ) -> dict:
        arguments: dict[str, Any] = {
            "metric_id": metric_id,
            "cohort_start": cohort_start,
            "cohort_end": cohort_end,
        }
        if dimensions:
            arguments["dimensions"] = dimensions
        return execute(
            "query_after_sales_metrics", arguments,
            deps=deps, headers=_headers_of(ctx),
        )

    return {"query_after_sales_metrics": query_after_sales_metrics}


def build_investigation_tool_handlers(
    deps: Any, execute: Callable[..., dict]
) -> dict[str, Callable]:
    """Investigation explain is read-only; open/revise stay workbench-only."""

    def explain_anomaly(investigation_id: str, ctx: Context = None) -> dict:
        return execute(
            "explain_anomaly", {"investigation_id": investigation_id},
            deps=deps, headers=_headers_of(ctx),
        )

    return {"explain_anomaly": explain_anomaly}


def build_address_read_tool_handlers(
    deps: Any, execute: Callable[..., dict]
) -> dict[str, Callable]:
    """Address eligibility/status reads; raw address never enters model context."""

    def check_shipping_address_change(order_id: str, ctx: Context = None) -> dict:
        return execute(
            "check_shipping_address_change", {"order_id": order_id},
            deps=deps, headers=_headers_of(ctx),
        )

    def query_shipping_address_change(address_change_id: str, ctx: Context = None) -> dict:
        return execute(
            "query_shipping_address_change", {"address_change_id": address_change_id},
            deps=deps, headers=_headers_of(ctx),
        )

    return {
        "check_shipping_address_change": check_shipping_address_change,
        "query_shipping_address_change": query_shipping_address_change,
    }


def build_address_write_tool_handlers(
    deps: Any, execute: Callable[..., dict]
) -> dict[str, Callable]:
    """Server injects confirmation/revision/idempotency; model passes intent only."""

    def update_shipping_address(order_id: str, address_change_id: str, ctx: Context = None) -> dict:
        return execute(
            "update_shipping_address",
            {"order_id": order_id, "address_change_id": address_change_id},
            deps=deps,
            headers=_headers_of(ctx),
        )

    return {"update_shipping_address": update_shipping_address}


def build_ticket_write_tool_handlers(
    deps: Any, execute: Callable[..., dict]
) -> dict[str, Callable]:
    """Model fields only; confirmation and idempotency resolve from trusted server state."""

    def create_ticket(title: str, description: str, priority: str, ctx: Context = None) -> dict:
        return execute(
            "create_ticket",
            {"title": title, "description": description, "priority": priority},
            deps=deps,
            headers=_headers_of(ctx),
        )

    def update_ticket(ticket_id: str, content: str, ctx: Context = None) -> dict:
        return execute(
            "update_ticket",
            {"ticket_id": ticket_id, "content": content},
            deps=deps,
            headers=_headers_of(ctx),
        )

    def handoff_to_human(reason: str, summary: str, ctx: Context = None) -> dict:
        return execute(
            "handoff_to_human",
            {"reason": reason, "summary": summary},
            deps=deps,
            headers=_headers_of(ctx),
        )

    return {
        "create_ticket": create_ticket,
        "update_ticket": update_ticket,
        "handoff_to_human": handoff_to_human,
    }


def create_mcp_server(
    tool_handlers: dict[str, Callable],
    *,
    registry_path: Path,
    host: str = "127.0.0.1",
    port: int = 8001,
) -> FastMCP:
    """按注册表注册已实现工具；注册表之外的工具名一律拒绝注册。"""
    registry = load_tool_registry(registry_path)
    unknown = set(tool_handlers) - set(registry)
    if unknown:
        raise ValueError(f"工具未在 mcp-tools.json 注册：{sorted(unknown)}")
    server: FastMCP = FastMCP("after-sales-mcp", host=host, port=port)
    for name, handler in tool_handlers.items():
        meta = registry[name]
        server.add_tool(handler, name=name, description=meta.get("description") or name)
    return server
