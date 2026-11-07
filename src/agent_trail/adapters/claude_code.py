from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ..models import ActivitySession, AdapterStatus, ResumePlan, ScanResult
from .base import BaseAdapter
from .jsonl_utils import fingerprint_paths


class ClaudeCodeAdapter(BaseAdapter):
    source = "claude-code"
    adapter_version = 1

    @property
    def root(self) -> Path:
        # 官方行为：CLAUDE_CONFIG_DIR 重定向整个配置目录（含 projects/），
        # Windows 默认 %USERPROFILE%/.claude —— Path.home() 全平台一致。
        base = os.environ.get("CLAUDE_CONFIG_DIR")
        root = Path(base) if base else self.home / ".claude"
        return root / "projects"

    def discover(self) -> AdapterStatus:
        if not self.root.is_dir():
            return AdapterStatus(source=self.source, kind="unavailable")
        return AdapterStatus(
            source=self.source,
            kind="experimental",
            message="session metadata format has not been verified",
        )

    def scan(self, previous_fingerprint: str | None, now: datetime) -> ScanResult:
        discovery = self.discover()
        if discovery.kind == "unavailable":
            return ScanResult(discovery, None, [], True)
        fingerprint = fingerprint_paths([], self.adapter_version)
        return ScanResult(discovery, fingerprint, [], fingerprint != previous_fingerprint)

    def resume_plan(self, session: ActivitySession) -> ResumePlan:
        return ResumePlan(
            kind="none",
            workdir=session.workdir,
            reason="Claude Code resume has not been verified",
        )
