"""HTTP 公共传输契约（《技术详细规格》26.5）。

- 所有 Schema 版本 "1.0"，默认禁止未知字段（extra="forbid"）；
- 必填但无值的 nullable 字段必须显式 null；数组空值为 []，结构化空值为 {}；
- 扩展通过新 Schema 版本与兼容测试，不在旧结构里添加字段；
- 内部用例/领域层不使用本模块的 Pydantic 模型（经 converters 受控转换）。
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"


class StrictModel(BaseModel):
    """全项目传输模型的基类：禁未知字段、禁赋值漂移。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TraceContextModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    thread_id: str | None = None


class BusinessRefModel(StrictModel):
    resource_type: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    version: str | None = None


class RequestedInputModel(StrictModel):
    field: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    input_type: Literal["text", "choice", "secure_form"]
    required: bool
    form_ref: str | None = None


class FieldErrorModel(StrictModel):
    field: str = Field(min_length=1)
    code: str = Field(min_length=1)


class ErrorModel(StrictModel):
    """错误结构：不携带堆栈、凭证、原始工具结果或资源存在性。"""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    error_code: str = Field(min_length=1)
    message: str
    retryable: bool
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    details: dict = Field(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        # details 只允许 field_errors 列表，元素只含 field/code。
        field_errors = (
            self.details.get("field_errors", []) if isinstance(self.details, dict) else []
        )
        if not isinstance(field_errors, list):
            raise ValueError("details.field_errors 必须为数组")
        for item in field_errors:
            if not isinstance(item, dict) or set(item) - {"field", "code"} or not item.get("field"):
                raise ValueError("details.field_errors 元素只含 field/code 且 field 非空")


class RagEvidenceExtrasModel(StrictModel):
    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    vector_score: float | None = None
    bm25_score: float | None = None
    final_score: float | None = None
    retrieval_source: str = Field(min_length=1)


class KgEvidenceExtrasModel(StrictModel):
    entities: list[str] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)


class PolicyEvidenceExtrasModel(StrictModel):
    effective_from: str | None = None
    effective_to: str | None = None


class EvidenceBaseModel(StrictModel):
    evidence_id: str = Field(min_length=1)
    evidence_type: str
    source_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    version: str = Field(min_length=1)
    knowledge_release_id: str = Field(min_length=1)
    content: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class RagEvidenceModel(RagEvidenceExtrasModel, EvidenceBaseModel):
    evidence_type: Literal["rag"]


class KgEvidenceModel(KgEvidenceExtrasModel, EvidenceBaseModel):
    evidence_type: Literal["kg"]


class PolicyEvidenceModel(PolicyEvidenceExtrasModel, EvidenceBaseModel):
    evidence_type: Literal["policy"]


EvidenceModel = Annotated[
    Union[RagEvidenceModel, KgEvidenceModel, PolicyEvidenceModel],
    Field(discriminator="evidence_type"),
]
