from __future__ import annotations

import re

from .chrome import pad_plain, paint_row, string_display_width, strip_ansi, truncate_plain, wrap_text
from .theme import (
    BOLD,
    BORDER,
    BORDER_DIM,
    BRAND_LIME_DIM,
    BRIGHT_WHITE,
    CODE_BG,
    CODE_FG,
    CYAN,
    GREEN,
    HEADING_ACCENT,
    ITALIC,
    QUOTE_BAR,
    RESET,
    SUBTLE,
)

# Pre-compiled regexes for markdown parsing
_RE_TABLE_CELL_SEP = re.compile(r"^:?-+:?$")
_RE_LIST_ITEM = re.compile(r"^(\s*)[-*]\s+")
_RE_NUMBERED_LIST = re.compile(r"^(\s*)\d+\.\s+")
_RE_FILENAME = re.compile(r"^[^\s/\\]+\.[A-Za-z0-9_-]{1,16}$")

# Combined inline pattern: match code, bold, or italic in a single pass
# Order matters: code first (most specific), then bold, then italic
_RE_INLINE_COMBINED = re.compile(
    r"`([^`]+)`"  # group 1: inline code
    r"|\*\*([^*]+)\*\*"  # group 2: bold
    r"|(?<!\*)\*([^*]+)\*(?!\*)"  # group 3: italic
)


def _inline_replace(m: re.Match) -> str:
    """Single-pass replacement callback for inline markdown."""
    if m.group(1) is not None:
        value = m.group(1)
        path_like = "/" in value or "\\" in value or bool(_RE_FILENAME.fullmatch(value))
        return f"{GREEN if path_like else CODE_FG}{value}{RESET}"
    if m.group(2) is not None:
        return f"{BOLD}{m.group(2)}{RESET}"
    if m.group(3) is not None:
        return f"{ITALIC}{m.group(3)}{RESET}"
    return m.group(0)


def _inline(text: str, resume: str = "") -> str:
    return _RE_INLINE_COMBINED.sub(lambda m: _inline_replace(m) + resume, text)


def _table_cells(line: str) -> list[str] | None:
    """Split Markdown cells without treating escaped or code-span pipes as borders."""
    line = line.strip()
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    code = False
    separators = 0
    for ch in line:
        if escaped:
            if ch != "|":
                current.append("\\")
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "`":
            code = not code
            current.append(ch)
        elif ch == "|" and not code:
            cells.append("".join(current).strip())
            current = []
            separators += 1
        else:
            current.append(ch)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    if not separators:
        return None
    if line.startswith("|"):
        cells.pop(0)
    if line.endswith("|") and not line.endswith("\\|"):
        cells.pop()
    return cells


def _is_table_separator(cells: list[str] | None, count: int) -> bool:
    return bool(cells and len(cells) == count and all(_RE_TABLE_CELL_SEP.fullmatch(cell) for cell in cells))


def _table_row(cells: list[str], *, header: bool = False, widths: list[int] | None = None) -> str:
    style = f"{BRIGHT_WHITE}{BOLD}" if header else ""
    rendered = []
    for index, cell in enumerate(cells):
        content = f"{style}{_inline(cell, style)}{RESET}" if header else _inline(cell)
        if widths and index < len(cells) - 1:
            content += " " * max(0, widths[index] - string_display_width(content))
        rendered.append(content)
    return f" {BORDER}│{RESET} ".join(rendered)


def _table_separator(widths: list[int]) -> str:
    segments = []
    for index, col_w in enumerate(widths):
        extra = 1 if index in (0, len(widths) - 1) else 2
        segments.append("─" * (max(1, col_w) + extra))
    return f"{BORDER}{'┼'.join(segments)}{RESET}"


def _render_table(header: list[str], rows: list[list[str]], width: int | None) -> list[str]:
    if not rows:
        return [_table_row(header, header=True)]
    columns = max(len(header), *(len(row) for row in rows))
    all_rows = [header, *rows]
    widths = [
        max(string_display_width(_inline(row[i])) if i < len(row) else 0 for row in all_rows) for i in range(columns)
    ]
    total_width = sum(widths) + 3 * (columns - 1)
    if width is None or total_width <= width:
        return [
            _table_row(header, header=True, widths=widths),
            _table_separator(widths),
            *(_table_row(row, widths=widths) for row in rows),
        ]

    # Narrow screens use labeled fields, so wrapping cannot obscure which
    # value belongs to which column. An explicit empty value remains visible.
    result: list[str] = []
    for row_index, row in enumerate(rows):
        if row_index:
            result.append("")
        for index, value in enumerate(row):
            label = _inline(header[index], BOLD) if index < len(header) else ""
            if label:
                result.append(f"{BOLD}{label}{RESET}: {_inline(value)}")
            else:
                result.append(_inline(value))
    return result


def _render_code_block(code_lines: list[str], lang: str, width: int | None, *, closed: bool) -> list[str]:
    """Render a fenced code block with a uniform background width and rounded borders."""
    expanded = [line.expandtabs(4) for line in code_lines]
    max_line_w = max((string_display_width(line) for line in expanded), default=0)
    lang_label = truncate_plain(lang, max(1, (width - 6) if width is not None else 32)) if lang else ""
    lang_need = (string_display_width(lang_label) + 4) if lang_label else 4
    desired = max(16, max_line_w + 2, lang_need)
    block_w = min(desired, max(8, width - 2)) if width is not None else desired

    if lang_label and block_w >= string_display_width(lang_label) + 4:
        dash_count = max(1, block_w - string_display_width(lang_label) - 3)
        top = f"{BORDER}╭─{RESET}{CYAN} {lang_label} {RESET}{BORDER}{'─' * dash_count}{RESET}"
    else:
        top = f"{BORDER}╭{'─' * block_w}{RESET}"

    rendered = [top]
    inner_text_w = max(1, block_w - 2)
    for raw in expanded:
        wrapped = wrap_text(raw, inner_text_w).split("\n") if raw else [""]
        for part in wrapped:
            text_cell = f" {pad_plain(part, inner_text_w)} "
            if CODE_BG:
                body = paint_row(text_cell, block_w, CODE_BG)
            else:
                body = f" {part}"
            rendered.append(f"{BORDER}│{RESET}{body}")
    if closed:
        rendered.append(f"{BORDER}╰{'─' * block_w}{RESET}")
    return rendered


def render_markdownish(input_text: str, width: int | None = None) -> str:
    lines = input_text.split("\n")
    result_lines: list[str] = []

    def separate_section() -> None:
        """Give a heading its own row instead of gluing it to the text above."""
        if result_lines and strip_ansi(result_lines[-1]).strip():
            result_lines.append("")

    skip_until = 0
    for index, line in enumerate(lines):
        if index < skip_until:
            continue
        formatted = line

        if line.startswith("```"):
            lang = line[3:].strip()
            code_lines: list[str] = []
            cursor = index + 1
            closed = False
            while cursor < len(lines):
                if lines[cursor].startswith("```"):
                    closed = True
                    cursor += 1
                    break
                code_lines.append(lines[cursor])
                cursor += 1
            result_lines.extend(_render_code_block(code_lines, lang, width, closed=closed))
            skip_until = cursor
            continue

        trimmed_line = line.strip()

        # Horizontal rule
        if trimmed_line in ("---", "***", "___"):
            rule_w = min(36, max(12, width - 4)) if width is not None else 24
            result_lines.append(f"{BORDER_DIM}{'─' * rule_w}{RESET}")
            continue

        # Recognize tables only when a header is followed by a delimiter row.
        header = _table_cells(line)
        delimiter = _table_cells(lines[index + 1]) if index + 1 < len(lines) else None
        if header and _is_table_separator(delimiter, len(header)):
            rows: list[list[str]] = []
            skip_until = index + 2
            while skip_until < len(lines):
                cells = _table_cells(lines[skip_until])
                if cells is None:
                    break
                if len(cells) < len(header):
                    cells.extend([""] * (len(header) - len(cells)))
                rows.append(cells)
                skip_until += 1
            result_lines.extend(_render_table(header, rows, width))
            continue

        # Headings rely on color and weight for hierarchy: underlining the accent
        # color doubles the emphasis and reads like a link, so no decoration here.
        if line.startswith("### "):
            style = f"{BRIGHT_WHITE}{BOLD}"
            separate_section()
            result_lines.append(f"  {style}{_inline(line[4:], style)}{RESET}")
            continue
        if line.startswith("## "):
            style = f"{HEADING_ACCENT}{BOLD}"
            separate_section()
            result_lines.append(f"{style}{_inline(line[3:], style)}{RESET}")
            continue
        if line.startswith("# "):
            style = f"{HEADING_ACCENT}{BOLD}"
            separate_section()
            result_lines.append(f"{style}▍ {_inline(line[2:], style)}{RESET}")
            continue

        # Blockquote with visual bar
        if line.startswith("> "):
            style = f"{ITALIC}{SUBTLE}"
            result_lines.append(f"{QUOTE_BAR}▎{RESET} {style}{_inline(line[2:], style)}{RESET}")
            continue

        # Unordered list items with colored bullet
        m = _RE_LIST_ITEM.match(line)
        if m:
            indent = m.group(1)
            rest = line[m.end() :]
            formatted = f"{indent}{HEADING_ACCENT}•{RESET} {rest}"
        else:
            # Numbered list items with colored number
            m2 = _RE_NUMBERED_LIST.match(line)
            if m2:
                indent = m2.group(1)
                rest = line[m2.end() :]
                num = line[len(indent) : m2.end()].strip()
                formatted = f"{indent}{BRAND_LIME_DIM}{num}{RESET} {rest}"

        # Inline formatting — single-pass for code + bold + italic
        formatted = _inline(formatted)

        result_lines.append(formatted)

    return "\n".join(result_lines)
