"""inotify-backed change notification for agent session sources.

Uses ctypes against libc directly so the project stays on the standard
library.  The watcher never replaces the scan itself: it only replaces
the timer that wakes :meth:`ScanService.scan_all`.  Fingerprints remain
the cheap dedup layer, so any spurious event degrades to a fast no-op.
"""

from __future__ import annotations

import ctypes
import os
import select
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic

IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000

IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000

# Completed writes, atomic replacements and creations/deletions.  IN_MODIFY
# is deliberately left out: it fires mid-write while CLOSE_WRITE covers the
# finished append.
WATCH_MASK = (
    IN_ATTRIB
    | IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
)

_EVENT_HEADER = struct.Struct("iIII")


class LibcInotify:
    """ctypes bindings for the inotify(7) family; unavailable off Linux."""

    def __init__(self) -> None:
        self.libc: ctypes.CDLL | None = None
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            self.inotify_init1 = libc.inotify_init1
            self.inotify_add_watch = libc.inotify_add_watch
            self.inotify_rm_watch = libc.inotify_rm_watch
            self.inotify_init1.argtypes = [ctypes.c_int]
            self.inotify_init1.restype = ctypes.c_int
            self.inotify_add_watch.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint32,
            ]
            self.inotify_add_watch.restype = ctypes.c_int
            self.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
            self.inotify_rm_watch.restype = ctypes.c_int
            self.libc = libc
        except (OSError, AttributeError):
            self.libc = None

    @property
    def available(self) -> bool:
        return self.libc is not None


_LIBC: LibcInotify | None = None


def libc_inotify() -> LibcInotify:
    global _LIBC
    if _LIBC is None:
        _LIBC = LibcInotify()
    return _LIBC


def parse_events(raw: bytes) -> list[tuple[int, int, str]]:
    """Parse an inotify event buffer into ``(wd, mask, name)`` tuples."""
    events: list[tuple[int, int, str]] = []
    offset = 0
    while offset + _EVENT_HEADER.size <= len(raw):
        wd, mask, _cookie, name_len = _EVENT_HEADER.unpack_from(raw, offset)
        offset += _EVENT_HEADER.size
        name = raw[offset : offset + name_len]
        offset += name_len
        events.append((wd, mask, os.fsdecode(name.rstrip(b"\0"))))
    return events


class SourceWatcher:
    """Debounced, directory-level inotify watcher.

    ``on_change`` runs on the watch thread; the existing scan lock keeps it
    serialized against manual refreshes and the safety-net poll.
    """

    def __init__(
        self,
        roots: list[tuple[Path, bool]],
        on_change: Callable[[], None],
        *,
        debounce_seconds: float = 1.0,
        min_spacing_seconds: float = 5.0,
        max_watches: int = 2048,
    ) -> None:
        self._roots = [(Path(path), recursive) for path, recursive in roots]
        self._on_change = on_change
        self._debounce = debounce_seconds
        self._min_spacing = min_spacing_seconds
        self._max_watches = max_watches
        self._libc = libc_inotify()
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._wds: dict[int, Path] = {}
        self._paths: set[Path] = set()
        self._pending = False
        self._deadline = 0.0
        self._first_pending = 0.0
        self._last_trigger = 0.0
        self.last_error: str | None = None
        self.overflowed = False

    @property
    def healthy(self) -> bool:
        return (
            self._fd is not None
            and self._thread is not None
            and self._thread.is_alive()
        )

    @property
    def watch_count(self) -> int:
        with self._lock:
            return len(self._wds)

    def start(self) -> bool:
        if not self._libc.available:
            self.last_error = "inotify not available on this platform"
            return False
        fd = self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd == -1:
            self.last_error = f"inotify_init1 failed: errno {ctypes.get_errno()}"
            return False
        self._fd = fd
        with self._lock:
            self._add_roots_locked()
        self._thread = threading.Thread(
            target=self._run, name="agent-trail-watch", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def resync(self) -> None:
        """Re-add watches for configured roots (idempotent).

        Called after safety-net scans to pick up source directories created
        or moved while events were missed (e.g. after a queue overflow).
        """
        if self._fd is None:
            return
        with self._lock:
            self._add_roots_locked()

    # -- watch management ---------------------------------------------------

    def _add_roots_locked(self) -> None:
        for path, recursive in self._roots:
            self._add_tree_locked(path, recursive)

    def _add_tree_locked(self, path: Path, recursive: bool) -> bool:
        target = path if path.is_dir() else _nearest_existing_ancestor(path)
        walk = recursive and path.is_dir()
        if target is None:
            return False
        found = self._add_watch_locked(target)
        if not walk:
            return found
        for current, dirs, _files in os.walk(target):
            for name in dirs:
                if len(self._wds) >= self._max_watches:
                    return found
                found |= self._add_watch_locked(Path(current) / name)
        return found

    def _add_watch_locked(self, directory: Path) -> bool:
        if directory in self._paths:
            return True
        if len(self._wds) >= self._max_watches or self._fd is None:
            return False
        wd = self._libc.inotify_add_watch(
            self._fd, os.fsencode(directory), WATCH_MASK
        )
        if wd == -1:
            # ENOSPC or permissions: the safety-net poll still covers us.
            return False
        self._wds[wd] = directory
        self._paths.add(directory)
        return True

    def _drop_watch_locked(self, wd: int) -> None:
        path = self._wds.pop(wd, None)
        if path is not None:
            self._paths.discard(path)

    # -- event loop ----------------------------------------------------------

    def _run(self) -> None:
        assert self._fd is not None
        while not self._stop.is_set():
            timeout = self._select_timeout()
            try:
                readable, _, _ = select.select([self._fd], [], [], timeout)
            except (OSError, ValueError):
                break
            if readable:
                self._drain()
            self._maybe_trigger()

    def _select_timeout(self) -> float:
        if self._pending:
            return max(min(self._deadline - monotonic(), 0.5), 0.05)
        return 0.5

    def _drain(self) -> None:
        assert self._fd is not None
        while True:
            try:
                raw = os.read(self._fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                return
            if not raw:
                return
            self._handle_events(raw)

    def _handle_events(self, raw: bytes) -> None:
        now = monotonic()
        for wd, mask, name in parse_events(raw):
            if mask & IN_IGNORED:
                with self._lock:
                    self._drop_watch_locked(wd)
                continue
            if mask & IN_Q_OVERFLOW:
                self.overflowed = True
            if mask & IN_ISDIR and mask & (IN_CREATE | IN_MOVED_TO):
                with self._lock:
                    parent = self._wds.get(wd)
                    if parent is not None and name:
                        self._add_tree_locked(parent / name, True)
            if not self._pending:
                self._first_pending = now
            self._pending = True
            # Push the deadline out for new activity, but never starve a
            # busy source: pending work fires at most min_spacing after the
            # first event of the burst.
            cap = self._first_pending + max(self._min_spacing, self._debounce)
            self._deadline = min(now + self._debounce, cap)

    def _maybe_trigger(self) -> None:
        if not self._pending or monotonic() < self._deadline:
            return
        now = monotonic()
        spacing = self._last_trigger + self._min_spacing - now
        if spacing > 0:
            self._deadline = now + spacing
            return
        self._pending = False
        self._last_trigger = now
        try:
            self._on_change()
        except Exception as error:
            # Keep watching; the failure type carries no source content.
            self.last_error = f"{type(error).__name__}: {error}"


def _nearest_existing_ancestor(path: Path) -> Path | None:
    candidate = path.parent
    while not candidate.is_dir():
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return candidate
