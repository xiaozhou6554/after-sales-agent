"""步骤24基础：真实 MySQL 中验证绑定确认、一次性消费和幂等恢复。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from bootstrap.settings import load_settings

from application.common.context import OperationContext
from application.common.operation_confirmation import OperationConfirmationUseCase, content_digest
from application.common.ports.confirmation import ConfirmationConflictError
from application.common.ports.idempotency import IdempotencyConflictError
from infrastructure.messaging.idempotency_store import MySQLIdempotencyStore
from infrastructure.persistence.mysql.connection import create_mysql_engine
from infrastructure.security.confirmation_store import MySQLConfirmationStore


@pytest.fixture
def stores():
    settings = load_settings()
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield (
            MySQLConfirmationStore(engine).bind(connection),
            MySQLIdempotencyStore(engine).bind(connection),
        )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def _binding():
    return OperationContext(
        tenant_id="tenant-north",
        actor_subject_id="CUSTOMER-01",
        customer_id="CUSTOMER-01",
        confirmer_subject_id="CUSTOMER-01",
        operation_type="refund_request",
        target_id="ORDER-001",
        payload_digest=content_digest({"order_id": "ORDER-001", "reason": "quality"}),
        expected_revision=3,
    )


def test_confirmation_is_bound_and_single_use(stores) -> None:
    confirmations, _ = stores
    use_case = OperationConfirmationUseCase(confirmations, 300)
    binding = _binding()
    prepared = use_case.prepare(binding)
    now = datetime.now(timezone.utc)
    with pytest.raises(ConfirmationConflictError):
        confirmations.consume(prepared.record.confirmation_id, binding, "op-1", now)
    with pytest.raises(ConfirmationConflictError):
        use_case.verify(binding.tenant_id, prepared.record.confirmation_id, "wrong")
    verified = use_case.verify(
        binding.tenant_id, prepared.record.confirmation_id, prepared.mock_challenge
    )
    assert verified.status == "verified"
    with pytest.raises(ConfirmationConflictError):
        confirmations.consume(
            prepared.record.confirmation_id, replace(binding, expected_revision=4), "op-1", now
        )
    consumed = confirmations.consume(prepared.record.confirmation_id, binding, "op-1", now)
    assert consumed.status == "consumed"
    with pytest.raises(ConfirmationConflictError):
        confirmations.consume(prepared.record.confirmation_id, binding, "op-2", now)
    saved = confirmations.get(binding.tenant_id, prepared.record.confirmation_id)
    assert saved is not None and saved.status == "consumed"


def test_expired_confirmation_rejected(stores) -> None:
    confirmations, _ = stores
    binding = _binding()
    prepared = OperationConfirmationUseCase(confirmations, 1).prepare(binding)
    from hashlib import sha256

    with pytest.raises(ConfirmationConflictError):
        confirmations.verify(
            binding.tenant_id,
            prepared.record.confirmation_id,
            sha256(prepared.mock_challenge.encode()).hexdigest(),
            prepared.record.expires_at + timedelta(seconds=1),
        )


def test_idempotency_same_request_reuses_result_and_conflict_fails(stores) -> None:
    _, idempotency = stores
    binding = _binding()
    args = (binding.tenant_id, binding.actor_subject_id, binding.operation_type, "key-1")
    first = idempotency.reserve(*args, binding.payload_digest)
    assert idempotency.complete(first, "succeeded", "refund-1")
    repeated = idempotency.reserve(*args, binding.payload_digest)
    assert repeated.record_id == first.record_id
    assert repeated.status == "succeeded" and repeated.result_ref == "refund-1"
    assert not idempotency.complete(first, "succeeded", "refund-2")
    with pytest.raises(IdempotencyConflictError):
        idempotency.reserve(*args, "f" * 64)
