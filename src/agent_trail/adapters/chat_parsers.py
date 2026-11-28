"""无头驱动各 Agent CLI 的聊天后端：argv 构造 + stdout 行解析。

每种 CLI 一行一个 JSON 事件（或纯文本行），这里统一转成 ChatEvent。
解析必须防御式：未知行、未知字段一律忽略，绝不抛异常打断会话流。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from ..models import ChatEvent


@dataclass(frozen=True)
class ChatBackend:
    """一种 Agent CLI 的无头驱动方式。

    prompt_via="stdin"：ChatManager 把 prompt 写进 stdin（避开 Windows
    约 32k 字符的命令行上限，也不经过 shell）；"argv" 则把 prompt 作为
    最后一个 argv 元素追加，prompt 长度由 ChatSupport.max_prompt_chars
    封顶（Windows 安全值）。
    """

    executable: str
    build_argv: Callable[[str | None], list[str]]
    parse_line: Callable[[str], ChatEvent | None]
    prompt_via: str = "stdin"


def _json_dict(raw: str) -> dict | None:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _blocks_text(content: object) -> str:
    """从 content 块数组里拼接文本块；字符串直接返回。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


# --- claude-code -----------------------------------------------------------
# `claude -p --output-format stream-json --verbose`：stdin 收 prompt，
# stdout 逐行 JSON；assistant 行是完整消息，result 行带最终 session_id。


def _claude_argv(resume_ref: str | None) -> list[str]:
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    if resume_ref:
        argv += ["--resume", resume_ref]
    return argv


def _claude_parse(raw: str) -> ChatEvent | None:
    value = _json_dict(raw)
    if value is None:
        return None
    kind = value.get("type")
    if kind == "assistant":
        message = value.get("message")
        text = _blocks_text(message.get("content") if isinstance(message, dict) else None)
        if text:
            return ChatEvent(type="text", text=text)
        return None
    if kind == "result":
        session_ref = value.get("session_id")
        session_ref = str(session_ref) if session_ref else None
        if value.get("is_error"):
            return ChatEvent(
                type="error",
                text=str(value.get("result") or "agent reported an error"),
                session_ref=session_ref,
            )
        text = str(value.get("result") or "")
        return ChatEvent(type="done", text=text, session_ref=session_ref)
    # system / user / stream_event 等行不进对话流（未开启 partial，不会重复）。
    return None


# --- codex -----------------------------------------------------------------
# `codex exec --json <prompt>`（argv 传 prompt）；恢复用
# `codex exec resume <id> --json <prompt>`。事件形如
# {"type":"thread.started","thread_id":...} / {"type":"item.completed","item":...}。


def _codex_argv(resume_ref: str | None) -> list[str]:
    argv = ["codex", "exec"]
    if resume_ref:
        argv += ["resume", resume_ref]
    argv += ["--json"]
    return argv


def _codex_parse(raw: str) -> ChatEvent | None:
    value = _json_dict(raw)
    if value is None:
        return None
    kind = value.get("type")
    if kind == "thread.started":
        thread_id = value.get("thread_id")
        return ChatEvent(type="text", text="", session_ref=str(thread_id) if thread_id else None)
    if kind == "item.completed":
        item = value.get("item")
        if not isinstance(item, dict):
            return None
        if item.get("item_type") == "agent_message":
            text = str(item.get("text") or "")
            if text:
                return ChatEvent(type="text", text=text)
        return None
    if kind in ("turn.failed", "error"):
        message = value.get("message") or value.get("error") or "agent failed"
        return ChatEvent(type="error", text=str(message))
    return None


# --- opencode --------------------------------------------------------------
# `opencode run <prompt>` 输出纯文本（非 JSON 事件流），逐行透传；
# `--session <id>` 续聊。session_ref 无法从 stdout 可靠取得，续聊能力
# 依赖 CLI 自身，MVP 阶段每次新开。


def _opencode_argv(resume_ref: str | None) -> list[str]:
    argv = ["opencode", "run"]
    if resume_ref:
        argv += ["--session", resume_ref]
    return argv


def _opencode_parse(raw: str) -> ChatEvent | None:
    return ChatEvent(type="text", text=raw + "\n")


# --- kimi-code -------------------------------------------------------------
# 真机 2026-11-28 验证（kimi 2.8.1）：
#   `kimi -p <prompt> --output-format stream-json`
#   事件形如 {"role":"assistant","content":"收到"} 与
#   {"role":"meta","type":"session.resume_hint","session_id":"session_…"}；
#   续聊用 `kimi -p <prompt> --session <id> --output-format stream-json`。
# -p 是带值选项，不能把 prompt 追加到末尾之后，故把 "-p" 放在 argv 末尾，
# 由 ChatManager 把 prompt 作为最后一个 argv 元素紧跟其后。


def _kimi_argv(resume_ref: str | None) -> list[str]:
    argv = ["kimi"]
    if resume_ref:
        argv += ["--session", resume_ref]
    argv += ["--output-format", "stream-json", "-p"]
    return argv


def _kimi_parse(raw: str) -> ChatEvent | None:
    value = _json_dict(raw)
    if value is None:
        return None
    role = value.get("role")
    if role == "assistant":
        content = value.get("content")
        if isinstance(content, str) and content:
            return ChatEvent(type="text", text=content)
        if isinstance(content, list):
            parts = [
                str(block.get("text"))
                for block in content
                if isinstance(block, dict) and block.get("text")
            ]
            if parts:
                return ChatEvent(type="text", text="\n".join(parts))
        return None
    if role == "meta" and value.get("type") == "session.resume_hint":
        session_id = value.get("session_id")
        return ChatEvent(type="text", text="", session_ref=str(session_id) if session_id else None)
    return None


def _claude_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="claude",
        build_argv=_claude_argv,
        parse_line=_claude_parse,
        prompt_via="stdin",
    )


def _codex_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="codex",
        build_argv=_codex_argv,
        parse_line=_codex_parse,
        prompt_via="argv",
    )


def _opencode_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="opencode",
        build_argv=_opencode_argv,
        parse_line=_opencode_parse,
        prompt_via="argv",
    )


def _kimi_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="kimi",
        build_argv=_kimi_argv,
        parse_line=_kimi_parse,
        prompt_via="argv",
    )


CHAT_BACKENDS: dict[str, ChatBackend] = {
    "claude-code": _claude_chat_backend(),
    "codex": _codex_chat_backend(),
    "opencode": _opencode_chat_backend(),
    "kimi-code": _kimi_chat_backend(),
}
