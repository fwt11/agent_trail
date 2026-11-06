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

def _detect_locale() -> str:
    override = os.environ.get("AGENT_TRAIL_LANG")
    if override:
        return "zh-CN" if override.lower().startswith("zh") else "en"
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value:
            return "zh-CN" if value.lower().startswith("zh") else "en"
    return "en"


LOCALE = _detect_locale()

STRINGS = {
    "zh-CN": {
        "app.title": "Agent 足迹",
        "app.tooltip": "跨 Agent 的工作足迹时间线",
        "status.no_agent": "状态：未找到 agent-trail（请安装或设置 AGENT_TRAIL_BIN）",
        "status.running": "状态：运行中 · 127.0.0.1:{port}",
        "status.starting": "状态：启动中…",
        "status.crashed": "状态：异常退出（已自动重试 {n} 次）",
        "status.stopped": "状态：已停止",
        "menu.open": "打开控制台",
        "menu.refresh": "立即刷新",
        "menu.stop": "停止服务",
        "menu.start": "启动服务",
        "menu.autostart": "开机自启",
        "menu.quit": "退出",
        "install.removed": "已移除 {path}",
        "install.done": "已安装 {path}",
        "error.no_icon": "缺少图标文件：{path}",
        "error.already_running": "Agent 足迹 托盘已在运行",
        "error.no_gi": "缺少 GI 绑定，无法启动托盘：",
        "error.original": "原始错误：{error}",
    },
    "en": {
        "app.title": "Agent Trail",
        "app.tooltip": "Cross-agent work timeline",
        "status.no_agent": "Status: agent-trail not found (install it or set AGENT_TRAIL_BIN)",
        "status.running": "Status: running · 127.0.0.1:{port}",
        "status.starting": "Status: starting…",
        "status.crashed": "Status: exited unexpectedly ({n} automatic retries)",
        "status.stopped": "Status: stopped",
        "menu.open": "Open console",
        "menu.refresh": "Refresh now",
        "menu.stop": "Stop service",
        "menu.start": "Start service",
        "menu.autostart": "Launch at login",
        "menu.quit": "Quit",
        "install.removed": "Removed {path}",
        "install.done": "Installed {path}",
        "error.no_icon": "Missing icon file: {path}",
        "error.already_running": "Agent Trail tray is already running",
        "error.no_gi": "Missing GI bindings, cannot start the tray:",
        "error.original": "Original error: {error}",
    },
}


def tr(key: str, **params) -> str:
    text = STRINGS[LOCALE].get(key) or STRINGS["en"].get(key) or key
    for name, value in params.items():
        text = text.replace("{" + name + "}", str(value))
    return text


LOCK_PATH = Path.home() / ".agent-trail" / "tray.lock"
LOG_PATH = Path.home() / ".agent-trail" / "tray.log"
AUTOSTART_PATH = Path.home() / ".config" / "autostart" / "agent-trail-tray.desktop"
APP_MENU_PATH = Path.home() / ".local" / "share" / "applications" / "agent-trail-tray.desktop"

MODULE_DIR = Path(__file__).resolve().parent
ICON_PATH = MODULE_DIR / "icon.png"
DESKTOP_TEMPLATE = MODULE_DIR / "desktop.template"

URL_RE = re.compile(r"^http://127\.0\.0\.1:(\d+)/\?key=(\S+)$")


def emit(event: dict) -> None:
    """把事件按 JSON 行打到 stdout，供调试与端到端断言。"""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _looks_executable(path: str) -> bool:
    candidate = Path(path)
    return candidate.is_file() and os.access(candidate, os.X_OK)


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
        # serve 的拉起命令不依赖 PATH：executable 未显式给出（或给出的路径
        # 无效）时，退到 [sys.executable, -m, agent_trail]——托盘进程自己能
        # 跑，serve 就能跑（重执行场景的 PYTHONPATH 会被子进程继承）。
        self.executable = executable
        if self.executable is not None and not _looks_executable(self.executable):
            self.executable = None
        self._env_bin = os.environ.get("AGENT_TRAIL_BIN")
        if self._env_bin is not None and not _looks_executable(self._env_bin):
            self._env_bin = None
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
        return True  # 至少有 [sys.executable, -m agent_trail] 兜底

    def _serve_command(self) -> list[str]:
        for candidate in (self.executable, self._env_bin):
            if candidate and _looks_executable(candidate):
                return [candidate, "serve", "--no-browser"]
        return [sys.executable, "-m", "agent_trail", "serve", "--no-browser"]

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
                self._serve_command(),
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


def render_desktop_entry(executable: str, icon: Path,
                         template: Path = DESKTOP_TEMPLATE) -> str:
    text = template.read_text(encoding="utf-8")
    return text.replace("@EXEC@", f"{executable} tray").replace("@ICON@", str(icon))


def autostart_enabled(target: Path = AUTOSTART_PATH) -> bool:
    return target.exists()


def set_autostart(enabled: bool, executable: str, icon: Path,
                  target: Path = AUTOSTART_PATH,
                  template: Path = DESKTOP_TEMPLATE) -> None:
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_desktop_entry(executable, icon, template), encoding="utf-8")
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

    def rewrite_pid(self) -> None:
        """守护化后由最终进程调用：锁文件里写自己的 pid。"""
        if self._fd is not None:
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)  # ftruncate 不回卷写偏移，必须显式归零
            os.write(self._fd, f"{os.getpid()}\n".encode())

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None


# -- GTK 托盘与窗口 ---------------------------------------------------------


def run_gtk(icon_path: Path, show: bool = False) -> int:
    # WebKit 的 DMABUF/GBM 渲染路径在无 DRM 权限的会话里分配缓冲失败
    # （KMS: DRM_IOCTL_MODE_CREATE_DUMB failed），窗口渲染一片空白。
    # 默认退到软件合成；用户可用同名环境变量显式覆盖。
    os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
    import gi

    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("WebKit2", "4.1")
    except ValueError:
        gi.require_version("WebKit2", "4.0")
    from gi.repository import GLib, Gtk
    from gi.repository import WebKit2

    from .sni import MenuSpec, SniTray

    class TrayApp:
        def __init__(self) -> None:
            self.core = TrailCore(on_event=lambda event: GLib.idle_add(self.on_event, event))
            self.tray = SniTray(tr("app.title"), tr("app.tooltip"), icon_path)
            self.tray.bind_activate(self.open_console)
            self.window: Gtk.Window | None = None
            self.view: WebKit2.WebView | None = None
            self.pending_open = False
            self.update_menu()

        # 托盘菜单（声明式，状态变化整体重建）--------------------------------
        def update_menu(self) -> None:
            core = self.core
            if not core.available:
                status = tr("status.no_agent")
            elif core.state == "running":
                status = tr("status.running", port=core.port)
            elif core.state == "starting":
                status = tr("status.starting")
            elif core.state == "crashed":
                status = tr("status.crashed", n=core.max_restarts)
            else:
                status = tr("status.stopped")
            running = core.state == "running"
            service_live = core.process is not None
            self.tray.set_menu([
                MenuSpec(status, enabled=False),
                MenuSpec(tr("menu.open"), enabled=running, action=self.open_console),
                MenuSpec(tr("menu.refresh"), enabled=running, action=self.refresh_now),
                MenuSpec(is_separator=True),
                MenuSpec(tr("menu.stop") if service_live else tr("menu.start"),
                         enabled=core.available, action=self.toggle_service),
                MenuSpec(tr("menu.autostart"), is_check=True, checked=autostart_enabled(),
                         action=self.toggle_autostart),
                MenuSpec(is_separator=True),
                MenuSpec(tr("menu.quit"), action=self.quit),
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
                window = Gtk.Window(title=tr("app.title"))
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
            set_autostart(not autostart_enabled(), self._agent_trail_bin(), icon_path)
            self.update_menu()

        @staticmethod
        def _agent_trail_bin() -> str:
            return shutil.which("agent-trail") or sys.argv[0]

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


def _daemonize(log_path: Path) -> bool:
    """双 fork 脱离终端；返回 False 表示当前在父进程（调用方应立即退出）。

    守护进程的 stdout/stderr 追加到日志文件，stdin 接到 /dev/null。
    单实例锁的 fd 随 fork 继承，由守护进程继续持有。
    """
    if os.fork() > 0:
        return False
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    for fd in (log_fd, null_fd):
        if fd > 2:
            os.close(fd)
    return True


def _reexec_with_system_python(argv: list[str], guard: "SingleInstance | None" = None) -> int | None:
    """当前解释器缺 GI 绑定时，改用带 gi 的系统 Python 重执行自己。

    通过 PYTHONPATH 把包所在目录传给目标解释器（对 pip 正常安装和
    editable 源码布局都成立）；AGENT_TRAIL_TRAY_REEXEC 防止重执行环。
    """
    if os.environ.get("AGENT_TRAIL_TRAY_REEXEC"):
        return None
    candidates = ["/usr/bin/python3", "/usr/local/bin/python3", shutil.which("python3")]
    # 包含 agent_trail 包的目录（site-packages 或 editable 的 src/）
    import agent_trail
    package_root = str(Path(agent_trail.__file__).resolve().parents[1])
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen or not Path(candidate).is_file():
            continue
        seen.add(candidate)
        probe = subprocess.run(
            [candidate, "-c", "import gi"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if probe.returncode != 0:
            continue
        env = dict(os.environ)
        env["AGENT_TRAIL_TRAY_REEXEC"] = "1"
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            package_root + os.pathsep + existing if existing else package_root
        )
        if guard is not None:
            guard.release()  # 父进程缺 gi 不运行 GUI，锁必须让给重执行的子进程
        emit({"event": "tray_reexec", "python": candidate})
        # argv 不含子命令词（cli 层已剥离），重执行时补回
        return subprocess.call([candidate, "-m", "agent_trail", "tray", *argv], env=env)
    return None


def main(argv: list[str] | None = None) -> int:
    replay = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="agent-trail tray", description=f"{tr('app.title')} tray")
    parser.add_argument("--install", action="store_true", help="写入应用菜单入口")
    parser.add_argument("--uninstall", action="store_true", help="移除应用菜单入口与开机自启")
    parser.add_argument("--show", action="store_true", help="启动时同时打开控制台窗口")
    parser.add_argument("--foreground", action="store_true", help="前台运行（默认脱离终端后台运行，日志见 ~/.agent-trail/tray.log）")
    args = parser.parse_args(argv)

    if args.uninstall:
        for target in (APP_MENU_PATH, AUTOSTART_PATH):
            if target.exists():
                target.unlink()
                print(tr("install.removed", path=target))
        return 0
    if args.install:
        APP_MENU_PATH.parent.mkdir(parents=True, exist_ok=True)
        executable = shutil.which("agent-trail") or sys.argv[0]
        APP_MENU_PATH.write_text(render_desktop_entry(executable, ICON_PATH), encoding="utf-8")
        print(tr("install.done", path=APP_MENU_PATH))
        return 0

    if not ICON_PATH.is_file():
        print(tr("error.no_icon", path=ICON_PATH), file=sys.stderr)
        return 1
    # 锁在前台获取：重复启动时提示直接出现在用户终端；fd 由守护进程继承
    guard = SingleInstance()
    if not guard.acquire():
        print(tr("error.already_running"), flush=True)
        return 0
    if not args.foreground and not _daemonize(LOG_PATH):
        return 0  # 父进程：立即把终端还给用户；托盘在后台继续
    guard.rewrite_pid()  # 锁文件写守护进程的 pid，kill $(cat 锁文件) 才有效
    try:
        return run_gtk(ICON_PATH, show=args.show)
    except (ImportError, ValueError) as error:
        code = _reexec_with_system_python(replay, guard)
        if code is not None:
            return code
        print(
            tr("error.no_gi") + "\n"
            "  Debian/Ubuntu/deepin: sudo apt install python3-gi "
            "gir1.2-webkit2-4.1\n"
            + tr("error.original", error=error),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
