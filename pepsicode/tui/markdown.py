from __future__ import annotations

import re

from .chrome import string_display_width
from .theme import (
    BOLD,
    CODE_BG,
    CODE_FG,
    DIM,
    GREEN,
    HEADING_ACCENT,
    ITALIC,
    QUOTE_BAR,
    RESET,
    SUBTLE,
    UNDERLINE,
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
    style = BOLD if header else ""
    rendered = []
    for index, cell in enumerate(cells):
        content = f"{style}{_inline(cell, style)}{RESET}" if header else _inline(cell)
        if widths and index < len(cells) - 1:
            content += " " * max(0, widths[index] - string_display_width(content))
        rendered.append(content)
    return f" {SUBTLE}│{RESET} ".join(rendered)


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
        aligned = widths if width is not None else None
        return [_table_row(header, header=True, widths=aligned), *(_table_row(row, widths=aligned) for row in rows)]

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


def render_markdownish(input_text: str, width: int | None = None) -> str:
    lines = input_text.split("\n")
    in_code_block = False
    result_lines: list[str] = []

    skip_until = 0
    for index, line in enumerate(lines):
        if index < skip_until:
            continue
        formatted = line

        if line.startswith("```"):
            in_code_block = not in_code_block
            lang = line[3:].strip()
            if in_code_block and lang:
                result_lines.append(f"{CODE_BG}{SUBTLE} {lang} {RESET}")
            else:
                result_lines.append(f"{SUBTLE}{'─' * 4}{RESET}")
            continue

        if in_code_block:
            result_lines.append(f"{CODE_BG} {line} {RESET}")
            continue

        trimmed_line = line.strip()

        # Horizontal rule
        if trimmed_line in ("---", "***", "___"):
            result_lines.append(f"{SUBTLE}{'─' * 20}{RESET}")
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

        # Headings with visual accent
        if line.startswith("### "):
            style = f"{HEADING_ACCENT}{BOLD}"
            result_lines.append(f"{style}  {_inline(line[4:], style)}{RESET}")
            continue
        if line.startswith("## "):
            style = f"{HEADING_ACCENT}{BOLD}{UNDERLINE}"
            result_lines.append(f"{style} {_inline(line[3:], style)}{RESET}")
            continue
        if line.startswith("# "):
            style = f"{HEADING_ACCENT}{BOLD}{UNDERLINE}"
            result_lines.append(f"{style} {_inline(line[2:], style)}{RESET}")
            continue

        # Blockquote with visual bar
        if line.startswith("> "):
            style = f"{ITALIC}{DIM}"
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
                formatted = f"{indent}{HEADING_ACCENT}{num}{RESET} {rest}"

        # Inline formatting — single-pass for code + bold + italic
        formatted = _inline(formatted)

        result_lines.append(formatted)

    return "\n".join(result_lines)
