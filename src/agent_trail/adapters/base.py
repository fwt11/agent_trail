from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from ..models import (
    ActivitySession,
    AdapterStatus,
    ChatSupport,
    ConversationContent,
    ResumePlan,
    ScanResult,
)


class BaseAdapter(ABC):
    source: str
    adapter_version: int = 1

    def __init__(self, home: Path) -> None:
        self.home = home
        self._status = AdapterStatus(source=self.source, kind="unavailable")

    @abstractmethod
    def discover(self) -> AdapterStatus:
        raise NotImplementedError

    def status(self) -> AdapterStatus:
        return self._status

    def watch_roots(self) -> list[tuple[Path, bool]]:
        """Directories to watch for change notifications as (path, recursive).

        Defaults to no watching; adapters that read files should return the
        directories that contain their sources so misses degrade to polling.
        """
        return []

    @abstractmethod
    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        raise NotImplementedError

    @abstractmethod
    def resume_plan(self, session: ActivitySession) -> ResumePlan:
        raise NotImplementedError

    def read_messages(self, session: ActivitySession) -> ConversationContent | None:
        """按需解析会话消息正文；不支持内容查看的适配器返回 None。

        这是可选能力（元数据优先原则的受控放宽）：正文只进本地控制台展示
        与本机索引，绝不外传；源文件仍然只读。解析必须防御式——任何单条
        解析失败都跳过而不是让整个调用失败。
        """
        return None

    def chat_support(self) -> ChatSupport | None:
        """声明本适配器的 CLI 可被无头模式驱动（应用内聊天）。

        返回 None 表示不支持：控制台对该来源只提供"跳回终端"。
        """
        return None
