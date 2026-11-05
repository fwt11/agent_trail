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


class MiniMaxCodeAdapter(BaseAdapter):
    source = "minimax-code"
    adapter_version = 1

    @property
    def database_path(self) -> Path:
        return self.home / ".minimax" / "v2" / "sqlite" / "runtime-state.sqlite"

    def watch_roots(self) -> list[tuple[Path, bool]]:
        return [(self.database_path.parent, False)]

    def discover(self) -> AdapterStatus:
        return AdapterStatus(
            source=self.source,
            kind="ok" if self.database_path.is_file() else "unavailable",
        )

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        wal_path = Path(f"{self.database_path}-wal")
        paths = [self.database_path, *( [wal_path] if wal_path.is_file() else [] )]
        fingerprint = fingerprint_paths(paths, self.adapter_version)
        if fingerprint == previous_fingerprint:
            return ScanResult(
                AdapterStatus(source=self.source, kind="ok"), fingerprint, [], False
            )
        try:
            with closing(connect_readonly(self.database_path)) as connection:
                rows = connection.execute(
                    "SELECT session_id, workspace_dir, project_workspace_dir, title, "
                    "created_at_ms, updated_at_ms, archived, project_id, parent_session_id "
                    "FROM local_runtime_sessions"
                ).fetchall()
                project_paths = dict(
                    connection.execute(
                        "SELECT project_id, workspace_dir FROM local_runtime_projects"
                    ).fetchall()
                )
                statistics = self._statistics(connection)
        except sqlite3.Error as error:
            return ScanResult(self._error_status(error), None, [], True)
        sessions: list[ActivitySession] = []
        error_count = 0
        for row in rows:
            try:
                session_id, workdir, project_workdir, title, created, updated, archived, project_id, parent_session_id = row
                workdir = workdir or project_workdir or project_paths.get(project_id)
                if not session_id or not workdir:
                    error_count += 1
                    continue
                stats = statistics.get(str(session_id), {})
                sessions.append(
                    ActivitySession(
                        source=self.source,
                        session_id=str(session_id),
                        workdir=str(workdir),
                        title=str(title) if title else None,
                        title_source="native" if title else "fallback",
                        created_at=to_utc_iso(created) if created is not None else None,
                        updated_at=to_utc_iso(updated),
                        additions=stats.get("additions"),
                        deletions=stats.get("deletions"),
                        files_changed=stats.get("files_changed"),
                        source_locator=str(self.database_path),
                        resume_kind="none" if parent_session_id else "cli",
                        resume_ref=None if parent_session_id else str(session_id),
                        archived=bool(archived),
                        adapter_version=self.adapter_version,
                        source_fingerprint=fingerprint,
                        last_scanned_at=now.isoformat(),
                    )
                )
            except (TypeError, ValueError, OverflowError, OSError):
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
    def _statistics(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_runtime_turn_diffs'"
        ).fetchone()
        if not exists:
            return {}
        statistics: dict[str, dict[str, int]] = {}
        rows = connection.execute(
            "SELECT session_id, file_changes_json FROM local_runtime_turn_diffs"
        )
        for session_id, raw in rows:
            try:
                changes = json.loads(raw)
                if not isinstance(changes, list):
                    continue
                totals = statistics.setdefault(
                    str(session_id), {"additions": 0, "deletions": 0, "files_changed": 0}
                )
                for change in changes:
                    if not isinstance(change, dict):
                        continue
                    for field in ("additions", "deletions"):
                        value = change.get(field)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            totals[field] += int(value)
                    totals["files_changed"] += 1
            except (json.JSONDecodeError, TypeError):
                continue
        return statistics

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
            executable="mcode",
            args=("--session", session.resume_ref),
            workdir=session.workdir,
        )
