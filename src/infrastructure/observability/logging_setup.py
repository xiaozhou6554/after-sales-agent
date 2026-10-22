"""结构化 JSON 日志（技术详细规格 20.1）：按租户/请求/链路索引。

- 每条记录自动附加当前请求的链路三元组、thread 与真实主体；
- 字段清单对齐 20.1：request_id/trace_id/user_id/线程/名称/参数摘要/时间/
  结果摘要/错误/重试/最终结果由调用方以 message+extra 提供；
- 日志内容必须已由调用方脱敏（application.common.masking）；本模块只补链路上下文。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from application.common.tracing import current_subject, current_trace


class TraceContextFilter(logging.Filter):
    """为未显式携带链路的记录补充上下文；调用方 extra 里的字段不被覆盖。

    中间件与路由分属不同任务上下文：路由侧绑定的主体在中间件任务里读不到，
    因此中间件/安全日志经 extra 显式携带，本过滤器只补其余日志的缺口。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        trace = current_trace()
        subject = current_subject()
        candidates = {
            "request_id": trace.request_id if trace else None,
            "correlation_id": trace.correlation_id if trace else None,
            "trace_id": trace.trace_id if trace else None,
            "thread_id": trace.thread_id if trace else None,
            "tenant_id": subject.tenant_id if subject else None,
            "subject_id": subject.subject_id if subject else None,
        }
        for name, value in candidates.items():
            if not hasattr(record, name):
                setattr(record, name, value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "correlation_id": getattr(record, "correlation_id", None),
            "trace_id": getattr(record, "trace_id", None),
            "thread_id": getattr(record, "thread_id", None),
            "tenant_id": getattr(record, "tenant_id", None),
            "subject_id": getattr(record, "subject_id", None),
        }
        extras = getattr(record, "details", None)
        if extras is not None:
            payload["details"] = extras
        if record.exc_info:
            # 只记录异常类型与消息，不落堆栈（26.x：错误不携带堆栈）。
            payload["details"] = {
                **(payload.get("details") or {}),
                "exception": f"{record.exc_info[0].__name__}: {record.exc_info[1]}",
            }
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """进程级日志装配：stdout JSON、链路过滤；幂等可重复调用。"""
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceContextFilter())
    root.handlers = [handler]
    root.setLevel(level)
