"""存活与就绪检查：存活只表示进程在运行；就绪返回 ready/degraded/unavailable。

响应不包含用户业务数据（技术详细规格 26）；unavailable 返回 503，
degraded 返回 200 但显式标注降级，不伪造可用。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from application.common.health import UNAVAILABLE

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict:
    return {"status": "alive"}


@router.get("/health/ready")
def ready(request: Request) -> JSONResponse:
    report = request.app.state.readiness.execute()
    status_code = 503 if report.status == UNAVAILABLE else 200
    return JSONResponse(
        status_code=status_code,
        content={"status": report.status, "dependencies": report.dependencies},
    )
