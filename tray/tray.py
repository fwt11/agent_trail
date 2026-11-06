#!/usr/bin/env python3
"""Agent 足迹 — 系统托盘壳（Linux / GI）。

以子进程方式管理 `agent-trail serve`，在内嵌 WebKit 窗口中展示控制台，
并在系统托盘（StatusNotifierItem）提供菜单。本文件不 import agent_trail：
可执行文件经 PATH 或环境变量 AGENT_TRAIL_BIN 解析，因此壳可以运行在带
GI 绑定的系统 Python 上（python3-gi + gir1.2-ayatanaappindicator3-0.1
+ gir1.2-webkit2-4.1），而后端继续使用任意装有 agent-trail 的解释器。

TrailCore 是平台无关的子进程管理器，不依赖 GUI，可单独测试；
TrayApp 是 GTK 胶水。Windows/macOS 后端（pystray + pywebview）预留为
同接口替换。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_TITLE = "Agent 足迹"
LOCK_PATH = Path.home() / ".agent-trail" / "tray.lock"
AUTOSTART_PATH = Path.home() / ".config" / "autostart" / "agent-trail-tray.desktop"
APP_MENU_PATH = Path.home() / ".local" / "share" / "applications" / "agent-trail-tray.desktop"

SCRIPT_PATH = Path(__file__).resolve()
ICON_PATH = SCRIPT_PATH.parent / "icon.png"
DESKTOP_TEMPLATE = SCRIPT_PATH.parent / "agent-trail-tray.desktop.in"

URL_RE = re.compile(r"^http://127\.0\.0\.1:(\d+)/\?key=(\S+)$")


def emit(event: dict) -> None:
    """把事件按 JSON 行打到 stdout，供调试与端到端断言。"""
    print(json.dumps(event, ensure_ascii=False), flush=True)


class TrailCore:
    """平台无关的服务子进程管理器；不含任何 GUI 依赖。

    状态机：stopped → starting → running → (crash) → starting …
    异常退出按 restart_delay×次数 退避自动重启，超过 max_restarts 后停在
    crashed 等待用户手动启动；用户主动停止（stop）永不自动重启。
    """

    def __init__(
        self,
        executable: str | None = None,
        on_event=None,
        *,
        max_restarts: int = 5,
        restart_delay: float = 3.0,
        stability_seconds: float = 300.0,
    ) -> None:
        self.executable = executable
        if self.executable is None:
            self.executable = os.environ.get("AGENT_TRAIL_BIN") or shutil.which("agent-trail")
        self.on_event = on_event or (lambda event: None)
        self.max_restarts = max_restarts
        self.restart_delay = restart_delay
        # 崩溃前若已稳定运行超过该时长，重启计数重新开始（长期稳定运行的服务
        # 偶发一次崩溃总能获得新的重试机会；启动即崩的 flapper 会被累计放弃）。
        self.stability_seconds = stability_seconds
        self.state = "stopped"  # stopped | starting | running | crashed
        self.url: str | None = None
        self.port: int | None = None
        self.key: str | None = None
        self.process: subprocess.Popen | None = None
        self._user_stop = False
        self._restarts = 0
        self._spawned_at = 0.0
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self.executable)

    def start(self, *, reset_attempts: bool = True) -> bool:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return True
            if not self.available:
                self.state = "stopped"
                self._emit({"event": "unavailable"})
                return False
            self._user_stop = False
            if reset_attempts:
                self._restarts = 0
            self.state = "starting"
            self.url = self.port = self.key = None
            self._spawned_at = time.monotonic()
            self.process = subprocess.Popen(
                [self.executable, "serve", "--no-browser"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            threading.Thread(
                target=self._pump, args=(self.process,), name="tray-pump", daemon=True
            ).start()
        self._emit({"event": "starting"})
        return True

    def stop(self) -> None:
        with self._lock:
            self._user_stop = True
            proc = self.process
            self.process = None  # 立即摘除：随后的 start() 必定拉起新进程
        if proc is None:
            if self.state != "stopped":
                self.state = "stopped"
                self._emit({"event": "stopped"})
            return
        self.state = "stopped"
        self.url = self.port = self.key = None
        try:
            proc.send_signal(signal.SIGINT)  # serve 以 KeyboardInterrupt 优雅停机
        except ProcessLookupError:
            pass
        threading.Timer(8.0, self._force_kill, args=(proc,)).start()
        self._emit({"event": "stopped"})

    def refresh(self) -> tuple[bool, str]:
        """触发一次手动刷新；在调用方线程执行（可能耗时一次全扫描）。"""
        with self._lock:
            base = f"http://127.0.0.1:{self.port}" if self.port else None
            key = self.key
        if not base or not key:
            return False, "服务未运行"
        request = urllib.request.Request(
            base + "/api/refresh",
            data=b"{}",
            method="POST",
            headers={
                "X-Agent-Trail-Key": key,
                "Origin": base,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status == 200, f"HTTP {response.status}"
        except urllib.error.HTTPError as error:
            return False, f"HTTP {error.code}"
        except Exception as error:  # 脱敏：只带异常类型名
            return False, type(error).__name__

    # -- 内部 ---------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        emit(event)
        try:
            self.on_event(event)
        except Exception:
            pass

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            match = URL_RE.match(line.strip())
            if match is None:
                continue
            with self._lock:
                if self.process is not proc:
                    break  # 进程已被替换，旧 URL 不入库
                self.port = int(match.group(1))
                self.key = match.group(2)
                self.url = match.group(0)
                self.state = "running"
            self._emit({"event": "running", "url": self.url, "port": self.port})
        code = proc.wait()
        with self._lock:
            if self.process is not proc:
                return  # 旧进程已被新进程替代，静默退场
            self.process = None
            self.url = self.port = self.key = None
            if self._user_stop:
                self.state = "stopped"
                self._emit({"event": "stopped"})
                return
            stable_run = time.monotonic() - self._spawned_at >= self.stability_seconds
            self._restarts = 1 if stable_run else self._restarts + 1
            if self._restarts > self.max_restarts:
                self.state = "crashed"
                self._emit({"event": "gave-up", "attempts": self.max_restarts})
                return
            delay = self.restart_delay * self._restarts
            attempt = self._restarts
            self.state = "starting"  # 退避等待期，UI 显示"启动中…"
        self._emit({"event": "exited", "code": code, "attempt": attempt, "restart_in": round(delay, 2)})
        timer = threading.Timer(delay, self._restart)
        timer.daemon = True
        timer.start()

    def _restart(self) -> None:
        with self._lock:
            if self._user_stop:
                return
        self.start(reset_attempts=False)

    @staticmethod
    def _force_kill(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


# -- 桌面入口文件 -----------------------------------------------------------


def render_desktop_entry(python_bin: str, script: Path, icon: Path,
                         template: Path = DESKTOP_TEMPLATE) -> str:
    text = template.read_text(encoding="utf-8")
    return (
        text.replace("@PYTHON@", python_bin)
        .replace("@SCRIPT@", str(script))
        .replace("@ICON@", str(icon))
    )


def autostart_enabled(target: Path = AUTOSTART_PATH) -> bool:
    return target.exists()


def set_autostart(enabled: bool, python_bin: str, script: Path, icon: Path,
                  target: Path = AUTOSTART_PATH,
                  template: Path = DESKTOP_TEMPLATE) -> None:
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            render_desktop_entry(python_bin, script, icon, template), encoding="utf-8"
        )
    elif target.exists():
        target.unlink()


class SingleInstance:
    """flock 单实例锁；进程退出时由内核自动释放。"""

    def __init__(self, path: Path = LOCK_PATH) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(self._fd)
            self._fd = None
            return False
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode())
        return True


# -- GTK 托盘与窗口 ---------------------------------------------------------


def run_gtk(icon_path: Path, show: bool = False) -> int:
    import gi

    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("WebKit2", "4.1")
    except ValueError:
        gi.require_version("WebKit2", "4.0")
    from gi.repository import GLib, Gtk
    from gi.repository import WebKit2

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sni import MenuSpec, SniTray

    class TrayApp:
        def __init__(self) -> None:
            self.core = TrailCore(on_event=lambda event: GLib.idle_add(self.on_event, event))
            self.tray = SniTray(APP_TITLE, "跨 Agent 的工作足迹时间线", icon_path)
            self.tray.bind_activate(self.open_console)
            self.window: Gtk.Window | None = None
            self.view: WebKit2.WebView | None = None
            self.pending_open = False
            self.update_menu()

        # 托盘菜单（声明式，状态变化整体重建）--------------------------------
        def update_menu(self) -> None:
            core = self.core
            if not core.available:
                status = "状态：未找到 agent-trail（请安装或设置 AGENT_TRAIL_BIN）"
            elif core.state == "running":
                status = f"状态：运行中 · 127.0.0.1:{core.port}"
            elif core.state == "starting":
                status = "状态：启动中…"
            elif core.state == "crashed":
                status = f"状态：异常退出（已自动重试 {core.max_restarts} 次）"
            else:
                status = "状态：已停止"
            running = core.state == "running"
            service_live = core.process is not None
            self.tray.set_menu([
                MenuSpec(status, enabled=False),
                MenuSpec("打开控制台", enabled=running, action=self.open_console),
                MenuSpec("立即刷新", enabled=running, action=self.refresh_now),
                MenuSpec(is_separator=True),
                MenuSpec("停止服务" if service_live else "启动服务",
                         enabled=core.available, action=self.toggle_service),
                MenuSpec("开机自启", is_check=True, checked=autostart_enabled(),
                         action=self.toggle_autostart),
                MenuSpec(is_separator=True),
                MenuSpec("退出", action=self.quit),
            ])

        # 状态事件 -----------------------------------------------------------
        def on_event(self, event: dict) -> bool:
            self.update_menu()
            if event.get("event") == "running":
                url = event["url"]
                if self.pending_open:
                    self.pending_open = False
                    self.show_window(url)
                elif self.window is not None and self.window.get_visible():
                    # 服务重启后 token 变化，窗口需要换到新地址
                    self.view.load_uri(url)
            return False  # GLib.idle_add 只跑一次

        # 动作 ---------------------------------------------------------------
        def open_console(self, *_args) -> None:
            if self.core.state == "running" and self.core.url:
                self.show_window(self.core.url)
            elif self.core.available:
                self.pending_open = True
                if self.core.state != "starting":
                    self.core.start()

        def show_window(self, url: str) -> None:
            if self.window is None:
                window = Gtk.Window(title=APP_TITLE)
                window.set_default_size(1280, 820)
                view = WebKit2.WebView()
                window.add(view)
                window.connect("delete-event", self.on_window_delete)
                window.show_all()
                self.window, self.view = window, view
            self.view.load_uri(url)
            self.window.present()

        def on_window_delete(self, *_args) -> bool:
            self.window.hide()
            return True  # 阻止销毁，窗口保活

        def toggle_service(self, *_args) -> None:
            if self.core.process is not None:
                self.core.stop()
            else:
                self.core.start()

        def toggle_autostart(self, *_args) -> None:
            set_autostart(not autostart_enabled(),
                          sys.executable, SCRIPT_PATH, icon_path)
            self.update_menu()

        def refresh_now(self, *_args) -> None:
            def worker() -> None:
                ok, _detail = self.core.refresh()
                GLib.idle_add(self.after_refresh, ok)

            threading.Thread(target=worker, name="tray-refresh", daemon=True).start()

        def after_refresh(self, ok: bool) -> bool:
            if ok and self.view is not None and self.window.get_visible():
                self.view.reload()
            return False

        def quit(self, *_args) -> None:
            # 清理路径任何一步挂死都不能阻拦退出：3 秒后强制终止兜底。
            threading.Timer(3.0, lambda: os._exit(0)).start()
            try:
                self.core.stop()
            except Exception:
                pass
            try:
                self.tray.close()
            except Exception:
                pass
            try:
                if self.window is not None:
                    self.window.destroy()
            except Exception:
                pass
            Gtk.main_quit()

    app = TrayApp()

    def on_unix_signal() -> bool:
        GLib.idle_add(app.quit)
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, on_unix_signal)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, on_unix_signal)

    app.core.start()  # 托盘启动即拉起服务
    if show:
        app.pending_open = True
    Gtk.main()
    return 0


# -- 入口 --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-trail-tray", description=f"{APP_TITLE} 托盘")
    parser.add_argument("--install", action="store_true", help="写入应用菜单入口")
    parser.add_argument("--uninstall", action="store_true", help="移除应用菜单入口与开机自启")
    parser.add_argument("--show", action="store_true", help="启动时同时打开控制台窗口")
    args = parser.parse_args(argv)

    if args.uninstall:
        for target in (APP_MENU_PATH, AUTOSTART_PATH):
            if target.exists():
                target.unlink()
                print(f"已移除 {target}")
        return 0
    if args.install:
        APP_MENU_PATH.parent.mkdir(parents=True, exist_ok=True)
        APP_MENU_PATH.write_text(
            render_desktop_entry(sys.executable, SCRIPT_PATH, ICON_PATH), encoding="utf-8"
        )
        print(f"已安装 {APP_MENU_PATH}")
        return 0

    if not ICON_PATH.is_file():
        print(f"缺少图标文件：{ICON_PATH}", file=sys.stderr)
        return 1
    guard = SingleInstance()
    if not guard.acquire():
        print(f"{APP_TITLE} 托盘已在运行", flush=True)
        return 0
    try:
        return run_gtk(ICON_PATH, show=args.show)
    except (ImportError, ValueError) as error:
        print(
            "缺少 GI 绑定，无法启动托盘：\n"
            "  Debian/Ubuntu/deepin: sudo apt install python3-gi "
            "gir1.2-ayatanaappindicator3-0.1 gir1.2-webkit2-4.1\n"
            f"  原始错误：{error}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
