"""API 与 Agent 工作流结构化 DTO（《技术详细规格》26.6，纯 Python）。

传输层（interface）负责 Pydantic 校验与受控转换；本模块是转换的落点，
不依赖任何第三方库。业务不变量在构造时校验（fail-closed）：
- status=pending 必须携带已持久化 async_task_id 且 next_action=wait；
- status=needs_input 必须给出 requested_inputs 且 next_action=ask_user；
- status=error 必须携带 error 对象；success/pending 的 error 必须为 null。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from application.common.context import BusinessRef, ErrorDto, RequestedInput, TraceContext

SCHEMA_VERSION = "1.0"

STATUS_SUCCESS = "success"
STATUS_NEEDS_INPUT = "needs_input"
STATUS_PENDING = "pending"
STATUS_PARTIAL = "partial"
STATUS_ERROR = "error"
API_STATUSES = (STATUS_SUCCESS, STATUS_NEEDS_INPUT, STATUS_PENDING, STATUS_PARTIAL, STATUS_ERROR)

NEXT_FINISH = "finish"
NEXT_ASK_USER = "ask_user"
NEXT_HANDOFF = "handoff"
NEXT_WAIT = "wait"
NEXT_CONTINUE = "continue"
NEXT_RETRY = "retry"
NEXT_REPLAN = "replan"
NEXT_DISPATCH = "dispatch"
NEXT_PLAN = "plan"


class ContractViolation(ValueError):
    """DTO 构造违反 26.6 不变量。"""


@dataclass(frozen=True)
class ChatRequestDto:
    trace: TraceContext
    message: str
    business_refs: tuple[BusinessRef, ...] = ()
    thread_id: str | None = None
    # 客服代客目标客户：只代表目标，不代表授权；授权在用例内按 AuthContext 校验。
    customer_id: str | None = None


@dataclass(frozen=True)
class ApiResponseDto:
    trace: TraceContext
    status: str
    answer: str
    next_action: str
    handoff_required: bool = False
    requested_inputs: tuple[RequestedInput, ...] = ()
    sources: tuple["Evidence", ...] = ()
    async_task_id: str | None = None
    data: dict = field(default_factory=dict)
    error: ErrorDto | None = None

    def __post_init__(self) -> None:
        if self.status not in API_STATUSES:
            raise ContractViolation(f"未知 API status: {self.status!r}")
        if self.status == STATUS_PENDING:
            if not self.async_task_id:
                raise ContractViolation("pending 必须携带已持久化的 async_task_id")
            if self.next_action != NEXT_WAIT:
                raise ContractViolation("pending 的 next_action 必须为 wait")
        if self.status == STATUS_NEEDS_INPUT:
            if not self.requested_inputs:
                raise ContractViolation("needs_input 必须给出 requested_inputs")
            if self.next_action != NEXT_ASK_USER:
                raise ContractViolation("needs_input 的 next_action 必须为 ask_user")
        if self.status == STATUS_ERROR and self.error is None:
            raise ContractViolation("error 状态必须携带 error 对象")
        if self.status in (STATUS_SUCCESS, STATUS_PENDING) and self.error is not None:
            raise ContractViolation(f"{self.status} 状态的 error 必须为 null")


# ---------- 证据（26.5 严格联合） ----------


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    evidence_type: str  # rag / kg / policy
    source_id: str
    source: str
    version: str
    knowledge_release_id: str
    content: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.evidence_type not in ("rag", "kg", "policy"):
            raise ContractViolation(f"未知 evidence_type: {self.evidence_type!r}")


@dataclass(frozen=True)
class RagEvidence(Evidence):
    chunk_id: str = ""
    document_id: str = ""
    vector_score: float | None = None
    bm25_score: float | None = None
    final_score: float | None = None
    retrieval_source: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.evidence_type != "rag":
            raise ContractViolation("RagEvidence 的 evidence_type 必须为 rag")
        if not self.chunk_id or not self.document_id or not self.retrieval_source:
            raise ContractViolation("rag 证据必须携带 chunk_id/document_id/retrieval_source")


@dataclass(frozen=True)
class KgEvidence(Evidence):
    entities: tuple[str, ...] = ()
    relationships: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.evidence_type != "kg":
            raise ContractViolation("KgEvidence 的 evidence_type 必须为 kg")


@dataclass(frozen=True)
class PolicyEvidence(Evidence):
    effective_from: str | None = None
    effective_to: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.evidence_type != "policy":
            raise ContractViolation("PolicyEvidence 的 evidence_type 必须为 policy")


# ---------- 只读工具结果摘要（26.6，金额为整数分） ----------


@dataclass(frozen=True)
class OrderSummaryDto:
    order_id: str
    owner_customer_id: str
    status: str
    payment_status: str
    fulfillment_status: str
    currency: str
    amount_minor: int
    snapshot_version: str
    observed_at: datetime


@dataclass(frozen=True)
class PaymentSummaryDto:
    payment_id: str
    order_id: str
    paid_minor: int
    refunded_minor: int
    currency: str
    snapshot_version: str
    observed_at: datetime


@dataclass(frozen=True)
class TrackingPointDto:
    time: datetime
    status: str
    description: str


@dataclass(frozen=True)
class LogisticsSummaryDto:
    tracking_number: str
    status: str
    tracking: tuple[TrackingPointDto, ...] = ()
    exception_code: str | None = None
    estimated_delivery_at: datetime | None = None
    snapshot_version: str = ""
    observed_at: datetime | None = None


# ---------- Agent 工作流内部契约（26.6，供步骤11+ 使用） ----------


@dataclass(frozen=True)
class WorkflowFact:
    fact_type: str
    value: dict
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowResultDto:
    trace: TraceContext
    capability: str
    invocation_id: str
    status: str  # success / needs_input / partial / failed
    facts: tuple[WorkflowFact, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    next_action: str = NEXT_CONTINUE
    requested_inputs: tuple[RequestedInput, ...] = ()
    handoff_required: bool = False
    error: ErrorDto | None = None

    def __post_init__(self) -> None:
        if self.status not in ("success", "needs_input", "partial", "failed"):
            raise ContractViolation(f"未知 WorkflowResult status: {self.status!r}")
        if self.next_action not in (NEXT_CONTINUE, NEXT_ASK_USER, NEXT_HANDOFF, NEXT_FINISH):
            raise ContractViolation(f"未知 WorkflowResult next_action: {self.next_action!r}")
        if self.status == "needs_input" and not self.requested_inputs:
            raise ContractViolation("needs_input 必须给出 requested_inputs")
        if self.status == "failed" and self.error is None:
            raise ContractViolation("failed 必须携带 error 对象")
