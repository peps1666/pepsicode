"""Validation for durable M1 state before a session is installed."""

from __future__ import annotations

import math

_WORK_STATES = {
    "queued",
    "starting",
    "stop_requested",
    "waiting_user",
    "result_ready",
    "blocked",
    "failed",
    "cancelled",
    "stale",
    "recovery_pending",
}


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 128


def _rows(state, key):
    rows = state.get(key, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"Invalid persisted orchestration {key}")
    return rows


def validate_orchestration(state):
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
        raise ValueError("Unsupported orchestration state")
    if not {"work", "effects"} <= state.keys():
        raise ValueError("Missing orchestration work or launch effects")
    runs = state.get("runs", {})
    if not isinstance(runs, dict) or any(
        not _identity(task_id)
        or not isinstance(run, dict)
        or not _identity(run.get("run_id"))
        or not _identity(run.get("session_id"))
        or run.get("mode") != "interactive"
        for task_id, run in runs.items()
    ):
        raise ValueError("Invalid persisted orchestration run")
    attempts, traces = {}, set()
    for row in _rows(state, "work"):
        fields = ("work_id", "attempt_id", "trace_id", "task_id", "work_key", "digest")
        if (
            not all(_identity(row.get(field)) for field in fields)
            or type(row.get("revision")) is not int
            or row["revision"] < 1
            or not isinstance(row.get("specification"), dict)
            or any(
                name in row["specification"] and not isinstance(row["specification"][name], str)
                for name in ("agent_type", "goal")
            )
            or not isinstance(row.get("state"), str)
            or row.get("state") not in _WORK_STATES
            or row["attempt_id"] in attempts
            or row["trace_id"] in traces
            or ("run_id" in row and row["run_id"] != runs.get(row["task_id"], {}).get("run_id"))
        ):
            raise ValueError("Invalid persisted Worker attempt")
        attempts[row["attempt_id"]] = row
        traces.add(row["trace_id"])
    effect_ids, claimed_attempts = set(), set()
    for row in _rows(state, "effects"):
        if (
            not _identity(row.get("effect_id"))
            or not _identity(row.get("attempt_id"))
            or row["attempt_id"] not in attempts
            or row["attempt_id"] in claimed_attempts
            or row["effect_id"] in effect_ids
            or row.get("kind") != "launch"
            or not isinstance(row.get("state"), str)
            or row.get("state") not in {"pending", "claimed", "settled", "recovery_pending"}
            or (row["state"] == "claimed" and not _identity(row.get("generation")))
        ):
            raise ValueError("Invalid persisted launch effect")
        effect_ids.add(row["effect_id"])
        claimed_attempts.add(row["attempt_id"])
    if claimed_attempts != attempts.keys():
        raise ValueError("An orchestration attempt is missing its launch effect")
    environments = state.get("environments", {})
    work_ids = {row["work_id"] for row in attempts.values()}
    if not isinstance(environments, dict) or any(
        work_id not in work_ids
        or not isinstance(record, dict)
        or record.get("state") not in ("prepare_pending", "preparing", "prepared", "prepare_failed", "pruned")
        or record.get("isolation_requested") not in ("workspace", "hardened")
        or record.get("isolation_effective")
        != (record["isolation_requested"] if record["state"] == "prepared" else None)
        or any(
            not isinstance(record.get(field), str)
            for field in ("repository", "base_sha", "expected_head", "branch", "worktree")
        )
        or not isinstance(record.get("directories"), dict)
        for work_id, record in environments.items()
    ):
        raise ValueError("Invalid persisted managed environment")
    receipts = state.get("dispatch_receipts", {})
    if not isinstance(receipts, dict) or any(
        not isinstance(key, str) or not _identity(attempt_id) or attempt_id not in attempts
        for key, attempt_id in receipts.items()
    ):
        raise ValueError("Invalid persisted dispatch receipt")
    event_ids = set()
    for row in _rows(state, "events"):
        if (
            not _identity(row.get("event_id"))
            or not _identity(row.get("attempt_id"))
            or row["attempt_id"] not in attempts
            or row["event_id"] not in effect_ids
            or row["event_id"] in event_ids
            or not isinstance(row.get("state"), str)
            or row.get("state") not in {"pending", "delivered", "stale"}
        ):
            raise ValueError("Invalid persisted orchestration event")
        attempt = attempts[row["attempt_id"]]
        if (row.get("task_id"), row.get("revision")) != (attempt["task_id"], attempt["revision"]):
            raise ValueError("Orchestration event belongs to another task revision")
        event_ids.add(row["event_id"])
    budgets = state.get("budgets", {})
    if not isinstance(budgets, dict):
        raise ValueError("Invalid persisted orchestration budgets")
    for task_id, budget in budgets.items():
        if (
            not _identity(task_id)
            or not isinstance(budget, dict)
            or not isinstance(budget.get("journal"), str)
            or type(budget.get("max_requests")) is not int
            or budget["max_requests"] < 1
            or not {"cost_limit", "deadline"} <= budget.keys()
            or any(
                value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value <= 0)
                for value in (budget["cost_limit"], budget["deadline"])
            )
        ):
            raise ValueError("Invalid persisted orchestration budget")
    _validate_candidates(state, work_ids)
    _validate_repairs(state, work_ids)
    _validate_integrations(state)
    _validate_deliveries(state)


def _validate_candidates(state, work_ids):
    candidates = state.get("candidates")
    if candidates is None:
        return
    if not isinstance(candidates, dict) or any(
        work_id not in work_ids
        or not isinstance(record, dict)
        or record.get("state") not in ("recorded", "stale")
        or type(record.get("revision")) is not int
        or record["revision"] < 1
        or any(
            not isinstance(record.get(field), str) or not record.get(field)
            for field in ("attempt_id", "base_sha", "head_sha", "branch", "worktree")
        )
        for work_id, record in candidates.items()
    ):
        raise ValueError("Invalid persisted candidate record")


def _validate_repairs(state, work_ids):
    repairs = state.get("repairs")
    if repairs is None:
        return
    if not isinstance(repairs, dict) or any(
        work_id not in work_ids
        or not isinstance(record, dict)
        or type(record.get("count")) is not int
        or record["count"] < 0
        or record.get("last_fingerprint") is not None
        and not isinstance(record["last_fingerprint"], str)
        for work_id, record in repairs.items()
    ):
        raise ValueError("Invalid persisted repair record")


def _validate_integrations(state):
    integrations = state.get("integrations")
    if integrations is None:
        return
    if not isinstance(integrations, dict) or any(
        not _identity(integration_id)
        or not isinstance(record, dict)
        or record.get("state")
        not in (
            "preparing",
            "integrating",
            "integrated",
            "conflict",
            "accepted",
            "acceptance_failed",
            "integration_failed",
            "stale",
            "pruned",
        )
        or any(
            not isinstance(record.get(field), str) or not record.get(field)
            for field in ("task_id", "base_sha", "branch", "worktree")
        )
        or type(record.get("revision")) is not int
        or record["revision"] < 1
        or not isinstance(record.get("candidates"), list)
        or not isinstance(record.get("candidate_heads"), dict)
        or not isinstance(record.get("conflicts"), list)
        for integration_id, record in integrations.items()
    ):
        raise ValueError("Invalid persisted integration record")


def _validate_deliveries(state):
    deliveries = state.get("deliveries")
    if deliveries is None:
        return
    if not isinstance(deliveries, dict) or any(
        not _identity(delivery_id)
        or not isinstance(record, dict)
        or record.get("kind") not in ("local", "push", "pull_request")
        or record.get("status") not in ("pending", "local", "delivered", "uncertain", "failed")
        or not isinstance(record.get("attempts"), list)
        or not isinstance(record.get("external_refs"), dict)
        or not _identity(record.get("integration_id"))
        for delivery_id, record in deliveries.items()
    ):
        raise ValueError("Invalid persisted delivery record")
