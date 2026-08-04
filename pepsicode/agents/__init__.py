"""Agent subsystem: trace management, markdown agent definitions, tool filtering.

Public entry points:

* :class:`TraceManager` -- sub-agent observability (token/cost/turn tracking).
* :func:`resolve_agent_tools` -- layered tool filtering for sub-agents.
* :class:`AgentLoader` / :func:`get_default_loader` -- hot-reloadable markdown
  agent definitions with three-level discovery (project > user > built-in).
"""

from pepsicode.agents.loader import AgentLoader, get_default_loader
from pepsicode.agents.tool_filter import GLOBAL_DISALLOWED, resolve_agent_tools
from pepsicode.agents.trace import TraceManager, TraceNode

__all__ = [
    "AgentLoader",
    "GLOBAL_DISALLOWED",
    "TraceManager",
    "TraceNode",
    "get_default_loader",
    "resolve_agent_tools",
]
