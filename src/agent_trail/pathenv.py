"""桌面启动形态的 PATH 补全。

从应用菜单/托盘启动时，进程 PATH 只有系统默认几项，不包含用户 shell
里配置的 Agent CLI 安装目录（如 ~/.kimi-code/bin），导致控制台"找不到
Agent"。这里把各 CLI 的常见安装目录前置进进程 PATH（存在才加、去重、
不改相对顺序）——扫描、对话能力探测、恢复启动、聊天子进程全链路共享。
"""

from __future__ import annotations

import os
from pathlib import Path

# 各 Agent CLI 的常见安装目录（按 ~ 展开探测；Windows 同样适用）
CANDIDATE_DIRS = (
    "~/.kimi-code/bin",      # Kimi Code CLI
    "~/.local/bin",          # pipx / claude 原生安装器 / opencode 安装脚本
    "~/bin",
    "~/.opencode/bin",       # OpenCode
    "~/.codebuddy/bin",      # CodeBuddy
    "~/.claude/local",       # Claude Code（npm 本地形态）
    "~/.npm-global/bin",     # npm 自定义全局前缀
    "~/.minimax-code/bin",   # MiniMax Code
    "~/.yarn/bin",
)


def augment_process_path(extra_dirs=()) -> list[str]:
    """把候选目录前置进进程 PATH；返回本次新增的目录列表（幂等）。"""
    seen = {
        os.path.normpath(part)
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part
    }
    prepended: list[str] = []
    candidates = [os.path.expanduser(d) for d in (*CANDIDATE_DIRS, *extra_dirs)]
    for directory in candidates:
        normalized = os.path.normpath(directory)
        if not os.path.isdir(normalized) or normalized in seen:
            continue
        seen.add(normalized)
        prepended.append(normalized)
    if prepended:
        os.environ["PATH"] = os.pathsep.join(
            [*prepended, os.environ.get("PATH", "")]
        )
    return prepended
