"""Hot-reloadable markdown-driven agent definitions.

Mirrors the three-level discovery pattern used by :mod:`pepsicode.skills`:

1. **Project** -- ``<cwd>/.pepsi-code/agents/*.md``  (highest priority)
2. **User** -- ``~/.pepsi-code/agents/*.md``
3. **Built-in** -- ``pepsicode/agents/builtins/*.md``  (lowest priority)

A higher-priority file with the same base name overrides a lower one.  Each
``.get()`` call checks the file's mtime and reloads on change -- so editing an
agent definition takes effect on the next sub-agent invocation without
restarting pepsicode.

The built-in definitions (explore.md, plan.md, general-purpose.md,
verification.md) are shipped with pepsicode and can be overridden by placing a
file with the same name in the project or user directory.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pepsicode.agents.parser import parse_frontmatter
from pepsicode.sub_agents import AgentDefinition, AgentType

# Mapping from agent file name (without .md) to the built-in AgentType enum.
# Custom agents (not in this map) get ``AgentType.GENERAL`` so they run with
# full capabilities by default; the markdown frontmatter can still restrict
# tools via ``allowedTools`` / ``disallowedTools``.
_BUILTIN_TYPES: dict[str, AgentType] = {
    "explore": AgentType.EXPLORE,
    "plan": AgentType.PLAN,
    "general": AgentType.GENERAL,
    "general-purpose": AgentType.GENERAL,
    "verification": AgentType.GENERAL,  # verification is a specialized general agent
}


@dataclass
class _CacheEntry:
    """Cached agent definition with mtime for hot-reload detection."""

    definition: AgentDefinition
    mtime: float
    path: str


class AgentLoader:
    """Discovers, loads, and hot-reloads agent definitions from markdown files.

    Thread-safe.  Construct one per session and call :meth:`get` or
    :meth:`list_names` as needed.
    """

    def __init__(self, cwd: str | Path) -> None:
        self._cwd = Path(cwd)
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        self._builtin_dir = Path(__file__).parent / "builtins"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, name: str) -> AgentDefinition | None:
        """Return the agent definition for ``name`` (e.g. ``"explore"``).

        Reloads from disk if the file changed since the last read.  Returns
        ``None`` when no definition exists.
        """
        normalized = name.strip().lower()
        if not normalized:
            return None

        path = self._resolve(normalized)
        if path is None:
            return None

        with self._lock:
            cached = self._cache.get(normalized)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                return None

            if cached is not None and cached.mtime == mtime and cached.path == str(path):
                return cached.definition

            definition = self._load_file(path, normalized)
            if definition is not None:
                self._cache[normalized] = _CacheEntry(definition=definition, mtime=mtime, path=str(path))
            return definition

    def list_names(self) -> list[str]:
        """Return all available agent names (built-in + discovered)."""
        names: set[str] = set()
        for root in self._roots():
            if not root.exists():
                continue
            for entry in root.iterdir():
                if entry.suffix == ".md" and entry.is_file():
                    names.add(entry.stem.lower())
        return sorted(names)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _roots(self) -> list[Path]:
        """Return search roots in priority order (highest first)."""
        home = Path.home()
        return [
            self._cwd / ".pepsi-code" / "agents",  # project
            home / ".pepsi-code" / "agents",  # user
            self._builtin_dir,  # built-in
        ]

    def _resolve(self, name: str) -> Path | None:
        """Find the highest-priority ``<name>.md`` file."""
        for root in self._roots():
            candidate = root / f"{name}.md"
            if candidate.exists():
                return candidate
        return None

    def _load_file(self, path: Path, name: str) -> AgentDefinition | None:
        """Parse a markdown agent file into an :class:`AgentDefinition`."""
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None

        frontmatter, body = parse_frontmatter(raw)
        if not frontmatter:
            # No frontmatter -- treat the whole file as the system prompt with
            # defaults.
            frontmatter = {}
            body = raw

        return self._build_definition(name, frontmatter, body)

    def _build_definition(
        self,
        name: str,
        fm: dict[str, Any],
        body: str,
    ) -> AgentDefinition:
        """Convert parsed frontmatter + body into an :class:`AgentDefinition`."""

        # Map frontmatter keys (camelCase, as in mewcode) to AgentDefinition
        # fields (snake_case).  Accept both forms for flexibility.
        def _get(*keys: str, default: Any = None) -> Any:
            for k in keys:
                if k in fm:
                    return fm[k]
            return default

        display_name = str(_get("name", "displayName", default=name.title()))
        description = str(_get("description", default=""))
        model = str(_get("model", default="inherit"))
        max_turns = int(_get("maxTurns", "max_turns", default=10))
        is_read_only = bool(_get("isReadOnly", "is_read_only", default=False))
        allowed_tools = _get("allowedTools", "allowed_tools", "tools", default=[])
        if isinstance(allowed_tools, str):
            allowed_tools = [allowed_tools]
        allowed_tools = [str(t) for t in allowed_tools]

        disallowed_tools = _get("disallowedTools", "disallowed_tools", default=[])
        if isinstance(disallowed_tools, str):
            disallowed_tools = [disallowed_tools]
        disallowed_tools = [str(t) for t in disallowed_tools]

        agent_type = _BUILTIN_TYPES.get(name, AgentType.GENERAL)

        return AgentDefinition(
            type=agent_type,
            name=display_name,
            description=description,
            system_prompt_template=body.strip() or f"You are {display_name}.",
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            model=model,
            max_turns=max_turns,
            is_read_only=is_read_only,
        )


# ---------------------------------------------------------------------------
# Default singleton for convenience
# ---------------------------------------------------------------------------

_default_loader: AgentLoader | None = None
_default_loader_cwd: str | None = None
_default_lock = threading.Lock()


def get_default_loader(cwd: str | Path) -> AgentLoader:
    """Return a process-wide :class:`AgentLoader` for ``cwd``.

    A new loader is created when the cwd changes so project-level agent
    overrides are picked up.
    """
    global _default_loader, _default_loader_cwd
    cwd_str = str(cwd)
    with _default_lock:
        if _default_loader is None or _default_loader_cwd != cwd_str:
            _default_loader = AgentLoader(cwd)
            _default_loader_cwd = cwd_str
        return _default_loader


__all__ = ["AgentLoader", "get_default_loader"]
