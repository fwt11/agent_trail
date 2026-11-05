from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from dataclasses import asdict
from pathlib import Path
from time import monotonic

from .adapters import default_adapters
from .models import SessionFilter
from .scanner import ScanService
from .server import LocalServer
from .store import LocalStore
from .watcher import SourceWatcher


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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        if not 0 <= args.port <= 65535:
            parser.error("--port must be between 0 and 65535")
        if args.interval <= 0:
            parser.error("--interval must be positive")
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
            server = LocalServer(store, scanner, port=args.port)
            url = server.start()
            print(url, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            watcher: SourceWatcher | None = None
            if not args.no_watch:
                roots: list[tuple[Path, bool]] = []
                for adapter in adapters:
                    roots.extend(adapter.watch_roots())
                watcher = SourceWatcher(roots, on_change=scanner.scan_all)
                if watcher.start():
                    print(
                        json.dumps(
                            {"event": "watch_started", "watches": watcher.watch_count},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    print(
                        json.dumps(
                            {"event": "watch_unavailable", "reason": watcher.last_error},
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

            scheduler = threading.Thread(
                target=scanner.run_forever,
                args=(args.interval,),
                kwargs={"effective_interval": poll_interval,
                        "after_scan": watcher.resync if watcher is not None else None},
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
                server.stop()
    return 0
