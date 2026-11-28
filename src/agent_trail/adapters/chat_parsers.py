"""无头驱动各 Agent CLI 的聊天后端：argv 构造 + stdout 行解析。

每种 CLI 一行一个 JSON 事件（或纯文本行），这里统一转成 ChatEvent。
解析必须防御式：未知行、未知字段一律忽略，绝不抛异常打断会话流。

有状态解析器（claude 的 partial 增量、codex 的累积 item.updated）
通过 create_parser 工厂为每个聊天回合创建独立实例；无状态后端直接
复用纯函数。
"""

from __future__ import annotations

import json
import shutil
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

    create_parser 为每个回合产出独立的行解析器（有状态后端在此隔离状态）。

    acp_command 非空表示该 CLI 原生支持 ACP；acp_command_builder 用于
    依赖环境（npx 适配器/独立二进制）的 ACP 接入，返回 None 时回退
    one-shot。聊天主路径走 ACP 长驻会话（真流式/思考流/工具步骤/模型
    与思考强度与模式切换/交互审批/应用内多轮上下文）。
    """

    executable: str
    build_argv: Callable[[str | None], list[str]]
    create_parser: Callable[[], Callable[[str], ChatEvent | None]]
    prompt_via: str = "stdin"
    acp_command: tuple[str, ...] | None = None
    acp_command_builder: Callable[[], tuple[str, ...] | None] | None = None

    def resolve_acp_command(self) -> tuple[str, ...] | None:
        if self.acp_command_builder is not None:
            try:
                return self.acp_command_builder()
            except Exception:
                return None
        return self.acp_command


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


def _stateless(parse: Callable[[str], ChatEvent | None]) -> Callable[[], Callable[[str], ChatEvent | None]]:
    return lambda: parse


# --- claude-code -----------------------------------------------------------
# `claude -p --output-format stream-json --verbose --include-partial-messages`：
# stdin 收 prompt；stream_event 行携带 token 级增量（真流式），assistant 行
# 是完整消息（已发过增量时跳过，避免重复渲染），result 行带最终 session_id。


def _claude_argv(resume_ref: str | None) -> list[str]:
    argv = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    if resume_ref:
        argv += ["--resume", resume_ref]
    return argv


class _ClaudeStreamParser:
    def __init__(self) -> None:
        self._partial_pending = False

    def __call__(self, raw: str) -> ChatEvent | None:
        value = _json_dict(raw)
        if value is None:
            return None
        kind = value.get("type")
        if kind == "stream_event":
            event = value.get("event")
            if not isinstance(event, dict):
                return None
            if event.get("type") == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        self._partial_pending = True
                        return ChatEvent(type="text", text=text, delta=True)
            return None
        if kind == "assistant":
            message = value.get("message")
            text = _blocks_text(message.get("content") if isinstance(message, dict) else None)
            had_partial = self._partial_pending
            self._partial_pending = False
            if text and not had_partial:
                return ChatEvent(type="text", text=text)
            if had_partial:
                # 一条消息的增量结束：通知分段（后续增量属于下一条消息）。
                return ChatEvent(type="segment_end")
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
            return ChatEvent(type="done", text=str(value.get("result") or ""), session_ref=session_ref)
        return None


def _claude_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="claude",
        build_argv=_claude_argv,
        create_parser=_ClaudeStreamParser,
        prompt_via="stdin",
        # 官方适配器 claude-agent-acp（Anthropic/Zed/JetBrains，registry
        # 确认为二进制分发）：PATH 中存在即走 ACP 主通道，否则回退 one-shot。
        acp_command_builder=lambda: ("claude-agent-acp",)
        if shutil.which("claude-agent-acp")
        else None,
    )


# --- codex -----------------------------------------------------------------
# `codex exec --json <prompt>`（argv 传 prompt）；恢复用
# `codex exec resume <id> --json <prompt>`。新版事件流里 agent_message 会
# 经 item.updated 逐次给出"到目前为止"的完整文本（累积型），最终由
# item.completed 收口；旧版只有 item.completed。


def _codex_argv(resume_ref: str | None) -> list[str]:
    argv = ["codex", "exec"]
    if resume_ref:
        argv += ["resume", resume_ref]
    argv += ["--json"]
    return argv


class _CodexEventParser:
    def __init__(self) -> None:
        self._saw_updates = False

    def __call__(self, raw: str) -> ChatEvent | None:
        value = _json_dict(raw)
        if value is None:
            return None
        kind = value.get("type")
        if kind == "thread.started":
            thread_id = value.get("thread_id")
            return ChatEvent(type="text", text="", session_ref=str(thread_id) if thread_id else None)
        if kind in ("item.updated", "item.delta"):
            item = value.get("item")
            if not isinstance(item, dict):
                return None
            if item.get("item_type") == "agent_message":
                text = str(item.get("text") or "")
                if text:
                    self._saw_updates = True
                    return ChatEvent(type="text", text=text, replace=True)
            return None
        if kind == "item.completed":
            item = value.get("item")
            if not isinstance(item, dict):
                return None
            if item.get("item_type") == "agent_message":
                text = str(item.get("text") or "")
                if text:
                    if self._saw_updates:
                        return ChatEvent(type="text", text=text, replace=True)
                    return ChatEvent(type="text", text=text)
            return None
        if kind in ("turn.failed", "error"):
            message = value.get("message") or value.get("error") or "agent failed"
            return ChatEvent(type="error", text=str(message))
        return None


def _codex_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="codex",
        build_argv=_codex_argv,
        create_parser=_CodexEventParser,
        prompt_via="argv",
        # 官方适配器 @agentclientprotocol/codex-acp（registry 确认为 npx
        # 分发，需 Node）；二进制安装的 codex-acp 优先。
        acp_command_builder=lambda: ("codex-acp",)
        if shutil.which("codex-acp")
        else ("npx", "-y", "@agentclientprotocol/codex-acp")
        if shutil.which("npx") and shutil.which("codex")
        else None,
    )


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
    # opencode run 的 stdout 是一条连续的输出流，行与行之间直接续写。
    return ChatEvent(type="text", text=raw + "\n", delta=True)


def _opencode_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="opencode",
        build_argv=_opencode_argv,
        create_parser=_stateless(_opencode_parse),
        prompt_via="argv",
        # `opencode acp`：官方 ACP 服务（agentclientprotocol.com registry
        # 确认，v2.4.2 起原生支持）。
        acp_command=("opencode", "acp"),
    )


# --- codebuddy -------------------------------------------------------------
# 官方 ACP registry 确认：npx @tencent-ai/codebuddy-code --acp，即 CLI 的
# `codebuddy --acp`（用户已在 Zed 中实测可用）。one-shot 回退为 CC 风格
# 的 `-p/--output-format stream-json`（resume `--resume <id>` 真机验证过），
# 事件流防御式复用 claude 解析器。


def _codebuddy_argv(resume_ref: str | None) -> list[str]:
    argv = ["codebuddy"]
    if resume_ref:
        argv += ["--resume", resume_ref]
    argv += ["--output-format", "stream-json", "-p"]
    return argv


def _codebuddy_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="codebuddy",
        build_argv=_codebuddy_argv,
        create_parser=_ClaudeStreamParser,
        prompt_via="argv",
        acp_command=("codebuddy", "--acp"),
    )


# --- kimi-code -------------------------------------------------------------
# 真机 2026-11-28 验证（kimi 2.8.1）：
#   `kimi -p <prompt> --output-format stream-json`
#   事件形如 {"role":"assistant","content":"收到"} 与
#   {"role":"meta","type":"session.resume_hint","session_id":"session_…"}；
#   续聊用 `kimi -p <prompt> --session <id> --output-format stream-json`。
#   无增量事件（整条消息一次性输出，真机 16.8s 单行确认）；前端用
#   打字机渐显补足观感。
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


def _kimi_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="kimi",
        build_argv=_kimi_argv,
        create_parser=_stateless(_kimi_parse),
        prompt_via="argv",
        # `kimi acp`：官方 ACP 服务（真机 2026-11-28 全能力实证——真流式/
        # 思考流/工具步骤/模型与思考强度与模式 set_config_option/恢复）。
        acp_command=("kimi", "acp"),
    )


# --- minimax-code ----------------------------------------------------------
# `mcode acp`：官方 ACP 服务（官方文档 code.minimax.io/docs/cli/reference
# 确认，与 kimi 同款接入；本机未装 CLI，真机验证待用户环境）。
# one-shot 回退用 `mcode exec --output-format stream-json`（官方文档确认
# 存在）；其事件流为 plain dict stream-json，防御式复用 codex 解析器，
# 形状不匹配的行会被忽略，不影响 ACP 主路径。


def _mcode_argv(resume_ref: str | None) -> list[str]:
    argv = ["mcode", "exec", "--output-format", "stream-json"]
    if resume_ref:
        argv += ["--resume", resume_ref]
    return argv


def _mcode_chat_backend() -> ChatBackend:
    return ChatBackend(
        executable="mcode",
        build_argv=_mcode_argv,
        create_parser=_CodexEventParser,
        prompt_via="argv",
        acp_command=("mcode", "acp"),
    )


CHAT_BACKENDS: dict[str, ChatBackend] = {
    "claude-code": _claude_chat_backend(),
    "codex": _codex_chat_backend(),
    "opencode": _opencode_chat_backend(),
    "kimi-code": _kimi_chat_backend(),
    "minimax-code": _mcode_chat_backend(),
    "codebuddy": _codebuddy_chat_backend(),
}
