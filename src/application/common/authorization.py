"""授权策略与权限校验（《技术详细规格》16.1、27.2，纯 Python）。

- 角色不等于权限全集：判定一律基于 AuthContext 携带的 scopes 与 data_scope；
- 未授权资源与不存在统一 404 + RESOURCE_UNAVAILABLE（26.8），
  401 仅用于未认证或身份过期；本模块不引入 FORBIDDEN 语义；
- "*" 是系统级客户范围的受控标记，仅允许 operations/admin 携带
  （由基础设施适配器在装载 fixture 时强制校验）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from application.common.context import AuthContext
from application.common.errors import ErrorCode, http_status_for

CUSTOMER_ROLE = "customer"
SUPPORT_ROLE = "support"
HUMAN_AGENT_ROLE = "human_agent"
SUPERVISOR_ROLE = "supervisor"
OPERATIONS_ROLE = "operations"
ADMIN_ROLE = "admin"
WAREHOUSE_ROLE = "warehouse"
PAYMENT_ROLE = "payment"
ONTOLOGY_ROLE = "ontology"
ANALYTICS_ROLE = "analytics"

KNOWN_ROLES: frozenset[str] = frozenset(
    {
        CUSTOMER_ROLE,
        SUPPORT_ROLE,
        HUMAN_AGENT_ROLE,
        SUPERVISOR_ROLE,
        OPERATIONS_ROLE,
        ADMIN_ROLE,
        WAREHOUSE_ROLE,
        PAYMENT_ROLE,
        ONTOLOGY_ROLE,
        ANALYTICS_ROLE,
    }
)

# 一期全部 scope：13 个 MCP 工具 scope（data/contracts/mcp-tools.json 唯一来源）
# 加 7 个工作台 scope（27.2 接口表）；客户端声明的任何其他 scope 都拒绝。
KNOWN_SCOPES: frozenset[str] = frozenset(
    {
        "order:read",
        "payment:read",
        "logistics:read",
        "knowledge:read",
        "refund:read",
        "refund:request",
        "return:read",
        "ticket:create",
        "ticket:read",
        "ticket:update",
        "order:address:update",
        "analytics:read",
        "ontology:manage",
        "incident:read",
        "incident:manage",
        "customer:read",
        "ticket:assign",
        "ticket:status",
        "handoff:read",
        "handoff:accept",
        "refund:review",
        "refund:execute",
        "audit:read",
        "warehouse:receive",
        "warehouse:inspect",
    }
)

# 系统级客户范围标记：仅 operations/admin 允许；不能与其他客户 ID 混用。
SYSTEM_ALL_CUSTOMERS = "*"

GENERIC_DENIED_MESSAGE = "资源不存在或不在授权范围内"


class AuthorizationDenied(Exception):
    """授权失败：携带公共错误码，由接口层映射 HTTP 状态与响应信封。"""

    def __init__(self, error_code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.retryable = False

    @property
    def http_status(self) -> int:
        return http_status_for(self.error_code.value)


class MockLoginDisabled(AuthorizationDenied):
    """Mock 登录入口在当前环境关闭；按关闭入口的统一语义返回 404。"""

    def __init__(self, message: str = "Mock 登录在当前环境已关闭") -> None:
        super().__init__(ErrorCode.RESOURCE_UNAVAILABLE, message)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_valid_identity(auth: AuthContext, now: datetime) -> None:
    if not auth.is_valid_at(now):
        raise AuthorizationDenied(ErrorCode.UNAUTHENTICATED, "身份无效或已过期")


def require_tenant(auth: AuthContext, tenant_id: str, *, now: datetime | None = None) -> None:
    """跨租户访问按统一安全语义拒绝，不泄露其他租户资源是否存在。"""
    _require_valid_identity(auth, now or utc_now())
    if auth.tenant_id != tenant_id:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)


def require_scope(auth: AuthContext, scope: str, *, now: datetime | None = None) -> None:
    _require_valid_identity(auth, now or utc_now())
    if scope not in auth.scopes:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)


def require_all_scopes(
    auth: AuthContext, scopes: tuple[str, ...] | list[str], *, now: datetime | None = None
) -> None:
    _require_valid_identity(auth, now or utc_now())
    missing = set(scopes) - set(auth.scopes)
    if missing:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)


def accessible_customers(auth: AuthContext) -> tuple[str, ...]:
    """返回授权客户清单；"*" 表示租户内系统级范围（仅 operations/admin）。"""
    return tuple(auth.data_scope.customer_ids)


def has_customer_access(auth: AuthContext, customer_id: str) -> bool:
    allowed = auth.data_scope.customer_ids
    if SYSTEM_ALL_CUSTOMERS in allowed:
        return True
    return customer_id in allowed


def require_customer_access(
    auth: AuthContext,
    customer_id: str,
    *,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """客户本人访问、客服代客范围的统一定点；越权与不存在不区分。"""
    _require_valid_identity(auth, now or utc_now())
    if tenant_id is not None and auth.tenant_id != tenant_id:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
    if not has_customer_access(auth, customer_id):
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)


def verify_tool_context(
    auth: AuthContext,
    required_scopes: tuple[str, ...] | list[str],
    *,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """MCP 服务端对每次工具调用的二次授权（16.1）：有效期、租户、scope 全部重查。

    即使连接来自可信 API 进程，也不跳过最终用户授权；身份与 scope 以本次
    调用携带的可信 AuthContext 为准，不接受模型或客户端转述。
    """
    _require_valid_identity(auth, now or utc_now())
    if tenant_id is not None and auth.tenant_id != tenant_id:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)
    missing = set(required_scopes) - set(auth.scopes)
    if missing:
        raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, GENERIC_DENIED_MESSAGE)


def identity_summary(auth: AuthContext) -> dict:
    """GET /me 的授权范围摘要：只描述已授予内容，不含可改写的身份对象。"""
    return {
        "tenant_id": auth.tenant_id,
        "subject_id": auth.subject_id,
        "roles": list(auth.roles),
        "scopes": list(auth.scopes),
        "data_scope": {
            "team_ids": list(auth.data_scope.team_ids),
            "customer_ids": list(auth.data_scope.customer_ids),
        },
        "session_expires_at": auth.expires_at.isoformat(),
    }
