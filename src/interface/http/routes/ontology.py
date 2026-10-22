"""Development-only ontology release workbench: draft/validate/publish/rollback."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from application.common.authorization import AuthorizationDenied, require_scope
from application.common.context import TraceContext
from application.common.errors import ErrorCode
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.routes.warehouse import _respond
from interface.http.schemas.common import SCHEMA_VERSION, StrictModel

router = APIRouter(prefix="/workbench/ontology", tags=["ontology-release"])


class ReleaseDraftModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    release_id: str = Field(min_length=1, max_length=64)
    effective_at: datetime | None = None


class ReleasePublishModel(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    expected_active_revision: int | None = Field(default=None, ge=0)


def _service(request: Request):
    service = getattr(request.app.state, "ontology_release_service", None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "服务暂不可用")
    return service


def _trace() -> TraceContext:
    base = request_trace()
    return TraceContext(
        "1.0", base.request_id, base.correlation_id, base.trace_id, base.thread_id
    )


def _ontology_auth(auth) -> None:
    require_scope(auth, "ontology:manage")


@router.post("/releases", status_code=201)
def draft_release(
    payload: ReleaseDraftModel,
    request: Request,
    auth=Depends(get_auth_context),
):
    _ontology_auth(auth)
    try:
        release = _service(request).draft(
            auth.tenant_id,
            payload.release_id,
            effective_at=payload.effective_at,
        )
    except Exception as exc:
        _failure(exc)
    return _respond(201, _release_dict(release), "发布草稿已冻结")


@router.post("/releases/{release_id}/validate")
def validate_release(
    release_id: str, request: Request, auth=Depends(get_auth_context)
):
    _ontology_auth(auth)
    try:
        result = _service(request).validate_release(auth.tenant_id, release_id)
    except Exception as exc:
        _failure(exc)
    return _respond(200, result, "SHACL 校验通过")


@router.post("/releases/{release_id}/publish")
def publish_release(
    release_id: str,
    payload: ReleasePublishModel,
    request: Request,
    auth=Depends(get_auth_context),
):
    _ontology_auth(auth)
    try:
        release = _service(request).publish(
            auth.tenant_id,
            release_id,
            expected_active_revision=payload.expected_active_revision,
            actor_subject_id=auth.subject_id,
            trace=_trace(),
        )
    except Exception as exc:
        _failure(exc)
    return _respond(200, _release_dict(release), "本体发布已激活")


@router.post("/releases/{release_id}/rollback")
def rollback_release(
    release_id: str,
    payload: ReleasePublishModel,
    request: Request,
    auth=Depends(get_auth_context),
):
    _ontology_auth(auth)
    try:
        release = _service(request).rollback(
            auth.tenant_id,
            release_id,
            expected_active_revision=payload.expected_active_revision,
            actor_subject_id=auth.subject_id,
            trace=_trace(),
        )
    except Exception as exc:
        _failure(exc)
    return _respond(200, _release_dict(release), "本体已回滚")


@router.get("/active")
def active_release(request: Request, auth=Depends(get_auth_context)):
    require_scope(auth, "analytics:read")
    release = _service(request).active(auth.tenant_id)
    return _respond(200, _release_dict(release) if release else {"active": None})


@router.get("/releases")
def list_releases(request: Request, auth=Depends(get_auth_context)):
    _ontology_auth(auth)
    history = _service(request).history(auth.tenant_id)
    return _respond(200, {"releases": [_release_dict(r) for r in history]})


def _release_dict(release: dict) -> dict:
    return {
        "release_id": release.get("release_id"),
        "schema_version": release.get("schema_version"),
        "mapping_version": release.get("mapping_version"),
        "metric_version": release.get("metric_version"),
        "manifest_digest": release.get("manifest_digest"),
        "status": release.get("status"),
        "revision": release.get("revision"),
        "effective_at": (
            release["effective_at"].isoformat()
            if release.get("effective_at") is not None
            and hasattr(release["effective_at"], "isoformat")
            else release.get("effective_at")
        ),
        "activated_at": (
            release["activated_at"].isoformat()
            if release.get("activated_at") is not None
            and hasattr(release["activated_at"], "isoformat")
            else release.get("activated_at")
        ),
    }


def _failure(exc: Exception):
    if isinstance(exc, AuthorizationDenied):
        raise exc
    raise AuthorizationDenied(ErrorCode.STATE_CONFLICT, str(exc)[:300] or "操作失败")
