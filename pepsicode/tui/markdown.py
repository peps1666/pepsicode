from __future__ import annotations

import re

from .theme import (
    BOLD,
    CODE_BG,
    CODE_FG,
    DIM,
    HEADING_ACCENT,
    ITALIC,
    QUOTE_BAR,
    RESET,
    SUBTLE,
    UNDERLINE,
)

# Pre-compiled regexes for markdown parsing
_RE_TABLE_SEP = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+$")
_RE_TABLE_ROW = re.compile(r"^\|.*\|$")
_RE_LIST_ITEM = re.compile(r"^(\s*)[-*]\s+")
_RE_NUMBERED_LIST = re.compile(r"^(\s*)\d+\.\s+")

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
        return f"{CODE_BG}{CODE_FG}{m.group(1)}{RESET}"
    if m.group(2) is not None:
        return f"{BOLD}{m.group(2)}{RESET}"
    if m.group(3) is not None:
        return f"{ITALIC}{m.group(3)}{RESET}"
    return m.group(0)


def render_markdownish(input_text: str) -> str:
    lines = input_text.split("\n")
    in_code_block = False
    result_lines: list[str] = []

    for line in lines:
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

        # Table separator logic
        if _RE_TABLE_SEP.match(trimmed_line):
            result_lines.append(f"{SUBTLE}{trimmed_line.replace('|', ' ').strip()}{RESET}")
            continue

        # Table data row logic
        if _RE_TABLE_ROW.match(trimmed_line):
            cells = [cell.strip() for cell in line.split("|") if cell.strip()]
            result_lines.append(f" {SUBTLE}│{RESET} ".join(cells))
            continue

        # Headings with visual accent
        if line.startswith("### "):
            result_lines.append(f"{HEADING_ACCENT}{BOLD}  {line[4:]}{RESET}")
            continue
        if line.startswith("## "):
            result_lines.append(f"{HEADING_ACCENT}{BOLD}{UNDERLINE} {line[3:]}{RESET}")
            continue
        if line.startswith("# "):
            result_lines.append(f"{HEADING_ACCENT}{BOLD}{UNDERLINE} {line[2:]}{RESET}")
            continue

        # Blockquote with visual bar
        if line.startswith("> "):
            result_lines.append(f"{QUOTE_BAR}▎{RESET} {ITALIC}{DIM}{line[2:]}{RESET}")
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
        formatted = _RE_INLINE_COMBINED.sub(_inline_replace, formatted)

        result_lines.append(formatted)

    return "\n".join(result_lines)
