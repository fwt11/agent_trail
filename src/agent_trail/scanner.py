from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from .adapters.base import BaseAdapter
from .models import AdapterStatus
from .store import LocalStore


class ScanService:
    def __init__(self, store: LocalStore, adapters: list[BaseAdapter]) -> None:
        self.store = store
        self.adapters = adapters
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._statuses: dict[str, AdapterStatus] = {}

    @property
    def statuses(self) -> list[AdapterStatus]:
        return [self._statuses[source] for source in sorted(self._statuses)]

    def scan_all(self) -> list[AdapterStatus]:
        with self._lock:
            now = datetime.now(timezone.utc)
            scanned_at = now.isoformat()
            statuses: list[AdapterStatus] = []
            for adapter in self.adapters:
                previous = self.store.get_fingerprint(adapter.source)
                try:
                    result = adapter.scan(previous, now)
                    status = AdapterStatus(
                        source=adapter.source,
                        kind=result.status.kind,
                        message=result.status.message,
                        record_count=len(result.sessions) if result.changed else result.status.record_count,
                        error_count=result.status.error_count,
                        last_scanned_at=scanned_at,
                    )
                    if status.kind in {"ok", "experimental"}:
                        if result.changed:
                            self.store.replace_source(
                                adapter.source, result.sessions, result.fingerprint, scanned_at
                            )
                        else:
                            self.store.mark_unchanged(
                                adapter.source, previous, scanned_at
                            )
                        if not result.changed:
                            status = AdapterStatus(
                                source=adapter.source,
                                kind=status.kind,
                                message=status.message,
                                record_count=result.status.record_count,
                                error_count=status.error_count,
                                last_scanned_at=scanned_at,
                            )
                    elif status.kind == "unavailable":
                        if previous is not None:
                            self.store.replace_source(
                                adapter.source, [], None, scanned_at
                            )
                        else:
                            self.store.mark_unchanged(adapter.source, None, scanned_at)
                    else:
                        self.store.mark_unchanged(adapter.source, previous, scanned_at)
                except Exception as error:
                    status = AdapterStatus(
                        source=adapter.source,
                        kind="error",
                        message=f"scan failed: {type(error).__name__}",
                        last_scanned_at=scanned_at,
                    )
                    self.store.mark_unchanged(adapter.source, previous, scanned_at)
                adapter._status = status
                self._statuses[adapter.source] = status
                statuses.append(status)
            return statuses

    def run_forever(
        self,
        interval_seconds: float = 30,
        effective_interval: Callable[[], float] | None = None,
        after_scan: Callable[[], None] | None = None,
    ) -> None:
        while not self._stop.wait(
            effective_interval() if effective_interval is not None else interval_seconds
        ):
            try:
                self.scan_all()
            except Exception:
                # Store-level failures must not kill the scheduler; adapter
                # errors are already isolated inside scan_all.
                continue
            if after_scan is not None:
                try:
                    after_scan()
                except Exception:
                    continue

    def stop(self) -> None:
        self._stop.set()
