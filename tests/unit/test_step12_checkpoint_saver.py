"""步骤12 单元测试：Checkpoint 端口语义、归并规则、脱敏编码与冲突判定。

不依赖数据库：编解码/归并/作用域校验为纯逻辑；MySQL 行为由集成契约测试覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.agent.ports.checkpoint import (
    CheckpointConflictError,
    CheckpointPersistenceError,
    CheckpointRepositoryPort,
    CheckpointSchemaError,
    CheckpointUnavailableError,
)
from infrastructure.agent.mysql_checkpoint_saver import (
    AGENT_STATE_SCHEMA_VERSION,
    MySQLCheckpointSaver,
)


def make_saver() -> MySQLCheckpointSaver:
    return MySQLCheckpointSaver(None)  # 纯逻辑测试不触库


# ---------- 错误层级与端口形态 ----------


def test_error_hierarchy_transport_vs_semantic() -> None:
    assert issubclass(CheckpointUnavailableError, CheckpointPersistenceError)
    assert issubclass(CheckpointConflictError, CheckpointPersistenceError)
    assert issubclass(CheckpointSchemaError, CheckpointPersistenceError)
    # 传输失败与语义冲突分属不同分支，供降级/收束逻辑区分。
    assert not issubclass(CheckpointUnavailableError, CheckpointConflictError)


def test_saver_implements_langgraph_base_and_port() -> None:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    saver = make_saver()
    assert isinstance(saver, BaseCheckpointSaver)
    # 端口方法齐备；基类无租户 delete_thread 拒绝执行。
    assert callable(saver.latest)
    assert callable(saver.delete_checkpoints)
    with pytest.raises(ValueError, match="tenant_id"):
        saver.delete_thread("thread-1")


# ---------- 联合恢复键作用域 ----------


def test_scope_requires_tenant_and_thread() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        MySQLCheckpointSaver._scope({"configurable": {"thread_id": "t"}})
    with pytest.raises(ValueError, match="thread_id"):
        MySQLCheckpointSaver._scope({"configurable": {"tenant_id": "tenant-a"}})
    scope = MySQLCheckpointSaver._scope(
        {"configurable": {"tenant_id": "tenant-a", "thread_id": "t1"}}
    )
    assert scope == ("tenant-a", "t1", "")


# ---------- state 序列化脱敏与往返 ----------


def test_encode_masks_sensitive_content_and_roundtrips() -> None:
    saver = make_saver()
    checkpoint = {
        "v": 1,
        "id": "cp-1",
        "ts": "2026-10-20T00:00:00+00:00",
        "channel_values": {"messages": ["联系方式 13812345678"]},
        "channel_versions": {"messages": "1"},
        "versions_seen": {"supervisor": {"messages": "1"}},
        "updated_channels": ["messages"],
    }
    column = saver._encode(checkpoint)
    # 库内文本不含明文手机号（写入前脱敏，规格 543 行）。
    assert "13812345678" not in column
    restored = saver._decode_wrapped(column)
    assert restored["channel_values"]["messages"] == ["联系方式 ***"]
    assert restored["id"] == "cp-1"
    assert restored["versions_seen"] == {"supervisor": {"messages": "1"}}


def test_current_node_from_metadata_writes() -> None:
    assert MySQLCheckpointSaver._current_node({"writes": {"tool": {"x": 1}}}) == "tool"
    assert MySQLCheckpointSaver._current_node({"writes": {}}) is None
    assert MySQLCheckpointSaver._current_node(None) is None


# ---------- 同 ID 幂等/冲突判定 ----------


def test_assert_idempotent_allows_identical_content() -> None:
    row = SimpleNamespace(
        state='{"a": 1}', metadata='{"source": "loop"}', parent_checkpoint_id="p1"
    )
    MySQLCheckpointSaver._assert_idempotent(row, "p1", '{"a":1}', '{"source":"loop"}')


def test_assert_idempotent_conflicts_on_content_diff_or_parent_diff() -> None:
    row = SimpleNamespace(
        state='{"a": 1}', metadata='{"source": "loop"}', parent_checkpoint_id="p1"
    )
    with pytest.raises(CheckpointConflictError, match="拒绝覆盖"):
        MySQLCheckpointSaver._assert_idempotent(row, "p1", '{"a": 2}', '{"source":"loop"}')
    with pytest.raises(CheckpointConflictError, match="拒绝覆盖"):
        MySQLCheckpointSaver._assert_idempotent(row, "pX", '{"a": 1}', '{"source":"loop"}')


# ---------- pending_writes 归并 ----------


def test_merge_pending_writes_regular_first_write_wins() -> None:
    existing = [{"task_id": "t1", "task_path": "", "channel": "messages", "value": "旧"}]
    incoming = [{"task_id": "t1", "task_path": "", "channel": "messages", "value": "新"}]
    merged = MySQLCheckpointSaver._merge_pending_writes(existing, incoming)
    assert merged == [{"task_id": "t1", "task_path": "", "channel": "messages", "value": "旧"}]


def test_merge_pending_writes_special_channel_last_write_wins() -> None:
    existing = [{"task_id": "", "task_path": "", "channel": "__interrupt__", "value": {"i": 1}}]
    incoming = [{"task_id": "", "task_path": "", "channel": "__interrupt__", "value": {"i": 2}}]
    merged = MySQLCheckpointSaver._merge_pending_writes(existing, incoming)
    assert merged == [
        {"task_id": "", "task_path": "", "channel": "__interrupt__", "value": {"i": 2}}
    ]


def test_merge_pending_writes_new_task_appended_and_sorted() -> None:
    existing = [{"task_id": "t2", "task_path": "p:1", "channel": "messages", "value": "b"}]
    incoming = [{"task_id": "t1", "task_path": "", "channel": "observations", "value": "a"}]
    merged = MySQLCheckpointSaver._merge_pending_writes(existing, incoming)
    assert [e["task_id"] for e in merged] == ["t1", "t2"]


def test_pending_from_column_orders_and_returns_tuples() -> None:
    column = (
        '[{"task_id": "t2", "task_path": "p", "channel": "messages", "value": "b"},'
        ' {"task_id": "t1", "task_path": "p", "channel": "observations", "value": "a"}]'
    )
    pending = MySQLCheckpointSaver._pending_from_column(column)
    assert pending == [("t1", "observations", "a"), ("t2", "messages", "b")]
    assert MySQLCheckpointSaver._pending_from_column(None) == []
    assert MySQLCheckpointSaver._pending_from_column("[]") == []


# ---------- State 结构版本 ----------


def test_state_schema_version_is_pinned() -> None:
    assert AGENT_STATE_SCHEMA_VERSION == "1"


def test_is_placeholder_detects_placeholder_not_real_rows() -> None:
    # 占位行承接先到的写入，但不得被误判为真实 Checkpoint，反之亦然。
    assert MySQLCheckpointSaver._is_placeholder('{"placeholder": true}')
    assert not MySQLCheckpointSaver._is_placeholder(
        '{"serde_type": "msgpack", "payload_b64": "abc"}'
    )
    assert not MySQLCheckpointSaver._is_placeholder("not-json")
    assert not MySQLCheckpointSaver._is_placeholder(None)
    # 只含部分键的行不算占位（真实行恒为 serde 包装，占位行键唯一）。
    assert not MySQLCheckpointSaver._is_placeholder('{"placeholder": false}')


def test_port_protocol_method_names_unchanged() -> None:
    # 技术规格 1039 行命名：端口协议方法保持稳定，防止装配侧静默失配。
    assert hasattr(CheckpointRepositoryPort, "latest")
    assert hasattr(CheckpointRepositoryPort, "delete_checkpoints")
