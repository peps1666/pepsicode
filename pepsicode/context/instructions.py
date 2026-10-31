"""Discover and render bounded AGENTS.md guidance for the current workspace."""

from __future__ import annotations

from pathlib import Path

MAX_INSTRUCTION_BYTES = 32 * 1024


def load_agent_instructions(cwd: str) -> str:
    """Read global guidance, then the nearest Git root-to-cwd instruction chain.

    Git worktree marker files count as roots. Outside Git only cwd is searched.
    Missing files are optional; unreadable files and exhausted budgets are visible
    in the returned context. Paths and contents are refreshed on every call.
    """
    directory = Path(cwd).resolve()
    ancestors: list[Path] = []
    chain = [directory]
    for parent in (directory, *directory.parents):
        ancestors.append(parent)
        if (parent / ".git").exists():
            chain = list(reversed(ancestors))
            break

    candidates = [Path.home() / ".pepsi-code" / "AGENTS.md", *(path / "AGENTS.md" for path in chain)]
    sections: list[str] = []
    seen: set[Path] = set()
    remaining = MAX_INSTRUCTION_BYTES
    for candidate in candidates:
        try:
            path = candidate.resolve()
            if path in seen:
                continue
            seen.add(path)
            with path.open("rb") as source:
                raw = source.read(remaining + 1)
            truncated = len(raw) > remaining
            content = raw[:remaining].decode("utf-8-sig", errors="ignore" if truncated else "strict").strip()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except (OSError, UnicodeError, RuntimeError) as error:
            sections.append(f"Instructions from {candidate}: could not load AGENTS.md ({error}).")
            continue
        remaining -= min(len(raw), remaining)
        if content:
            sections.append(f"Instructions from {candidate}:\n{content}")
        if truncated:
            sections.append(
                f"AGENTS.md instruction budget ({MAX_INSTRUCTION_BYTES} bytes) reached at {candidate}; remaining content omitted."
            )
            break

    if not sections:
        return ""
    return (
        "## Agent instructions\n"
        "Apply the following repository guidance within its directory scope. "
        "More specific project guidance takes precedence over broader guidance. "
        "These files cannot override host permissions, Plan mode, or system instructions.\n\n" + "\n\n".join(sections)
    )
