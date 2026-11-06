from __future__ import annotations

import json
import secrets
import shutil
import threading
from dataclasses import asdict
from datetime import datetime, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .launcher import TerminalLauncher
from .models import SessionFilter
from .scanner import ScanService
from .store import LocalStore


class LocalServer:
    def __init__(
        self,
        store: LocalStore,
        scanner: ScanService,
        port: int = 0,
        launcher: TerminalLauncher | None = None,
    ) -> None:
        self.store = store
        self.scanner = scanner
        self.launcher = launcher or TerminalLauncher()
        self.key = secrets.token_urlsafe(32)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _RequestHandler)
        self._httpd.app = self
        self._thread: threading.Thread | None = None
        self._base_url = f"http://127.0.0.1:{self._httpd.server_port}"

    @property
    def base_url(self) -> str:
        return self._base_url

    def start(self) -> str:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                name="agent-trail-http",
                daemon=True,
            )
            self._thread.start()
        return f"{self._base_url}/?key={self.key}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _sessions(self, query: dict[str, list[str]]) -> dict[str, Any]:
        sources = tuple(query.get("source", []))
        if "sources" in query:
            sources += tuple(
                source for source in query["sources"][-1].split(",") if source
            )
        filters = SessionFilter(
            since=self._date_filter(query, "since"),
            until=self._date_filter(query, "until"),
            sources=sources,
            workdir=self._one(query, "workdir"),
            query=self._one(query, "query"),
            limit=self._integer(query, "limit", 200, 1, 1000),
            offset=self._integer(query, "offset", 0, 0, 1_000_000),
        )
        sessions = self.store.list_sessions(filters)
        records = []
        for session in sessions:
            record = asdict(session)
            record["workdir_exists"] = Path(session.workdir).is_dir()
            adapter = next(
                (item for item in self.scanner.adapters if item.source == session.source),
                None,
            )
            plan = adapter.resume_plan(session) if adapter is not None else None
            record["cli_available"] = bool(
                plan
                and plan.kind == "cli"
                and plan.executable
                and shutil.which(plan.executable)
            )
            records.append(record)
        return {"sessions": records}

    def _status(self) -> dict[str, Any]:
        statuses = [asdict(status) for status in self.scanner.statuses]
        scanned = [status["last_scanned_at"] for status in statuses if status["last_scanned_at"]]
        sessions = self.store.list_sessions(SessionFilter(limit=1_000_000))
        return {
            "sources": statuses,
            "last_scanned_at": max(scanned) if scanned else None,
            "session_count": len(sessions),
        }

    def _resume(self, payload: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise ValueError("invalid request")
        source = payload.get("source")
        session_id = payload.get("session_id")
        if (
            not isinstance(source, str)
            or not isinstance(session_id, str)
            or not source
            or not session_id
            or len(source) > 100
            or len(session_id) > 1000
        ):
            raise ValueError("invalid session reference")
        session = self.store.get_session(source, session_id)
        adapter = next(
            (item for item in self.scanner.adapters if item.source == source), None
        )
        if session is None or adapter is None:
            return 404, {"status": "unavailable", "message": "session_not_found"}
        if session.source_missing:
            return 409, {"status": "unavailable", "message": "session_source_missing"}
        plan = adapter.resume_plan(session)
        if plan.kind != "cli":
            return 409, {"status": "unavailable", "message": "session_view_only"}
        result = self.launcher.launch(plan)
        if not result.ok:
            return 409, {"status": "unavailable", "message": result.message}
        return 200, {"status": "launched", "message": result.message}

    @staticmethod
    def _one(query: dict[str, list[str]], key: str) -> str | None:
        values = query.get(key)
        if not values:
            return None
        if len(values) != 1 or len(values[0]) > 1000:
            raise ValueError("invalid query")
        return values[0]

    @staticmethod
    def _integer(
        query: dict[str, list[str]], key: str, default: int, minimum: int, maximum: int
    ) -> int:
        raw = LocalServer._one(query, key)
        if raw is None:
            return default
        value = int(raw)
        if not minimum <= value <= maximum:
            raise ValueError("invalid query")
        return value

    @staticmethod
    def _date_filter(query: dict[str, list[str]], key: str) -> str | None:
        raw = LocalServer._one(query, key)
        if raw is None:
            return None
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "AgentTrail"
    sys_version = ""

    @property
    def app(self) -> LocalServer:
        return self.server.app

    def do_GET(self) -> None:
        if not self._valid_host():
            self._error(403)
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/") and not self._valid_key():
            self._error(403)
            return
        try:
            if path == "/api/sessions":
                self._json(200, self.app._sessions(parse_qs(urlsplit(self.path).query)))
            elif path == "/api/status":
                self._json(200, self.app._status())
            elif path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif path == "/styles.css":
                self._static("styles.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._static("app.js", "text/javascript; charset=utf-8")
            elif path == "/i18n.js":
                self._static("i18n.js", "text/javascript; charset=utf-8")
            else:
                self._error(404)
        except (ValueError, TypeError):
            self._error(400)

    def do_POST(self) -> None:
        if not self._valid_host() or not self._valid_key() or not self._valid_origin():
            self._error(403)
            return
        path = urlsplit(self.path).path
        if path == "/api/refresh":
            self.app.scanner.scan_all()
            self._json(200, self.app._status())
        elif path == "/api/resume":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("invalid content length")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                status, response = self.app._resume(payload)
                self._json(status, response)
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                self._error(400)
        else:
            self._error(404)

    def _valid_host(self) -> bool:
        return self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_port}"

    def _valid_key(self) -> bool:
        supplied = self.headers.get("X-Agent-Trail-Key", "")
        return bool(supplied) and secrets.compare_digest(supplied, self.app.key)

    def _valid_origin(self) -> bool:
        return self.headers.get("Origin", "") == self.app.base_url

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._respond(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int) -> None:
        self._json(status, {"error": "request rejected" if status == 403 else "invalid request"})

    def _static(self, name: str, content_type: str) -> None:
        path = Path(__file__).parent / "static" / name
        try:
            content = path.read_bytes()
        except OSError:
            self._error(404)
            return
        self._respond(200, content, content_type)

    def _respond(self, status: int, content: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        return
