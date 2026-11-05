from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ..models import ActivitySession, AdapterStatus, ResumePlan, ScanResult
from .base import BaseAdapter
from .jsonl_utils import fingerprint_paths, to_utc_iso
from .sqlite_utils import connect_readonly


class ZCodeAdapter(BaseAdapter):
    source = "zcode"
    adapter_version = 1

    @property
    def cli_path(self) -> Path:
        return self.home / ".zcode" / "cli" / "db" / "db.sqlite"

    @property
    def tasks_path(self) -> Path:
        return self.home / ".zcode" / "v2" / "tasks-index.sqlite"

    def watch_roots(self) -> list[tuple[Path, bool]]:
        return [(self.cli_path.parent, False), (self.tasks_path.parent, False)]

    def discover(self) -> AdapterStatus:
        available = self.cli_path.is_file() or self.tasks_path.is_file()
        return AdapterStatus(source=self.source, kind="ok" if available else "unavailable")

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        databases = [path for path in (self.cli_path, self.tasks_path) if path.is_file()]
        paths = [
            path
            for database in databases
            for path in (database, Path(f"{database}-wal"))
            if path.is_file()
        ]
        fingerprint = fingerprint_paths(paths, self.adapter_version)
        if fingerprint == previous_fingerprint:
            return ScanResult(
                AdapterStatus(source=self.source, kind="ok"), fingerprint, [], False
            )
        try:
            cli_records = self._cli_records() if self.cli_path.is_file() else {}
            task_records = self._task_records() if self.tasks_path.is_file() else {}
        except sqlite3.Error as error:
            return ScanResult(self._error_status(error), None, [], True)
        sessions: list[ActivitySession] = []
        error_count = 0
        for session_id in sorted(set(cli_records) | set(task_records)):
            try:
                cli = cli_records.get(session_id, {})
                task = task_records.get(session_id, {})
                workdir = cli.get("workdir") or task.get("workdir")
                updated = cli.get("updated_at") or task.get("updated_at")
                if not workdir or not updated:
                    error_count += 1
                    continue
                title = cli.get("title") or task.get("title")
                created_values = [
                    value
                    for value in (cli.get("created_at"), task.get("created_at"))
                    if value is not None
                ]
                sessions.append(
                    ActivitySession(
                        source=self.source,
                        session_id=session_id,
                        workdir=workdir,
                        title=title or None,
                        title_source="native" if title else "fallback",
                        created_at=min(created_values) if created_values else None,
                        updated_at=max(
                            value
                            for value in (cli.get("updated_at"), task.get("updated_at"))
                            if value is not None
                        ),
                        additions=cli.get("additions"),
                        deletions=cli.get("deletions"),
                        files_changed=cli.get("files_changed"),
                        source_locator=str(self.cli_path if cli else self.tasks_path),
                        resume_kind="none",
                        archived=bool(cli.get("archived") or task.get("archived")),
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

    def _cli_records(self) -> dict[str, dict]:
        with closing(connect_readonly(self.cli_path)) as connection:
            rows = connection.execute(
                "SELECT id, directory, title, summary_additions, summary_deletions, "
                "summary_files, time_created, time_updated, time_archived FROM session"
            ).fetchall()
        records = {}
        for row in rows:
            records[str(row[0])] = {
                "workdir": str(row[1]),
                "title": row[2] or None,
                "additions": row[3],
                "deletions": row[4],
                "files_changed": row[5],
                "created_at": to_utc_iso(row[6]),
                "updated_at": to_utc_iso(row[7]),
                "archived": row[8] is not None,
            }
        return records

    def _task_records(self) -> dict[str, dict]:
        with closing(connect_readonly(self.tasks_path)) as connection:
            rows = connection.execute(
                "SELECT task_id, workspace_path, title, created_at, updated_at, "
                "archived, deleted FROM tasks"
            ).fetchall()
        records = {}
        for row in rows:
            records[str(row[0])] = {
                "workdir": str(row[1]),
                "title": row[2] or None,
                "created_at": to_utc_iso(row[3]),
                "updated_at": to_utc_iso(row[4]),
                "archived": bool(row[5] or row[6]),
            }
        return records

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
            reason="ZCode resume route is not verified",
        )
