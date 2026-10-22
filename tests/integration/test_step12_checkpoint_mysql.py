"""步骤12 MySQL Checkpoint 契约测试：真实库上的恢复键、版本链与并发语义。

需要本地 docker MySQL（开发环境已启动）；测试只使用 tenant-step12 隔离数据，
结束后清理本测试写入的行。验收口径：重复保存不覆盖、冲突不静默丢失、
未提交节点写入可恢复、跨租户不串数据（流程步骤12 / 规格技术详细规格 11.1）。
"""

from __future__ import annotations

import json

import pytest
from bootstrap.migrate import build_executor, run_apply
from bootstrap.settings import load_settings
from langgraph.checkpoint.base import uuid6
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from application.agent.ports.checkpoint import (
    CheckpointConflictError,
    CheckpointSchemaError,
    CheckpointUnavailableError,
)
from infrastructure.agent.mysql_checkpoint_saver import MySQLCheckpointSaver
from infrastructure.persistence.mysql.connection import create_mysql_engine

TEST_TENANT = "tenant-step12"


def make_checkpoint(cp_id: str, *, message: str = "你好 13812345678") -> dict:
    return {
        "v": 1,
        "id": cp_id,
        "ts": "2026-10-20T00:00:00+00:00",
        "channel_values": {"messages": [message]},
        "channel_versions": {"messages": "1", "__start__": "1"},
        "versions_seen": {"supervisor": {"__start__": "1"}},
        "updated_channels": ["messages"],
    }


def cfg(tenant: str = TEST_TENANT, thread: str = "thread-c1", **extra) -> dict:
    return {"configurable": {"tenant_id": tenant, "thread_id": thread, **extra}}


@pytest.fixture()
def checkpoint_case():
    settings = load_settings()
    run_apply(build_executor(settings))  # 显式迁移：幂等，未应用则应用 v0004
    engine = create_mysql_engine(
        settings.mysql_url, settings.mysql_connect_timeout_s, settings.mysql_read_timeout_s
    )
    saver = MySQLCheckpointSaver(engine)
    yield saver, engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_checkpoint WHERE tenant_id LIKE 'tenant-step12%'"))
    engine.dispose()


def _row_count(engine, tenant: str = TEST_TENANT) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM agent_checkpoint WHERE tenant_id = :t"),
            {"t": tenant},
        ).scalar_one()


# ---------- put / get_tuple 基础契约 ----------


def test_put_and_latest_roundtrip_masks_state(checkpoint_case) -> None:
    saver, _ = checkpoint_case
    returned = saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    assert returned["configurable"]["checkpoint_id"]
    summary = saver.latest(TEST_TENANT, "thread-c1")
    assert summary is not None
    assert summary.revision == 0
    assert summary.parent_checkpoint_id is None
    assert summary.state_schema_version == "1"
    # 库内与读取视图均不含明文手机号（写入前脱敏，规格 543 行）。
    assert "13812345678" not in json.dumps(summary.state, ensure_ascii=False)
    assert summary.state["channel_values"]["messages"] == ["你好 ***"]


def test_revision_chain_parent_links_and_latest_ordering(checkpoint_case) -> None:
    saver, _ = checkpoint_case
    a = saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    aid = a["configurable"]["checkpoint_id"]
    b = saver.put(cfg(checkpoint_id=aid), make_checkpoint(uuid6()), {"source": "loop"}, {})
    bid = b["configurable"]["checkpoint_id"]
    saver.put(cfg(checkpoint_id=bid), make_checkpoint(uuid6()), {"source": "loop"}, {})

    latest = saver.get_tuple(cfg())
    assert latest is not None
    assert latest.checkpoint["id"] not in (aid, bid)
    assert latest.metadata["source"] == "loop"
    assert latest.parent_config["configurable"]["checkpoint_id"] == bid
    assert latest.pending_writes == []
    # 返回的 config 必须携带租户，供引擎后续 put 沿用联合恢复键。
    assert latest.config["configurable"]["tenant_id"] == TEST_TENANT

    explicit = saver.get_tuple(cfg(checkpoint_id=aid))
    assert explicit is not None
    assert explicit.checkpoint["id"] == aid
    assert explicit.parent_config is None


# ---------- 幂等与冲突（549-552 行） ----------


def test_same_id_same_content_idempotent(checkpoint_case) -> None:
    saver, engine = checkpoint_case
    cp = make_checkpoint(uuid6())
    first = saver.put(cfg(), cp, {"source": "input", "step": 0}, {})
    second = saver.put(cfg(), cp, {"source": "input", "step": 0}, {})
    assert first["configurable"]["checkpoint_id"] == second["configurable"]["checkpoint_id"]
    assert _row_count(engine) == 1


def test_same_id_different_content_conflicts_without_overwrite(checkpoint_case) -> None:
    saver, engine = checkpoint_case
    cp = make_checkpoint(uuid6())
    saver.put(cfg(), cp, {"source": "input"}, {})
    tampered = make_checkpoint(cp["id"], message="内容被篡改")
    with pytest.raises(CheckpointConflictError, match="拒绝覆盖"):
        saver.put(cfg(), tampered, {"source": "input"}, {})
    assert _row_count(engine) == 1
    summary = saver.latest(TEST_TENANT, "thread-c1")
    assert summary is not None
    assert summary.state["channel_values"]["messages"] == ["你好 ***"]


def test_missing_parent_conflicts(checkpoint_case) -> None:
    saver, engine = checkpoint_case
    with pytest.raises(CheckpointConflictError, match="父"):
        saver.put(
            cfg(checkpoint_id="nonexistent"),
            make_checkpoint(uuid6()),
            {"source": "loop"},
            {},
        )
    assert _row_count(engine) == 0


def test_fork_of_same_parent_conflicts(checkpoint_case) -> None:
    saver, engine = checkpoint_case
    a = saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    aid = a["configurable"]["checkpoint_id"]
    b = saver.put(cfg(checkpoint_id=aid), make_checkpoint(uuid6()), {"source": "loop"}, {})
    bid = b["configurable"]["checkpoint_id"]
    assert saver.latest(TEST_TENANT, "thread-c1").revision == 1
    with pytest.raises(CheckpointConflictError, match="分叉"):
        saver.put(cfg(checkpoint_id=aid), make_checkpoint(uuid6()), {"source": "loop"}, {})
    # 冲突分支不落库，最新仍是唯一子版本。
    assert saver.latest(TEST_TENANT, "thread-c1").checkpoint_id == bid
    assert _row_count(engine) == 2


# ---------- pending_writes（553-554 行） ----------


def test_pending_writes_merge_sort_and_survive_restart(checkpoint_case) -> None:
    saver, engine = checkpoint_case
    a = saver.put(cfg(), make_checkpoint(uuid6()), {"source": "loop"}, {})
    aid = a["configurable"]["checkpoint_id"]
    target = cfg(checkpoint_id=aid)
    saver.put_writes(
        target,
        [("messages", ["工具观察 13912345678"])],
        task_id="task-b",
        task_path="",
    )
    saver.put_writes(
        target,
        [("observations", {"summary": "订单已送达"})],
        task_id="task-a",
        task_path="",
    )
    # 首轮验证后新建保存器实例模拟进程重启，数据须仍可恢复。
    restarted = MySQLCheckpointSaver(engine)
    tup = restarted.get_tuple(cfg(checkpoint_id=aid))
    assert tup is not None
    assert [(tid, ch) for tid, ch, _ in tup.pending_writes] == [
        ("task-a", "observations"),
        ("task-b", "messages"),
    ]
    assert tup.pending_writes[1][2] == ["工具观察 ***"]


def test_put_writes_before_put_stages_placeholder_not_checkpoint(checkpoint_case) -> None:
    """引擎契约：写入可先于同 ID put 到达（输入写入先挂）。

    占位行承接写入但不作为 Checkpoint 呈现（规格 553 行：未完成的
    pending_writes 不得伪装成已完成 Checkpoint）。
    """
    saver, engine = checkpoint_case
    root = saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    root_id = root["configurable"]["checkpoint_id"]
    ghost = str(uuid6())
    saver.put_writes(cfg(checkpoint_id=ghost), [("messages", "先到的写入")], task_id="t1")
    # 占位行落库承接写入，但读取路径不把它呈现为 Checkpoint。
    assert _row_count(engine) == 2
    assert saver.get_tuple(cfg(checkpoint_id=ghost)) is None
    assert saver.latest(TEST_TENANT, "thread-c1").checkpoint_id == root_id
    assert all(t.checkpoint["id"] != ghost for t in saver.list(cfg()))


def test_put_after_placeholder_fills_row_and_keeps_pending_writes(checkpoint_case) -> None:
    saver, engine = checkpoint_case
    root = saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    root_id = root["configurable"]["checkpoint_id"]
    ghost = str(uuid6())
    saver.put_writes(
        cfg(checkpoint_id=ghost),
        [("messages", "输入 13912345678"), ("next_action", {"k": 1})],
        task_id="t1",
    )
    # 真实 put 到达：占位行原位补全，先到的 pending_writes 保留。
    saver.put(
        cfg(checkpoint_id=root_id),
        make_checkpoint(ghost, message="另一条 13912345678"),
        {"source": "loop"},
        {},
    )
    assert _row_count(engine) == 2
    tup = saver.get_tuple(cfg(checkpoint_id=ghost))
    assert tup is not None
    assert tup.checkpoint["id"] == ghost
    assert tup.metadata["source"] == "loop"
    assert tup.parent_config["configurable"]["checkpoint_id"] == root_id
    assert [channel for _, channel, _ in tup.pending_writes] == [
        "messages",
        "next_action",
    ]
    assert tup.pending_writes[0][2] == "输入 ***"
    # 填全后的行参与正常检索，占位行不再存在。
    assert saver.latest(TEST_TENANT, "thread-c1").checkpoint_id == ghost


# ---------- 租户隔离（532/545 行） ----------


def test_cross_tenant_same_thread_isolated(checkpoint_case) -> None:
    saver, _ = checkpoint_case
    saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    assert saver.get_tuple(cfg(tenant="tenant-step12-other")) is None
    assert saver.latest("tenant-step12-other", "thread-c1") is None
    # 原租户数据不受影响。
    assert saver.get_tuple(cfg()) is not None


# ---------- list 契约 ----------


def test_list_ordering_filter_before_limit(checkpoint_case) -> None:
    saver, _ = checkpoint_case
    a = saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    aid = a["configurable"]["checkpoint_id"]
    b = saver.put(cfg(checkpoint_id=aid), make_checkpoint(uuid6()), {"source": "loop"}, {})
    bid = b["configurable"]["checkpoint_id"]
    saver.put(cfg(checkpoint_id=bid), make_checkpoint(uuid6()), {"source": "loop"}, {})

    ids = [t.checkpoint["id"] for t in saver.list(cfg())]
    assert len(ids) == 3
    assert ids == sorted(ids, reverse=True)
    limited = [t.checkpoint["id"] for t in saver.list(cfg(), limit=2)]
    assert len(limited) == 2 and aid not in limited
    before = [t.checkpoint["id"] for t in saver.list(cfg(), before=cfg(checkpoint_id=bid))]
    assert before == [aid]
    # 与官方语义一致：config 携带 checkpoint_id 时按该 ID 精确过滤。
    addressed = [t.checkpoint["id"] for t in saver.list(cfg(checkpoint_id=aid))]
    assert addressed == [aid]
    inputs = [t.checkpoint["id"] for t in saver.list(cfg(), filter={"source": "input"})]
    assert inputs == [aid]


# ---------- Schema 版本与 fail-closed ----------


def test_incompatible_schema_version_blocks_recovery(checkpoint_case) -> None:
    saver, engine = checkpoint_case
    returned = saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_checkpoint SET state_schema_version = '999' "
                "WHERE tenant_id = :t AND checkpoint_id = :cid"
            ),
            {"t": TEST_TENANT, "cid": returned["configurable"]["checkpoint_id"]},
        )
    with pytest.raises(CheckpointSchemaError, match="不兼容"):
        saver.get_tuple(cfg())
    with pytest.raises(CheckpointSchemaError, match="不兼容"):
        saver.latest(TEST_TENANT, "thread-c1")


def test_put_without_tenant_is_rejected(checkpoint_case) -> None:
    saver, _ = checkpoint_case
    with pytest.raises(ValueError, match="tenant_id"):
        saver.put({"configurable": {"thread_id": "t"}}, make_checkpoint(uuid6()), {}, {})


def test_transport_failure_maps_to_unavailable() -> None:
    # 指向不可达端口：连接失败折算 CheckpointUnavailableError（不静默吞并）。
    broken = sa_create_engine(
        "mysql+pymysql://after_sales:change-me@127.0.0.1:59999/after_sales",
        connect_args={"connect_timeout": 1, "read_timeout": 1, "write_timeout": 1},
    )
    saver = MySQLCheckpointSaver(broken)
    with pytest.raises(CheckpointUnavailableError):
        saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    broken.dispose()


def test_delete_checkpoints_scoped_and_base_method_refuses(checkpoint_case) -> None:
    saver, engine = checkpoint_case
    saver.put(cfg(), make_checkpoint(uuid6()), {"source": "input"}, {})
    saver.put(cfg(thread="thread-c2"), make_checkpoint(uuid6()), {"source": "input"}, {})
    with pytest.raises(ValueError, match="tenant_id"):
        saver.delete_thread("thread-c1")
    saver.delete_checkpoints(TEST_TENANT, "thread-c1")
    assert _row_count(engine) == 1
    assert saver.get_tuple(cfg()) is None
    assert saver.get_tuple(cfg(thread="thread-c2")) is not None
