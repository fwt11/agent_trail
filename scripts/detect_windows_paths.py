#!/usr/bin/env python3
"""session-trail 闭源 Agent 会话路径探测器（Windows）。

目标：定位 Kimi Code / CodeBuddy / MiniMax Code / ZCode / DeepSeek Harness
在 Windows 上的会话数据目录，供 session-trail 适配器使用。

隐私边界（与主项目一致）：只收集 **路径 / 大小 / 修改时间 / 扩展名**，
绝不读取任何文件内容。

用法（在 Windows 上，任意 Python 3.8+）：
  python detect_session_paths.py            # 一条龙：探测 → 基线快照 →
                                            # 引导你在各 Agent 里跑一句话 →
                                            # 对比 → 生成报告
  python detect_session_paths.py snapshot   # 仅保存基线快照
  python detect_session_paths.py diff       # 与基线对比，输出报告

报告默认写到 %USERPROFILE%\\.agent-trail\\path-report.txt，把这份文件
发回来即可。
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Windows 控制台可能是 GBK，统一转 UTF-8 避免中文输出崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOME = Path(os.environ.get("USERPROFILE") or Path.home())
APPDATA = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")

AGENT_NAMES = ("kimi-code", "codebuddy", "minimax-code", "zcode", "deepseek-harness")

# 各闭源工具的候选目录（Windows 惯例猜测——猜不中没关系，快照对比会兜底）
CANDIDATES: dict[str, list[str]] = {
    "kimi-code": [
        ".kimi-code", ".kimi",
        "AppData/Roaming/kimi-code", "AppData/Roaming/Kimi Code",
        "AppData/Local/kimi-code", "AppData/Local/Kimi",
    ],
    "codebuddy": [
        ".codebuddy", ".codebuddy-win",
        "AppData/Roaming/CodeBuddy", "AppData/Roaming/codebuddy",
        "AppData/Local/CodeBuddy", "AppData/Local/codebuddy",
        "AppData/Roaming/Tencent/CodeBuddy",
    ],
    "minimax-code": [
        ".minimax", "AppData/Roaming/minimax-code", "AppData/Roaming/MiniMax",
        "AppData/Local/MiniMax", "AppData/Local/minimax-code",
    ],
    "zcode": [
        ".zcode", "AppData/Roaming/zcode", "AppData/Roaming/ZCode",
        "AppData/Local/zcode", "AppData/Local/ZCode",
    ],
    "deepseek-harness": [
        ".dsh-tui", ".dsh", ".deepseek",
        "AppData/Roaming/DeepSeek", "AppData/Local/DeepSeek",
        "AppData/Roaming/dsh-tui", "AppData/Local/dsh-tui",
    ],
}

# 快照时跳过的噪音目录（缓存/系统/包管理器，量大且无关）
PRUNE_DIRS = {
    "cache", "cache2", "code cache", "gpucache", "temp", "tmp",
    "node_modules", ".git", "__pycache__", "packages", "crashreports",
    "crashdumps", "pip", "venv", ".venv", "site-packages", "windowsapps",
    "squirreltemp", "d3dscache", "logs", "diagnostics", "saved state",
    "service worker", "cachestorage", "webrtc logs", "Crashpad",
}
PRUNE_PREFIXES = ("microsoft",)  # AppData 下的 Microsoft 系统目录

SESSION_EXTS = {".jsonl", ".sqlite", ".db", ".sqlite3", ".json", ".db-wal"}
SNAPSHOT_LIMIT = 1_000_000  # 文件数安全上限

# 去重：Windows 上 APPDATA/LOCALAPPDATA 在 HOME 内部，整个 HOME 遍历已覆盖；
# 只有它们在 HOME 之外时才作为独立 scope（省 2/3 重复遍历——真实机器上
# 用户目录动辄 50 万文件，重复遍历会撞上限导致漏测）。
SCOPE_DIRS = [HOME]
for _extra in (APPDATA, LOCALAPPDATA):
    try:
        if os.path.realpath(_extra).lower().startswith(
            os.path.realpath(HOME).lower() + os.sep
        ) or os.path.realpath(_extra).lower() == os.path.realpath(HOME).lower():
            continue
    except OSError:
        continue
    SCOPE_DIRS.append(_extra)


def scope_key(path: Path) -> str:
    """把绝对路径归到所属 scope 下的相对路径。"""
    resolved = Path(path)
    for scope in SCOPE_DIRS:
        try:
            return str(resolved.relative_to(scope))
        except ValueError:
            continue
    return str(resolved)


def walk_files() -> dict[str, list]:
    """遍历 scope 目录（跳过噪音），返回 {相对路径: [size, mtime_ns]}。"""
    snapshot: dict[str, list] = {}
    seen_dirs: set[str] = set()
    count = 0
    start = time.monotonic()
    for scope in SCOPE_DIRS:
        if not scope.is_dir():
            continue
        for current, dirs, files in os.walk(scope, topdown=True, onerror=None):
            current_path = Path(current)
            rel_dir = scope_key(current_path)
            if rel_dir in seen_dirs:  # LOCALAPPDATA 与 HOME 重叠时防重复
                dirs[:] = []
                continue
            seen_dirs.add(rel_dir)
            dirs[:] = [d for d in dirs if d.lower() not in PRUNE_DIRS
                       and not any(d.lower().startswith(p) for p in PRUNE_PREFIXES)]
            for name in files:
                file_path = current_path / name
                rel = scope_key(file_path)
                if rel in snapshot:
                    continue
                try:
                    stat = file_path.stat()
                except OSError:
                    continue
                snapshot[rel] = [stat.st_size, stat.st_mtime_ns]
                count += 1
                if count >= SNAPSHOT_LIMIT:
                    print(f"  [警告] 文件数达到上限 {SNAPSHOT_LIMIT}，提前结束遍历")
                    return snapshot
    elapsed = time.monotonic() - start
    print(f"  遍历完成：{len(snapshot)} 个文件，耗时 {elapsed:.1f}s")
    return snapshot


def probe_candidates() -> None:
    """直接探测候选目录：存在性 + 会话样文件统计。"""
    print("[1/4] 候选目录探测")
    found_any = False
    for agent, rels in CANDIDATES.items():
        for rel in rels:
            target = HOME / rel.replace("/", os.sep)
            if not target.exists():
                continue
            found_any = True
            stats: Counter = Counter()
            newest = ""
            newest_mtime = 0.0
            for current, _dirs, files in os.walk(target, onerror=None):
                for name in files:
                    ext = Path(name).suffix.lower()
                    stats[ext or "(无扩展名)"] += 1
                    try:
                        mtime = (Path(current) / name).stat().st_mtime
                    except OSError:
                        continue
                    if mtime > newest_mtime:
                        newest_mtime = mtime
                        newest = str((Path(current) / name))
            summary = ", ".join(f"{k}×{v}" for k, v in stats.most_common(6)) or "空目录"
            print(f"  [{agent}] {target}  →  {summary}")
            if newest:
                print(f"           最新文件: {newest}")
    if not found_any:
        print("  （候选目录都不存在——正常，快照对比会揭示真实位置）")


def load_snapshot(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        print(f"  基线读取失败: {error}")
        return None


def save_snapshot(snapshot: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")


def diff_report(before: dict, after: dict, out_path: Path) -> str:
    new_files = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(
        key for key in (set(before) & set(after))
        if before[key] != after[key]
    )
    lines: list[str] = []
    lines.append(f"报告生成: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"基线文件数: {len(before)}  对比文件数: {len(after)}")
    lines.append(f"新增: {len(new_files)}  修改: {len(modified)}  消失: {len(removed)}")
    lines.append("")
    lines.append("=== 按目录分组的摘要（新增+修改，按数量排序）===")

    groups: dict[str, Counter] = defaultdict(Counter)
    exts: dict[str, Counter] = defaultdict(Counter)
    for key, kind in (
        [(k, "new") for k in new_files] + [(k, "mod") for k in modified]
    ):
        parts = key.replace("/", os.sep).split(os.sep)
        group = os.sep.join(parts[:2]) if len(parts) > 1 else parts[0]
        groups[group][kind] += 1
        exts[group][Path(key).suffix.lower() or "(无)"] += 1

    keywords = ("kimi", "codebuddy", "minimax", "zcode", "dsh", "deepseek", "mavis")
    def rank(item):
        group_lower = item[0].lower()
        is_agent = any(k in group_lower for k in keywords)
        total = item[1]["new"] + item[1]["mod"]
        # 五家相关的组永远置顶（按总量），其余按数量排后
        return (0 if is_agent else 1, -total if is_agent else total)
    ranked = sorted(groups.items(), key=rank)
    for group, counter in ranked:
        total = counter["new"] + counter["mod"]
        ext_summary = ", ".join(f"{k}×{v}" for k, v in exts[group].most_common(6))
        lines.append(f"  {group}: 新 {counter['new']} / 改 {counter['mod']} | {ext_summary}")

    lines.append("")
    lines.append("=== 会话样扩展名（.jsonl/.sqlite/.db）的新增文件（最多 60 条路径）===")
    interesting = [k for k in new_files if Path(k).suffix.lower() in SESSION_EXTS]
    lines.append(f"（共 {len(interesting)} 个，只列路径不读内容）")
    for key in interesting[:60]:
        lines.append(f"  {key}")
    if len(interesting) > 60:
        lines.append("  ...（其余见 JSON）")

    lines.append("")
    lines.append("=== 孤儿提示：消失的文件（通常不用关心，最多 10 条）===")
    for key in removed[:10]:
        lines.append(f"  {key}")

    lines.append("")
    lines.append("（完整 JSON 清单已另存为同名 .json 文件）")
    report = "\n".join(lines)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    json_path = out_path.with_suffix(".json")
    json_path.write_text(json.dumps(
        {"new": new_files, "modified": modified, "removed": removed},
        ensure_ascii=False,
    ), encoding="utf-8")
    return report


def main(argv: list[str]) -> int:
    state_dir = HOME / ".agent-trail"
    baseline_path = Path(os.environ.get("TEMP") or state_dir) / "session-snapshot-baseline.json"
    report_path = state_dir / "path-report.txt"
    mode = argv[1] if len(argv) > 1 else "all"

    if mode == "snapshot":
        print("[基线快照]")
        save_snapshot(walk_files(), baseline_path)
        print(f"  已保存: {baseline_path}")
        return 0

    if mode == "diff":
        before = load_snapshot(baseline_path)
        if before is None:
            print("  没有基线快照，请先运行: python detect_session_paths.py snapshot")
            return 1
        after = walk_files()
        print(diff_report(before, after, report_path))
        print(f"\n报告已写入: {report_path}")
        return 0

    # 一条龙
    print("session-trail 闭源 Agent 会话路径探测器（只收集路径/大小/时间，不读文件内容）")
    print(f"HOME={HOME}\nAPPDATA={APPDATA}\nLOCALAPPDATA={LOCALAPPDATA}\n")
    probe_candidates()

    print("\n[2/4] 建立基线快照")
    before = walk_files()
    save_snapshot(before, baseline_path)

    print("\n[3/4] 请现在依次打开以下工具，各发一句话（会话里随便说点什么）：")
    for name in AGENT_NAMES:
        print(f"    - {name}")
    input("完成后回到这里按回车继续 > ")

    print("[4/4] 对比快照")
    after = walk_files()
    report = diff_report(before, after, report_path)
    print(report)
    print(f"\n完整报告已写入: {report_path}")
    print("把这份文件发回来即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
