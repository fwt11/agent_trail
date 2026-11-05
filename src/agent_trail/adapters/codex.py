from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ..models import ActivitySession, AdapterStatus, ResumePlan, ScanResult
from .base import BaseAdapter
from .jsonl_utils import fingerprint_paths, to_utc_iso
from .sqlite_utils import connect_readonly


class CodexAdapter(BaseAdapter):
    source = "codex"
    adapter_version = 1

    @property
    def root(self) -> Path:
        return self.home / ".codex"

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
