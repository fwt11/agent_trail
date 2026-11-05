from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import ResumePlan


@dataclass(frozen=True)
class LaunchResult:
    ok: bool
    message: str
    pid: int | None = None


class TerminalLauncher:
    _allowed_executables = frozenset(
        {"kimi", "codebuddy", "mcode", "dst", "pi", "opencode", "codex"}
    )
    _terminals = (
        ("x-terminal-emulator", ("-e",)),
        ("gnome-terminal", ("--",)),
        ("wezterm", ("start", "--cwd", "{workdir}", "--")),
        ("konsole", ("-e",)),
        ("xfce4-terminal", ("-x",)),
        ("kitty", ("--",)),
        ("alacritty", ("-e",)),
        ("foot", ("--",)),
        ("xterm", ("-e",)),
    )

    def __init__(
        self,
        terminal: str | None = None,
        args_template: tuple[str, ...] | None = None,
    ) -> None:
        self.terminal = terminal
        self.args_template = args_template

    def launch(self, plan: ResumePlan) -> LaunchResult:
        if (
            plan.kind != "cli"
            or not plan.executable
            or plan.executable not in self._allowed_executables
        ):
            return LaunchResult(False, "此会话没有已验证的恢复方式")
        if not plan.workdir or not Path(plan.workdir).is_dir():
            return LaunchResult(False, "原工作目录不存在，未启动终端")
        if not shutil.which(plan.executable):
            return LaunchResult(False, f"未在 PATH 中找到 {plan.executable}，未启动终端")
        if not all(isinstance(argument, str) and "\0" not in argument for argument in plan.args):
            return LaunchResult(False, "恢复参数无效，未启动终端")
        try:
            terminal, template = self._terminal_command()
        except ValueError:
            return LaunchResult(False, "终端参数配置无效")
        if terminal is None:
            return LaunchResult(False, "未找到可用终端，请配置 AGENT_TRAIL_TERMINAL")
        command = [plan.executable, *plan.args]
        arguments = [
            argument
            for item in template
            for argument in (
                command
                if item == "{command}"
                else [plan.workdir]
                if item == "{workdir}"
                else [item]
            )
        ]
        if "{command}" not in template:
            arguments.extend(command)
        try:
            process = subprocess.Popen(
                [terminal, *arguments],
                cwd=plan.workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            return LaunchResult(False, "终端启动失败，请检查终端配置")
        return LaunchResult(True, "已发起，请在原工具中确认恢复结果", process.pid)

    def _terminal_command(self) -> tuple[str | None, tuple[str, ...]]:
        terminal_name = self.terminal or os.environ.get("AGENT_TRAIL_TERMINAL")
        if terminal_name:
            template = (
                self.args_template
                if self.args_template is not None
                else self._environment_template()
            )
            if template.count("{command}") > 1:
                raise ValueError("duplicate command placeholder")
            return shutil.which(terminal_name), template
        for name, arguments in self._terminals:
            terminal = shutil.which(name)
            if terminal:
                return terminal, arguments
        return None, ()

    @staticmethod
    def _environment_template() -> tuple[str, ...]:
        raw = os.environ.get("AGENT_TRAIL_TERMINAL_ARGS", "[]")
        value = json.loads(raw)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("terminal arguments must be a string array")
        return tuple(value)
