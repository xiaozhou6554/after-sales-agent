"""受控内容摘要和一次性验证用例。"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from application.common.context import OperationContext
from application.common.ports.confirmation import ConfirmationRecord, ConfirmationStorePort


def content_digest(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _challenge_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedConfirmation:
    record: ConfirmationRecord
    mock_challenge: str


class OperationConfirmationUseCase:
    def __init__(self, store: ConfirmationStorePort, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            raise ValueError("确认有效期必须大于零")
        self._store = store
        self._ttl = timedelta(seconds=ttl_seconds)

    def prepare(self, binding: OperationContext) -> PreparedConfirmation:
        challenge = secrets.token_urlsafe(24)
        record = self._store.issue(
            binding, _challenge_digest(challenge), datetime.now(timezone.utc) + self._ttl
        )
        return PreparedConfirmation(record, challenge)

    def verify(self, tenant_id: str, confirmation_id: str, challenge: str) -> ConfirmationRecord:
        return self._store.verify(
            tenant_id, confirmation_id, _challenge_digest(challenge), datetime.now(timezone.utc)
        )

    def get(self, tenant_id: str, confirmation_id: str) -> ConfirmationRecord | None:
        return self._store.get(tenant_id, confirmation_id)
