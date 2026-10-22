"""HTTP 可观测性中间件（技术详细规格 20.1/20.2）。

- 每个请求绑定服务端链路上下文（contextvars），日志自动携带；
- 记录开始/结束时间、方法、路由模板、状态与耗时；不记录头部与请求体；
- 指标经统一采集点累计（请求计数与延迟），业务后续只补充业务事件。
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request, Response

from application.common.tracing import bind_request_context
from interface.http.middleware.auth import new_server_trace

logger = logging.getLogger("after_sales.access")


def install_observability(app: FastAPI) -> None:
    @app.middleware("http")
    async def observe_request(request: Request, call_next) -> Response:
        trace = new_server_trace()
        bind_request_context(trace)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            route = request.scope.get("route")
            path_template = getattr(route, "path", request.url.path)
            metrics = getattr(request.app.state, "metrics", None)
            if metrics is not None:
                tags = {"method": request.method, "path": path_template}
                metrics.incr("http_requests", tags=tags)
                if status_code >= 400:
                    metrics.incr("http_errors", tags={**tags, "status": str(status_code)})
                metrics.observe("http_latency_ms", duration_ms, tags=tags)
            # 链路与主体字段直接随记录携带（20.1）；主体由认证依赖写入共享 scope。
            subject = getattr(request.state, "subject_frame", None)
            logger.info(
                "%s %s -> %s",
                request.method,
                path_template,
                status_code,
                extra={
                    "details": {"duration_ms": round(duration_ms, 2)},
                    "request_id": trace.request_id,
                    "correlation_id": trace.correlation_id,
                    "trace_id": trace.trace_id,
                    "thread_id": trace.thread_id,
                    "tenant_id": subject.tenant_id if subject else None,
                    "subject_id": subject.subject_id if subject else None,
                },
            )
