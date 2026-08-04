"""Agent trace manager for sub-agent observability.

Records a lightweight tree of sub-agent invocations so callers can see token
usage, tool-call counts, and wall-clock time per sub-agent -- including nested
calls.  Mirrors mewcode's TraceManager but kept synchronous and dependency
free.

Design notes
------------
* ``trace_id`` is a short opaque string unique per sub-agent invocation.
* ``parent_id`` links a sub-agent to its caller (the root agent's id is the
  ``session_id`` passed to :class:`TraceManager`).
* Tokens are aggregated bottom-up via :meth:`TraceManager.get_total` so the
  parent agent's cost reflects everything its children consumed.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator


def _new_id() -> str:
    """Return a short, URL-safe, opaque id."""
    return secrets.token_hex(4)


@dataclass(slots=True)
class TraceNode:
    """A single sub-agent invocation record."""

    trace_id: str
    parent_id: str | None
    agent_type: str
    name: str
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    status: str = "running"  # running | completed | failed

    @property
    def elapsed_seconds(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def mark_complete(self, status: str = "completed") -> None:
        self.ended_at = time.time()
        self.status = status


class TraceManager:
    """In-memory registry of sub-agent traces.

    Thread-safe; cheap to construct.  The agent loop typically holds one
    instance per session and passes it to the Task tool so every sub-agent
    invocation is recorded.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or _new_id()
        self._nodes: dict[str, TraceNode] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        agent_type: str,
        name: str,
        parent_id: str | None = None,
    ) -> TraceNode:
        """Register a new sub-agent invocation and return its node.

        ``parent_id`` defaults to the session id so top-level sub-agents are
        linked to the root agent.
        """
        node = TraceNode(
            trace_id=_new_id(),
            parent_id=parent_id or self.session_id,
            agent_type=agent_type,
            name=name,
        )
        with self._lock:
            self._nodes[node.trace_id] = node
        return node

    def record_tokens(self, trace_id: str, input_tokens: int, output_tokens: int) -> None:
        """Accumulate token usage reported by the model adapter."""
        with self._lock:
            node = self._nodes.get(trace_id)
            if node is None:
                return
            node.input_tokens += int(input_tokens or 0)
            node.output_tokens += int(output_tokens or 0)

    def record_tool_call(self, trace_id: str) -> None:
        """Increment the tool-call counter for a sub-agent."""
        with self._lock:
            node = self._nodes.get(trace_id)
            if node is None:
                return
            node.tool_call_count += 1

    def complete(self, trace_id: str, status: str = "completed") -> None:
        """Mark a sub-agent invocation as finished."""
        with self._lock:
            node = self._nodes.get(trace_id)
            if node is None:
                return
            node.mark_complete(status)

    def get(self, trace_id: str) -> TraceNode | None:
        with self._lock:
            return self._nodes.get(trace_id)

    def all_nodes(self) -> list[TraceNode]:
        with self._lock:
            return list(self._nodes.values())

    def get_total(self, trace_id: str | None = None) -> dict[str, int]:
        """Aggregate token usage for ``trace_id`` and all its descendants.

        When ``trace_id`` is ``None`` the session root is used so the total
        reflects every sub-agent that ran.  The starting node's own tokens are
        included in the sum.
        """
        with self._lock:
            nodes = list(self._nodes.values())

        if trace_id is None:
            target_id: str | None = self.session_id
        else:
            target_id = trace_id

        # Collect the starting node (if it exists) plus all descendants.
        collected: list[TraceNode] = []
        start_node = next((n for n in nodes if n.trace_id == target_id), None)
        if start_node is not None:
            collected.append(start_node)

        def _children(parent: str | None) -> Iterator[TraceNode]:
            for node in nodes:
                if node.parent_id == parent:
                    yield node
                    yield from _children(node.trace_id)

        collected.extend(_children(target_id))

        total_input = sum(n.input_tokens for n in collected)
        total_output = sum(n.output_tokens for n in collected)
        total_tools = sum(n.tool_call_count for n in collected)
        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "tool_call_count": total_tools,
        }

    def format_summary(self, trace_id: str | None = None) -> str:
        """Render a one-line summary suitable for appending to tool output."""
        totals = self.get_total(trace_id)
        return (
            f"[trace: in={totals['input_tokens']} out={totals['output_tokens']} "
            f"tool_calls={totals['tool_call_count']}]"
        )


__all__ = ["TraceManager", "TraceNode"]
