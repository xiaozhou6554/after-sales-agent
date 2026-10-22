"""MockAuthAdapter：本地 Mock 认证（技术详细规格 16.1、27.2、27.3）。

- 上下文结构与生产一致（不可变 AuthContext），仅签发方式不同；
- 只接受后端预置 account_id，角色/scope/授权范围全部来自受控 fixture，
  客户端不能指定任意角色或 scope；
- Mock 登录仅开发/测试启用；生产由 bootstrap 关闭路由且本适配器拒绝签发；
- 会话令牌为自包含 HMAC 签名载荷（account_id + 会话 id + 有效期），身份字段
  一律在 resolve 时从受控 fixture 重建，令牌本身不携带角色/scope；
  签名密钥由 fixture 内容派生（仅 Mock 用途，非生产凭据），因此同一 fixture
  下多个进程（API 网关与 MCP 服务）可以互认彼此签发的登录态；
- 生产接入时替换为企业 IdP 适配器，本适配器整体下线。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from application.common.authorization import (
    ADMIN_ROLE,
    KNOWN_ROLES,
    KNOWN_SCOPES,
    OPERATIONS_ROLE,
    SYSTEM_ALL_CUSTOMERS,
    AuthorizationDenied,
    MockLoginDisabled,
)
from application.common.context import AuthContext, DataScope
from application.common.errors import ErrorCode
from application.common.ports.auth import MockSessionGrant

SESSION_TTL_DEFAULT_S = 3600.0

_ACCOUNT_FIELDS = frozenset(
    {"tenant_id", "account_id", "subject_id", "role", "scopes", "team_ids", "allowed_customer_ids"}
)
_TOP_FIELDS = frozenset({"schema_version", "fixture_version", "synthetic_only", "accounts", "note"})


@dataclass(frozen=True)
class MockIdentityRecord:
    """27.3 受控 fixture 的映射字段；授权范围只来自这里。"""

    tenant_id: str
    account_id: str
    subject_id: str
    role: str
    scopes: tuple[str, ...]
    team_ids: tuple[str, ...]
    allowed_customer_ids: tuple[str, ...]


def _require_non_empty_str(raw: dict, key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} 必须为非空字符串")
    return value


def _require_str_list(raw: dict, key: str, label: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{label}.{key} 必须为非空字符串数组")
    if len(set(value)) != len(value):
        raise ValueError(f"{label}.{key} 存在重复项")
    return tuple(value)


def parse_mock_identities(raw: object) -> dict[str, MockIdentityRecord]:
    """严格解析身份 fixture；任何未知字段、未知角色/scope、非法范围都拒绝。"""
    if not isinstance(raw, dict):
        raise ValueError("身份 fixture 必须为对象")
    unknown_top = set(raw) - _TOP_FIELDS
    if unknown_top:
        raise ValueError(f"身份 fixture 存在未知字段: {sorted(unknown_top)}")
    if raw.get("schema_version") != "1.0":
        raise ValueError("身份 fixture schema_version 必须为 '1.0'")
    accounts = raw.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("身份 fixture accounts 必须为非空数组")

    records: dict[str, MockIdentityRecord] = {}
    subjects: set[str] = set()
    for item in accounts:
        if not isinstance(item, dict):
            raise ValueError("accounts 元素必须为对象")
        unknown = set(item) - _ACCOUNT_FIELDS
        if unknown:
            raise ValueError(f"账户存在未知字段: {sorted(unknown)}")
        account_id = _require_non_empty_str(item, "account_id", "账户")
        label = f"账户 {account_id}"
        if account_id in records:
            raise ValueError(f"账户 account_id 重复: {account_id}")
        tenant_id = _require_non_empty_str(item, "tenant_id", label)
        subject_id = _require_non_empty_str(item, "subject_id", label)
        if subject_id in subjects:
            raise ValueError(f"账户 subject_id 重复: {subject_id}")
        subjects.add(subject_id)
        role = _require_non_empty_str(item, "role", label)
        if role not in KNOWN_ROLES:
            raise ValueError(f"{label} 未知角色: {role!r}")
        scopes = _require_str_list(item, "scopes", label)
        if not scopes:
            raise ValueError(f"{label} scopes 不能为空")
        unknown_scopes = set(scopes) - KNOWN_SCOPES
        if unknown_scopes:
            raise ValueError(f"{label} 存在未注册 scope: {sorted(unknown_scopes)}")
        team_ids = _require_str_list(item, "team_ids", label)
        allowed = _require_str_list(item, "allowed_customer_ids", label)
        if role == "customer":
            if team_ids:
                raise ValueError(f"{label} 为 customer 角色但携带团队范围")
            if len(allowed) != 1 or allowed[0] == SYSTEM_ALL_CUSTOMERS:
                raise ValueError(f"{label} 为 customer 角色但本人客户范围不唯一")
        if SYSTEM_ALL_CUSTOMERS in allowed:
            if role not in (OPERATIONS_ROLE, ADMIN_ROLE):
                raise ValueError(f"{label} 不允许使用系统范围标记 *")
            if len(allowed) != 1:
                raise ValueError(f"{label} 系统范围标记 * 不能与其他客户混用")
        records[account_id] = MockIdentityRecord(
            tenant_id=tenant_id,
            account_id=account_id,
            subject_id=subject_id,
            role=role,
            scopes=scopes,
            team_ids=team_ids,
            allowed_customer_ids=allowed,
        )
    return records


def load_mock_identities(path: str | Path) -> dict[str, MockIdentityRecord]:
    with open(path, encoding="utf-8") as handle:
        return parse_mock_identities(json.load(handle))


_TOKEN_PREFIX = "mock-v1"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive_dev_secret(records: dict[str, MockIdentityRecord]) -> bytes:
    """由 fixture 内容派生 Mock 会话签名密钥（跨进程一致，仅 Mock 用途）。"""
    canonical = json.dumps(
        {account_id: vars(record) for account_id, record in records.items()},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    seeded = f"mock-session-v1:{canonical}".encode("utf-8")
    return hashlib.sha256(seeded).digest()


def _encode_session_token(secret: bytes, payload: dict) -> str:
    body = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{_TOKEN_PREFIX}.{body}.{signature}"


def _decode_session_token(secret: bytes, token: str) -> dict | None:
    """校验签名并解析载荷；格式/签名/结构任一不符返回 None（统一未认证语义）。"""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        return None
    body, signature = parts[1], parts[2]
    expected = _b64encode(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


class MockAuthAdapter:
    """进程内 Mock 登录态；resolve 面向网关中间件，逐次校验有效期。"""

    def __init__(
        self,
        records: dict[str, MockIdentityRecord],
        *,
        enabled: bool,
        session_ttl_s: float = SESSION_TTL_DEFAULT_S,
        clock: Callable[[], datetime] | None = None,
        token_secret: str | None = None,
    ) -> None:
        self._records = records
        self._enabled = enabled
        self._session_ttl_s = session_ttl_s
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sessions: dict[str, AuthContext] = {}
        self._secret = token_secret.encode("utf-8") if token_secret else _derive_dev_secret(records)

    @classmethod
    def from_fixture(
        cls,
        fixture_path: str | Path,
        *,
        enabled: bool,
        session_ttl_s: float = SESSION_TTL_DEFAULT_S,
        clock: Callable[[], datetime] | None = None,
        token_secret: str | None = None,
    ) -> "MockAuthAdapter":
        return cls(
            load_mock_identities(fixture_path),
            enabled=enabled,
            session_ttl_s=session_ttl_s,
            clock=clock,
            token_secret=token_secret,
        )

    def mock_login_enabled(self) -> bool:
        return self._enabled

    def issue_task_session(
        self, tenant_id: str, subject_id: str, customer_id: str
    ) -> MockSessionGrant:
        """从受控账户重新签发后台会话，并核对任务的完整授权范围。"""
        if not self._enabled:
            raise MockLoginDisabled("生产环境未配置后台服务身份")
        record = next(
            (
                item
                for item in self._records.values()
                if item.tenant_id == tenant_id and item.subject_id == subject_id
            ),
            None,
        )
        if record is None or (
            customer_id not in record.allowed_customer_ids
            and "*" not in record.allowed_customer_ids
        ):
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "后台任务身份或客户范围失效")
        return self.issue_mock_session(record.account_id)

    def issue_mock_session(self, account_id: str) -> MockSessionGrant:
        if not self._enabled:
            raise MockLoginDisabled("Mock 登录在当前环境已关闭")
        record = self._records.get(account_id)
        if record is None:
            # 登录失败与参数错误一致：预置账户之外一律拒绝，不签发降级身份。
            raise AuthorizationDenied(ErrorCode.INVALID_ARGUMENT, "account_id 不在预置账户列表")
        now = self._clock()
        session_id = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=self._session_ttl_s)
        token = _encode_session_token(
            self._secret,
            {
                "account_id": record.account_id,
                "sid": session_id,
                "iat": now.timestamp(),
                "exp": expires_at.timestamp(),
            },
        )
        context = AuthContext(
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            roles=(record.role,),
            scopes=record.scopes,
            data_scope=DataScope(
                customer_ids=record.allowed_customer_ids,
                team_ids=record.team_ids,
            ),
            session_id=session_id,
            issued_at=now,
            expires_at=expires_at,
            verification_ref=f"mock:{record.account_id}",
        )
        self._sessions[token] = context
        return MockSessionGrant(session_token=token, auth=context)

    def resolve(self, session_token: str) -> AuthContext:
        now = self._clock()
        context = self._sessions.get(session_token)
        if context is None:
            # 本进程未签发（如另一进程签发）：验签后从 fixture 重建身份。
            context = self._resolve_signed(session_token)
        if context is None or not context.is_valid_at(now):
            # 不区分"令牌不存在"、"签名不符"与"已过期"，统一未认证语义。
            raise AuthorizationDenied(ErrorCode.UNAUTHENTICATED, "会话不存在或已过期")
        return context

    def _resolve_signed(self, session_token: str) -> AuthContext | None:
        payload = _decode_session_token(self._secret, session_token)
        if payload is None:
            return None
        record = self._records.get(payload.get("account_id", ""))
        if record is None:
            return None  # 账户已从 fixture 移除：登录态随之失效
        try:
            session_id = str(payload["sid"])
            issued_at = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)
            expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return None
        return AuthContext(
            tenant_id=record.tenant_id,
            subject_id=record.subject_id,
            roles=(record.role,),
            scopes=record.scopes,
            data_scope=DataScope(
                customer_ids=record.allowed_customer_ids,
                team_ids=record.team_ids,
            ),
            session_id=session_id,
            issued_at=issued_at,
            expires_at=expires_at,
            verification_ref=f"mock:{record.account_id}",
        )
