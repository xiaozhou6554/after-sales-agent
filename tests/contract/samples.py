"""版本化固定契约样例（《项目开发流程》步骤06：正常、空、错误、部分失败、异步受理）。

样例即契约：跨进程契约测试与后续步骤的验收都以这些字典为基准；
修改任何字段都必须伴随规格 26 章的同步修订说明。
"""

TRACE_FIELDS = {
    "request_id": "req-2026-10-20-0001",
    "correlation_id": "corr-refund-0001",
    "trace_id": "trace-refund-0001",
    "thread_id": None,
}

# ---------- APIResponse 五态 ----------

API_RESPONSE_SUCCESS = {
    **TRACE_FIELDS,
    "status": "success",
    "answer": "您的订单 ORDER-001 已签收，退货申请已受理。",
    "next_action": "finish",
    "handoff_required": False,
    "requested_inputs": [],
    "sources": [
        {
            "evidence_id": "evid-001",
            "evidence_type": "rag",
            "source_id": "doc-9",
            "source": "退货政策",
            "version": "2026-09",
            "knowledge_release_id": "kr-2026-09",
            "content": "签收后15日内可申请退货。",
            "confidence": 0.92,
            "chunk_id": "chunk-0091",
            "document_id": "doc-9",
            "vector_score": 0.83,
            "bm25_score": 0.71,
            "final_score": 0.92,
            "retrieval_source": "vector",
        }
    ],
    "async_task_id": None,
    "data": {},
    "error": None,
}

API_RESPONSE_EMPTY_SOURCES = {
    **API_RESPONSE_SUCCESS,
    "answer": "",
    "sources": [],
}

API_RESPONSE_NEEDS_INPUT = {
    **TRACE_FIELDS,
    "status": "needs_input",
    "answer": "请提供需要查询的订单号。",
    "next_action": "ask_user",
    "requested_inputs": [
        {
            "field": "order_id",
            "prompt": "请提供订单号",
            "input_type": "text",
            "required": True,
            "form_ref": None,
        }
    ],
}

API_RESPONSE_PARTIAL = {
    **TRACE_FIELDS,
    "status": "partial",
    "answer": "订单信息已获取，物流查询暂时不可用。",
    "next_action": "finish",
    "sources": [],
}

API_RESPONSE_ASYNC_ACCEPTED = {
    **TRACE_FIELDS,
    "status": "pending",
    "answer": "退款申请已受理，正在处理。",
    "next_action": "wait",
    "async_task_id": "task-0001",
}

API_RESPONSE_ERROR = {
    **TRACE_FIELDS,
    "status": "error",
    "answer": "当前无法处理该请求。",
    "next_action": "finish",
    "error": {
        "error_code": "DEPENDENCY_UNAVAILABLE",
        "message": "依赖服务暂时不可用，请稍后重试。",
        "retryable": True,
        "request_id": TRACE_FIELDS["request_id"],
        "correlation_id": TRACE_FIELDS["correlation_id"],
        "trace_id": TRACE_FIELDS["trace_id"],
        "details": {"field_errors": []},
    },
}

# ---------- MCP 信封 ----------

MCP_TRACE = {
    "request_id": TRACE_FIELDS["request_id"],
    "correlation_id": TRACE_FIELDS["correlation_id"],
    "trace_id": TRACE_FIELDS["trace_id"],
    "thread_id": None,
}

MCP_REQUEST = {
    "trace": MCP_TRACE,
    "agent_name": "after-sales-agent",
    "invocation_id": "inv-0001",
    "tool_name": "query_order",
    "status": "request",
    "error_code": None,
    "parameter_summary": {"order_id": "ORDER-001"},
    "arguments": {"order_id": "ORDER-001", "customer_id": "CUSTOMER-01"},
}

MCP_RESPONSE_SUCCESS = {
    "trace": MCP_TRACE,
    "agent_name": "after-sales-agent",
    "invocation_id": "inv-0001",
    "tool_name": "query_order",
    "status": "success",
    "error_code": None,
    "parameter_summary": {"order_id": "ORDER-001"},
    "data": {
        "order": {
            "order_id": "ORDER-001",
            "owner_customer_id": "CUSTOMER-01",
            "status": "delivered",
            "payment_status": "paid",
            "fulfillment_status": "signed",
            "currency": "CNY",
            "amount_minor": 100000,
            "snapshot_version": "oms-v3",
            "observed_at": "2026-10-19T04:00:00Z",
        },
        "snapshot_version": "oms-v3",
        "observed_at": "2026-10-19T04:00:00Z",
    },
    "error": None,
}

MCP_RESPONSE_ERROR = {
    "trace": MCP_TRACE,
    "agent_name": "after-sales-agent",
    "invocation_id": "inv-0001",
    "tool_name": "query_order",
    "status": "error",
    "error_code": "RESOURCE_UNAVAILABLE",
    "parameter_summary": {"order_id": "ORDER-404"},
    "data": {},
    "error": {
        "error_code": "RESOURCE_UNAVAILABLE",
        "message": "资源不可用。",
        "retryable": False,
        "request_id": TRACE_FIELDS["request_id"],
        "correlation_id": TRACE_FIELDS["correlation_id"],
        "trace_id": TRACE_FIELDS["trace_id"],
        "details": {"field_errors": []},
    },
}

TOOL_REGISTRATION = {
    "schema_version": "1.0",
    "name": "query_order",
    "owner": "OMS",
    "access": "read",
    "side_effect": "none",
    "auth_scopes": ["order:read"],
    "requires_confirmation": False,
    "idempotency_required": False,
    "input_schema": {"type": "object", "required": ["order_id"]},
}

# ---------- 事件（26.8 信封 + ticket.created 载荷） ----------

EVENT_ENVELOPE_TICKET_CREATED = {
    "schema_version": "1.0",
    "event_id": "evt-0001",
    "event_type": "ticket.created",
    "event_version": 1,
    "tenant_id": "tenant-north",
    "subject_id": "customer-01",
    "request_id": TRACE_FIELDS["request_id"],
    "correlation_id": TRACE_FIELDS["correlation_id"],
    "trace_id": TRACE_FIELDS["trace_id"],
    "operation_id": "op-0001",
    "causation_id": None,
    "parent_event_id": None,
    "producer": "ticket-module",
    "occurred_at": "2026-10-20T08:00:00Z",
    "retry_count": 0,
    "idempotency_key": "idem-ticket-0001-north",
    "payload": {
        "ticket_id": "TCK-0001",
        "revision": 0,
        "status": "open",
        "priority": "normal",
        "context_ref": "ctx-0001",
        "actor_subject_id": "customer-01",
        "customer_id": "CUSTOMER-01",
    },
}
