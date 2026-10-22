"""知识索引任务租约的技术无关标识。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgeIndexLease:
    tenant_id: str
    knowledge_release_id: str
    backend: str
    manifest_digest: str
    worker_id: str
    revision: int
