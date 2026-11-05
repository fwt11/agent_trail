from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import ActivitySession, AdapterStatus, ResumePlan, ScanResult
from .base import BaseAdapter
from .jsonl_utils import fingerprint_paths, to_utc_iso


class DeepSeekHarnessAdapter(BaseAdapter):
    source = "deepseek-harness"
    adapter_version = 1

    @property
    def index_path(self) -> Path:
        return self.home / ".dsh-tui" / "session-index.json"

    @property
    def database_path(self) -> Path:
        return self.home / ".dsh-tui" / "sessions.sqlite"

    @property
    def cache_path(self) -> Path:
        return self.home / ".dsh" / "storages" / "session_projcache" / "sessions"

    def watch_roots(self) -> list[tuple[Path, bool]]:
        return [(self.index_path.parent, False), (self.cache_path, False)]

    def discover(self) -> AdapterStatus:
        available = (
            self.index_path.is_file()
            or self.database_path.is_file()
            or self.cache_path.is_dir()
        )
        return AdapterStatus(source=self.source, kind="ok" if available else "unavailable")

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        cache_paths = list(self.cache_path.glob("*.json")) if self.cache_path.is_dir() else []
        paths = [
            path
            for path in (self.index_path, self.database_path)
            if path.is_file()
        ]
        fingerprint = fingerprint_paths([*paths, *cache_paths], self.adapter_version)
        if fingerprint == previous_fingerprint:
            return ScanResult(
                AdapterStatus(source=self.source, kind="ok", record_count=len(cache_paths)),
                fingerprint,
                [],
                False,
            )
        indexed: dict[str, dict[str, Any]] = {}
        error_count = 0
        try:
            raw_index = json.loads(self.index_path.read_text(encoding="utf-8"))
            raw_entries = raw_index.get("entries", {})
            entries = (
                list(raw_entries.items())
                if isinstance(raw_entries, dict)
                else [(str(item.get("id") or item.get("sessionId") or ""), item) for item in raw_entries]
            )
            for session_id, entry in entries:
                if not session_id or not isinstance(entry, dict):
                    error_count += 1
                    continue
                derived = entry.get("derived", {})
                if not isinstance(derived, dict):
                    error_count += 1
                    continue
                indexed[str(session_id)] = {
                    "title": derived.get("title"),
                    "identity": derived.get("identity"),
                }
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
            error_count += 1
        database_records: dict[str, dict[str, Any]] = {}
        if self.database_path.is_file():
            try:
                connection = sqlite3.connect(
                    f"{self.database_path.as_uri()}?mode=ro", uri=True
                )
                try:
                    rows = connection.execute("SELECT id, created_at, cwd FROM sessions")
                    database_records = {
                        str(row[0]): {"created_at": row[1], "cwd": row[2]} for row in rows
                    }
                finally:
                    connection.close()
            except sqlite3.Error:
                error_count += 1
        cache_records: dict[str, dict[str, Any]] = {}
        cache_files: dict[str, Path] = {}
        for path in cache_paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                identity = raw.get("record", {}).get("identity", {})
                if not isinstance(identity, dict):
                    error_count += 1
                    continue
                cache_id = path.stem
                normalized_id = cache_id.removeprefix("session-")
                if normalized_id in indexed or normalized_id in database_records:
                    cache_id = normalized_id
                cache_records[cache_id] = {
                    key: identity[key] for key in ("createdAt", "cwd") if key in identity
                }
                cache_files[cache_id] = path
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
                error_count += 1
        sessions: list[ActivitySession] = []
        session_ids = set(indexed) | set(database_records) | set(cache_records)
        for session_id in sorted(session_ids):
            index = indexed.get(session_id, {})
            cache_id = session_id
            database = database_records.get(session_id, {})
            cache = cache_records.get(cache_id, {})
            workdir = database.get("cwd") or cache.get("cwd")
            if not workdir:
                error_count += 1
                continue
            try:
                raw_created = database.get("created_at") or cache.get("createdAt")
                created_at = to_utc_iso(raw_created) if raw_created is not None else None
                source_path = cache_files.get(cache_id) or self.index_path
                updated_at = (
                    to_utc_iso(source_path.stat().st_mtime)
                    if source_path.is_file()
                    else created_at
                )
                if updated_at is None:
                    error_count += 1
                    continue
                title = index.get("title")
                sessions.append(
                    ActivitySession(
                        source=self.source,
                        session_id=session_id,
                        workdir=str(workdir),
                        title=str(title) if title else None,
                        title_source="native" if title else "fallback",
                        created_at=created_at,
                        updated_at=updated_at,
                        source_locator=str(source_path),
                        resume_kind="none",
                        resume_ref=None,
                        adapter_version=self.adapter_version,
                        source_fingerprint=fingerprint,
                        last_scanned_at=now.isoformat(),
                    )
                )
            except (OSError, TypeError, ValueError, OverflowError):
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

    def resume_plan(self, session: ActivitySession) -> ResumePlan:
        return ResumePlan(
            kind="none",
            workdir=session.workdir,
            reason="DeepSeek Harness runtime and plugins are incompatible",
        )
