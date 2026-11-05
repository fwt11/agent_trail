from pathlib import Path

from .base import BaseAdapter
from .claude_code import ClaudeCodeAdapter
from .codebuddy import CodeBuddyAdapter
from .codex import CodexAdapter
from .deepseek_harness import DeepSeekHarnessAdapter
from .kimi_code import KimiCodeAdapter
from .minimax_code import MiniMaxCodeAdapter
from .opencode import OpenCodeAdapter
from .pi import PiAdapter
from .zcode import ZCodeAdapter


def default_adapters(home: Path) -> list[BaseAdapter]:
    return [
        KimiCodeAdapter(home),
        CodeBuddyAdapter(home),
        PiAdapter(home),
        DeepSeekHarnessAdapter(home),
        MiniMaxCodeAdapter(home),
        ZCodeAdapter(home),
        OpenCodeAdapter(home),
        CodexAdapter(home),
        ClaudeCodeAdapter(home),
    ]
