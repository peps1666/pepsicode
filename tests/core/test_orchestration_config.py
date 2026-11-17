from __future__ import annotations

import time
from queue import SimpleQueue
from threading import Event, Thread

import pytest

from pepsicode import config
from pepsicode.core import session as sessions
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
from pepsicode.orchestration.runtime import _collect_finished, run_owned
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
