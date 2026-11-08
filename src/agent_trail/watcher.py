"""Change notification for agent session sources, with pluggable backends.

The watcher never replaces the scan itself: it only replaces the timer
that wakes :meth:`ScanService.scan_all`.  Fingerprints remain the cheap
dedup layer, so any spurious trigger degrades to a fast no-op.

Backends:
- Linux: ``SourceWatcher`` — inotify(7) via ctypes against libc, seconds
  level latency, directory-level watches with recursive growth.
- Windows/macOS (and Linux fallback): ``PollingWatcher`` — a background
  thread polls a lightweight signature (paths + size + mtime) of the
  watched roots every few seconds and feeds the same debounce window.
"""

from __future__ import annotations

import ctypes
import os
import select
import struct
import sys
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
        if not sys.platform.startswith("linux"):
            # dlopen(None) 是 POSIX 约定；Windows 上 CDLL(None) 直接 TypeError
            return
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
        except (OSError, AttributeError, TypeError):
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


class BaseWatcher:
    """Shared watcher interface + the debounce/minimum-spacing time window.

    Backends detect change and call :meth:`_note_activity`; the shared
    window coalesces bursts and :meth:`_maybe_trigger` invokes
    ``on_change`` when the deadline passes.  ``on_change`` runs on the
    backend thread; the scan lock keeps it serialized against manual
    refreshes and the safety-net poll.
    """

    kind = "base"

    def __init__(
        self,
        roots: list[tuple[Path, bool]],
        on_change: Callable[[], None],
        *,
        debounce_seconds: float = 1.0,
        min_spacing_seconds: float = 5.0,
    ) -> None:
        self._roots = [(Path(path), recursive) for path, recursive in roots]
        self._on_change = on_change
        self._debounce = debounce_seconds
        self._min_spacing = min_spacing_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pending = False
        self._deadline = 0.0
        self._first_pending = 0.0
        self._last_trigger = 0.0
        self.last_error: str | None = None

    @property
    def healthy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def watch_count(self) -> int:
        raise NotImplementedError

    def start(self) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def resync(self) -> None:
        """Re-arm the backend after safety-net scans (idempotent)."""
        return None

    # -- shared debounce window ----------------------------------------------

    def _note_activity(self, now: float) -> None:
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


class SourceWatcher(BaseWatcher):
    """Debounced, directory-level inotify watcher (Linux)."""

    kind = "inotify"

    def __init__(
        self,
        roots: list[tuple[Path, bool]],
        on_change: Callable[[], None],
        *,
        debounce_seconds: float = 1.0,
        min_spacing_seconds: float = 5.0,
        max_watches: int = 2048,
    ) -> None:
        super().__init__(
            roots, on_change,
            debounce_seconds=debounce_seconds, min_spacing_seconds=min_spacing_seconds,
        )
        self._max_watches = max_watches
        self._libc = libc_inotify()
        self._fd: int | None = None
        self._lock = threading.Lock()
        self._wds: dict[int, Path] = {}
        self._paths: set[Path] = set()
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
        super().stop()
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
            self._note_activity(now)


class PollingWatcher(BaseWatcher):
    """Portable backend: poll a lightweight signature of the watched roots.

    Used on Windows/macOS where inotify does not exist.  The signature is
    existence + per-file (size, mtime_ns) over the root trees — enough to
    notice session files appearing, growing or vanishing without parsing
    any content.  Resolution is therefore ``poll_seconds`` (default 5s)
    rather than seconds-level like inotify; the fingerprint dedup layer
    makes extra triggers cheap.
    """

    kind = "polling"

    def __init__(
        self,
        roots: list[tuple[Path, bool]],
        on_change: Callable[[], None],
        *,
        debounce_seconds: float = 1.0,
        min_spacing_seconds: float = 5.0,
        poll_seconds: float = 5.0,
    ) -> None:
        super().__init__(
            roots, on_change,
            debounce_seconds=debounce_seconds, min_spacing_seconds=min_spacing_seconds,
        )
        self._poll_seconds = poll_seconds
        self._signature: tuple | None = None

    @property
    def watch_count(self) -> int:
        return len(self._roots)

    def start(self) -> bool:
        try:
            self._signature = self._compute_signature()
        except Exception as error:
            self.last_error = f"signature scan failed: {type(error).__name__}"
            return False
        self._thread = threading.Thread(
            target=self._run, name="agent-trail-watch", daemon=True
        )
        self._thread.start()
        return True

    # -- signature ------------------------------------------------------------

    def _compute_signature(self) -> tuple:
        parts: list = []
        for path, recursive in self._roots:
            if not path.is_dir():
                # Missing root: watch the nearest existing ancestor so its
                # creation is noticed (same semantics as the inotify backend).
                ancestor = _nearest_existing_ancestor(path)
                if ancestor is None:
                    parts.append((str(path), "missing"))
                    continue
                parts.append((str(path), "missing", self._dir_signature(ancestor, False)))
            elif recursive:
                parts.append((str(path), True, self._tree_signature(path)))
            else:
                parts.append((str(path), False, self._dir_signature(path, True)))
        return tuple(parts)

    def _tree_signature(self, root: Path) -> tuple:
        parts: list = []
        for current, dirs, files in os.walk(root):
            dirs.sort()
            for name in sorted(files):
                file_path = Path(current) / name
                try:
                    stat = file_path.stat()
                    parts.append((str(file_path), stat.st_size, stat.st_mtime_ns))
                except OSError:
                    parts.append((str(file_path), "unreadable"))
        return tuple(parts)

    def _dir_signature(self, directory: Path, entries: bool) -> tuple:
        parts: list = [str(directory)]
        try:
            stat = directory.stat()
            parts.append((stat.st_size, stat.st_mtime_ns))
        except OSError:
            parts.append("unreadable")
            return tuple(parts)
        if entries:
            try:
                for entry in sorted(directory.iterdir()):
                    try:
                        stat = entry.stat()
                        parts.append((entry.name, stat.st_size, stat.st_mtime_ns))
                    except OSError:
                        parts.append((entry.name, "unreadable"))
            except OSError:
                parts.append("unreadable-entries")
        return tuple(parts)

    # -- loop -----------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            now = monotonic()
            try:
                signature = self._compute_signature()
            except Exception as error:
                self.last_error = f"signature scan failed: {type(error).__name__}"
                continue
            if signature != self._signature:
                self._signature = signature
                self._note_activity(now)
            self._maybe_trigger()
        # Drain a pending trigger on shutdown so the last change is not lost.
        self._maybe_trigger()


def create_watcher(
    roots: list[tuple[Path, bool]],
    on_change: Callable[[], None],
    *,
    debounce_seconds: float = 1.0,
    min_spacing_seconds: float = 5.0,
    poll_seconds: float = 5.0,
    **kwargs,
) -> BaseWatcher:
    """Pick the best backend for the current platform."""
    if sys.platform.startswith("linux"):
        return SourceWatcher(
            roots, on_change,
            debounce_seconds=debounce_seconds,
            min_spacing_seconds=min_spacing_seconds,
            **kwargs,
        )
    return PollingWatcher(
        roots, on_change,
        debounce_seconds=debounce_seconds,
        min_spacing_seconds=min_spacing_seconds,
        poll_seconds=poll_seconds,
    )


def _nearest_existing_ancestor(path: Path) -> Path | None:
    candidate = path.parent
    while not candidate.is_dir():
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return candidate
