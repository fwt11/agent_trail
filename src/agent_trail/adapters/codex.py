from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

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
from .chat_parsers import _codex_chat_backend
from .jsonl_utils import fingerprint_paths, read_jsonl_records, to_utc_iso
from .sqlite_utils import connect_readonly


def _rollout_text(content: object) -> str:
    """拼接 rollout content 数组里的文本块（input_text/output_text/text）。"""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in ("input_text", "output_text", "text"):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


class CodexAdapter(BaseAdapter):
    source = "codex"
    adapter_version = 1

    @property
    def root(self) -> Path:
        # 官方行为（源码 codex-rs/utils/home-dir）：CODEX_HOME 优先，否则
        # dirs::home_dir()/.codex —— 全平台同布局（Windows = %USERPROFILE%/.codex）。
        override = os.environ.get("CODEX_HOME")
        return Path(override) if override else self.home / ".codex"

    @property
    def database_path(self) -> Path:
        return self.root / "state_5.sqlite"

    @property
    def sessions_path(self) -> Path:
        return self.root / "sessions"

    def watch_roots(self) -> list[tuple[Path, bool]]:
        return [(self.database_path.parent, False), (self.sessions_path, True)]

    def discover(self) -> AdapterStatus:
        available = self.database_path.is_file() or self.sessions_path.is_dir()
        return AdapterStatus(source=self.source, kind="ok" if available else "unavailable")

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        rollout_paths = list(self.sessions_path.glob("**/*.jsonl")) if self.sessions_path.is_dir() else []
        database_paths = [self.database_path, Path(f"{self.database_path}-wal")]
        fingerprint = fingerprint_paths(
            [path for path in database_paths if path.is_file()] + rollout_paths,
            self.adapter_version,
        )
        if fingerprint == previous_fingerprint:
            return ScanResult(
                AdapterStatus(source=self.source, kind="ok"), fingerprint, [], False
            )
        database_records = {}
        if self.database_path.is_file():
            try:
                with closing(connect_readonly(self.database_path)) as connection:
                    rows = connection.execute(
                        "SELECT id, title, cwd, created_at, updated_at, rollout_path, archived "
                        "FROM threads"
                    ).fetchall()
                for row in rows:
                    database_records[str(row[0])] = row[1:]
            except sqlite3.Error as error:
                return ScanResult(self._error_status(error), None, [], True)
        sessions: list[ActivitySession] = []
        error_count = 0
        for session_id, row in database_records.items():
            try:
                title, workdir, created, updated, rollout_path, archived = row
                if not workdir:
                    error_count += 1
                    continue
                sessions.append(
                    ActivitySession(
                        source=self.source,
                        session_id=session_id,
                        workdir=str(workdir),
                        title=str(title) if title else None,
                        title_source="native" if title else "fallback",
                        created_at=to_utc_iso(created),
                        updated_at=to_utc_iso(updated),
                        source_locator=str(rollout_path),
                        resume_kind="none",
                        resume_ref=None,
                        archived=bool(archived),
                        adapter_version=self.adapter_version,
                        source_fingerprint=fingerprint,
                        last_scanned_at=now.isoformat(),
                    )
                )
            except (TypeError, ValueError, OverflowError, OSError):
                error_count += 1
        for path in rollout_paths:
            try:
                metadata = self._rollout_metadata(path)
                if metadata is None:
                    error_count += 1
                    continue
                session_id = str(metadata.get("id") or "")
                if not session_id or session_id in database_records or not metadata.get("cwd"):
                    continue
                created = metadata.get("timestamp")
                sessions.append(
                    ActivitySession(
                        source=self.source,
                        session_id=session_id,
                        workdir=str(metadata["cwd"]),
                        title=None,
                        title_source="fallback",
                        created_at=to_utc_iso(created) if created is not None else None,
                        updated_at=to_utc_iso(path.stat().st_mtime),
                        source_locator=str(path),
                        resume_kind="none",
                        resume_ref=None,
                        adapter_version=self.adapter_version,
                        source_fingerprint=fingerprint,
                        last_scanned_at=now.isoformat(),
                    )
                )
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

    @staticmethod
    def _rollout_metadata(path: Path) -> dict | None:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("type") != "session_meta":
                    continue
                payload = value.get("payload")
                if not isinstance(payload, dict):
                    return None
                return {
                    key: payload[key] for key in ("id", "timestamp", "cwd") if key in payload
                }
        return None

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
        return ResumePlan(
            kind="none",
            workdir=session.workdir,
            reason="Codex requires an unverified directory-trust decision",
        )

    def read_messages(self, session: ActivitySession) -> ConversationContent | None:
        # 正文就是 rollout JSONL（DB 的 rollout_path 指向它）：取
        # response_item 且 payload.type=="message" 的 user/assistant 项。
        # developer/system 项与 <user_instructions>/<environment_context>
        # 这类注入文本跳过，避免污染时间线与全文检索。
        rollout = self._rollout_path(session)
        if rollout is None:
            return None
        try:
            parsed = read_jsonl_records(rollout)
        except OSError:
            return None
        messages: list[ConversationMessage] = []
        for record in parsed.records:
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _rollout_text(payload.get("content"))
            if not text:
                continue
            if role == "user" and text.startswith(
                ("<user_instructions>", "<environment_context>", "<permissions")
            ):
                continue
            timestamp = record.get("timestamp")
            messages.append(
                ConversationMessage(
                    role=role,
                    text=text,
                    timestamp=to_utc_iso(timestamp)
                    if isinstance(timestamp, (int, float, str))
                    else None,
                )
            )
        return ConversationContent(messages=messages, truncated=False)

    def _rollout_path(self, session: ActivitySession) -> Path | None:
        locator = session.source_locator
        if not locator:
            return None
        try:
            path = Path(locator)
        except (OSError, ValueError):
            return None
        return path if path.is_file() else None

    def chat_support(self) -> ChatSupport | None:
        # `codex exec` 是官方无头模式（见 chat_parsers），argv 传 prompt
        # 有命令行长度上限，封在 Windows 安全值。
        return ChatSupport(executable="codex", max_prompt_chars=8000)
