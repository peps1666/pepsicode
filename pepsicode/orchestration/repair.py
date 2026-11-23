"""Bounded repair for failed implementation attempts (M4 §20.1, B13).

A failed ``general`` attempt may be retried as a *repair* when the failure
looks like code or environment trouble, the work item has repair budget left
(``maxRepairAttempts``, counted per work item across its lineage) and the
failure fingerprint changed since the last repair — retrying the same failure
is no progress, not repair. Every other failure class (transport, permission,
budget, unknown side effects) is for a human or coordinator decision, never
for an automatic repair loop.
"""

from __future__ import annotations

import hashlib
import json

REPAIRABLE_CLASSES = ("code", "environment")
_KNOWN_CLASSES = frozenset((*REPAIRABLE_CLASSES, "permission", "budget", "transport", "unknown"))

_ENV_MARKERS = ("no module named", "command not found", "pip", "npm err", "dependency", "environment", "venv")
_PERMISSION_MARKERS = ("permission", "denied", "unauthorized", "approval", "policy")
_BUDGET_MARKERS = ("budget", "exhausted", "rate limit", "quota", "deadline")
_TRANSPORT_MARKERS = ("timeout", "connection", "network", "unreachable", "stream", "transport")


def settled_evidence(attempt) -> dict | None:
    """Structured settlement evidence recorded by the Worker executor (if any).

    Failed attempts settle with a JSON evidence prefix (stage, failure class,
    failing check, output excerpt, journal reference) derived from host-observed
    facts. Attempts settled before that format existed (or with a plain text
    summary) return ``None`` and fall back to the text scan.
    """
    summary = str(attempt.get("summary") or "")
    if not summary.startswith("{"):
        return None
    try:
        data = json.loads(summary.split("\n", 1)[0])
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("class"), str) and data["class"] in _KNOWN_CLASSES:
        return data
    return None


def classify_failure(attempt) -> str:
    """Classify one failed attempt from its persisted outcome text."""
    evidence = settled_evidence(attempt)
    if evidence is not None:
        return str(evidence["class"])
    text = str(attempt.get("summary") or attempt.get("error") or "").lower()
    if not text:
        return "unknown"
    if any(marker in text for marker in _PERMISSION_MARKERS):
        return "permission"
    if any(marker in text for marker in _BUDGET_MARKERS):
        return "budget"
    if any(marker in text for marker in _TRANSPORT_MARKERS):
        return "transport"
    if any(marker in text for marker in _ENV_MARKERS):
        return "environment"
    return "code"


def failure_fingerprint(attempt) -> str:
    """Stable digest of the failure evidence; log-only wording changes don't count.

    Structured settlement evidence fingerprints the deterministic facts only
    (stage, class, failing check) so trace IDs, timing and output wording never
    reset the no-progress counter.
    """
    evidence = settled_evidence(attempt)
    if evidence is not None:
        stable = {key: evidence[key] for key in ("stage", "class", "check") if evidence.get(key) is not None}
        payload = repr({"work_key": attempt.get("work_key"), **stable}).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]
    evidence = {key: attempt.get(key) for key in ("work_key", "outcome", "summary") if attempt.get(key) is not None}
    payload = repr(evidence).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def repair_state(state, work_id):
    """Return the persisted repair record for one work item (may be empty)."""
    repairs = state.get("repairs") or {}
    return repairs.get(work_id) or {"count": 0, "last_fingerprint": None}


def check_repair_allowed(state, work_id, attempt, *, max_attempts):
    """Raise unless this failure may consume one unit of repair budget."""
    record = repair_state(state, work_id)
    if record["count"] >= max_attempts:
        raise ValueError(
            f"This work item has reached the repair limit ({max_attempts} attempt(s)); a person decides the next step"
        )
    fingerprint = failure_fingerprint(attempt)
    if record["last_fingerprint"] == fingerprint:
        raise ValueError("The last repair produced the same failure (no progress); a person decides the next step")
    return fingerprint


def record_repair(state, work_id, fingerprint):
    """Persist one consumed repair unit inside an orchestration snapshot."""
    repairs = state.setdefault("repairs", {})
    record = repairs.get(work_id) or {"count": 0, "last_fingerprint": None}
    repairs[work_id] = {"count": record["count"] + 1, "last_fingerprint": fingerprint}
