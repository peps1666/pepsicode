"""Task ownership, durable execution events, cancellation and shared request budgets."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from pepsicode.cancellation import TurnCancelledError
from pepsicode.llm.cost_tracker import MODEL_PRICING, BudgetExceededError, CostTracker

_scope: ContextVar[ExecutionScope | None] = ContextVar("execution_scope", default=None)
_tool_context: ContextVar[Any] = ContextVar("tool_context", default=None)


class ModelRequestLimitError(RuntimeError):
    """The task chain exhausted its model-request allowance."""


class CombinedCancellation:
    """A local cancellation handle that also observes its parent's stop signal."""

    def __init__(self, parent=None, local=None):
        self.local = local if local is not None else threading.Event()
        self.parent = parent

    def is_set(self):
        return self.local.is_set() or (self.parent is not None and self.parent.is_set())

    def set(self):
        self.local.set()

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not self.is_set():
            remaining = deadline - time.monotonic() if deadline is not None else 0.05
            if remaining <= 0:
                return self.is_set()
            self.local.wait(min(0.05, remaining))
        return True


class ExecutionJournal:
    """Append-before-dispatch events; an unfinished operation has an unknown outcome."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def append(self, event: str, **data: Any) -> None:
        record = {"event": event, "time": time.time(), **data}
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())

    @staticmethod
    def read(path: Path) -> list[dict]:
        records = []
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # Only an unterminated final record can be a torn tail.
                    if line.endswith("\n") or stream.read():
                        raise ValueError("Execution journal contains a corrupt committed record") from None
                    break
                records.append(record)
        return records

    @classmethod
    def interrupted_calls(cls, path: Path) -> list[dict]:
        pending = {}
        finished_background = set()
        for record in cls.read(path):
            if record["event"] == "recovery/pending":
                pending.update({call["call_id"]: call for call in record["calls"]})
            elif record["event"] == "tool/started":
                pending[record["call_id"]] = record
            elif record["event"] == "tool/reconciled":
                pending.pop(record["call_id"], None)
            elif record["event"] == "background/settled" and record.get("state") == "completed":
                finished_background.add(record["call_id"])
                pending.pop(record["call_id"], None)
            elif record["event"] == "tool/settled":
                if record.get("state") == "running" and record["call_id"] in finished_background:
                    pending.pop(record["call_id"], None)
                if record.get("state") not in {"unknown", "running"} and record.get("effect_state") != "unknown":
                    pending.pop(record["call_id"], None)
        return [
            {**record, "state": "unknown", "retry": "inspect external state before retrying mutations"}
            for record in pending.values()
        ]


@dataclass
class RequestBudget:
    tracker: CostTracker
    limit: float | None = None
    max_requests: int = 200
    requests: int = 0
    unmetered: bool = False
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self):
        if self.limit is not None and (not math.isfinite(self.limit) or self.limit <= 0):
            raise ValueError("Task budget must be a positive finite USD estimate")
        if isinstance(self.max_requests, bool) or not isinstance(self.max_requests, int) or self.max_requests < 1:
            raise ValueError("modelRequestLimit must be a positive integer")

    def check(self):
        if self.limit is not None:
            if self.unmetered:
                raise RuntimeError("Model usage unavailable; cannot safely continue a budget-limited task")
            if self.tracker.total_cost_usd >= self.limit:
                raise BudgetExceededError(self.limit, self.tracker.total_cost_usd)
        if self.requests >= self.max_requests:
            raise ModelRequestLimitError("Shared model request limit reached")

    @contextmanager
    def request(self, scope: ExecutionScope, body: dict):
        if self.limit is None:
            # With no money cap there is no in-flight balance to reserve.
            # Allocate the request slot atomically, then release for transport.
            while not self._lock.acquire(timeout=0.05):
                scope.check_cancelled()
            try:
                scope.check_cancelled()
                self.check()
                self.requests += 1
            finally:
                self._lock.release()
            yield
            return
        # Serialize priced requests, including children and summaries, so no
        # sibling can spend against an unaccounted in-flight response.
        while not self._lock.acquire(timeout=0.05):
            scope.check_cancelled()
        try:
            scope.check_cancelled()
            self.check()
            if self.limit is not None:
                pricing = MODEL_PRICING.get(body["model"], MODEL_PRICING["default"])
                # Conservative preflight estimate; vendor tokenizers/prices
                # still determine the actual bill, not this local guard.
                projected = (
                    len(json.dumps(body, ensure_ascii=False).encode("utf-8")) * pricing["input"]
                    + body.get("max_tokens", 4096) * pricing["output"]
                ) / 1_000_000
                if self.tracker.total_cost_usd + projected > self.limit:
                    raise BudgetExceededError(self.limit, self.tracker.total_cost_usd + projected)
            self.requests += 1
            yield
        finally:
            self._lock.release()

    def record(self, model: str, usage: dict | None):
        with self._lock:
            self._record(model, usage)

    def _record(self, model: str, usage: dict | None):
        if not isinstance(usage, dict) or not all(
            isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool) and usage[key] >= 0
            for key in ("input_tokens", "output_tokens")
        ):
            self.unmetered = True
            return
        self.tracker.add_usage(
            model,
            usage["input_tokens"],
            usage["output_tokens"],
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
        )


@dataclass(eq=False)
class ExecutionScope:
    cwd: str
    permissions: Any = None
    cancellation_event: Any = None
    budget: RequestBudget | None = None
    journal: ExecutionJournal | None = None
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_task_id: str | None = None
    deadline: float | None = None
    state: str = "running"
    unresolved: dict = field(default_factory=dict)
    control: dict = field(default_factory=dict)
    repetitions: dict = field(default_factory=dict)
    repeat_thresholds: tuple = (3, 5, 8)
    observations: dict = field(default_factory=dict)
    snapshot_metrics: list = field(default_factory=list)
    recovery_version: list = field(default_factory=lambda: [0])
    _recovery_lock: Any = field(default_factory=threading.RLock, repr=False)

    @property
    def has_unknown(self):
        with self._recovery_lock:
            return bool(self.unresolved)

    def mark_unknown(self, call_id, **details):
        with self._recovery_lock:
            self.unresolved[call_id] = {"call_id": call_id, **details, "state": "unknown"}
            self.recovery_version[0] += 1
            self.state = "unknown"

    def reconcile(self, call_id: str, revision: int, evidence: dict, outcome: str):
        self.check_cancelled()
        with self._recovery_lock:
            if revision != self.recovery_version[0] or call_id not in self.unresolved:
                raise PermissionError("Unknown operation set changed; inspect execution_status again")
            if outcome != "unknown":
                self.record("tool/reconciled", call_id=call_id, evidence=evidence)
                self.unresolved.pop(call_id)
                self.recovery_version[0] += 1
            self.state = "unknown" if self.has_unknown else "running"

    def snapshot(self):
        return {
            "task_id": self.task_id,
            "state": "unknown" if self.has_unknown else self.state,
            "journal": str(self.journal.path) if self.journal else None,
            "recovery_revision": self.recovery_version[0],
            "usage_uncertain": bool(self.budget and self.budget.unmetered),
            "workspace_scans": {
                "calls": len(self.snapshot_metrics),
                "duration_ms": round(sum(item["duration_ms"] for item in self.snapshot_metrics), 3),
                "bytes": sum(item["bytes"] for item in self.snapshot_metrics),
                "recent": self.snapshot_metrics[-32:],
            },
        }

    def check_cancelled(self):
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            raise TurnCancelledError("Task stopped by user")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            if self.cancellation_event is not None:
                self.cancellation_event.set()
            raise TurnCancelledError("Task deadline reached")

    def record(self, event: str, **data):
        if self.journal is not None:
            self.journal.append(event, task_id=self.task_id, parent_task_id=self.parent_task_id, **data)

    def child(self, cwd=None, permissions=None, cancellation_event=None):
        return ExecutionScope(
            cwd=cwd or self.cwd,
            permissions=permissions if permissions is not None else self.permissions,
            cancellation_event=CombinedCancellation(self.cancellation_event, cancellation_event)
            if cancellation_event is not None and cancellation_event is not self.cancellation_event
            else self.cancellation_event,
            budget=self.budget,
            journal=self.journal,
            parent_task_id=self.task_id,
            deadline=self.deadline,
            unresolved=self.unresolved,
            repeat_thresholds=self.repeat_thresholds,
            observations=self.observations,
            recovery_version=self.recovery_version,
            _recovery_lock=self._recovery_lock,
        )


def current_scope() -> ExecutionScope | None:
    return _scope.get()


def current_tool_context():
    return _tool_context.get()


@contextmanager
def bind_tool_context(context):
    token = _tool_context.set(context)
    try:
        yield
    finally:
        _tool_context.reset(token)


@contextmanager
def bind_scope(scope: ExecutionScope | None):
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def _recover(scope, previous):
    if isinstance(previous, dict) and previous.get("usage_uncertain") and scope.budget:
        scope.budget.unmetered = True
    if not isinstance(previous, dict) or not previous.get("journal"):
        return
    path = Path(previous["journal"]).resolve()
    reserved = (Path(scope.cwd) / ".pepsi-code" / "executions").resolve()
    if path.parent != reserved or path.suffix != ".jsonl":
        raise ValueError("Recovery journal must belong to this workspace's execution directory")
    try:
        calls = ExecutionJournal.interrupted_calls(path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        # A missing/corrupt audit trail cannot prove that effects did not run.
        calls = [
            {
                "call_id": previous.get("task_id", "unreadable-journal"),
                "tool": "unreadable journal",
                "error": type(error).__name__,
            }
        ]
    for call in calls:
        scope.unresolved[call["call_id"]] = call
    if calls:
        scope.state = "unknown"
        scope.record("recovery/pending", calls=calls)


def scoped_turn(function):
    """Give direct CLI turns, verifier retries and child loops the same policy owner."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        from pepsicode.core.session_tree import checkpoint_execution, checkpoint_messages, previous_execution

        parent = current_scope()
        runtime = kwargs.get("runtime")
        if not isinstance(runtime, dict):
            runtime = getattr(kwargs.get("model"), "runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        owns_task = parent is None or str(kwargs.get("agent_scope", "main")).startswith("subagent")
        if parent is not None and not str(kwargs.get("agent_scope", "main")).startswith("subagent"):
            scope = parent
        elif parent is not None:
            scope = parent.child(kwargs.get("cwd"), kwargs.get("permissions"), kwargs.get("cancellation_event"))
        else:
            cwd = kwargs["cwd"]
            tracker = kwargs.get("cost_tracker") or CostTracker()
            limit = kwargs.get("cost_limit_usd", runtime.get("budgetLimitUsd"))
            scope = ExecutionScope(
                cwd,
                kwargs.get("permissions"),
                kwargs.get("cancellation_event"),
                RequestBudget(tracker, limit, runtime.get("modelRequestLimit", 200)),
                control=runtime.setdefault("_taskControl", {}),
            )
            if Path(cwd).is_dir():
                scope.journal = ExecutionJournal(Path(cwd) / ".pepsi-code" / "executions" / f"{scope.task_id}.jsonl")
            thresholds = runtime.get("repeatToolThresholds", [3, 5, 8])
            if (
                not isinstance(thresholds, (list, tuple))
                or not thresholds
                or any(isinstance(n, bool) or not isinstance(n, int) or n < 2 for n in thresholds)
                or list(thresholds) != sorted(set(thresholds))
            ):
                raise ValueError("repeatToolThresholds must be increasing unique integers >= 2")
            scope.repeat_thresholds = tuple(thresholds)
            _recover(scope, previous_execution(runtime))
            timeout = runtime.get("taskTimeout")
            if timeout is not None:
                if (
                    isinstance(timeout, bool)
                    or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout)
                    or timeout <= 0
                ):
                    raise ValueError("taskTimeout must be a positive finite duration")
                scope.deadline = time.monotonic() + timeout
        with bind_scope(scope):
            if kwargs.get("input_queue") is not None:
                scope.input_queue = kwargs["input_queue"]
            if scope.cancellation_event is None:
                scope.cancellation_event = threading.Event()
            if kwargs.get("cancellation_event") is None:
                kwargs["cancellation_event"] = scope.cancellation_event
            if kwargs.get("cost_tracker") is None and scope.budget:
                kwargs["cost_tracker"] = scope.budget.tracker
            scope.check_cancelled()
            if owns_task:
                scope.record("task/started")
            if parent is None:
                if scope.has_unknown:
                    kwargs["messages"] = [
                        *kwargs.get("messages", []),
                        {
                            "role": "user",
                            "internal": True,
                            "content": "Harness recovery: previous operations have unknown outcomes. "
                            "Do not replay mutations. Use execution_status to inspect pending calls, "
                            "read the affected state, then reconcile with evidence and approval. "
                            "Further writes, processes and external mutations are blocked until reconciliation.",
                        },
                    ]
                runtime["_lastExecution"] = scope.snapshot()
                checkpoint_messages(kwargs.get("messages", []))
                checkpoint_execution(scope.snapshot())
            timer = None
            if owns_task and scope.deadline is not None:
                timer = threading.Timer(max(0, scope.deadline - time.monotonic()), scope.cancellation_event.set)
                timer.daemon = True
                timer.start()
            try:
                result = function(*args, **kwargs)
                last = result[-1] if isinstance(result, list) and result else {}
                if owns_task:
                    scope.state = (
                        "unknown"
                        if scope.has_unknown
                        else "cancelled"
                        if scope.cancellation_event.is_set()
                        else (
                            "awaiting_user"
                            if last.get("awaitingUser")
                            else "failed"
                            if last.get("isError")
                            else "succeeded"
                        )
                    )
                    scope.record("task/settled", state=scope.state)
                if owns_task and scope.has_unknown and isinstance(result, list):
                    notice = (
                        "Execution has unresolved outcomes. Inspect the affected state before retrying; "
                        "use execution_status to reconcile with evidence and approval."
                    )
                    result.append({"role": "assistant", "content": notice, "isError": True})
                    callback = kwargs.get("on_error_message")
                    if callback:
                        callback(notice)
                return result
            except BaseException as error:
                if owns_task:
                    scope.state = (
                        "unknown"
                        if scope.has_unknown
                        else "cancelled"
                        if isinstance(error, (TurnCancelledError, KeyboardInterrupt))
                        else "failed"
                    )
                    scope.record("task/settled", state=scope.state, error_type=type(error).__name__)
                raise
            finally:
                if timer is not None:
                    timer.cancel()
                if parent is None:
                    runtime["_lastExecution"] = scope.snapshot()
                    checkpoint_execution(scope.snapshot())

    return wrapped
