"""传输模型与应用 DTO 的受控转换（《技术详细规格》26.6/26.8）。

方向约束：接口层在调用用例前把传输模型转成 application DTO；
用例返回的 application DTO 在接口层转回传输模型。领域层不感知两者。
"""

from __future__ import annotations

from application.common.context import BusinessRef, RequestedInput, TraceContext
from application.common.dto import (
    ApiResponseDto,
    ChatRequestDto,
    KgEvidence,
    PolicyEvidence,
    RagEvidence,
)
from interface.http.schemas.chat import APIResponseModel, ChatRequestModel
from interface.http.schemas.common import (
    ErrorModel,
    EvidenceModel,
    KgEvidenceModel,
    PolicyEvidenceModel,
    RagEvidenceModel,
    RequestedInputModel,
    TraceContextModel,
)


def trace_model_to_dto(model: TraceContextModel) -> TraceContext:
    return TraceContext(
        schema_version=model.schema_version,
        request_id=model.request_id,
        correlation_id=model.correlation_id,
        trace_id=model.trace_id,
        thread_id=model.thread_id,
    )


def chat_request_to_dto(model: ChatRequestModel, trace: TraceContext) -> ChatRequestDto:
    return ChatRequestDto(
        trace=trace,
        message=model.message,
        business_refs=tuple(
            BusinessRef(
                resource_type=ref.resource_type,
                resource_id=ref.resource_id,
                version=ref.version,
            )
            for ref in model.business_refs
        ),
        thread_id=model.thread_id,
        customer_id=model.customer_id,
    )


def error_dto_to_model(dto) -> ErrorModel:
    return ErrorModel(
        schema_version=dto.schema_version,
        error_code=dto.error_code,
        message=dto.message,
        retryable=dto.retryable,
        request_id=dto.request_id,
        correlation_id=dto.correlation_id,
        trace_id=dto.trace_id,
        details={"field_errors": [{"field": fe.field, "code": fe.code} for fe in dto.details]},
    )


def requested_input_dto_to_model(dto: RequestedInput) -> RequestedInputModel:
    return RequestedInputModel(
        field=dto.field,
        prompt=dto.prompt,
        input_type=dto.input_type,
        required=dto.required,
        form_ref=dto.form_ref,
    )


def evidence_dto_to_model(evidence) -> EvidenceModel:
    base = dict(
        evidence_id=evidence.evidence_id,
        evidence_type=evidence.evidence_type,
        source_id=evidence.source_id,
        source=evidence.source,
        version=evidence.version,
        knowledge_release_id=evidence.knowledge_release_id,
        content=evidence.content,
        confidence=evidence.confidence,
    )
    if isinstance(evidence, RagEvidence):
        return RagEvidenceModel(
            **base,
            chunk_id=evidence.chunk_id,
            document_id=evidence.document_id,
            vector_score=evidence.vector_score,
            bm25_score=evidence.bm25_score,
            final_score=evidence.final_score,
            retrieval_source=evidence.retrieval_source,
        )
    if isinstance(evidence, KgEvidence):
        return KgEvidenceModel(
            **base, entities=list(evidence.entities), relationships=list(evidence.relationships)
        )
    if isinstance(evidence, PolicyEvidence):
        return PolicyEvidenceModel(
            **base, effective_from=evidence.effective_from, effective_to=evidence.effective_to
        )
    raise ValueError(f"未支持的证据类型: {type(evidence).__name__}")


def api_response_dto_to_model(dto: ApiResponseDto) -> APIResponseModel:
    return APIResponseModel(
        request_id=dto.trace.request_id,
        correlation_id=dto.trace.correlation_id,
        trace_id=dto.trace.trace_id,
        thread_id=dto.trace.thread_id,
        status=dto.status,
        answer=dto.answer,
        next_action=dto.next_action,
        handoff_required=dto.handoff_required,
        requested_inputs=[requested_input_dto_to_model(item) for item in dto.requested_inputs],
        sources=[evidence_dto_to_model(item) for item in dto.sources],
        async_task_id=dto.async_task_id,
        data=dto.data,
        error=error_dto_to_model(dto.error) if dto.error else None,
    )
