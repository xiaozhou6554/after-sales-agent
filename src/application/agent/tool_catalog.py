"""受限路由白名单：以 mcp-tools.json（唯一源）按授权 scope 过滤模型可见工具。

Supervisor 只能在白名单内选择工具（技术详细规格 16.1/749 行）；白名单外
的工具对模型不可见，模型仍点名的调用由守卫拒绝且不执行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from application.agent.ports.llm import ToolSpec
from application.common.context import AuthContext


@dataclass(frozen=True)
class ToolCatalog:
    """按主体授权过滤后的工具目录；entries 保留注册表原始条目。"""

    specs: tuple[ToolSpec, ...]
    entries: dict[str, dict]

    @classmethod
    def load(
        cls,
        registry_path: str | Path,
        auth: AuthContext,
        *,
        registered_names: frozenset[str] | None = None,
    ) -> "ToolCatalog":
        with open(registry_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        tools = manifest["tools"] if isinstance(manifest, dict) else manifest
        specs: list[ToolSpec] = []
        entries: dict[str, dict] = {}
        for tool in tools:
            name = tool["name"]
            if registered_names is not None and name not in registered_names:
                continue  # 契约预留但运行时未注册的工具不可见
            scopes = tuple(tool.get("auth_scopes", ()))
            if any(scope not in auth.scopes for scope in scopes):
                continue  # 未授权工具不进入模型可见清单
            if tool.get("access") != "read":
                continue  # 一期写操作走受控服务端流程，不作为模型可选工具
            entries[name] = tool
            specs.append(
                ToolSpec(
                    name=name,
                    description=tool.get("description") or name,
                    parameters=tool.get("input_schema") or {"type": "object"},
                )
            )
        return cls(specs=tuple(specs), entries=entries)

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in self.entries

    def access_of(self, tool_name: str) -> str:
        return self.entries[tool_name].get("access", "read")
