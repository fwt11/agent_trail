from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JsonlReadResult:
    records: list[dict[str, Any]]
    error_count: int


def read_jsonl_metadata(path: Path, allowed_fields: set[str]) -> JsonlReadResult:
    records: list[dict[str, Any]] = []
    error_count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                error_count += 1
                continue
            if not isinstance(value, dict):
                error_count += 1
                continue
            records.append({key: value[key] for key in allowed_fields if key in value})
    return JsonlReadResult(records=records, error_count=error_count)


def to_utc_iso(value: Any) -> str:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    numeric = float(value)
    if abs(numeric) >= 100_000_000_000:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, timezone.utc).isoformat()


def fingerprint_paths(paths: list[Path], adapter_version: int) -> str:
    digest = hashlib.sha256(f"adapter:{adapter_version}\n".encode())
    for path in sorted(paths, key=lambda item: str(item)):
        stat = path.stat()
        digest.update(f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()
