"""应用内聊天：双通道驱动各 Agent CLI。

- ACP 通道（主路径）：一个线程对应一个 ACP 会话（kimi 原生；claude/codex
  经官方/社区适配器），获得真流式、思考流、工具步骤、模型/思考强度/模式
  切换、应用内多轮上下文与交互审批。
- one-shot 通道（回退）：无头 `-p` 模式单轮驱动，适用于暂无 ACP 的 CLI。

安全模型（与 launcher.py 同源）：
- 只用 argv 数组启动白名单内的可执行文件，prompt 经 stdin 或单个 argv
  元素直传，绝不经过 shell
- cwd 必须存在；并发任务上限 4；one-shot 600s 墙钟超时
- ACP 子进程独立进程组；空闲 10 分钟自动回收
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .acp import AcpClient, AcpError
from .adapters.base import BaseAdapter
from .adapters.chat_parsers import CHAT_BACKENDS, ChatBackend
from .models import ChatThread
from .store import LocalStore

MAX_CONCURRENT_TASKS = 4
TASK_TIMEOUT_SECONDS = 600.0
CANCEL_GRACE_SECONDS = 8.0
DEFAULT_MAX_PROMPT_CHARS = 8000
TITLE_MAX_CHARS = 60
# 已结束任务在注册表里保留一段时间：SSE 晚到也能重放（断线重连同理）。
TASK_RETENTION_SECONDS = 900.0
TASK_RETENTION_MAX = 64
# ACP 会话空闲回收：线程 10 分钟无活动即关闭 agent 子进程。
ACP_IDLE_SECONDS = 600.0
ACP_REAP_INTERVAL = 60.0
APPROVAL_TIMEOUT_SECONDS = 300.0


class ChatError(Exception):
    """对外可读的聊天请求失败原因（HTTP 409/400 的 message）。"""


def _content_blocks(prompt: str, attachments: list[dict]) -> list[dict]:
    """构建 ACP session/prompt 内容块：文本 + 附件 resource_link。

    附件一律传本机绝对路径（kind=path 零拷贝引用原文件；kind=upload
    已由 server 落盘）——agent 用自己的读取工具打开，服务器不复制图片。
    """
    blocks: list[dict] = [{"type": "text", "text": prompt}]
    for att in attachments:
        uri = Path(att["path"]).resolve().as_uri()
        blocks.append({
            "type": "resource_link",
            "uri": uri,
            "name": att.get("name") or Path(att["path"]).name,
        })
    return blocks


def save_upload(uploads_dir: Path, thread_id: str, name: str, data: bytes) -> Path:
    """把上传内容落盘到 chat_uploads/<线程>/ 并返回路径（跨机场景专用）。"""
    import re as _re

    safe_name = _re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name or "file")[-120:] or "file"
    directory = uploads_dir / thread_id
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{uuid.uuid4().hex[:8]}-{safe_name}"
    target.write_bytes(data)
    return target


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _TurnBase:
    """一个聊天回合的共享事件流：按游标重放，不消费元素。"""

    def __init__(self) -> None:
        self.task_id = uuid.uuid4().hex
        self.thread_id = ""
        self.condition = threading.Condition()
        self.events: list[dict] = []
        self.finished_at: float | None = None
        self.assistant_text: list[str] = []
        self._cancel = threading.Event()

    @property
    def is_finished(self) -> bool:
        return self.finished_at is not None

    def _emit(self, event: dict) -> None:
        with self.condition:
            self.events.append(event)
            self.condition.notify_all()

    def finish(self) -> None:
        with self.condition:
            self.finished_at = time.monotonic()
            self.condition.notify_all()


class ChatManager:
    def __init__(
        self,
        store: LocalStore,
        adapters: list[BaseAdapter],
        uploads_dir: str | Path | None = None,
    ) -> None:
        self.store = store
        self.adapters = list(adapters)
        self.uploads_dir = Path(uploads_dir) if uploads_dir else None
        self._turns: dict[str, _TurnBase] = {}
        self._acp_clients: dict[str, tuple[AcpClient, float]] = {}
        self._approvals: dict[str, tuple[threading.Event, dict]] = {}
        # 每个来源最近一次会话的 configOptions（模型/思考强度/模式选项），
        # 供起始页在会话建立前渲染选择器。
        self._config_cache: dict[str, list[dict]] = {}
        # 起始页选中 agent 即预建的 ACP 会话（cwd=home）：发送时若线程
        # 工作目录一致则直接复用，避免"发消息后才出现配置/才建会话"。
        self._pending_sessions: dict[str, tuple[AcpClient, float]] = {}
        self._prefetching: set[str] = set()
        self._acp_lock = threading.Lock()
        self._lock = threading.Lock()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True, name="acp-reaper")
        self._reaper.start()

    # ---- 能力盘点 ----

    def capabilities(self) -> list[dict]:
        return [
            {
                "source": source,
                "executable": backend.executable,
                "available": shutil.which(backend.executable) is not None,
                "acp": backend.acp_command is not None,
            }
            for source, backend in CHAT_BACKENDS.items()
        ]

    def thread_config(self, thread_id: str) -> list[dict]:
        """活跃 ACP 会话的 configOptions（模型/思考强度/模式选择器数据源）。"""
        client = self._acp_client(thread_id)
        return client.config_options if client is not None else []

    def source_config(self, source: str) -> list[dict]:
        """该来源最近一次会话的 configOptions 缓存（起始页用）。"""
        return self._config_cache.get(source, [])

    def prefetch_session(self, source: str) -> None:
        """选中 agent 后台预建 ACP 会话：配置选项立即就绪，发送时复用。"""
        backend = CHAT_BACKENDS.get(source)
        if backend is None:
            return
        if shutil.which(backend.executable) is None:
            return
        command = backend.resolve_acp_command()
        if command is None:
            return
        with self._acp_lock:
            entry = self._pending_sessions.get(source)
            if entry is not None and entry[0].alive:
                self._pending_sessions[source] = (entry[0], time.monotonic())
                return
            if source in self._prefetching:
                return
            self._prefetching.add(source)

        def _work() -> None:
            try:
                client = AcpClient(
                    command=list(command),
                    cwd=str(Path.home()),
                    on_update=lambda update: None,  # 无活动回合，更新即弃
                    on_permission=lambda params: {"outcome": {"outcome": "cancelled"}},
                    on_closed=lambda: None,
                )
                client.start()
                client.new_session(str(Path.home()))
                if client.config_options:
                    self._config_cache[source] = client.config_options
                with self._acp_lock:
                    self._pending_sessions[source] = (client, time.monotonic())
            except AcpError:
                pass  # 预建失败静默：发送时按正常路径建会话并如实报错
            finally:
                with self._acp_lock:
                    self._prefetching.discard(source)

        threading.Thread(target=_work, daemon=True, name="acp-prefetch").start()

    # ---- 线程管理 ----

    def create_thread(
        self,
        source: str,
        workdir: str | None,
        title: str | None,
        config: dict[str, str] | None = None,
    ) -> ChatThread:
        backend = CHAT_BACKENDS.get(source)
        if backend is None:
            raise ChatError("chat unsupported for source")
        if shutil.which(backend.executable) is None:
            raise ChatError("executable not found")
        directory = Path(workdir).expanduser() if workdir else Path.home()
        if not directory.is_dir():
            raise ChatError("workdir does not exist")
        now = utc_now()
        thread = ChatThread(
            id=uuid.uuid4().hex,
            source=source,
            title=(title or "").strip()[:TITLE_MAX_CHARS] or "New chat",
            workdir=str(directory),
            created_at=now,
            updated_at=now,
            config=dict(config) if config else {},
        )
        self.store.create_thread(thread)
        return thread

    def set_thread_settings(self, thread_id: str, values: dict[str, str]) -> None:
        thread = self.store.get_thread(thread_id)
        if thread is None:
            raise ChatError("thread not found")
        self.store.set_thread_settings(thread_id, values)
        client = self._acp_client(thread_id)
        if client is None:
            return  # 下一次回合开始前由 _AcpTurn 应用
        updated = self.store.get_thread(thread_id)
        if updated is None:
            return
        for config_id, value in (updated.config or {}).items():
            if value and client.current_config(config_id) != value:
                try:
                    client.set_config(config_id, value)
                except AcpError:
                    pass  # 选择器数据可能含 agent 不支持的值；下回合再报
        self._touch_acp(thread_id)

    def start_turn(self, thread_id: str, prompt: str, attachments: list[dict] | None = None) -> str:
        thread = self.store.get_thread(thread_id)
        if thread is None:
            raise ChatError("thread not found")
        backend = CHAT_BACKENDS.get(thread.source)
        if backend is None:
            raise ChatError("chat unsupported for source")
        executable = shutil.which(backend.executable)
        if executable is None:
            raise ChatError(f"{backend.executable} not found in PATH")
        prompt = prompt.strip()
        if not prompt and not attachments:
            raise ChatError("empty prompt")
        max_chars = self._max_prompt_chars(thread.source)
        if len(prompt) > max_chars:
            raise ChatError(f"prompt exceeds {max_chars} characters")
        # 附件统一为路径引用：kind=path（零拷贝）与 kind=upload（已由
        # server 落盘）在 agent 视角等价——都是本机可读的文件路径。
        normalized: list[dict] = []
        for att in attachments or []:
            path_value = att.get("path")
            if not isinstance(path_value, str) or not path_value:
                raise ChatError("invalid attachment path")
            path_obj = Path(path_value).expanduser()
            if not path_obj.is_file():
                raise ChatError(f"attachment missing: {path_obj.name}")
            normalized.append({
                "kind": att.get("kind") or "upload",
                "name": str(att.get("name") or path_obj.name)[:200],
                "path": str(path_obj),
                "mime": str(att.get("mime") or ""),
            })
        if len(normalized) > 8:
            raise ChatError("too many attachments")
        self.store.append_chat_message(
            thread_id, "user", prompt, utc_now(), attachments=normalized or None
        )
        if thread.title in ("", "New chat", "新对话"):
            # 首条消息自动命名线程（AionUI 式体验）。
            title = prompt.replace("\n", " ")[:TITLE_MAX_CHARS].strip() or "New chat"
            self.store.rename_thread(thread.id, title, utc_now())
        if backend.acp_command is not None or backend.acp_command_builder is not None:
            acp_command = backend.resolve_acp_command()
            if acp_command is not None:
                turn: _TurnBase = _AcpTurn(
                    manager=self, thread=thread, prompt=prompt, backend=backend,
                    acp_command=acp_command, attachments=normalized,
                )
            else:
                # ACP 依赖（如 npx 适配器）不可用：回退 one-shot。
                turn = _ChatTask(
                    manager=self, thread=thread, prompt=prompt, backend=backend,
                    executable=executable, attachments=normalized,
                )
        else:
            turn = _ChatTask(
                manager=self, thread=thread, prompt=prompt, backend=backend,
                executable=executable, attachments=normalized,
            )
        with self._lock:
            self._turns[turn.task_id] = turn
        turn.start()
        return turn.task_id

    def subscribe(self, task_id: str) -> "_TurnBase | None":
        with self._lock:
            turn = self._turns.get(task_id)
        return turn

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            turn = self._turns.get(task_id)
        if turn is None or turn.is_finished:
            return False
        turn.request_cancel()
        return True

    def pending_approvals(self, thread_id: str) -> list[dict]:
        return self.store.list_pending_approvals(thread_id)

    def decide_approval(self, approval_id: str, option_id: str | None) -> bool:
        with self._acp_lock:
            entry = self._approvals.get(approval_id)
        if entry is None:
            return False
        decided, decision = entry
        decision["option_id"] = option_id
        decided.set()
        return True

    def _max_prompt_chars(self, source: str) -> int:
        adapter = next((item for item in self.adapters if item.source == source), None)
        support = adapter.chat_support() if adapter is not None else None
        return support.max_prompt_chars if support is not None else DEFAULT_MAX_PROMPT_CHARS

    def _prune_locked(self) -> None:
        """回收过期任务，避免注册表无限增长。"""
        now = time.monotonic()
        expired = [
            task_id
            for task_id, turn in self._turns.items()
            if turn.finished_at is not None and now - turn.finished_at > TASK_RETENTION_SECONDS
        ]
        for task_id in expired:
            del self._turns[task_id]
        while len(self._turns) > TASK_RETENTION_MAX:
            finished = sorted(
                (item for item in self._turns.items() if item[1].finished_at is not None),
                key=lambda item: item[1].finished_at,
            )
            if not finished:
                break
            del self._turns[finished[0][0]]

    # ---- ACP 会话生命周期 ----

    def _acp_client(self, thread_id: str) -> AcpClient | None:
        with self._acp_lock:
            entry = self._acp_clients.get(thread_id)
        if entry is None:
            return None
        client, _last_used = entry
        return client if client.alive else None

    def _ensure_acp_client(self, thread: ChatThread, acp_command: tuple[str, ...]) -> AcpClient:
        """复用或新建线程的 ACP 会话；优先：预建会话（工作目录一致时）
        → 恢复历史会话 → 新建。"""
        existing = self._acp_client(thread.id)
        if existing is not None:
            return existing
        # 起始页预建的会话：工作目录一致时直接绑定到线程（配置零等待）。
        with self._acp_lock:
            pending = self._pending_sessions.pop(thread.source, None)
        if pending is not None and pending[0].alive and pending[0].session_id:
            client = pending[0]
            if Path(client.cwd).resolve() == Path(thread.workdir).resolve():
                self._bind_client(thread, client)
                return client
            client.close()  # 工作目录不同：预建会话作废，按线程目录重建
        client = AcpClient(
            command=list(acp_command),
            cwd=thread.workdir,
            on_update=lambda update, thread_id=thread.id: self._handle_acp_update(thread_id, update),
            on_permission=lambda params, thread_id=thread.id: self._handle_acp_permission(thread_id, params),
            on_closed=lambda thread_id=thread.id: self._drop_acp_client(thread_id),
        )
        client.start()
        resumed = False
        if thread.acp_session_id:
            try:
                client.load_session(thread.acp_session_id, thread.workdir)
                resumed = True
            except AcpError:
                resumed = False  # 会话失效则新开
        if not resumed:
            client.new_session(thread.workdir)
        self._bind_client(thread, client)
        return client

    def _bind_client(self, thread: ChatThread, client: AcpClient) -> None:
        """把会话绑定到线程：重写回调路由 + 持久化 session 引用。"""
        client.on_update = lambda update, thread_id=thread.id: self._handle_acp_update(thread_id, update)
        client.on_permission = lambda params, thread_id=thread.id: self._handle_acp_permission(thread_id, params)
        client.on_closed = lambda thread_id=thread.id: self._drop_acp_client(thread_id)
        if client.session_id:
            self.store.set_thread_acp_session(thread.id, client.session_id)
        if client.config_options:
            self._config_cache[thread.source] = client.config_options
        with self._acp_lock:
            self._acp_clients[thread.id] = (client, time.monotonic())

    def _touch_acp(self, thread_id: str) -> None:
        with self._acp_lock:
            entry = self._acp_clients.get(thread_id)
            if entry is not None:
                self._acp_clients[thread_id] = (entry[0], time.monotonic())

    def _drop_acp_client(self, thread_id: str) -> None:
        with self._acp_lock:
            self._acp_clients.pop(thread_id, None)

    def _reap_loop(self) -> None:
        while True:
            time.sleep(ACP_REAP_INTERVAL)
            now = time.monotonic()
            with self._acp_lock:
                expired = [
                    thread_id
                    for thread_id, (client, last_used) in self._acp_clients.items()
                    if now - last_used > ACP_IDLE_SECONDS
                ]
                pending_expired = [
                    source
                    for source, (client, last_used) in self._pending_sessions.items()
                    if now - last_used > ACP_IDLE_SECONDS
                ]
            for thread_id in expired:
                with self._acp_lock:
                    entry = self._acp_clients.pop(thread_id, None)
                if entry is not None:
                    entry[0].close()
            for source in pending_expired:
                with self._acp_lock:
                    entry = self._pending_sessions.pop(source, None)
                if entry is not None:
                    entry[0].close()

    def close_all(self) -> None:
        with self._acp_lock:
            clients = [client for client, _last in self._acp_clients.values()]
            clients.extend(client for client, _last in self._pending_sessions.values())
            self._acp_clients.clear()
            self._pending_sessions.clear()
        for client in clients:
            client.close()

    # ---- ACP 更新 / 审批 ----

    def _active_turn_for(self, thread_id: str) -> _TurnBase | None:
        with self._lock:
            for turn in self._turns.values():
                if turn.thread_id == thread_id and turn.finished_at is None:
                    return turn
        return None

    def _handle_acp_update(self, thread_id: str, update: dict) -> None:
        turn = self._active_turn_for(thread_id)
        if turn is None:
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = str((update.get("content") or {}).get("text") or "")
            if text:
                turn.assistant_text.append(text)
                turn._emit({"type": "text", "text": text, "delta": True})
        elif kind == "agent_thought_chunk":
            text = str((update.get("content") or {}).get("text") or "")
            if text:
                turn._emit({"type": "thought", "text": text, "delta": True})
        elif kind in ("tool_call", "tool_call_update"):
            turn._emit({
                "type": "tool",
                "tool": {
                    "toolCallId": update.get("toolCallId"),
                    "title": update.get("title"),
                    "kind": update.get("kind"),
                    "status": update.get("status"),
                },
            })
        elif kind == "session_info_update":
            title = update.get("title")
            if title:
                self.store.rename_thread(thread_id, str(title)[:TITLE_MAX_CHARS], utc_now())
                turn._emit({"type": "title", "text": str(title)})

    def _handle_acp_permission(self, thread_id: str, params: dict) -> dict:
        """ACP 权限请求：登记待审批项并阻塞等待 UI 决策。"""
        options = params.get("options") or []
        tool_call = params.get("toolCall") or {}
        approval_id = uuid.uuid4().hex
        decided = threading.Event()
        decision: dict = {"option_id": None}
        self.store.create_approval(
            approval_id=approval_id,
            thread_id=thread_id,
            title=str(tool_call.get("title") or params.get("kind") or "tool"),
            kind=str(tool_call.get("kind") or ""),
            options_json=json.dumps(options, ensure_ascii=False),
        )
        with self._acp_lock:
            self._approvals[approval_id] = (decided, decision)
        try:
            if not decided.wait(APPROVAL_TIMEOUT_SECONDS):
                option_id = None  # 超时按取消处理
            else:
                option_id = decision["option_id"]
        finally:
            with self._acp_lock:
                self._approvals.pop(approval_id, None)
            self.store.resolve_approval(approval_id, option_id, timeout=option_id is None)
        if option_id is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}


class _AcpTurn(_TurnBase, threading.Thread):
    """ACP 回合：复用线程的长驻会话，prompt 阻塞至 stopReason。"""

    def __init__(
        self,
        manager: ChatManager,
        thread: ChatThread,
        prompt: str,
        backend: ChatBackend,
        acp_command: tuple[str, ...],
        attachments: list[dict] | None = None,
    ) -> None:
        threading.Thread.__init__(self, name="agent-trail-acp", daemon=True)
        _TurnBase.__init__(self)
        self.manager = manager
        self.thread = thread
        self.prompt = prompt
        self.backend = backend
        self.acp_command = acp_command
        self.content_blocks = _content_blocks(prompt, attachments or [])

    def request_cancel(self) -> None:
        self._cancel.set()
        client = self.manager._acp_client(self.thread.id)
        if client is not None:
            client.cancel()

    def run(self) -> None:
        try:
            self._run_turn()
        finally:
            self.finish()

    def _run_turn(self) -> None:
        self.thread_id = self.thread.id
        try:
            client = self.manager._ensure_acp_client(self.thread, self.acp_command)
        except AcpError as error:
            self._emit({"type": "error", "text": str(error)})
            return
        self.manager._touch_acp(self.thread.id)
        # 应用线程设置（通用键值：模型/思考强度/模式及各 agent 自有配置），
        # 失败继续用会话当前值。
        for config_id, value in (self.thread.config or {}).items():
            if value and client.current_config(config_id) != value:
                try:
                    client.set_config(config_id, value)
                except AcpError as error:
                    self._emit({"type": "error", "text": str(error)})
        try:
            stop_reason = client.prompt_blocks(self.content_blocks)
        except AcpError as error:
            self._emit({"type": "error", "text": str(error)})
            return
        text = "".join(self.assistant_text)
        if text.strip():
            self.manager.store.append_chat_message(self.thread.id, "assistant", text, utc_now())
        if client.session_id and client.session_id != self.thread.acp_session_id:
            self.manager.store.set_thread_acp_session(self.thread.id, client.session_id)
        self.manager._touch_acp(self.thread.id)
        self._emit({"type": "done", "text": stop_reason})


class _ChatTask(_TurnBase, threading.Thread):
    """one-shot 回合 = 一个 Agent 子进程 + 一段可重放的事件历史。

    事件追加进 self.events 并以 Condition 通知订阅者；SSE 处理器按
    游标重放，不消费元素，因此多客户端/断线重连都安全。
    """

    def __init__(
        self,
        manager: ChatManager,
        thread: ChatThread,
        prompt: str,
        backend: ChatBackend,
        executable: str,
        attachments: list[dict] | None = None,
    ) -> None:
        threading.Thread.__init__(self, name="agent-trail-chat", daemon=True)
        _TurnBase.__init__(self)
        self.manager = manager
        self.thread_id = thread.id
        self.thread = thread
        self.prompt = prompt
        self.backend = backend
        self.executable = executable
        self.attachments = attachments or []
        self._process: subprocess.Popen | None = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def request_cancel(self) -> None:
        self._cancel.set()
        self._terminate()

    def _terminate(self) -> None:
        """终止整个进程组：Agent CLI 会派生工具子进程，只杀主进程的话
        孙进程仍握着 stdout 管道，取消会迟迟不生效。"""
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "posix" and process.pid:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=CANCEL_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix" and process.pid:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass

    def run(self) -> None:
        try:
            self._run_process()
        finally:
            self.finish()

    def _run_process(self) -> None:
        prompt = self.prompt
        if self.attachments:
            # one-shot 通道没有 resource_link：把附件路径写进提示词，
            # 由 agent 的读取工具自行打开（零拷贝）。
            lines = "\n".join(f"- {att['path']}" for att in self.attachments)
            prompt = f"{self.prompt}\n\n[附件文件]\n{lines}"
        argv = self.backend.build_argv(self.thread.session_ref)
        if self.backend.prompt_via == "argv":
            argv.append(prompt)
        try:
            self._process = subprocess.Popen(
                argv,
                cwd=self.thread.workdir,
                stdin=subprocess.PIPE if self.backend.prompt_via == "stdin" else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,  # POSIX：独立进程组便于整组终止；Windows 忽略
            )
        except OSError as error:
            self._persist_assistant(f"failed to start {self.backend.executable}: {error}")
            self._emit({"type": "error", "text": f"failed to start agent: {error}"})
            return
        if self.backend.prompt_via == "stdin" and self._process.stdin is not None:
            # 大 prompt 可能超过管道缓冲，另起线程写入避免与 stdout 读取互锁。
            feeder = threading.Thread(target=lambda: self._feed_stdin(prompt), daemon=True)
            feeder.start()
        parse_line = self.backend.create_parser()
        deadline = datetime.now(timezone.utc).timestamp() + TASK_TIMEOUT_SECONDS
        assistant_parts: list[str] = []
        error_text: str | None = None
        session_ref = self.thread.session_ref
        timed_out = False
        assert self._process.stdout is not None
        for line in self._process.stdout:
            if self._cancel.is_set():
                break
            if datetime.now(timezone.utc).timestamp() > deadline:
                timed_out = True
                self.request_cancel()
                break
            event = parse_line(line.rstrip("\r\n"))
            if event is None:
                continue
            if event.session_ref and event.session_ref != session_ref:
                session_ref = event.session_ref
                self.manager.store.set_thread_session_ref(self.thread.id, session_ref)
            if event.type == "segment_end":
                # 一条消息结束（claude partial 边界）：下一段增量开新段。
                if assistant_parts and assistant_parts[-1]:
                    assistant_parts.append("")
                continue
            if event.type == "text" and event.text:
                if event.replace:
                    # 累积型事件（如 codex item.updated）：整段替换当前
                    # 助手文本，不再追加。
                    assistant_parts = [event.text]
                elif event.delta and assistant_parts:
                    # 同一条消息的增量分片：直接续写，不加段落分隔。
                    assistant_parts[-1] += event.text
                else:
                    assistant_parts.append(event.text)
                self._emit({
                    "type": "text",
                    "text": event.text,
                    "replace": event.replace,
                    "delta": event.delta,
                })
            elif event.type == "error":
                error_text = event.text
                self._emit({"type": "error", "text": event.text})
        if self._cancel.is_set():
            self._terminate()
        exit_code = self._wait_exit()
        if timed_out:
            error_text = error_text or f"chat timed out after {int(TASK_TIMEOUT_SECONDS)}s"
        if not self._cancel.is_set() and exit_code not in (0, None) and error_text is None and not assistant_parts:
            error_text = f"{self.backend.executable} exited with code {exit_code}"
        if error_text is not None and not assistant_parts:
            self._persist_assistant(error_text)
        elif assistant_parts:
            self._persist_assistant("\n\n".join(part for part in assistant_parts if part))
        self._emit({"type": "done", "session_ref": session_ref, "cancelled": self._cancel.is_set()})

    def _feed_stdin(self, prompt: str) -> None:
        assert self._process is not None and self._process.stdin is not None
        try:
            self._process.stdin.write(prompt + "\n")
            self._process.stdin.close()
        except (OSError, ValueError):
            pass

    def _wait_exit(self) -> int | None:
        process = self._process
        if process is None:
            return None
        try:
            return process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return None

    def _persist_assistant(self, text: str) -> None:
        self.manager.store.append_chat_message(self.thread.id, "assistant", text, utc_now())
