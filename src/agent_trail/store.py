from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import ActivitySession, ChatMessage, ChatThread, ConversationMessage, SearchHit, SessionFilter


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

_COLUMNS = (
    "source",
    "session_id",
    "workdir",
    "workdir_normalized",
    "title",
    "title_source",
    "created_at",
    "updated_at",
    "additions",
    "deletions",
    "files_changed",
    "source_locator",
    "resume_kind",
    "resume_ref",
    "source_missing",
    "archived",
    "adapter_version",
    "source_fingerprint",
    "last_scanned_at",
)


class LocalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                source TEXT NOT NULL,
                session_id TEXT NOT NULL,
                workdir TEXT NOT NULL,
                workdir_normalized TEXT NOT NULL,
                title TEXT,
                title_source TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT NOT NULL,
                additions INTEGER,
                deletions INTEGER,
                files_changed INTEGER,
                source_locator TEXT NOT NULL,
                resume_kind TEXT NOT NULL,
                resume_ref TEXT,
                source_missing INTEGER NOT NULL,
                archived INTEGER NOT NULL,
                adapter_version INTEGER NOT NULL,
                source_fingerprint TEXT NOT NULL,
                last_scanned_at TEXT NOT NULL,
                PRIMARY KEY (source, session_id)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS adapter_state (
                source TEXT PRIMARY KEY,
                fingerprint TEXT,
                last_scanned_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS sessions_updated_at ON sessions(updated_at DESC)"
        )
        # ---- 工作台扩展（0.5.0）：全部追加式建表，旧库直接升级 ----
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS content_messages (
                source TEXT NOT NULL,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp TEXT,
                kind TEXT NOT NULL DEFAULT 'text',
                PRIMARY KEY (source, session_id, seq)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS content_state (
                source TEXT NOT NULL,
                session_id TEXT NOT NULL,
                locator TEXT NOT NULL,
                locator_mtime_ns INTEGER NOT NULL,
                locator_size INTEGER NOT NULL,
                adapter_version INTEGER NOT NULL,
                truncated INTEGER NOT NULL,
                indexed_at TEXT NOT NULL,
                PRIMARY KEY (source, session_id)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_threads (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                workdir TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                session_ref TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS chat_messages_thread ON chat_messages(thread_id, id)"
        )
        # ACP 通道（0.6.0）：线程级模式/模型/思考强度 + ACP 会话引用。
        # 旧库追加列，存在即跳过（SQLite 无 ADD COLUMN IF NOT EXISTS）。
        for column in ("mode", "model", "thinking", "acp_session_id", "settings_json"):
            try:
                self._connection.execute(
                    f"ALTER TABLE chat_threads ADD COLUMN {column} TEXT"
                )
            except sqlite3.OperationalError:
                pass  # 列已存在
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_approvals (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT,
                options_json TEXT NOT NULL,
                status TEXT NOT NULL,
                option_id TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS chat_approvals_thread ON chat_approvals(thread_id, status)"
        )
        self._fts_available = self._try_create_fts()
        self._connection.commit()

    def _try_create_fts(self) -> bool:
        # 个别发行版裁剪了 FTS5；失败则全文检索降级为 LIKE 扫描。
        try:
            self._connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
                "text, source UNINDEXED, session_id UNINDEXED, seq UNINDEXED)"
            )
            self._connection.execute("SELECT 1 FROM message_fts LIMIT 1")
        except sqlite3.OperationalError:
            return False
        return True

    @property
    def fts_available(self) -> bool:
        return self._fts_available

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> LocalStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def replace_source(
        self,
        source: str,
        sessions: Iterable[ActivitySession],
        fingerprint: str | None,
        scanned_at: str,
    ) -> None:
        records = list(sessions)
        if any(record.source != source for record in records):
            raise ValueError("session source does not match adapter")
        placeholders = ",".join("?" for _ in _COLUMNS)
        assignments = ",".join(f"{column}=excluded.{column}" for column in _COLUMNS[2:])
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE sessions SET source_missing=1, resume_kind='none', "
                "resume_ref=NULL, last_scanned_at=? WHERE source=?",
                (scanned_at, source),
            )
            self._connection.executemany(
                f"INSERT INTO sessions ({','.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(source, session_id) DO UPDATE SET {assignments}",
                [self._values(record) for record in records],
            )
            self._connection.execute(
                "INSERT INTO adapter_state(source, fingerprint, last_scanned_at) VALUES(?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET fingerprint=excluded.fingerprint, "
                "last_scanned_at=excluded.last_scanned_at",
                (source, fingerprint, scanned_at),
            )

    def mark_unchanged(
        self, source: str, fingerprint: str | None, scanned_at: str
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO adapter_state(source, fingerprint, last_scanned_at) VALUES(?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET fingerprint=excluded.fingerprint, "
                "last_scanned_at=excluded.last_scanned_at",
                (source, fingerprint, scanned_at),
            )

    def list_sessions(self, filters: SessionFilter) -> list[ActivitySession]:
        clauses: list[str] = []
        parameters: list[object] = []
        if filters.since is not None:
            clauses.append("updated_at >= ?")
            parameters.append(filters.since)
        if filters.until is not None:
            clauses.append("updated_at < ?")
            parameters.append(filters.until)
        if filters.sources:
            clauses.append(f"source IN ({','.join('?' for _ in filters.sources)})")
            parameters.extend(filters.sources)
        if filters.workdir is not None:
            clauses.append("workdir_normalized = ?")
            parameters.append(filters.workdir)
        if filters.query:
            escaped = self._escape_like(filters.query)
            clauses.append("(title LIKE ? ESCAPE '\\' OR workdir LIKE ? ESCAPE '\\')")
            pattern = f"%{escaped}%"
            parameters.extend((pattern, pattern))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((filters.limit, filters.offset))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM sessions {where} "
                "ORDER BY updated_at DESC, source, session_id LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
            return [self._record(row) for row in rows]

    def get_session(self, source: str, session_id: str) -> ActivitySession | None:
        with self._lock:
            row = self._connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM sessions WHERE source=? AND session_id=?",
                (source, session_id),
            ).fetchone()
            return self._record(row) if row is not None else None

    def get_fingerprint(self, source: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT fingerprint FROM adapter_state WHERE source=?", (source,)
            ).fetchone()
            return row[0] if row is not None else None

    # ---- 会话正文（本地缓存 + 全文检索） ----

    def content_state(
        self, source: str, session_id: str
    ) -> tuple[int, int, int, bool] | None:
        """返回 (locator_mtime_ns, locator_size, adapter_version, truncated)。"""
        with self._lock:
            row = self._connection.execute(
                "SELECT locator_mtime_ns, locator_size, adapter_version, truncated "
                "FROM content_state WHERE source=? AND session_id=?",
                (source, session_id),
            ).fetchone()
            if row is None:
                return None
            return int(row[0]), int(row[1]), int(row[2]), bool(row[3])

    def index_content(
        self,
        source: str,
        session_id: str,
        locator: str,
        locator_stat: tuple[int, int],
        adapter_version: int,
        messages: list[ConversationMessage],
        truncated: bool,
        indexed_at: str,
    ) -> None:
        rows = [
            (source, session_id, seq, message.role, message.text, message.timestamp, message.kind)
            for seq, message in enumerate(messages)
        ]
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM content_messages WHERE source=? AND session_id=?",
                (source, session_id),
            )
            self._connection.execute(
                "DELETE FROM message_fts WHERE source=? AND session_id=?",
                (source, session_id),
            )
            self._connection.executemany(
                "INSERT INTO content_messages (source, session_id, seq, role, text, timestamp, kind) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            self._connection.executemany(
                "INSERT INTO message_fts (text, source, session_id, seq) VALUES (?,?,?,?)",
                [
                    (message.text, source, session_id, seq)
                    for seq, message in enumerate(messages)
                ],
            )
            self._connection.execute(
                "INSERT INTO content_state (source, session_id, locator, locator_mtime_ns, "
                "locator_size, adapter_version, truncated, indexed_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source, session_id) DO UPDATE SET locator=excluded.locator, "
                "locator_mtime_ns=excluded.locator_mtime_ns, locator_size=excluded.locator_size, "
                "adapter_version=excluded.adapter_version, truncated=excluded.truncated, "
                "indexed_at=excluded.indexed_at",
                (
                    source,
                    session_id,
                    locator,
                    locator_stat[0],
                    locator_stat[1],
                    adapter_version,
                    int(truncated),
                    indexed_at,
                ),
            )

    def list_messages(self, source: str, session_id: str) -> list[ConversationMessage]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT role, text, timestamp, kind FROM content_messages "
                "WHERE source=? AND session_id=? ORDER BY seq",
                (source, session_id),
            ).fetchall()
            return [
                ConversationMessage(role=row[0], text=row[1], timestamp=row[2], kind=row[3])
                for row in rows
            ]

    def search_content(self, query: str, limit: int = 30) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        with self._lock:
            if self._fts_available:
                fts_query = " AND ".join(
                    f'"{token.replace(chr(34), chr(34) * 2)}"' for token in query.split()
                )
                try:
                    rows = self._connection.execute(
                        "SELECT source, session_id, seq, role, "
                        "snippet(message_fts, 0, '<mark>', '</mark>', '…', 16) "
                        "FROM message_fts WHERE message_fts MATCH ? "
                        "ORDER BY rank LIMIT ?",
                        (fts_query, limit),
                    ).fetchall()
                    return [
                        SearchHit(source=row[0], session_id=row[1], seq=row[2], role=row[3], snippet=row[4])
                        for row in rows
                    ]
                except sqlite3.OperationalError:
                    pass  # 语法/引擎异常时退回 LIKE
            return self._search_content_like(query, limit)

    def _search_content_like(self, query: str, limit: int) -> list[SearchHit]:
        escaped = self._escape_like(query)
        pattern = f"%{escaped}%"
        rows = self._connection.execute(
            "SELECT source, session_id, seq, role, text FROM content_messages "
            "WHERE text LIKE ? ESCAPE '\\' ORDER BY rowid DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
        hits: list[SearchHit] = []
        for row in rows:
            text = row[4]
            position = text.lower().find(query.lower())
            start = max(0, position - 60)
            end = min(len(text), position + len(query) + 60)
            snippet = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
            snippet = snippet.replace(query, f"<mark>{query}</mark>")
            hits.append(SearchHit(source=row[0], session_id=row[1], seq=row[2], role=row[3], snippet=snippet))
        return hits

    # ---- 应用内聊天线程 ----

    def create_thread(self, thread: ChatThread) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO chat_threads (id, source, title, workdir, created_at, updated_at, "
                "session_ref, mode, model, thinking, acp_session_id, settings_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    thread.id,
                    thread.source,
                    thread.title,
                    thread.workdir,
                    thread.created_at,
                    thread.updated_at,
                    thread.session_ref,
                    thread.mode,
                    thread.model,
                    thread.thinking,
                    thread.acp_session_id,
                    json.dumps(thread.config, ensure_ascii=False) if thread.config else None,
                ),
            )

    def list_threads(self, limit: int = 100) -> list[ChatThread]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, source, title, workdir, created_at, updated_at, session_ref, "
                "mode, model, thinking, acp_session_id, settings_json "
                "FROM chat_threads ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._thread(row) for row in rows]

    def get_thread(self, thread_id: str) -> ChatThread | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, source, title, workdir, created_at, updated_at, session_ref, "
                "mode, model, thinking, acp_session_id, settings_json "
                "FROM chat_threads WHERE id=?",
                (thread_id,),
            ).fetchone()
            return self._thread(row) if row is not None else None

    def append_chat_message(self, thread_id: str, role: str, text: str, created_at: str) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO chat_messages (thread_id, role, text, created_at) VALUES (?,?,?,?)",
                (thread_id, role, text, created_at),
            )
            self._connection.execute(
                "UPDATE chat_threads SET updated_at=? WHERE id=?", (created_at, thread_id)
            )
            return int(cursor.lastrowid)

    def thread_messages(self, thread_id: str, limit: int = 1000) -> list[ChatMessage]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, thread_id, role, text, created_at FROM chat_messages "
                "WHERE thread_id=? ORDER BY id LIMIT ?",
                (thread_id, limit),
            ).fetchall()
            return [
                ChatMessage(id=row[0], thread_id=row[1], role=row[2], text=row[3], created_at=row[4])
                for row in rows
            ]

    def set_thread_session_ref(self, thread_id: str, session_ref: str | None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE chat_threads SET session_ref=? WHERE id=?", (session_ref, thread_id)
            )

    def list_recent_workdirs(self, limit: int = 8) -> list[dict]:
        """时间线里最近活动过的项目目录（文件夹选择器的快捷入口）。"""
        with self._lock:
            rows = self._connection.execute(
                "SELECT workdir, MAX(updated_at) AS last_at FROM sessions "
                "GROUP BY workdir_normalized ORDER BY last_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [{"workdir": row[0], "last_at": row[1]} for row in rows]

    def set_thread_settings(self, thread_id: str, values: dict[str, str]) -> None:
        """更新线程的 ACP 配置选择（通用键值，键为各 agent 的 configOption id）。"""
        if not values:
            return
        with self._lock:
            row = self._connection.execute(
                "SELECT settings_json FROM chat_threads WHERE id=?", (thread_id,)
            ).fetchone()
            current = {}
            if row is not None and row[0]:
                try:
                    current = json.loads(row[0])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    current = {}
            current.update(values)
            merged = json.dumps(current, ensure_ascii=False)
        assignments: list[str] = ["settings_json=?"]
        parameters: list[object] = [merged]
        for key in ("mode", "model", "thinking"):
            if key in values:
                assignments.append(f"{key}=?")
                parameters.append(values[key] or None)
        parameters.append(thread_id)
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE chat_threads SET {', '.join(assignments)} WHERE id=?",
                parameters,
            )

    def set_thread_acp_session(self, thread_id: str, acp_session_id: str | None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE chat_threads SET acp_session_id=? WHERE id=?",
                (acp_session_id, thread_id),
            )

    def rename_thread(self, thread_id: str, title: str, updated_at: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE chat_threads SET title=?, updated_at=? WHERE id=?",
                (title, updated_at, thread_id),
            )

    @staticmethod
    def _thread(row: sqlite3.Row) -> ChatThread:
        return ChatThread(
            id=row["id"],
            source=row["source"],
            title=row["title"],
            workdir=row["workdir"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            session_ref=row["session_ref"],
            mode=row["mode"],
            model=row["model"],
            thinking=row["thinking"],
            acp_session_id=row["acp_session_id"],
            config=json.loads(row["settings_json"]) if row["settings_json"] else {},
        )

    # ---- ACP 工具审批 ----

    def create_approval(self, approval_id: str, thread_id: str, title: str, kind: str, options_json: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO chat_approvals (id, thread_id, title, kind, options_json, "
                "status, created_at) VALUES (?,?,?,?,?,?,?)",
                (approval_id, thread_id, title, kind, options_json, "pending", _utc_now()),
            )

    def list_pending_approvals(self, thread_id: str) -> list[dict]:
        import json as _json

        with self._lock:
            rows = self._connection.execute(
                "SELECT id, thread_id, title, kind, options_json, created_at "
                "FROM chat_approvals WHERE thread_id=? AND status='pending' ORDER BY created_at",
                (thread_id,),
            ).fetchall()
            return [
                {
                    "id": row[0],
                    "thread_id": row[1],
                    "title": row[2],
                    "kind": row[3],
                    "options": _json.loads(row[4]),
                    "created_at": row[5],
                }
                for row in rows
            ]

    def resolve_approval(self, approval_id: str, option_id: str | None, timeout: bool = False) -> None:
        status = "timeout" if timeout else "decided"
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE chat_approvals SET status=?, option_id=?, decided_at=? WHERE id=?",
                (status, option_id, _utc_now(), approval_id),
            )

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _values(record: ActivitySession) -> tuple[object, ...]:
        values: list[object] = []
        for column in _COLUMNS:
            value = getattr(record, column)
            values.append(int(value) if isinstance(value, bool) else value)
        return tuple(values)

    @staticmethod
    def _record(row: sqlite3.Row) -> ActivitySession:
        values = dict(row)
        values["source_missing"] = bool(values["source_missing"])
        values["archived"] = bool(values["archived"])
        return ActivitySession(**values)
