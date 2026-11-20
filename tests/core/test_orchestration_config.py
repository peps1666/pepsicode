from __future__ import annotations

import subprocess
import time
from queue import SimpleQueue
from threading import Event, Thread

import pytest

from pepsicode import config
from pepsicode.core import session as sessions
from pepsicode.core.input_queue import TurnInputQueue
from pepsicode.core.session_tree import recording_session
from pepsicode.core.task_control import begin_task
from pepsicode.execution.runtime import (
    ExecutionJournal,
    ExecutionScope,
    RequestBudget,
    bind_scope,
    execution_journal_dir,
)
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.orchestration.coordinator import coordinator_turn
from pepsicode.orchestration.delivery import deliver_pending
from pepsicode.orchestration.runtime import _collect_finished, _deliver_results, run_owned
from pepsicode.orchestration.store import (
    admit_work,
    claim_activation,
    claim_launch,
    retry_work,
    settle_launch,
)
from pepsicode.orchestration.worker import WorkerExecutor
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.task_state import task_state_tool


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "home" / "sessions")
    result = sessions.create_new_session(str(tmp_path))
    with recording_session(result):
        begin_task([{"role": "user", "content": "Inspect this project"}], {})
    return result


@pytest.fixture
def scope(session, monkeypatch):
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", sessions.PEPSI_CODE_DIR)
    permissions = PermissionManager(session.workspace, mode="full_access")
    try:
        yield ExecutionScope(
            session.workspace,
            permissions,
            Event(),
            RequestBudget(CostTracker()),
            ExecutionJournal(execution_journal_dir(session.workspace) / "config.jsonl"),
        )
    finally:
        permissions.close()


def _runtime(monkeypatch, orchestration=None, **extra):
    settings = {"model": "test", "env": {"ANTHROPIC_API_KEY": "test-only"}}
    if orchestration is not None:
        settings["orchestration"] = orchestration
    settings.update(extra)
    monkeypatch.setattr(config, "load_effective_settings", lambda cwd=None: settings)
    return config.load_runtime_config()


def _dispatch(session, key):
    task_id = session.task_control["task"]["id"]
    return admit_work(session, task_id=task_id, revision=1, work_key=key, specification={"goal": key})


def _settle_result(session, key):
    item = _dispatch(session, key)
    claimed = claim_launch(session, generation="host")
    assert claimed["attempt_id"] == item["attempt_id"]
    assert settle_launch(
        session, attempt_id=item["attempt_id"], generation="host", outcome="result_ready", summary="x" * 50
    )


def test_config_exposes_documented_orchestration_defaults(monkeypatch):
    runtime = _runtime(monkeypatch)

    assert runtime["orchestration"] == {
        "enabled": False,
        "executionMode": "interactive",
        "workerIsolation": "shared",
        "maxRepairAttempts": 2,
        "maxConcurrentWorkers": 3,
        "maxQueuedWorkItems": 32,
        "maxActiveWorkItemsPerRun": 32,
        "maxAutoRetries": 1,
        "maxCoordinatorActivations": 20,
        "eventBatchLimit": 8,
        "eventContextChars": 12_000,
        "eventDebounceMs": 250,
        "eventMaxDelayMs": 1000,
        "coordinatorReserveRequests": 2,
        "workerTimeoutSeconds": 300,
    }


def test_config_accepts_orchestration_overrides(monkeypatch):
    runtime = _runtime(
        monkeypatch,
        {
            "enabled": True,
            "maxConcurrentWorkers": 1,
            "maxQueuedWorkItems": 2,
            "maxActiveWorkItemsPerRun": 3,
            "maxAutoRetries": 0,
            "maxCoordinatorActivations": 4,
            "eventBatchLimit": 2,
            "eventContextChars": 100,
            "eventDebounceMs": 0,
            "eventMaxDelayMs": 0,
            "coordinatorReserveRequests": 0,
        },
    )

    orchestration = runtime["orchestration"]
    assert orchestration["enabled"] is True
    assert orchestration["maxConcurrentWorkers"] == 1
    assert orchestration["maxQueuedWorkItems"] == 2
    assert orchestration["maxActiveWorkItemsPerRun"] == 3
    assert orchestration["maxAutoRetries"] == 0
    assert orchestration["maxCoordinatorActivations"] == 4
    assert orchestration["eventBatchLimit"] == 2
    assert orchestration["eventContextChars"] == 100
    assert orchestration["eventDebounceMs"] == 0
    assert orchestration["eventMaxDelayMs"] == 0
    assert orchestration["coordinatorReserveRequests"] == 0


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"maxConcurrentWorkers": 0}, "maxConcurrentWorkers"),
        ({"maxConcurrentWorkers": True}, "maxConcurrentWorkers"),
        ({"maxQueuedWorkItems": 0}, "maxQueuedWorkItems"),
        ({"maxActiveWorkItemsPerRun": 0}, "maxActiveWorkItemsPerRun"),
        ({"maxAutoRetries": -1}, "maxAutoRetries"),
        ({"maxCoordinatorActivations": 0}, "maxCoordinatorActivations"),
        ({"eventBatchLimit": 0}, "eventBatchLimit"),
        ({"eventContextChars": 0}, "eventContextChars"),
        ({"eventDebounceMs": -1}, "eventDebounceMs"),
        ({"eventMaxDelayMs": 100, "eventDebounceMs": 250}, "eventMaxDelayMs"),
        ({"coordinatorReserveRequests": -1}, "coordinatorReserveRequests"),
    ],
)
def test_config_rejects_invalid_orchestration_limits(monkeypatch, overrides, match):
    with pytest.raises(ValueError, match=match):
        _runtime(monkeypatch, overrides)


def test_config_rejects_coordinator_reserve_at_or_above_request_limit(monkeypatch):
    with pytest.raises(ValueError, match="coordinatorReserveRequests"):
        _runtime(monkeypatch, {"enabled": True, "coordinatorReserveRequests": 2}, modelRequestLimit=2)


def test_disabled_orchestration_ignores_reserve_against_small_request_limit(monkeypatch):
    runtime = _runtime(monkeypatch, None, modelRequestLimit=1)

    assert runtime["orchestration"]["coordinatorReserveRequests"] == 2


def test_delivery_uses_configured_batch_limit(session):
    _settle_result(session, "one")
    _settle_result(session, "two")

    assert len(deliver_pending(session, batch_limit=1)) == 1
    assert len(deliver_pending(session, batch_limit=1)) == 1


def test_delivery_uses_configured_context_limit(session):
    _settle_result(session, "one")

    assert deliver_pending(session, context_limit=1) == []
    assert len(deliver_pending(session)) == 1


def test_delivery_rejects_non_positive_limits(session):
    with pytest.raises(ValueError, match="batch_limit"):
        deliver_pending(session, batch_limit=0)
    with pytest.raises(ValueError, match="context_limit"):
        deliver_pending(session, context_limit=0)


def test_coordinator_dispatch_uses_configured_queue_limit(session, scope):
    runtime = {"orchestration": {"maxQueuedWorkItems": 1, "maxActiveWorkItemsPerRun": 10}}
    with recording_session(session), bind_scope(scope), coordinator_turn(session, runtime=runtime) as tools:
        first = tools.execute(
            "task_dispatch",
            {"work_key": "one", "goal": "Inspect", "agent_type": "explore"},
            ToolContext(session.workspace, execution=scope),
        )
        second = tools.execute(
            "task_dispatch",
            {"work_key": "two", "goal": "Inspect", "agent_type": "explore"},
            ToolContext(session.workspace, execution=scope),
        )

    assert first.ok
    assert not second.ok and "queue is full" in second.output


@pytest.mark.parametrize("outcome", ["failed", "waiting_user"])
def test_report_batch_wakes_for_action_while_sibling_is_running(session, outcome):
    task = session.task_control["task"]
    first = admit_work(
        session,
        task_id=task["id"],
        revision=1,
        work_key="one",
        specification={"goal": "Inspect", "deliverable": "worker_reports"},
    )
    _dispatch(session, "two")
    _dispatch(session, "three")
    for _ in range(3):
        claim_launch(session, generation="host")
    settle_launch(session, generation="host", attempt_id=first["attempt_id"], outcome="result_ready", summary="Done.")
    delivered = []

    def receive(current, _):
        delivered.append(deliver_pending(current))

    _deliver_results(session, receive, batch_reports=True)
    assert not delivered
    second = session.task_control["orchestration"]["work"][1]
    settle_launch(
        session, generation="host", attempt_id=second["attempt_id"], outcome=outcome, summary="Needs attention"
    )
    _deliver_results(session, receive, batch_reports=True)
    assert len(delivered) == 1 and len(delivered[0]) == 2


def test_caller_requirements_and_first_dispatch_preserve_project_deliverable(session):
    task = session.task_control["task"]

    def request_reports():
        admit_work(
            session,
            task_id=task["id"],
            revision=1,
            work_key="two",
            specification={"goal": "two", "deliverable": "worker_reports"},
        )

    task["requirements"] = {"behavior": True}
    with pytest.raises(ValueError, match="requires project delivery"):
        request_reports()
    session.task_control["task"]["requirements"] = {}
    _dispatch(session, "one")
    with pytest.raises(ValueError, match="different deliverable"):
        request_reports()
    assert session.task_control["task"]["orchestration_output"]["kind"] == "project"


def test_dispatch_allows_non_git_workspace_isolation_and_checks_git_for_hardened(session, scope):
    def git(*args):
        subprocess.run(["git", *args], cwd=session.workspace, check=True, capture_output=True, timeout=15)

    def dispatch(tools, kind, key=None):
        return tools.execute(
            "task_dispatch",
            {"work_key": key or kind, "goal": "Handle module", "agent_type": kind},
            ToolContext(session.workspace, permissions=scope.permissions, execution=scope),
        )

    # workspace isolation supports non-Git/empty folders via directory-isolated workspaces
    workspace_runtime = {"orchestration": {"workerIsolation": "workspace"}}
    with bind_scope(scope), coordinator_turn(session, runtime=workspace_runtime) as tools:
        assert dispatch(tools, "general", key="dir-worker").ok

    # hardened isolation still requires a Git repository root with an initial commit
    hardened_runtime = {"orchestration": {"workerIsolation": "hardened"}}
    with bind_scope(scope), coordinator_turn(session, runtime=hardened_runtime) as tools:
        missing_repo = dispatch(tools, "general")
        assert not missing_repo.ok and "派工未入队" in missing_repo.output
        assert dispatch(tools, "explore").ok
        git("init")
        missing_head = dispatch(tools, "general")
        assert not missing_head.ok and "没有可用的 HEAD 提交" in missing_head.output
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "Base")
        assert dispatch(tools, "general").ok

    assert [item["work_key"] for item in session.task_control["orchestration"]["work"]] == [
        "dir-worker",
        "explore",
        "general",
    ]


def test_dispatch_uses_updated_contract_but_waits_for_undelivered_steering(session, scope):
    queue = TurnInputQueue()
    queue.bind_session(session)
    queue.start()
    context = ToolContext(session.workspace, permissions=scope.permissions, execution=scope)
    task = session.task_control["task"]
    with bind_scope(scope), coordinator_turn(session, admitted_only=True) as tools:
        assert (
            ToolRegistry([task_state_tool])
            .execute(
                "task_state",
                {
                    "action": "define",
                    "task_id": task["id"],
                    "revision": 1,
                    "criteria": [{"id": "feature", "kind": "file_exists", "path": "feature.py"}],
                },
                context,
            )
            .ok
        )
        assignment = {"work_key": "first", "goal": "Implement feature", "agent_type": "general"}
        dispatched = tools.execute("task_dispatch", assignment, context)
        assert dispatched.ok and dispatched.details["revision"] == 1
        queue.enqueue("Change the format used by the implementation")
        stale = tools.execute("task_dispatch", {**assignment, "work_key": "second"}, context)
        assert not stale.ok and "requested revision" in stale.output


@pytest.mark.parametrize("deliverable", ["project", "worker_reports"])
def test_retry_checks_git_baseline_for_hardened_and_allows_directory_workspace(session, scope, deliverable):
    item = admit_work(
        session,
        task_id=session.task_control["task"]["id"],
        revision=1,
        work_key="implement",
        specification={"goal": "Implement feature", "agent_type": "general", "deliverable": deliverable},
    )
    assert claim_launch(session, generation="host")
    assert settle_launch(session, attempt_id=item["attempt_id"], generation="host", outcome="failed", summary="Failed")
    hardened_runtime = {"orchestration": {"workerIsolation": "hardened"}}
    context = ToolContext(session.workspace, permissions=scope.permissions, execution=scope)
    with bind_scope(scope), coordinator_turn(session, cancel=lambda *_: None, runtime=hardened_runtime) as tools:
        result = tools.execute("task_retry", {"attempt_id": item["attempt_id"]}, context)
        assert not result.ok and "派工未入队" in result.output
        assert result.awaitUser == (deliverable == "worker_reports")
    assert len(session.task_control["orchestration"]["work"]) == 1

    workspace_runtime = {"orchestration": {"workerIsolation": "workspace"}}
    with bind_scope(scope), coordinator_turn(session, cancel=lambda *_: None, runtime=workspace_runtime) as tools:
        retried = tools.execute("task_retry", {"attempt_id": item["attempt_id"]}, context)
        assert retried.ok
    assert len(session.task_control["orchestration"]["work"]) == 2


def test_config_validates_worker_timeout_seconds(monkeypatch):
    runtime = _runtime(monkeypatch, {"workerTimeoutSeconds": 450})
    assert runtime["orchestration"]["workerTimeoutSeconds"] == 450
    for invalid in (0, -5, True, "60", float("inf")):
        with pytest.raises(ValueError, match="workerTimeoutSeconds"):
            _runtime(monkeypatch, {"workerTimeoutSeconds": invalid})


def test_coordinator_dispatch_uses_configured_active_limit(session, scope):
    runtime = {"orchestration": {"maxQueuedWorkItems": 10, "maxActiveWorkItemsPerRun": 1}}
    with recording_session(session), bind_scope(scope), coordinator_turn(session, runtime=runtime) as tools:
        first = tools.execute(
            "task_dispatch",
            {"work_key": "one", "goal": "Inspect", "agent_type": "explore"},
            ToolContext(session.workspace, execution=scope),
        )
        second = tools.execute(
            "task_dispatch",
            {"work_key": "two", "goal": "Inspect", "agent_type": "explore"},
            ToolContext(session.workspace, execution=scope),
        )

    assert first.ok
    assert not second.ok and "active work limit" in second.output


def test_claim_activation_honors_limit(session):
    assert claim_activation(session, limit=1)
    assert not claim_activation(session, limit=1)


def test_retry_honors_configured_automatic_retry_limit(session):
    task_id = session.task_control["task"]["id"]
    item = admit_work(
        session,
        task_id=task_id,
        revision=1,
        work_key="explore",
        specification={"agent_type": "explore", "goal": "Inspect"},
    )
    assert claim_launch(session, generation="host")["attempt_id"] == item["attempt_id"]
    assert settle_launch(session, attempt_id=item["attempt_id"], generation="host", outcome="failed", summary="boom")

    first = retry_work(session, task_id=task_id, revision=1, attempt_id=item["attempt_id"], max_auto_retries=1)
    assert claim_launch(session, generation="host")["attempt_id"] == first["attempt_id"]
    assert settle_launch(session, attempt_id=first["attempt_id"], generation="host", outcome="failed", summary="boom")

    with pytest.raises(ValueError, match="automatic retry limit"):
        retry_work(session, task_id=task_id, revision=1, attempt_id=first["attempt_id"], max_auto_retries=1)


def test_retry_honors_repair_limit_above_default(session):
    task_id = session.task_control["task"]["id"]
    attempt = admit_work(
        session,
        task_id=task_id,
        revision=1,
        work_key="repair",
        specification={"agent_type": "general", "goal": "Update the project"},
    )
    for index in range(3):
        assert claim_launch(session, generation="host")["attempt_id"] == attempt["attempt_id"]
        assert settle_launch(
            session,
            attempt_id=attempt["attempt_id"],
            generation="host",
            outcome="failed",
            summary=f"code error {index}",
        )
        attempt = retry_work(
            session,
            task_id=task_id,
            revision=1,
            attempt_id=attempt["attempt_id"],
            max_repair_attempts=3,
        )
    assert claim_launch(session, generation="host")["attempt_id"] == attempt["attempt_id"]
    assert settle_launch(
        session, attempt_id=attempt["attempt_id"], generation="host", outcome="failed", summary="code error 4"
    )
    with pytest.raises(ValueError, match="repair limit"):
        retry_work(
            session,
            task_id=task_id,
            revision=1,
            attempt_id=attempt["attempt_id"],
            max_repair_attempts=3,
        )


def test_worker_uses_configured_coordinator_reserve(session, scope):
    worker = WorkerExecutor(scope, ToolRegistry([]), runtime={}, coordinator_reserve=5)

    with worker.bind(session, generation="host"):
        assert scope.budget.coordinator_reserve == 5


def test_collect_finished_coalesces_within_debounce():
    ready = SimpleQueue()
    first, second = object(), object()
    active = {first: None, second: None}
    finished = [first]

    def emit_second():
        time.sleep(0.05)
        ready.put(second)

    Thread(target=emit_second, daemon=True).start()
    assert _collect_finished(ready, active, finished, 500, 2000) == [first, second]


def test_collect_finished_never_delays_steering_and_respects_max_delay():
    ready = SimpleQueue()
    first, second = object(), object()
    ready.put(None)
    assert _collect_finished(ready, {first: None, second: None}, [first], 500, 2000) == [first]

    assert _collect_finished(SimpleQueue(), {first: None, second: None}, [first], 500, 0) == [first]


def test_run_owned_reads_max_workers_from_runtime(session):
    _dispatch(session, "one")
    _dispatch(session, "two")
    started = [Event(), Event()]
    release = Event()

    def execute(item):
        if not started[0].is_set():
            started[0].set()
            assert release.wait(5)
        elif not started[1].is_set():
            started[1].set()
        return "result_ready", "done"

    def on_result(current, attempt_id):
        deliver_pending(current)

    runtime = {"orchestration": {"maxConcurrentWorkers": 1, "eventDebounceMs": 0, "eventMaxDelayMs": 0}}
    thread = Thread(
        target=lambda: run_owned(
            session,
            execute=execute,
            on_result=on_result,
            is_read_only=lambda _: True,
            runtime=runtime,
        )
    )
    thread.start()
    try:
        assert started[0].wait(5)
        assert not started[1].wait(0.2)
    finally:
        release.set()
    thread.join(10)

    assert not thread.is_alive()
    assert started[1].is_set()
