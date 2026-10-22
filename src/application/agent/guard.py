"""确定性结果守卫：工具信封 → 事实/追问/如实失败，绝不编造（提示词 6.2 规则 10/11）。

守卫是纯函数：只解释工具响应信封，不做二次调用、不做重试决策；
重试预算由 MCP 客户端唯一持有，守卫看到的已是末次结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 守卫结论枚举
GUARD_FACTS = "facts"  # 信封 success：取得结构化事实
GUARD_EMPTY = "empty"  # 信封 empty：查询成功但无记录（如支付未知）
GUARD_DENIED = "denied"  # 越权/不存在：统一安全语义，不得透露存在性
GUARD_FAILED = "failed"  # 依赖失败/内部失败：预算已用尽，如实告知
GUARD_INVALID = "invalid"  # 参数不合法/工具不可用：不重试
GUARD_UNAUTHENTICATED = "unauthenticated"  # 会话失效：停止查询，转人工
GUARD_CONFLICT = "conflict"  # 知识证据冲突：禁止确定性结论，触发Reflection

KNOWLEDGE_TOOLS = frozenset(
    {
        "search_knowledge_base",
        "query_knowledge_graph",
        "search_hybrid_knowledge",
        "get_policy_detail",
    }
)


@dataclass(frozen=True)
class GuardVerdict:
    outcome: str
    # success 时为工具 data DTO（具名结构）；其余为 None。
    data: dict | None = None
    error_code: str | None = None
    message: str | None = None
    # 供 Observation 摘要与最终回答引用的业务事实描述（不含内部标识）。
    summary: str = ""
    facts: dict = field(default_factory=dict)

    @property
    def is_failure(self) -> bool:
        return self.outcome in (
            GUARD_DENIED,
            GUARD_FAILED,
            GUARD_INVALID,
            GUARD_UNAUTHENTICATED,
            GUARD_CONFLICT,
        )

    @property
    def needs_reflection(self) -> bool:
        """异常才进入 Reflection（流程步骤10：仅异常时调用）。"""
        return self.outcome in (GUARD_FAILED, GUARD_INVALID, GUARD_CONFLICT)


def evaluate_envelope(tool_name: str, envelope: dict) -> GuardVerdict:
    """解释工具响应信封；未知结构按失败处理（不编造）。"""
    status = envelope.get("status")
    if status == "success":
        data = envelope.get("data")
        if not isinstance(data, dict) or not data:
            return GuardVerdict(
                outcome=GUARD_FAILED,
                error_code="DEPENDENCY_UNAVAILABLE",
                message="工具返回缺少数据",
                summary=f"{tool_name} 返回数据缺失",
            )
        if tool_name in KNOWLEDGE_TOOLS and (
            data.get("conflict_status") not in (None, "none")
            or data.get("generation_ready") is False
        ):
            return GuardVerdict(
                outcome=GUARD_CONFLICT,
                error_code="KNOWLEDGE_CONFLICT",
                message="知识证据无法核实，请转人工确认",
                summary="知识证据冲突或未通过相关性门槛",
            )
        return GuardVerdict(
            outcome=GUARD_FACTS,
            data=data,
            summary=_facts_summary(tool_name, data),
            facts=data,
        )
    if status == "empty":
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        payment_status = data.get("payment_status")
        summary = "该查询没有可用记录"
        if payment_status == "unknown":
            summary = "该订单暂无支付记录"
        return GuardVerdict(outcome=GUARD_EMPTY, data=data, summary=summary, facts=data)
    if status in ("partial", "degraded") and tool_name in KNOWLEDGE_TOOLS:
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        if data.get("conflict_status") not in (None, "none"):
            return GuardVerdict(
                outcome=GUARD_CONFLICT,
                data=data,
                error_code="KNOWLEDGE_CONFLICT",
                message="知识证据冲突，请转人工核实",
                summary="知识证据冲突，未形成确定性结论",
            )
        if tool_name == "get_policy_detail" and data.get("match_status") == "ambiguous":
            return GuardVerdict(
                outcome=GUARD_CONFLICT,
                data=data,
                error_code="POLICY_AMBIGUOUS",
                message="找到多个同名政策，请明确政策名称或ID",
                summary="政策名称存在歧义，未选择任何一条",
            )
        return GuardVerdict(
            outcome=GUARD_FAILED,
            data=data,
            error_code="KNOWLEDGE_PARTIAL",
            message="知识检索仅部分完成，不能确认完整结论",
            summary="知识检索仅部分完成；未验证的关系不能作为答案",
        )
    if status == "error":
        code = envelope.get("error_code") or "DEPENDENCY_UNAVAILABLE"
        error = envelope.get("error") or {}
        message = error.get("message") or "查询未成功"
        if code == "UNAUTHENTICATED":
            return GuardVerdict(
                outcome=GUARD_UNAUTHENTICATED,
                error_code=code,
                message=message,
                summary="身份校验未通过",
            )
        if code == "RESOURCE_UNAVAILABLE":
            # 越权与不存在同一安全语义：不得透露资源是否存在。
            return GuardVerdict(
                outcome=GUARD_DENIED,
                error_code=code,
                message=message,
                summary="未查到该资源",
            )
        if code == "INVALID_ARGUMENT":
            return GuardVerdict(
                outcome=GUARD_INVALID,
                error_code=code,
                message=message,
                summary="查询参数未通过校验",
            )
        return GuardVerdict(
            outcome=GUARD_FAILED,
            error_code=code,
            message=message,
            summary="系统暂时查不到该信息",
        )
    # partial/degraded/未知状态：一期不编造，按失败收束并转人工。
    return GuardVerdict(
        outcome=GUARD_FAILED,
        error_code=str(envelope.get("error_code") or "DEPENDENCY_UNAVAILABLE"),
        message="工具返回状态无法确认",
        summary="系统暂时无法确认该信息",
    )


def _facts_summary(tool_name: str, data: dict) -> str:
    """把具名 DTO 摘要为最终回答可用的事实句（金额服务端格式化，两位小数）。"""
    if tool_name == "query_order" and isinstance(data.get("order"), dict):
        order = data["order"]
        amount = format_money(order.get("amount_minor", 0), order.get("currency", "CNY"))
        return (
            f"订单 {order.get('order_id')} 状态 {order.get('status')}，"
            f"支付状态 {order.get('payment_status')}，金额 {amount}。"
        )
    if tool_name == "query_payment":
        amount = format_money(data.get("paid_minor", 0), data.get("currency", "CNY"))
        refunded = format_money(data.get("refunded_minor", 0), data.get("currency", "CNY"))
        ids = "、".join(data.get("payment_ids") or []) or "无"
        return (
            f"订单 {data.get('order_id')} 支付状态 {data.get('payment_status')}，"
            f"已支付 {amount}，已退款 {refunded}，支付单 {ids}。"
        )
    if tool_name == "query_logistics":
        shipments = data.get("shipments") or []
        summaries = [_shipment_summary(item) for item in shipments if isinstance(item, dict)]
        if not summaries:
            return "暂无物流记录。"
        return f"订单 {data.get('order_id')} 的物流：" + "；".join(summaries)
    if tool_name == "query_tracking":
        return _shipment_summary(data)
    if tool_name == "check_delivery_exception":
        exceptions = data.get("exceptions") or []
        if not data.get("has_exception"):
            return f"订单 {data.get('order_id')} 暂无已确认的物流异常。"
        codes = "、".join(
            str(item.get("exception_code")) for item in exceptions if isinstance(item, dict)
        )
        return f"订单 {data.get('order_id')} 存在物流异常：{codes}。"
    if tool_name == "search_knowledge_base":
        return _document_evidence_summary(data.get("results") or [])
    if tool_name == "query_knowledge_graph":
        return _relation_evidence_summary(data.get("relations") or [], data.get("nodes") or [])
    if tool_name == "search_hybrid_knowledge":
        return " ".join(
            filter(
                None,
                (
                    _document_evidence_summary(data.get("rag_evidence") or []),
                    _relation_evidence_summary(data.get("kg_evidence") or [], []),
                ),
            )
        )
    if tool_name == "get_policy_detail" and isinstance(data.get("policy"), dict):
        policy = data["policy"]
        clauses = "；".join(str(item) for item in (policy.get("clauses") or [])[:3])
        refs = "、".join(policy.get("evidence_refs") or [])
        return (
            f"政策 {policy.get('policy_name')}（版本 {policy.get('version')}，"
            f"证据 {refs}）：{clauses}"
        )
    return "工具返回了查询结果。"


def _document_evidence_summary(items: list[dict]) -> str:
    facts = [
        f"{str(item.get('snippet') or '')[:400]}（文档 {item.get('document_id')}，"
        f"版本 {item.get('version')}，证据 {item.get('evidence_id')}）"
        for item in items[:3]
        if isinstance(item, dict) and item.get("evidence_id")
    ]
    return "检索到的文档证据：" + "；".join(facts) if facts else ""


def _relation_evidence_summary(items: list[dict], nodes: list[dict]) -> str:
    labels = {item.get("node_id"): item.get("label") for item in nodes if isinstance(item, dict)}
    facts = [
        f"{labels.get(item.get('from'), item.get('from'))} "
        f"{item.get('relation_type')} "
        f"{labels.get(item.get('to'), item.get('to'))}"
        f"（版本 {item.get('version')}，证据 {item.get('evidence_id')}）"
        for item in items[:5]
        if isinstance(item, dict) and item.get("evidence_id")
    ]
    return "已核实的图谱关系：" + "；".join(facts) if facts else ""


def _shipment_summary(shipment: dict) -> str:
    """只转述物流源明确提供的状态和时间；无 ETA 时不推断。"""
    status = shipment.get("status") or "未知"
    tracking = shipment.get("tracking_number") or "未知单号"
    summary = f"运单 {tracking} 状态 {status}"
    events = shipment.get("events") or []
    if events and isinstance(events[-1], dict):
        event = events[-1]
        summary += f"，最新轨迹 {event.get('status')}（{event.get('time')}）"
    if shipment.get("exception_code"):
        summary += f"，异常 {shipment['exception_code']}"
    if shipment.get("estimated_delivery_at"):
        summary += f"，物流源预计送达 {shipment['estimated_delivery_at']}"
    return summary + "。"


def format_money(amount_minor: int, currency: str) -> str:
    """服务端金额格式化：整数分 → 两位小数＋币种（最终回答提示词 6.4 要求）。"""
    value = int(amount_minor or 0) / 100
    return f"{value:.2f} {currency}"


# 面向用户的字段措辞（仅用于追问文案，不参与任何路由或授权判断）。
FIELD_LABELS = {"order_id": "订单号"}


def argument_defects(schema: dict, arguments: dict) -> list[str]:
    """按注册表 Schema 预检模型参数（16.1 参数校验的应用侧前置）。

    返回缺陷字段清单：必填缺失、空串、Schema 外多余键。空清单表示可以
    发起工具调用；服务端守卫仍会逐次二次校验，此处只避免明知不合法的
    调用消耗传输与重试预算。
    """
    if not isinstance(schema, dict) or not isinstance(arguments, dict):
        return []
    properties = schema.get("properties") or {}
    defects: list[str] = []
    for field_name in schema.get("required") or []:
        value = arguments.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            defects.append(str(field_name))
    alternatives = schema.get("oneOf") or []
    if alternatives:
        present = [
            option
            for option in alternatives
            if all(
                arguments.get(name) is not None
                and not (isinstance(arguments.get(name), str) and not arguments[name].strip())
                for name in option.get("required", ())
            )
        ]
        if len(present) != 1:
            defects.append(str(alternatives[0].get("required", ["missing_info"])[0]))
    for key in arguments:
        if key not in properties:
            defects.append(str(key))
    return defects


def ask_user_prompt_for(defect_fields: list[str]) -> str:
    """把缺陷字段转为一句用户可理解的追问（一次只问一个，提示词规则 10）。"""
    if not defect_fields:
        return "请补充查询所需的信息。"
    label = "、".join(FIELD_LABELS.get(field, field) for field in defect_fields)
    return f"请提供{label}。"
