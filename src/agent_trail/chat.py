"""应用内聊天：无头驱动各 Agent CLI 的进程管理器。

安全模型（与 launcher.py 同源）：
- 只用 argv 数组启动白名单内的可执行文件（CHAT_BACKENDS + shutil.which
  双重校验），prompt 经 stdin 或单个 argv 元素直传，绝不经过 shell
- cwd 必须存在；并发任务上限 4；每个任务 600s 墙钟超时
- stderr 不并入对话流（避免日志噪音被当成回复）；进程异常退出时给出
  明确的错误事件，历史照常落盘
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

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


class ChatError(Exception):
    """对外可读的聊天请求失败原因（HTTP 409/400 的 message）。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatManager:
    def __init__(self, store: LocalStore, adapters: list[BaseAdapter]) -> None:
        self.store = store
        self.adapters = list(adapters)
        self._tasks: dict[str, _ChatTask] = {}
        self._lock = threading.Lock()

    # ---- 能力盘点 ----

    def capabilities(self) -> list[dict]:
        return [
            {
                "source": source,
                "executable": backend.executable,
                "available": shutil.which(backend.executable) is not None,
            }
            for source, backend in CHAT_BACKENDS.items()
        ]

    # ---- 线程管理 ----

    def create_thread(self, source: str, workdir: str | None, title: str | None) -> ChatThread:
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
        )
        self.store.create_thread(thread)
        return thread

    def start_turn(self, thread_id: str, prompt: str) -> str:
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
        if not prompt:
            raise ChatError("empty prompt")
        max_chars = self._max_prompt_chars(thread.source)
        if len(prompt) > max_chars:
            raise ChatError(f"prompt exceeds {max_chars} characters")
        with self._lock:
            self._prune_locked()
            running = sum(1 for task in self._tasks.values() if task.alive)
            if running >= MAX_CONCURRENT_TASKS:
                raise ChatError("too many running chats")
        self.store.append_chat_message(thread_id, "user", prompt, utc_now())
        if thread.title in ("", "New chat", "新对话"):
            # 首条消息自动命名线程（AionUI 式体验）。
            title = prompt.replace("\n", " ")[:TITLE_MAX_CHARS].strip() or "New chat"
            self.store.rename_thread(thread.id, title, utc_now())
        task = _ChatTask(
            manager=self,
            thread=thread,
            prompt=prompt,
            backend=backend,
            executable=executable,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        task.start()
        return task.task_id

    def subscribe(self, task_id: str) -> "_ChatTask | None":
        """SSE 订阅入口：返回任务对象（事件历史 + Condition）。

        事件不消费只追加，晚到的/断线重连的订阅者都能从头重放。
        """
        with self._lock:
            task = self._tasks.get(task_id)
        return task

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None or not task.alive:
            return False
        task.request_cancel()
        return True

    def _prune_locked(self) -> None:
        """回收过期任务，避免注册表无限增长。"""
        now = time.monotonic()
        expired = [
            task_id
            for task_id, task in self._tasks.items()
            if task.finished_at is not None
            and now - task.finished_at > TASK_RETENTION_SECONDS
        ]
        for task_id in expired:
            del self._tasks[task_id]
        while len(self._tasks) > TASK_RETENTION_MAX:
            finished = sorted(
                (item for item in self._tasks.items() if item[1].finished_at is not None),
                key=lambda item: item[1].finished_at,
            )
            if not finished:
                break
            del self._tasks[finished[0][0]]

    def _max_prompt_chars(self, source: str) -> int:
        adapter = next((item for item in self.adapters if item.source == source), None)
        support = adapter.chat_support() if adapter is not None else None
        return support.max_prompt_chars if support is not None else DEFAULT_MAX_PROMPT_CHARS


class _ChatTask(threading.Thread):
    """一个聊天回合 = 一个 Agent 子进程 + 一段可重放的事件历史。

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
    ) -> None:
        super().__init__(name="agent-trail-chat", daemon=True)
        self.task_id = uuid.uuid4().hex
        self.manager = manager
        self.thread = thread
        self.prompt = prompt
        self.backend = backend
        self.executable = executable
        self.condition = threading.Condition()
        self.events: list[dict] = []
        self._cancel = threading.Event()
        self._process: subprocess.Popen | None = None
        self.finished_at: float | None = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def is_finished(self) -> bool:
        return self.finished_at is not None

    def _emit(self, event: dict) -> None:
        with self.condition:
            self.events.append(event)
            self.condition.notify_all()

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
            with self.condition:
                self.finished_at = time.monotonic()
                self.condition.notify_all()

    def _run_process(self) -> None:
        argv = self.backend.build_argv(self.thread.session_ref)
        if self.backend.prompt_via == "argv":
            argv.append(self.prompt)
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
            feeder = threading.Thread(target=self._feed_stdin, daemon=True)
            feeder.start()
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
            event = self.backend.parse_line(line.rstrip("\r\n"))
            if event is None:
                continue
            if event.session_ref and event.session_ref != session_ref:
                session_ref = event.session_ref
                self.manager.store.set_thread_session_ref(self.thread.id, session_ref)
            if event.type == "text" and event.text:
                assistant_parts.append(event.text)
                self._emit({"type": "text", "text": event.text})
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
            self._persist_assistant("\n\n".join(assistant_parts))
        self._emit({"type": "done", "session_ref": session_ref, "cancelled": self._cancel.is_set()})

    def _feed_stdin(self) -> None:
        assert self._process is not None and self._process.stdin is not None
        try:
            self._process.stdin.write(self.prompt + "\n")
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
