"""MCP 逐次调用上下文守卫（技术详细规格 16.1、27.2）。

即使连接来自可信 API 进程，每个工具调用都必须重新验证最终用户身份：
AuthContext 由传输层单独携带，不进入模型参数或工具 arguments；
所需 scope 以 data/contracts/mcp-tools.json（唯一源）的 auth_scopes 为准，
由 application.common.authorization.verify_tool_context 统一判定。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from application.common.authorization import AuthorizationDenied, verify_tool_context
from application.common.context import AuthContext
from application.common.errors import ErrorCode


class ToolContextGuard:
    """绑定工具清单的上下文守卫；MCP 工具处理器在每次调用前执行 verify。"""

    def __init__(self, manifest_path: str | Path) -> None:
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        tools = manifest["tools"] if isinstance(manifest, dict) else manifest
        self._tools: dict[str, dict] = {tool["name"]: tool for tool in tools}
        self._scopes: dict[str, tuple[str, ...]] = {
            name: tuple(tool.get("auth_scopes", ())) for name, tool in self._tools.items()
        }

    def required_scopes(self, tool_name: str) -> tuple[str, ...]:
        scopes = self._scopes.get(tool_name)
        if scopes is None:
            # 未知工具不区分"未注册"与"无权"，统一安全语义。
            raise AuthorizationDenied(ErrorCode.RESOURCE_UNAVAILABLE, "工具不可用")
        return scopes

    def validate_arguments(self, tool_name: str, arguments: dict) -> None:
        """按注册表 input_schema（唯一源）校验模型可见参数；不合法即拒绝。

        覆盖本注册表实际使用的关键字：type/required/properties/
        additionalProperties/minLength/enum。
        """
        schema = self._tools.get(tool_name, {}).get("input_schema") or {}
        errors = _schema_errors(schema, arguments, "")
        if errors:
            raise AuthorizationDenied(
                ErrorCode.INVALID_ARGUMENT, f"工具参数不合法: {'; '.join(errors[:3])}"
            )

    def verify(
        self,
        tool_name: str,
        auth: AuthContext,
        *,
        tenant_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        verify_tool_context(
            auth,
            self.required_scopes(tool_name),
            tenant_id=tenant_id,
            now=now,
        )


def _schema_errors(schema: dict, value, path: str) -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path or 'arguments'} 必须为对象"]
        for name in schema.get("required", ()):
            if name not in value:
                errors.append(f"缺少参数 {path}{name}")
        alternatives = schema.get("anyOf", ())
        if alternatives and not any(
            all(name in value for name in option.get("required", ())) for option in alternatives
        ):
            errors.append(f"{path or 'arguments'} 缺少必要的参数组合")
        exclusive = schema.get("oneOf", ())
        if (
            exclusive
            and sum(
                all(name in value for name in option.get("required", ())) for option in exclusive
            )
            != 1
        ):
            errors.append(f"{path or 'arguments'} 必须且只能提供一种参数组合")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"未知参数 {path}{name}")
        for name, item in value.items():
            sub = properties.get(name)
            if isinstance(sub, dict):
                errors.extend(_schema_errors(sub, item, f"{path}{name}."))
        return errors
    if expected == "string":
        if not isinstance(value, str):
            return [f"{path or '值'} 必须为字符串"]
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < min_length:
            errors.append(f"{path or '值'} 长度不得小于 {min_length}")
        max_length = schema.get("maxLength")
        if max_length is not None and len(value) > max_length:
            errors.append(f"{path or '值'} 长度不得大于 {max_length}")
    if expected == "array":
        if not isinstance(value, list):
            return [f"{path or '值'} 必须为数组"]
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path or '值'} 数量不足")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path or '值'} 数量过多")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item_schema, item, f"{path}{index}."))
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path or '值'} 不在允许范围内")
    return errors
