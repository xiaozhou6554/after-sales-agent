"""认证网关端口：接口层经此取得可信 AuthContext，不感知具体认证实现。

- 生产环境由企业 IdP 适配器实现 resolve；
- issue_mock_session 仅由 MockAuthAdapter 支持且只在开发/测试启用；
  客户端只能提交预置 account_id，不能指定角色或 scope。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from application.common.context import AuthContext


@dataclass(frozen=True)
class MockSessionGrant:
    """一次 Mock 登录签发结果：不透明会话令牌 + 服务端签发的不可变上下文。"""

    session_token: str
    auth: AuthContext


class AuthGateway(Protocol):
    def mock_login_enabled(self) -> bool: ...

    def issue_mock_session(self, account_id: str) -> MockSessionGrant: ...

    def resolve(self, session_token: str) -> AuthContext: ...
