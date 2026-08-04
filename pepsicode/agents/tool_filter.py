"""Layered tool filtering for sub-agents.

Replaces the hard-coded ``_READ_ONLY_TOOLS`` / ``_GENERAL_EXTRA_TOOLS`` lists
that used to live in ``tools/task.py`` with a single resolver that applies a
stack of filters.  Each layer can only remove tools or pin a whitelist -- never
add new ones -- so the parent registry is the single source of truth.

Filter layers (applied in order):

1. **Global disallow** -- tools no sub-agent may ever use (prevents recursion
   via the Task tool, blocks plan/permission manipulation).
2. **Definition disallow** -- the agent's ``disallowed_tools`` list (from
   markdown frontmatter or the built-in :class:`AgentDefinition`).
3. **Whitelist** -- when the agent defines ``allowed_tools`` only those are
   kept (mutually exclusive with layer 2 in practice, but both may appear).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pepsicode.sub_agents import AgentDefinition
    from pepsicode.tooling import ToolDefinition, ToolRegistry

# Tools no sub-agent may ever call.  ``task`` prevents unbounded recursion;
# the others let a sub-agent bypass the parent's permission/plan flow.
GLOBAL_DISALLOWED: frozenset[str] = frozenset(
    {
        "task",  # prevent recursion
        "ask_user",  # sub-agents cannot interact with the user
    }
)


def resolve_agent_tools(
    parent_registry: ToolRegistry,
    definition: AgentDefinition,
) -> ToolRegistry:
    """Build a restricted :class:`ToolRegistry` for a sub-agent.

    Parameters
    ----------
    parent_registry
        The full tool registry the parent agent has access to.
    definition
        The sub-agent definition -- may carry ``disallowed_tools`` and/or
        ``allowed_tools``.
    """
    from pepsicode.tooling import ToolRegistry

    # Start from everything the parent can see.
    available: dict[str, ToolDefinition] = {t.name: t for t in parent_registry.list()}

    # Layer 1: global disallow.
    for name in GLOBAL_DISALLOWED:
        available.pop(name, None)

    # Layer 2: definition-level disallow (from markdown frontmatter or
    # built-in AgentDefinition).
    disallowed = getattr(definition, "disallowed_tools", None) or []
    for name in disallowed:
        available.pop(name, None)

    # Layer 3: optional whitelist.  When present, keep only the listed tools.
    allowed = getattr(definition, "allowed_tools", None)
    if allowed:
        keep = set(allowed)
        available = {n: t for n, t in available.items() if n in keep}

    return ToolRegistry(list(available.values()))


__all__ = ["GLOBAL_DISALLOWED", "resolve_agent_tools"]
