from __future__ import annotations

import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

# ANSI color constants
RESET = "[0m"
DIM = "[2m"
CYAN = "[36m"
GREEN = "[32m"
YELLOW = "[33m"
RED = "[31m"
BLUE = "[34m"
MAGENTA = "[35m"
BOLD = "[1m"
REVERSE = "[7m"
ITALIC = "[3m"
UNDERLINE = "[4m"
BRIGHT_GREEN = "[92m"
BRIGHT_RED = "[91m"
BRIGHT_CYAN = "[96m"
BRIGHT_YELLOW = "[93m"
BRIGHT_BLUE = "[94m"
BRIGHT_MAGENTA = "[95m"
BRIGHT_WHITE = "[97m"
# Extended 256-color palette -- Claude-style amber-only accent
BORDER = "[38;5;214m"      # amber (unified with ACCENT)
BORDER_DIM = "[38;5;237m"  # dark gray for secondary borders
ACCENT = "[38;5;214m"      # warm amber accent
SUBTLE = "[38;5;243m"      # gray for subtle text
HIGHLIGHT_BG = "[48;5;238m"  # dark bg highlight for selections

# ---------------------------------------------------------------------------
# Unicode decorative characters -- Claude-style minimal set
# ---------------------------------------------------------------------------
ICON_PROMPT = ">"     # >
ICON_SUCCESS = "[OK]"
ICON_ERROR = "[X]"
ICON_DIVIDER = "-"
ICON_DOT = "·"        # middle dot
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
APP_VERSION = "v0.1"


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
    r'[\u4e00-\u9fff\u3000-\u303f\u3040-\u309f\u30a0-\u30ff'
    r'\uff00-\uffef\u2e80-\u2eff\u1100-\u11ff'
    r'\U0001F300-\U0001FAF6\U00020000-\U0003FFFD]'
)


@lru_cache(maxsize=2048)
def _stripped_display_width(stripped: str) -> int:
    """Width of a string that is already ANSI-stripped. Cached for hot paths."""
    wide_chars = len(_WIDE_CHAR_PATTERN.findall(stripped))
    return len(stripped) + wide_chars


def string_display_width(text: str) -> int:
    """Sum of char_display_width for stripped text. Uses LRU cache on stripped content."""
    stripped = _ANSI_RE.sub("", text)
    return _stripped_display_width(stripped)


def truncate_plain(text: str, width: int) -> str:
    """Truncate with '...' suffix, CJK aware. Preserves ANSI codes."""
    if string_display_width(text) <= width:
        return text

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

        for is_ansi, seg in tokens:
            if is_ansi:
                current_line += seg
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
                        result_lines.append(current_line)
                        current_line = ""
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
                    result_lines.append(current_line)
                current_line = word if word else ""
                current_width = word_w
                first_word = False

        if current_line:
            result_lines.append(current_line)

    return "\n".join(result_lines)


def pad_plain(text: str, width: int) -> str:
    """Right-pad to width, CJK aware."""
    display_w = string_display_width(text)
    return text + (" " * max(0, width - display_w))


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
    inner_width = width - 4
    if right:
        l_w = string_display_width(left)
        r_w = string_display_width(right)
        gap = inner_width - l_w - r_w
        if gap < 1:
            left = truncate_plain(left, inner_width - r_w - 1)
            gap = 1
        return f"  {left}{' ' * gap}{right}"
    else:
        return f"  {pad_plain(left, inner_width)}"


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


# Panel-title icon mapping (minimal)
_PANEL_ICONS: dict[str, str] = {
    "pepsicode": ICON_DOT,
    "session feed": ICON_DOT,
    "prompt": ICON_PROMPT,
    "activity": ICON_DIVIDER,
    "action required": ICON_DOT,
}


def render_panel(title: str, body: str, right_title: str | None = None, min_body_lines: int = 0) -> str:
    """Claude-style: simple title + thin line + body, no box-drawing."""
    width, _ = _cached_terminal_size()
    if width < 40:
        width = 40

    icon = _PANEL_ICONS.get(title.lower(), "")
    icon_str = f"{ACCENT}{icon} {RESET}" if icon else ""

    title_display = f"{icon_str}{ACCENT}{BOLD}{title}{RESET}"
    right_display = f"{SUBTLE}{right_title}{RESET}" if right_title else None

    # Top: title with thin line
    title_row = panel_row(title_display, width, right_display)
    separator = f"{SUBTLE}{'─' * width}{RESET}"

    res = [title_row, separator]

    body_lines = body.splitlines() if body else []
    wrapped_lines: list[str] = []
    for bl in body_lines:
        wrapped_lines.extend(wrap_panel_body_line(bl, width))

    while len(wrapped_lines) < min_body_lines:
        wrapped_lines.append("")

    for wl in wrapped_lines:
        res.append(panel_row(wl, width))
    res.append(f"{SUBTLE}{'─' * width}{RESET}")
    return "\n".join(res)


def render_banner(runtime: dict | None, cwd: str, permission_summary: list[str], session: dict[str, int]) -> str:
    """Claude-style: minimal banner, plain text lines."""
    model = runtime.get("model", "not-configured") if runtime else "not-configured"
    provider = "offline"
    if runtime and runtime.get("baseUrl"):
        provider = runtime["baseUrl"].replace("https://", "").replace("http://", "").split("/")[0]

    cwd_path = Path(cwd)
    folder_name = cwd_path.name or str(cwd_path)

    width, _ = _cached_terminal_size()

    msg_count = session.get("messageCount", 0)
    evt_count = session.get("transcriptCount", 0)

    lines = [
        f"{ACCENT}{BOLD}{APP_NAME}{RESET} {SUBTLE}{APP_VERSION}{RESET}",
        f"{SUBTLE}{folder_name}{RESET}  {DIM}{model}{RESET}  {SUBTLE}{provider}{RESET}",
        f"{SUBTLE}{msg_count} msgs{RESET}  {SUBTLE}{evt_count} events{RESET}",
    ]
    return "\n".join(lines)


def render_status_line(status: str | None) -> str:
    """Claude-style: amber dot status indicator."""
    if status:
        return f"{ACCENT}{ICON_DOT}{RESET} {YELLOW}{BOLD}{status}{RESET}"
    return f"{ACCENT}{ICON_DOT}{RESET} {DIM}Ready{RESET}"


def render_tool_panel(
    active_tool: str | None, recent_tools: list[dict[str, str]], background_tasks: list[dict[str, Any]] | None = None
) -> str:
    """Claude-style: minimal tool status panel."""
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
    status: str | None, tools_enabled: bool, skills_enabled: bool, background_tasks: list[dict[str, Any]] | None = None
) -> str:
    """Claude-style: minimal single-line footer."""
    if background_tasks is None:
        background_tasks = []
    width, _ = _cached_terminal_size()
    left = render_status_line(status)

    parts = []
    if tools_enabled:
        parts.append(f"{SUBTLE}tools{RESET}")
    if skills_enabled:
        parts.append(f"{SUBTLE}skills{RESET}")
    right = f"{SUBTLE}{' · '.join(parts)}{RESET}" if parts else ""

    gap = max(1, width - string_display_width(left) - string_display_width(right))
    return f"{left}{' ' * gap}{right}"


def render_slash_menu(commands: list[Any], selected_index: int) -> str:
    """Claude-style: amber-accented command menu with categories."""
    if not commands:
        return f"{SUBTLE}no commands{RESET}"
    width, _ = _cached_terminal_size()
    inner_width = max(20, width - 4)

    # Category colors
    cat_colors: dict[str, str] = {
        "Session": CYAN,
        "Tools": GREEN,
        "Status": YELLOW,
        "Files": MAGENTA,
        "General": SUBTLE,
    }

    # Group by category preserving order
    from itertools import groupby
    grouped: list[tuple[str, list[Any]]] = []
    for cat, cmds in groupby(commands, key=lambda c: getattr(c, "category", "General")):
        grouped.append((cat, list(cmds)))

    rows: list[str] = []
    # Header
    rows.append(f"  {ACCENT}{ICON_ARROW}{RESET} {BOLD}commands{RESET}  {SUBTLE}Tab/Enter to select, Esc to close{RESET}")
    rows.append(f"  {SUBTLE}{ICON_DIVIDER * min(50, inner_width)}{RESET}")

    flat_index = 0
    for cat_idx, (category, cmds) in enumerate(grouped):
        if cat_idx > 0:
            rows.append("")  # gap between categories

        # Category header
        color = cat_colors.get(category, SUBTLE)
        rows.append(f"  {color}{BOLD}{category}{RESET}")

        for cmd in cmds:
            usage = getattr(cmd, "usage", str(cmd))
            desc = getattr(cmd, "description", "")
            shortcut = str(flat_index + 1) if flat_index < 9 else " "

            if flat_index == selected_index:
                line = (
                    f"  {HIGHLIGHT_BG}{ACCENT}{ICON_ARROW}{RESET}"
                    f"{HIGHLIGHT_BG} {shortcut} {BRIGHT_WHITE}{BOLD}{usage}{RESET}"
                    f"{HIGHLIGHT_BG}  {SUBTLE}{desc}{RESET}"
                )
            else:
                line = (
                    f"   {SUBTLE}{shortcut}{RESET}"
                    f"  {usage}"
                    f"  {SUBTLE}{desc}{RESET}"
                )
            rows.append(truncate_plain(line, width))
            flat_index += 1

    # Footer
    rows.append(f"  {SUBTLE}{ICON_DIVIDER * min(50, inner_width)}{RESET}")
    rows.append(f"  {SUBTLE}Esc to close{RESET}  {SUBTLE}{ICON_DOT}{RESET}  {SUBTLE}Arrows or 1-9 to select{RESET}")

    return "\n".join(rows)


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


def apply_word_emphasis(content: str, color: str, emphasis_range: tuple[int, int] | None = None) -> str:
    """Apply color and word-level emphasis."""
    if not emphasis_range:
        return f"{color}{content}{RESET}"
    s, e = emphasis_range
    return f"{color}{content[:s]}{BOLD}{REVERSE}{content[s:e]}{RESET}{color}{content[e:]}{RESET}"


def colorize_unified_diff_block(block: str) -> str:
    """Full diff with word-level highlighting and look-ahead pairing."""
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
                res.append("-" + apply_word_emphasis(removals[j], RED, emphasis))
                res.append("+" + apply_word_emphasis(additions[j], GREEN, emphasis))
            for j in range(paired, len(removals)):
                res.append(f"{RED}-{removals[j]}{RESET}")
            for j in range(paired, len(additions)):
                res.append(f"{GREEN}+{additions[j]}{RESET}")
            continue
        if line.startswith("+"):
            res.append(f"{GREEN}{line}{RESET}")
            i += 1
        else:
            res.append(f"{DIM}{line}{RESET}")
            i += 1
    return "\n".join(res)


def _looks_like_diff_block(detail: str) -> bool:
    """Check if a detail string looks like a unified diff block."""
    return (
        "\n" in detail
        and ("--- a/" in detail or "+++ b/" in detail or "@@ " in detail)
    )


def colorize_edit_permission_details(details: list[str]) -> list[str]:
    """Colorize diff blocks in permission details."""
    return [
        colorize_unified_diff_block(d) if _looks_like_diff_block(d) else d
        for d in details
    ]


def get_permission_prompt_max_scroll_offset(request: dict[str, Any], expanded: bool = False) -> int:
    """Calculate max scroll offset for permission details."""
    if not expanded:
        return 0
    flat = flatten_detail_lines(request.get("details", []))
    _, rows = _cached_terminal_size()
    max_visible = max(4, rows - 20)
    return max(0, len(flat) - max_visible)


def flatten_detail_lines(details: list[str]) -> list[str]:
    """Flatten a list of detail strings (which may contain newlines) into individual lines."""
    result: list[str] = []
    for detail in details:
        result.extend(detail.split("\n"))
    return result


def slice_visible_details(flat_lines: list[str], scroll_offset: int, max_visible: int | None = None) -> tuple[list[str], int]:
    """Return the visible slice of detail lines and total count."""
    if max_visible is None:
        _, rows = _cached_terminal_size()
        max_visible = max(4, rows - 20)
    total = len(flat_lines)
    offset = max(0, min(scroll_offset, max(0, total - max_visible)))
    return flat_lines[offset:offset + max_visible], total


def render_permission_prompt(
    request: dict[str, Any],
    expanded: bool = False,
    scroll_offset: int = 0,
    selected_choice_index: int = 0,
    feedback_mode: bool = False,
    feedback_input: str = "",
) -> str:
    """Claude-style: permission prompt with amber accents."""
    lines: list[str] = []
    if feedback_mode:
        lines.extend(
            [
                f"{BRIGHT_YELLOW}{ICON_PROMPT} Provide reason for rejection:{RESET}",
                f"  {GREEN}{ICON_PROMPT}{RESET} {feedback_input}_",
                "",
                f"{SUBTLE}  Press Enter to send, Esc to cancel.{RESET}",
            ]
        )
    else:
        lines.extend([request.get("summary", "Permission Request"), ""])
        details = request.get("details", [])
        if details:
            flat = flatten_detail_lines(details)
            if not expanded:
                lines.append(f"{SUBTLE}  {ICON_ARROW} {len(flat)} lines hidden {SUBTLE}|{RESET} {DIM}press 'v' to expand | Ctrl+O toggle{RESET}")
            else:
                colorized = colorize_edit_permission_details(flat)
                visible, total = slice_visible_details(colorized, scroll_offset)
                lines.extend(visible)
                if total > len(visible):
                    lines.append(f"{SUBTLE}  {ICON_DIVIDER * 3} scroll {scroll_offset+1}/{total} (Wheel/PgUp/PgDn) {ICON_DIVIDER * 3}{RESET}")
            lines.append("")
        for i, choice in enumerate(request.get("choices", [])):
            label = choice.get("label", "")
            key = choice.get("key", "")
            if i == selected_choice_index:
                lines.append(f"  {HIGHLIGHT_BG}{ACCENT}{ICON_ARROW}{RESET}{HIGHLIGHT_BG} {BRIGHT_WHITE}{BOLD}{label}{RESET}{HIGHLIGHT_BG} {SUBTLE}({key}){RESET}")
            else:
                lines.append(f"    {SUBTLE}{ICON_DOT}{RESET} {label} {SUBTLE}({key}){RESET}")
    return render_panel("Action Required", "\n".join(lines), right_title="Permission")
