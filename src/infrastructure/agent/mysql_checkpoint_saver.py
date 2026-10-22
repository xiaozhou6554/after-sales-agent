"""MySQL Checkpoint 保存器（技术详细规格 11.1，流程步骤12）。

为锁定的 langgraph-checkpoint==4.3.0 自研适配器：实现 ``BaseCheckpointSaver``
的 put/get_tuple/list/put_writes/delete_thread，并同时实现应用层
``CheckpointRepositoryPort``（latest/delete_thread），供恢复核对与运维清理。

- 联合恢复键（tenant_id, thread_id, checkpoint_ns, checkpoint_id）：
  tenant_id 经 ``config["configurable"]`` 注入，缺失即拒绝（fail-closed）；
- 版本与并发（549-552 行）：revision 根 0、子=父+1；同一父仅允许一个有效子
  版本（重复分叉/父缺失/同 ID 不同内容 → CheckpointConflictError，不静默覆盖）；
  相同 ID 且内容一致 → 幂等成功；
- pending_writes（553-554 行）：以 (task_id, channel) 归并存储（常规写先到
  先得、特殊通道后到覆盖，节点更新为每 channel 单写，语义与官方索引一致），
  随所在 Checkpoint 行持久化，服务重启后仍可恢复，读取统一为列表；
  引擎允许 put_writes 先于同 ID 的 put 到达（输入写入挂到尚未落库的
  Checkpoint，官方契约如此），此时落占位行承接写入，占位行不作为
  Checkpoint 呈现（get_tuple/latest/list 一律过滤），待真实 put 到达原位补全；
- 脱敏（543 行）：state/metadata/pending_writes 写入前经 ``mask_mapping``；
- state_schema_version（558 行）：读取时不兼容版本抛 CheckpointSchemaError
  阻止恢复，不丢弃未知字段；
- 传输失败统一折算 CheckpointUnavailableError；语义冲突在事务内回滚后原样
  上抛，不吞并。
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Iterator, Sequence

from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    RunnableConfig,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from application.agent.ports.checkpoint import (
    CheckpointConflictError,
    CheckpointRepositoryPort,
    CheckpointSchemaError,
    CheckpointSummary,
    CheckpointUnavailableError,
)
from application.common.masking import mask_mapping

logger = logging.getLogger("after_sales.agent")

# 本步锁定的 Agent State 结构版本；不兼容行阻止恢复（规格 558 行）。
AGENT_STATE_SCHEMA_VERSION = "1"

# 引擎契约允许 put_writes 先于同 ID 的 put 到达（输入写入挂到尚未落库的
# checkpoint）。此时落"占位行"承接 pending_writes：占位行不是 Checkpoint，
# 所有读取路径一律过滤，不得伪装成已完成 Checkpoint（规格 553 行）。
_PLACEHOLDER_STATE = '{"placeholder": true}'


class MySQLCheckpointSaver(BaseCheckpointSaver[str], CheckpointRepositoryPort):
    """LangGraph 状态持久化（MySQL）＋应用层恢复核对端口的双重实现。"""

    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self._engine = engine

    # ------------------------------------------------------------------
    # 恢复键与编解码
    # ------------------------------------------------------------------

    @staticmethod
    def _scope(config: RunnableConfig | None) -> tuple[str, str, str]:
        configurable = (config or {}).get("configurable") or {}
        tenant_id = configurable.get("tenant_id")
        thread_id = configurable.get("thread_id")
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("checkpoint 操作缺少 tenant_id（联合恢复键必填，fail-closed）")
        if not thread_id or not isinstance(thread_id, str):
            raise ValueError("checkpoint 操作缺少 thread_id（联合恢复键必填，fail-closed）")
        checkpoint_ns = configurable.get("checkpoint_ns") or ""
        return tenant_id, thread_id, str(checkpoint_ns)

    def _encode(self, obj: Any) -> str:
        """先脱敏、再 serde 序列化；列内存 serde 标签＋base64 文本。

        JsonPlusSerializer 默认输出 msgpack（含非文本字节），必须先对
        Python 对象脱敏再序列化，避免敏感原文经编码进入数据库（规格 543 行）。
        """
        tag, data = self.serde.dumps_typed(mask_mapping(obj))
        wrapped = {
            "serde_type": tag,
            "payload_b64": base64.b64encode(data).decode("ascii"),
        }
        return json.dumps(wrapped, ensure_ascii=False, separators=(",", ":"))

    def _decode_wrapped(self, column: str) -> Any:
        wrapped = json.loads(column)
        return self.serde.loads_typed(
            (wrapped["serde_type"], base64.b64decode(wrapped["payload_b64"]))
        )

    @staticmethod
    def _decode_value(wrapped: Any) -> Any:
        """pending_writes 的单值读取：存入时已是脱敏后的可序列化对象。"""
        return wrapped

    @staticmethod
    def _mask_value(value: Any) -> Any:
        return mask_mapping(value)

    @staticmethod
    def _current_node(metadata: CheckpointMetadata) -> str | None:
        writes = metadata.get("writes") if isinstance(metadata, dict) else None
        if isinstance(writes, dict) and writes:
            return str(next(iter(writes)))[:64]
        return None

    @staticmethod
    def _is_placeholder(state_text: Any) -> bool:
        """占位行判定：键唯一且为真；真实行恒为 {serde_type, payload_b64} 包装。"""
        if not state_text:
            return False
        try:
            parsed = json.loads(state_text)
        except (TypeError, ValueError):
            return False
        return (
            isinstance(parsed, dict)
            and set(parsed.keys()) == {"placeholder"}
            and parsed["placeholder"] is True
        )

    # 占位行不含 serde 包装，SQL 侧据此过滤（真实行必有 serde_type 键）。
    _SQL_EXCLUDE_PLACEHOLDER = "AND JSON_EXTRACT(state, '$.serde_type') IS NOT NULL"

    # ------------------------------------------------------------------
    # BaseCheckpointSaver：put
    # ------------------------------------------------------------------

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        tenant_id, thread_id, checkpoint_ns = self._scope(config)
        checkpoint_id = str(checkpoint["id"])
        parent_id = (config.get("configurable") or {}).get("checkpoint_id") or None
        state_text = self._encode(dict(checkpoint))
        effective_metadata = dict(get_checkpoint_metadata(config, metadata))
        metadata_text = self._encode(effective_metadata)
        current_node = self._current_node(effective_metadata)
        returned: RunnableConfig = {
            "configurable": {
                "tenant_id": tenant_id,
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }
        try:
            with self._engine.begin() as conn:
                existing = conn.execute(
                    text(
                        "SELECT state, metadata, parent_checkpoint_id, revision "
                        "FROM agent_checkpoint "
                        "WHERE tenant_id = :tenant_id AND thread_id = :thread_id "
                        "AND checkpoint_ns = :checkpoint_ns AND checkpoint_id = :checkpoint_id "
                        "FOR UPDATE"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint_id,
                    },
                ).first()
                if existing is not None:
                    if self._is_placeholder(existing.state):
                        # 占位行原位补全为真实 Checkpoint；已到的 pending_writes
                        # 保留（先于 put 到达的写入本就属于本 Checkpoint）。
                        revision = self._next_revision(
                            conn, tenant_id, thread_id, checkpoint_ns, parent_id, checkpoint_id
                        )
                        conn.execute(
                            text(
                                "UPDATE agent_checkpoint SET "
                                "parent_checkpoint_id = :parent_checkpoint_id, "
                                "current_node = :current_node, "
                                "state_schema_version = :state_schema_version, "
                                "revision = :revision, state = :state, metadata = :metadata "
                                "WHERE tenant_id = :tenant_id AND thread_id = :thread_id "
                                "AND checkpoint_ns = :checkpoint_ns "
                                "AND checkpoint_id = :checkpoint_id"
                            ),
                            {
                                "tenant_id": tenant_id,
                                "thread_id": thread_id,
                                "checkpoint_ns": checkpoint_ns,
                                "checkpoint_id": checkpoint_id,
                                "parent_checkpoint_id": parent_id,
                                "current_node": current_node,
                                "state_schema_version": AGENT_STATE_SCHEMA_VERSION,
                                "revision": revision,
                                "state": state_text,
                                "metadata": metadata_text,
                            },
                        )
                        return returned
                    self._assert_idempotent(existing, parent_id, state_text, metadata_text)
                    return returned
                revision = self._next_revision(
                    conn, tenant_id, thread_id, checkpoint_ns, parent_id, checkpoint_id
                )
                conn.execute(
                    text(
                        "INSERT INTO agent_checkpoint ("
                        "tenant_id, thread_id, checkpoint_ns, checkpoint_id, "
                        "parent_checkpoint_id, current_node, state_schema_version, "
                        "revision, state, metadata, pending_writes"
                        ") VALUES ("
                        ":tenant_id, :thread_id, :checkpoint_ns, :checkpoint_id, "
                        ":parent_checkpoint_id, :current_node, :state_schema_version, "
                        ":revision, :state, :metadata, :pending_writes)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint_id,
                        "parent_checkpoint_id": parent_id,
                        "current_node": current_node,
                        "state_schema_version": AGENT_STATE_SCHEMA_VERSION,
                        "revision": revision,
                        "state": state_text,
                        "metadata": metadata_text,
                        "pending_writes": json.dumps([], ensure_ascii=False),
                    },
                )
        except (CheckpointConflictError, CheckpointSchemaError):
            raise
        except SQLAlchemyError as exc:
            raise CheckpointUnavailableError(f"checkpoint 写入失败：{exc}") from exc
        return returned

    @staticmethod
    def _assert_idempotent(
        existing: Any, parent_id: str | None, state_text: str, metadata_text: str
    ) -> None:
        """同 ID 重复写入：内容一致幂等成功；不一致视为冲突，不得覆盖。"""
        same_parent = (existing.parent_checkpoint_id or None) == (parent_id or None)
        if (
            same_parent
            and json.loads(existing.state) == json.loads(state_text)
            and json.loads(existing.metadata) == json.loads(metadata_text)
        ):
            return
        raise CheckpointConflictError(
            "相同 checkpoint_id 的内容不一致，拒绝覆盖（结构化冲突，不静默丢失）"
        )

    @staticmethod
    def _next_revision(
        conn: Any,
        tenant_id: str,
        thread_id: str,
        checkpoint_ns: str,
        parent_id: str | None,
        checkpoint_id: str,
    ) -> int:
        if parent_id is None:
            return 0
        parent = conn.execute(
            text(
                "SELECT revision FROM agent_checkpoint "
                "WHERE tenant_id = :tenant_id AND thread_id = :thread_id "
                "AND checkpoint_ns = :checkpoint_ns AND checkpoint_id = :parent_id "
                "FOR UPDATE"
            ),
            {
                "tenant_id": tenant_id,
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "parent_id": parent_id,
            },
        ).first()
        if parent is None:
            raise CheckpointConflictError(
                "parent_checkpoint_id 不存在（同租户/线程/命名空间下无此父版本）"
            )
        fork = conn.execute(
            text(
                "SELECT checkpoint_id FROM agent_checkpoint "
                "WHERE tenant_id = :tenant_id AND thread_id = :thread_id "
                "AND checkpoint_ns = :checkpoint_ns AND parent_checkpoint_id = :parent_id "
                "AND checkpoint_id != :checkpoint_id LIMIT 1"
            ),
            {
                "tenant_id": tenant_id,
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "parent_id": parent_id,
                "checkpoint_id": checkpoint_id,
            },
        ).first()
        if fork is not None:
            raise CheckpointConflictError("同一父 Checkpoint 已存在有效子版本，拒绝重复分叉")
        return int(parent.revision) + 1

    # ------------------------------------------------------------------
    # BaseCheckpointSaver：get_tuple / list
    # ------------------------------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        tenant_id, thread_id, checkpoint_ns = self._scope(config)
        checkpoint_id = get_checkpoint_id(config)
        try:
            row = self._fetch_row(tenant_id, thread_id, checkpoint_ns, checkpoint_id)
        except SQLAlchemyError as exc:
            raise CheckpointUnavailableError(f"checkpoint 读取失败：{exc}") from exc
        if row is None:
            return None
        return self._row_to_tuple(row, tenant_id)

    def _fetch_row(
        self,
        tenant_id: str,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str | None,
    ) -> Any:
        base_where = (
            "FROM agent_checkpoint "
            "WHERE tenant_id = :tenant_id AND thread_id = :thread_id "
            "AND checkpoint_ns = :checkpoint_ns"
        )
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        }
        if checkpoint_id:
            sql = (
                f"SELECT * {base_where} AND checkpoint_id = :checkpoint_id "
                f"{self._SQL_EXCLUDE_PLACEHOLDER}"
            )
            params["checkpoint_id"] = checkpoint_id
        else:
            sql = (
                f"SELECT * {base_where} {self._SQL_EXCLUDE_PLACEHOLDER} "
                "ORDER BY checkpoint_id DESC LIMIT 1"
            )
        with self._engine.connect() as conn:
            return conn.execute(text(sql), params).first()

    def _row_to_tuple(self, row: Any, tenant_id: str) -> CheckpointTuple:
        if row.state_schema_version != AGENT_STATE_SCHEMA_VERSION:
            raise CheckpointSchemaError(
                f"Checkpoint state_schema_version 不兼容："
                f"行内 {row.state_schema_version!r}，当前支持 {AGENT_STATE_SCHEMA_VERSION!r}；"
                "阻止恢复并转人工，不丢弃未知 State"
            )
        checkpoint = self._decode_wrapped(row.state)
        metadata = self._decode_wrapped(row.metadata)
        thread_id = row.thread_id
        checkpoint_ns = row.checkpoint_ns
        base_configurable: dict[str, Any] = {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        }
        if row.parent_checkpoint_id:
            parent_config: RunnableConfig | None = {
                "configurable": {
                    **base_configurable,
                    "checkpoint_id": row.parent_checkpoint_id,
                }
            }
        else:
            parent_config = None
        pending = self._pending_from_column(row.pending_writes)
        return CheckpointTuple(
            config={
                "configurable": {
                    **base_configurable,
                    "checkpoint_id": row.checkpoint_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending,
        )

    @staticmethod
    def _pending_from_column(column: str | None) -> list[PendingWrite]:
        entries = json.loads(column) if column else []
        ordered = sorted(
            entries, key=lambda e: (str(e.get("task_path") or ""), str(e.get("task_id") or ""))
        )
        return [
            (
                str(e["task_id"]),
                str(e["channel"]),
                MySQLCheckpointSaver._decode_value(e.get("value")),
            )
            for e in ordered
        ]

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        # 与会话/审计一致：任何检索都必须带租户，拒绝无租户全表遍历。
        tenant_id, thread_id, checkpoint_ns = self._scope(config)
        config_id = get_checkpoint_id(config) if config else None
        before_id = get_checkpoint_id(before) if before else None
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
        }
        sql = (
            "SELECT * FROM agent_checkpoint "
            "WHERE tenant_id = :tenant_id AND thread_id = :thread_id "
            f"AND checkpoint_ns = :checkpoint_ns {self._SQL_EXCLUDE_PLACEHOLDER}"
        )
        if config_id:
            sql += " AND checkpoint_id = :config_id"
            params["config_id"] = config_id
        if before_id:
            sql += " AND checkpoint_id < :before_id"
            params["before_id"] = before_id
        sql += " ORDER BY checkpoint_id DESC"
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = int(limit)
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(text(sql), params).all()
        except SQLAlchemyError as exc:
            raise CheckpointUnavailableError(f"checkpoint 列举失败：{exc}") from exc
        for row in rows:
            metadata = self._decode_wrapped(row.metadata)
            if filter and not all(metadata.get(key) == value for key, value in filter.items()):
                continue
            yield self._row_to_tuple(row, tenant_id)

    # ------------------------------------------------------------------
    # BaseCheckpointSaver：put_writes / delete_thread
    # ------------------------------------------------------------------

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        tenant_id, thread_id, checkpoint_ns = self._scope(config)
        checkpoint_id = (config.get("configurable") or {}).get("checkpoint_id")
        if not checkpoint_id:
            raise ValueError("put_writes 需要 configurable.checkpoint_id（写入目标必填）")
        incoming = [
            {
                "task_id": task_id,
                "task_path": task_path,
                "channel": str(channel),
                "value": self._mask_value(value),
            }
            for channel, value in writes
        ]
        scope_params = {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
        # 先直接 INSERT 占位行承接（官方契约允许写入先于 Checkpoint 落库；
        # 不做"先 SELECT FOR UPDATE 再 INSERT"，避免与并发 put 的间隙锁
        # 互等死锁）。目标行已存在时落入归并路径（SAVEPOINT 内捕获重复键）。
        try:
            with self._engine.begin() as conn:
                try:
                    with conn.begin_nested():
                        conn.execute(
                            text(
                                "INSERT INTO agent_checkpoint ("
                                "tenant_id, thread_id, checkpoint_ns, checkpoint_id, "
                                "parent_checkpoint_id, current_node, state_schema_version, "
                                "revision, state, metadata, pending_writes"
                                ") VALUES ("
                                ":tenant_id, :thread_id, :checkpoint_ns, :checkpoint_id, "
                                "NULL, NULL, :state_schema_version, 0, "
                                ":state, '{}', :pending_writes)"
                            ),
                            {
                                **scope_params,
                                "state_schema_version": AGENT_STATE_SCHEMA_VERSION,
                                "state": _PLACEHOLDER_STATE,
                                "pending_writes": json.dumps(
                                    self._merge_pending_writes([], incoming),
                                    ensure_ascii=False,
                                ),
                            },
                        )
                    return
                except IntegrityError:
                    pass
                row = conn.execute(
                    text(
                        "SELECT pending_writes FROM agent_checkpoint "
                        "WHERE tenant_id = :tenant_id AND thread_id = :thread_id "
                        "AND checkpoint_ns = :checkpoint_ns AND checkpoint_id = :checkpoint_id "
                        "FOR UPDATE"
                    ),
                    scope_params,
                ).first()
                if row is None:
                    raise CheckpointConflictError(
                        "put_writes 目标行在并发窗口内消失，拒绝归并（结构化冲突）"
                    )
                merged = self._merge_pending_writes(
                    json.loads(row.pending_writes) if row.pending_writes else [],
                    incoming,
                )
                conn.execute(
                    text(
                        "UPDATE agent_checkpoint SET pending_writes = :pending_writes "
                        "WHERE tenant_id = :tenant_id AND thread_id = :thread_id "
                        "AND checkpoint_ns = :checkpoint_ns AND checkpoint_id = :checkpoint_id"
                    ),
                    {
                        "pending_writes": json.dumps(merged, ensure_ascii=False),
                        **scope_params,
                    },
                )
        except CheckpointConflictError:
            raise
        except SQLAlchemyError as exc:
            raise CheckpointUnavailableError(f"pending_writes 写入失败：{exc}") from exc

    @staticmethod
    def _merge_pending_writes(
        existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """(task_id, channel) 归并：常规写先到先得，特殊通道后到覆盖。

        LangGraph 节点更新对每个 channel 仅产生一条写，语义与官方
        (task_id, WRITES_IDX_MAP.get(channel, idx)) 索引在此前提下一致。
        """
        merged = list(existing)
        for entry in incoming:
            special = entry["channel"] in WRITES_IDX_MAP
            same_slot = [
                e
                for e in merged
                if e.get("task_id") == entry["task_id"] and e.get("channel") == entry["channel"]
            ]
            if same_slot and not special:
                continue
            if same_slot and special:
                merged = [
                    e
                    for e in merged
                    if not (
                        e.get("task_id") == entry["task_id"]
                        and e.get("channel") == entry["channel"]
                    )
                ]
            merged.append(entry)
        merged.sort(key=lambda e: (str(e.get("task_path") or ""), str(e.get("task_id") or "")))
        return merged

    def delete_thread(self, thread_id: str) -> None:
        # LangGraph 基类签名只带 thread_id；租户隔离键要求必须显式带租户，
        # 拒绝跨租户模糊删除（应用侧使用 CheckpointRepositoryPort.delete_thread）。
        raise ValueError(
            "checkpoint 删除必须携带 tenant_id（联合恢复键）；"
            "请使用 CheckpointRepositoryPort.delete_thread(tenant_id, thread_id)"
        )

    # ------------------------------------------------------------------
    # CheckpointRepositoryPort
    # ------------------------------------------------------------------

    def latest(
        self, tenant_id: str, thread_id: str, *, checkpoint_ns: str = ""
    ) -> CheckpointSummary | None:
        return self.by_id(tenant_id, thread_id, "", checkpoint_ns=checkpoint_ns)

    def by_id(
        self, tenant_id: str, thread_id: str, checkpoint_id: str, *, checkpoint_ns: str = ""
    ) -> CheckpointSummary | None:
        try:
            row = self._fetch_row(tenant_id, thread_id, checkpoint_ns, checkpoint_id or None)
        except SQLAlchemyError as exc:
            raise CheckpointUnavailableError(f"checkpoint 读取失败：{exc}") from exc
        if row is None:
            return None
        if row.state_schema_version != AGENT_STATE_SCHEMA_VERSION:
            raise CheckpointSchemaError(
                f"Checkpoint state_schema_version 不兼容："
                f"行内 {row.state_schema_version!r}，当前支持 {AGENT_STATE_SCHEMA_VERSION!r}；"
                "阻止恢复核对并转人工，不丢弃未知 State"
            )
        state = self._decode_wrapped(row.state)
        return CheckpointSummary(
            checkpoint_id=str(row.checkpoint_id),
            parent_checkpoint_id=row.parent_checkpoint_id,
            current_node=row.current_node,
            state_schema_version=str(row.state_schema_version),
            revision=int(row.revision),
            saved_at=str(row.saved_at),
            state=mask_mapping(state),
            pending_writes=json.loads(row.pending_writes) if row.pending_writes else [],
        )

    def delete_checkpoints(self, tenant_id: str, thread_id: str) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "DELETE FROM agent_checkpoint "
                        "WHERE tenant_id = :tenant_id AND thread_id = :thread_id"
                    ),
                    {"tenant_id": tenant_id, "thread_id": thread_id},
                )
        except SQLAlchemyError as exc:
            raise CheckpointUnavailableError(f"checkpoint 删除失败：{exc}") from exc
