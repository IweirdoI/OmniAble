"""Agent 本地会话存储（TASK-018 / Issue #23）.

设计说明（按 Issue 要求先声明）：

数据位置
    `data/agent_sessions.db`（SQLite 单文件，项目根相对；可用环境变量
    `OS_AGENT_SESSIONS_DB` 覆盖）。与 ingestion staging 同目录约定，
    不入 Git（data/ 已 gitignore）。

与会话记录和 Memory Orchestrator 的关系
    会话记录是**展示与恢复用的对话副本**，其生命周期与长期记忆相互独立
    （Issue 明确要求独立，不另建平行记忆系统）。带 session_id 的请求由
    服务端从本库构建模型 history（排除 suppressed 与未完成消息），
    前端不再需要自行携带历史；未提供 session_id 的请求行为完全不变。

保留策略
    本地单机应用，不自动过期；用户删除会话时级联物理删除全部消息。
    遗忘联动：/forget 删除记忆后，引用这些记忆的消息标记 suppressed
    并把正文替换为占位——suppressed 消息**永不进入模型 history**（幂等）。

恢复语义
    仅在回合成功完成后由服务端落库（user + assistant 双条，status=completed）；
    中断、失败、部分完成（未核实工具操作）与客户端断线的回合不落库，
    恢复时不会冒充成功。

导入边界
    无既有聊天记录格式可导入（历史仅驻留前端内存）；旧客户端不带
    session_id 时行为与改造前一致（向后兼容）。

Schema（PRAGMA user_version = 3）：
    sessions(id TEXT PK, user_id TEXT NOT NULL, title TEXT NOT NULL,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL)
    messages(id TEXT PK, session_id TEXT NOT NULL
                 REFERENCES sessions(id) ON DELETE CASCADE,
             role TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
             memory_id TEXT, stored_memory_ids TEXT,
             referenced_memory_ids TEXT,
             suppressed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)
    forgotten_memories(user_id TEXT NOT NULL, memory_id TEXT NOT NULL,
                       forgotten_at TEXT NOT NULL,
                       PRIMARY KEY (user_id, memory_id))

    v2 增量：`referenced_memory_ids` 记录该回合**回答实际引用的召回记忆** id。
    v3 增量：`forgotten_memories` 是已遗忘记忆的墓碑表——遗忘成功即写入，
    迟到回合落库时校验关联 id，命中则不落库（复核阻塞项 3：遗忘前已召回、
    遗忘后才完成的回合无法把旧内容重新写回历史）。
    旧库（v1/v2）在初始化时按需补列/建表，旧记录兼容。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

# 被遗忘消息的展示占位（同时保证原文不再进入模型 history）
FORGOTTEN_PLACEHOLDER = "[该条消息已按遗忘指令删除]"

DEFAULT_DB_PATH = "data/agent_sessions.db"

_SCHEMA_VERSION = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    """单机 SQLite 会话存储：用户隔离、completed-only 恢复、遗忘联动。"""

    def __init__(self, db_path: str | os.PathLike[str] | None = None, *,
                 project_root: str | os.PathLike[str] | None = None):
        resolved = Path(
            db_path or os.getenv("OS_AGENT_SESSIONS_DB") or DEFAULT_DB_PATH
        )
        if not resolved.is_absolute():
            base = Path(project_root) if project_root else Path.cwd()
            resolved = base / resolved
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.path = resolved
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(resolved), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                        REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    memory_id TEXT,
                    stored_memory_ids TEXT,
                    referenced_memory_ids TEXT,
                    suppressed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
                    ON sessions(user_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id);
                CREATE TABLE IF NOT EXISTS forgotten_memories (
                    user_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    forgotten_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, memory_id)
                );
                """
            )
            # v1 → v2 迁移：旧库补 referenced_memory_ids 列（旧记录为 NULL）
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "referenced_memory_ids" not in columns:
                self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN referenced_memory_ids TEXT"
                )
            self._conn.execute(
                f"PRAGMA user_version = {_SCHEMA_VERSION}"
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------

    def create_session(self, user_id: str, title: str | None = None) -> dict[str, Any]:
        now = _now()
        session = {
            "id": str(uuid4()),
            "user_id": user_id,
            "title": (title or "新会话").strip()[:200] or "新会话",
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(id, user_id, title, created_at, updated_at)"
                " VALUES(:id, :user_id, :title, :created_at, :updated_at)",
                session,
            )
            self._conn.commit()
        return session

    def list_sessions(self, user_id: str, *, offset: int = 0,
                      limit: int = 50) -> dict[str, Any]:
        """按更新时间倒序分页列出用户会话（含消息数）；严格用户隔离。"""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE user_id = :u",
                {"u": user_id},
            ).fetchone()["n"]
            rows = self._conn.execute(
                """
                SELECT s.id, s.title, s.created_at, s.updated_at,
                       (SELECT COUNT(*) FROM messages m
                         WHERE m.session_id = s.id AND m.suppressed = 0) AS message_count
                FROM sessions s WHERE s.user_id = :u
                ORDER BY s.updated_at DESC LIMIT :limit OFFSET :offset
                """,
                {"u": user_id, "limit": limit, "offset": offset},
            ).fetchall()
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": [dict(row) for row in rows],
        }

    def get_session(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        """会话详情 + 消息（展示口径：suppressed 消息显示占位）。跨用户 → None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = :sid AND user_id = :u",
                {"sid": session_id, "u": user_id},
            ).fetchone()
            if row is None:
                return None
            messages = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = :sid ORDER BY rowid ASC",
                {"sid": session_id},
            ).fetchall()
        session = dict(row)
        session["messages"] = [self._display_message(dict(m)) for m in messages]
        return session

    def delete_session(self, user_id: str, session_id: str) -> bool:
        """删除会话（级联消息，物理删除）。跨用户 → False。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE id = :sid AND user_id = :u",
                {"sid": session_id, "u": user_id},
            )
            self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 消息（回合落库 / 模型 history 构建 / 遗忘联动）
    # ------------------------------------------------------------------

    def append_turn(self, user_id: str, session_id: str, *,
                    user_content: str, assistant_content: str,
                    memory_id: str | None = None,
                    stored_memory_ids: list[str] | None = None,
                    referenced_memory_ids: list[str] | None = None) -> bool:
        """成功回合落库：user + assistant 双条 completed 消息。跨用户 → False。

        记忆关联覆盖三类来源（任一类命中即整轮可抑制）：
          - memory_id：本轮写入的用户输入记忆
          - stored_memory_ids：consolidate 晋升产生的记忆
          - referenced_memory_ids：**回答实际引用的召回记忆**（复核阻塞项 2）
        """
        linked_ids = [
            *([memory_id] if memory_id else []),
            *(stored_memory_ids or []),
            *(referenced_memory_ids or []),
        ]
        now = _now()
        stored_json = (
            json.dumps(stored_memory_ids, ensure_ascii=False)
            if stored_memory_ids else None
        )
        referenced_json = (
            json.dumps(sorted(set(referenced_memory_ids)), ensure_ascii=False)
            if referenced_memory_ids else None
        )
        # Serialize the tombstone check and insert even across DB connections.
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            owned = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = :sid AND user_id = :u",
                {"sid": session_id, "u": user_id},
            ).fetchone()
            if owned is None or self.forgotten_ids(user_id, linked_ids):
                return False
            # 记忆链接记在本轮两条消息上：遗忘抑制按整轮生效（assistant 回复
            # 是同一回合的相关文本，不单独残留）
            self._conn.executemany(
                "INSERT INTO messages(id, session_id, role, content, status,"
                " memory_id, stored_memory_ids, referenced_memory_ids,"
                " suppressed, created_at)"
                " VALUES(:id, :sid, :role, :content, 'completed', :mid, :sids,"
                " :refs, 0, :ts)",
                [
                    {"id": str(uuid4()), "sid": session_id, "role": "user",
                     "content": user_content, "mid": memory_id, "sids": stored_json,
                     "refs": referenced_json, "ts": now},
                    {"id": str(uuid4()), "sid": session_id, "role": "assistant",
                     "content": assistant_content, "mid": memory_id, "sids": stored_json,
                     "refs": referenced_json, "ts": now},
                ],
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = :ts WHERE id = :sid",
                {"ts": now, "sid": session_id},
            )
        return True

    def build_model_history(self, user_id: str, session_id: str,
                            *, limit: int = 20) -> list[dict[str, str]] | None:
        """Return the visible, completed messages for this session."""
        snapshot = self.load_model_history(user_id, session_id, limit=limit)
        return snapshot[0] if snapshot is not None else None

    def load_model_history(self, user_id: str, session_id: str,
                           *, limit: int = 20) -> tuple[list[dict[str, str]], set[str]] | None:
        """Read the last completed messages and their source IDs together."""
        with self._lock:
            owned = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = :sid AND user_id = :u",
                {"sid": session_id, "u": user_id},
            ).fetchone()
            if owned is None:
                return None
            rows = self._conn.execute(
                "SELECT role, content, memory_id, stored_memory_ids, referenced_memory_ids FROM messages"
                " WHERE session_id = :sid AND status = 'completed' AND suppressed = 0"
                " ORDER BY rowid DESC LIMIT :limit",
                {"sid": session_id, "limit": limit},
            ).fetchall()
        history = [
            {"role": row["role"], "content": row["content"]}
            for row in reversed(rows)
        ]
        source_ids = {memory_id for row in rows for memory_id in self._linked_memory_ids(row)}
        return history, source_ids

    def forget_memories(self, user_id: str, memory_ids: list[str]) -> dict:
        """遗忘联动唯一入口：写墓碑 + 抑制现存消息（幂等）。

        调用方（REST /forget、聊天内遗忘路由、流式终态）只需调用本方法，
        即同时获得：
          1. 现存消息抑制——引用这些 id 的回合正文替换为占位、退出模型历史
          2. 墓碑记录——此后晚到的回合（本人遗忘前已召回、遗忘后才完成）
             在落库时被拦下，无法把旧内容重新写回历史（复核阻塞项 3）
        """
        targets = {str(mid) for mid in memory_ids if mid}
        if not targets:
            return {"forgotten_recorded": 0, "messages_suppressed": 0}
        now = _now()
        with self._lock, self._conn:
            remembered = self._conn.executemany(
                "INSERT OR IGNORE INTO forgotten_memories"
                "(user_id, memory_id, forgotten_at) VALUES(:u, :m, :ts)",
                [{"u": user_id, "m": mid, "ts": now} for mid in targets],
            ).rowcount
            suppressed = self._suppress_linked_turns(user_id, targets)
        return {"forgotten_recorded": remembered, "messages_suppressed": suppressed}

    def forgotten_ids(self, user_id: str,
                      memory_ids: list[str]) -> set[str]:
        """返回这些 id 中已被遗忘的部分（墓碑查询）。"""
        targets = sorted({str(mid) for mid in memory_ids if mid})
        if not targets:
            return set()
        with self._lock:
            rows = self._conn.execute(
                "SELECT memory_id FROM forgotten_memories"
                " WHERE user_id = ? AND memory_id IN (SELECT value FROM json_each(?))",
                (user_id, json.dumps(targets, ensure_ascii=False)),
            ).fetchall()
        return {row["memory_id"] for row in rows}

    def suppress_by_memory_ids(self, user_id: str,
                               memory_ids: list[str]) -> int:
        """遗忘联动：引用已删记忆的消息标记 suppressed 并替换正文为占位。

        命中规则：message.memory_id、message.stored_memory_ids 或
        message.referenced_memory_ids（后两者为 JSON 数组）与传入 id 有交集。
        后者覆盖「回答引用的召回记忆」——删除源记忆后，旧回答也不会留在
        模型历史里（复核阻塞项 2）。幂等：重复执行对已 suppressed 消息零变化。
        返回本次新 suppress 的消息数。跨用户隔离：仅在本人会话范围内生效。
        """
        if not memory_ids:
            return 0
        targets = {str(mid) for mid in memory_ids if mid}
        if not targets:
            return 0
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            return self._suppress_linked_turns(user_id, targets)

    def _suppress_linked_turns(self, user_id: str, targets: set[str]) -> int:
        rows = self._conn.execute(
            """
            SELECT m.id, m.memory_id, m.stored_memory_ids, m.referenced_memory_ids
            FROM messages m JOIN sessions s ON s.id = m.session_id
            WHERE s.user_id = :u AND m.suppressed = 0
              AND (m.memory_id IS NOT NULL
                   OR m.stored_memory_ids IS NOT NULL
                   OR m.referenced_memory_ids IS NOT NULL)
            """,
            {"u": user_id},
        )
        updates = [
            (FORGOTTEN_PLACEHOLDER, row["id"])
            for row in rows if self._linked_memory_ids(row) & targets
        ]
        return self._conn.executemany(
            "UPDATE messages SET suppressed = 1, content = ? WHERE id = ?", updates,
        ).rowcount

    @staticmethod
    def _linked_memory_ids(row: sqlite3.Row) -> set[str]:
        linked = {row["memory_id"]} if row["memory_id"] else set()
        for column in ("stored_memory_ids", "referenced_memory_ids"):
            raw = row[column]
            if raw:
                try:
                    linked.update(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return linked

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _display_message(message: dict[str, Any]) -> dict[str, Any]:
        if message.get("suppressed"):
            message["content"] = FORGOTTEN_PLACEHOLDER
        return message

    def record_completed_turn(self, user_id: str, session_id: str,
                              user_message: str, result: dict[str, Any], *,
                              history_memory_ids: set[str] | None = None) -> bool:
        """把一个成功完成的回合落库（仅 workflow.status == completed）。

        不落库的回合：
          - 中断 / 超时 / 部分完成（未核实工具操作）
          - **遗忘路由回合**（reset_history=True 或 route=forget）——遗忘指令
            本身不得作为对话记录保存（复核阻塞项 2）
        恢复时因此不会冒充成功。遗忘路由的 deleted_ids 由调用方转交
        suppress_by_memory_ids。
        """
        workflow = result.get("workflow") or {}
        if workflow.get("status") != "completed":
            return False
        if result.get("reset_history") or workflow.get("route") == "forget":
            return False
        write = result.get("memory_write") or {}
        memory_id = write.get("memory_id") if write.get("status") == "accepted" else None
        stored_ids = write.get("stored_memory_ids") or None
        referenced_ids = set(result.get("referenced_memory_ids") or ()) | (history_memory_ids or set())
        answer = str(result.get("answer") or "")
        return self.append_turn(
            user_id,
            session_id,
            user_content=user_message,
            assistant_content=answer,
            memory_id=memory_id,
            stored_memory_ids=stored_ids,
            referenced_memory_ids=sorted(referenced_ids),
        )
