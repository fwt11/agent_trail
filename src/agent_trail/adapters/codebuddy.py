from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..models import ActivitySession, AdapterStatus, ResumePlan, ScanResult
from .base import BaseAdapter
from .jsonl_utils import fingerprint_paths, read_jsonl_metadata, to_utc_iso


class CodeBuddyAdapter(BaseAdapter):
    source = "codebuddy"
    adapter_version = 1

    @property
    def projects_path(self) -> Path:
        return self.home / ".codebuddy" / "projects"

    def watch_roots(self) -> list[tuple[Path, bool]]:
        return [(self.projects_path, True)]

    def discover(self) -> AdapterStatus:
        return AdapterStatus(
            source=self.source,
            kind="ok" if self.projects_path.is_dir() else "unavailable",
        )

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        paths = list(self.projects_path.glob("**/*.jsonl"))
        fingerprint = fingerprint_paths(paths, self.adapter_version)
        if fingerprint == previous_fingerprint:
            return ScanResult(
                AdapterStatus(source=self.source, kind="ok", record_count=len(paths)),
                fingerprint,
                [],
                False,
            )
        sessions: list[ActivitySession] = []
        error_count = 0
        for path in paths:
            result = read_jsonl_metadata(path, {"sessionId", "cwd", "timestamp"})
            error_count += result.error_count
            records = [item for item in result.records if item.get("timestamp") is not None]
            if not records:
                error_count += 1
                continue
            workdir = next((str(item["cwd"]) for item in reversed(result.records) if item.get("cwd")), "")
            if not workdir:
                error_count += 1
                continue
            session_id = str(records[0].get("sessionId") or path.stem)
            timestamps = [float(item["timestamp"]) for item in records]
            sessions.append(
                ActivitySession(
                    source=self.source,
                    session_id=session_id,
                    workdir=workdir,
                    title=None,
                    title_source="fallback",
                    created_at=to_utc_iso(min(timestamps)),
                    updated_at=to_utc_iso(max(timestamps)),
                    source_locator=str(path),
                    resume_kind="cli",
                    resume_ref=session_id,
                    adapter_version=self.adapter_version,
                    source_fingerprint=fingerprint,
                    last_scanned_at=now.isoformat(),
                )
            )
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
        if session.source_missing or session.resume_kind != "cli" or not session.resume_ref:
            return ResumePlan(kind="none", workdir=session.workdir, reason="source unavailable")
        return ResumePlan(
            kind="cli",
            executable="codebuddy",
            args=("--resume", session.resume_ref),
            workdir=session.workdir,
        )
