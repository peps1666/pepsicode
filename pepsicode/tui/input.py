from __future__ import annotations

from .chrome import (
    ACCENT,
    BOLD,
    DIM,
    ICON_PROMPT,
    RESET,
    REVERSE,
    char_display_width,
    string_display_width,
    truncate_plain,
)


def render_input_prompt(
    current_input: str, cursor_offset: int, width: int | None = None, hint: str = "Ask anything..."
) -> str:
    """A prompt with a horizontal viewport that always includes the cursor."""
    offset = max(0, min(cursor_offset, len(current_input)))
    # Show pasted newlines on one row without changing the submitted input.
    visible_input = current_input.replace("\n", "↵").replace("\r", "↵").replace("\t", " ")
    before = visible_input[:offset]
    current = visible_input[offset] if offset < len(visible_input) else " "
    after = visible_input[offset + 1 :]
    available = max(2, width - 3) if width is not None else None
    if available is not None and string_display_width(before + current + after) > available:
        start = offset
        used = 0
        left_budget = max(0, available - char_display_width(current) - 2)
        while start > 0 and used + char_display_width(visible_input[start - 1]) <= left_budget:
            start -= 1
            used += char_display_width(visible_input[start])
        before = ("‹" if start else "") + visible_input[start:offset]
        remaining = available - string_display_width(before + current)
        tail: list[str] = []
        used = 0
        for char in after:
            if used + char_display_width(char) > max(0, remaining - 1):
                break
            tail.append(char)
            used += char_display_width(char)
        after = "".join(tail) + ("›" if len(tail) < len(after) and remaining else "")

    if available is not None:
        hint = truncate_plain(hint, max(0, available - 1))
    placeholder = f"{DIM}{hint}{RESET}" if not current_input else ""

    prompt_icon = f"{ACCENT}{ICON_PROMPT}{RESET}"
    return f" {prompt_icon} {before}{REVERSE}{BOLD}{current}{RESET}{after}{placeholder}"
