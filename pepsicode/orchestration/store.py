"""Atomic work, launch effect and result transitions in a session snapshot.

The caller must hold the runtime's exclusive session ownership. The session
lock only serializes writers using the same in-process session object.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid

from pepsicode.core.task_control import edit_control
from pepsicode.orchestration.repair import (
    REPAIRABLE_CLASSES,
    check_repair_allowed,
    classify_failure,
    record_repair,
)


def _check_capacity(state, task_id, revision, limit):
    queued = sum(
        item["state"] == "queued" and (item["task_id"], item["revision"]) == (task_id, revision)
        for item in state["work"]
    )
    if queued >= limit:
        raise ValueError("The work queue is full")


def claim_activation(session, limit=20):
    """Bound automatic coordinator turns for one parent task revision."""
    if type(limit) is not int or limit < 1:
        raise ValueError("Invalid coordinator activation limit")
    with edit_control(session=session) as control:
        task = control.get("task") or {}
        state = control.setdefault("orchestration", {"version": 1, "work": [], "effects": []})
        counts = state.setdefault("activations", {})
        key = f"{task.get('id')}:{task.get('revision')}"
        if counts.get(key, 0) >= limit:
            return False
        counts[key] = counts.get(key, 0) + 1
        return True


def _append_attempt(state, item):
    item.update(attempt_id=uuid.uuid4().hex, trace_id=uuid.uuid4().hex, state="queued", queued_at=time.time())
    state["work"].append(item)
    state["effects"].append(
        {"effect_id": uuid.uuid4().hex, "kind": "launch", "attempt_id": item["attempt_id"], "state": "pending"}
    )
    return copy.deepcopy(item)


def _run_identity(state, session, task_id):
    runs = state.setdefault("runs", {})
    if task_id not in runs:
        runs[task_id] = {"run_id": uuid.uuid4().hex, "session_id": session.session_id, "mode": "interactive"}
    return runs[task_id]["run_id"]


def admit_work(
    session,
    *,
    task_id: str,
    revision: int,
    work_key: str,
    specification: dict,
    limit: int = 32,
    active_limit: int = 32,
    receipt_key=None,
):
    """Persist a queued work item and launch effect together, or reject it.

    A caller must supply a stable key and an explicit task revision.  An
    accepted item is only a durable intent, never evidence of execution.
    """
    if not isinstance(work_key, str) or not 0 < len(work_key.strip()) <= 128 or not isinstance(specification, dict):
        raise ValueError("A work key and specification are required")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("Invalid task revision")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Invalid queue limit")
    if isinstance(active_limit, bool) or not isinstance(active_limit, int) or active_limit < 1:
        raise ValueError("Invalid active work limit")
    encoded = json.dumps(specification, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with edit_control(session=session) as control:
        task = control.get("task") or {}
        if task.get("id") != task_id or task.get("revision") != revision or task.get("status") != "active":
            raise ValueError("The parent task is not active at the requested revision")
        state = control.setdefault("orchestration", {"version": 1, "work": [], "effects": []})
        if state.get("version") != 1:
            raise ValueError("Unsupported orchestration state")
        run_id = _run_identity(state, session, task_id)
        receipts = state.setdefault("dispatch_receipts", {}) if receipt_key else {}
        for item in reversed(state["work"]):
            if item["task_id"] == task_id and item["work_key"] == work_key:
                if item["digest"] != digest or item["revision"] != revision:
                    raise ValueError("Work key conflicts with an existing dispatch")
                item.setdefault("run_id", run_id)
                if receipt_key:
                    if receipts.get(receipt_key, item["attempt_id"]) != item["attempt_id"]:
                        raise ValueError("Dispatch receipt already belongs to another attempt")
                    receipts[receipt_key] = item["attempt_id"]
                return copy.deepcopy(item)
        if receipt_key and receipt_key in receipts:
            raise ValueError("Dispatch receipt already belongs to another attempt")
        _check_capacity(state, task_id, revision, limit)
        active = {
            item["work_id"]
            for item in state["work"]
            if (item["task_id"], item["revision"]) == (task_id, revision)
            and item["state"] in {"queued", "starting", "stop_requested", "waiting_user", "result_ready"}
        }
        if len(active) >= active_limit:
            raise ValueError("The current revision has reached its active work limit")
        item = {
            "work_id": uuid.uuid4().hex,
            "run_id": run_id,
            "task_id": task_id,
            "revision": revision,
            "work_key": work_key,
            "digest": digest,
            "specification": json.loads(encoded),
        }
        accepted = _append_attempt(state, item)
        if receipt_key:
            receipts[receipt_key] = accepted["attempt_id"]
        return accepted


def claim_launch(session, *, generation: str, eligible=lambda item: True):
    """Reserve the next current launch before handing it to an executor."""
    with edit_control(session=session) as control:
        state = control.get("orchestration")
        if state is None or any(effect["state"] == "recovery_pending" for effect in state["effects"]):
            return None
        task = control.get("task") or {}
        if task.get("status") != "active":
            return None
        by_attempt = {item["attempt_id"]: item for item in state["work"]}
        for effect in state["effects"]:
            if effect["state"] != "pending":
                continue
            item = by_attempt[effect["attempt_id"]]
            if (item["task_id"], item["revision"]) != (task.get("id"), task.get("revision")):
                item["state"] = "stale"
                effect["state"] = "settled"
                continue
            if not eligible(item):
                return None
            effect.update(state="claimed", generation=generation)
            item["state"] = "starting"
            return copy.deepcopy(item)
        return None


def settle_launch(session, *, attempt_id: str, generation: str, outcome: str, summary: str):
    """Commit one terminal result and its pending coordinator event together."""
    if outcome not in {"result_ready", "waiting_user", "failed", "blocked", "cancelled"}:
        raise ValueError("Invalid attempt outcome")
    with edit_control(session=session) as control:
        state = control.get("orchestration") or {}
        for effect in state.get("effects", []):
            if effect["attempt_id"] != attempt_id or effect["state"] != "claimed":
                continue
            if effect["generation"] != generation:
                return False
            item = next(item for item in state["work"] if item["attempt_id"] == attempt_id)
            task = control.get("task") or {}
            current = (item["task_id"], item["revision"]) == (task.get("id"), task.get("revision"))
            item.update(
                state=outcome if current else "stale", outcome=outcome, summary=summary[:4000], settled_at=time.time()
            )
            effect["state"] = "settled"
            if current and task.get("status") in {"active", "waiting_user"}:
                state.setdefault("events", []).append(
                    {
                        "event_id": effect["effect_id"],
                        "attempt_id": attempt_id,
                        "task_id": item["task_id"],
                        "revision": item["revision"],
                        "kind": outcome,
                        "state": "pending",
                    }
                )
            return True
        return False


def recover_launches(session, *, generation: str):
    """Fence abandoned claims without requeueing potentially started work."""
    with edit_control(session=session) as control:
        state = control.get("orchestration")
        if state is None:
            return []
        by_attempt = {item["attempt_id"]: item for item in state["work"]}
        interrupted = []
        for effect in state["effects"]:
            if effect["state"] == "claimed" and effect["generation"] != generation:
                effect["state"] = "recovery_pending"
                attempt_id = effect["attempt_id"]
                by_attempt[attempt_id]["state"] = "recovery_pending"
                interrupted.append(attempt_id)
        return interrupted


def cancel_queued(session):
    """Cancel durable intents for the current task that have not been claimed."""
    with edit_control(session=session) as control:
        state = control.get("orchestration") or {}
        task_id = (control.get("task") or {}).get("id")
        cancelled = set()
        for item in state.get("work", []):
            if item["task_id"] == task_id and item["state"] == "queued":
                item.update(state="cancelled", outcome="cancelled", settled_at=time.time())
                cancelled.add(item["attempt_id"])
        for effect in state.get("effects", []):
            if effect["attempt_id"] in cancelled and effect["state"] == "pending":
                effect["state"] = "settled"


def abandon_interrupted(session, task_id):
    """Close confirmed interrupted claims without requeueing any attempt."""
    with edit_control(session=session) as control:
        state = control.get("orchestration") or {}
        attempts = {item["attempt_id"]: item for item in state.get("work", []) if item["task_id"] == task_id}
        for effect in state.get("effects", []):
            item = attempts.get(effect["attempt_id"])
            if item is not None and effect["state"] == "recovery_pending":
                item.update(
                    state="failed",
                    outcome="interrupted",
                    summary="Interrupted attempt was not replayed.",
                    settled_at=time.time(),
                )
                effect["state"] = "settled"


def request_stop(session, *, task_id, revision, attempt_id):
    """Commit a targeted stop before the scheduler signals its live Worker."""
    with edit_control(session=session) as control:
        task = control.get("task") or {}
        if (task.get("id"), task.get("revision")) != (task_id, revision):
            raise ValueError("The parent task revision changed")
        state = control.get("orchestration") or {}
        item = next((row for row in state.get("work", []) if row["attempt_id"] == attempt_id), None)
        if item is None or (item["task_id"], item["revision"]) != (task_id, revision):
            raise ValueError("No such attempt for the current revision")
        if item["state"] == "queued":
            effect = next(row for row in state["effects"] if row["attempt_id"] == attempt_id)
            item.update(state="cancelled", outcome="cancelled", settled_at=time.time())
            effect["state"] = "settled"
        elif item["state"] == "starting":
            item["state"] = "stop_requested"
        elif item["state"] not in {"stop_requested", "cancelled"}:
            raise ValueError("This attempt is no longer stoppable")
        return item["state"]


def retry_work(
    session,
    *,
    task_id,
    revision,
    attempt_id,
    max_repair_attempts=2,
    max_auto_retries=1,
    queue_limit=32,
    active_limit=32,
):
    """Admit one replacement for a confirmed failure or answered question.

    Failed ``explore`` attempts and answered questions follow the M1 rules. A
    failed ``general`` attempt may additionally be retried as a bounded
    *repair* (M4 §20.1): repairable failure class, repair budget left, and a
    failure fingerprint that actually changed since the last repair (B13).
    """
    if type(max_auto_retries) is not int or max_auto_retries < 0:
        raise ValueError("Invalid automatic retry limit")
    if type(queue_limit) is not int or queue_limit < 1:
        raise ValueError("Invalid queue limit")
    if type(active_limit) is not int or active_limit < 1:
        raise ValueError("Invalid active work limit")
    with edit_control(session=session) as control:
        task = control.get("task") or {}
        if (task.get("id"), task.get("revision"), task.get("status")) != (task_id, revision, "active"):
            raise ValueError("The parent task is not active at the requested revision")
        state = control.get("orchestration") or {}
        if any(effect["state"] == "recovery_pending" for effect in state.get("effects", [])):
            raise ValueError("Reconcile the previous execution before retrying")
        old = next((row for row in state.get("work", []) if row["attempt_id"] == attempt_id), None)
        if old is None or old["task_id"] != task_id:
            raise ValueError("No such attempt for the current task")
        lineage = [row for row in state["work"] if row["work_id"] == old["work_id"]]
        latest = lineage[-1]
        if latest.get("retry_of") == attempt_id and latest["revision"] == revision:
            return copy.deepcopy(latest)
        if latest is not old:
            raise ValueError("This attempt has been replaced or exhausted its retry limit")
        continuation = []
        repair_fingerprint = None
        questions = [
            q for q in control.get("questions", []) if q.get("trace_id") == old["trace_id"] and q.get("required")
        ]
        if old["state"] == "waiting_user" or (old["state"] == "stale" and questions):
            if len(lineage) >= 3:
                raise ValueError("This attempt has exhausted its retry limit")
            if not questions or any(q["status"] != "answered" for q in questions):
                raise ValueError("The Worker still needs a directed answer")
            continuation = [{"question": q["question"][:400], "answer": q["answer"][:1600]} for q in questions[-3:]]
        elif old["state"] == "failed" and old.get("outcome") == "failed" and old["revision"] == revision:
            if old["specification"].get("agent_type") == "explore":
                automatic = sum(
                    1
                    for row in lineage
                    if row.get("retry_of") and not row.get("repair_of") and row.get("outcome") == "failed"
                )
                if automatic >= max_auto_retries:
                    raise ValueError("This attempt has exhausted its automatic retry limit")
            elif old["specification"].get("agent_type") == "general":
                failure_class = classify_failure(old)
                if failure_class not in REPAIRABLE_CLASSES:
                    raise ValueError(
                        f"A {failure_class} failure is not repairable automatically; a person decides the next step"
                    )
                repair_fingerprint = check_repair_allowed(state, old["work_id"], old, max_attempts=max_repair_attempts)
                continuation = [
                    {
                        "question": "Previous attempt failed; repair within the original assignment",
                        "answer": str(old.get("error") or old.get("summary") or "unspecified failure")[:1600],
                    }
                ]
            else:
                raise ValueError("This attempt cannot be retried automatically")
        else:
            raise ValueError("This attempt cannot be retried automatically")
        _check_capacity(state, task_id, revision, queue_limit)
        active = {
            row["work_id"]
            for row in state["work"]
            if (row["task_id"], row["revision"]) == (task_id, revision)
            and row["state"] in {"queued", "starting", "stop_requested", "waiting_user", "result_ready"}
        }
        if old["work_id"] not in active and len(active) >= active_limit:
            raise ValueError("The current revision has reached its active work limit")
        item = {key: copy.deepcopy(old[key]) for key in ("work_id", "task_id", "work_key", "digest", "specification")}
        item.update(revision=revision, retry_of=attempt_id, continuation=continuation, previous_summary=old["summary"])
        if repair_fingerprint:
            item["repair_of"] = attempt_id
            record_repair(state, old["work_id"], repair_fingerprint)
        item["run_id"] = _run_identity(state, session, task_id)
        return _append_attempt(state, item)


def note_work(session, *, task_id, revision, attempt_id, note):
    """Attach bounded context before an attempt is claimed."""
    with edit_control(session=session) as control:
        task = control.get("task") or {}
        if (task.get("id"), task.get("revision"), task.get("status")) != (task_id, revision, "active"):
            raise ValueError("The parent task is not active at the requested revision")
        work = (control.get("orchestration") or {}).get("work", [])
        item = next((row for row in work if row["attempt_id"] == attempt_id), None)
        if item is None or (item["task_id"], item["revision"], item["state"]) != (task_id, revision, "queued"):
            raise ValueError("Only current queued attempts accept context notes")
        notes = item.setdefault("notes", [])
        if note not in notes:
            if len(notes) >= 4:
                raise ValueError("This attempt has reached the note limit")
            notes.append(note)
        return len(notes)
