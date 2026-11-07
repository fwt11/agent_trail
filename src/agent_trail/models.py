from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

TitleSource = Literal["native", "fallback", "missing"]
ResumeKind = Literal["cli", "deep_link", "none"]
AdapterStatusKind = Literal["ok", "unavailable", "experimental", "error", "incompatible"]


@dataclass(frozen=True)
class ActivitySession:
    source: str
    session_id: str
    workdir: str
    updated_at: str
    workdir_normalized: str = ""
    title: str | None = None
    title_source: TitleSource = "missing"
    created_at: str | None = None
    additions: int | None = None
    deletions: int | None = None
    files_changed: int | None = None
    source_locator: str = ""
    resume_kind: ResumeKind = "none"
    resume_ref: str | None = None
    source_missing: bool = False
    archived: bool = False
    adapter_version: int = 1
    source_fingerprint: str = ""
    last_scanned_at: str = ""

    def __post_init__(self) -> None:
        if self.title_source not in {"native", "fallback", "missing"}:
            raise ValueError("invalid title_source")
        if self.resume_kind not in {"cli", "deep_link", "none"}:
            raise ValueError("invalid resume_kind")
        if not self.workdir_normalized:
            object.__setattr__(
                self,
                "workdir_normalized",
                _normalize_path(self.workdir),
            )


@dataclass(frozen=True)
class AdapterStatus:
    source: str
    kind: AdapterStatusKind
    message: str = ""
    record_count: int = 0
    error_count: int = 0
    last_scanned_at: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"ok", "unavailable", "experimental", "error", "incompatible"}:
            raise ValueError("invalid adapter status")


@dataclass(frozen=True)
class ScanResult:
    status: AdapterStatus
    fingerprint: str | None
    sessions: list[ActivitySession] = field(default_factory=list)
    changed: bool = True


@dataclass(frozen=True)
class ResumePlan:
    kind: ResumeKind
    workdir: str
    executable: str | None = None
    args: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class SessionFilter:
    since: str | None = None
    until: str | None = None
    sources: tuple[str, ...] = ()
    workdir: str | None = None
    query: str | None = None
    limit: int = 200
    offset: int = 0


def _normalize_path(value: str) -> str:
    """规范化工作目录：POSIX 保持现状；Windows 追加大小写归一。

    NTFS 大小写不敏感，同一目录的两种写法应匹配到同一条记录。
    """
    normalized = os.path.normpath(os.path.expanduser(value))
    if os.name == "nt":
        normalized = normalized.lower()
    return normalized
