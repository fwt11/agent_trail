from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path
from time import monotonic

from .adapters import default_adapters
from .chat import ChatManager
from .pathenv import augment_process_path
from .content import ContentIndexer
from .models import SessionFilter
from .scanner import ScanService
from .server import LocalServer
from .store import LocalStore
from .watcher import PollingWatcher, create_watcher


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-trail")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".agent-trail")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan")
    commands.add_parser("list")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--interval", type=float, default=30)
    serve.add_argument("--no-watch", action="store_true")
    serve.add_argument(
        "--no-content-index",
        action="store_true",
        help="disable local message-body indexing (privacy-first mode; "
        "in-app search and session message views stay off, chat still works)",
    )
    tray = commands.add_parser("tray", help="system tray app (Linux native; Windows/macOS via [tray] extra)")
    tray.add_argument("--show", action="store_true", help="open the console window at startup")
    tray.add_argument("--foreground", action="store_true", help="run in the foreground (default: detach with logs in ~/.agent-trail/tray.log)")
    tray.add_argument("--install", action="store_true", help="add an applications-menu entry")
    tray.add_argument("--uninstall", action="store_true", help="remove the menu entry and autostart")
    update = commands.add_parser("update", help="check PyPI and upgrade session-trail in-place")
    update.add_argument("--check", action="store_true", help="only check for updates, do not upgrade")
    commands.add_parser("version", help="print installed and latest versions")
    return parser


def main(argv: list[str] | None = None) -> int:
    # 桌面图标启动的进程 PATH 只有系统默认项，补全 Agent CLI 常见安装
    # 目录（~/.kimi-code/bin 等），否则控制台找不到任何 Agent。
    augment_process_path()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        if not 0 <= args.port <= 65535:
            parser.error("--port must be between 0 and 65535")
        if args.interval <= 0:
            parser.error("--interval must be positive")
    if args.command == "tray":
        if sys.platform != "linux":
            print("agent-trail tray is Linux-only for now "
                  "(Windows/macOS backends are planned).")
            return 1
        # 托盘是独立子进程形态，不需要打开本地索引；当前解释器缺 GI
        # 绑定时由 tray.main 内部重执行到带 gi 的系统 Python。
        from .tray.app import main as tray_main
        rest = list(argv) if argv is not None else sys.argv[1:]
        if rest and rest[0] == "tray":
            rest = rest[1:]  # 子命令词由 cli 层消费，不传给 tray 解析器
        return tray_main(rest)
    if args.command in ("update", "version"):
        from . import update as update_mod

        current = update_mod.current_version()
        latest = update_mod.latest_version(args.data_dir.expanduser())
        print(json.dumps({
            "current": current,
            "latest": latest,
            "has_update": update_mod.has_update(current, latest),
        }, ensure_ascii=False))
        if args.command == "version" or (args.command == "update" and args.check):
            return 0
        plan = update_mod.upgrade_plan()
        if plan is None:
            print(f"无法在此环境自升级（PEP 668 或缺少 pip），请手动执行：{update_mod.manual_hint()}")
            return 1
        print(f"运行升级：{' '.join(plan['cmd'])}")
        ok, output = update_mod.run_upgrade(plan["cmd"], on_line=print)
        print("升级完成" if ok else "升级失败，请查看上方输出")
        return 0 if ok else 1

    index_path = args.data_dir.expanduser() / "index.sqlite"
    with LocalStore(index_path) as store:
        adapters = default_adapters(args.home.expanduser())
        if args.command == "scan":
            started = monotonic()
            statuses = ScanService(store, adapters).scan_all()
            for status in statuses:
                print(json.dumps(asdict(status), ensure_ascii=False))
            print(
                json.dumps(
                    {
                        "event": "scan_complete",
                        "adapter_count": len(statuses),
                        "elapsed_ms": round((monotonic() - started) * 1000, 2),
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == "list":
            for session in store.list_sessions(SessionFilter()):
                print(json.dumps(asdict(session), ensure_ascii=False))
        elif args.command == "serve":
            scanner = ScanService(store, adapters)
            scanner.scan_all()
            content_enabled = not args.no_content_index and not _env_flag("AGENT_TRAIL_NO_CONTENT")
            indexer = ContentIndexer(store, adapters) if content_enabled else None
            if indexer is not None:
                indexer.index_due()
            chat = ChatManager(store, adapters, uploads_dir=args.data_dir / "chat_uploads")
            server = LocalServer(store, scanner, port=args.port, indexer=indexer, chat=chat)
            url = server.start()
            print(url, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            watcher = None
            if not args.no_watch:
                roots: list[tuple[Path, bool]] = []
                for adapter in adapters:
                    roots.extend(adapter.watch_roots())
                # 工厂按平台分发：Linux=inotify，Windows/macOS=轮询。
                # 不要直接构造 SourceWatcher——它依赖 POSIX 的
                # ctypes.CDLL(None)，在 Windows 上会 TypeError。
                watcher = create_watcher(roots, on_change=scanner.scan_all)
                if not watcher.start():
                    # 首选后端不可用（如 Linux 上 inotify 被禁）：退到可移植
                    # 的轮询后端，再失败才降级为纯 --interval 轮询。
                    fallback = PollingWatcher(roots, on_change=scanner.scan_all)
                    if fallback.start():
                        watcher = fallback
                if watcher is not None and watcher.healthy:
                    print(
                        json.dumps(
                            {
                                "event": "watch_started",
                                "kind": watcher.kind,
                                "watches": watcher.watch_count,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    reason = watcher.last_error if watcher is not None else "unavailable"
                    print(
                        json.dumps(
                            {"event": "watch_unavailable", "reason": reason},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    watcher = None

            def poll_interval() -> float:
                # While the watcher is healthy the poll degrades to a slow
                # safety net; if it dies we fall back to --interval.
                if watcher is not None and watcher.healthy:
                    return max(args.interval, 300.0)
                return args.interval

            def after_scan() -> None:
                if watcher is not None:
                    watcher.resync()
                if indexer is not None:
                    indexer.index_due()

            scheduler = threading.Thread(
                target=scanner.run_forever,
                args=(args.interval,),
                kwargs={"effective_interval": poll_interval, "after_scan": after_scan},
                name="agent-trail-scan",
                daemon=True,
            )
            scheduler.start()
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            finally:
                scanner.stop()
                scheduler.join(timeout=5)
                if watcher is not None:
                    watcher.stop()
                chat.close_all()
                server.stop()
    return 0
