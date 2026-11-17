from __future__ import annotations

import io
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

from pepsicode import config
from pepsicode.core import session as sessions
from pepsicode.core.session_tree import recording_session
from pepsicode.core.task_control import begin_task
from pepsicode.execution import process
from pepsicode.execution.filesystem import FILES
from pepsicode.execution.runtime import (
    ExecutionJournal,
    ExecutionScope,
    RequestBudget,
    current_scope,
    execution_journal_dir,
)
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.orchestration.environment import (
    attribute_managed_output,
    prepare_environment,
    prune_environment,
    prune_environments,
    validate_prepared_environment,
)
from pepsicode.orchestration.runtime import run_pending
from pepsicode.orchestration.store import admit_work
from pepsicode.orchestration.worker import WorkerExecutor
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.run_command import run_command_tool
from pepsicode.tools.write_file import write_file_tool
from pepsicode.types import AgentStep


@pytest.fixture
def project(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True, timeout=15
        ).stdout.strip()

    git("init")
    (tmp_path / "target.txt").write_text("committed", encoding="utf-8")
    git("add", "target.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture baseline")
    sha = git("rev-parse", "HEAD")
    (tmp_path / "target.txt").write_text("dirty main", encoding="utf-8")
    (tmp_path / ".env").write_text("UNTRACKED_TEST_VALUE=fixture", encoding="utf-8")
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", config.PEPSI_CODE_DIR)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", config.PEPSI_CODE_DIR / "sessions")
    data = sessions.create_new_session(str(tmp_path))
    with recording_session(data):
        task = begin_task([{"role": "user", "content": "Implement a change"}], {})
    items = [
        admit_work(
            data,
            task_id=task["id"],
            revision=1,
            work_key=key,
            specification={"agent_type": "general", "goal": "Update target.txt"},
        )
        for key in ("first", "second")
    ]
    permissions = PermissionManager(str(tmp_path), mode="full_access")
    scope = ExecutionScope(
        str(tmp_path), permissions=permissions, journal=ExecutionJournal(execution_journal_dir(tmp_path) / "m2.jsonl")
    )
    try:
        yield data, scope, sha, items
    finally:
        permissions.close()


def test_worktrees_have_distinct_checkouts_and_keep_dirty_main(project):
    data, scope, sha, items = project
    manifests = [prepare_environment(data.session_id, item["work_id"], scope=scope, base_sha=sha) for item in items]
    assert len({manifest["branch"] for manifest in manifests}) == 2
    assert all(manifest["state"] == "prepared" and manifest["expected_head"] == sha for manifest in manifests)
    barrier = Barrier(2)

    def edit(manifest, content):
        checkout = Path(manifest["worktree"])
        assert (checkout / "target.txt").read_text(encoding="utf-8") == "committed"
        assert not (checkout / ".env").exists()
        assert all(Path(path).is_dir() for path in manifest["directories"].values())
        FILES.write(ToolContext(str(checkout)), "target.txt", content.encode())
        barrier.wait(timeout=8)
        result = process.run(
            [
                sys.executable,
                "-c",
                "import os,sys; from pathlib import Path; "
                "assert Path('target.txt').read_text() == sys.argv[1]; print(os.getpid())",
                content,
            ],
            cwd=str(checkout),
            context=ToolContext(str(checkout)),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return int(result.stdout)

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(edit, manifest, name) for manifest, name in zip(manifests, ("one", "two"), strict=True)]
        assert len({future.result(timeout=15) for future in futures}) == 2
    assert (Path(data.workspace) / "target.txt").read_text(encoding="utf-8") == "dirty main"
    again = prepare_environment(data.session_id, items[0]["work_id"], scope=scope, base_sha=sha)
    assert again == manifests[0]
    assert (Path(again["worktree"]) / "target.txt").read_text(encoding="utf-8") == "one"
    with pytest.raises(RuntimeError, match="M2 Worker process"):
        run_pending(data.session_id, execute=lambda _: pytest.fail("Shared executor ran"), on_result=lambda *_: None)
    (Path(again["worktree"]) / ".git").unlink()
    with pytest.raises(ValueError, match="own Git worktree"):
        prepare_environment(data.session_id, items[0]["work_id"], scope=scope, base_sha=sha)


def test_hardened_is_rejected_before_git(project, monkeypatch):
    data, scope, sha, items = project
    monkeypatch.setattr("pepsicode.orchestration.environment.sys.platform", "win32")
    monkeypatch.setattr("pepsicode.orchestration.environment._git", lambda *args, **kwargs: pytest.fail("Git ran"))
    with pytest.raises(ValueError, match="Only workspace isolation"):
        prepare_environment(data.session_id, items[0]["work_id"], scope=scope, base_sha=sha, isolation="hardened")


def test_scheduler_prepares_workspace_before_launch(project):
    from threading import Event

    data, scope, base, items = project
    scope.cancellation_event = Event()
    scope.budget = RequestBudget(CostTracker())

    class Model:
        def next(self, messages):
            return AgentStep(type="assistant", content="Prepared Worker completed.")

    worker = WorkerExecutor(
        scope, ToolRegistry([]), model_factory=lambda _: Model(), prepared_process=True, isolation="workspace"
    )
    assert run_pending(data.session_id, worker=worker, on_result=lambda *_: None)
    saved = sessions.load_session(data.session_id).task_control["orchestration"]
    assert [row["state"] for row in saved["work"]] == ["result_ready", "result_ready"]
    assert {row["base_sha"] for row in saved["environments"].values()} == {base}
    assert saved["environments"].keys() == {item["work_id"] for item in items}


def test_failed_checkout_is_retained_and_not_replayed(project, monkeypatch):
    from pepsicode.orchestration import environment

    data, scope, sha, items = project
    original = environment._git
    launches = []

    def git(context, cwd, *arguments, **kwargs):
        if arguments[0] == "worktree":
            launches.append(arguments)
            context.effect_state = "unknown"
            raise RuntimeError("Lost the checkout response")
        return original(context, cwd, *arguments, **kwargs)

    monkeypatch.setattr(environment, "_git", git)
    work_id = items[0]["work_id"]
    with pytest.raises(RuntimeError, match="Lost the checkout"):
        prepare_environment(data.session_id, work_id, scope=scope, base_sha=sha)
    saved = sessions.load_session(data.session_id)
    manifest = saved.task_control["orchestration"]["environments"][work_id]
    assert manifest["state"] == "prepare_failed"
    assert manifest["isolation_effective"] is None
    assert Path(manifest["worktree"]).parent.is_dir()
    assert ExecutionJournal.interrupted_calls(scope.journal.path)[0]["tool"] == "m2_prepare_environment"
    with pytest.raises(PermissionError, match="Reconcile"):
        prepare_environment(data.session_id, work_id, scope=scope, base_sha=sha)
    assert len(launches) == 1


def test_preparation_refuses_an_existing_branch(project):
    data, scope, sha, items = project
    work_id = items[0]["work_id"]
    branch = f"codex/m2-{work_id}"
    subprocess.run(["git", "branch", branch, sha], cwd=data.workspace, check=True, timeout=15)
    with pytest.raises(ValueError, match="branch name already exists"):
        prepare_environment(data.session_id, work_id, scope=scope, base_sha=sha)
    result = subprocess.run(
        ["git", "rev-parse", branch], cwd=data.workspace, check=True, capture_output=True, text=True, timeout=15
    )
    assert result.stdout.strip() == sha
    assert not (Path(data.workspace) / ".pepsi-code" / "m2" / work_id).exists()


def test_prepared_workers_run_model_and_tool_rounds_in_independent_processes(project, monkeypatch):
    data, scope, sha, items = project
    manifests = [prepare_environment(data.session_id, item["work_id"], scope=scope, base_sha=sha) for item in items]
    scope.budget = RequestBudget(CostTracker())
    scope.cancellation_event = Event()
    expected = dict(zip((row["worktree"] for row in manifests), ("first", "second"), strict=True))
    temps = {row["worktree"]: row["directories"]["tmp"] for row in manifests}
    monkeypatch.setenv("M2_TEST_PROVIDER_SECRET", "do-not-inherit")
    concurrent = Barrier(2)

    class Model:
        def next(self, messages):
            active = current_scope()
            assert active.parent_task_id == scope.task_id
            with active.budget.request(active, {}):
                results = [message for message in messages if message["role"] == "tool_result"]
                if results:
                    assert not results[0]["isError"]
                    assert results[1]["isError"] and "escapes the Worker checkout" in results[1]["content"]
                    assert not results[2]["isError"], results[2]["content"]
                    assert "m2-env-ok" in results[2]["content"]
                    return AgentStep(type="assistant", content=f"Updated {expected[active.cwd]}.")
                concurrent.wait(timeout=12)
                return AgentStep(
                    type="tool_calls",
                    calls=[
                        {
                            "id": "write",
                            "toolName": "write_file",
                            "input": {"path": "target.txt", "content": expected[active.cwd]},
                        },
                        {
                            "id": "outside",
                            "toolName": "write_file",
                            "input": {"path": str(Path(data.workspace) / "target.txt"), "content": "wrong"},
                        },
                        {
                            "id": "check",
                            "toolName": "run_command",
                            "input": {
                                "command": sys.executable,
                                "args": [
                                    "-c",
                                    "import os,sys; from pathlib import Path; "
                                    "assert 'M2_TEST_PROVIDER_SECRET' not in os.environ; "
                                    "assert Path(os.environ['TMPDIR']) == Path(sys.argv[1]); "
                                    "assert Path('target.txt').read_text() == sys.argv[2]; print('m2-env-ok')",
                                    temps[active.cwd],
                                    expected[active.cwd],
                                ],
                            },
                        },
                    ],
                )

    worker = WorkerExecutor(
        scope, ToolRegistry([write_file_tool, run_command_tool]), model_factory=lambda _: Model(), prepared_process=True
    )
    run_pending(data.session_id, worker=worker, on_result=lambda *_: None, max_workers=2)
    saved = sessions.load_session(data.session_id)
    assert [row["state"] for row in saved.task_control["orchestration"]["work"]] == ["result_ready", "result_ready"]
    starts = [
        entry
        for entry in ExecutionJournal.read(scope.journal.path)
        if entry["event"] == "orchestration/worker_process_started"
    ]
    assert {entry["attempt_id"] for entry in starts} == {item["attempt_id"] for item in items}
    assert len({entry["pid"] for entry in starts}) == 2
    assert scope.budget.requests == 4
    for checkout, content in expected.items():
        assert (Path(checkout) / "target.txt").read_text(encoding="utf-8") == content
    assert (Path(data.workspace) / "target.txt").read_text(encoding="utf-8") == "dirty main"


def test_prepared_process_keeps_provider_request_and_accounting_in_host(project, monkeypatch):
    data, scope, sha, items = project
    prepare_environment(data.session_id, items[0]["work_id"], scope=scope, base_sha=sha)
    scope.budget = RequestBudget(CostTracker())
    scope.cancellation_event = Event()
    requests = []

    def respond(request, **options):
        requests.append(json.loads(request.data))
        return io.BytesIO(
            json.dumps(
                {
                    "content": [{"type": "text", "text": "Inspection complete."}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 50, "output_tokens": 7},
                }
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", respond)
    worker = WorkerExecutor(
        scope,
        ToolRegistry([]),
        runtime={"model": "fixture", "baseUrl": "https://example.invalid", "apiKey": "fixture-secret"},
        prepared_process=True,
    )
    run_pending(data.session_id, worker=worker, on_result=lambda *_: None, max_workers=1)
    saved = sessions.load_session(data.session_id)
    assert saved.task_control["orchestration"]["work"][0]["state"] == "result_ready"
    assert len(requests) == scope.budget.requests == 1
    journal = ExecutionJournal.read(scope.journal.path)
    prepared = [record for record in journal if record["event"] == "model/prepared"]
    assert [record["trace_id"] for record in prepared] == [items[0]["trace_id"]]
    assert prepared[0]["body"] == requests[0]
    assert "fixture-secret" not in scope.journal.path.read_text(encoding="utf-8")


def test_crashed_prepared_worker_leaves_sibling_and_checkout_available(project, monkeypatch):
    from pepsicode.orchestration import process_worker

    data, scope, sha, items = project
    manifests = [prepare_environment(data.session_id, item["work_id"], scope=scope, base_sha=sha) for item in items]
    scope.budget = RequestBudget(CostTracker())
    scope.cancellation_event = Event()
    source = process_worker._DRIVER.read_text(encoding="utf-8")
    driver = Path(data.workspace) / "fixture-worker.py"
    driver.write_text(
        source.replace(
            "    sequence = 0",
            f"    if start['attempt_id'] == {items[0]['attempt_id']!r}:\n        sys.exit(7)\n    sequence = 0",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(process_worker, "_DRIVER", driver)

    class Model:
        def next(self, messages):
            return AgentStep(type="assistant", content="Sibling finished.")

    worker = WorkerExecutor(scope, ToolRegistry([]), model_factory=lambda _: Model(), prepared_process=True)
    run_pending(data.session_id, worker=worker, on_result=lambda *_: None, max_workers=2)
    saved = sessions.load_session(data.session_id)
    results = saved.task_control["orchestration"]["work"]
    assert [row["state"] for row in results] == ["failed", "result_ready"]
    assert "code 7" in results[0]["summary"]
    assert "Sibling finished" in results[1]["summary"]
    assert all(Path(row["worktree"]).is_dir() for row in manifests)


def test_known_prepare_failure_delivers_event_and_keeps_sibling_running(project):
    from pepsicode.core.task_control import edit_control

    data, scope, sha, items = project
    subprocess.run(
        ["git", "branch", f"codex/m2-{items[0]['work_id']}", sha],
        cwd=data.workspace,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    scope.budget = RequestBudget(CostTracker())
    scope.cancellation_event = Event()
    delivered = []

    def on_result(session, attempt_id):
        delivered.append(attempt_id)
        with edit_control(session=session) as control:
            for row in control["orchestration"]["events"]:
                if row["attempt_id"] == attempt_id and row["state"] == "pending":
                    row["state"] = "delivered"

    class Model:
        def next(self, messages):
            return AgentStep(type="assistant", content="Sibling finished.")

    worker = WorkerExecutor(
        scope, ToolRegistry([]), model_factory=lambda _: Model(), prepared_process=True, isolation="workspace"
    )
    run_pending(data.session_id, worker=worker, on_result=on_result, max_workers=2)
    saved = sessions.load_session(data.session_id)
    results = saved.task_control["orchestration"]["work"]
    assert results[0]["state"] == "failed"
    assert results[0]["outcome"] == "failed"
    assert json.loads(results[0]["summary"])["stage"] == "prepare"
    assert results[1]["state"] == "result_ready"
    assert delivered == [items[0]["attempt_id"], items[1]["attempt_id"]]
    assert not scope.has_unknown


def test_unknown_prepare_failure_preserves_state_and_blocks_scheduling(project, monkeypatch):
    from pepsicode.orchestration import environment as env_mod

    data, scope, sha, items = project
    scope.budget = RequestBudget(CostTracker())
    scope.cancellation_event = Event()
    orig_verify = env_mod._verify_checkout

    def fail_after_worktree(context, environment):
        if environment["branch"].endswith(items[0]["work_id"]):
            raise RuntimeError("Simulated failure after checkout creation")
        return orig_verify(context, environment)

    monkeypatch.setattr(env_mod, "_verify_checkout", fail_after_worktree)

    class Model:
        def next(self, messages):
            return AgentStep(type="assistant", content="Should not run.")

    worker = WorkerExecutor(
        scope, ToolRegistry([]), model_factory=lambda _: Model(), prepared_process=True, isolation="workspace"
    )
    with pytest.raises(RuntimeError, match="Simulated failure after checkout creation"):
        run_pending(data.session_id, worker=worker, on_result=lambda *_: None, max_workers=1)
    assert scope.has_unknown
    saved = sessions.load_session(data.session_id)
    env0 = saved.task_control["orchestration"]["environments"][items[0]["work_id"]]
    assert env0["state"] == "prepare_failed"
    assert Path(env0["worktree"]).is_dir()
    assert saved.task_control["orchestration"]["work"][1]["state"] == "queued"


def test_retry_checkout_verifies_commit_attribution(project):
    data, scope, sha, items = project
    item = items[0]
    manifest = prepare_environment(data.session_id, item["work_id"], scope=scope, base_sha=sha)
    checkout = Path(manifest["worktree"])

    def git_in_checkout(*args):
        return subprocess.run(
            ["git", *args], cwd=checkout, check=True, capture_output=True, text=True, timeout=15
        ).stdout.strip()

    # Unattributed descendant commit (no Worker ran, not in head_updates) must be rejected.
    (checkout / "target.txt").write_text("unattributed", encoding="utf-8")
    git_in_checkout("add", "target.txt")
    git_in_checkout("-c", "user.name=Rogue", "-c", "user.email=rogue@example.invalid", "commit", "-m", "Unattributed")
    with pytest.raises(ValueError, match="HEAD changed"):
        validate_prepared_environment(sessions.load_session(data.session_id), item, scope)
    with pytest.raises(ValueError, match="HEAD changed"):
        prepare_environment(data.session_id, item["work_id"], scope=scope, base_sha=sha)

    # Reset to baseline, create a legitimate commit, and record its attribution.
    git_in_checkout("reset", "--hard", sha)
    (checkout / "target.txt").write_text("attributed", encoding="utf-8")
    git_in_checkout("add", "target.txt")
    git_in_checkout("-c", "user.name=Worker", "-c", "user.email=worker@example.invalid", "commit", "-m", "Attributed")
    updated = attribute_managed_output(sessions.load_session(data.session_id), item, scope)
    assert updated is not None and len(updated["head_updates"]) == 1

    # Attributed commit passes both validation and retry preparation.
    cwd, _ = validate_prepared_environment(sessions.load_session(data.session_id), item, scope)
    assert Path(cwd) == checkout
    reprepared = prepare_environment(data.session_id, item["work_id"], scope=scope, base_sha=sha)
    assert reprepared["head_updates"] == updated["head_updates"]

    # Rewinding HEAD back to baseline invalidates attribution and must be rejected.
    git_in_checkout("reset", "--hard", sha)
    with pytest.raises(ValueError, match="HEAD changed"):
        validate_prepared_environment(sessions.load_session(data.session_id), item, scope)
    with pytest.raises(ValueError, match="HEAD changed"):
        prepare_environment(data.session_id, item["work_id"], scope=scope, base_sha=sha)


def test_worker_environment_retention_and_pruning(project, monkeypatch):
    from pepsicode.orchestration.candidate import record_candidate, validate_candidate
    from pepsicode.orchestration.external_delivery import deliver_integration
    from pepsicode.orchestration.integration import (
        integrate_candidates,
        run_integration_acceptance,
        validate_integration,
    )
    from pepsicode.orchestration.store import claim_launch, settle_launch

    data, scope, sha, items = project
    first, second = items

    # Known side-effect-free prepare failure (before git worktree add) can be retried cleanly.
    orig_mkdir = FILES.mkdir
    failed_once = {"done": False}

    def fail_mkdir_once(path):
        if not failed_once["done"] and second["work_id"] in str(path):
            failed_once["done"] = True
            raise OSError("Simulated pre-worktree directory error")
        return orig_mkdir(path)

    monkeypatch.setattr(FILES, "mkdir", fail_mkdir_once)
    with pytest.raises(OSError, match="Simulated pre-worktree"):
        prepare_environment(data.session_id, second["work_id"], scope=scope, base_sha=sha)
    assert not scope.has_unknown
    saved = sessions.load_session(data.session_id)
    assert saved.task_control["orchestration"]["environments"][second["work_id"]]["state"] == "prepare_failed"
    assert saved.task_control["orchestration"]["environments"][second["work_id"]]["effect_unknown"] is False

    # Retrying preparation for the same work_id succeeds and cleans up cleanly.
    manifest1 = prepare_environment(data.session_id, second["work_id"], scope=scope, base_sha=sha)
    assert manifest1["state"] == "prepared"

    # Prepare first work_id; active/queued environments cannot be pruned.
    manifest0 = prepare_environment(data.session_id, first["work_id"], scope=scope, base_sha=sha)
    checkout0 = Path(manifest0["worktree"])
    saved = sessions.load_session(data.session_id)
    with pytest.raises(ValueError, match="active or pending"):
        prune_environment(saved, scope, first["work_id"])

    # Claim first, then claim & fail second so second is retained for inspection.
    claimed_first = claim_launch(saved, generation="gen-1")
    assert claimed_first is not None and claimed_first["attempt_id"] == first["attempt_id"]
    claimed_second = claim_launch(saved, generation="gen-1")
    assert claimed_second is not None and claimed_second["attempt_id"] == second["attempt_id"]
    assert settle_launch(saved, attempt_id=second["attempt_id"], generation="gen-1", outcome="failed", summary="failed")

    # Settle first with uncommitted changes -> retained.
    dirty_file = checkout0 / "dirty.txt"
    dirty_file.write_text("wip", encoding="utf-8")
    assert settle_launch(
        saved, attempt_id=first["attempt_id"], generation="gen-1", outcome="result_ready", summary="ok"
    )
    saved = sessions.load_session(data.session_id)
    with pytest.raises(ValueError, match="uncommitted"):
        prune_environment(saved, scope, first["work_id"])

    # Commit deliverable -> still retained until accepted and delivered.
    dirty_file.unlink()
    (checkout0 / "target.txt").write_text("delivered-change", encoding="utf-8")
    subprocess.run(["git", "add", "target.txt"], cwd=checkout0, check=True, capture_output=True, text=True, timeout=15)
    subprocess.run(
        ["git", "-c", "user.name=W", "-c", "user.email=w@example.invalid", "commit", "-m", "deliverable"],
        cwd=checkout0,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert attribute_managed_output(saved, first, scope) is not None
    saved = sessions.load_session(data.session_id)
    with pytest.raises(ValueError, match="undelivered"):
        prune_environment(saved, scope, first["work_id"])

    # Record candidate, integrate, accept, and deliver.
    record_candidate(saved, scope, first["work_id"])
    monkeypatch.setattr(
        saved,
        "runtime",
        {"verificationCommands": [{"name": "ok", "command": [sys.executable, "-c", "print('ok')"]}]},
        raising=False,
    )
    integ = integrate_candidates(saved, scope, [first["work_id"]])
    integ = run_integration_acceptance(saved, scope, integ["integration_id"])
    deliver_integration(saved, scope, integ["integration_id"])
    saved = sessions.load_session(data.session_id)
    settled_at = next(
        row["settled_at"] for row in saved.task_control["orchestration"]["work"] if row["work_id"] == first["work_id"]
    )

    # Inside retention window -> refused.
    with pytest.raises(ValueError, match="retention period"):
        prune_environment(saved, scope, first["work_id"], retention_seconds=3600, now=settled_at + 10)

    # Past retention window: first (delivered) is pruned; second (failed) is retained.
    summary = prune_environments(saved, scope, retention_seconds=3600, now=settled_at + 4000)
    assert [row["state"] for row in summary["pruned"]] == ["pruned"]
    assert first["work_id"] == summary["pruned"][0]["branch"].removeprefix("codex/m2-")
    assert second["work_id"] in summary["retained"]
    assert "inspection" in summary["retained"][second["work_id"]]
    assert not checkout0.exists()
    assert Path(manifest1["worktree"]).is_dir()

    reloaded = sessions.load_session(data.session_id)
    assert reloaded.task_control["orchestration"]["environments"][first["work_id"]]["state"] == "pruned"
    assert validate_candidate(reloaded, scope, first["work_id"])["state"] == "recorded"
    assert validate_integration(reloaded, scope, integ["integration_id"])["state"] == "accepted"
    journal = ExecutionJournal.read(scope.journal.path)
    assert any(
        entry.get("event") == "tool/settled"
        and entry.get("call_id") == f"m2-prune:{first['work_id']}"
        and entry.get("state") == "succeeded"
        for entry in journal
    )


def test_environment_manifest_ports_lockfiles_cache_lock_and_resource_limits(project, monkeypatch):
    from threading import Event

    from pepsicode import main as main_module
    from pepsicode.core.task_control import edit_control
    from pepsicode.orchestration.environment import own_dependency_cache
    from pepsicode.service import cli as service_cli

    data, scope, _sha, items = project
    real_python = sys.executable
    repo = Path(data.workspace)
    (repo / "pyproject.toml").write_text('[project]\nname = "fixture-proj"\nversion = "0.1.0"\n', encoding="utf-8")
    subprocess.run(["git", "add", "pyproject.toml"], cwd=repo, check=True, capture_output=True, text=True, timeout=15)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Add pyproject"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    new_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, timeout=15
    ).stdout.strip()

    with edit_control(session=data) as control:
        control["orchestration"]["work"][0]["specification"]["install_command"] = [
            real_python,
            "-c",
            "import os, pathlib; "
            "assert os.environ.get('PEPSI_WORKER_PORT_START'); "
            "assert os.environ.get('PEPSI_TEST_DATA_DIR'); "
            "pathlib.Path(os.environ['PIP_CACHE_DIR']).mkdir(parents=True, exist_ok=True); "
            "pathlib.Path('installed.marker').write_text(os.environ['PEPSI_WORKER_PORT_START'], encoding='utf-8')",
        ]

    m0 = prepare_environment(data.session_id, items[0]["work_id"], scope=scope, base_sha=new_sha)
    m1 = prepare_environment(data.session_id, items[1]["work_id"], scope=scope, base_sha=new_sha)

    assert m0["steps"] == ["checkout", "directories", "dependencies"]
    assert m1["steps"] == ["checkout", "directories"]
    assert (Path(m0["worktree"]) / "installed.marker").read_text(encoding="utf-8") == str(m0["port_range"]["start"])
    with own_dependency_cache(repo):
        assert m0["cache_policy"]["shared_cache_lock"] == str(repo.resolve() / ".pepsi-code" / "m2" / "cache.lock")
        with pytest.raises(RuntimeError, match="ownership"), own_dependency_cache(repo):
            pass

    p0, p1 = m0["port_range"], m1["port_range"]
    assert p0["end"] - p0["start"] == 9
    assert p1["end"] - p1["start"] == 9
    assert p0["end"] < p1["start"] or p1["end"] < p0["start"]

    assert m0["lockfile_digests"]["pyproject.toml"]
    assert m0["cache_policy"]["mode"] == "isolated_per_worker"
    assert m0["cache_policy"]["writable_symlinks_allowed"] is False
    assert m0["environment_contract"]["runtime_version"].startswith(
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    assert m0["environment_contract"]["network_policy"] == "workspace_policy"
    assert m0["resource_limits"]["wall_time_seconds"]["enforcement"] == "enforced"
    assert m0["resource_limits"]["ipc_frame_bytes"]["enforcement"] == "enforced"
    assert m0["resource_limits"]["cpu"]["enforcement"] == "unsupported"
    assert m0["resource_limits"]["memory_bytes"]["enforcement"] == "unsupported"

    again = prepare_environment(data.session_id, items[0]["work_id"], scope=scope, base_sha=new_sha)
    assert again == m0

    # Verify packaged (`sys.frozen = True`) CLI & Worker process resolution without re-entering server exe.
    fake_exe = str(repo / "pepsicode-server.exe")
    with monkeypatch.context() as patcher:
        patcher.setattr(sys, "frozen", True, raising=False)
        patcher.setattr(sys, "executable", fake_exe)
        patcher.setattr("pepsicode.orchestration.process_worker.project_python", lambda _cwd: real_python)

        cmd = service_cli._service_foreground_command(repo, 9123)
        assert cmd == [fake_exe, "service", "foreground", "--workspace", str(repo.resolve()), "--port", "9123"]

        patcher.setattr(sys, "argv", ["pepsicode", "service", "status", "--workspace", str(repo)])
        with pytest.raises(SystemExit) as exited:
            main_module.main()
        assert exited.value.code == 1

        scope.budget = RequestBudget(CostTracker())
        scope.cancellation_event = Event()

        class FrozenCheckModel:
            def next(self, messages):
                results = [msg for msg in messages if msg["role"] == "tool_result"]
                if results:
                    assert not results[0]["isError"], results[0]["content"]
                    assert "frozen-worker-ok" in results[0]["content"]
                    return AgentStep(type="assistant", content="Packaged worker finished.")
                return AgentStep(
                    type="tool_calls",
                    calls=[
                        {
                            "id": "env-check",
                            "toolName": "run_command",
                            "input": {
                                "command": real_python,
                                "args": [
                                    "-c",
                                    "import os, sys; "
                                    "assert os.environ['PEPSI_WORKER_PORT_START'] == sys.argv[1]; "
                                    "assert os.environ['PEPSI_WORKER_PORT_END'] == sys.argv[2]; "
                                    "assert os.environ['PEPSI_TEST_DATA_DIR'] == sys.argv[3]; "
                                    "print('frozen-worker-ok')",
                                    str(m1["port_range"]["start"]),
                                    str(m1["port_range"]["end"]),
                                    str(m1["directories"]["test-data"]),
                                ],
                            },
                        }
                    ],
                )

        # Mark first work_id done so run_pending launches only second under frozen mode.
        from pepsicode.orchestration.store import claim_launch, settle_launch

        saved = sessions.load_session(data.session_id)
        claim = claim_launch(saved, generation="g-prep")
        assert claim is not None and claim["attempt_id"] == items[0]["attempt_id"]
        assert settle_launch(
            saved, attempt_id=items[0]["attempt_id"], generation="g-prep", outcome="result_ready", summary="done"
        )
        worker = WorkerExecutor(
            scope, ToolRegistry([run_command_tool]), model_factory=lambda _: FrozenCheckModel(), prepared_process=True
        )
        run_pending(data.session_id, worker=worker, on_result=lambda *_: None, max_workers=1)
        reloaded = sessions.load_session(data.session_id)
        assert [row["state"] for row in reloaded.task_control["orchestration"]["work"]] == [
            "result_ready",
            "result_ready",
        ]
