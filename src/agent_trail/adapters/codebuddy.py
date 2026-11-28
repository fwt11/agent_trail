from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

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


def _blocks_text(content: object) -> str:
    """拼接 input_text/output_text/text 块；字符串 content 直接返回。"""
    if isinstance(content, str):
        return content
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


def _is_injected_user_text(text: str) -> bool:
    return text.startswith(
        ("<system-reminder>", "<system_warning>", "<command-", "<local-command", "Caveat:")
    )


def _latest_text(records: list[dict[str, Any]], record_type: str, field: str) -> str | None:
    for record in reversed(records):
        if record.get("type") != record_type:
            continue
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class CodeBuddyAdapter(BaseAdapter):
    source = "codebuddy"
    adapter_version = 2

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
        paths = [
            path
            for path in self.projects_path.glob("**/*.jsonl")
            if "subagents" not in path.relative_to(self.projects_path).parts
        ]
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
            result = read_jsonl_metadata(
<<<<<<< HEAD
                path, {"sessionId", "cwd", "timestamp", "type", "aiTitle", "summary"}
=======
                path, {"sessionId", "cwd", "timestamp", "type", "aiTitle"}
>>>>>>> 573792d (AionUI-style workbench: in-app chat, body viewer, full-text search (0.5.0))
            )
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
<<<<<<< HEAD
            title = _latest_text(result.records, "ai-title", "aiTitle") or _latest_text(
                result.records, "summary", "summary"
=======
            # `ai-title` 记录是 CodeBuddy 自己生成并展示的标题（真机 2026-11
            # 确认），按 sessionId 匹配后作为原生标题。
            ai_title = next(
                (
                    str(item["aiTitle"])
                    for item in result.records
                    if item.get("type") == "ai-title"
                    and item.get("aiTitle")
                    and (not item.get("sessionId") or str(item["sessionId"]) == session_id)
                ),
                None,
>>>>>>> 573792d (AionUI-style workbench: in-app chat, body viewer, full-text search (0.5.0))
            )
            timestamps = [float(item["timestamp"]) for item in records]
            sessions.append(
                ActivitySession(
                    source=self.source,
                    session_id=session_id,
                    workdir=workdir,
<<<<<<< HEAD
                    title=title,
                    title_source="native" if title else "fallback",
=======
                    title=ai_title,
                    title_source="native" if ai_title else "fallback",
>>>>>>> 573792d (AionUI-style workbench: in-app chat, body viewer, full-text search (0.5.0))
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

    def read_messages(self, session: ActivitySession) -> ConversationContent | None:
        # 正文就在 source_locator 指向的项目 JSONL 里：type=="message" 且
        # role 为 user/assistant 的记录，content 是 input_text/output_text
        # 块数组（真机 2026-11 确认）。
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
            role = record.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _blocks_text(record.get("content"))
            if not text:
                continue
            if role == "user" and _is_injected_user_text(text):
                continue
            timestamp = record.get("timestamp")
            messages.append(
                ConversationMessage(
                    role=role,
                    text=text,
                    timestamp=to_utc_iso(timestamp) if isinstance(timestamp, (int, float)) else None,
                )
            )
        return ConversationContent(messages=messages, truncated=False)
