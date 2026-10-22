"""知识查询入口只接受服务端解析的、完整激活的发布版本。"""

from typing import Protocol


class ActiveKnowledgeReleasePort(Protocol):
    def get_active_release_id(self, tenant_id: str) -> str | None: ...
