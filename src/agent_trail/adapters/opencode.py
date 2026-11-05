from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import ActivitySession, AdapterStatus, ResumePlan, ScanResult
from .base import BaseAdapter
from .jsonl_utils import fingerprint_paths, to_utc_iso
from .sqlite_utils import connect_readonly


class OpenCodeAdapter(BaseAdapter):
    source = "opencode"
    adapter_version = 1

    @property
    def root(self) -> Path:
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
