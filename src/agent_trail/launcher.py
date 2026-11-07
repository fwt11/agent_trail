from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
    # Linux 检测列表；Windows/macOS 见 _terminals_for_platform()。
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
    _terminals_windows = (
        ("wt.exe", ("-d", "{workdir}", "cmd", "/k", "{command}")),
        ("cmd.exe", ("/c", "start", "/D", "{workdir}", "{command}")),
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
            return LaunchResult(False, "resume.no_verified_path")
        if not plan.workdir or not Path(plan.workdir).is_dir():
            return LaunchResult(False, "resume.workdir_missing")
        if not shutil.which(plan.executable):
            return LaunchResult(False, "resume.cli_not_found")
        if not all(isinstance(argument, str) and "\0" not in argument for argument in plan.args):
            return LaunchResult(False, "resume.invalid_args")
        try:
            terminal, template = self._terminal_command()
        except ValueError:
            return LaunchResult(False, "resume.terminal_config_invalid")
        if terminal is None:
            return LaunchResult(False, "resume.no_terminal")
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
        popen_kwargs = {"close_fds": True}
        if os.name == "posix":
            # 新会话脱离当前终端；Windows 上该参数不存在（ValueError）。
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(
                [terminal, *arguments],
                cwd=plan.workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_kwargs,
            )
        except OSError:
            return LaunchResult(False, "resume.launch_failed")
        return LaunchResult(True, "resume.launch_ok", process.pid)

    @staticmethod
    def _terminals_for_platform() -> tuple:
        if sys.platform == "win32":
            return TerminalLauncher._terminals_windows
        return TerminalLauncher._terminals  # POSIX（含 macOS 的回退路径）

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
        for name, arguments in self._terminals_for_platform():
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
