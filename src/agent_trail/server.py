from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
from dataclasses import asdict
from datetime import datetime, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from .chat import ChatError
from .launcher import TerminalLauncher
from .models import SessionFilter
from .scanner import ScanService
from .store import LocalStore

if TYPE_CHECKING:
    from .chat import ChatManager
    from .content import ContentIndexer

CHAT_BODY_MAX = 65536
CHAT_SEND_BODY_MAX = 15 * 1024 * 1024  # 附件 base64 后的总量上限


class LocalServer:
    def __init__(
        self,
        store: LocalStore,
        scanner: ScanService,
        port: int = 0,
        launcher: TerminalLauncher | None = None,
        indexer: ContentIndexer | None = None,
        chat: ChatManager | None = None,
    ) -> None:
        self.store = store
        self.scanner = scanner
        self.launcher = launcher or TerminalLauncher()
        self.indexer = indexer
        self.chat = chat
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
            "content_enabled": self.indexer is not None,
            "fts_available": self.store.fts_available,
            "chat_enabled": self.chat is not None,
        }

    def _session_messages(self, query: dict[str, list[str]]) -> dict[str, Any]:
        source = self._one(query, "source")
        session_id = self._one(query, "session_id")
        if not source or not session_id:
            raise ValueError("invalid session reference")
        if self.indexer is None:
            return {"status": "disabled"}
        return self.indexer.session_payload(source, session_id)

    def _search(self, query: dict[str, list[str]]) -> dict[str, Any]:
        if self.indexer is None:
            return {"available": False, "fts": False, "hits": []}
        text = self._one(query, "q") or ""
        limit = self._integer(query, "limit", 30, 1, 100)
        hits = self.store.search_content(text, limit)
        records = []
        for hit in hits:
            session = self.store.get_session(hit.source, hit.session_id)
            records.append(
                {
                    "source": hit.source,
                    "session_id": hit.session_id,
                    "seq": hit.seq,
                    "role": hit.role,
                    "snippet": hit.snippet,
                    "session_title": session.title if session is not None else None,
                    "workdir": session.workdir if session is not None else None,
                    "updated_at": session.updated_at if session is not None else None,
                }
            )
        return {"available": True, "fts": self.store.fts_available, "hits": records}

    def _update_status(self) -> dict[str, Any]:
        """托盘每日检查后的缓存结果（serve 自身不外呼 PyPI）。"""
        from . import update as _update

        current = _update.current_version()
        cached = _update.read_cache(Path.home() / ".agent-trail")
        latest = (cached or {}).get("latest")
        return {
            "current": current,
            "latest": latest,
            "has_update": _update.has_update(current, latest),
        }

    def _chat_capabilities(self) -> dict[str, Any]:
        return {"agents": self.chat.capabilities() if self.chat is not None else []}

    def _chat_file(self, query: dict[str, list[str]]) -> tuple[bytes, str]:
        """回显 chat_uploads 下的上传文件（仅限该目录，防任意读取）。"""
        import mimetypes as _mimetypes

        relative = self._one(query, "name")
        if not relative or len(relative) > 300 or ".." in relative:
            raise ValueError("invalid file reference")
        root = self.chat.uploads_dir if self.chat is not None else None
        if root is None:
            raise ValueError("uploads unavailable")
        target = (root / relative).resolve()
        if not str(target).startswith(str(root.resolve())):
            raise ValueError("invalid file reference")
        if not target.is_file():
            raise ValueError("file not found")
        mime = _mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return target.read_bytes(), mime

    def _chat_threads(self) -> dict[str, Any]:
        return {"threads": [asdict(thread) for thread in self.store.list_threads()]}

    def _chat_thread(self, query: dict[str, list[str]]) -> dict[str, Any]:
        thread_id = self._one(query, "id")
        if not thread_id or len(thread_id) > 128:
            raise ValueError("invalid thread reference")
        thread = self.store.get_thread(thread_id)
        if thread is None:
            raise ValueError("thread not found")
        return {
            "thread": asdict(thread),
            "messages": [asdict(message) for message in self.store.thread_messages(thread_id)],
        }

    def _chat_new(self, payload: Any) -> tuple[int, dict[str, Any]]:
        if self.chat is None:
            return 409, {"status": "unavailable", "message": "chat disabled"}
        if not isinstance(payload, dict):
            raise ValueError("invalid request")
        source = payload.get("source")
        if not isinstance(source, str) or not source or len(source) > 100:
            raise ValueError("invalid source")
        workdir = payload.get("workdir")
        title = payload.get("title")
        if workdir is not None and (not isinstance(workdir, str) or len(workdir) > 1000):
            raise ValueError("invalid workdir")
        if title is not None and (not isinstance(title, str) or len(title) > 200):
            raise ValueError("invalid title")
        settings = payload.get("settings")
        if settings is None:
            settings = {key: payload[key] for key in ("mode", "model", "thinking") if payload.get(key)}
        if settings is not None and not isinstance(settings, dict):
            raise ValueError("invalid settings")
        cleaned: dict[str, str] = {}
        for key, value in (settings or {}).items():
            if not isinstance(key, str) or not key or len(key) > 100:
                raise ValueError("invalid setting key")
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError(f"invalid setting value for {key}")
            cleaned[key] = value
        try:
            thread = self.chat.create_thread(source, workdir, title, config=cleaned)
        except ChatError as error:
            return 409, {"status": "unavailable", "message": str(error)}
        return 200, {"thread": asdict(thread)}

    def _chat_send(self, payload: Any) -> tuple[int, dict[str, Any]]:
        if self.chat is None:
            return 409, {"status": "unavailable", "message": "chat disabled"}
        if not isinstance(payload, dict):
            raise ValueError("invalid request")
        thread_id = payload.get("thread_id")
        prompt = payload.get("prompt")
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 128:
            raise ValueError("invalid thread reference")
        if not isinstance(prompt, str) or len(prompt) > CHAT_BODY_MAX:
            raise ValueError("invalid prompt")
        raw_attachments = payload.get("attachments") or []
        if not isinstance(raw_attachments, list) or len(raw_attachments) > 8:
            raise ValueError("invalid attachments")
        import base64 as _base64

        attachments: list[dict] = []
        total_bytes = 0
        for att in raw_attachments:
            if not isinstance(att, dict):
                raise ValueError("invalid attachment")
            kind = att.get("kind")
            name = att.get("name")
            mime = att.get("mime")
            if not isinstance(name, str) or not name or len(name) > 200:
                raise ValueError("invalid attachment name")
            if mime is not None and (not isinstance(mime, str) or len(mime) > 100):
                raise ValueError("invalid attachment mime")
            if kind == "path":
                # 同机零拷贝：文件浏览器/手输给出真实路径，agent 直读原文件。
                path_value = att.get("path")
                if not isinstance(path_value, str) or not path_value or len(path_value) > 1024:
                    raise ValueError("invalid attachment path")
                attachments.append({"kind": "path", "name": name, "path": path_value, "mime": mime or ""})
            elif kind == "upload":
                # 拖拽/粘贴/跨机：浏览器只有内容，落盘到 chat_uploads/<线程>/。
                data = att.get("data")
                if not isinstance(data, str):
                    raise ValueError("invalid attachment data")
                try:
                    decoded = _base64.b64decode(data, validate=True)
                except Exception as error:
                    raise ValueError("invalid attachment encoding") from error
                total_bytes += len(decoded)
                if total_bytes > 20 * 1024 * 1024:
                    raise ValueError("attachments exceed 20MB")
                try:
                    saved = self.chat.save_upload(thread_id, name, decoded)
                except ChatError as error:
                    return 409, {"status": "unavailable", "message": str(error)}
                attachments.append({"kind": "upload", "name": name, "path": str(saved), "mime": mime or ""})
            else:
                raise ValueError("invalid attachment kind")
        try:
            task_id = self.chat.start_turn(thread_id, prompt, attachments=attachments)
        except ChatError as error:
            return 409, {"status": "unavailable", "message": str(error)}
        return 200, {"task_id": task_id}

    def _chat_cancel(self, payload: Any) -> tuple[int, dict[str, Any]]:
        if self.chat is None:
            return 409, {"status": "unavailable", "message": "chat disabled"}
        if not isinstance(payload, dict):
            raise ValueError("invalid request")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
            raise ValueError("invalid task reference")
        cancelled = self.chat.cancel(task_id)
        return 200, {"status": "cancelled" if cancelled else "unknown_task"}

    def _chat_config(self, query: dict[str, list[str]]) -> dict[str, Any]:
        thread_id = self._one(query, "thread_id")
        if not thread_id or len(thread_id) > 128:
            raise ValueError("invalid thread reference")
        options = self.chat.thread_config(thread_id) if self.chat is not None else []
        return {"configOptions": options}

    def _chat_config_defaults(self, query: dict[str, list[str]]) -> dict[str, Any]:
        source = self._one(query, "source")
        if not source or len(source) > 100:
            raise ValueError("invalid source")
        options = self.chat.source_config(source) if self.chat is not None else []
        if self.chat is not None and not options:
            # 无缓存：后台预建会话（选 agent 即取配置），前端稍后重取。
            self.chat.prefetch_session(source)
        return {"configOptions": options}

    def _chat_settings(self, payload: Any) -> tuple[int, dict[str, Any]]:
        if self.chat is None:
            return 409, {"status": "unavailable", "message": "chat disabled"}
        if not isinstance(payload, dict):
            raise ValueError("invalid request")
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id or len(thread_id) > 128:
            raise ValueError("invalid thread reference")
        values = payload.get("settings")
        if values is None:
            # 兼容旧字段：mode/model/thinking 顶层键
            values = {key: payload[key] for key in ("mode", "model", "thinking") if payload.get(key)}
        if not isinstance(values, dict) or not values:
            raise ValueError("invalid settings")
        cleaned: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(key, str) or not key or len(key) > 100:
                raise ValueError("invalid setting key")
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError(f"invalid setting value for {key}")
            cleaned[key] = value
        try:
            self.chat.set_thread_settings(thread_id, cleaned)
        except ChatError as error:
            return 409, {"status": "unavailable", "message": str(error)}
        return 200, {"status": "ok"}

    def _chat_approvals(self, query: dict[str, list[str]]) -> dict[str, Any]:
        thread_id = self._one(query, "thread_id")
        if not thread_id or len(thread_id) > 128:
            raise ValueError("invalid thread reference")
        pending = self.chat.pending_approvals(thread_id) if self.chat is not None else []
        return {"approvals": pending}

    def _fs_list(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """文件/文件夹选择器的服务端目录浏览（本机 + 令牌已授权）。"""
        raw = self._one(query, "path")
        path = Path(raw).expanduser() if raw else Path.home()
        if not path.is_absolute():
            path = Path.home() / path
        path = Path(os.path.normpath(str(path)))
        if not path.is_dir():
            raise ValueError("not a directory")
        include_files = self._one(query, "files") is not None
        entries = []
        try:
            for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
                try:
                    if child.is_dir():
                        entries.append({"name": child.name, "path": str(child), "is_dir": True})
                    elif include_files:
                        entries.append({
                            "name": child.name,
                            "path": str(child),
                            "is_dir": False,
                            "size": child.stat().st_size,
                        })
                except OSError:
                    continue
        except OSError as error:
            raise ValueError(f"unreadable directory: {error}") from error
        parent = path.parent if path.parent != path else None
        return {
            "path": str(path),
            "parent": str(parent) if parent else None,
            "home": str(Path.home()),
            "recents": self.store.list_recent_workdirs(8),
            "entries": entries[:400],
        }

    def _chat_approval_decision(self, payload: Any) -> tuple[int, dict[str, Any]]:
        if self.chat is None:
            return 409, {"status": "unavailable", "message": "chat disabled"}
        if not isinstance(payload, dict):
            raise ValueError("invalid request")
        approval_id = payload.get("id")
        if not isinstance(approval_id, str) or not approval_id or len(approval_id) > 128:
            raise ValueError("invalid approval reference")
        option_id = payload.get("option_id")
        if option_id is not None and (not isinstance(option_id, str) or len(option_id) > 200):
            raise ValueError("invalid option reference")
        decided = self.chat.decide_approval(approval_id, option_id)
        return 200, {"status": "decided" if decided else "unknown_approval"}

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
            elif path == "/api/session/messages":
                self._json(200, self.app._session_messages(parse_qs(urlsplit(self.path).query)))
            elif path == "/api/search":
                self._json(200, self.app._search(parse_qs(urlsplit(self.path).query)))
            elif path == "/api/chat/capabilities":
                self._json(200, self.app._chat_capabilities())
            elif path == "/api/chat/threads":
                self._json(200, self.app._chat_threads())
            elif path == "/api/chat/thread":
                self._json(200, self.app._chat_thread(parse_qs(urlsplit(self.path).query)))
            elif path == "/api/chat/config":
                self._json(200, self.app._chat_config(parse_qs(urlsplit(self.path).query)))
            elif path == "/api/chat/config-defaults":
                self._json(200, self.app._chat_config_defaults(parse_qs(urlsplit(self.path).query)))
            elif path == "/api/chat/approvals":
                self._json(200, self.app._chat_approvals(parse_qs(urlsplit(self.path).query)))
            elif path == "/api/fs/list":
                self._json(200, self.app._fs_list(parse_qs(urlsplit(self.path).query)))
            elif path == "/api/update/status":
                self._json(200, self.app._update_status())
            elif path == "/api/chat/file":
                content, mime = self.app._chat_file(parse_qs(urlsplit(self.path).query))
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(content)
            elif path == "/api/chat/stream":
                self._chat_stream(parse_qs(urlsplit(self.path).query))
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
            if self.app.indexer is not None:
                self.app.indexer.index_due()
            self._json(200, self.app._status())
        elif path == "/api/resume":
            self._post_json(path, self.app._resume, 8192)
        elif path in ("/api/chat/new", "/api/chat/cancel", "/api/chat/settings", "/api/chat/approvals/decision"):
            handlers = {
                "/api/chat/new": self.app._chat_new,
                "/api/chat/cancel": self.app._chat_cancel,
                "/api/chat/settings": self.app._chat_settings,
                "/api/chat/approvals/decision": self.app._chat_approval_decision,
            }
            self._post_json(path, handlers[path], CHAT_BODY_MAX)
        elif path == "/api/chat/send":
            self._post_json(path, self.app._chat_send, CHAT_SEND_BODY_MAX)
        else:
            self._error(404)

    def _post_json(self, path: str, handler, max_length: int) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= max_length:
                raise ValueError("invalid content length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            status, response = handler(payload)
            self._json(status, response)
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            self._error(400)

    def _chat_stream(self, query: dict[str, list[str]]) -> None:
        """SSE：按游标重放任务事件历史并实时推送新事件。

        事件不消费（任务对象持有完整历史），断线重连、晚到的订阅者都能
        从头重放。认证沿用 X-Agent-Trail-Key 头（前端用 fetch 流读取而非
        EventSource，避免 key 出现在 URL/日志里）。
        """
        if self.app.chat is None:
            self._error(403)
            return
        task_id = LocalServer._one(query, "task")
        task = self.app.chat.subscribe(task_id) if task_id else None
        if task is None:
            self._error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        cursor = 0

        def emit(event: dict) -> bool:
            data = json.dumps(event, ensure_ascii=False).encode("utf-8")
            self.wfile.write(b"data: " + data + b"\n\n")
            self.wfile.flush()
            return True

        try:
            while True:
                with task.condition:
                    while cursor >= len(task.events) and not task.is_finished:
                        if not task.condition.wait(timeout=15.0):
                            break
                    events = list(task.events)
                pending = events[cursor:]
                for event in pending:
                    emit(event)
                cursor = len(events)
                if task.is_finished and cursor >= len(events):
                    break
                if not pending and not task.is_finished:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

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
