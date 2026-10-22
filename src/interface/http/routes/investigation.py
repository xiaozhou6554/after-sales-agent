"""主管异常追查工作台：开立/解释/修订/重放与只读发现（FR-24）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from application.common.authorization import (
    AuthorizationDenied,
    require_all_scopes,
    require_scope,
)
from application.common.errors import ErrorCode
from interface.http.middleware.auth import get_auth_context
from interface.http.routes.warehouse import _respond
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(prefix="/workbench/investigations", tags=["investigation"])


class MetricAnomalyModel(StrictModel):
    schema_version: str = SCHEMA_VERSION
    metric_id: str = Field(min_length=1, max_length=64)
    product_id: str = Field(min_length=1, max_length=64)
    cohort: str = Field(min_length=4, max_length=32)
    baseline_cohort: str = Field(min_length=4, max_length=32)
    threshold_pp: str = Field(default="5", min_length=1, max_length=16)
    reason: str | None = Field(default=None, max_length=255)


class ReviseModel(StrictModel):
    schema_version: str = SCHEMA_VERSION
    reason: str = Field(min_length=1, max_length=255)


def _service(request: Request):
    service = getattr(request.app.state, "investigation_service", None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务暂不可用")
    return service


@router.post("/metric-anomalies")
def open_metric_anomaly(
    payload: MetricAnomalyModel, request: Request, auth=Depends(get_auth_context)
):
    require_scope(auth, "incident:manage")
    result = _service(request).open_metric_anomaly(
        auth,
        metric_id=payload.metric_id,
        product_id=payload.product_id,
        cohort=payload.cohort,
        baseline_cohort=payload.baseline_cohort,
        threshold_pp=payload.threshold_pp,
        reason=payload.reason,
    )
    return _respond(200, result, "异常调查已开立（固定快照，只增修订）")


@router.get("/detect/runtime")
def detect_runtime(request: Request, auth=Depends(get_auth_context)):
    require_scope(auth, "incident:read")
    return _respond(200, _service(request).detect_runtime_anomalies(auth))


@router.get("/detect/cross-system")
def detect_cross_system(request: Request, auth=Depends(get_auth_context)):
    require_scope(auth, "incident:read")
    return _respond(200, _service(request).detect_cross_system_conflicts(auth))


@router.get("/detect/knowledge")
def detect_knowledge(request: Request, auth=Depends(get_auth_context)):
    require_scope(auth, "incident:read")
    return _respond(200, _service(request).detect_knowledge_anomalies(auth))


@router.get("")
def list_investigations(request: Request, auth=Depends(get_auth_context)):
    require_scope(auth, "incident:read")
    investigations = _service(request).list_investigations(auth.tenant_id)
    return _respond(200, {"results": investigations, "count": len(investigations)})


@router.post("/{investigation_id}/revise")
def revise_investigation(
    investigation_id: str,
    payload: ReviseModel,
    request: Request,
    auth=Depends(get_auth_context),
):
    require_scope(auth, "incident:manage")
    result = _service(request).revise(
        auth, investigation_id,
        actor_subject_id=auth.subject_id, reason=payload.reason,
    )
    return _respond(200, result, "修订已追加（原修订保持不变）")


@router.post("/{investigation_id}/replay")
def replay_investigation(
    investigation_id: str,
    request: Request,
    revision: int = Query(ge=1),
    auth=Depends(get_auth_context),
):
    require_all_scopes(auth, ["incident:read", "analytics:read"])
    return _respond(
        200,
        _service(request).replay(auth, investigation_id, revision),
        "固定快照重放完成",
    )


@router.get("/{investigation_id}")
def explain_investigation(
    investigation_id: str,
    request: Request,
    revision: int | None = Query(default=None, ge=1),
    auth=Depends(get_auth_context),
):
    require_all_scopes(auth, ["incident:read", "analytics:read"])
    return _respond(
        200,
        _service(request).explain(auth, investigation_id, revision=revision),
    )
