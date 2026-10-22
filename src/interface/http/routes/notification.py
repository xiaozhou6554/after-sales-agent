"""仅本地开发启用的通知状态查询；unknown 如实标注待核实，不伪造成功。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext
from application.common.errors import ErrorCode
from application.notification.queries import NotificationQueryUseCase
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.schemas.chat import APIResponseModel

router = APIRouter(tags=["notification"])

STATUS_LABELS = {
    "pending": "发送中", "sent": "已发送", "failed": "发送失败", "unknown": "待核实"
}


def _respond(status_code: int, answer: str, data: dict, *, status="success") -> JSONResponse:
    trace = request_trace()
    response = APIResponseModel(
        request_id=trace.request_id,
        correlation_id=trace.correlation_id,
        trace_id=trace.trace_id,
        thread_id=trace.thread_id,
        status=status,
        answer=answer,
        next_action="finish",
        data=data,
    )
    return JSONResponse(status_code=status_code, content=response.model_dump(mode="json"))


def _service(request: Request) -> NotificationQueryUseCase:
    service = getattr(request.app.state, "notification_queries", None)
    if service is None:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "通知服务暂不可用")
    return service


@router.get("/notifications")
def list_notifications(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    data = _service(request).list(auth, status=status, limit=limit)
    items = data["notifications"]
    answer = f"共 {len(items)} 条通知"
    if data["unknown_count"]:
        answer += f"，其中 {data['unknown_count']} 条发送结果待核实"
    return _respond(200, answer, data)


@router.get("/notifications/{notification_id}")
def get_notification(
    notification_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    item = _service(request).get(auth, notification_id)
    return _respond(200, f"通知发送状态：{item['status_label']}", item)
