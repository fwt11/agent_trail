from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from ..models import ActivitySession, AdapterStatus, ResumePlan, ScanResult


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
