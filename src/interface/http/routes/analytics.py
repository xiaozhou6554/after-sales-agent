"""Development-only analytics workbench: projection run and snapshot evidence."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from application.common.authorization import AuthorizationDenied, require_scope
from application.common.errors import ErrorCode
from interface.http.middleware.auth import get_auth_context
from interface.http.routes.warehouse import _respond
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(prefix="/workbench/analytics", tags=["after-sales-analytics"])


class ProjectionRunModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    projection_version: int = Field(ge=1)
    as_of: date | None = None


def _service(request: Request):
    service = getattr(request.app.state, "analytics_projection", None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务暂不可用")
    return service


@router.post("/projection/run")
def run_projection(
    payload: ProjectionRunModel, request: Request, auth=Depends(get_auth_context)
):
    require_scope(auth, "ontology:manage")
    summary = _service(request).project(
        auth.tenant_id,
        payload.projection_version,
        as_of=payload.as_of or date.today(),
    )
    return _respond(200, summary, "分析投影已完成")


@router.get("/snapshots")
def query_snapshots(
    request: Request,
    metric_id: str = Query(min_length=1, max_length=64),
    cohort_start: str = Query(min_length=4, max_length=32),
    cohort_end: str = Query(min_length=4, max_length=32),
    auth=Depends(get_auth_context),
):
    require_scope(auth, "analytics:read")
    snapshots = _service(request).raw_snapshots(
        auth.tenant_id,
        metric_id=metric_id,
        cohort_start=cohort_start,
        cohort_end=cohort_end,
    )
    return _respond(200, {"results": snapshots, "count": len(snapshots)})
