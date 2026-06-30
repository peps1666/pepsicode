from __future__ import annotations

from .chrome import (
    ACCENT,
    BOLD,
    DIM,
    HIGHLIGHT_BG,
    ICON_PROMPT,
    RESET,
)


def render_input_prompt(current_input: str, cursor_offset: int) -> str:
    """Claude-style: single > prompt, no hint bar."""
    offset = max(0, min(cursor_offset, len(current_input)))
    before = current_input[:offset]
    current = current_input[offset] if offset < len(current_input) else " "
    after = current_input[offset + 1 :]

    placeholder = f"{DIM}Ask anything...{RESET}" if not current_input else ""

    prompt_icon = f"{ACCENT}{ICON_PROMPT}{RESET}"
    return f" {prompt_icon} {before}{HIGHLIGHT_BG}{BOLD}{current}{RESET}{after}{placeholder}"
