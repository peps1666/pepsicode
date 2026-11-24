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
BRAND_LIME = _sgr("38;5;155", color=True)
BRAND_LIME_DIM = _sgr("38;5;113", color=True)
ACCENT = BRAND_LIME
PLAN_ACCENT = _sgr("38;5;81", color=True)
PLAN_BADGE_BG = _sgr("48;5;23", color=True)
BUILD_BADGE_BG = _sgr("48;5;22", color=True)
WARN_BADGE_BG = _sgr("48;5;58", color=True)
TEXT = _sgr("38;5;252", color=True)
TEXT_FAINT = _sgr("38;5;240", color=True)
BORDER = _sgr("38;5;239", color=True)
BORDER_ACTIVE = BRAND_LIME
BORDER_DIM = _sgr("38;5;236", color=True)
SUBTLE = _sgr("38;5;245", color=True)
SURFACE_BG = _sgr("48;5;235", color=True)
HIGHLIGHT_BG = _sgr("48;5;238", color=True)
PROGRESS_TRACK_BG = _sgr("48;5;237", color=True)
CODE_BG = _sgr("48;5;235", color=True)
# Raised surface behind a user turn: one step above the terminal background, so
# the block reads as a card without competing with code blocks or selections.
USER_BG = _sgr("48;5;236", color=True)
DIFF_ADD_BG = _sgr("48;5;22", color=True)
DIFF_DEL_BG = _sgr("48;5;52", color=True)
CODE_FG = _sgr("38;5;117", color=True)
QUOTE_BAR = BRAND_LIME_DIM
HEADING_ACCENT = BRAND_LIME
