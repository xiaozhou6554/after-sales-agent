"""本体发布清单值对象：不可变 manifest + SHA-256 摘要（FR-20 发布契约）。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime


def canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReleaseManifest:
    release_id: str
    tenant_id: str
    schema_version: str
    mapping_version: int
    metric_version: int
    files: dict[str, str]
    effective_at: datetime | None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = {
            "release_id": self.release_id,
            "tenant_id": self.tenant_id,
            "schema_version": self.schema_version,
            "mapping_version": self.mapping_version,
            "metric_version": self.metric_version,
            "files": dict(sorted(self.files.items())),
            "effective_at": self.effective_at.isoformat() if self.effective_at else None,
        }
        payload.update(self.extra)
        return payload

    def digest(self) -> str:
        return manifest_digest(self.to_dict())

    def verify_digest(self, expected: str) -> bool:
        return expected == self.digest()
