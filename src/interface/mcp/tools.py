"""MCP 工具处理器：受控传输解析、逐次二次授权、审计与信封构造（9.4、16.1）。

- 模型只控制 arguments；租户/主体/权限经受控传输（HTTP 头）注入并在服务端
  逐次重验（ToolContextGuard），AuthContext 不进入模型参数或返回值；
- 链路三元组取自可信 API 进程请求头（服务到服务），缺失时由服务端生成；
- 每次已认证调用写审计意图与完成结果；授权拒绝另记单条终态安全审计（736 行）；
- 处理器绝不外抛异常：所有失败都映射为统一错误信封，避免 SDK isError 通道
  泄露内部信息。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from application.common.audit import STATUS_FAILED, STATUS_SUCCESS, AuditUseCase
from application.common.authorization import GENERIC_DENIED_MESSAGE, AuthorizationDenied
from application.common.context import AuthContext, TraceContext
from application.common.errors import ErrorCode
from application.common.masking import mask_mapping, mask_text
from application.common.ports.audit import AuditPersistenceError
from application.common.ports.auth import AuthGateway
from application.common.ports.confirmation import ConfirmationConflictError
from application.common.ports.idempotency import IdempotencyConflictError
from application.common.tracing import SubjectFrame, bind_request_context, reset_request_context
from application.knowledge.graph.ports import GraphQuery, GraphScope, GraphSearchPort
from application.knowledge.hybrid import HybridRetriever
from application.knowledge.policy import PolicyReader
from application.knowledge.rag.retriever import RagRetriever
from application.knowledge.release_port import ActiveKnowledgeReleasePort
from application.logistics.ports.logistics_query import LogisticsDependencyError
from application.logistics.use_cases.logistics_query import QueryLogisticsUseCase
from application.order.dto.order import PAYMENT_OUTCOME_UNKNOWN, PaymentQueryResult
from application.order.ports.order_query import OrderDependencyError
from application.order.use_cases.order_query import QueryOrderUseCase, QueryPaymentUseCase
from application.refund.qualification import RefundLineSelection, RefundQualificationUseCase
from application.refund.query import RefundRequestQueryUseCase
from application.refund.submission import RefundSubmissionUseCase
from application.refund.tool_intent import RefundToolIntentPort
from application.ticket.handoff import MockHumanHandoff
from application.ticket.service import (
    TicketApplicationService,
    TicketContextSnapshot,
    TicketPriority,
    TicketVersionConflict,
    TicketVisibility,
)
from application.ticket.tool_intent import TicketToolIntentConflict, TicketToolIntentPort
from interface.http.middleware.auth import new_server_trace
from interface.mcp.context_guard import ToolContextGuard
from interface.mcp.knowledge_projection import (
    graph_tool_data,
    hybrid_tool_data,
    policy_tool_data,
    rag_tool_data,
)
from interface.mcp.schemas.envelope import build_tool_response

mcp_security_logger = logging.getLogger("after_sales.security")

UNAUTHENTICATED_MESSAGE = "身份无效或会话已过期"
DEPENDENCY_MESSAGE = "依赖服务暂不可用，请稍后重试"
INTERNAL_MESSAGE = "服务内部错误，请稍后重试"

TRACE_HEADER_NAMES = ("x-request-id", "x-correlation-id", "x-trace-id", "x-thread-id")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class MCPToolDeps:
    """单个 MCP 进程内全部工具共享的依赖；由 bootstrap 装配注入。"""

    auth_gateway: AuthGateway
    guard: ToolContextGuard
    query_order: QueryOrderUseCase
    query_payment: QueryPaymentUseCase
    audit: AuditUseCase
    query_logistics: QueryLogisticsUseCase | None = None
    active_knowledge_release: ActiveKnowledgeReleasePort | None = None
    rag_retriever: RagRetriever | None = None
    graph_search: GraphSearchPort | None = None
    hybrid_retriever: HybridRetriever | None = None
    policy_reader: PolicyReader | None = None
    refund_qualification: RefundQualificationUseCase | None = None
    refund_request_query: RefundRequestQueryUseCase | None = None
    refund_submission: RefundSubmissionUseCase | None = None
    refund_tool_intent: RefundToolIntentPort | None = None
    ticket_query: TicketApplicationService | None = None
    ticket_tool_intent: TicketToolIntentPort | None = None
    # 仓储退货进度只读查询（FR-21）；写入通道不暴露给模型。
    warehouse_query: Any | None = None
    # 退款执行资金状态只读查询（FR-22）；执行通道不暴露给模型。
    refund_execution_query: Any | None = None
    # 版本化售后指标只读查询（FR-23）；指标口径未批准时返回 blocked。
    analytics_metrics: Any | None = None
    # 当前激活本体发布（版本组合随结果返回）；无激活时返回空版本。
    ontology_active_release: Any | None = None
    # 异常调查只读解释（FR-24）；开立/修订只在工作台，不暴露给模型。
    investigation: Any | None = None
    # 收货地址修改（FR-18）：资格/状态只读与受控提交；明文地址不经模型。
    address_change: Any | None = None
    address_apply: Any | None = None
    address_tool_intent: Any | None = None
    human_handoff: MockHumanHandoff | None = None
    # 受信任部署开关（服务端配置），不是模型参数；Mock 人工队列的可用性。
    human_handoff_available: bool = True
    # 会话有效期以 aware UTC 存储；时钟必须同为 aware UTC，避免本地时区漂移。
    clock: Callable[[], datetime] = _utc_now


def _trace_from_headers(headers) -> TraceContext | None:
    values = [headers.get(name) for name in TRACE_HEADER_NAMES]
    required = values[:3]
    if any(not value for value in required):
        return None
    return TraceContext(
        schema_version="1.0",
        request_id=str(required[0]),
        correlation_id=str(required[1]),
        trace_id=str(required[2]),
        thread_id=(str(values[3]) if values[3] else None),
    )


def _bearer_from_headers(headers) -> str | None:
    """与 HTTP Bearer 语义一致；MCP 侧直接作用于请求头映射。"""
    header = headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def _resolve_auth(deps: MCPToolDeps, headers) -> AuthContext | None:
    token = _bearer_from_headers(headers)
    if token is None:
        return None
    try:
        return deps.auth_gateway.resolve(token)
    except AuthorizationDenied:
        return None


def _error_envelope(
    trace: TraceContext,
    invocation_id: str,
    tool_name: str,
    parameter_summary: dict,
    error_code: str,
    message: str,
    retryable: bool,
) -> dict:
    return build_tool_response(
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
        invocation_id=invocation_id,
        tool_name=tool_name,
        status="error",
        parameter_summary=parameter_summary,
        error_code=error_code,
        message=message,
        retryable=retryable,
    )


def _order_data(snapshot) -> dict:
    """领域快照投影为具名 DTO；接口层只读属性，不导入领域类型。"""
    return {
        "order": {
            "order_id": snapshot.order_id,
            "owner_customer_id": snapshot.owner_customer_id,
            "status": snapshot.status,
            "payment_status": snapshot.payment_status,
            "fulfillment_status": snapshot.fulfillment_status,
            "currency": snapshot.currency,
            "amount_minor": snapshot.amount_minor,
        },
        "snapshot_version": snapshot.snapshot_version,
        "observed_at": snapshot.observed_at.isoformat(),
    }


def _payment_data(result: PaymentQueryResult) -> tuple[str, dict]:
    order = result.order
    if result.outcome == PAYMENT_OUTCOME_UNKNOWN or result.snapshot is None:
        # 支付未知不是错误：status=empty，data 仍满足具名 DTO。
        return "empty", {
            "order_id": order.order_id,
            "payment_status": "unknown",
            "paid_minor": 0,
            "refunded_minor": 0,
            "currency": order.currency,
            "payment_ids": [],
            "snapshot_version": order.snapshot_version,
            "observed_at": order.observed_at.isoformat(),
        }
    snapshot = result.snapshot
    return "success", {
        "order_id": snapshot.order_id,
        "payment_status": snapshot.payment_status,
        "paid_minor": snapshot.paid_minor,
        "refunded_minor": snapshot.refunded_minor,
        "currency": snapshot.currency,
        "payment_ids": list(snapshot.payment_ids),
        "snapshot_version": snapshot.snapshot_version,
        "observed_at": snapshot.observed_at.isoformat(),
    }


def _refund_policy_data(assessment) -> dict:
    return {
        "eligibility": assessment.eligibility,
        "can_submit": assessment.can_submit,
        "reason_code": assessment.reason_code,
        "policy_id": assessment.policy_id or "",
        "policy_version": assessment.policy_version or "",
        "order_version": assessment.order_version,
        "evidence_refs": (
            [f"mock-policy:{assessment.policy_id}:{assessment.policy_version}"]
            if assessment.policy_id and assessment.policy_version
            else []
        ),
        "missing_information": list(assessment.missing_fields),
    }


def _refund_quote_data(quote) -> dict:
    return {
        "quote_id": quote.quote_id,
        "amount_minor": quote.amount_minor,
        "currency": quote.currency,
        "amount_breakdown": {
            "goods_amount_minor": quote.goods_minor,
            "freight_amount_minor": quote.shipping_minor,
        },
        "lines": [
            {
                "order_line_id": line.line_id,
                "quantity": line.quantity,
                "goods_amount_minor": line.goods_minor,
                "freight_amount_minor": line.shipping_minor,
                "amount_minor": line.goods_minor + line.shipping_minor,
            }
            for line in quote.lines
        ],
        "policy_version": quote.policy_version,
        "order_version": quote.order_version,
        "payment_version": quote.payment_version,
        "expires_at": quote.expires_at.isoformat(),
        "requires_manual_review": quote.eligibility == "manual_review",
    }


def _refund_request_data(snapshot) -> dict:
    return {
        "query_status": "found",
        "refund_request": {
            "refund_request_id": snapshot.refund_request_id,
            "order_id": snapshot.order_id,
            "review_status": snapshot.review_status,
            "return_status": snapshot.return_status,
            "funds_status": snapshot.funds_status,
            "amount_minor": snapshot.amount_minor,
            "currency": snapshot.currency,
            "revision": snapshot.revision,
            "updated_at": snapshot.updated_at.isoformat(),
        },
    }


def _shipment_data(snapshot) -> dict:
    return {
        "tracking_number": snapshot.tracking_number,
        "status": snapshot.status,
        "events": [
            {
                "time": event.time.isoformat(),
                "status": event.status,
                "description": event.description,
            }
            for event in snapshot.events
        ],
        "exception_code": snapshot.exception_code,
        "estimated_delivery_at": (
            snapshot.estimated_delivery_at.isoformat()
            if snapshot.estimated_delivery_at is not None
            else None
        ),
        "source_version": snapshot.source_version,
        "observed_at": snapshot.observed_at.isoformat(),
    }


def _ticket_data(record) -> dict:
    """Whitelisted public summary; never project message or context payloads."""
    ticket = record.ticket
    return {
        "found": True,
        "ticket": {
            "ticket_id": ticket.ticket_id,
            "status": ticket.status.value,
            "priority": ticket.priority.value,
            "revision": ticket.revision,
            "assigned_to": ticket.assigned_to,
            "updated_at": record.updated_at.isoformat(),
        },
    }


def _customer_subject_id(auth: AuthContext) -> str:
    """MCP writes bind to the single customer of a customer-own session only."""
    scope = auth.data_scope.customer_ids
    if "customer" not in auth.roles or len(scope) != 1:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
    return scope[0]


def _ticket_write_dispatch(
    tool_name: str,
    arguments: dict[str, Any],
    deps: MCPToolDeps,
    auth: AuthContext,
    trace: TraceContext,
) -> tuple[str, dict]:
    if tool_name == "handoff_to_human":
        if deps.human_handoff is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        reason_code = str(arguments["reason"])
        summary_masked = mask_text(str(arguments["summary"]))
        customer_id = _customer_subject_id(auth)
        context = TicketContextSnapshot(
            question_masked=reason_code,
            summary=summary_masked,
            agent_results=(),
            tool_result_summaries=(),
            failure_reason=None,
            business_refs=(),
            current_state="open",
            next_action="wait for human agent",
            source_message_ids=(),
        )
        idempotency_key = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{auth.tenant_id}:{auth.subject_id}:handoff:mcp:{reason_code}:{summary_masked}",
        ).hex
        try:
            result = deps.human_handoff.request(
                auth,
                trace,
                customer_id=customer_id,
                reason_code=reason_code,
                context=context,
                idempotency_key=idempotency_key,
                available=deps.human_handoff_available,
            )
        except ValueError as exc:
            raise AuthorizationDenied(
                ErrorCode.STATE_CONFLICT, "转接请求状态冲突，请查询原结果"
            ) from exc
        data = {
            "handoff_id": result.handoff_id,
            "handoff_status": result.status,
            "ticket_id": result.ticket_id,
        }
        if result.status == "unavailable":
            data["fallback_hint"] = "人工暂不可用；如需工单处理，请完成内容确认后创建工单"
            return "partial", data
        return "success", data
    if deps.ticket_tool_intent is None or deps.ticket_query is None:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
    customer_id = _customer_subject_id(auth)
    try:
        if tool_name == "create_ticket":
            priority = TicketPriority(str(arguments["priority"]))
            intent = deps.ticket_tool_intent.resolve_create(
                auth,
                customer_id=customer_id,
                title=str(arguments["title"]),
                description_masked=mask_text(str(arguments["description"])),
                priority=priority,
            )
            result = deps.ticket_query.create(auth, **intent.commit_kwargs())
        else:
            intent = deps.ticket_tool_intent.resolve_update(
                auth,
                ticket_id=str(arguments["ticket_id"]),
                content_masked=mask_text(str(arguments["content"])),
                visibility=TicketVisibility.CUSTOMER,
            )
            result = deps.ticket_query.update(auth, **intent.commit_kwargs())
    except TicketToolIntentConflict as exc:
        raise AuthorizationDenied(
            ErrorCode.CONFIRMATION_INVALID,
            "缺少唯一有效的客户确认，请先完成受控确认流程",
        ) from exc
    except TicketVersionConflict as exc:
        raise AuthorizationDenied(
            ErrorCode.VERSION_CONFLICT, "工单已被其他人更新，请查询最新内容后重试"
        ) from exc
    except ValueError as exc:
        raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "工单状态已变化，请查询原结果") from exc
    return (
        "success",
        {
            "ticket_id": result.ticket_id,
            "revision": result.revision,
            "replayed": result.replayed,
        },
    )


def execute_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    deps: MCPToolDeps,
    headers,
    invocation_id: str | None = None,
) -> dict:
    """一次工具调用的完整服务端语义：鉴权→二次授权→审计→执行→信封。

    本函数是工具边界兜底：任何未预期异常都不外抛（SDK isError 通道会泄露
    内部信息），统一转泛化错误信封。
    """
    try:
        return _execute_tool_call_impl(
            tool_name, arguments, deps=deps, headers=headers, invocation_id=invocation_id
        )
    except Exception:  # noqa: BLE001 - 信封通道兜底
        fallback_trace = new_server_trace()
        mcp_security_logger.error(
            "tool execution failed unexpectedly: tool=%s",
            tool_name,
            extra={
                "details": {"tool_name": tool_name},
                "request_id": fallback_trace.request_id,
                "correlation_id": fallback_trace.correlation_id,
                "trace_id": fallback_trace.trace_id,
                "thread_id": None,
                "tenant_id": None,
                "subject_id": None,
            },
        )
        return _error_envelope(
            fallback_trace,
            invocation_id or uuid.uuid4().hex,
            tool_name,
            {},
            ErrorCode.DEPENDENCY_UNAVAILABLE.value,
            INTERNAL_MESSAGE,
            retryable=False,
        )


def _execute_tool_call_impl(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    deps: MCPToolDeps,
    headers,
    invocation_id: str | None,
) -> dict:
    invocation = invocation_id or uuid.uuid4().hex
    trace = _trace_from_headers(headers) or new_server_trace()
    parameter_summary = mask_mapping(dict(arguments))

    auth = _resolve_auth(deps, headers)
    if auth is None:
        mcp_security_logger.warning(
            "authorization denied: %s",
            ErrorCode.UNAUTHENTICATED.value,
            extra={
                "details": {"tool_name": tool_name},
                "request_id": trace.request_id,
                "correlation_id": trace.correlation_id,
                "trace_id": trace.trace_id,
                "thread_id": trace.thread_id,
                "tenant_id": None,
                "subject_id": None,
            },
        )
        return _error_envelope(
            trace,
            invocation,
            tool_name,
            parameter_summary,
            ErrorCode.UNAUTHENTICATED.value,
            UNAUTHENTICATED_MESSAGE,
            retryable=False,
        )

    context_tokens = bind_request_context(trace, SubjectFrame(auth.tenant_id, auth.subject_id))
    try:
        return _execute_authorized(
            tool_name,
            arguments,
            deps=deps,
            auth=auth,
            trace=trace,
            invocation=invocation,
            parameter_summary=parameter_summary,
        )
    finally:
        reset_request_context(context_tokens)


def _execute_authorized(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    deps: MCPToolDeps,
    auth: AuthContext,
    trace: TraceContext,
    invocation: str,
    parameter_summary: dict,
) -> dict:
    audit_ids = {
        "tenant_id": auth.tenant_id,
        "actor_subject_id": auth.subject_id,
        "action": f"tool.{tool_name}",
        "request_id": trace.request_id,
        "correlation_id": trace.correlation_id,
        "trace_id": trace.trace_id,
        "thread_id": trace.thread_id,
    }
    try:
        deps.guard.verify(tool_name, auth, tenant_id=auth.tenant_id, now=deps.clock())
        # 模型可见参数按注册表 Schema（唯一源）校验；不合法即拒绝。
        deps.guard.validate_arguments(tool_name, arguments)
    except AuthorizationDenied as denied:
        deps.audit.record_action(
            **audit_ids,
            parameter_summary=parameter_summary,
            result_summary={"reason": "authorization"},
            status="denied",
            error_code=denied.error_code.value,
        )
        mcp_security_logger.warning(
            "authorization denied: %s",
            denied.error_code.value,
            extra={
                "details": {"tool_name": tool_name},
                "request_id": trace.request_id,
                "correlation_id": trace.correlation_id,
                "trace_id": trace.trace_id,
                "thread_id": trace.thread_id,
                "tenant_id": auth.tenant_id,
                "subject_id": auth.subject_id,
            },
        )
        return _error_envelope(
            trace,
            invocation,
            tool_name,
            parameter_summary,
            denied.error_code.value,
            denied.message,
            retryable=False,
        )

    try:
        audit_id = deps.audit.begin_tool_call(**audit_ids, parameter_summary=parameter_summary)
    except AuditPersistenceError:
        # 意图无法持久化：调用不执行，按依赖失败返回（20.3）。
        return _error_envelope(
            trace,
            invocation,
            tool_name,
            parameter_summary,
            ErrorCode.DEPENDENCY_UNAVAILABLE.value,
            DEPENDENCY_MESSAGE,
            retryable=True,
        )

    started = time.perf_counter()
    try:
        status, data = _dispatch(tool_name, arguments, deps, auth, trace)
    except AuthorizationDenied as denied:
        _complete(deps, audit_id, auth.tenant_id, STATUS_FAILED, denied.error_code.value, started)
        mcp_security_logger.warning(
            "authorization denied: %s",
            denied.error_code.value,
            extra={
                "details": {"tool_name": tool_name},
                "request_id": trace.request_id,
                "correlation_id": trace.correlation_id,
                "trace_id": trace.trace_id,
                "thread_id": trace.thread_id,
                "tenant_id": auth.tenant_id,
                "subject_id": auth.subject_id,
            },
        )
        return _error_envelope(
            trace,
            invocation,
            tool_name,
            parameter_summary,
            denied.error_code.value,
            denied.message,
            retryable=False,
        )
    except (OrderDependencyError, LogisticsDependencyError):
        _complete(
            deps,
            audit_id,
            auth.tenant_id,
            STATUS_FAILED,
            ErrorCode.DEPENDENCY_UNAVAILABLE.value,
            started,
        )
        return _error_envelope(
            trace,
            invocation,
            tool_name,
            parameter_summary,
            ErrorCode.DEPENDENCY_UNAVAILABLE.value,
            DEPENDENCY_MESSAGE,
            retryable=True,
        )
    except Exception:  # noqa: BLE001 - 工具边界兜底：不向模型泄露内部异常
        _complete(
            deps,
            audit_id,
            auth.tenant_id,
            STATUS_FAILED,
            ErrorCode.DEPENDENCY_UNAVAILABLE.value,
            started,
        )
        return _error_envelope(
            trace,
            invocation,
            tool_name,
            parameter_summary,
            ErrorCode.DEPENDENCY_UNAVAILABLE.value,
            INTERNAL_MESSAGE,
            retryable=False,
        )
    _complete(deps, audit_id, auth.tenant_id, STATUS_SUCCESS, None, started)
    return build_tool_response(
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
        invocation_id=invocation,
        tool_name=tool_name,
        status=status,
        parameter_summary=parameter_summary,
        data=data,
    )


def _complete(
    deps: MCPToolDeps,
    audit_id: str,
    tenant_id: str,
    status: str,
    error_code: str | None,
    started: float,
) -> None:
    duration_ms = int((time.perf_counter() - started) * 1000)
    try:
        deps.audit.complete_tool_call(
            audit_id=audit_id,
            tenant_id=tenant_id,
            result_summary={"status": status},
            duration_ms=duration_ms,
            status=status,
            error_code=error_code,
        )
    except AuditPersistenceError:
        # 只读结果补写失败不改变业务结果；高风险写入的阻断语义在对应步骤接入。
        mcp_security_logger.error(
            "audit completion failed: audit_id=%s status=%s", audit_id, status
        )


def _dispatch(
    tool_name: str,
    arguments: dict[str, Any],
    deps: MCPToolDeps,
    auth: AuthContext,
    trace: TraceContext,
) -> tuple[str, dict]:
    if tool_name in ("check_refund_policy", "calculate_refund"):
        if deps.refund_qualification is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        order_id = str(arguments["order_id"])
        reason_code = str(arguments["reason_code"])
        if tool_name == "check_refund_policy":
            assessment = deps.refund_qualification.check_policy(auth, order_id, reason_code)
            return (
                "partial" if assessment.eligibility == "unknown" else "success",
                _refund_policy_data(assessment),
            )
        selections = tuple(
            RefundLineSelection(str(line["order_line_id"]), line["quantity"])
            for line in arguments["lines"]
        )
        assessment, quote = deps.refund_qualification.calculate_quote(
            auth, order_id, reason_code, selections
        )
        if quote is None:
            raise AuthorizationDenied(
                ErrorCode.STATE_CONFLICT,
                f"退款报价暂不可用：{assessment.reason_code}",
            )
        return "success", _refund_quote_data(quote)
    if tool_name == "query_refund_request":
        if deps.refund_request_query is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        snapshot = deps.refund_request_query.by_id(auth, str(arguments["refund_request_id"]))
        return "success", _refund_request_data(snapshot)
    if tool_name == "create_refund_request":
        if deps.refund_submission is None or deps.refund_tool_intent is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        try:
            intent = deps.refund_tool_intent.resolve(
                auth, str(arguments["order_id"]), str(arguments["reason_code"])
            )
            result = deps.refund_submission.submit(
                auth,
                intent.quote_id,
                intent.confirmation_id,
                intent.idempotency_key,
                trace,
            )
        except ConfirmationConflictError as exc:
            raise AuthorizationDenied(
                ErrorCode.CONFIRMATION_INVALID, "缺少唯一有效的客户确认，请重新确认"
            ) from exc
        except IdempotencyConflictError as exc:
            raise AuthorizationDenied(
                ErrorCode.IDEMPOTENCY_CONFLICT, "原退款操作内容冲突，请查询原结果"
            ) from exc
        except ValueError as exc:
            raise AuthorizationDenied(
                ErrorCode.STATE_CONFLICT, "退款申请状态已变化，请查询原结果"
            ) from exc
        if result.status != "pending_review":
            raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, "申请已有审核进展，请查询原结果")
        return "success", {
            "refund_request_id": result.refund_request_id,
            "order_id": str(arguments["order_id"]),
            "review_status": "pending_review",
            "amount_minor": result.amount_minor,
            "currency": result.currency,
            "funds_status": "not_executed",
            "revision": 1,
        }
    if tool_name == "check_shipping_address_change":
        if deps.address_change is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        return "success", deps.address_change.check(auth, str(arguments["order_id"]))
    if tool_name == "query_shipping_address_change":
        if deps.address_change is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        data = deps.address_change.query(auth, str(arguments["address_change_id"]))
        data["observed_at"] = data["observed_at"].isoformat()
        return "success", data
    if tool_name == "update_shipping_address":
        if deps.address_apply is None or deps.address_tool_intent is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        order_id = str(arguments["order_id"])
        try:
            intent = deps.address_tool_intent.resolve(auth, order_id)
            if intent.address_change_id != str(arguments["address_change_id"]):
                raise AuthorizationDenied(
                    ErrorCode.CONFIRMATION_INVALID, "缺少唯一有效的客户确认，请重新确认"
                )
            result = deps.address_apply.apply(
                auth,
                order_id,
                intent.address_change_id,
                intent.confirmation_id,
                intent.idempotency_key,
                trace,
            )
        except ConfirmationConflictError as exc:
            raise AuthorizationDenied(
                ErrorCode.CONFIRMATION_INVALID, "缺少唯一有效的客户确认，请重新确认"
            ) from exc
        except IdempotencyConflictError as exc:
            raise AuthorizationDenied(
                ErrorCode.IDEMPOTENCY_CONFLICT, "原地址修改内容冲突，请查询原结果"
            ) from exc
        except ValueError as exc:
            raise AuthorizationDenied(
                ErrorCode.STATE_CONFLICT, "地址修改状态已变化，请查询原结果"
            ) from exc
        return "success", {
            "operation_id": result.operation_id,
            "address_change_id": result.address_change_id,
            "status": result.status,
            "result_order_version": result.result_order_version,
            "error_code": result.error_code,
            "observed_at": result.observed_at.isoformat(),
        }
    if tool_name in (
        "search_knowledge_base",
        "query_knowledge_graph",
        "search_hybrid_knowledge",
        "get_policy_detail",
    ):
        resolver = deps.active_knowledge_release
        if resolver is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        release_id = resolver.get_active_release_id(auth.tenant_id)
        if release_id is None or (
            tool_name in ("search_knowledge_base", "search_hybrid_knowledge")
            and arguments.get("release_id") not in (None, release_id)
        ):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        if tool_name == "search_knowledge_base":
            if deps.rag_retriever is None:
                raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
            try:
                result = deps.rag_retriever.search(
                    str(arguments["query"]),
                    tenant_id=auth.tenant_id,
                    knowledge_release_id=release_id,
                    top_k=arguments.get("top_k"),
                )
            except ValueError as exc:
                raise AuthorizationDenied(
                    ErrorCode.INVALID_ARGUMENT, GENERIC_DENIED_MESSAGE
                ) from exc
            data = rag_tool_data(result)
            status = (
                "success" if data["retrieval_status"] == "complete" else data["retrieval_status"]
            )
            return status, data
        if tool_name == "search_hybrid_knowledge":
            if deps.hybrid_retriever is None:
                raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
            try:
                result = deps.hybrid_retriever.search(
                    str(arguments["query"]),
                    GraphQuery(
                        tuple(arguments["entity_names"]),
                        tuple(arguments["relation_types"]),
                    ),
                    tenant_id=auth.tenant_id,
                    knowledge_release_id=release_id,
                )
            except ValueError as exc:
                raise AuthorizationDenied(
                    ErrorCode.INVALID_ARGUMENT, GENERIC_DENIED_MESSAGE
                ) from exc
            data = hybrid_tool_data(result)
            return (
                "success" if result.retrieval_status == "complete" else result.retrieval_status
            ), data
        if tool_name == "get_policy_detail":
            if deps.policy_reader is None:
                raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
            identifier = arguments.get("policy_name") or arguments.get("policy_id")
            if not isinstance(identifier, str) or bool(arguments.get("policy_name")) == bool(
                arguments.get("policy_id")
            ):
                raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, GENERIC_DENIED_MESSAGE)
            raw_as_of = arguments.get("as_of")
            if raw_as_of is None:
                as_of = deps.clock().date()
            else:
                try:
                    parsed = datetime.fromisoformat(str(raw_as_of).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        raise ValueError("naive datetime")
                    as_of = parsed.date()
                except ValueError as exc:
                    raise AuthorizationDenied(
                        ErrorCode.INVALID_ARGUMENT, GENERIC_DENIED_MESSAGE
                    ) from exc
            try:
                result = deps.policy_reader.get(
                    identifier,
                    tenant_id=auth.tenant_id,
                    knowledge_release_id=release_id,
                    as_of=as_of,
                )
            except ValueError as exc:
                raise AuthorizationDenied(
                    ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE
                ) from exc
            status = (
                "success"
                if result.match_status == "found"
                else "partial"
                if result.match_status == "ambiguous"
                else "empty"
            )
            return status, policy_tool_data(result)
        if deps.graph_search is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        result = deps.graph_search.search(
            GraphQuery(
                tuple(arguments["entity_names"]),
                tuple(arguments["relation_types"]),
                limit=arguments.get("limit", 10),
            ),
            GraphScope(auth.tenant_id, release_id),
        )
        return ("success" if result.relations else "empty"), graph_tool_data(result)
    if tool_name == "query_order":
        snapshot = deps.query_order.execute(auth, str(arguments["order_id"]))
        return "success", _order_data(snapshot)
    if tool_name == "query_payment":
        status, data = _payment_data(deps.query_payment.execute(auth, str(arguments["order_id"])))
        return status, data
    if tool_name == "query_ticket":
        if deps.ticket_query is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        return "success", _ticket_data(deps.ticket_query.query(auth, str(arguments["ticket_id"])))
    if tool_name == "query_return_progress":
        if deps.warehouse_query is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        return (
            "success",
            deps.warehouse_query.query_progress(auth, str(arguments["return_id"])).as_dict(),
        )
    if tool_name == "query_refund_execution":
        if deps.refund_execution_query is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        return (
            "success",
            deps.refund_execution_query.query_by_request(
                auth, str(arguments["refund_request_id"])
            ),
        )
    if tool_name == "query_after_sales_metrics":
        if deps.analytics_metrics is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        active = (
            deps.ontology_active_release.get_active(auth.tenant_id)
            if deps.ontology_active_release is not None
            else None
        )
        return (
            "success",
            deps.analytics_metrics.query_metrics(
                auth,
                metric_id=str(arguments["metric_id"]),
                cohort_start=str(arguments["cohort_start"]),
                cohort_end=str(arguments["cohort_end"]),
                active_release=active,
            ),
        )
    if tool_name == "explain_anomaly":
        if deps.investigation is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        return (
            "success",
            deps.investigation.explain(
                auth, str(arguments["investigation_id"]), revision=None
            ),
        )
    if tool_name in ("create_ticket", "update_ticket", "handoff_to_human"):
        return _ticket_write_dispatch(tool_name, arguments, deps, auth, trace)
    if tool_name in ("query_tracking", "query_logistics", "check_delivery_exception"):
        if deps.query_logistics is None:
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
        if tool_name == "query_tracking":
            shipment = deps.query_logistics.by_tracking(auth, str(arguments["tracking_number"]))
            return "success", _shipment_data(shipment)
        if tool_name == "query_logistics":
            order_id, shipments = deps.query_logistics.query(
                auth, arguments.get("order_id"), arguments.get("tracking_number")
            )
            return "success" if shipments else "empty", {
                "order_id": order_id,
                "shipments": [_shipment_data(shipment) for shipment in shipments],
                "observed_at": deps.clock().isoformat(),
            }
        order_id = str(arguments["order_id"])
        exceptions = deps.query_logistics.check_exception(auth, order_id)
        shipments = deps.query_logistics.by_order(auth, order_id)
        return "success", {
            "order_id": order_id,
            "has_exception": bool(exceptions),
            "exceptions": list(exceptions),
            "source_version": (
                shipments[0].source_version if shipments else deps.query_logistics.source_version
            ),
            "observed_at": deps.clock().isoformat(),
        }
    raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
