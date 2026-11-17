"""Candidate integration with head-bound acceptance (M4 §20.2).

Integration runs in a dedicated worktree created from the candidates' common
base SHA. Each candidate is cherry-picked serially under the host-controlled
Git entry; a conflict aborts the pick and keeps both sides of the change
(B15 — never force-drop one side, never reset). Post-merge acceptance commands
run through the execution layer and are bound to the exact integration HEAD:
if the HEAD moves during acceptance the evidence is void, and a later change
of any candidate worktree or the integration checkout marks the whole record
``stale`` so old evidence can never accept new code (B17).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from pepsicode.cancellation import TurnCancelledError
from pepsicode.core.session_tree import checkpoint_execution, recording_session
from pepsicode.core.task_control import edit_control
from pepsicode.core.verification_discovery import discover_checks
from pepsicode.execution import process
from pepsicode.orchestration.candidate import record_candidate, validate_candidate
from pepsicode.orchestration.git_ops import run_git
from pepsicode.orchestration.runtime import own_workspace
from pepsicode.tooling import ToolContext

_INTEGRATION_ROOT = "m4/integrations"
_DEFAULT_COMMAND_TIMEOUT = 120


def _task(session):
    task = session.task_control.get("task") or {}
    if task.get("status") != "active" or not isinstance(task.get("id"), str) or type(task.get("revision")) is not int:
        raise ValueError("No active parent task for candidate integration")
    return task


def _save_integration(session, record):
    with edit_control(session=session) as control:
        control.setdefault("orchestration", {}).setdefault("integrations", {})[record["integration_id"]] = dict(record)


def _journalled(scope, context, call_id, tool, action):
    """Run one mutating step with the M2 commit/unknown journal pattern."""
    scope.record("tool/started", call_id=call_id, tool=tool, mutating=True)
    checkpoint_execution(scope.snapshot())
    context.effect_state = "none"
    try:
        result = action()
    except Exception:
        unknown = context.effect_state in {"started", "completed", "unknown"}
        if unknown:
            scope.mark_unknown(call_id, tool=tool)
        scope.record(
            "tool/settled",
            call_id=call_id,
            state="unknown" if unknown else "failed",
            effect_state="unknown" if unknown else "none",
        )
        raise
    scope.record("tool/settled", call_id=call_id, state="succeeded", effect_state="committed")
    return result


def _head(context, checkout):
    branch = run_git(context, checkout, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    head = run_git(context, checkout, "rev-parse", "--verify", "HEAD").stdout.strip()
    return branch, head


def _clean(context, checkout):
    return not run_git(context, checkout, "status", "--porcelain", "--untracked-files=all").stdout.strip()


def acceptance_command_digest(commands):
    return hashlib.sha256(json.dumps(commands, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _conflicted_files(context, checkout):
    output = run_git(context, checkout, "diff", "--name-only", "--diff-filter=U").stdout
    return [line.strip() for line in output.splitlines() if line.strip()][:32]


def integrate_candidates(session, scope, work_ids, *, workspace_owned=False):
    """Record candidates, build an integration worktree and pick them serially."""
    if scope is None or scope.permissions is None or scope.journal is None:
        raise ValueError("Integration requires execution permissions and a journal")
    if scope.has_unknown:
        raise PermissionError("Reconcile the earlier execution before integrating candidates")
    if not isinstance(work_ids, list) or not work_ids or any(not isinstance(item, str) for item in work_ids):
        raise ValueError("Specify the work item IDs to integrate")
    if len(set(work_ids)) != len(work_ids):
        raise ValueError("Duplicate work item IDs")
    task = _task(session)
    records = []
    for work_id in work_ids:
        # Record (or re-record) the candidate here: the delivered head is bound
        # at integration time, and a later worktree change makes it stale (B17).
        record = record_candidate(session, scope, work_id)
        if record["revision"] != task["revision"]:
            raise ValueError(f"Candidate {work_id} belongs to another task revision")
        records.append(record)
    base_shas = {record["base_sha"] for record in records}
    if len(base_shas) != 1:
        raise ValueError("Candidates do not share a common base SHA")
    base_sha = base_shas.pop()

    repo = Path(session.workspace).resolve()
    integration_id = uuid.uuid4().hex[:12]
    branch = f"codex/integrate-{integration_id}"
    root = repo / ".pepsi-code" / _INTEGRATION_ROOT / integration_id
    checkout = root / "checkout"
    manifest = {
        "integration_id": integration_id,
        "task_id": task["id"],
        "revision": task["revision"],
        "base_sha": base_sha,
        "branch": branch,
        "worktree": str(checkout),
        "candidates": list(work_ids),
        "candidate_heads": {record["work_id"]: record["head_sha"] for record in records},
        "conflicts": [],
        "acceptance": None,
        "state": "preparing",
        "created_at": time.time(),
    }
    _save_integration(session, manifest)
    context = ToolContext(str(repo), permissions=scope.permissions, execution=scope)
    common = Path(
        run_git(context, repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    ).resolve()
    with nullcontext() if workspace_owned else own_workspace(repo), own_workspace(common), recording_session(session):
        try:
            _journalled(
                scope,
                context,
                f"m4-integrate:{integration_id}:create",
                "m4_create_integration",
                lambda: run_git(context, repo, "worktree", "add", "--no-track", "-b", branch, str(checkout), base_sha),
            )
            manifest["state"] = "integrating"
            _save_integration(session, manifest)
            for work_id in work_ids:
                head = manifest["candidate_heads"][work_id]
                commits = run_git(
                    context, repo, "rev-list", "--reverse", "--first-parent", f"{base_sha}..{head}"
                ).stdout.splitlines()
                if (
                    not commits
                    or run_git(context, repo, "merge-base", "--is-ancestor", base_sha, head, allowed=(0, 1)).returncode
                ):
                    raise ValueError(f"Candidate {work_id} is not based on the integration baseline")
                call_id = f"m4-integrate:{integration_id}:pick:{work_id}"

                def pick(commits=commits):
                    # The sanitized git environment has no user identity; the
                    # pick creates commits and needs one. A conflict exit (1)
                    # with unmerged files is a deterministic, known outcome —
                    # resolve it inline; anything else stays an unknown effect.
                    result = run_git(
                        context,
                        checkout,
                        "-c",
                        "user.name=PepsiCode Integration",
                        "-c",
                        "user.email=integration@pepsicode.invalid",
                        "cherry-pick",
                        "--allow-empty",
                        *commits,
                        allowed=(0, 1),
                    )
                    if result.returncode == 0:
                        return "picked"
                    files = _conflicted_files(context, checkout)
                    if not files:
                        raise RuntimeError(
                            f"Git cherry-pick failed ({result.returncode}): {result.stderr.strip()[:400]}"
                        )
                    # Keep both sides of the change: abort the pick instead of
                    # forcing one candidate over the other (B15).
                    run_git(context, checkout, "cherry-pick", "--abort")
                    return ("conflict", files)

                outcome = _journalled(scope, context, call_id, "m4_cherry_pick_candidate", pick)
                if outcome != "picked":
                    manifest["conflicts"].append({"work_id": work_id, "files": outcome[1]})
                    manifest["state"] = "conflict"
                    _save_integration(session, manifest)
                    return manifest
            manifest["head_sha"] = _head(context, checkout)[1]
            manifest["state"] = "integrated"
            _save_integration(session, manifest)
        except Exception as error:
            manifest["state"] = "integration_failed"
            manifest["error"] = str(error)[:1000]
            _save_integration(session, manifest)
            raise
    return manifest


def validate_integration(session, scope, integration_id):
    """Revalidate the integration checkout and every candidate head (B17)."""
    state = session.task_control.get("orchestration") or {}
    record = (state.get("integrations") or {}).get(integration_id)
    if record is None:
        raise ValueError(f"No integration record {integration_id}")
    if record.get("state") in ("stale", "pruned", "conflict", "integration_failed"):
        return record
    context = ToolContext(str(session.workspace), permissions=scope.permissions, execution=scope)
    stale_reason = None
    checkout = Path(record["worktree"])
    task = session.task_control.get("task") or {}
    if (record["task_id"], record["revision"]) != (task.get("id"), task.get("revision")):
        stale_reason = "parent task or revision changed"
    elif not checkout.is_dir():
        stale_reason = "integration worktree is missing"
    else:
        branch, head = _head(context, checkout)
        if (branch, head) != (record["branch"], record.get("head_sha", "")):
            stale_reason = "integration checkout branch or HEAD changed"
        elif record["state"] == "accepted" and not _clean(context, checkout):
            stale_reason = "integration checkout has uncommitted changes"
    if stale_reason is None:
        for work_id, expected in record["candidate_heads"].items():
            candidate = validate_candidate(session, scope, work_id)
            if candidate.get("state") == "stale" or candidate.get("head_sha") != expected:
                stale_reason = f"candidate {work_id} changed after integration"
                break
    if stale_reason is not None:
        record = {
            **record,
            "state": "stale",
            "stale_reason": stale_reason,
            "acceptance": {**(record.get("acceptance") or {}), "valid": False},
        }
        _save_integration(session, record)
    return record


def run_integration_acceptance(session, scope, integration_id, *, runtime=None):
    """Run the configured acceptance commands bound to the integration HEAD."""
    settings = runtime if runtime is not None else getattr(session, "runtime", None) or {}
    commands = settings.get("verificationCommands") or []
    record = validate_integration(session, scope, integration_id)
    if record["state"] != "integrated":
        raise ValueError(f"Integration {integration_id} is not ready for acceptance (state={record['state']})")
    if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
        raise ValueError("verificationCommands must be a list of command objects")
    repo = Path(session.workspace).resolve()
    checkout = Path(record["worktree"])
    context = ToolContext(str(repo), permissions=scope.permissions, execution=scope)
    head_before = _head(context, checkout)[1]
    clean_before = _clean(context, checkout)
    results = []
    for index, check in enumerate(discover_checks(str(checkout), commands) if clean_before else []):
        argv = check.argv
        started = time.monotonic()
        call_id = f"m4-accept:{record['integration_id']}:{index}"
        try:
            result = _journalled(
                scope,
                context,
                call_id,
                "m4_acceptance_command",
                lambda: process.run(
                    argv,
                    context=context,
                    cwd=check.cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=check.timeout or _DEFAULT_COMMAND_TIMEOUT,
                    env={**os.environ, **check.env},
                ),
            )
            exit_code, output = result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            exit_code, output = None, "acceptance command timed out"
        except TurnCancelledError:
            raise
        results.append(
            {
                "name": check.name,
                "cwd": str(Path(check.cwd).relative_to(checkout)),
                "command": argv,
                "exit_code": exit_code,
                "output_sha256": hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()[:16],
                "output_excerpt": output[:2000],
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        )
    head_after = _head(context, checkout)[1]
    clean_after = _clean(context, checkout)
    task = session.task_control.get("task") or {}
    valid = (
        bool(results)
        and all(item["exit_code"] == 0 for item in results)
        and head_before == head_after == record["head_sha"]
        and clean_after
        and (task.get("id"), task.get("revision")) == (record["task_id"], record["revision"])
    )
    acceptance = {
        "valid": valid,
        "head_sha": head_before,
        "head_changed_during_acceptance": head_after != head_before,
        "worktree_clean": clean_before and clean_after,
        "task_revision": record["revision"],
        "commands": results,
        "command_digest": acceptance_command_digest(commands),
        "run_at": time.time(),
    }
    record = {
        **record,
        "acceptance": acceptance,
        "state": "accepted" if valid else "acceptance_failed",
    }
    _save_integration(session, record)
    return record


def prune_integration(session, scope, integration_id):
    """Remove an accepted/stale integration worktree only after explicit review (B20)."""
    state = session.task_control.get("orchestration") or {}
    record = (state.get("integrations") or {}).get(integration_id)
    if record is None:
        raise ValueError(f"No integration record {integration_id}")
    acceptance = record.get("acceptance") or {}
    if record.get("state") not in {"stale", "conflict", "integration_failed"} and not acceptance.get("valid"):
        raise ValueError("Only accepted, conflicted or stale integrations can be pruned")
    repo = Path(session.workspace).resolve()
    context = ToolContext(str(repo), permissions=scope.permissions, execution=scope)
    common = Path(
        run_git(context, repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    ).resolve()
    with own_workspace(repo), own_workspace(common):
        checkout = Path(record["worktree"])
        _journalled(
            scope,
            context,
            f"m4-prune:{integration_id}",
            "m4_prune_integration",
            lambda: run_git(context, repo, "worktree", "remove", str(checkout)),
        )
        root = repo / ".pepsi-code" / _INTEGRATION_ROOT / integration_id
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
    record = {**record, "state": "pruned", "pruned_at": time.time()}
    _save_integration(session, record)
    return record
