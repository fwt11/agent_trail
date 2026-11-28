from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import (
    ActivitySession,
    AdapterStatus,
    ChatSupport,
    ConversationContent,
    ConversationMessage,
    ResumePlan,
    ScanResult,
)
from .base import BaseAdapter
from .chat_parsers import _opencode_chat_backend
from .jsonl_utils import fingerprint_paths, to_utc_iso
from .sqlite_utils import connect_readonly


class OpenCodeAdapter(BaseAdapter):
    source = "opencode"
    adapter_version = 1

    @property
    def root(self) -> Path:
        # 平台差异（调研依据见 README 平台矩阵）：
        # - Linux/macOS: ~/.local/share/opencode（官方 troubleshooting 文档确认
        #   macOS 同 Linux，不走 Library/Application Support）
        # - Windows: %LOCALAPPDATA%/opencode（XDG data home 在 Windows 解析为
        #   %LOCALAPPDATA%）
        # - 官方覆盖变量 OPENCODE_DATA 优先于以上全部
        override = os.environ.get("OPENCODE_DATA")
        if override:
            return Path(override)
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA") or self.home / "AppData" / "Local"
            return Path(base) / "opencode"
        return self.home / ".local" / "share" / "opencode"

    @property
    def database_path(self) -> Path:
        return self.root / "opencode.db"

    @property
    def storage_path(self) -> Path:
        return self.root / "storage" / "session"

    def watch_roots(self) -> list[tuple[Path, bool]]:
        return [(self.database_path.parent, False), (self.storage_path, True)]

    def discover(self) -> AdapterStatus:
        available = self.database_path.is_file() or self.storage_path.is_dir()
        return AdapterStatus(source=self.source, kind="ok" if available else "unavailable")

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        legacy_paths = list(self.storage_path.glob("**/*.json")) if self.storage_path.is_dir() else []
        database_paths = [self.database_path, Path(f"{self.database_path}-wal")]
        fingerprint = fingerprint_paths(
            [path for path in database_paths if path.is_file()] + legacy_paths,
            self.adapter_version,
        )
        if fingerprint == previous_fingerprint:
            return ScanResult(
                AdapterStatus(source=self.source, kind="ok"), fingerprint, [], False
            )
        database_records: dict[str, dict[str, Any]] = {}
        if self.database_path.is_file():
            try:
                with closing(connect_readonly(self.database_path)) as connection:
                    rows = connection.execute(
                        "SELECT id, directory, title, time_created, time_updated, "
                        "summary_additions, summary_deletions, summary_files, time_archived "
                        "FROM session"
                    ).fetchall()
                for row in rows:
                    database_records[str(row[0])] = {
                        "workdir": row[1],
                        "title": row[2],
                        "created_at": row[3],
                        "updated_at": row[4],
                        "additions": row[5],
                        "deletions": row[6],
                        "files_changed": row[7],
                        "archived": row[8] is not None,
                        "locator": self.database_path,
                    }
            except sqlite3.Error as error:
                return ScanResult(self._error_status(error), None, [], True)
        sessions: list[ActivitySession] = []
        error_count = 0
        for session_id, record in database_records.items():
            try:
                sessions.append(self._record(session_id, record, fingerprint, now))
            except (TypeError, ValueError, OverflowError, OSError):
                error_count += 1
        database_ids = set(database_records)
        for path in legacy_paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                session_id = str(raw.get("id") or "")
                if not session_id or session_id in database_ids:
                    continue
                times = raw.get("time", {})
                summary = raw.get("summary", {})
                if not isinstance(times, dict) or not isinstance(summary, dict):
                    error_count += 1
                    continue
                record = {
                    "workdir": raw.get("directory"),
                    "title": raw.get("title"),
                    "created_at": times.get("created"),
                    "updated_at": times.get("updated"),
                    "additions": summary.get("additions"),
                    "deletions": summary.get("deletions"),
                    "files_changed": summary.get("files"),
                    "archived": bool(raw.get("archived", False)),
                    "locator": path,
                }
                sessions.append(self._record(session_id, record, fingerprint, now))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
                error_count += 1
        return ScanResult(
            AdapterStatus(
                source=self.source,
                kind="ok",
                record_count=len(sessions),
                error_count=error_count,
            ),
            fingerprint,
            sessions,
            True,
        )

    def _record(
        self,
        session_id: str,
        record: dict[str, Any],
        fingerprint: str,
        now: datetime,
    ) -> ActivitySession:
        if not record.get("workdir") or record.get("updated_at") is None:
            raise ValueError("missing session metadata")
        title = record.get("title")
        return ActivitySession(
            source=self.source,
            session_id=session_id,
            workdir=str(record["workdir"]),
            title=str(title) if title else None,
            title_source="native" if title else "fallback",
            created_at=to_utc_iso(record["created_at"]) if record.get("created_at") is not None else None,
            updated_at=to_utc_iso(record["updated_at"]),
            additions=record.get("additions"),
            deletions=record.get("deletions"),
            files_changed=record.get("files_changed"),
            source_locator=str(record["locator"]),
            resume_kind="cli",
            resume_ref=session_id,
            archived=bool(record.get("archived")),
            adapter_version=self.adapter_version,
            source_fingerprint=fingerprint,
            last_scanned_at=now.isoformat(),
        )

    def _error_status(self, error: sqlite3.Error) -> AdapterStatus:
        temporary = isinstance(error, sqlite3.OperationalError) and any(
            word in str(error).lower() for word in ("locked", "busy")
        )
        return AdapterStatus(
            source=self.source,
            kind="error" if temporary else "incompatible",
            message="database temporarily unavailable" if temporary else "database schema incompatible",
        )

    def resume_plan(self, session: ActivitySession) -> ResumePlan:
        if session.source_missing or session.resume_kind != "cli" or not session.resume_ref:
            return ResumePlan(kind="none", workdir=session.workdir, reason="source unavailable")
        return ResumePlan(
            kind="cli",
            executable="opencode",
            args=("--session", session.resume_ref),
            workdir=session.workdir,
        )

    def read_messages(self, session: ActivitySession) -> ConversationContent | None:
        # 正文两种载体（真机 2026-11 确认）：
        # - DB 会话（locator=opencode.db）：message/part 表，data 列是 JSON，
        #   形状与 storage 下的 JSON 文件一致
        # - legacy 会话（locator=storage/session/*.json）：storage/message/
        #   <sid>/msg_*.json + storage/part/<mid>/prt_*.json
        locator = session.source_locator
        if not locator:
            return None
        try:
            path = Path(locator)
        except (OSError, ValueError):
            return None
        if path == self.database_path and path.is_file():
            return self._messages_from_database(session.session_id)
        return self._messages_from_storage(session.session_id)

    def _messages_from_database(self, session_id: str) -> ConversationContent | None:
        try:
            with closing(connect_readonly(self.database_path)) as connection:
                rows = connection.execute(
                    "SELECT id, time_created, data FROM message "
                    "WHERE session_id=? ORDER BY time_created",
                    (session_id,),
                ).fetchall()
                part_rows = connection.execute(
                    "SELECT message_id, data FROM part WHERE session_id=? ORDER BY time_created",
                    (session_id,),
                ).fetchall()
        except sqlite3.Error:
            return None
        parts: dict[str, list[str]] = {}
        for message_id, raw in part_rows:
            payload = self._load_json(raw)
            if payload is None:
                continue
            text = self._part_text(payload)
            if text:
                parts.setdefault(str(message_id), []).append(text)
        parsed_rows: list[tuple] = []
        for message_id, created, raw in rows:
            payload = self._load_json(raw)
            if payload is None:
                continue
            parsed_rows.append((str(message_id), created, payload))
        return self._assemble(parsed_rows, parts, lambda row: to_utc_iso(row[1]))

    def _messages_from_storage(self, session_id: str) -> ConversationContent | None:
        message_dir = self.root / "storage" / "message" / session_id
        if not message_dir.is_dir():
            return None
        entries: list[tuple[str, int, dict]] = []
        for path in message_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            times = payload.get("time")
            created = times.get("created") if isinstance(times, dict) else None
            entries.append((path.name, int(created) if isinstance(created, (int, float)) else 0, payload))
        entries.sort(key=lambda item: (item[1], item[0]))
        part_root = self.root / "storage" / "part"
        parts: dict[str, list[str]] = {}
        for name, _created, payload in entries:
            message_id = str(payload.get("id") or name)
            texts: list[str] = []
            for path in sorted(part_root.joinpath(message_id).glob("*.json")):
                try:
                    blob = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                if not isinstance(blob, dict):
                    continue
                text = self._part_text(blob)
                if text:
                    texts.append(text)
            if texts:
                parts[message_id] = texts
        rows = [
            (str(payload.get("id") or name), created, payload)
            for name, created, payload in entries
        ]
        return self._assemble(rows, parts, lambda row: to_utc_iso(row[1]))

    def _assemble(
        self,
        rows: list[tuple],
        parts: dict[str, list[str]],
        stamp_of,
    ) -> ConversationContent:
        messages: list[ConversationMessage] = []
        for message_id, _created, payload in rows:
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            text = "\n".join(parts.get(str(message_id), []))
            if not text:
                continue
            messages.append(
                ConversationMessage(
                    role=role,
                    text=text,
                    timestamp=stamp_of((message_id, _created, payload)),
                )
            )
        return ConversationContent(messages=messages, truncated=False)

    @staticmethod
    def _load_json(raw: object) -> dict | None:
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _part_text(payload: dict) -> str:
        # synthetic 部件是 CLI 注入的系统文本（提醒/指令），不属于对话正文。
        if payload.get("type") != "text" or payload.get("synthetic"):
            return ""
        text = payload.get("text")
        return text if isinstance(text, str) and text.strip() else ""

    def chat_support(self) -> ChatSupport | None:
        # `opencode run` 是官方无头模式（见 chat_parsers）；argv 传 prompt，
        # 封在 Windows 安全值。
        return ChatSupport(executable="opencode", max_prompt_chars=8000)
