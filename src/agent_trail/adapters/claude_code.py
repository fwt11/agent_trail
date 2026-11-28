from __future__ import annotations

import json
import os
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
from .chat_parsers import _claude_chat_backend
from .jsonl_utils import fingerprint_paths, read_jsonl_records, to_utc_iso


class ClaudeCodeAdapter(BaseAdapter):
    source = "claude-code"
    adapter_version = 1

    @property
    def config_root(self) -> Path:
        # 官方行为：CLAUDE_CONFIG_DIR 重定向整个配置目录（含 projects/），
        # Windows 默认 %USERPROFILE%/.claude —— Path.home() 全平台一致。
        base = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(base) if base else self.home / ".claude"

    @property
    def root(self) -> Path:
        return self.config_root / "projects"

    def watch_roots(self) -> list[tuple[Path, bool]]:
        return [(self.root, True)]

    def discover(self) -> AdapterStatus:
        if not self.root.is_dir():
            return AdapterStatus(source=self.source, kind="unavailable")
        return AdapterStatus(
            source=self.source,
            kind="experimental",
            message="metadata parsed defensively; resume unverified",
        )

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        transcript_paths = list(self.root.glob("*/*.jsonl"))
        fingerprint = fingerprint_paths(transcript_paths, self.adapter_version)
        if fingerprint == previous_fingerprint:
            return ScanResult(
                AdapterStatus(source=self.source, kind=discovery.kind, message=discovery.message),
                fingerprint,
                [],
                False,
            )
        sessions: list[ActivitySession] = []
        error_count = 0
        for path in transcript_paths:
            try:
                record = self._transcript_metadata(path)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
                error_count += 1
                continue
            if record is None:
                continue
            session_id, cwd, created, updated, title = record
            if not session_id or not cwd:
                error_count += 1
                continue
            sessions.append(
                ActivitySession(
                    source=self.source,
                    session_id=session_id,
                    workdir=str(cwd),
                    title=title,
                    title_source="fallback" if title else "missing",
                    created_at=created,
                    updated_at=updated,
                    source_locator=str(path),
                    resume_kind="none",
                    resume_ref=None,
                    adapter_version=self.adapter_version,
                    source_fingerprint=fingerprint,
                    last_scanned_at=now.isoformat(),
                )
            )
        return ScanResult(
            AdapterStatus(
                source=self.source,
                kind="experimental",
                message=discovery.message,
                record_count=len(sessions),
                error_count=error_count,
            ),
            fingerprint,
            sessions,
            True,
        )

    def _transcript_metadata(self, path: Path) -> tuple[str, str, str | None, str, str | None] | None:
        """从一份 transcript JSONL 提取 (sessionId, cwd, created, updated, title)。

        只取元数据字段；正文行在 read_messages 时再惰性读取。
        """
        parsed = read_jsonl_records(path)
        session_id = ""
        cwd = ""
        title: str | None = None
        created: str | None = None
        updated: str | None = None
        for record in parsed.records:
            kind = record.get("type")
            timestamp = record.get("timestamp")
            stamp = to_utc_iso(timestamp) if isinstance(timestamp, str) else None
            if stamp:
                created = created or stamp
                updated = stamp
            if not session_id:
                value = record.get("sessionId")
                if isinstance(value, str):
                    session_id = value
            if not cwd:
                value = record.get("cwd")
                if isinstance(value, str) and value:
                    cwd = value
            if kind == "user" and title is None:
                message = record.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                text = content if isinstance(content, str) else _blocks_text(content)
                if text and not _is_injected_user_text(text):
                    title = text.strip().replace("\n", " ")[:120] or None
        if not updated:
            updated = to_utc_iso(path.stat().st_mtime)
        return session_id, cwd, created, updated, title

    def read_messages(self, session: ActivitySession) -> ConversationContent | None:
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
            kind = record.get("type")
            if kind not in ("user", "assistant"):
                continue
            if record.get("isSidechain") or record.get("isMeta"):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else _blocks_text(content)
            if not text:
                continue
            if kind == "user" and _is_injected_user_text(text):
                continue
            timestamp = record.get("timestamp")
            messages.append(
                ConversationMessage(
                    role="user" if kind == "user" else "assistant",
                    text=text,
                    timestamp=to_utc_iso(timestamp) if isinstance(timestamp, str) else None,
                )
            )
        return ConversationContent(messages=messages, truncated=False)

    def chat_support(self) -> ChatSupport | None:
        # `claude -p` 是官方无头模式（见 chat_parsers）；prompt 走 stdin，
        # 长度上限放宽到 100k 字符。
        return ChatSupport(executable="claude", max_prompt_chars=100_000)

    def resume_plan(self, session: ActivitySession) -> ResumePlan:
        return ResumePlan(
            kind="none",
            workdir=session.workdir,
            reason="Claude Code resume has not been verified",
        )


def _blocks_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts)


def _is_injected_user_text(text: str) -> bool:
    """过滤 Claude Code 塞进 user 槽位的注入文本（提醒/命令回显等）。"""
    return text.startswith(
        ("<system-reminder>", "<command-", "<local-command", "Caveat:", "<task-notification")
    )
