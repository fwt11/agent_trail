from __future__ import annotations

import json
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
from .jsonl_utils import fingerprint_paths, read_jsonl_metadata, read_jsonl_records, to_utc_iso


def _content_text(content: object) -> str:
    """拼接 kimi 消息 content 数组里的 text 块（think 等其他块跳过）。"""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts)


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

    def read_messages(self, session: ActivitySession) -> ConversationContent | None:
        # 正文在会话目录 agents/main/wire.jsonl（真机 2026-11 确认）：
        # - 用户输入 = context.append_message 且 message.origin.kind=="user"
        #   （"injection" 等其他 origin 是系统注入，跳过）
        # - 助手回复优先取 context.append_message 的 assistant 消息；个别
        #   协议版本只落流式 content.part 事件，此时回退聚合 part.text
        wire_path = self._wire_path(session)
        if wire_path is None:
            return None
        try:
            parsed = read_jsonl_records(wire_path)
        except OSError:
            return None
        if not parsed.records:
            return ConversationContent(messages=[], truncated=False)
        has_assistant_append = any(
            record.get("type") == "context.append_message"
            and isinstance(record.get("message"), dict)
            and record["message"].get("role") == "assistant"
            for record in parsed.records
        )
        messages: list[ConversationMessage] = []
        pending_text: list[str] = []
        pending_at: str | None = None

        def flush_pending() -> None:
            nonlocal pending_text, pending_at
            if pending_text:
                messages.append(
                    ConversationMessage(role="assistant", text="\n".join(pending_text), timestamp=pending_at)
                )
            pending_text = []
            pending_at = None

        for record in parsed.records:
            kind = record.get("type")
            moment = record.get("time")
            timestamp = to_utc_iso(moment) if isinstance(moment, (int, float, str)) else None
            if kind == "context.append_message":
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role == "assistant" and has_assistant_append:
                    text = _content_text(message.get("content"))
                    if text:
                        flush_pending()
                        messages.append(ConversationMessage(role="assistant", text=text, timestamp=timestamp))
                    continue
                origin = message.get("origin")
                origin_kind = origin.get("kind") if isinstance(origin, dict) else None
                if origin_kind == "user" and role == "user":
                    text = _content_text(message.get("content"))
                    if text:
                        flush_pending()
                        messages.append(ConversationMessage(role="user", text=text, timestamp=timestamp))
                continue
            if not has_assistant_append and kind == "context.append_loop_event":
                event = record.get("event")
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "content.part":
                    part = event.get("part")
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                        if not pending_at:
                            pending_at = timestamp
                        pending_text.append(str(part["text"]))
                elif event.get("type") in ("step.end", "turn.ended"):
                    flush_pending()
                continue
            if kind == "turn.ended":
                flush_pending()
        flush_pending()
        return ConversationContent(messages=messages, truncated=False)

    def _wire_path(self, session: ActivitySession) -> Path | None:
        try:
            locator = Path(session.source_locator)
        except (OSError, ValueError):
            return None
        if not locator.name:
            return None
        wire = locator.parent / "agents" / "main" / "wire.jsonl"
        return wire if wire.is_file() else None

    def chat_support(self) -> ChatSupport | None:
        # `kimi -p` 是官方无头模式，stream-json 事件与 --session 续聊已在
        # 真机验证（见 chat_parsers 注释）；argv 传 prompt，封在 Windows
        # 安全值。
        return ChatSupport(executable="kimi", max_prompt_chars=8000)
