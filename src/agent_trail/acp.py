"""ACP（Agent Client Protocol）客户端：JSON-RPC over stdio（ndjson）。

方言要点（kimi 2.8.1 真机实测，2026-11-28）：
- initialize → agentCapabilities{loadSession,...}
- session/new → {sessionId, configOptions:[{id,category, currentValue, options}]}
  （category: model / thought_level / mode —— 模型、思考强度、权限模式）
- session/set_config_option {sessionId, configId, value}
- session/prompt {sessionId, prompt:[blocks]}，阻塞至返回 stopReason
- 更新通知 params.update.sessionUpdate：agent_message_chunk /
  agent_thought_chunk / tool_call / tool_call_update / session_info_update
- session/request_permission（需回复 outcome）→ 交互审批
- session/load {sessionId, cwd, mcpServers} → 应用内恢复历史会话

本模块只做协议：把响应/通知分发给回调；语义映射（事件→ChatEvent、
权限→UI 决策）由 ChatManager 完成。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from typing import Any, Callable

INITIALIZE_TIMEOUT = 20.0
SESSION_TIMEOUT = 30.0
PROTOCOL_VERSION = 1


class AcpError(Exception):
    """对用户可读的 ACP 失败原因。"""


class _PendingRequest:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.error: dict | None = None


class AcpClient:
    """一个 ACP agent 子进程 + 一个会话。

    回调（在读取线程上调用，应快速返回；权限决策除外）：
    - on_update(update: dict)：sessionUpdate 通知载荷
    - on_permission(params: dict) -> dict：审批请求，返回
      {"outcome": {"outcome": "selected", "optionId": ...}}；阻塞等待
      用户决策由调用方实现。
    - on_closed()：进程退出（崩溃/被杀）。
    """

    def __init__(
        self,
        command: list[str],
        cwd: str,
        on_update: Callable[[dict], None],
        on_permission: Callable[[dict], dict],
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.on_update = on_update
        self.on_permission = on_permission
        self.on_closed = on_closed or (lambda: None)
        self._process: subprocess.Popen | None = None
        self._pending: dict[int, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._writer_lock = threading.Lock()
        self._closed = threading.Event()
        self.session_id: str | None = None
        self.config_options: list[dict] = []
        # 旧版规范的 modes 字段（availableModes/currentModeId）——部分
        # 适配器（codex-acp 等）用它而非 configOptions 表达权限模式。
        self.modes: dict | None = None
        self._initialize_lock = threading.Lock()
        self._initialized = False

    # ---- 生命周期 ----

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        """启动子进程并完成 initialize 握手（幂等）。"""
        with self._initialize_lock:
            if self._initialized:
                return
            try:
                self._process = subprocess.Popen(
                    self.command,
                    cwd=self.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                )
            except OSError as error:
                raise AcpError(f"failed to start agent: {error}") from error
            reader = threading.Thread(target=self._read_loop, daemon=True, name="acp-reader")
            reader.start()
            response = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
                },
                timeout=INITIALIZE_TIMEOUT,
            )
            if response is None or response.get("error") is not None:
                self.close()
                detail = json.dumps((response or {}).get("error"), ensure_ascii=False)[:200]
                raise AcpError(f"agent initialize failed: {detail or 'timeout'}")
            self._initialized = True

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix" and process.pid:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass

    # ---- 会话 ----

    def new_session(self, cwd: str) -> dict:
        response = self._request(
            "session/new", {"cwd": cwd, "mcpServers": []}, timeout=SESSION_TIMEOUT
        )
        return self._adopt_session(response, "session/new")

    def load_session(self, acp_session_id: str, cwd: str) -> dict:
        response = self._request(
            "session/load",
            {"sessionId": acp_session_id, "cwd": cwd, "mcpServers": []},
            timeout=SESSION_TIMEOUT,
        )
        return self._adopt_session(response, "session/load")

    def _adopt_session(self, response: dict | None, label: str) -> dict:
        if response is None:
            raise AcpError(f"{label} timed out")
        if response.get("error") is not None:
            raise AcpError(f"{label} failed: {json.dumps(response['error'], ensure_ascii=False)[:200]}")
        result = response.get("result") or {}
        self.session_id = result.get("sessionId") or self.session_id
        self.config_options = list(result.get("configOptions") or [])
        self.modes = result.get("modes")
        # 旧版规范：agent 用 modes 表达权限模式而没有 configOptions——
        # 合成同形的 mode 选项，前端与设置逻辑无需分辨方言。
        if self.modes and not any(item.get("id") == "mode" for item in self.config_options):
            available = [
                {"value": mode.get("id"), "name": mode.get("name") or mode.get("id")}
                for mode in self.modes.get("availableModes", [])
                if mode.get("id")
            ]
            if available:
                self.config_options.append({
                    "id": "mode",
                    "name": "Mode",
                    "category": "mode",
                    "currentValue": self.modes.get("currentModeId"),
                    "options": available,
                })
        return result

    def set_config(self, config_id: str, value: str) -> None:
        """切换模型/思考强度/权限模式等会话配置；需已建立会话。

        兼容三种方言，按序尝试：
        1. 新版 configOptions：session/set_config_option {configId, value}
        2. mode 的旧版字段：session/set_mode {modeId}
        3. 新版模型：session/set_model {modelId}
        """
        if self.session_id is None:
            raise AcpError("session not established")
        has_option = any(item.get("id") == config_id for item in self.config_options)
        if not has_option and config_id not in ("mode", "model"):
            raise AcpError(f"unsupported config: {config_id}")
        error: dict | None = None
        if has_option:
            response = self._request(
                "session/set_config_option",
                {"sessionId": self.session_id, "configId": config_id, "value": value},
                timeout=SESSION_TIMEOUT,
            )
            if response is None:
                raise AcpError("set_config_option timed out")
            if response.get("error") is None:
                for option in self.config_options:
                    if option.get("id") == config_id:
                        option["currentValue"] = value
                return
            error = response.get("error")
        if config_id == "mode" and self.modes:
            response = self._request(
                "session/set_mode",
                {"sessionId": self.session_id, "modeId": value},
                timeout=SESSION_TIMEOUT,
            )
            if response is not None and response.get("error") is None:
                self.modes["currentModeId"] = value
                for option in self.config_options:
                    if option.get("id") == "mode":
                        option["currentValue"] = value
                return
            error = (response or {}).get("error") if response else None
        if config_id == "model":
            response = self._request(
                "session/set_model",
                {"sessionId": self.session_id, "modelId": value},
                timeout=SESSION_TIMEOUT,
            )
            if response is not None and response.get("error") is None:
                for option in self.config_options:
                    if option.get("id") == "model":
                        option["currentValue"] = value
                return
            error = (response or {}).get("error") if response else None
        raise AcpError(
            f"set {config_id} failed: {json.dumps(error, ensure_ascii=False)[:200] if error else 'timeout'}"
        )

    def current_config(self, config_id: str) -> str | None:
        for option in self.config_options:
            if option.get("id") == config_id:
                return option.get("currentValue")
        return None

    # ---- 对话 ----

    def prompt(self, text: str) -> str:
        """发送一轮文本输入，阻塞到回合结束，返回 stopReason。"""
        return self.prompt_blocks([{"type": "text", "text": text}])

    def prompt_blocks(self, blocks: list[dict]) -> str:
        """发送一轮多模态输入（text / image / resource_link 块）。

        期间的流式更新经 on_update 回调推送；审批请求经 on_permission。
        """
        if self.session_id is None:
            raise AcpError("session not established")
        response = self._request(
            "session/prompt",
            {"sessionId": self.session_id, "prompt": blocks},
            timeout=None,
        )
        if response is None:
            raise AcpError("prompt timed out")
        if response.get("error") is not None:
            raise AcpError(f"prompt failed: {json.dumps(response['error'], ensure_ascii=False)[:200]}")
        return str((response.get("result") or {}).get("stopReason") or "end_turn")

    def cancel(self) -> None:
        if self.session_id is None or not self.alive:
            return
        try:
            self._send_notification("session/cancel", {"sessionId": self.session_id})
        except OSError:
            pass

    # ---- 协议底层 ----

    def _request(self, method: str, params: dict, timeout: float | None) -> dict | None:
        request_id = uuid.uuid4().int >> 96
        pending = _PendingRequest()
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        except OSError as error:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise AcpError(f"agent pipe closed: {error}") from error
        if not pending.event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            return None
        with self._pending_lock:
            self._pending.pop(request_id, None)
        return {"result": pending.result, "error": pending.error}

    def _send(self, payload: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AcpError("agent not running")
        with self._writer_lock:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _send_notification(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict):
                continue
            self._dispatch(message)
        self._closed.set()
        with self._pending_lock:
            for pending in self._pending.values():
                pending.event.set()
        self.on_closed()

    def _dispatch(self, message: dict) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and ("result" in message or "error" in message) and method is None:
            with self._pending_lock:
                pending = self._pending.pop(int(request_id), None)
            if pending is not None:
                pending.result = message.get("result")
                pending.error = message.get("error")
                pending.event.set()
            return
        if method == "session/request_permission":
            try:
                outcome = self.on_permission(message.get("params") or {})
                self._send({"jsonrpc": "2.0", "id": request_id, "result": outcome})
            except Exception:
                # 审批回调异常时必须回应，否则 agent 永久等待。
                self._send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                })
            return
        if method == "ping":
            self._send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return
        if method is not None and request_id is not None:
            # 其余 agent→client 请求：明确拒绝而不是让对端干等。
            self._send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"method not supported: {method}"},
            })
            return
        update = (message.get("params") or {}).get("update")
        if update is not None:
            try:
                self.on_update(update)
            except Exception:
                pass  # 单条更新回调异常不中断读取线程
