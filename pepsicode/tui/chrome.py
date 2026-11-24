from __future__ import annotations

import os
import re
import time
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from pepsicode.version import DISPLAY_VERSION

# Re-export the shared palette for existing TUI consumers.
from .theme import (  # noqa: F401
    ACCENT,
    BLUE,
    BOLD,
    BORDER,
    BORDER_ACTIVE,
    BORDER_DIM,
    BRAND_LIME,
    BRAND_LIME_DIM,
    BRIGHT_BLUE,
    BRIGHT_CYAN,
    BRIGHT_GREEN,
    BRIGHT_MAGENTA,
    BRIGHT_RED,
    BRIGHT_WHITE,
    BRIGHT_YELLOW,
    BUILD_BADGE_BG,
    CODE_BG,
    CODE_FG,
    CYAN,
    DIFF_ADD_BG,
    DIFF_DEL_BG,
    DIM,
    GREEN,
    HIGHLIGHT_BG,
    ITALIC,
    MAGENTA,
    PLAN_ACCENT,
    PLAN_BADGE_BG,
    PROGRESS_TRACK_BG,
    RED,
    RESET,
    REVERSE,
    SUBTLE,
    SURFACE_BG,
    TEXT,
    TEXT_FAINT,
    UNDERLINE,
    USER_BG,
    WARN_BADGE_BG,
    YELLOW,
)

# ---------------------------------------------------------------------------
# Unicode decorative characters -- Claude-style minimal set
# ---------------------------------------------------------------------------
ICON_PROMPT = ">"  # >
ICON_SUCCESS = "[OK]"
ICON_ERROR = "[X]"
ICON_DIVIDER = "-"
ICON_DOT = "·"  # middle dot
ICON_ARROW = ">"
# Compatibility aliases for old icon names (used in other modules)
ICON_pepsicode = ICON_DOT
ICON_USER = ICON_PROMPT
ICON_ASSISTANT = ICON_DOT
ICON_TOOL = ICON_DIVIDER
ICON_PROGRESS = ICON_DOT
ICON_RUNNING = ICON_DOT
ICON_FOLDER = ICON_DOT
ICON_MODEL = ICON_DOT
ICON_PROVIDER = ICON_DOT
ICON_SKILL = ICON_DOT
ICON_MSG = ICON_DOT
ICON_EVENT = ICON_DOT
ICON_MCP = ICON_DOT
ICON_BG = ICON_DOT
ICON_LOCK = ICON_DOT

# Pre-compiled regex for ANSI stripping (avoid re-compiling every call)
_ANSI_RE = re.compile(r"\[[0-9;]*m")
APP_NAME = "pepsicode"
APP_VERSION = DISPLAY_VERSION


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Cached terminal size (avoids repeated os.get_terminal_size syscalls)
# ---------------------------------------------------------------------------
_ts_cache: tuple[int, int] | None = None
_ts_cache_time: float = 0.0
_TS_TTL: float = 0.5


def _cached_terminal_size() -> tuple[int, int]:
    """Return (columns, rows) with caching."""
    global _ts_cache, _ts_cache_time
    now = time.monotonic()
    if _ts_cache is None or (now - _ts_cache_time) > _TS_TTL:
        try:
            ts = os.get_terminal_size()
            cols, rows = ts.columns, ts.lines
            if cols <= 0 or rows <= 0:
                _ts_cache = (100, 40)
            else:
                _ts_cache = (cols, rows)
        except (AttributeError, ValueError, OSError):
            _ts_cache = (100, 40)
        _ts_cache_time = now
    return _ts_cache


def invalidate_terminal_size_cache() -> None:
    """Force the next ``_cached_terminal_size`` call to re-query the OS."""
    global _ts_cache
    _ts_cache = None


# ---------------------------------------------------------------------------
# Width computation -- optimized hot path
# ---------------------------------------------------------------------------


def _build_wide_char_set() -> frozenset[int]:
    """Pre-compute the set of codepoint ranges that are double-width."""
    return frozenset()  # placeholder -- we use range checks below


def char_display_width(char: str) -> int:
    """CJK/Emoji width detection (return 2 for wide chars, 1 otherwise)."""
    if not char:
        return 0
    if unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    code = ord(char)
    if (
        0x1100 <= code <= 0x115F
        or code == 0x2329
        or code == 0x232A
        or (0x2E80 <= code <= 0xA4CF and code != 0x303F)
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE10 <= code <= 0xFE19
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x1F300 <= code <= 0x1FAF6
        or 0x20000 <= code <= 0x3FFFD
    ):
        return 2
    return 1


# Pre-compiled regex for wide character detection (CJK + Emoji)
_WIDE_CHAR_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3000-\u303f\u3040-\u309f\u30a0-\u30ff"
    r"\uff00-\uffef\u2e80-\u2eff\u1100-\u11ff"
    r"\U0001F300-\U0001FAF6\U00020000-\U0003FFFD]"
)


@lru_cache(maxsize=2048)
def _stripped_display_width(stripped: str) -> int:
    """Width of a string that is already ANSI-stripped. Cached for hot paths."""
    if stripped.isascii():
        return len(stripped)
    return sum(char_display_width(char) for char in stripped)


def string_display_width(text: str) -> int:
    """Sum of char_display_width for stripped text. Uses LRU cache on stripped content."""
    stripped = _ANSI_RE.sub("", text)
    return _stripped_display_width(stripped)


def truncate_plain(text: str, width: int) -> str:
    """Truncate with '...' suffix, CJK aware. Preserves ANSI codes."""
    width = max(0, width)
    if string_display_width(text) <= width:
        return text

    if width <= 3:
        return "." * width

    limit = max(0, width - 3)
    res = ""
    w = 0
    i = 0
    while i < len(text):
        match = _ANSI_RE.match(text, i)
        if match:
            res += match.group()
            i = match.end()
            continue

        char = text[i]
        cw = char_display_width(char)
        if w + cw > limit:
            res += "..."
            i += 1
            while i < len(text):
                m = _ANSI_RE.match(text, i)
                if m:
                    res += m.group()
                    i = m.end()
                else:
                    i += 1
            return res

        res += char
        w += cw
        i += 1
    return res


def wrap_text(text: str, width: int) -> str:
    """Wrap text to fit within ``width`` display columns.

    Unlike ``truncate_plain`` this never discards content -- lines that exceed
    the width are word-wrapped (breaking at spaces when possible) and
    character-wrapped when a single word is wider than the available space.

    * ANSI escape sequences are preserved and do not count toward width.
    * CJK / double-width characters are accounted for.
    """
    if width <= 0:
        return text

    result_lines: list[str] = []

    for raw_line in text.split("\n"):
        # Tabs must occupy explicit cells before measuring/wrapping output.
        raw_line = raw_line.expandtabs(4)
        if string_display_width(raw_line) <= width:
            result_lines.append(raw_line)
            continue

        # Tokenise the line into (is_ansi, segment) pairs so we can
        # measure display width of visible segments only.
        tokens: list[tuple[bool, str]] = []
        i = 0
        while i < len(raw_line):
            m = _ANSI_RE.match(raw_line, i)
            if m:
                tokens.append((True, m.group()))
                i = m.end()
            else:
                # Collect consecutive visible characters.
                j = i
                while j < len(raw_line) and not _ANSI_RE.match(raw_line, j):
                    j += 1
                tokens.append((False, raw_line[i:j]))
                i = j

        # Wrap tokens into output lines.
        current_line = ""
        current_width = 0
        active_sgr = ""

        for is_ansi, seg in tokens:
            if is_ansi:
                current_line += seg
                active_sgr = "" if seg in ("\x1b[0m", "\x1b[m") else active_sgr + seg
                continue

            # Split the visible segment on spaces to get word boundaries.
            words = seg.split(" ")
            first_word = True
            for word in words:
                if not word and not first_word:
                    # Preserve trailing / inter-word spaces.
                    space_w = 1
                    if current_width + space_w <= width:
                        current_line += " "
                        current_width += space_w
                    else:
                        result_lines.append(current_line + ("\x1b[0m" if active_sgr else ""))
                        current_line = active_sgr
                        current_width = 0
                    continue

                word_w = _stripped_display_width(word) if word else 0
                space_w = 0 if first_word else 1

                # Does the word (with its leading space) fit on the current line?
                if current_width + space_w + word_w <= width:
                    if not first_word:
                        current_line += " "
                        current_width += space_w
                    current_line += word
                    current_width += word_w
                    first_word = False
                    continue

                # Word doesn't fit -- start a new line.
                if current_width > 0:
                    result_lines.append(current_line + ("\x1b[0m" if active_sgr else ""))
                current_line = active_sgr
                current_width = 0
                # Paths, identifiers and CJK text may have no word boundaries.
                for char in word:
                    char_width = char_display_width(char)
                    if current_width + char_width > width and current_width:
                        result_lines.append(current_line + ("\x1b[0m" if active_sgr else ""))
                        current_line = active_sgr
                        current_width = 0
                    current_line += char
                    current_width += char_width
                first_word = False

        if current_line:
            result_lines.append(current_line + ("\x1b[0m" if active_sgr else ""))

    return "\n".join(result_lines)


def pad_plain(text: str, width: int) -> str:
    """Right-pad to width, CJK aware."""
    display_w = string_display_width(text)
    return text + (" " * max(0, width - display_w))


def paint_row(text: str, width: int, background: str) -> str:
    """Fill a row with a background color without losing the text's own styles."""
    if not background:
        return text
    # Re-arm the fill after every reset so styled spans keep their own colors.
    painted = text.replace(RESET, RESET + background) if RESET else text
    padding = " " * max(0, width - string_display_width(text))
    return f"{background}{painted}{padding}{RESET}"


def truncate_path_middle(path: str, width: int) -> str:
    """Truncate middle with '...' keeping both ends."""
    if string_display_width(path) <= width:
        return path
    if width <= 5:
        return truncate_plain(path, width)

    half = (width - 3) // 2
    start_chars = ""
    start_w = 0
    for c in path:
        cw = char_display_width(c)
        if start_w + cw > half:
            break
        start_chars += c
        start_w += cw

    end_chars = ""
    end_w = 0
    for c in reversed(path):
        cw = char_display_width(c)
        if end_w + cw > (width - 3 - start_w):
            break
        end_chars = c + end_chars
        end_w += cw

    return start_chars + "..." + end_chars


def color_badge(label: str, value: str, color: str, icon: str = "") -> str:
    """Render a styled badge: icon [label] value."""
    icon_part = f"{color}{icon} " if icon else ""
    return f"{icon_part}{color}{DIM}[{label}]{RESET} {BOLD}{value}{RESET}"


def border_line(kind: str, width: int, accent: bool = False) -> str:
    """Claude-style: plain thin line, no box-drawing corners."""
    color = ACCENT if accent else BORDER
    return f"{color}{'─' * width}{RESET}"


def panel_row(left: str, width: int, right: str | None = None, border_color: str = "") -> str:
    """Simple indented row, no box-drawing borders."""
    inner_width = max(0, width - 4)
    if right:
        right = truncate_plain(right, max(0, inner_width // 2))
        l_w = string_display_width(left)
        r_w = string_display_width(right)
        gap = inner_width - l_w - r_w
        if gap < 1:
            left = truncate_plain(left, inner_width - r_w - 1)
            gap = 1
        return f"  {left}{' ' * gap}{right}"
    else:
        return f"  {pad_plain(truncate_plain(left, inner_width), inner_width)}"


def empty_panel_row(width: int) -> str:
    """Empty indented row."""
    return panel_row("", width)


def wrap_panel_body_line(line: str, width: int) -> list[str]:
    """Wrap long lines for panel, CJK aware."""
    inner_width = width - 4
    if string_display_width(line) <= inner_width:
        return [line]

    ansi_spans: list[tuple[int, int]] = []
    for m in _ANSI_RE.finditer(line):
        ansi_spans.append((m.start(), m.end()))

    lines: list[str] = []
    current_line = ""
    current_w = 0
    i = 0
    span_idx = 0

    while i < len(line):
        if span_idx < len(ansi_spans) and i == ansi_spans[span_idx][0]:
            end = ansi_spans[span_idx][1]
            current_line += line[i:end]
            i = end
            span_idx += 1
            continue

        char = line[i]
        cw = char_display_width(char)
        if current_w + cw > inner_width:
            lines.append(current_line)
            current_line = ""
            current_w = 0
            if char == " ":
                i += 1
                continue
        current_line += char
        current_w += cw
        i += 1
    if current_line:
        lines.append(current_line)
    return lines


SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def spinner_char(frame: int) -> str:
    """Return the braille spinner glyph for ``frame``."""
    return SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]


def render_pill(label: str, fg: str, bg: str = "") -> str:
    """Render a compact colored pill badge (padded when background is enabled)."""
    if bg:
        return f"{bg}{fg}{BOLD} {label} {RESET}"
    return f"{fg}{BOLD}{label}{RESET}"


_BAR_BLOCKS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉", "█")


def render_progress_bar(percent: float, width: int = 8) -> str:
    """Render a compact horizontal meter colored by utilization level."""
    width = max(2, width)
    clamped = max(0.0, min(100.0, percent))
    total_eighths = width * 8
    units = 0 if clamped <= 0.0 else min(total_eighths, max(1, round(clamped / 100.0 * total_eighths)))
    full, rem = divmod(units, 8)
    partial = _BAR_BLOCKS[rem]
    if clamped >= 85.0:
        color = BRIGHT_RED
    elif clamped >= 65.0:
        color = YELLOW
    else:
        color = BRAND_LIME
    empty = width - full - (1 if rem else 0)
    if PROGRESS_TRACK_BG:
        return f"{PROGRESS_TRACK_BG}{color}{'█' * full}{partial}{' ' * empty}{RESET}"
    return f"{color}{'█' * full}{partial}{BORDER_DIM}{'░' * empty}{RESET}"


def render_divider(width: int, label: str = "", color: str = BORDER_DIM) -> str:
    """Render a clean horizontal divider, optionally with an inline label."""
    width = max(1, width)
    if not label or width < 8:
        return f"{color}{'─' * width}{RESET}"
    tag = truncate_plain(f" {label} ", max(1, width - 4))
    remaining = max(0, width - string_display_width(tag))
    left = min(2, remaining)
    right = max(0, remaining - left)
    return f"{color}{'─' * left}{RESET}{SUBTLE}{tag}{RESET}{color}{'─' * right}{RESET}"


def _box_edge(
    left_ch: str,
    right_ch: str,
    inner_w: int,
    left_label: str = "",
    right_label: str = "",
    border_color: str = BORDER,
) -> str:
    """Build a rounded top or bottom box border with optional embedded labels."""
    if not left_label and not right_label:
        return f"{border_color}{left_ch}{'─' * inner_w}{right_ch}{RESET}"

    if left_label and right_label and inner_w >= 11:
        max_right = min(string_display_width(f" {right_label} "), max(4, (inner_w - 3) // 2))
        right_txt = truncate_plain(f" {right_label} ", max_right)
        right_w = string_display_width(right_txt)
        max_left = inner_w - 3 - right_w
        if max_left >= 4:
            left_txt = truncate_plain(f" {left_label} ", max_left)
            left_w = string_display_width(left_txt)
            mid_dashes = max(1, inner_w - 2 - left_w - right_w)
            return (
                f"{border_color}{left_ch}─{RESET}{left_txt}{RESET}"
                f"{border_color}{'─' * mid_dashes}{RESET}{right_txt}{RESET}"
                f"{border_color}─{right_ch}{RESET}"
            )

    if left_label:
        max_left = inner_w - 2
        if max_left >= 4:
            left_txt = truncate_plain(f" {left_label} ", max_left)
            left_w = string_display_width(left_txt)
            right_dashes = max(1, inner_w - 1 - left_w)
            return (
                f"{border_color}{left_ch}─{RESET}{left_txt}{RESET}{border_color}{'─' * right_dashes}{right_ch}{RESET}"
            )

    if right_label:
        max_right = inner_w - 2
        if max_right >= 4:
            right_txt = truncate_plain(f" {right_label} ", max_right)
            right_w = string_display_width(right_txt)
            left_dashes = max(1, inner_w - 1 - right_w)
            return (
                f"{border_color}{left_ch}{'─' * left_dashes}{RESET}{right_txt}{RESET}{border_color}─{right_ch}{RESET}"
            )

    return f"{border_color}{left_ch}{'─' * inner_w}{right_ch}{RESET}"


def render_box(
    lines: list[str],
    width: int,
    *,
    title: str = "",
    right_title: str = "",
    footer: str = "",
    border_color: str = BORDER,
    fill_bg: str = "",
    pad_x: int = 1,
) -> list[str]:
    """Render lines inside a CJK-safe rounded card box, degrading cleanly on tiny widths."""
    width = max(0, width)
    if width == 0:
        return []
    if width < 10:
        out: list[str] = []
        if title:
            out.append(truncate_plain(title, width))
        for line in lines:
            row = (" " * pad_x + line) if width >= pad_x + 4 else line
            out.append(truncate_plain(row, width))
        if footer:
            out.append(truncate_plain(footer, width))
        return out

    inner_w = width - 2
    pad_x = max(0, pad_x)
    if inner_w < 2 * pad_x + 1:
        pad_x = 0
    content_w = max(1, inner_w - 2 * pad_x)

    res = [_box_edge("╭", "╮", inner_w, title, right_title, border_color)]
    for line in lines:
        clipped = truncate_plain(line, content_w)
        cell = (" " * pad_x) + pad_plain(clipped, content_w) + (" " * pad_x)
        inner = paint_row(cell, inner_w, fill_bg) if fill_bg else f"{cell}{RESET}"
        res.append(f"{border_color}│{RESET}{inner}{border_color}│{RESET}")
    res.append(_box_edge("╰", "╯", inner_w, footer, "", border_color))
    return res


def center_box_frame(box_lines: list[str], cols: int, rows: int) -> list[str]:
    """Center a modal box horizontally and gently vertically within ``(cols, rows)``."""
    usable_cols = max(1, cols - 1)
    rows = max(1, rows)
    box_w = max((string_display_width(line) for line in box_lines), default=0)
    left_pad = max(0, (usable_cols - box_w) // 2)
    padded_box = [(" " * left_pad + line) if left_pad else line for line in box_lines]
    if len(padded_box) >= rows:
        return [truncate_plain(line, usable_cols) for line in padded_box[:rows]]
    top_pad = min(3, (rows - len(padded_box)) // 3) if rows >= len(padded_box) + 4 else 0
    frame = [""] * top_pad + padded_box
    frame += [""] * max(0, rows - len(frame))
    return [truncate_plain(line, usable_cols) for line in frame[:rows]]


# Panel-title icon mapping (minimal)
_PANEL_ICONS: dict[str, str] = {
    "pepsicode": "◆",
    "session feed": "◆",
    "prompt": ICON_PROMPT,
    "activity": ICON_DIVIDER,
    "action required": "◆",
}


def render_panel(title: str, body: str, right_title: str | None = None, min_body_lines: int = 0) -> str:
    """Render a rounded card panel with title, wrapped body, and optional right badge."""
    width, _ = _cached_terminal_size()
    width = max(8, width - 1)

    icon = _PANEL_ICONS.get(title.lower(), "")
    icon_str = f"{ACCENT}{icon} {RESET}" if icon else ""

    title_display = f"{icon_str}{ACCENT}{BOLD}{title}{RESET}"
    right_display = f"{SUBTLE}{right_title}{RESET}" if right_title else ""

    body_lines = body.splitlines() if body else []
    wrapped_lines: list[str] = []
    for bl in body_lines:
        wrapped_lines.extend(wrap_panel_body_line(bl, width))

    while len(wrapped_lines) < min_body_lines:
        wrapped_lines.append("")

    boxed = render_box(
        wrapped_lines,
        width,
        title=title_display,
        right_title=right_display,
        border_color=BORDER,
    )
    return "\n".join(boxed)


def render_banner(runtime: dict | None, cwd: str, permission_summary: list[str], session: dict[str, int]) -> str:
    """Dual-tone PepsiCode banner with model, workspace, and session stats."""
    model = runtime.get("model", "not-configured") if runtime else "not-configured"
    provider = "offline"
    if runtime and runtime.get("baseUrl"):
        provider = runtime["baseUrl"].replace("https://", "").replace("http://", "").split("/")[0]

    cwd_path = Path(cwd)
    folder_name = cwd_path.name or str(cwd_path)

    msg_count = session.get("messageCount", 0)
    evt_count = session.get("transcriptCount", 0)

    lines = [
        f"{BRIGHT_WHITE}{BOLD}pepsi{BRAND_LIME}code{RESET}  {SUBTLE}{APP_VERSION}{RESET}",
        f"{CYAN}{folder_name}{RESET}  {TEXT}{model}{RESET}  {SUBTLE}{provider}{RESET}",
        f"{SUBTLE}{msg_count} msgs  ·  {evt_count} events{RESET}",
    ]
    return "\n".join(lines)


def render_status_line(status: str | None) -> str:
    """Brand-accented status indicator."""
    if status:
        return f"{ACCENT}{ICON_DOT}{RESET} {YELLOW}{BOLD}{status}{RESET}"
    return f"{ACCENT}{ICON_DOT}{RESET} {DIM}Ready{RESET}"


def render_tool_panel(
    active_tool: str | None, recent_tools: list[dict[str, str]], background_tasks: list[dict[str, Any]] | None = None
) -> str:
    """Minimal tool status panel."""
    if background_tasks is None:
        background_tasks = []
    parts: list[str] = []
    if active_tool:
        parts.append(f"{ACCENT}{ICON_DOT}{RESET} {YELLOW}{active_tool}{RESET}")
    for task in background_tasks:
        if task.get("status") == "running":
            parts.append(f"{SUBTLE}bg:{task.get('label', 'task')}{RESET}")
    if not parts and not recent_tools:
        parts.append(f"{SUBTLE}idle{RESET}")
    else:
        for tool in recent_tools[-3:]:
            if tool.get("status") == "success":
                parts.append(f"{GREEN}{ICON_SUCCESS} {tool.get('name', 'tool')}{RESET}")
            else:
                parts.append(f"{RED}{ICON_ERROR} {tool.get('name', 'tool')}{RESET}")
    return f"{SUBTLE}tools{RESET}  " + "  ".join(parts)


def render_footer_bar(
    status: str | None,
    tools_enabled: bool,
    skills_enabled: bool,
    background_tasks: list[dict[str, Any]] | None = None,
    mode: str = "auto",
    plan: bool = False,
) -> str:
    """Minimal single-line footer with aligned PLAN/BUILD badge."""
    if background_tasks is None:
        background_tasks = []
    width, _ = _cached_terminal_size()
    risk = " 高风险" if mode in {"full_access", "trust_commands"} else ""
    color = YELLOW if mode in {"trust_commands", "full_access"} else SUBTLE
    left = f"{BRIGHT_CYAN if plan else ACCENT}{BOLD}{'PLAN ' if plan else 'BUILD'}{RESET} {color}mode: {mode}{risk}{RESET}  {render_status_line(status)}"

    parts = []
    if tools_enabled:
        parts.append(f"{SUBTLE}tools{RESET}")
    if skills_enabled:
        parts.append(f"{SUBTLE}skills{RESET}")
    right = f"{SUBTLE}{' · '.join(parts)}{RESET}" if parts else ""

    gap = max(1, width - string_display_width(left) - string_display_width(right))
    return f"{left}{' ' * gap}{right}"


def render_slash_menu(
    commands: list[Any], selected_index: int, max_rows: int | None = None, width: int | None = None
) -> str:
    """Render slash commands inside a clean aligned card menu."""
    if not commands:
        return f"{SUBTLE}no commands{RESET}"
    if width is None:
        width, _ = _cached_terminal_size()
    width = max(8, width)

    if max_rows is not None:
        max_rows = max(1, max_rows)
        selected_index = max(0, min(selected_index, len(commands) - 1))
        if max_rows >= 4 and width >= 24:
            limit = max(1, max_rows - 2)
            start = max(0, min(selected_index - limit // 2, len(commands) - limit))
            visible = commands[start : min(len(commands), start + limit)]
            content_w = max(4, width - 4)
            usage_col = min(20, max((string_display_width(getattr(c, "usage", str(c))) for c in visible), default=8))
            usage_col = min(usage_col, max(6, content_w // 2))
            body_rows: list[str] = []
            for offset, cmd in enumerate(visible):
                index = start + offset
                usage = getattr(cmd, "usage", str(cmd))
                desc = getattr(cmd, "description", "")
                marker = ICON_ARROW if index == selected_index else " "
                usage_padded = pad_plain(truncate_plain(usage, usage_col), usage_col)
                desc_budget = max(0, content_w - usage_col - 4)
                desc_str = truncate_plain(desc, desc_budget) if desc_budget >= 4 else ""
                if index == selected_index:
                    row_txt = f"{ACCENT}{BOLD}{marker}{RESET} {BRIGHT_WHITE}{BOLD}{usage_padded}{RESET}"
                    if desc_str:
                        row_txt += f"  {TEXT}{desc_str}{RESET}"
                    body_rows.append(paint_row(truncate_plain(row_txt, content_w), content_w, HIGHLIGHT_BG))
                else:
                    row_txt = f"  {TEXT}{usage_padded}{RESET}" + (f"  {SUBTLE}{desc_str}{RESET}" if desc_str else "")
                    body_rows.append(row_txt)
            boxed = render_box(
                body_rows,
                width,
                title=f"{ACCENT}{BOLD}/ commands{RESET}",
                right_title=f"{SUBTLE}{selected_index + 1}/{len(commands)}{RESET}",
                footer=f"{SUBTLE}↑/↓ 选择 · Tab/Enter 确认 · Esc 关闭{RESET}",
                border_color=BORDER,
            )
            return "\n".join(truncate_plain(row, width) for row in boxed[:max_rows])

        limit = max(1, max_rows - 1) if max_rows >= 2 else 1
        start = max(0, min(selected_index - limit // 2, len(commands) - limit))
        rows: list[str] = []
        for index in range(start, min(len(commands), start + limit)):
            cmd = commands[index]
            usage = getattr(cmd, "usage", str(cmd))
            desc = getattr(cmd, "description", "")
            marker = ICON_ARROW if index == selected_index else " "
            label = f" {marker} {usage}  {desc}"
            label = truncate_plain(label, width)
            if index == selected_index:
                rows.append(paint_row(f"{ACCENT}{BOLD}{label}{RESET}", width, HIGHLIGHT_BG))
            else:
                rows.append(f"{SUBTLE}{label}{RESET}")
        if max_rows >= 2:
            rows.append(f" {SUBTLE}{selected_index + 1}/{len(commands)} · ↑/↓{RESET}")
        return "\n".join(truncate_plain(row, width) for row in rows[:max_rows])

    cat_colors: dict[str, str] = {
        "Session": CYAN,
        "Tools": BRAND_LIME,
        "Status": YELLOW,
        "Files": MAGENTA,
        "General": SUBTLE,
    }

    from itertools import groupby

    grouped: list[tuple[str, list[Any]]] = []
    for cat, cmds in groupby(commands, key=lambda c: getattr(c, "category", "General")):
        grouped.append((cat, list(cmds)))

    content_w = max(8, width - 4) if width >= 24 else max(4, width)
    usage_col = min(22, max((string_display_width(getattr(c, "usage", str(c))) for c in commands), default=8))
    usage_col = min(usage_col, max(6, content_w // 2))

    rows = []
    flat_index = 0
    for cat_idx, (category, cmds) in enumerate(grouped):
        if cat_idx > 0:
            rows.append("")
        color = cat_colors.get(category, SUBTLE)
        rows.append(f"{color}{BOLD}{category}{RESET}")

        for cmd in cmds:
            usage = getattr(cmd, "usage", str(cmd))
            desc = getattr(cmd, "description", "")
            shortcut = str(flat_index + 1) if flat_index < 9 else " "
            usage_padded = pad_plain(truncate_plain(usage, usage_col), usage_col)
            desc_budget = max(0, content_w - usage_col - 6)
            desc_str = truncate_plain(desc, desc_budget) if desc_budget >= 4 else ""

            if flat_index == selected_index:
                line = f"{ACCENT}{BOLD}{ICON_ARROW}{RESET} {SUBTLE}{shortcut}{RESET} {BRIGHT_WHITE}{BOLD}{usage_padded}{RESET}"
                if desc_str:
                    line += f"  {TEXT}{desc_str}{RESET}"
                rows.append(paint_row(truncate_plain(line, content_w), content_w, HIGHLIGHT_BG))
            else:
                line = f"  {SUBTLE}{shortcut}{RESET} {TEXT}{usage_padded}{RESET}"
                if desc_str:
                    line += f"  {SUBTLE}{desc_str}{RESET}"
                rows.append(line)
            flat_index += 1

    if width >= 24:
        return "\n".join(
            render_box(
                rows,
                width,
                title=f"{ACCENT}{BOLD}/ commands{RESET}",
                right_title=f"{SUBTLE}Tab/Enter select{RESET}",
                footer=f"{SUBTLE}↑/↓ or 1-9 select · Esc close{RESET}",
                border_color=BORDER,
            )
        )
    return "\n".join(truncate_plain(row, width) for row in rows)


def classify_diff_line(line: str) -> str:
    """Returns 'meta'|'add'|'remove'|'context'."""
    if line.startswith(("+++", "---", "@@")):
        return "meta"
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "remove"
    return "context"


def compute_changed_range(removed: str, added: str) -> tuple[int, int] | None:
    """Word-level emphasis ranges."""
    if not removed or not added:
        return None
    p = 0
    while p < len(removed) and p < len(added) and removed[p] == added[p]:
        p += 1
    s = 0
    while s < (len(removed) - p) and s < (len(added) - p) and removed[-(s + 1)] == added[-(s + 1)]:
        s += 1
    return (p, len(added) - s) if p < (len(added) - s) else None


def apply_word_emphasis(content: str, color: str, emphasis_range: tuple[int, int] | None = None, bg: str = "") -> str:
    """Apply color and word-level emphasis."""
    if not emphasis_range:
        return f"{bg}{color}{content}{RESET}"
    s, e = emphasis_range
    return f"{bg}{color}{content[:s]}{BOLD}{REVERSE}{content[s:e]}{RESET}{bg}{color}{content[e:]}{RESET}"


def colorize_unified_diff_block(block: str) -> str:
    """Full diff with subtle +/- background tints, word-level highlighting, and look-ahead pairing."""
    lines = block.splitlines()
    res: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(("--- ", "+++ ", "@@ ")):
            res.append(f"{CYAN}{line}{RESET}")
            i += 1
            continue
        if line.startswith("-"):
            removals: list[str] = []
            while i < len(lines) and lines[i].startswith("-"):
                removals.append(lines[i][1:])
                i += 1
            additions: list[str] = []
            while i < len(lines) and lines[i].startswith("+"):
                additions.append(lines[i][1:])
                i += 1
            paired = min(len(removals), len(additions))
            for j in range(paired):
                emphasis = compute_changed_range(removals[j], additions[j])
                res.append(f"{DIFF_DEL_BG}{RED}-{RESET}" + apply_word_emphasis(removals[j], RED, emphasis, DIFF_DEL_BG))
                res.append(
                    f"{DIFF_ADD_BG}{GREEN}+{RESET}" + apply_word_emphasis(additions[j], GREEN, emphasis, DIFF_ADD_BG)
                )
            for j in range(paired, len(removals)):
                res.append(f"{DIFF_DEL_BG}{RED}-{removals[j]}{RESET}")
            for j in range(paired, len(additions)):
                res.append(f"{DIFF_ADD_BG}{GREEN}+{additions[j]}{RESET}")
            continue
        if line.startswith("+"):
            res.append(f"{DIFF_ADD_BG}{GREEN}{line}{RESET}")
            i += 1
        else:
            res.append(f"{DIM}{line}{RESET}")
            i += 1
    return "\n".join(res)


def _looks_like_diff_block(detail: str) -> bool:
    """Check if a detail string looks like a unified diff block."""
    return "\n" in detail and ("--- a/" in detail or "+++ b/" in detail or "@@ " in detail)


def colorize_edit_permission_details(details: list[str]) -> list[str]:
    """Colorize diff blocks in permission details."""
    return [colorize_unified_diff_block(d) if _looks_like_diff_block(d) else d for d in details]


def get_permission_prompt_max_scroll_offset(request: dict[str, Any], expanded: bool = False) -> int:
    """Calculate max scroll offset for permission details."""
    if not expanded and request.get("kind") != "command":
        return 0
    flat = _permission_detail_lines(request)
    _, rows = _cached_terminal_size()
    max_visible = _permission_max_visible(request, rows)
    return max(0, len(flat) - max_visible)


def _permission_max_visible(request: dict[str, Any], rows: int) -> int:
    return max(2, rows - len(request.get("choices", [])) - 10)


def _permission_detail_lines(request: dict[str, Any]) -> list[str]:
    details = list(request.get("details", []))
    if request.get("kind") == "command" and request.get("scope"):
        details.append(f"scope: {request['scope']}")
    flat = flatten_detail_lines(details)
    if request.get("kind") != "command":
        return flat
    width, _ = _cached_terminal_size()
    box_width = max(8, width - 1)
    return [wrapped for line in flat for wrapped in wrap_panel_body_line(line, box_width)]


def flatten_detail_lines(details: list[str]) -> list[str]:
    """Flatten a list of detail strings (which may contain newlines) into individual lines."""
    result: list[str] = []
    for detail in details:
        result.extend(detail.split("\n"))
    return result


def slice_visible_details(
    flat_lines: list[str], scroll_offset: int, max_visible: int | None = None
) -> tuple[list[str], int]:
    """Return the visible slice of detail lines and total count."""
    if max_visible is None:
        _, rows = _cached_terminal_size()
        max_visible = max(4, rows - 20)
    total = len(flat_lines)
    offset = max(0, min(scroll_offset, max(0, total - max_visible)))
    return flat_lines[offset : offset + max_visible], total


def render_permission_prompt(
    request: dict[str, Any],
    expanded: bool = False,
    scroll_offset: int = 0,
    selected_choice_index: int = 0,
    feedback_mode: bool = False,
    feedback_input: str = "",
) -> str:
    """Render permission prompt inside a crisp rounded card with keycap choices."""
    cols, rows = _cached_terminal_size()
    width = max(8, cols - 1)
    content_w = max(4, width - 4) if width >= 10 else width
    is_command = request.get("kind") == "command"
    border_color = YELLOW if is_command else BORDER_ACTIVE
    title_color = YELLOW if is_command else ACCENT

    lines: list[str] = []
    if feedback_mode:
        lines.extend(
            [
                f"{BRIGHT_YELLOW}{BOLD}{ICON_PROMPT} Provide reason for rejection:{RESET}",
                f"  {BRAND_LIME}{ICON_PROMPT}{RESET} {feedback_input}_",
                "",
                f"{SUBTLE}  Enter send · Esc cancel{RESET}",
            ]
        )
        footer_hint = f"{SUBTLE}Enter 发送 · Esc 取消{RESET}"
    else:
        summary = request.get("summary", "Permission Request")
        lines.extend([f"{BRIGHT_WHITE}{BOLD}{summary}{RESET}", render_divider(content_w)])
        details = request.get("details", [])
        if details:
            flat = _permission_detail_lines(request)
            if not expanded and not is_command:
                lines.append(
                    f"{SUBTLE}  {ICON_ARROW} {len(flat)} lines hidden · {RESET}{DIM}press 'v' or Ctrl+O to expand{RESET}"
                )
            else:
                colorized = colorize_edit_permission_details(flat)
                visible, total = slice_visible_details(colorized, scroll_offset, _permission_max_visible(request, rows))
                lines.extend(visible)
                if total > len(visible):
                    lines.append(f"{SUBTLE}  ─── scroll {scroll_offset + 1}/{total} (Wheel / PgUp / PgDn) ───{RESET}")
            lines.append("")
        for i, choice in enumerate(request.get("choices", [])):
            label = choice.get("label", "")
            key = choice.get("key", "")
            if i == selected_choice_index:
                row_txt = f"{ACCENT}{BOLD}{ICON_ARROW}{RESET} {ACCENT}[{key}]{RESET} {BRIGHT_WHITE}{BOLD}{label}{RESET}"
                lines.append(paint_row(truncate_plain(row_txt, content_w), content_w, HIGHLIGHT_BG))
            else:
                lines.append(f"  {SUBTLE}[{key}]{RESET} {TEXT}{label}{RESET}")
        footer_hint = f"{SUBTLE}↑/↓ 选择 · Enter 确认 · v 详情{RESET}"

    wrapped_lines: list[str] = []
    for line in lines:
        if HIGHLIGHT_BG and HIGHLIGHT_BG in line:
            wrapped_lines.append(line)
        else:
            wrapped_lines.extend(wrap_panel_body_line(line, max(8, width)))

    right_tag = f"{YELLOW}Command{RESET}" if is_command else f"{SUBTLE}Permission{RESET}"
    boxed = render_box(
        wrapped_lines,
        width,
        title=f"{title_color}◆ {BOLD}Action Required{RESET}",
        right_title=right_tag,
        footer=footer_hint,
        border_color=border_color,
    )
    return "\n".join(boxed)
