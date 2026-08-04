"""Zero-dependency YAML frontmatter parser for agent definition files.

Only supports the flat ``key: value`` and ``key:`` + ``  - item`` list forms
that agent definitions need -- intentionally not a general YAML parser.  Keeps
pepsicode dependency-free while allowing markdown-driven agent configuration.

Example frontmatter this can parse::

    ---
    name: Explore
    description: Fast read-only agent
    model: inherit
    maxTurns: 5
    isReadOnly: true
    allowedTools:
      - read_file
      - list_files
    disallowedTools: []
    ---
    You are an exploration agent...
"""

from __future__ import annotations

import re
from typing import Any

_FRONTMARE_DELIM = "---"
_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.*)$")


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into ``(frontmatter_dict, body)``.

    Returns ``({}, raw)`` when no frontmatter block is present.
    """
    lines = raw.splitlines()
    if not lines or lines[0].strip() != _FRONTMARE_DELIM:
        return {}, raw

    # Find the closing ``---``.
    end_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMARE_DELIM:
            end_idx = i
            break
    if end_idx is None:
        return {}, raw

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1 :])
    return _parse_yaml_block(fm_lines), body


def _parse_yaml_block(lines: list[str]) -> dict[str, Any]:
    """Parse a flat YAML block into a dict.

    Supports ``key: value`` scalars and ``key:`` + ``- item`` lists.  Nested
    mappings are not supported (agent definitions don't need them).
    """
    result: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # ``key: value`` (inline scalar)
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if not key:
                i += 1
                continue

            # Empty value -- could be a list or an empty mapping.
            if value == "":
                # Peek: is the next non-empty line a list item?
                items: list[str] = []
                j = i + 1
                while j < len(lines):
                    next_stripped = lines[j].strip()
                    if not next_stripped or next_stripped.startswith("#"):
                        j += 1
                        continue
                    m = _LIST_ITEM_RE.match(lines[j])
                    if m:
                        items.append(_unquote(m.group(1).strip()))
                        j += 1
                    else:
                        break
                if items:
                    result[key] = items
                    i = j
                    continue
                # Empty list ``[]``
                result[key] = []
                i += 1
                continue

            # Inline value
            if value == "[]":
                result[key] = []
            elif value == "{}":
                result[key] = {}
            else:
                result[key] = _parse_scalar(value)
            i += 1
            continue

        i += 1

    return result


def _parse_scalar(value: str) -> Any:
    """Convert a YAML scalar string to a Python value."""
    # Boolean
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none", "~"):
        return None
    # Integer
    try:
        return int(value)
    except ValueError:
        pass
    # Float
    try:
        return float(value)
    except ValueError:
        pass
    return _unquote(value)


def _unquote(value: str) -> str:
    """Strip matching surrounding quotes from a string."""
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    return value


__all__ = ["parse_frontmatter"]
