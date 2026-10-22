"""按可信主体与客户范围查询持久化任务状态。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from application.agent.ports.checkpoint import (
    CheckpointSchemaError,
    CheckpointUnavailableError,
)
from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext, TraceContext
from application.common.dto import ApiResponseDto
from application.common.errors import ErrorCode
from interface.http.middleware.auth import get_auth_context, request_trace
from interface.http.schemas.converters import api_response_dto_to_model

router = APIRouter(tags=["tasks"])


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    task = request.app.state.task_service.get(auth, task_id)
    request_context = request_trace()
    trace = TraceContext(
        schema_version=request_context.schema_version,
        request_id=request_context.request_id,
        correlation_id=request_context.correlation_id,
        trace_id=request_context.trace_id,
        thread_id=task.thread_id,
    )
    pending = task.status in ("pending", "running")
    chat_service = getattr(request.app.state, "chat_service", None)
    try:
        answer = (
            chat_service.read_task_answer(task, auth)
            if task.task_type == "agent.chat" and hasattr(chat_service, "read_task_answer")
            else None
        )
    except CheckpointSchemaError as exc:
        raise AuthorizationDenied(ErrorCode.SCHEMA_VERSION_UNSUPPORTED, str(exc)) from exc
    except CheckpointUnavailableError as exc:
        raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "任务结果暂不可读") from exc
    response = ApiResponseDto(
        trace=trace,
        status="pending" if pending else "success",
        answer="任务仍在处理中。" if pending else "任务状态已更新。",
        next_action="wait" if pending else "finish",
        async_task_id=task.task_id if pending else None,
        data={
            "task": {
                "task_id": task.task_id,
                "status": task.status,
                "result_ref": task.result_ref,
                "error": task.error,
                "updated_at": task.updated_at.isoformat(),
                **({"answer": answer} if answer is not None else {}),
            }
        },
    )
    model = api_response_dto_to_model(response)
    return JSONResponse(status_code=200, content=model.model_dump(mode="json"))
