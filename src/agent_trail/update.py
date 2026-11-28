"""PyPI 版本检查与升级执行。

隐私与安全边界：
- 版本检查只向 pypi.org 查询本包的最新版本号（元数据查询，不携带任何
  用户数据），结果缓存一天；`--no-update-check` / AGENT_TRAIL_NO_UPDATE_CHECK
  可整体关闭
- 升级必须由用户显式触发（托盘菜单 / `agent-trail update`），不做静默
  自动更新——适配器在追赶各 agent 的数据格式，静默升级可能在用户不知情
  时改变行为
- 升级在当前解释器环境执行（venv/pipx 自感知）；PEP 668 外部管控环境
  （Debian 系统 Python）诚实降级为提示手动命令
"""

from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys
import sysconfig
import time
import urllib.request
from pathlib import Path

PACKAGE = "session-trail"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_INTERVAL_SECONDS = 86400.0
FETCH_TIMEOUT = 10.0
UPGRADE_TIMEOUT = 900.0


def no_update_check() -> bool:
    import os

    return os.environ.get("AGENT_TRAIL_NO_UPDATE_CHECK", "").strip().lower() in {"1", "true", "yes", "on"}


def current_version() -> str | None:
    try:
        return importlib.metadata.version(PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return None


def _version_tuple(value: str) -> tuple[int, ...]:
    """0.6.0 → (0, 6, 0)；容忍 0.6.0rc1/a2.post3 等后缀（数字段取整）。"""
    parts: list[int] = []
    for chunk in value.strip().split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def has_update(current: str | None, latest: str | None) -> bool:
    if not current or not latest:
        return False
    return _version_tuple(latest) > _version_tuple(current)


def _cache_path(data_dir: Path) -> Path:
    return data_dir / "update-check.json"


def read_cache(data_dir: Path) -> dict | None:
    try:
        data = json.loads(_cache_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def write_cache(data_dir: Path, latest: str | None) -> None:
    try:
        _cache_path(data_dir).parent.mkdir(parents=True, exist_ok=True)
        _cache_path(data_dir).write_text(
            json.dumps({"latest": latest, "checked_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass  # 缓存失败不影响功能


def latest_version(data_dir: Path, *, force: bool = False) -> str | None:
    """查询 PyPI 最新版本；缓存一天。网络失败时返回陈旧缓存或 None。"""
    cached = read_cache(data_dir)
    if (
        not force
        and cached is not None
        and time.time() - float(cached.get("checked_at") or 0) < CHECK_INTERVAL_SECONDS
    ):
        return cached.get("latest")
    try:
        request = urllib.request.Request(PYPI_URL, headers={"User-Agent": f"{PACKAGE}"})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        latest = str(data.get("info", {}).get("version") or "") or None
        write_cache(data_dir, latest)
        return latest
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, KeyError):
        return cached.get("latest") if cached else None


def _is_pipx() -> bool:
    return "pipx" in Path(sys.prefix).parts or "pipx" in Path(sys.executable).parts


def _externally_managed() -> bool:
    """PEP 668：Debian 系统受管环境直接 pip install 会被拒绝。"""
    stdlib = Path(sysconfig.get_path("stdlib"))
    return (stdlib.parent / "EXTERNALLY-MANAGED").is_file()


def upgrade_plan() -> dict | None:
    """返回升级命令计划；None 表示该环境无法自升级（附手动提示）。"""
    if _externally_managed():
        return None
    if _is_pipx() and shutil.which("pipx"):
        return {"kind": "pipx", "cmd": ["pipx", "upgrade", PACKAGE]}
    import importlib.util

    if importlib.util.find_spec("pip") is not None:
        return {"kind": "pip", "cmd": [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]}
    return None


def manual_hint() -> str:
    if _externally_managed():
        return "pip install --upgrade session-trail（当前为系统受管环境，请用虚拟环境/pipx）"
    if _is_pipx():
        return "pipx upgrade session-trail"
    return f"{sys.executable} -m pip install --upgrade session-trail"


def run_upgrade(cmd: list[str], on_line=None, timeout: float = UPGRADE_TIMEOUT) -> tuple[bool, str]:
    """执行升级命令，逐行回调输出；返回 (是否成功, 汇总输出)。"""
    lines: list[str] = []
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        return False, f"failed to start: {error}"
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    for line in process.stdout:
        stripped = line.rstrip("\r\n")
        lines.append(stripped)
        if on_line is not None:
            try:
                on_line(stripped)
            except Exception:
                pass
        if time.monotonic() > deadline:
            process.kill()
            lines.append(f"[timed out after {int(timeout)}s]")
            break
    code = process.wait(timeout=30)
    return code == 0, "\n".join(lines[-80:])
