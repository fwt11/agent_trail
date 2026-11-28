from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..models import (
    ActivitySession,
    AdapterStatus,
    ConversationContent,
    ConversationMessage,
    ResumePlan,
    ScanResult,
)
from .base import BaseAdapter
from .jsonl_utils import fingerprint_paths, read_jsonl_metadata, read_jsonl_records, to_utc_iso


def _message_text(content: object) -> str:
    """pi 的 message.content：字符串或块数组（text 块；thinking 等跳过）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts)


class PiAdapter(BaseAdapter):
    source = "pi"
    adapter_version = 1

    @property
    def root(self) -> Path:
        return self.home / ".pi" / "agent" / "sessions"

    def watch_roots(self) -> list[tuple[Path, bool]]:
        return [(self.root, True)]

    def discover(self) -> AdapterStatus:
        return AdapterStatus(
            source=self.source,
            kind="ok" if self.root.is_dir() else "unavailable",
        )

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        paths = list(self.root.glob("**/*.jsonl"))
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
            try:
                result = read_jsonl_metadata(
                    path, {"id", "timestamp", "cwd", "type", "name", "title"}
                )
                error_count += result.error_count
                header = next(
                    (
                        record
                        for record in result.records
                        if record.get("type") == "session" and record.get("id")
                    ),
                    None,
                )
                if header is None or not header.get("cwd"):
                    error_count += 1
                    continue
                timestamps = []
                for record in result.records:
                    if record.get("timestamp") is not None:
                        try:
                            timestamps.append(to_utc_iso(record["timestamp"]))
                        except (TypeError, ValueError, OverflowError, OSError):
                            error_count += 1
                title = next(
                    (
                        str(record.get("name") or record.get("title"))
                        for record in reversed(result.records)
                        if record.get("type")
                        in {"session_name", "session_title", "name", "title"}
                        and (record.get("name") or record.get("title"))
                    ),
                    None,
                )
                updated_at = max(timestamps) if timestamps else to_utc_iso(path.stat().st_mtime)
                sessions.append(
                    ActivitySession(
                        source=self.source,
                        session_id=str(header["id"]),
                        workdir=str(header["cwd"]),
                        title=title,
                        title_source="native" if title else "fallback",
                        created_at=min(timestamps) if timestamps else None,
                        updated_at=updated_at,
                        source_locator=str(path),
                        resume_kind="none",
                        resume_ref=None,
                        adapter_version=self.adapter_version,
                        source_fingerprint=fingerprint,
                        last_scanned_at=now.isoformat(),
                    )
                )
            except (OSError, UnicodeError, TypeError, ValueError, OverflowError):
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
            reason="Pi session restoration was not verified",
        )

    def read_messages(self, session: ActivitySession) -> ConversationContent | None:
        # pi 的会话 JSONL：type=="message" 记录内嵌 message={role, content}。
        locator = session.source_locator
        if not locator:
            return None
        try:
            path = Path(locator)
        except (OSError, ValueError):
            return None
        if not path.is_file():
            return None
        try:
            parsed = read_jsonl_records(path)
        except OSError:
            return None
        messages: list[ConversationMessage] = []
        for record in parsed.records:
            if record.get("type") != "message":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _message_text(message.get("content"))
            if not text:
                continue
            timestamp = record.get("timestamp")
            messages.append(
                ConversationMessage(
                    role=role,
                    text=text,
                    timestamp=to_utc_iso(timestamp) if isinstance(timestamp, str) else None,
                )
            )
        return ConversationContent(messages=messages, truncated=False)
