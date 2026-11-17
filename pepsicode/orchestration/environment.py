"""Prepare and validate M2 worktrees under a session's execution authority."""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from pepsicode.core.session import load_session
from pepsicode.core.session_tree import checkpoint_execution, recording_session
from pepsicode.core.task_control import edit_control
from pepsicode.execution import process
from pepsicode.execution.filesystem import FILES
from pepsicode.execution.runtime import bind_scope, bind_tool_context, execution_journal_dir, recover_execution_calls
from pepsicode.orchestration.git_ops import run_git
from pepsicode.orchestration.runtime import own_session, own_workspace
from pepsicode.sandbox.bubblewrap import BubblewrapBackend
from pepsicode.sandbox.types import SandboxMode, SandboxPolicy
from pepsicode.tooling import ToolContext

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_WORK_ID = re.compile(r"[0-9a-f]{32}\Z")
_ENV_DIRS = ("dependencies", "build", "tmp", "test-data")
_LOCKFILES = (
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "Cargo.lock",
)
_PORT_BASE = 40000
_PORT_SPAN = 10
_PORT_SLOTS = 2000


def own_dependency_cache(repo):
    """Serialize shared dependency-cache access across managed Worker environments."""
    lock_dir = Path(repo).resolve() / ".pepsi-code" / "m2"
    FILES.mkdir(lock_dir)
    return own_workspace(lock_dir / "cache.lock")


def _lockfile_digests(context, repo, base_sha):
    result = _git(context, repo, "ls-tree", base_sha, "--", *_LOCKFILES, allowed=(0, 128))
    if result.returncode != 0:
        return {}
    digests = {}
    for line in result.stdout.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if path and len(parts) >= 3:
            digests[path.strip()] = parts[2].strip()
    return digests


def _allocate_port_range(session, work_id):
    environments = (session.task_control.get("orchestration") or {}).get("environments") or {}
    existing = environments.get(work_id)
    if isinstance(existing, dict) and isinstance(existing.get("port_range"), dict):
        return dict(existing["port_range"])
    used = {
        env["port_range"].get("start")
        for wid, env in environments.items()
        if wid != work_id
        and isinstance(env, dict)
        and env.get("state") != "pruned"
        and isinstance(env.get("port_range"), dict)
    }
    slot = int(work_id[:8], 16) % _PORT_SLOTS
    for offset in range(_PORT_SLOTS):
        start = _PORT_BASE + ((slot + offset) % _PORT_SLOTS) * _PORT_SPAN
        if start not in used:
            return {"start": start, "end": start + _PORT_SPAN - 1}
    raise ValueError("No free managed Worker port range available")


def _resource_limits(isolation):
    return {
        "wall_time_seconds": {"limit": 60, "enforcement": "enforced"},
        "ipc_frame_bytes": {"limit": 65536, "enforcement": "enforced"},
        "ipc_messages": {"limit": 256, "enforcement": "enforced"},
        "process_tree": {"limit": "owned_group_or_job", "enforcement": "enforced"},
        "tmpfs_bytes": {
            "limit": 67108864 if isolation == "hardened" else None,
            "enforcement": "enforced" if isolation == "hardened" else "unsupported",
        },
        "cpu": {"limit": None, "enforcement": "unsupported"},
        "memory_bytes": {"limit": None, "enforcement": "unsupported"},
        "max_processes": {"limit": None, "enforcement": "unsupported"},
        "disk_bytes": {"limit": None, "enforcement": "unsupported"},
    }


def _literal(path):
    if path.resolve() != path or path.is_symlink():
        raise ValueError(f"Managed environment path redirects elsewhere: {path}")
    return path


def _git(context, cwd, *arguments, allowed=(0,)):
    return run_git(context, cwd, *arguments, allowed=allowed)


def _current_item(session, work_id):
    task = session.task_control.get("task") or {}
    state = session.task_control.get("orchestration") or {}
    attempt = next((row for row in reversed(state.get("work", [])) if row["work_id"] == work_id), None)
    if (
        attempt is None
        or (attempt["task_id"], attempt["revision"]) != (task.get("id"), task.get("revision"))
        or task.get("status") != "active"
        or attempt["state"] != "queued"
        or attempt["specification"].get("agent_type") != "general"
    ):
        raise ValueError("Work ID must identify a current queued implementation assignment")
    return attempt


def _save_environment(session, work_id, environment):
    with edit_control(session=session) as control:
        control["orchestration"].setdefault("environments", {})[work_id] = dict(environment)


def _verify_checkout(context, environment):
    checkout = _literal(Path(environment["worktree"]))
    top = _git(context, checkout, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top).resolve() != checkout:
        raise ValueError("Managed checkout no longer has its own Git worktree")
    common = _git(context, checkout, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    branch = _git(context, checkout, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    head = _git(context, checkout, "rev-parse", "--verify", "HEAD").stdout.strip()
    if Path(common).resolve() != Path(environment["repository"]) or branch != environment["branch"]:
        raise ValueError("Managed worktree repository or branch changed")
    updates = environment.get("head_updates") or []
    expected_current = (
        (updates[-1].get("to") if isinstance(updates[-1], dict) else None) if updates else environment["expected_head"]
    )
    if not isinstance(expected_current, str) or not _SHA.fullmatch(expected_current) or head != expected_current:
        raise ValueError("Managed worktree repository, branch or HEAD changed")
    if expected_current != environment["expected_head"] and not _continues_from(
        context, checkout, head, environment["expected_head"]
    ):
        raise ValueError("Managed worktree repository, branch or HEAD changed")
    for name in (".venv", "node_modules"):
        _literal(checkout / name)


def _continues_from(context, checkout, head, expected_head):
    """True when ``head`` sits on top of the recorded baseline (attribution check).

    Commits that continue linearly from ``expected_head`` on the managed branch
    belong to this work item's attempts. A rewritten, rewound or unrelated head
    is not attributable and must be reviewed before any Worker reuses the
    checkout.
    """
    if not _SHA.fullmatch(head) or not _SHA.fullmatch(expected_head):
        return False
    return _git(context, checkout, "merge-base", "--is-ancestor", expected_head, head, allowed=(0, 1)).returncode == 0


def attribute_managed_output(session, item, scope):
    """Record one settled attempt's committed output as managed-work provenance.

    Called after an attempt settles, while the host still owns the session.
    Only linear continuations of the recorded baseline are attributable; the
    manifest keeps the bounded attempt→head history and ``expected_head`` stays
    the prepared baseline so later candidates and integrations compare against
    the same recorded identity.
    """
    environment = session.task_control.get("orchestration", {}).get("environments", {}).get(item["work_id"])
    if not environment or environment.get("state") != "prepared":
        return None
    checkout = Path(environment["worktree"])
    context = ToolContext(str(Path(session.workspace).resolve()), permissions=scope.permissions, execution=scope)
    branch = _git(context, checkout, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    head = _git(context, checkout, "rev-parse", "--verify", "HEAD").stdout.strip()
    expected = environment["expected_head"]
    if branch != environment["branch"] or head == expected or not _SHA.fullmatch(head):
        return None
    if not _continues_from(context, checkout, head, expected):
        return None
    updated = dict(environment)
    updates = [dict(row) for row in updated.get("head_updates", [])][-7:]
    updates.append({"attempt_id": item["attempt_id"], "from": expected, "to": head, "at": time.time()})
    updated["head_updates"] = updates
    _save_environment(session, item["work_id"], updated)
    return updated


def validate_prepared_environment(session, item, scope):
    """Revalidate the saved checkout before a Worker uses it."""
    environment = session.task_control["orchestration"]["environments"][item["work_id"]]
    repo = Path(session.workspace).resolve()
    checkout = _literal(repo / ".pepsi-code" / "m2" / item["work_id"] / "checkout")
    if (
        environment["state"] != "prepared"
        or environment["revision"] != item["revision"]
        or environment["branch"] != f"codex/m2-{item['work_id']}"
        or Path(environment["worktree"]) != checkout
        or not Path(environment["repository"]).is_relative_to(repo)
    ):
        raise ValueError("The Worker environment does not match the current assignment")
    context = ToolContext(str(repo), permissions=scope.permissions, execution=scope)
    _verify_checkout(context, environment)
    runtime = _literal(checkout.parent / "runtime")
    if any(
        Path(environment["directories"].get(name, "")) != (target := _literal(runtime / name)) or not target.is_dir()
        for name in _ENV_DIRS
    ):
        raise ValueError("A Worker runtime directory is missing or changed")
    return str(checkout), dict(environment["directories"])


def prepare_environment(session_id, work_id, *, scope, base_sha, isolation="workspace"):
    """Prepare one queued work item's environment and return its durable manifest."""
    if isolation not in {"workspace", "hardened"}:
        raise ValueError("Unknown Worker isolation level")
    if isolation == "hardened" and sys.platform != "linux":
        raise ValueError("Only workspace isolation is available for managed preparation")
    if isolation == "hardened":
        BubblewrapBackend()
    if not isinstance(work_id, str) or not _WORK_ID.fullmatch(work_id):
        raise ValueError("Invalid managed work ID")
    if not isinstance(base_sha, str) or not _SHA.fullmatch(base_sha):
        raise ValueError("Provide an explicit committed base SHA")
    if scope.permissions is None or scope.journal is None:
        raise ValueError("Managed preparation requires execution permissions and a journal")
    with own_session(session_id):
        session = load_session(session_id)
        if session is None:
            raise ValueError("Could not load the orchestration session")
        with own_workspace(session.workspace):
            if recover_execution_calls(scope.cwd, session.execution_state):
                raise PermissionError("Reconcile the earlier execution before preparing a worktree")
            return prepare_owned(session, work_id, scope=scope, base_sha=base_sha, isolation=isolation)


def _repair_baseline(session):
    """Latest integration record for the current task revision, when usable.

    After a combined integration fails acceptance, its recorded head is the
    newest traceable baseline: it contains every candidate's committed output,
    so repair attempts dispatched for the same revision continue from it
    instead of restarting from the main workspace. Conflicted or unknown
    integrations have no head and never qualify (B15).
    """
    task = session.task_control.get("task") or {}
    state = session.task_control.get("orchestration") or {}
    records = [
        record
        for record in (state.get("integrations") or {}).values()
        if (record.get("task_id"), record.get("revision")) == (task.get("id"), task.get("revision"))
    ]
    if not records:
        return None
    latest = None
    for record in records:  # insertion-ordered; a later record wins a timestamp tie
        if latest is None or (record.get("created_at") or 0.0) >= (latest.get("created_at") or 0.0):
            latest = record
    if latest.get("state") != "acceptance_failed" or not latest.get("head_sha"):
        return None
    return latest


def prepare_owned(session, work_id, *, scope, isolation, base_sha=None):
    """Prepare a queued WorkItem while the caller owns the workspace and session."""
    if isolation == "hardened":
        BubblewrapBackend()
    repo = Path(session.workspace).resolve()
    if Path(scope.cwd).resolve() != repo or Path(scope.permissions.workspace_root).resolve() != repo:
        raise ValueError("Execution authority does not match the session workspace")
    if scope.permissions.is_plan_mode or scope.permissions.mode == "read_only":
        raise PermissionError("This execution mode cannot prepare a worktree")
    item = _current_item(session, work_id)
    context = ToolContext(str(repo), permissions=scope.permissions, execution=scope)
    with recording_session(session), bind_scope(scope), bind_tool_context(context):
        scope.check_cancelled()
        if scope.has_unknown:
            raise PermissionError("Reconcile the earlier execution before preparing a worktree")
        journal_path = scope.journal.path.resolve()
        if journal_path.parent != execution_journal_dir(scope.cwd) or journal_path.suffix != ".jsonl":
            raise ValueError("The preparation journal must belong to this workspace")
        top = _git(context, repo, "rev-parse", "--show-toplevel").stdout.strip()
        if Path(top).resolve() != repo:
            raise ValueError("Prepare a managed worktree from the repository root")
        common = Path(
            _git(context, repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        ).resolve()
        if common == repo or not common.is_relative_to(repo):
            raise ValueError("Managed preparation requires Git metadata inside the repository workspace")
        with own_workspace(common):
            if base_sha is None:
                saved = session.task_control["orchestration"].get("environments", {}).get(work_id)
                if saved:
                    base_sha = saved["base_sha"]
                    base_integration = saved.get("base_integration")
                else:
                    baseline = _repair_baseline(session)
                    if baseline is not None:
                        base_sha = baseline["head_sha"]
                        base_integration = baseline["integration_id"]
                    else:
                        base_sha = _git(context, repo, "rev-parse", "HEAD").stdout.strip()
                        base_integration = None
            else:
                base_integration = None
            return _prepare(session, item, context, repo, common, base_sha, isolation, base_integration)


def _prepare(session, item, context, repo, common, base_sha, isolation="workspace", base_integration=None):
    work_id = item["work_id"]
    saved = session.task_control["orchestration"].get("environments", {}).get(work_id)
    root = _literal(repo / ".pepsi-code" / "m2" / work_id)
    checkout = _literal(root / "checkout")
    support = _literal(root / "runtime")
    paths = {name: str(_literal(support / name)) for name in _ENV_DIRS}
    port_range = _allocate_port_range(session, work_id)
    cache_lock = str(_literal(repo / ".pepsi-code" / "m2") / "cache.lock")
    manifest = {
        "repository": str(common),
        "base_sha": base_sha,
        "expected_head": base_sha,
        "branch": f"codex/m2-{work_id}",
        "worktree": str(checkout),
        "directories": paths,
        "port_range": port_range,
        "lockfile_digests": _lockfile_digests(context, repo, base_sha),
        "cache_policy": {
            "mode": "isolated_per_worker",
            "shared_cache_lock": cache_lock,
            "writable_symlinks_allowed": False,
        },
        "environment_contract": {
            "runtime_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "env_keys": [
                "PATH",
                "TMP",
                "TEMP",
                "TMPDIR",
                "PIP_CACHE_DIR",
                "npm_config_cache",
                "PYTHONPYCACHEPREFIX",
                "PEPSI_WORKER_PORT_START",
                "PEPSI_WORKER_PORT_END",
                "PEPSI_TEST_DATA_DIR",
            ],
            "test_data_source": str(paths["test-data"]),
            "network_policy": "loopback_only" if isolation == "hardened" else "workspace_policy",
        },
        "resource_limits": _resource_limits(isolation),
        "revision": item["revision"],
        "isolation_requested": isolation,
        "isolation_effective": isolation,
        "base_integration": base_integration,
    }
    if saved:
        if saved["state"] == "prepare_failed" and not saved.get("effect_unknown") and not checkout.exists():
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
            saved = None
    if saved:
        if saved["state"] != "prepared":
            raise ValueError("Previous environment preparation requires review")
        if any(saved.get(key) != value for key, value in manifest.items() if key != "revision"):
            raise ValueError("Saved managed environment does not match the requested identity")
        _verify_checkout(context, saved)
        if not all(Path(path).is_dir() for path in paths.values()):
            raise ValueError("A managed runtime directory is missing")
        if saved["revision"] != item["revision"]:
            saved = {**saved, "revision": item["revision"]}
            _save_environment(session, work_id, saved)
        return saved

    if root.exists():
        raise ValueError("An unowned managed environment path already exists")
    commit = _git(context, repo, "rev-parse", "--verify", f"{base_sha}^{{commit}}").stdout.strip()
    if commit != base_sha:
        raise ValueError("Base SHA must refer to a commit")
    if _git(context, repo, "ls-tree", "--name-only", base_sha, "--", ".env", "settings.local.json").stdout.strip():
        raise ValueError("The requested baseline contains local configuration files")
    branch = manifest["branch"]
    if not _git(context, repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", allowed=(0, 1)).returncode:
        raise ValueError("Managed branch name already exists")
    manifest.update(state="prepare_pending", steps=[], isolation_effective=None)
    _save_environment(session, work_id, manifest)
    manifest["state"] = "preparing"
    _save_environment(session, work_id, manifest)
    scope = context.execution
    call_id = f"m2-prepare:{work_id}"
    scope.record("orchestration/environment_preparing", work_id=work_id)
    checkpoint_execution(scope.snapshot())
    scope.record("tool/started", call_id=call_id, tool="m2_prepare_environment", mutating=True)
    checkout_started = False
    context.effect_state = "none"
    try:
        FILES.mkdir(root)
        context.effect_state = "none"
        _git(context, repo, "worktree", "add", "--no-track", "-b", branch, str(checkout), base_sha)
        checkout_started = True
        _verify_checkout(context, manifest)
        manifest["steps"] = ["checkout"]
        _save_environment(session, work_id, manifest)
        for path in paths.values():
            FILES.mkdir(path)
        steps = ["checkout", "directories"]
        install_cmd = (item.get("specification") or {}).get("install_command")
        if install_cmd is not None:
            if (
                not isinstance(install_cmd, list)
                or not install_cmd
                or not all(isinstance(part, str) and part for part in install_cmd)
            ):
                raise ValueError("Worker install_command must be a non-empty list of strings")
            install_env = {
                name: os.environ[name]
                for name in ("PATH", "SystemRoot", "WINDIR", "SystemDrive", "PATHEXT")
                if name in os.environ
            }
            install_env.update(
                TMP=paths["tmp"],
                TEMP=paths["tmp"],
                TMPDIR=paths["tmp"],
                PIP_CACHE_DIR=str(Path(paths["dependencies"]) / "pip-cache"),
                npm_config_cache=str(Path(paths["dependencies"]) / "npm-cache"),
                PYTHONPYCACHEPREFIX=str(Path(paths["build"]) / "python-cache"),
                PEPSI_WORKER_PORT_START=str(port_range["start"]),
                PEPSI_WORKER_PORT_END=str(port_range["end"]),
                PEPSI_TEST_DATA_DIR=str(paths["test-data"]),
            )
            with own_dependency_cache(repo):
                process.run(
                    install_cmd,
                    context=context,
                    cwd=checkout,
                    env=install_env,
                    check=True,
                    capture_output=True,
                    timeout=60,
                    reason="Install dependencies inside the managed Worker environment",
                )
            steps.append("dependencies")
        if isolation == "hardened":
            sandbox = BubblewrapBackend()
            probe = sandbox.confine(
                ["/usr/bin/python3", "-I", "-c", "pass"], SandboxPolicy(SandboxMode.WORKSPACE_WRITE, str(checkout))
            )
            process.run(probe.argv, context=context, cwd=checkout, check=True, capture_output=True, timeout=10)
        manifest.update(state="prepared", steps=steps, isolation_effective=isolation)
        _save_environment(session, work_id, manifest)
    except Exception as error:
        unknown = checkout_started or context.effect_state in {"started", "completed", "unknown"}
        if unknown:
            scope.mark_unknown(call_id, tool="m2_prepare_environment", work_id=work_id)
        scope.record(
            "tool/settled",
            call_id=call_id,
            state="unknown" if unknown else "failed",
            effect_state="unknown" if unknown else "none",
        )
        if not unknown and not checkout.exists() and root.exists():
            shutil.rmtree(root, ignore_errors=True)
        manifest.update(
            state="prepare_failed",
            isolation_effective=None,
            effect_unknown=bool(unknown),
            error=str(error)[:1000],
        )
        _save_environment(session, work_id, manifest)
        raise
    scope.record("tool/settled", call_id=call_id, state="succeeded", effect_state="committed")
    return manifest


def prune_environment(session, scope, work_id, *, retention_seconds=0, now=None, owns_workspace=False):
    """Remove a settled Worker environment after retention and deliverable checks."""
    if scope is None or scope.permissions is None or scope.journal is None:
        raise ValueError("Pruning a managed environment requires execution permissions and a journal")
    state = session.task_control.get("orchestration") or {}
    if (
        scope.has_unknown
        or recover_execution_calls(scope.cwd, session.execution_state)
        or any(effect.get("state") == "recovery_pending" for effect in state.get("effects", []))
    ):
        raise PermissionError("Reconcile the earlier execution before pruning a worktree")
    environment = (state.get("environments") or {}).get(work_id)
    if environment is None:
        raise ValueError(f"No managed environment for {work_id}")
    if environment.get("state") == "pruned":
        return dict(environment)
    if environment.get("state") != "prepared":
        raise ValueError("Only prepared Worker environments can be pruned")
    repo = Path(session.workspace).resolve()
    root = _literal(repo / ".pepsi-code" / "m2" / work_id)
    checkout = _literal(root / "checkout")
    if (
        Path(environment["worktree"]) != checkout
        or environment.get("branch") != f"codex/m2-{work_id}"
        or not Path(environment["repository"]).resolve().is_relative_to(repo)
    ):
        raise ValueError("Managed environment does not belong to this workspace")
    lineage = [row for row in state.get("work", []) if row.get("work_id") == work_id]
    if not lineage or any(
        row.get("state") in {"queued", "starting", "stop_requested", "waiting_user", "recovery_pending"}
        for row in lineage
    ):
        raise ValueError("Cannot prune an active or pending Worker environment")
    latest = lineage[-1]
    if latest.get("state") in {"failed", "blocked", "recovery_pending"}:
        raise ValueError("Failed or blocked Worker environments are retained for inspection")
    current_time = time.time() if now is None else now
    if retention_seconds > 0:
        settled_at = latest.get("settled_at")
        if settled_at is None or (current_time - settled_at) < retention_seconds:
            raise ValueError("Worker environment is still within its retention period")
    context = ToolContext(str(repo), permissions=scope.permissions, execution=scope)
    _verify_checkout(context, environment)
    if _git(context, checkout, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise ValueError("Worker worktree has uncommitted or untracked changes")
    head = _git(context, checkout, "rev-parse", "--verify", "HEAD").stdout.strip()
    if head != environment["base_sha"]:
        delivered_ids = {
            record.get("integration_id")
            for record in (state.get("deliveries") or {}).values()
            if record.get("status") in {"local", "delivered"}
        }
        delivered = any(
            record.get("integration_id") in delivered_ids
            and record.get("state") in {"accepted", "pruned"}
            and (record.get("acceptance") or {}).get("valid") is True
            and (record.get("candidate_heads") or {}).get(work_id) == head
            for record in (state.get("integrations") or {}).values()
        )
        if not delivered:
            raise ValueError("Worker environment contains undelivered committed output")
    common = Path(environment["repository"]).resolve()
    with (
        nullcontext() if owns_workspace else own_workspace(repo),
        own_workspace(common),
        recording_session(session),
        bind_scope(scope),
        bind_tool_context(context),
    ):
        call_id = f"m2-prune:{work_id}"
        scope.record("tool/started", call_id=call_id, tool="m2_prune_environment", mutating=True)
        checkpoint_execution(scope.snapshot())
        context.effect_state = "none"
        try:
            _git(context, repo, "worktree", "remove", str(checkout))
            shutil.rmtree(root, ignore_errors=True)
        except Exception:
            unknown = context.effect_state in {"started", "completed", "unknown"}
            if unknown:
                scope.mark_unknown(call_id, tool="m2_prune_environment", work_id=work_id)
            scope.record(
                "tool/settled",
                call_id=call_id,
                state="unknown" if unknown else "failed",
                effect_state="unknown" if unknown else "none",
            )
            raise
        scope.record("tool/settled", call_id=call_id, state="succeeded", effect_state="committed")
        updated = {
            **environment,
            "state": "pruned",
            "isolation_effective": None,
            "pruned_at": current_time,
            "pruned_head": head,
        }
        _save_environment(session, work_id, updated)
        return updated


def prune_environments(session, scope, *, retention_seconds=0, now=None, owns_workspace=False):
    """Attempt retention-safe pruning of all Worker environments in a session."""
    state = session.task_control.get("orchestration") or {}
    pruned = []
    retained = {}
    for work_id in list(state.get("environments") or {}):
        try:
            pruned.append(
                prune_environment(
                    session,
                    scope,
                    work_id,
                    retention_seconds=retention_seconds,
                    now=now,
                    owns_workspace=owns_workspace,
                )
            )
        except ValueError as error:
            retained[work_id] = str(error)
    return {"pruned": pruned, "retained": retained}
