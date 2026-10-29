from __future__ import annotations

from pepsicode.llm.assistant_text import parse_assistant_text

from .chrome import (
    ACCENT,
    BOLD,
    BORDER,
    BRIGHT_RED,
    CYAN,
    DIM,
    GREEN,
    ICON_DIVIDER,
    ICON_DOT,
    RED,
    RESET,
    SUBTLE,
    YELLOW,
    _cached_terminal_size,
    string_display_width,
    strip_ansi,
    truncate_plain,
    wrap_text,
)
from .markdown import render_markdownish
from .types import TranscriptEntry

# Pre-build the separator — Claude-style simple thin line
_SEPARATOR = f"  {SUBTLE}{ICON_DIVIDER * 12}{RESET}"
_SEPARATOR_LINES = ["", _SEPARATOR, ""]
_SEPARATOR_LINE_COUNT = 3


def _indent_block(text: str, prefix: str = "  ") -> str:
    """Indent all lines in a block of text."""
    return "\n".join(prefix + line for line in text.split("\n"))


def preview_tool_body(tool_name: str, body: str) -> str:
    """Truncate tool output based on tool name and content size."""
    max_chars = 1000 if tool_name == "read_file" else 1800
    max_lines = 20 if tool_name == "read_file" else 36

    lines = body.split("\n")
    limited_lines = lines[:max_lines] if len(lines) > max_lines else lines
    limited = "\n".join(limited_lines)

    if len(limited) > max_chars:
        limited = limited[:max_chars] + "..."

    if limited != body:
        return f"{limited}\n{DIM}... output truncated in transcript{RESET}"

    return limited


_TOOL_LABELS = {
    "read_file": "Read file",
    "write_file": "Write file",
    "edit_file": "Edit file",
    "patch_file": "Patch file",
    "run_command": "Run command",
    "search_files": "Search files",
    "grep_files": "Search files",
    "list_files": "List files",
    "file_tree": "Explore files",
    "web_fetch": "Fetch page",
    "task": "Run agent",
    "load_skill": "Load skill",
}


_DIFF_TOOLS = frozenset({"edit_file", "patch_file", "write_file", "diff_viewer"})


def _tool_status(entry: TranscriptEntry) -> tuple[str, str, str]:
    """Return the ``(color, icon, label)`` used by the header."""
    if entry.status == "running":
        return YELLOW, "·", "running"
    if entry.status == "error":
        return BRIGHT_RED, "!", "failed"
    if entry.status == "success":
        return GREEN, "✓", "done"
    return SUBTLE, "·", ""


def _tool_summary(entry: TranscriptEntry, name: str) -> str:
    """Turn the raw input summary into a compact header target."""
    summary = entry.toolSummary or (entry.body if entry.status == "running" else "")
    summary = " ".join(strip_ansi(summary).split())
    if summary == name:
        return ""
    if summary.startswith(name + " "):
        summary = summary[len(name) + 1 :]
    return summary.removeprefix("path=").strip()


def _render_tool_header(
    name: str,
    label: str,
    summary: str,
    cols: int,
    color: str,
    icon: str,
    status: str,
) -> str:
    """Build a one-line header with an optional trailing status.

    The last terminal column is left unused, matching FrameRenderer, so a
    full-width row cannot wrap. Narrow terminals drop the status before they
    truncate the operation itself.
    """
    available = max(1, cols - 1)
    right = f"{color}{status}{RESET}" if status else ""
    right_width = string_display_width(right)
    if right and available - right_width - 2 < 12:
        right, right_width = "", 0
    lead = f"{icon} {label}"
    lead_width = string_display_width(lead)
    # On narrow screens, showing what ran is more useful than showing "done".
    # Drop the status only when doing so actually makes room for the target.
    target_with_status = available - right_width - 2 - 2 - lead_width - 2
    target_without_status = available - 2 - lead_width - 2
    if right and summary and target_with_status < 4 <= target_without_status:
        right, right_width = "", 0
    left_budget = available - right_width - (2 if right else 0)
    lead_budget = max(1, left_budget - 2)
    if lead_width > lead_budget:
        lead = truncate_plain(lead, lead_budget)
        left = f"  {color}{lead}{RESET}"
    else:
        left = f"  {color}{icon}{RESET} {BOLD}{label}{RESET}"
        target_budget = lead_budget - string_display_width(lead) - 2
        if summary and target_budget >= 4:
            target = summary
            if name == "run_command" and not target.startswith("$"):
                target = f"$ {target}"
            target = truncate_plain(target, target_budget)
            style = ACCENT if name == "run_command" else SUBTLE
            left += f"  {style}{target}{RESET}"
    if string_display_width(left) > left_budget:
        left = truncate_plain(left, left_budget)
    if not right:
        return left
    # Keep status close enough to read as part of the row; on wide terminals a
    # far-right status would float away from short tool names.
    gap = min(4, max(2, available - string_display_width(left) - right_width))
    return f"{left}{' ' * gap}{right}"


def _output_style(name: str, line: str) -> str:
    """Add a little semantic color without guessing at arbitrary output."""
    stripped = line.lstrip()
    if stripped.startswith(("ERROR:", "Error:", "error:")):
        return BRIGHT_RED
    if name in _DIFF_TOOLS:
        if stripped.startswith(("+++", "---", "@@")):
            return CYAN
        if stripped.startswith("+"):
            return GREEN
        if stripped.startswith("-"):
            return RED
    return ""


def render_tool_activity(entry: TranscriptEntry, cols: int) -> list[str]:
    """Render a tool call as a compact header plus a left-railed output well."""
    cols = max(8, cols)
    name = entry.toolName or "tool"
    label = _TOOL_LABELS.get(name, name)
    running = entry.status == "running"
    failed = entry.status == "error"
    color, icon, status = _tool_status(entry)
    summary = _tool_summary(entry, name)
    header = _render_tool_header(name, label, summary, cols, color, icon, status)

    # Two spaces, one rail cell, one gutter, and one unused terminal column.
    content_width = max(1, cols - 5)
    rail = BRIGHT_RED if failed else BORDER

    def row(prefix: str, text: str, style: str = "") -> str:
        text = truncate_plain(strip_ansi(text).expandtabs(4), content_width)
        if not text:
            return f"  {rail}{prefix}{RESET}"
        return f"  {rail}{prefix}{RESET} {style}{text}{RESET}"

    def body_row(text: str, style: str = "") -> str:
        return row("│", text, style)

    def close_row(text: str, style: str = SUBTLE) -> str:
        return row("╰", text, style)

    if running:
        return [header]

    # Shell output is the primary content of a command card. Other tools keep
    # their existing collapsed summaries, while failures always show details.
    if entry.collapsed and not failed and (name != "run_command" or not entry.toolSummary):
        body = " ".join(strip_ansi(entry.collapsedSummary or "").split()) or "No output"
        return [header, close_row(body)]

    if entry.collapsePhase and not entry.collapsed and not failed:
        return [header, close_row(f"collapsing{'.' * entry.collapsePhase}")]

    # Drop leading/trailing blank rows so the well starts and ends on content.
    raw_lines = (entry.body or "").splitlines()
    while raw_lines and not raw_lines[0].strip():
        raw_lines.pop(0)
    while raw_lines and not raw_lines[-1].strip():
        raw_lines.pop()
    if not raw_lines:
        return [header, close_row("No output")]

    # Bound physical rows, including a single very long line on a narrow screen.
    max_rows = 8 if failed else 6
    output: list[tuple[str, str]] = []
    truncated = False
    for raw in raw_lines:
        style = _output_style(name, raw)
        parts = wrap_text(strip_ansi(raw).expandtabs(4), content_width).split("\n")
        for part in parts:
            if len(output) >= max_rows:
                truncated = True
                break
            output.append((part, style))
        if truncated:
            break

    lines = [header]
    lines.extend(body_row(part, style or SUBTLE) for part, style in output)
    if truncated:
        hint = "… output continues · /transcript-save <path>" if content_width > 3 else "…"
        lines.append(close_row(hint))
    return lines


def _render_transcript_entry(entry: TranscriptEntry) -> str:
    """Claude-style: minimal icons, clean labels."""
    if entry.kind == "user":
        label = f"{ACCENT}>{RESET} {BOLD}you{RESET}"
        return f"{label}\n{_indent_block(entry.body)}"

    if entry.kind == "assistant":
        if entry.status == "error":
            return f"{BRIGHT_RED}{BOLD}Error{RESET}\n{_indent_block(render_markdownish(entry.body))}"
        content, _ = parse_assistant_text(entry.body)
        return render_markdownish(content)

    if entry.kind == "progress":
        label = f"{ACCENT}{ICON_DOT}{RESET} {SUBTLE}progress{RESET}"
        content, _ = parse_assistant_text(entry.body)
        return f"{label}\n{_indent_block(render_markdownish(content))}"

    if entry.kind == "thinking":
        return f"{ACCENT}✻{RESET} {SUBTLE}thinking{RESET}\n{_indent_block(f'{SUBTLE}{entry.body}{RESET}')}"

    if entry.kind == "tool":
        cols, _ = _cached_terminal_size()
        return "\n".join(render_tool_activity(entry, max(8, cols - 4)))

    return ""


def get_transcript_window_size(window_size: int | None = None) -> int:
    """Calculate the number of lines to display in the transcript window."""
    if window_size is not None:
        return max(1, window_size)
    _, rows = _cached_terminal_size()
    return max(1, rows - 15)


# ---------------------------------------------------------------------------
# Per-entry rendering cache
# ---------------------------------------------------------------------------

_entry_cache: dict[int, tuple[tuple, list[str]]] = {}
_CACHE_MAX_SIZE = 500


def _get_entry_lines(entry: TranscriptEntry) -> list[str]:
    state = (
        entry.kind,
        entry.body,
        entry.status,
        entry.collapsed,
        entry.collapsePhase,
        entry.collapsedSummary,
        entry.toolName,
        entry.toolSummary,
        _cached_terminal_size()[0] if entry.kind == "tool" else None,
    )

    entry_id = id(entry)
    cached = _entry_cache.get(entry_id)
    if cached is not None and cached[0] == state:
        return cached[1]

    lines = _render_transcript_entry(entry).split("\n")

    if len(_entry_cache) > _CACHE_MAX_SIZE:
        keys = list(_entry_cache.keys())
        for k in keys[: len(keys) // 2]:
            del _entry_cache[k]

    _entry_cache[entry_id] = (state, lines)
    return lines


# ---------------------------------------------------------------------------
# Per-entry line count cache
# ---------------------------------------------------------------------------

_line_count_cache: dict[int, tuple[tuple, int]] = {}


def _get_entry_line_count(entry: TranscriptEntry) -> int:
    """Get the number of rendered lines for an entry (uses cache)."""
    state = (
        entry.kind,
        entry.body,
        entry.status,
        entry.collapsed,
        entry.collapsePhase,
        entry.collapsedSummary,
        entry.toolName,
        entry.toolSummary,
        _cached_terminal_size()[0] if entry.kind == "tool" else None,
    )
    entry_id = id(entry)

    cached_lc = _line_count_cache.get(entry_id)
    if cached_lc is not None and cached_lc[0] == state:
        return cached_lc[1]

    cached_full = _entry_cache.get(entry_id)
    if cached_full is not None and cached_full[0] == state:
        count = len(cached_full[1])
        _line_count_cache[entry_id] = (state, count)
        return count

    lines = _get_entry_lines(entry)
    count = len(lines)
    _line_count_cache[entry_id] = (state, count)
    return count


# ---------------------------------------------------------------------------
# Windowed transcript rendering
# ---------------------------------------------------------------------------


def _compute_total_lines(entries: list[TranscriptEntry]) -> int:
    """Compute total line count across all entries including separators."""
    if not entries:
        return 0
    total = 0
    for i, entry in enumerate(entries):
        if i > 0:
            total += _SEPARATOR_LINE_COUNT
        total += _get_entry_line_count(entry)
    return total


def _render_visible_window(
    entries: list[TranscriptEntry],
    start_line: int,
    end_line: int,
) -> list[str]:
    """Only render entries that intersect with the visible [start_line, end_line) range."""
    if not entries:
        return []

    result: list[str] = []
    current_line = 0

    for i, entry in enumerate(entries):
        if i > 0:
            sep_start = current_line
            sep_end = current_line + _SEPARATOR_LINE_COUNT
            if sep_start < end_line and sep_end > start_line:
                vis_start = max(0, start_line - sep_start)
                vis_end = min(_SEPARATOR_LINE_COUNT, end_line - sep_start)
                result.extend(_SEPARATOR_LINES[vis_start:vis_end])
            current_line = sep_end
            if current_line >= end_line:
                break

        entry_line_count = _get_entry_line_count(entry)
        entry_start = current_line
        entry_end = current_line + entry_line_count

        if entry_start < end_line and entry_end > start_line:
            lines = _get_entry_lines(entry)
            vis_start = max(0, start_line - entry_start)
            vis_end = min(entry_line_count, end_line - entry_start)
            result.extend(lines[vis_start:vis_end])

        current_line = entry_end
        if current_line >= end_line:
            break

    return result


def get_transcript_max_scroll_offset(entries: list[TranscriptEntry], window_size: int | None = None) -> int:
    """Calculate the maximum possible scroll offset."""
    if not entries:
        return 0
    total = _compute_total_lines(entries)
    ws = get_transcript_window_size(window_size)
    return max(0, total - ws)


def render_transcript(entries: list[TranscriptEntry], scroll_offset: int, window_size: int | None = None) -> str:
    """Render a windowed view of the transcript."""
    if not entries:
        return ""

    total_lines = _compute_total_lines(entries)
    ws = get_transcript_window_size(window_size)
    max_offset = max(0, total_lines - ws)
    offset = max(0, min(scroll_offset, max_offset))

    end = total_lines - offset
    start = max(0, end - ws)

    visible_lines = _render_visible_window(entries, start, end)
    body = "\n".join(visible_lines)

    if offset == 0:
        return body

    return f"{body}\n\n{SUBTLE}  {ICON_DIVIDER * 2} scroll {offset}/{max_offset} {ICON_DIVIDER * 2}{RESET}"


# ---------------------------------------------------------------------------
# Legacy full-render API
# ---------------------------------------------------------------------------


def _render_transcript_lines(entries: list[TranscriptEntry]) -> list[str]:
    """Render all entries into a list of lines with separators."""
    all_lines: list[str] = []
    for i, entry in enumerate(entries):
        if i > 0:
            all_lines.extend(_SEPARATOR_LINES)
        all_lines.extend(_get_entry_lines(entry))
    return all_lines


def format_transcript_text(entries: list[TranscriptEntry]) -> str:
    """Format transcript entries as plain text (no ANSI) for saving to file."""
    parts = []
    for entry in entries:
        label = "you" if entry.kind == "user" else entry.kind
        if entry.kind == "tool":
            status_text = f" ({entry.status})" if entry.status else ""
            label = f"{entry.toolName or 'tool'}{status_text}"
            if entry.toolSummary:
                label += f"\n  Input: {entry.toolSummary}"
        indented = "\n".join("  " + line for line in entry.body.splitlines())
        parts.append(f"{label}\n{indented}")
    return "\n\n---\n\n".join(parts)
