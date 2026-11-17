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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Iterator


def _new_id() -> str:
    """Return a short, URL-safe, opaque id."""
    return secrets.token_hex(16)


TRACE_VERSION = 1
SUMMARY_LIMIT = 2000
REPORT_LIMIT = 64
RECENT_TRACE_LIMIT = 128
REPORT_STATES = {"progress", "ready", "blocked", "failed", "dropped"}
EXECUTION_STATES = {
    "starting",
    "running",
    "completed",
    "awaiting_user",
    "failed",
    "blocked",
    "cancelled",
    "timed_out",
    "budget_exhausted",
    "unknown",
    "interrupted",
}


@dataclass(frozen=True, slots=True)
class Activity:
    id: str
    kind: str
    summary: str
    started_at: float = field(default_factory=time.time)
    tool: str | None = None
    input_preview: str = ""


@dataclass(frozen=True, slots=True)
class AgentReport:
    id: str
    status: str
    summary: str
    revision: int | None
    stale: bool
    question_id: str | None = None
    submitted_at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class ToolPreview:
    id: str
    tool: str
    summary: str
    status: str
    output: str
    duration_ms: int
    input_preview: str = ""


@dataclass(slots=True)
class TraceNode:
    """A single sub-agent invocation record."""

    trace_id: str
    parent_id: str | None
    agent_type: str
    name: str
    session_id: str = ""
    parent_task_id: str | None = None
    spawn_revision: int | None = None
    execution_scope_id: str | None = None
    parent_call_id: str | None = None
    journal: str | None = None
    branch_entry_id: str | None = None
    task: str = ""
    last_activity_at: float = field(default_factory=time.time)
    end_reason: str | None = None
    result_summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    status: str = "running"
    activities: tuple[Activity, ...] = ()
    reports: tuple[AgentReport, ...] = ()
    recent_tools: tuple[ToolPreview, ...] = ()

    @property
    def elapsed_seconds(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def mark_complete(self, status: str = "completed") -> None:
        self.ended_at = time.time()
        self.status = status


class TraceManager:
    """Thread-safe registry of live and recent traces, with optional session history.

    Thread-safe; cheap to construct.  The agent loop typically holds one
    instance per session and passes it to the Task tool so every sub-agent
    invocation is recorded.
    """

    def __init__(self, session_id: str | None = None, *, read_archive: Callable[[], list[dict]] | None = None) -> None:
        self.session_id = session_id or _new_id()
        self._nodes: dict[str, TraceNode] = {}
        self._lock = threading.Lock()
        self._sequence = 0
        self._read_archive = read_archive
        self._has_archive = False
        self.persistence_error: str | None = None

    def create(
        self,
        *,
        agent_type: str,
        name: str,
        parent_id: str | None = None,
        parent_task_id: str | None = None,
        spawn_revision: int | None = None,
        task: str = "",
        branch_entry_id: str | None = None,
        parent_call_id: str | None = None,
        status: str = "running",
        trace_id: str | None = None,
    ) -> TraceNode:
        """Register a new sub-agent invocation and return its node.

        ``parent_id`` defaults to the session id so top-level sub-agents are
        linked to the root agent.
        """
        node = TraceNode(
            trace_id=trace_id or _new_id(),
            parent_id=parent_id or self.session_id,
            agent_type=agent_type,
            name=name[:160],
            session_id=self.session_id,
            parent_task_id=parent_task_id,
            spawn_revision=spawn_revision,
            task=task[:SUMMARY_LIMIT],
            branch_entry_id=branch_entry_id,
            parent_call_id=parent_call_id,
            status=status,
        )
        with self._lock:
            if node.trace_id in self._nodes:
                raise ValueError("Agent trace already exists")
            self._nodes[node.trace_id] = node
            self._sequence += 1
        return replace(node)

    def _update(self, trace_id, change, *, after_exit=False) -> None:
        with self._lock:
            node = self._nodes.get(trace_id)
            if node is not None and (after_exit or node.ended_at is None):
                if change(node) is not False:
                    node.last_activity_at = time.time()
                    self._sequence += 1

    def touch(self) -> None:
        with self._lock:
            self._sequence += 1

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def set_persistence_error(self, error: str | None) -> None:
        error = error[:SUMMARY_LIMIT] if error else None
        with self._lock:
            if self.persistence_error != error:
                self.persistence_error = error
                self._sequence += 1

    def set_scope(self, trace_id: str, scope_id: str, journal: str | None = None) -> None:
        def update(node):
            node.execution_scope_id = scope_id
            node.journal = journal
            node.status = "running"

        self._update(trace_id, update)

    def record_tokens(self, trace_id: str, input_tokens: int, output_tokens: int) -> None:
        """Accumulate token usage reported by the model adapter."""

        def update(node):
            node.input_tokens += input_tokens or 0
            node.output_tokens += output_tokens or 0

        self._update(trace_id, update)

    def record_tool_call(self, trace_id: str) -> None:
        """Increment the tool-call counter for a sub-agent."""
        self._update(trace_id, lambda node: setattr(node, "tool_call_count", node.tool_call_count + 1))

    def activity(self, trace_id: str, activity_id: str, value: Activity | None) -> None:
        def update(node):
            activities = tuple(item for item in node.activities if item.id != activity_id)
            if value is None and len(activities) == len(node.activities):
                return False
            node.activities = (*activities, value) if value else activities

        self._update(trace_id, update, after_exit=value is None and activity_id.startswith("background:"))

    def finish_tool(self, trace_id: str, call_id: str, tool: str, status: str, output: str, duration_ms: int) -> None:
        def update(node):
            activity = next((item for item in node.activities if item.id == call_id), None)
            preview = ToolPreview(
                call_id,
                tool,
                activity.summary if activity else tool,
                status,
                output[:500],
                duration_ms,
                activity.input_preview if activity else "",
            )
            node.activities = tuple(item for item in node.activities if item.id != call_id)
            node.recent_tools = (*node.recent_tools[-7:], preview)

        self._update(trace_id, update)

    def record_report(self, trace_id: str, report: AgentReport) -> None:
        self._update(trace_id, lambda node: setattr(node, "reports", (*node.reports, report)))

    def rollback_report(self, trace_id: str, report: AgentReport) -> None:
        self._update(trace_id, lambda node: setattr(node, "reports", tuple(r for r in node.reports if r != report)))

    def complete(
        self, trace_id: str, status: str = "completed", reason: str | None = None, *, result_summary: str = ""
    ) -> None:
        """Mark a sub-agent invocation as finished."""

        def update(node):
            node.mark_complete(status)
            node.activities = tuple(item for item in node.activities if item.kind == "background")
            node.end_reason = reason[:SUMMARY_LIMIT] if reason else None
            node.result_summary = result_summary[:SUMMARY_LIMIT]

        self._update(trace_id, update)

    def _snapshot(self) -> tuple[int, dict[str, TraceNode], bool]:
        with self._lock:
            return self._sequence, {key: replace(node) for key, node in self._nodes.items()}, self._has_archive

    def snapshot(self, *, include_history=True) -> tuple[int, list[TraceNode]]:
        sequence, nodes, archived = self._snapshot()
        history = (
            [
                _decode_node({"session_id": self.session_id, **record})
                for record in self._read_archive()
                if record["trace_id"] not in nodes
            ]
            if include_history and archived
            else []
        )
        return sequence, [*history, *nodes.values()]

    def has_records(self) -> bool:
        with self._lock:
            return bool(self._nodes)

    @property
    def has_history(self) -> bool:
        with self._lock:
            return self._has_archive

    def serialize(self) -> dict:
        sequence, nodes, archived = self._snapshot()
        history = [record for record in self._read_archive() if record["trace_id"] not in nodes] if archived else []
        return {
            "version": TRACE_VERSION,
            "sequence": sequence,
            "records": [*history, *(asdict(node) for node in nodes.values())],
        }

    def retain_recent(self, committed: list[dict]) -> None:
        """Release old settled records only after their session snapshot is on disk."""
        if self._read_archive is None:
            return
        with self._lock:
            settled = [
                record["trace_id"]
                for record in committed
                if record.get("ended_at") is not None and not record.get("activities")
            ]
            for trace_id in settled[:-RECENT_TRACE_LIMIT]:
                self._nodes.pop(trace_id, None)
                self._has_archive = True

    def restore(self, data: dict) -> None:
        if not data:
            return
        sequence, records = data["sequence"], data["records"]
        if (
            type(data.get("version")) is not int
            or data["version"] != TRACE_VERSION
            or type(sequence) is not int
            or sequence < 0
            or not isinstance(records, list)
        ):
            raise ValueError("Invalid Agent trace data")
        nodes = {}
        for record in records:
            node = _decode_node({"session_id": self.session_id, **record})
            if (
                node.session_id != self.session_id
                or not isinstance(node.trace_id, str)
                or not 0 < len(node.trace_id) <= 64
                or node.trace_id in nodes
                or node.status not in EXECUTION_STATES
                or not isinstance(node.task, str)
                or len(node.task) > SUMMARY_LIMIT
                or not isinstance(node.name, str)
                or len(node.name) > 160
                or not isinstance(node.result_summary, str)
                or len(node.result_summary) > SUMMARY_LIMIT
                or len(node.recent_tools) > 8
                or any(
                    len(item.output) > 500 or len(item.input_preview) > 500 or len(item.summary) > 240
                    for item in node.recent_tools
                )
                or any(len(item.summary) > 240 or len(item.input_preview) > 500 for item in node.activities)
                or any(type(value) not in {int, float} for value in (node.started_at, node.last_activity_at))
                or (node.ended_at is not None and type(node.ended_at) not in {int, float})
                or len(node.reports) > REPORT_LIMIT
                or any(
                    item.status not in REPORT_STATES
                    or not isinstance(item.summary, str)
                    or len(item.summary) > SUMMARY_LIMIT
                    for item in node.reports
                )
            ):
                raise ValueError("Invalid Agent trace record")
            if node.ended_at is None or node.activities:
                node.status = "interrupted"
                node.end_reason = "Host restarted before execution or background work settled."
                node.ended_at = node.ended_at or node.last_activity_at
                node.activities = ()
                sequence += 1
            nodes[node.trace_id] = node
        with self._lock:
            self._nodes, self._sequence = nodes, sequence
        self.retain_recent(records)

    def get(self, trace_id: str) -> TraceNode | None:
        with self._lock:
            node = self._nodes.get(trace_id)
            if node is not None:
                return replace(node)
            archived = self._has_archive
        if archived:
            return next(
                (
                    _decode_node({"session_id": self.session_id, **item})
                    for item in self._read_archive()
                    if item["trace_id"] == trace_id
                ),
                None,
            )
        return None

    def all_nodes(self) -> list[TraceNode]:
        return self.snapshot()[1]

    def get_total(self, trace_id: str | None = None) -> dict[str, int]:
        """Aggregate token usage for ``trace_id`` and all its descendants.

        When ``trace_id`` is ``None`` the session root is used so the total
        reflects every sub-agent that ran.  The starting node's own tokens are
        included in the sum.
        """
        nodes = self.all_nodes()

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
            f"[trace: in={totals['input_tokens']} out={totals['output_tokens']} tool_calls={totals['tool_call_count']}]"
        )


def _decode_node(record: dict) -> TraceNode:
    values = dict(record)
    values["activities"] = tuple(Activity(**item) for item in values.get("activities", ()))
    values["reports"] = tuple(AgentReport(**item) for item in values.get("reports", ()))
    values["recent_tools"] = tuple(ToolPreview(**item) for item in values.get("recent_tools", ()))
    return TraceNode(**values)


__all__ = ["TraceManager", "TraceNode"]
