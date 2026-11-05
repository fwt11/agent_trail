from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .models import ActivitySession, SessionFilter

_COLUMNS = (
    "source",
    "session_id",
    "workdir",
    "workdir_normalized",
    "title",
    "title_source",
    "created_at",
    "updated_at",
    "additions",
    "deletions",
    "files_changed",
    "source_locator",
    "resume_kind",
    "resume_ref",
    "source_missing",
    "archived",
    "adapter_version",
    "source_fingerprint",
    "last_scanned_at",
)


class LocalStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                source TEXT NOT NULL,
                session_id TEXT NOT NULL,
                workdir TEXT NOT NULL,
                workdir_normalized TEXT NOT NULL,
                title TEXT,
                title_source TEXT NOT NULL,
                created_at TEXT,
                updated_at TEXT NOT NULL,
                additions INTEGER,
                deletions INTEGER,
                files_changed INTEGER,
                source_locator TEXT NOT NULL,
                resume_kind TEXT NOT NULL,
                resume_ref TEXT,
                source_missing INTEGER NOT NULL,
                archived INTEGER NOT NULL,
                adapter_version INTEGER NOT NULL,
                source_fingerprint TEXT NOT NULL,
                last_scanned_at TEXT NOT NULL,
                PRIMARY KEY (source, session_id)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS adapter_state (
                source TEXT PRIMARY KEY,
                fingerprint TEXT,
                last_scanned_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS sessions_updated_at ON sessions(updated_at DESC)"
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> LocalStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def replace_source(
        self,
        source: str,
        sessions: Iterable[ActivitySession],
        fingerprint: str | None,
        scanned_at: str,
    ) -> None:
        records = list(sessions)
        if any(record.source != source for record in records):
            raise ValueError("session source does not match adapter")
        placeholders = ",".join("?" for _ in _COLUMNS)
        assignments = ",".join(f"{column}=excluded.{column}" for column in _COLUMNS[2:])
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE sessions SET source_missing=1, resume_kind='none', "
                "resume_ref=NULL, last_scanned_at=? WHERE source=?",
                (scanned_at, source),
            )
            self._connection.executemany(
                f"INSERT INTO sessions ({','.join(_COLUMNS)}) VALUES ({placeholders}) "
                f"ON CONFLICT(source, session_id) DO UPDATE SET {assignments}",
                [self._values(record) for record in records],
            )
            self._connection.execute(
                "INSERT INTO adapter_state(source, fingerprint, last_scanned_at) VALUES(?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET fingerprint=excluded.fingerprint, "
                "last_scanned_at=excluded.last_scanned_at",
                (source, fingerprint, scanned_at),
            )

    def mark_unchanged(
        self, source: str, fingerprint: str | None, scanned_at: str
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO adapter_state(source, fingerprint, last_scanned_at) VALUES(?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET fingerprint=excluded.fingerprint, "
                "last_scanned_at=excluded.last_scanned_at",
                (source, fingerprint, scanned_at),
            )

    def list_sessions(self, filters: SessionFilter) -> list[ActivitySession]:
        clauses: list[str] = []
        parameters: list[object] = []
        if filters.since is not None:
            clauses.append("updated_at >= ?")
            parameters.append(filters.since)
        if filters.until is not None:
            clauses.append("updated_at < ?")
            parameters.append(filters.until)
        if filters.sources:
            clauses.append(f"source IN ({','.join('?' for _ in filters.sources)})")
            parameters.extend(filters.sources)
        if filters.workdir is not None:
            clauses.append("workdir_normalized = ?")
            parameters.append(filters.workdir)
        if filters.query:
            escaped = self._escape_like(filters.query)
            clauses.append("(title LIKE ? ESCAPE '\\' OR workdir LIKE ? ESCAPE '\\')")
            pattern = f"%{escaped}%"
            parameters.extend((pattern, pattern))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((filters.limit, filters.offset))
        with self._lock:
            rows = self._connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM sessions {where} "
                "ORDER BY updated_at DESC, source, session_id LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
            return [self._record(row) for row in rows]

    def get_session(self, source: str, session_id: str) -> ActivitySession | None:
        with self._lock:
            row = self._connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM sessions WHERE source=? AND session_id=?",
                (source, session_id),
            ).fetchone()
            return self._record(row) if row is not None else None

    def get_fingerprint(self, source: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT fingerprint FROM adapter_state WHERE source=?", (source,)
            ).fetchone()
            return row[0] if row is not None else None

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _values(record: ActivitySession) -> tuple[object, ...]:
        values: list[object] = []
        for column in _COLUMNS:
            value = getattr(record, column)
            values.append(int(value) if isinstance(value, bool) else value)
        return tuple(values)

    @staticmethod
    def _record(row: sqlite3.Row) -> ActivitySession:
        values = dict(row)
        values["source_missing"] = bool(values["source_missing"])
        values["archived"] = bool(values["archived"])
        return ActivitySession(**values)
