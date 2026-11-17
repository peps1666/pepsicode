"""M4 delivery-loop regression tests (checklist section 20, B13/B15/B17).

Drives candidate recording, candidate integration (serial cherry-picks with
head-bound acceptance) and the bounded repair loop against real temporary Git
repositories, following the M2 environment test fixture pattern.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from pepsicode import config
from pepsicode.core import session as sessions
from pepsicode.core.session_tree import recording_session
from pepsicode.core.task_control import begin_task
from pepsicode.execution.runtime import (
    ExecutionJournal,
    ExecutionScope,
    execution_journal_dir,
)
from pepsicode.orchestration.candidate import record_candidate, validate_candidate
from pepsicode.orchestration.environment import prepare_environment
from pepsicode.orchestration.integration import (
    integrate_candidates,
    prune_integration,
    run_integration_acceptance,
    validate_integration,
)
from pepsicode.orchestration.store import admit_work, claim_launch, retry_work, settle_launch
from pepsicode.permissions import PermissionManager


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=15
    ).stdout.strip()


def commit_all(checkout, message):
    git(checkout, "-c", "user.name=Worker", "-c", "user.email=worker@example.invalid", "add", "-A")
    git(checkout, "-c", "user.name=Worker", "-c", "user.email=worker@example.invalid", "commit", "-m", message)


def settle(data, attempt_id, outcome="result_ready", summary="done"):
    assert claim_launch(data, generation="gen-1", eligible=lambda item: item["attempt_id"] == attempt_id)
    assert settle_launch(data, attempt_id=attempt_id, generation="gen-1", outcome=outcome, summary=summary)


def make_project(tmp_path, monkeypatch, edits):
    """One prepared environment per edit; each worker commits and delivers."""
    git(tmp_path, "init")
    (tmp_path / "target.txt").write_text("committed", encoding="utf-8")
    git(tmp_path, "add", "target.txt")
    git(
        tmp_path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Fixture baseline",
    )
    sha = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "target.txt").write_text("dirty main", encoding="utf-8")
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", config.PEPSI_CODE_DIR)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", config.PEPSI_CODE_DIR / "sessions")
    data = sessions.create_new_session(str(tmp_path))
    with recording_session(data):
        task = begin_task([{"role": "user", "content": "Implement a change"}], {})
    permissions = PermissionManager(str(tmp_path), mode="full_access")
    scope = ExecutionScope(
        str(tmp_path), permissions=permissions, journal=ExecutionJournal(execution_journal_dir(tmp_path) / "m4.jsonl")
    )
    manifests = []
    try:
        for key, edit in edits.items():
            item = admit_work(
                data,
                task_id=task["id"],
                revision=1,
                work_key=key,
                specification={"agent_type": "general", "goal": "Update the project"},
            )
            manifest = prepare_environment(data.session_id, item["work_id"], scope=scope, base_sha=sha)
            # Preparation writes through its own session copy (M2 host boundary);
            # reload so later edit_control calls on this snapshot don't clobber it.
            data = sessions.load_session(data.session_id)
            checkout = Path(manifest["worktree"])
            for filename, content in edit.items():
                (checkout / filename).write_text(content, encoding="utf-8")
            commit_all(checkout, f"Worker {key}")
            settle(data, item["attempt_id"])
            manifests.append({"work_id": item["work_id"], "attempt_id": item["attempt_id"], "manifest": manifest})
        yield data, scope, sha, manifests
    finally:
        permissions.close()


def checkout_of(data, work_id):
    return Path(data.workspace) / ".pepsi-code" / "m2" / work_id / "checkout"


@pytest.fixture
def compatible(tmp_path, monkeypatch):
    """Two candidates that touch different files (integrable without conflict)."""
    yield from make_project(
        tmp_path,
        monkeypatch,
        {"first": {"target.txt": "one"}, "second": {"feature.txt": "feature-two"}},
    )


@pytest.fixture
def conflicting(tmp_path, monkeypatch):
    """Two candidates that edit the same line of the same file (B15)."""
    yield from make_project(
        tmp_path,
        monkeypatch,
        {"first": {"target.txt": "one"}, "second": {"target.txt": "two"}},
    )


def test_candidate_records_committed_head_and_detects_change(compatible):
    data, scope, sha, manifests = compatible
    work_id = manifests[0]["work_id"]
    record = record_candidate(data, scope, work_id)
    assert record["state"] == "recorded"
    assert record["base_sha"] == sha
    assert record["head_sha"] != sha
    assert record["branch"] == manifests[0]["manifest"]["branch"]
    assert record["changed_files"] == ["target.txt"]

    # A further commit in the same worktree invalidates the recorded head (B17).
    recorded_head = record["head_sha"]
    checkout = checkout_of(data, work_id)
    (checkout / "target.txt").write_text("one\nextra", encoding="utf-8")
    commit_all(checkout, "extra change")
    stale = validate_candidate(data, scope, work_id)
    assert stale["state"] == "stale" and stale["head_sha"] == recorded_head


def test_integration_binds_acceptance_to_integration_head(compatible, monkeypatch):
    data, scope, sha, manifests = compatible
    work_ids = [manifest["work_id"] for manifest in manifests]
    for work_id in work_ids:
        record_candidate(data, scope, work_id)
    runtime = {
        "verificationCommands": [{"name": "smoke", "command": [sys.executable, "-c", "print('ok')"], "timeout": 30}]
    }
    monkeypatch.setattr(data, "runtime", runtime, raising=False)

    record = integrate_candidates(data, scope, work_ids)
    assert record["state"] == "integrated"
    assert record["base_sha"] == sha
    assert set(record["candidate_heads"]) == set(work_ids)
    assert record["head_sha"] != sha

    record = run_integration_acceptance(data, scope, record["integration_id"])
    assert record["state"] == "accepted"
    acceptance = record["acceptance"]
    assert acceptance["valid"] is True
    assert acceptance["head_sha"] == record["head_sha"]
    assert acceptance["task_revision"] == record["revision"]
    assert [item["exit_code"] for item in acceptance["commands"]] == [0]
    command_digest = acceptance["command_digest"]

    # A later candidate-side change marks the whole record stale: the old
    # acceptance can never stand in for new code (B17).
    late_checkout = checkout_of(data, work_ids[0])
    (late_checkout / "target.txt").write_text("one\nlate change", encoding="utf-8")
    commit_all(late_checkout, "late change")
    stale = validate_integration(data, scope, record["integration_id"])
    assert stale["state"] == "stale"
    assert stale["acceptance"]["valid"] is False
    assert stale["acceptance"]["command_digest"] == command_digest

    # Delivering a stale integration is refused.
    from pepsicode.orchestration.external_delivery import deliver_integration

    with pytest.raises(ValueError, match="accepted"):
        deliver_integration(data, scope, record["integration_id"])


def test_acceptance_failure_blocks_delivery(compatible, monkeypatch):
    data, scope, sha, manifests = compatible
    work_ids = [manifest["work_id"] for manifest in manifests]
    for work_id in work_ids:
        record_candidate(data, scope, work_id)
    runtime = {"verificationCommands": [{"name": "broken", "command": [sys.executable, "-c", "raise SystemExit(3)"]}]}
    monkeypatch.setattr(data, "runtime", runtime, raising=False)
    record = integrate_candidates(data, scope, work_ids)
    record = run_integration_acceptance(data, scope, record["integration_id"])
    assert record["state"] == "acceptance_failed"
    assert record["acceptance"]["valid"] is False
    assert record["acceptance"]["commands"][0]["exit_code"] == 3

    from pepsicode.orchestration.external_delivery import deliver_integration

    with pytest.raises(ValueError, match="accepted"):
        deliver_integration(data, scope, record["integration_id"])


def test_conflicting_candidates_keep_both_sides(conflicting):
    data, scope, sha, manifests = conflicting
    work_ids = [manifest["work_id"] for manifest in manifests]
    for work_id in work_ids:
        record_candidate(data, scope, work_id)
    record = integrate_candidates(data, scope, work_ids)
    assert record["state"] == "conflict"
    assert len(record["conflicts"]) == 1
    assert record["conflicts"][0]["work_id"] == work_ids[1]
    assert "target.txt" in record["conflicts"][0]["files"]
    # Neither candidate worktree was touched; the main workspace is untouched.
    assert (Path(manifests[0]["manifest"]["worktree"]) / "target.txt").read_text(encoding="utf-8") == "one"
    assert (Path(manifests[1]["manifest"]["worktree"]) / "target.txt").read_text(encoding="utf-8") == "two"
    assert (Path(data.workspace) / "target.txt").read_text(encoding="utf-8") == "dirty main"


def test_integration_failure_marks_unknown_and_blocks_replay(compatible, monkeypatch):
    from pepsicode.orchestration import integration as integration_module

    data, scope, sha, manifests = compatible
    work_ids = [manifest["work_id"] for manifest in manifests]
    for work_id in work_ids:
        record_candidate(data, scope, work_id)
    real_run_git = integration_module.run_git
    picks = {"count": 0}

    def losing_pick(context, cwd, *arguments, **kwargs):
        if "cherry-pick" in arguments:
            picks["count"] += 1
            if picks["count"] == 2:
                context.effect_state = "completed"
                raise RuntimeError("Git cherry-pick failed (1): simulated loss")
        return real_run_git(context, cwd, *arguments, **kwargs)

    monkeypatch.setattr(integration_module, "run_git", losing_pick)
    with pytest.raises(RuntimeError, match="simulated loss"):
        integrate_candidates(data, scope, work_ids)
    assert scope.has_unknown, "a lost cherry-pick must surface as an unknown effect"

    # With an unreconciled unknown effect, a new integration is refused: no
    # blind replay of work whose outcome is not known.
    with pytest.raises(PermissionError, match="Reconcile"):
        integrate_candidates(data, scope, work_ids)


def test_repair_budget_fingerprint_and_classes(tmp_path, monkeypatch):
    git(tmp_path, "init")
    (tmp_path / "target.txt").write_text("committed", encoding="utf-8")
    git(tmp_path, "add", "target.txt")
    git(
        tmp_path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Fixture baseline",
    )
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", config.PEPSI_CODE_DIR)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", config.PEPSI_CODE_DIR / "sessions")
    data = sessions.create_new_session(str(tmp_path))
    with recording_session(data):
        task = begin_task([{"role": "user", "content": "Implement a change"}], {})
    permissions = PermissionManager(str(tmp_path), mode="full_access")
    scope = ExecutionScope(
        str(tmp_path), permissions=permissions, journal=ExecutionJournal(execution_journal_dir(tmp_path) / "m4r.jsonl")
    )
    try:
        item = admit_work(
            data,
            task_id=task["id"],
            revision=1,
            work_key="repair-me",
            specification={"agent_type": "general", "goal": "Update the project"},
        )
        settle(data, item["attempt_id"], outcome="failed", summary="pytest failed: 1 test")
        repaired = retry_work(
            data, task_id=task["id"], revision=1, attempt_id=item["attempt_id"], max_repair_attempts=2
        )
        assert repaired["repair_of"] == item["attempt_id"]
        assert repaired["continuation"], "a repair retry carries the failure evidence"
        assert data.task_control["orchestration"]["repairs"][repaired["work_id"]]["count"] == 1

        # Same failure fingerprint again: no progress, no repair (B13).
        settle(data, repaired["attempt_id"], outcome="failed", summary="pytest failed: 1 test")
        with pytest.raises(ValueError, match="no progress"):
            retry_work(data, task_id=task["id"], revision=1, attempt_id=repaired["attempt_id"], max_repair_attempts=2)

        # A different failure still consumes budget, but the limit holds (B13).
        second = admit_work(
            data,
            task_id=task["id"],
            revision=1,
            work_key="repair-limit",
            specification={"agent_type": "general", "goal": "Update the project"},
        )
        settle(data, second["attempt_id"], outcome="failed", summary="no module named yaml")
        first_fix = retry_work(
            data, task_id=task["id"], revision=1, attempt_id=second["attempt_id"], max_repair_attempts=1
        )
        assert first_fix["repair_of"] == second["attempt_id"]
        settle(data, first_fix["attempt_id"], outcome="failed", summary="a different code error now")
        with pytest.raises(ValueError, match="repair limit"):
            retry_work(data, task_id=task["id"], revision=1, attempt_id=first_fix["attempt_id"], max_repair_attempts=1)

        # Non-repairable failure classes never enter the repair loop.
        guarded = admit_work(
            data,
            task_id=task["id"],
            revision=1,
            work_key="guarded",
            specification={"agent_type": "general", "goal": "Update the project"},
        )
        settle(data, guarded["attempt_id"], outcome="failed", summary="approval denied by policy")
        with pytest.raises(ValueError, match="not repairable"):
            retry_work(data, task_id=task["id"], revision=1, attempt_id=guarded["attempt_id"], max_repair_attempts=2)
    finally:
        permissions.close()


def test_prune_removes_only_reviewed_integrations(compatible, monkeypatch):
    data, scope, sha, manifests = compatible
    monkeypatch.setattr(
        data,
        "runtime",
        {"verificationCommands": [{"name": "smoke", "command": [sys.executable, "-c", "print('ok')"], "timeout": 30}]},
        raising=False,
    )
    work_ids = [manifest["work_id"] for manifest in manifests]

    # An accepted integration can be pruned after delivery review (B20).
    record = run_integration_acceptance(data, scope, integrate_candidates(data, scope, work_ids)["integration_id"])
    checkout = Path(record["worktree"])
    assert checkout.is_dir()
    pruned = prune_integration(data, scope, record["integration_id"])
    assert pruned["state"] == "pruned"
    assert not checkout.exists()
    assert not Path(data.workspace).joinpath(".pepsi-code", "m4", "integrations", record["integration_id"]).exists()
    assert validate_integration(data, scope, record["integration_id"])["state"] == "pruned"
    # The version evidence stays in the pruned record.
    assert pruned["acceptance"]["valid"] is True
    assert pruned["head_sha"] == record["head_sha"]

    # An integrated-but-not-accepted integration is not a reviewed artifact.
    pending = integrate_candidates(data, scope, work_ids)
    assert pending["state"] == "integrated"
    with pytest.raises(ValueError, match="pruned"):
        prune_integration(data, scope, pending["integration_id"])
    assert Path(pending["worktree"]).is_dir(), "the unaccepted integration worktree must survive"


def test_schema_validates_m4_sections(tmp_path, monkeypatch):
    from pepsicode.orchestration.schema import validate_orchestration

    work_id = uuid.uuid4().hex
    attempt_id = uuid.uuid4().hex
    state = {
        "version": 1,
        "work": [
            {
                "work_id": work_id,
                "attempt_id": attempt_id,
                "trace_id": uuid.uuid4().hex,
                "task_id": "task-1",
                "work_key": "k",
                "digest": "d",
                "revision": 1,
                "specification": {"agent_type": "general", "goal": "g"},
                "state": "queued",
            }
        ],
        "effects": [{"effect_id": uuid.uuid4().hex, "kind": "launch", "attempt_id": attempt_id, "state": "pending"}],
        "candidates": {},
        "integrations": {},
        "deliveries": {},
        "repairs": {},
    }
    validate_orchestration(state)

    # A candidate record for an unknown work item is rejected.
    state["candidates"]["unknown-id"] = {
        "attempt_id": attempt_id,
        "base_sha": "0" * 40,
        "head_sha": "1" * 40,
        "branch": "codex/m2-x",
        "worktree": "w",
        "revision": 1,
        "state": "recorded",
    }
    with pytest.raises(ValueError, match="candidate"):
        validate_orchestration(state)

    # A well-formed candidate, integration, delivery and repair record passes.
    del state["candidates"]["unknown-id"]
    state["candidates"][work_id] = {
        "attempt_id": attempt_id,
        "base_sha": "0" * 40,
        "head_sha": "1" * 40,
        "branch": "codex/m2-x",
        "worktree": "w",
        "revision": 1,
        "state": "recorded",
    }
    state["integrations"]["int-1"] = {
        "integration_id": "int-1",
        "task_id": "task-1",
        "revision": 1,
        "base_sha": "0" * 40,
        "branch": "codex/integrate-int-1",
        "worktree": "w",
        "candidates": [work_id],
        "candidate_heads": {work_id: "1" * 40},
        "conflicts": [],
        "state": "integrated",
    }
    state["deliveries"]["del-1"] = {
        "delivery_id": "del-1",
        "integration_id": "int-1",
        "kind": "push",
        "status": "delivered",
        "attempts": [],
        "external_refs": {},
    }
    state["repairs"][work_id] = {"count": 1, "last_fingerprint": "ab" * 8}
    validate_orchestration(state)

    state["deliveries"]["del-1"]["status"] = "magical"
    with pytest.raises(ValueError, match="delivery"):
        validate_orchestration(state)


def test_coordinator_uses_configured_acceptance(compatible):
    data, scope, _, manifests = compatible
    work_ids = [item["work_id"] for item in manifests]
    # Supply config as the interactive host does, without grafting it onto SessionData.
    runtime = {"verificationCommands": [{"name": "probe", "command": [sys.executable, "-c", "print('ok')"]}]}
    from threading import Event

    from pepsicode.execution.runtime import RequestBudget, bind_scope
    from pepsicode.llm.cost_tracker import CostTracker
    from pepsicode.orchestration.interactive import coordinator_runner
    from pepsicode.orchestration.runtime import own_workspace
    from pepsicode.tooling import ToolContext, ToolRegistry

    scope.cancellation_event = Event()
    scope.budget = RequestBudget(CostTracker())
    checked = []

    def runner(**options):
        if not checked:
            checked.append(
                options["tools"].execute(
                    "task_integrate", {"work_ids": work_ids}, ToolContext(data.workspace, execution=scope)
                )
            )
        return options["messages"]

    run = coordinator_runner(data, runtime, runner)
    with own_workspace(data.workspace), recording_session(data), bind_scope(scope):
        run(tools=ToolRegistry([]), model=None, messages=data.messages, cwd=data.workspace)
    result = checked[0]
    assert result.details["state"] == "accepted", result.details


def integrate_with_check(data, scope, manifests, command):
    record = integrate_candidates(data, scope, [item["work_id"] for item in manifests])
    data.runtime = {"verificationCommands": [{"name": "probe", "command": [sys.executable, "-c", command]}]}
    return record


def test_acceptance_matches_committed_content(compatible):
    data, scope, _, manifests = compatible
    record = integrate_with_check(
        data, scope, manifests, "from pathlib import Path; assert Path('target.txt').read_text() == 'dirty'"
    )
    (Path(record["worktree"]) / "target.txt").write_text("dirty", encoding="utf-8")
    accepted = run_integration_acceptance(data, scope, record["integration_id"])
    assert accepted["state"] != "accepted", accepted["acceptance"]
