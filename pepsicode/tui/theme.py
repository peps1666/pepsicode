"""Shared terminal palette, with plain output for pipes and dumb terminals."""

from __future__ import annotations

import os
import sys

_STYLES_ENABLED = os.environ.get("TERM") != "dumb" and (
    sys.stdout.isatty() or os.environ.get("CLICOLOR_FORCE", "0") not in ("", "0")
)
_COLORS_ENABLED = _STYLES_ENABLED and "NO_COLOR" not in os.environ


def _sgr(code: str, *, color: bool = False) -> str:
    enabled = _COLORS_ENABLED if color else _STYLES_ENABLED
    return f"\x1b[{code}m" if enabled else ""


RESET = _sgr("0")
DIM = _sgr("2")
BOLD = _sgr("1")
REVERSE = _sgr("7")
ITALIC = _sgr("3")
UNDERLINE = _sgr("4")
CYAN = _sgr("36", color=True)
GREEN = _sgr("32", color=True)
YELLOW = _sgr("33", color=True)
RED = _sgr("31", color=True)
BLUE = _sgr("34", color=True)
MAGENTA = _sgr("35", color=True)
BRIGHT_GREEN = _sgr("92", color=True)
BRIGHT_RED = _sgr("91", color=True)
BRIGHT_CYAN = _sgr("96", color=True)
BRIGHT_YELLOW = _sgr("93", color=True)
BRIGHT_BLUE = _sgr("94", color=True)
BRIGHT_MAGENTA = _sgr("95", color=True)
BRIGHT_WHITE = _sgr("97", color=True)
ACCENT = _sgr("38;5;215", color=True)
BORDER = _sgr("38;5;240", color=True)
BORDER_DIM = _sgr("38;5;237", color=True)
SUBTLE = _sgr("38;5;245", color=True)
HIGHLIGHT_BG = _sgr("48;5;237", color=True)
CODE_BG = _sgr("48;5;236", color=True)
CODE_FG = ACCENT
QUOTE_BAR = SUBTLE
HEADING_ACCENT = ACCENT
