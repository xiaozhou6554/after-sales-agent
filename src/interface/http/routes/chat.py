"""对话路由（POST /chat，技术详细规格 26.6，流程步骤10）。

- 身份来自 Bearer 会话令牌（get_auth_context），请求体不携带身份字段；
- 链路三元组由服务端中间件生成，响应经受控转换回传输模型；
- 错误状态按 26.8 映射 HTTP 码；授权拒绝由全局异常处理器收束。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from application.agent.ports.checkpoint import (
    CheckpointConflictError,
    CheckpointSchemaError,
    CheckpointUnavailableError,
)
from application.common.authorization import AuthorizationDenied
from application.common.context import AuthContext
from application.common.dto import ApiResponseDto
from application.common.errors import ErrorCode, http_status_for
from application.common.tasks.chat_submission import ChatTaskSubmission
from application.common.tasks.models import TaskConflictError
from interface.http.middleware.auth import bearer_token, get_auth_context, request_trace
from interface.http.schemas.chat import ChatRequestModel
from interface.http.schemas.converters import api_response_dto_to_model, chat_request_to_dto

router = APIRouter(tags=["chat"])
logger = logging.getLogger("after_sales.agent")


@router.post("/chat")
def chat(
    payload: ChatRequestModel,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    trace = request_trace()
    dto = chat_request_to_dto(payload, trace)
    service = request.app.state.chat_service
    prefer = request.headers.get("Prefer", "")
    if "respond-async" in {item.strip().lower() for item in prefer.split(",")}:
        if not request.app.state.background_chat_enabled:
            raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "后台执行身份尚未配置")
        key = request.headers.get("Idempotency-Key", "")
        try:
            accepted = ChatTaskSubmission(service, request.app.state.task_service).submit(
                dto, auth, key
            )
        except (TaskConflictError, CheckpointConflictError) as exc:
            raise AuthorizationDenied(ErrorCode.IDEMPOTENCY_CONFLICT, str(exc)) from exc
        except CheckpointSchemaError as exc:
            raise AuthorizationDenied(ErrorCode.SCHEMA_VERSION_UNSUPPORTED, str(exc)) from exc
        except CheckpointUnavailableError as exc:
            raise AuthorizationDenied(
                ErrorCode.DEPENDENCY_UNAVAILABLE, "Checkpoint 暂不可用"
            ) from exc
        except ValueError as exc:
            raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, str(exc)) from exc
        except AuthorizationDenied:
            raise
        except Exception as exc:  # noqa: BLE001 - 持久化失败统一转受控依赖错误
            logger.exception("异步对话受理失败")
            raise AuthorizationDenied(ErrorCode.DEPENDENCY_UNAVAILABLE, "任务受理暂不可用") from exc
        task = accepted.task
        pending = task.status in ("pending", "running")
        result = ApiResponseDto(
            trace=accepted.trace,
            status="pending" if pending else "success",
            answer="任务已受理。" if pending else "任务状态已更新。",
            next_action="wait" if pending else "finish",
            async_task_id=task.task_id if pending else None,
            data={
                "task": {
                    "task_id": task.task_id,
                    "status": task.status,
                    "result_ref": task.result_ref,
                    "error": task.error,
                    "updated_at": task.updated_at.isoformat(),
                }
            },
        )
        model = api_response_dto_to_model(result)
        return JSONResponse(
            status_code=202 if pending else 200, content=model.model_dump(mode="json")
        )
    result = service.execute(dto, auth, session_token=bearer_token(request) or "")
    model = api_response_dto_to_model(result)
    status_code = http_status_for(result.error.error_code) if result.status == "error" else 200
    return JSONResponse(status_code=status_code, content=model.model_dump(mode="json"))
