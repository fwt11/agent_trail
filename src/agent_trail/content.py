"""会话正文的本地索引器：惰性读取、mtime/size 失效键、限额封顶。

隐私边界：正文只进本机 index.sqlite 与控制台展示，绝不外传；
源文件始终只读。整体开关见 serve 的 --no-content-index。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from .adapters.base import BaseAdapter
from .models import ActivitySession, ConversationMessage, SessionFilter
from .store import LocalStore

MAX_MESSAGES_PER_SESSION = 2000
MAX_CHARS_PER_MESSAGE = 20000
MAX_SESSIONS_PER_CYCLE = 50


class ContentIndexer:
    def __init__(
        self,
        store: LocalStore,
        adapters: list[BaseAdapter],
        max_sessions_per_cycle: int = MAX_SESSIONS_PER_CYCLE,
        max_messages_per_session: int = MAX_MESSAGES_PER_SESSION,
        max_chars_per_message: int = MAX_CHARS_PER_MESSAGE,
    ) -> None:
        self.store = store
        self.adapters = list(adapters)
        self.max_sessions_per_cycle = max_sessions_per_cycle
        self.max_messages_per_session = max_messages_per_session
        self.max_chars_per_message = max_chars_per_message
        self._lock = threading.Lock()

    def supports(self, adapter: BaseAdapter) -> bool:
        return type(adapter).read_messages is not BaseAdapter.read_messages

    def index_due(self) -> int:
        """扫描后调用：把 locator 发生变化的会话正文增量索引，返回索引数。"""
        indexed = 0
        with self._lock:
            for adapter in self.adapters:
                if indexed >= self.max_sessions_per_cycle:
                    break
                if not self.supports(adapter):
                    continue
                sessions = self.store.list_sessions(
                    SessionFilter(sources=(adapter.source,), limit=1_000_000)
                )
                for session in sessions:
                    if indexed >= self.max_sessions_per_cycle:
                        break
                    if self._index_session(adapter, session):
                        indexed += 1
        return indexed

    def session_payload(self, source: str, session_id: str) -> dict:
        """供 /api/session/messages：缓存新鲜直读，否则现读现索引。"""
        session = self.store.get_session(source, session_id)
        if session is None:
            return {"status": "session_not_found"}
        adapter = self._adapter(source)
        if adapter is None or not self.supports(adapter):
            return {"status": "unsupported"}
        stat = self._locator_stat(session)
        state = self.store.content_state(source, session_id)
        if stat is not None and self._is_fresh(session, stat, state):
            messages = self.store.list_messages(source, session_id)
            return self._payload(messages, bool(state[3]) if state else False)
        if stat is None:
            # 源文件已消失：只要缓存里还有正文就照常展示（配合
            # sessions.source_missing 的墓碑语义）。
            if state is None:
                return {"status": "source_unavailable"}
            return self._payload(self.store.list_messages(source, session_id), bool(state[3]))
        content = adapter.read_messages(session)
        if content is None:
            return {"status": "read_failed"}
        messages, truncated = self._truncate(content.messages)
        self._index(adapter, session, stat, messages, truncated)
        return self._payload(messages, truncated)

    def _index_session(self, adapter: BaseAdapter, session: ActivitySession) -> bool:
        stat = self._locator_stat(session)
        if stat is None:
            return False
        if self._is_fresh(session, stat, self.store.content_state(session.source, session.session_id)):
            return False
        content = adapter.read_messages(session)
        if content is None:
            return False
        messages, truncated = self._truncate(content.messages)
        self._index(adapter, session, stat, messages, truncated)
        return True

    def _index(
        self,
        adapter: BaseAdapter,
        session: ActivitySession,
        stat: tuple[int, int],
        messages: list[ConversationMessage],
        truncated: bool,
    ) -> None:
        self.store.index_content(
            source=session.source,
            session_id=session.session_id,
            locator=session.source_locator,
            locator_stat=stat,
            adapter_version=adapter.adapter_version,
            messages=messages,
            truncated=truncated,
            indexed_at=datetime.now(timezone.utc).isoformat(),
        )

    def _is_fresh(
        self,
        session: ActivitySession,
        stat: tuple[int, int],
        state: tuple[int, int, int, bool] | None,
    ) -> bool:
        if state is None:
            return False
        mtime_ns, size, adapter_version, _truncated = state
        return (
            stat == (mtime_ns, size)
            and session.adapter_version == adapter_version
        )

    def _locator_stat(self, session: ActivitySession) -> tuple[int, int] | None:
        locator = session.source_locator
        if not locator:
            return None
        try:
            info = Path(locator).stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    def _truncate(self, messages: list[ConversationMessage]) -> tuple[list[ConversationMessage], bool]:
        truncated = len(messages) > self.max_messages_per_session
        kept = messages[: self.max_messages_per_session]
        capped: list[ConversationMessage] = []
        for message in kept:
            text = message.text
            if len(text) > self.max_chars_per_message:
                text = text[: self.max_chars_per_message] + "…[truncated]"
                capped.append(
                    ConversationMessage(role=message.role, text=text, timestamp=message.timestamp, kind=message.kind)
                )
            else:
                capped.append(message)
        return capped, truncated

    def _adapter(self, source: str) -> BaseAdapter | None:
        return next((item for item in self.adapters if item.source == source), None)

    @staticmethod
    def _payload(messages: list[ConversationMessage], truncated: bool) -> dict:
        return {
            "status": "ok",
            "messages": [
                {"role": m.role, "text": m.text, "timestamp": m.timestamp, "kind": m.kind}
                for m in messages
            ],
            "truncated": truncated,
        }
