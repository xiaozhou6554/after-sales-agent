"""对话服务：POST /chat 的应用用例（同步闭环，流程步骤10 / M1）。

职责：组装公共 State 与受限白名单→驱动 LangGraph 工作流→按 26.6 契约
映射 APIResponse。可信身份来自服务端 AuthContext；会话令牌仅经受控传输
转发给 MCP 客户端，不进入 State、模型上下文或返回值。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

from application.agent.ports.checkpoint import (
    CheckpointConflictError,
    CheckpointRepositoryPort,
    CheckpointSchemaError,
    CheckpointUnavailableError,
)
from application.agent.ports.llm import ChatMessage, LLMGateway
from application.agent.ports.memory import (
    MemoryPersistenceError,
    MemoryUnavailableError,
    SessionRepositoryPort,
)
from application.agent.ports.tools import ToolExecutionPort
from application.agent.prompts import PromptCatalog
from application.agent.state import NEXT_ASK_USER, initial_state
from application.agent.tool_catalog import ToolCatalog
from application.agent.workflow.graph import WorkflowDeps, build_chat_graph
from application.common.authorization import require_customer_access
from application.common.context import (
    AuthContext,
    ErrorDto,
    RequestedInput,
    TraceContext,
)
from application.common.dto import ApiResponseDto, ChatRequestDto
from application.common.errors import ErrorCode
from application.common.execution.invocation_scope import bind_tool_tenant
from application.common.execution.timeout_policy import bind_deadline_at
from application.common.tasks.models import TaskRecord
from application.memory.ports.profile import ProfileUnavailableError
from application.memory.use_cases.long_term import LongTermMemoryUseCase
from application.memory.use_cases.session_context import SessionContextUseCase

# 简单同步请求硬超时（技术详细规格 1.4）。
SYNC_HARD_TIMEOUT_S = 15.0

logger = logging.getLogger("after_sales.agent")


class ChatService:
    def __init__(
        self,
        *,
        llm: LLMGateway,
        tools: ToolExecutionPort,
        memory: SessionRepositoryPort,
        prompts: PromptCatalog,
        registry_path: str,
        max_steps: int = 8,
        timeout_s: float = SYNC_HARD_TIMEOUT_S,
        checkpointer: CheckpointRepositoryPort | None = None,
        session_context: SessionContextUseCase | None = None,
        long_term_memory: LongTermMemoryUseCase | None = None,
        registered_tool_names: frozenset[str] | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._memory = memory
        self._prompts = prompts
        self._registry_path = registry_path
        self._max_steps = max_steps
        self._timeout_s = timeout_s
        self._registered_tool_names = registered_tool_names
        # 步骤12：MySQL Checkpoint 保存器经 Bootstrap 注入；None 仅限
        # 测试/显式降级（生产装配必须提供，规格 545 行禁仅内存存储）。
        self._checkpointer = checkpointer
        self._session_context = session_context
        self._long_term_memory = long_term_memory
        self._graph = None

    def _compiled_graph(self):
        if self._graph is None:
            self._graph = build_chat_graph(checkpointer=self._checkpointer)
        return self._graph

    def prepare_async(
        self,
        request: ChatRequestDto,
        auth: AuthContext,
        *,
        thread_id: str,
        idempotency_key: str,
    ) -> str:
        """先将输入暂停在 Supervisor 前，再允许任务及 Outbox 事务受理。"""
        if self._checkpointer is None:
            raise CheckpointUnavailableError("异步任务需要持久化 Checkpoint")
        if request.customer_id:
            require_customer_access(auth, request.customer_id)
        digest = hashlib.sha256(
            json.dumps(
                [
                    auth.tenant_id,
                    auth.subject_id,
                    thread_id,
                    idempotency_key,
                    request.customer_id,
                    request.message,
                    [vars(ref) for ref in request.business_refs],
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        summary = self._checkpointer.latest(auth.tenant_id, thread_id)
        values = (summary.state.get("channel_values") or {}) if summary else {}
        if values.get("user_id") and values.get("user_id") != auth.subject_id:
            raise CheckpointConflictError("会话不属于当前主体")
        if values.get("submission_digest") == digest:
            return summary.checkpoint_id
        if values.get("submission_digest") and not values.get("final_answer"):
            raise CheckpointConflictError("同一会话仍有尚未完成的后台对话")
        state = initial_state(
            user_id=auth.subject_id,
            thread_id=thread_id,
            trace_id=request.trace.trace_id,
            user_question=request.message,
        )
        state["submission_digest"] = digest
        paused = build_chat_graph(checkpointer=self._checkpointer, interrupt_before=["supervisor"])
        paused.invoke(
            state,
            config={
                "configurable": {
                    "tenant_id": auth.tenant_id,
                    "thread_id": thread_id,
                    "checkpoint_ns": "",
                },
                "recursion_limit": 50,
            },
            durability="sync",
        )
        saved = self._checkpointer.latest(auth.tenant_id, thread_id)
        saved_values = (saved.state.get("channel_values") or {}) if saved else {}
        if saved is None or saved_values.get("submission_digest") != digest:
            raise CheckpointConflictError("异步任务准备后的 Checkpoint 与请求不一致")
        return saved.checkpoint_id

    def resume_task(
        self,
        task: TaskRecord,
        auth: AuthContext,
        session_token: str,
    ) -> str:
        """沿已持久化暂停点运行；不重新追加用户输入或建立新图线程。"""
        if self._checkpointer is None:
            raise CheckpointUnavailableError("后台恢复需要持久化 Checkpoint")
        if task.tenant_id != auth.tenant_id or task.actor_subject_id != auth.subject_id:
            raise ValueError("任务执行身份与持久记录不匹配")
        require_customer_access(auth, task.customer_id)
        saved = self._checkpointer.latest(task.tenant_id, task.thread_id)
        if saved is None:
            raise CheckpointUnavailableError("后台任务缺少恢复 Checkpoint")
        prepared = self._checkpointer.by_id(
            task.tenant_id, task.thread_id, task.checkpoint_ref or ""
        )
        if prepared is None:
            raise CheckpointConflictError("后台任务原暂停点不存在")
        values = saved.state.get("channel_values") or {}
        prepared_values = prepared.state.get("channel_values") or {}
        if (
            not values.get("submission_digest")
            or values.get("submission_digest") != prepared_values.get("submission_digest")
            or values.get("user_id") != auth.subject_id
            or not values.get("user_question")
        ):
            raise CheckpointConflictError("后台任务恢复依据与异步提交不匹配")
        history = (
            self._session_context.history(task.tenant_id, task.thread_id, auth.subject_id)
            if self._session_context is not None
            else self._memory.load(task.tenant_id, task.thread_id, subject_id=auth.subject_id)
        )
        if self._long_term_memory is not None:
            history = self._long_term_memory.context(auth, task.customer_id) + history
        trace = TraceContext(
            schema_version="1.0",
            request_id=task.input_masked["request_id"],
            correlation_id=task.input_masked["correlation_id"],
            trace_id=task.input_masked["trace_id"],
            thread_id=task.thread_id,
        )
        deadline = time.monotonic() + 30.0
        deps = WorkflowDeps(
            llm=self._llm,
            tools=self._tools,
            catalog=ToolCatalog.load(
                self._registry_path, auth, registered_names=self._registered_tool_names
            ),
            prompts=self._prompts,
            history=list(history),
            session_token=session_token,
            trace=trace,
            max_steps=self._max_steps,
            deadline_monotonic=deadline,
        )
        with bind_deadline_at(deadline), bind_tool_tenant(task.tenant_id):
            result = self._compiled_graph().invoke(
                None,
                config={
                    "configurable": {
                        "deps": deps,
                        "tenant_id": task.tenant_id,
                        "thread_id": task.thread_id,
                        "checkpoint_ns": "",
                    },
                    "recursion_limit": 50,
                },
                durability="sync",
            )
        if not result.get("final_answer"):
            raise CheckpointConflictError("后台任务未形成可查询答复")
        if result.get("final_status") == "error":
            raise RuntimeError("后台对话以错误状态结束")
        finished = self._checkpointer.latest(task.tenant_id, task.thread_id)
        if finished is None:
            raise CheckpointUnavailableError("后台任务结果 Checkpoint 未落盘")
        summaries = [
            {
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
                "error_code": item.get("error_code"),
            }
            for item in result.get("tool_calls") or []
            if item.get("tool_name")
        ]
        inserted = self._memory.append_turn_once(
            task.tenant_id,
            task.thread_id,
            auth.subject_id,
            task.task_id,
            values["user_question"],
            result["final_answer"],
            tool_summaries=summaries,
        )
        if inserted and self._long_term_memory is not None:
            self._long_term_memory.remember_turn(auth, task.customer_id, values["user_question"])
        return finished.checkpoint_id

    def read_task_answer(self, task: TaskRecord, auth: AuthContext) -> str | None:
        if task.status != "succeeded" or not task.result_ref or self._checkpointer is None:
            return None
        if task.tenant_id != auth.tenant_id or task.actor_subject_id != auth.subject_id:
            raise ValueError("任务结果身份不匹配")
        require_customer_access(auth, task.customer_id)
        saved = self._checkpointer.by_id(task.tenant_id, task.thread_id, task.result_ref)
        if saved is None:
            raise CheckpointUnavailableError("任务结果 Checkpoint 不存在")
        return str((saved.state.get("channel_values") or {}).get("final_answer") or "")

    def execute(
        self,
        request: ChatRequestDto,
        auth: AuthContext,
        session_token: str,
    ) -> ApiResponseDto:
        deadline_monotonic = time.monotonic() + self._timeout_s
        trace = request.trace
        thread_id = request.thread_id or f"thread-{uuid.uuid4().hex}"
        effective_trace = TraceContext(
            schema_version=trace.schema_version,
            request_id=trace.request_id,
            correlation_id=trace.correlation_id,
            trace_id=trace.trace_id,
            thread_id=thread_id,
        )
        # 客服代客：目标客户必须在授权数据范围内（普通用户只能本人）。
        if request.customer_id:
            require_customer_access(auth, request.customer_id)

        catalog = ToolCatalog.load(
            self._registry_path, auth, registered_names=self._registered_tool_names
        )
        # 会话历史承载上下文；身份/权限始终来自 AuthContext，不从会话恢复。
        try:
            history = (
                self._session_context.history(auth.tenant_id, thread_id, auth.subject_id)
                if self._session_context is not None
                else self._memory.load(auth.tenant_id, thread_id, subject_id=auth.subject_id)
            )
            if self._long_term_memory is not None:
                try:
                    history = (
                        self._long_term_memory.context(auth, request.customer_id or auth.subject_id)
                        + history
                    )
                except ProfileUnavailableError:
                    logger.exception("长期记忆数据库不可用，需管理员检查")
            self._memory.append(
                auth.tenant_id,
                thread_id,
                auth.subject_id,
                ChatMessage(role="user", content=request.message),
            )
        except MemoryUnavailableError:
            logger.exception(
                "session memory unavailable: request_id=%s", effective_trace.request_id
            )
            return self._dependency_error(effective_trace)
        except MemoryPersistenceError:
            # 语义冲突（会话已归属其他主体）：不可重试，不泄露他人会话存在性。
            logger.warning("session subject conflict: request_id=%s", effective_trace.request_id)
            return self._conflict_error(effective_trace)

        state = initial_state(
            user_id=auth.subject_id,
            thread_id=thread_id,
            trace_id=effective_trace.trace_id,
            user_question=request.message,
        )
        deps = WorkflowDeps(
            llm=self._llm,
            tools=self._tools,
            catalog=catalog,
            prompts=self._prompts,
            history=list(history),
            session_token=session_token,
            trace=effective_trace,
            max_steps=self._max_steps,
            deadline_monotonic=deadline_monotonic,
        )
        if self._checkpointer is not None:
            verification_error = self._verify_recovery_basis(auth, thread_id, effective_trace)
            if verification_error is not None:
                return verification_error
        try:
            # 每个确定性状态迁移同步持久化（规格 545 行保存时机）；
            # 无 checkpointer 时不传 durability（引擎在无持久化路径下
            # 不支持 sync 等待语义）。
            persist_kwargs = {"durability": "sync"} if self._checkpointer else {}
            with bind_deadline_at(deadline_monotonic), bind_tool_tenant(auth.tenant_id):
                final_state = self._compiled_graph().invoke(
                    state,
                    config={
                        "configurable": {
                            "deps": deps,
                            # 联合恢复键：租户由服务端 AuthContext 注入（规格 545 行）。
                            "tenant_id": auth.tenant_id,
                            "thread_id": thread_id,
                            "checkpoint_ns": "",
                        },
                        "recursion_limit": 50,
                    },
                    **persist_kwargs,
                )
        except CheckpointConflictError:
            logger.warning("checkpoint conflict: request_id=%s", effective_trace.request_id)
            return self._checkpoint_conflict_error(effective_trace)
        except CheckpointSchemaError:
            logger.error(
                "checkpoint schema incompatible, blocking recovery: request_id=%s",
                effective_trace.request_id,
            )
            return self._schema_unsupported_error(effective_trace)
        except Exception:  # noqa: BLE001 - LLM/编排异常按依赖失败收束
            logger.exception("chat workflow failed: request_id=%s", effective_trace.request_id)
            return self._dependency_error(effective_trace)

        answer = (deps.final_draft or "").strip()
        next_action = deps.draft_next
        if next_action == NEXT_ASK_USER:
            append_error = self._remember_assistant(
                auth,
                thread_id,
                answer,
                final_state,
                effective_trace,
                question=request.message,
                customer_id=request.customer_id or auth.subject_id,
            )
            if append_error is not None:
                return append_error
            return ApiResponseDto(
                trace=effective_trace,
                status="needs_input",
                answer=answer or "请补充查询所需的信息。",
                next_action="ask_user",
                requested_inputs=(
                    RequestedInput(
                        field=deps.requested_field or "missing_info",
                        prompt=answer or "请补充查询所需的信息。",
                        input_type="text",
                        required=True,
                    ),
                ),
            )
        if deps.final_status == "error" or deps.final_error is not None:
            # 本轮已按失败收束：落盘失败只记录，不覆盖更原始的错误契约。
            try:
                self._remember_assistant(
                    auth,
                    thread_id,
                    answer,
                    final_state,
                    effective_trace,
                    question=request.message,
                    customer_id=request.customer_id or auth.subject_id,
                )
            except MemoryPersistenceError:
                logger.warning(
                    "assistant turn not persisted: request_id=%s",
                    effective_trace.request_id,
                )
            return ApiResponseDto(
                trace=effective_trace,
                status="error",
                answer=answer or "当前无法处理该请求。",
                next_action="handoff" if deps.final_handoff else "finish",
                handoff_required=bool(deps.final_handoff),
                error=deps.final_error or self._dependency_error_payload(effective_trace),
            )
        append_error = self._remember_assistant(
            auth,
            thread_id,
            answer,
            final_state,
            effective_trace,
            question=request.message,
            customer_id=request.customer_id or auth.subject_id,
        )
        if append_error is not None:
            return append_error
        return ApiResponseDto(
            trace=effective_trace,
            status="success",
            answer=answer,
            next_action="handoff" if final_state.get("handoff_required") else "finish",
            handoff_required=bool(final_state.get("handoff_required")),
        )

    def _verify_recovery_basis(
        self,
        auth: AuthContext,
        thread_id: str,
        trace: TraceContext,
    ) -> ApiResponseDto | None:
        """恢复前核对：读取线程最新 Checkpoint，记录已完成调用与未提交写入。

        实际的断点续跑由引擎基于 get_tuple/pending_writes 完成；本核对在
        进入编排前确认恢复依据可读（读失败按依赖失败收束，不盲目重跑），
        并把"已完成工具调用/未提交节点写入"证据落入日志供追踪核对。
        """
        try:
            summary = self._checkpointer.latest(auth.tenant_id, thread_id)
        except CheckpointUnavailableError:
            logger.exception(
                "checkpoint read failed before invoke: request_id=%s", trace.request_id
            )
            return self._dependency_error(trace)
        except CheckpointSchemaError:
            logger.error(
                "checkpoint schema incompatible, blocking recovery: request_id=%s",
                trace.request_id,
            )
            return self._schema_unsupported_error(trace)
        if summary is None:
            return None
        completed_tools = [
            item for item in (summary.state or {}).get("tool_calls") or [] if item.get("tool_name")
        ]
        logger.info(
            "checkpoint recovery basis: request_id=%s thread_id=%s latest=%s "
            "revision=%d completed_tool_calls=%d pending_writes=%d",
            trace.request_id,
            thread_id,
            summary.checkpoint_id,
            summary.revision,
            len(completed_tools),
            len(summary.pending_writes),
        )
        return None

    def _checkpoint_conflict_error(self, trace: TraceContext) -> ApiResponseDto:
        """Checkpoint 并发/分叉冲突：409 结构化返回，不静默覆盖历史。"""
        return ApiResponseDto(
            trace=trace,
            status="error",
            answer="当前会话存在并发更新，请稍后重试。",
            next_action="finish",
            error=ErrorDto(
                error_code=ErrorCode.VERSION_CONFLICT.value,
                message="会话状态并发冲突",
                retryable=True,
                request_id=trace.request_id,
                correlation_id=trace.correlation_id,
                trace_id=trace.trace_id,
            ),
        )

    @staticmethod
    def _schema_unsupported_error(trace: TraceContext) -> ApiResponseDto:
        """State 结构版本不兼容：阻止恢复并转人工（规格 558 行）。"""
        return ApiResponseDto(
            trace=trace,
            status="error",
            answer="该会话的数据结构版本不受支持，已转人工处理。",
            next_action="handoff",
            handoff_required=True,
            error=ErrorDto(
                error_code=ErrorCode.SCHEMA_VERSION_UNSUPPORTED.value,
                message="Checkpoint 结构版本不兼容，恢复被阻止",
                retryable=False,
                request_id=trace.request_id,
                correlation_id=trace.correlation_id,
                trace_id=trace.trace_id,
            ),
        )

    def _remember_assistant(
        self,
        auth: AuthContext,
        thread_id: str,
        answer: str,
        final_state: dict,
        trace: TraceContext,
        *,
        question: str,
        customer_id: str,
    ) -> ApiResponseDto | None:
        """落盘助手轮（含本轮工具摘要）；持久化失败返回依赖错误信封。

        对话上下文不得静默丢弃：已产生有效回答但无法持久化时如实报错，
        由用户重试而非假装上下文连续。
        """
        summaries = [
            {
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
                "error_code": item.get("error_code"),
            }
            for item in final_state.get("tool_calls") or []
            if item.get("tool_name")
        ]
        try:
            self._memory.append(
                auth.tenant_id,
                thread_id,
                auth.subject_id,
                ChatMessage(role="assistant", content=answer),
                tool_summaries=summaries,
            )
        except MemoryUnavailableError:
            logger.exception("session memory write failed: request_id=%s", trace.request_id)
            return self._dependency_error(trace)
        except MemoryPersistenceError:
            logger.warning("session subject conflict on write: request_id=%s", trace.request_id)
            return self._conflict_error(trace)
        if self._long_term_memory is not None:
            self._long_term_memory.remember_turn(auth, customer_id, question)
        return None

    def _dependency_error(self, trace: TraceContext) -> ApiResponseDto:
        return ApiResponseDto(
            trace=trace,
            status="error",
            answer="依赖服务暂不可用，请稍后重试。",
            next_action="finish",
            error=self._dependency_error_payload(trace),
        )

    @staticmethod
    def _conflict_error(trace: TraceContext) -> ApiResponseDto:
        """会话归属冲突：同 thread_id 已归属其他主体，重试无意义（404 语义）。"""
        return ApiResponseDto(
            trace=trace,
            status="error",
            answer="资源不存在或不在授权范围内。",
            next_action="finish",
            error=ErrorDto(
                error_code=ErrorCode.RESOURCE_UNAVAILABLE.value,
                message="资源不存在或不在授权范围内",
                retryable=False,
                request_id=trace.request_id,
                correlation_id=trace.correlation_id,
                trace_id=trace.trace_id,
            ),
        )

    @staticmethod
    def _dependency_error_payload(trace: TraceContext) -> ErrorDto:
        return ErrorDto(
            error_code=ErrorCode.DEPENDENCY_UNAVAILABLE.value,
            message="依赖服务暂不可用，请稍后重试",
            retryable=True,
            request_id=trace.request_id,
            correlation_id=trace.correlation_id,
            trace_id=trace.trace_id,
        )
