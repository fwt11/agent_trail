from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..models import ActivitySession, AdapterStatus, ResumePlan, ScanResult
from .base import BaseAdapter
from .jsonl_utils import fingerprint_paths, read_jsonl_metadata, to_utc_iso


class KimiCodeAdapter(BaseAdapter):
    source = "kimi-code"
    adapter_version = 1

    @property
    def root(self) -> Path:
        # Windows 已实测同布局（2026-11-08 用户机快照对比：session_index.jsonl
        # 与 sessions/wm_*/session_*/state.json 均在 ~/.kimi-code 下）。
        return self.home / ".kimi-code"

    @property
    def index_path(self) -> Path:
        return self.root / "session_index.jsonl"

    def watch_roots(self) -> list[tuple[Path, bool]]:
        return [(self.root, True)]

    def discover(self) -> AdapterStatus:
        return AdapterStatus(
            source=self.source,
            kind="ok" if self.index_path.is_file() else "unavailable",
        )

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        index = read_jsonl_metadata(
            self.index_path, {"sessionId", "sessionDir", "workDir"}
        )
        entries = [item for item in index.records if item.get("sessionId")]
        state_paths = [self._state_path(item) for item in entries]
        fingerprint = fingerprint_paths([self.index_path, *state_paths], self.adapter_version)
        if fingerprint == previous_fingerprint:
            return ScanResult(
                AdapterStatus(
                    source=self.source,
                    kind="ok",
                    record_count=len(entries),
                    error_count=index.error_count,
                ),
                fingerprint,
                [],
                False,
            )
        sessions: list[ActivitySession] = []
        error_count = index.error_count
        for entry in entries:
            state_path = self._state_path(entry)
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                state = {key: raw[key] for key in ("id", "cwd", "title", "createdAt", "updatedAt", "archived") if key in raw}
                session_id = str(entry["sessionId"])
                workdir = str(state.get("cwd") or entry.get("workDir") or "")
                if not workdir:
                    error_count += 1
                    continue
                title = state.get("title")
                updated = state.get("updatedAt") or state_path.stat().st_mtime
                created = state.get("createdAt")
                sessions.append(
                    ActivitySession(
                        source=self.source,
                        session_id=session_id,
                        workdir=workdir,
                        title=str(title) if title else None,
                        title_source="native" if title else "fallback",
                        created_at=to_utc_iso(created) if created is not None else None,
                        updated_at=to_utc_iso(updated),
                        source_locator=str(state_path),
                        resume_kind="cli",
                        resume_ref=session_id,
                        archived=bool(state.get("archived", False)),
                        adapter_version=self.adapter_version,
                        source_fingerprint=fingerprint,
                        last_scanned_at=now.isoformat(),
                    )
                )
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                error_count += 1
        status = AdapterStatus(
            source=self.source,
            kind="ok",
            record_count=len(sessions),
            error_count=error_count,
        )
        return ScanResult(status, fingerprint, sessions, True)

    def _state_path(self, entry: dict) -> Path:
        session_dir = Path(str(entry.get("sessionDir", "")))
        if not session_dir.is_absolute():
            session_dir = self.root / session_dir
        return session_dir / "state.json"

    def resume_plan(self, session: ActivitySession) -> ResumePlan:
        if session.source_missing or session.resume_kind != "cli" or not session.resume_ref:
            return ResumePlan(kind="none", workdir=session.workdir, reason="source unavailable")
        return ResumePlan(
            kind="cli",
            executable="kimi",
            args=("--session", session.resume_ref),
            workdir=session.workdir,
        )
