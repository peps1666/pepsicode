"""Recover the budget for one parent task from its provider request journal."""

from __future__ import annotations

import time
from pathlib import Path

from pepsicode.core.task_control import edit_control
from pepsicode.execution.runtime import ExecutionJournal, execution_journal_dir
from pepsicode.llm.cost_tracker import CostTracker


def bind_budget(session, scope, *, recovery=False):
    task_id = (session.task_control.get("task") or {}).get("id")
    if not task_id:
        raise ValueError("Managed Workers require a parent task")
    state = session.task_control.get("orchestration") or {}
    binding = state.get("budgets", {}).get(task_id)
    if binding is None:
        if recovery:
            raise ValueError("The interrupted attempt has no managed execution journal")
        deadline = None if scope.deadline is None else time.time() + scope.deadline - time.monotonic()
        binding = {
            "journal": str(scope.journal.path.resolve()),
            "max_requests": scope.budget.max_requests,
            "cost_limit": scope.budget.limit,
            "deadline": deadline,
        }
        # The durable pointer must always refer to a journal that already exists.
        scope.record("orchestration/started", user_task_id=task_id)
        with edit_control(session=session) as control:
            state = control.setdefault("orchestration", {"version": 1, "work": [], "effects": []})
            state.setdefault("budgets", {})[task_id] = binding
        return

    path = Path(binding["journal"]).resolve()
    if path.parent != execution_journal_dir(scope.cwd) or path.suffix != ".jsonl":
        raise ValueError("Saved orchestration journal does not belong to this workspace")
    if not recovery and ExecutionJournal.interrupted_calls(path):
        raise PermissionError("Reconcile the saved Worker execution before resuming")
    prepared, finished = {}, {}
    for entry in ExecutionJournal.read(path):
        if entry["event"] == "model/prepared":
            prepared[entry["request_id"]] = entry["body"]["model"]
        elif entry["event"] in {"model/settled", "model/failed"}:
            finished[entry["request_id"]] = entry
    historical = CostTracker()
    uncertain = False
    for request_id, model in prepared.items():
        entry = finished.get(request_id, {})
        usage = entry.get("usage")
        if entry.get("event") != "model/settled" or not usage:
            uncertain = True
        if usage:
            historical.add_usage(
                model,
                usage["input_tokens"],
                usage["output_tokens"],
                cache_read_tokens=usage.get("cache_read_tokens", 0),
                cache_write_tokens=usage.get("cache_write_tokens", 0),
            )
    budget = scope.budget
    budget.requests = max(budget.requests, len(prepared))
    budget.tracker.total_cost_usd = max(budget.tracker.total_cost_usd, historical.total_cost_usd)
    budget.unmetered |= uncertain
    budget.max_requests = min(budget.max_requests, binding["max_requests"])
    if binding["cost_limit"] is not None:
        budget.limit = min(budget.limit, binding["cost_limit"]) if budget.limit is not None else binding["cost_limit"]
    if binding["deadline"] is not None:
        deadline = time.monotonic() + binding["deadline"] - time.time()
        scope.deadline = min(scope.deadline, deadline) if scope.deadline is not None else deadline
    scope.journal = ExecutionJournal(path)
