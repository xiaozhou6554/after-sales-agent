"""索引 Worker：固定清单，构建后读回验证，再提交任务结果。"""

from __future__ import annotations

import hashlib
from typing import Protocol

from .models import KnowledgeIndexLease


class IndexJobPort(Protocol):
    def claim(
        self,
        tenant_id: str,
        knowledge_release_id: str,
        backend: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
    ) -> KnowledgeIndexLease | None: ...

    def complete(
        self,
        lease: KnowledgeIndexLease,
        *,
        verified_manifest_digest: str | None,
    ) -> str: ...


class ManifestPort(Protocol):
    def get_manifest(self, tenant_id: str, knowledge_release_id: str) -> bytes: ...


class BackendIndexPort(Protocol):
    def build(self, lease: KnowledgeIndexLease, manifest: bytes) -> None: ...

    def verify(self, lease: KnowledgeIndexLease, manifest: bytes) -> str: ...


class KnowledgeIndexWorker:
    def __init__(
        self,
        jobs: IndexJobPort,
        manifests: ManifestPort,
        backends: dict[str, BackendIndexPort],
    ) -> None:
        self._jobs = jobs
        self._manifests = manifests
        self._backends = backends

    def run_once(
        self,
        tenant_id: str,
        knowledge_release_id: str,
        backend: str,
        worker_id: str,
    ) -> str:
        if backend not in self._backends:
            raise ValueError("索引后端未配置")
        lease = self._jobs.claim(tenant_id, knowledge_release_id, backend, worker_id)
        if lease is None:
            return "idle"
        try:
            manifest = self._manifests.get_manifest(tenant_id, knowledge_release_id)
            if hashlib.sha256(manifest).hexdigest() != lease.manifest_digest:
                raise ValueError("持久化清单与索引任务摘要不一致")
            adapter = self._backends[backend]
            adapter.build(lease, manifest)
            verified = adapter.verify(lease, manifest)
            if verified != lease.manifest_digest:
                raise ValueError("后端读回校验未通过")
        except Exception:
            result = self._jobs.complete(lease, verified_manifest_digest=None)
            return "lease_lost" if result == "lease_lost" else "failed"
        return self._jobs.complete(lease, verified_manifest_digest=verified)
