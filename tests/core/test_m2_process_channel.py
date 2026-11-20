from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from pepsicode.cancellation import TurnCancelledError
from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope, RequestBudget
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.orchestration.process_channel import WorkerProcessError, WorkerProtocolError, run_worker_process
from pepsicode.permissions import PermissionManager

_CHILD = """
import json
import os
import sys
import time

start = json.loads(sys.stdin.readline())
identity = {key: start[key] for key in ("version", "run_id", "attempt_id", "generation", "nonce")}
def send(kind, sequence, payload):
    print(json.dumps(dict(identity, kind=kind, sequence=sequence, payload=payload)), flush=True)

mode = start["payload"]["mode"]
if mode == "silent":
    time.sleep(30)
if mode == "wrong":
    identity["attempt_id"] = "another-attempt"
if mode == "oversized":
    print("x" * (64 * 1024 + 1), flush=True)
    sys.exit(0)
send("request", 2 if mode == "sequence" else 1, {
    "operation": "approve" if mode == "unsupported" else "request_slot",
    "arguments": {},
})
if mode == "crash_during_operation":
    sys.exit(9)
response = json.loads(sys.stdin.readline())
if mode == "crash":
    sys.stderr.write("fatal child diagnostic boom\\n")
    sys.stderr.flush()
    sys.exit(7)
send("result", 2, {"pid": os.getpid(), "granted": response["payload"]["granted"]})
"""


@pytest.fixture
def workspace(tmp_path):
    permissions = PermissionManager(str(tmp_path), mode="full_access")
    journal = ExecutionJournal(tmp_path / "worker.jsonl")
    budget = RequestBudget(CostTracker(), max_requests=2)

    def launch(attempt_id, mode, callback, *, stop=None, timeout=10):
        checkout = tmp_path / attempt_id
        checkout.mkdir()
        scope = ExecutionScope(
            str(checkout),
            permissions=permissions,
            journal=journal,
            budget=budget,
            parent_task_id="parent",
            cancellation_event=stop if stop is not None else Event(),
        )
        return run_worker_process(
            [sys.executable, "-I", "-u", "-c", _CHILD],
            scope=scope,
            cwd=checkout,
            run_id="run",
            attempt_id=attempt_id,
            generation="generation",
            payload={"mode": mode},
            operations={"request_slot": callback},
            timeout=timeout,
        )

    try:
        yield launch, journal, budget
    finally:
        permissions.close()


def test_crashing_worker_does_not_stop_sibling_and_both_charge_host_budget(workspace):
    launch, journal, budget = workspace
    both_started = Barrier(2)

    def request(scope, _):
        assert scope.budget is budget
        with budget.request(scope, {}):
            both_started.wait(timeout=8)
            return {"granted": True}

    with ThreadPoolExecutor(2) as pool:
        crashed = pool.submit(launch, "first", "crash", request)
        surviving = pool.submit(launch, "second", "complete", request)
        with pytest.raises(WorkerProcessError) as error:
            crashed.result(timeout=15)
        assert error.value.exit_code == 7
        assert "fatal child diagnostic boom" in str(error.value)
        result = surviving.result(timeout=15)
    assert result.payload == {"pid": result.pid, "granted": True}
    assert budget.requests == 2
    exits = {
        row["attempt_id"]: (row["pid"], row["exit_code"])
        for row in journal.read(journal.path)
        if row["event"] == "orchestration/worker_process_ended"
    }
    assert exits["first"][0] != result.pid
    assert exits["second"] == (result.pid, 0)


@pytest.mark.parametrize(
    "mode, message",
    [("wrong", "identity"), ("oversized", "Oversized"), ("sequence", "sequence"), ("unsupported", "unsupported")],
)
def test_invalid_child_frames_never_reach_host_operations(workspace, mode, message):
    launch, _, budget = workspace
    with pytest.raises(WorkerProtocolError, match=message):
        launch("worker", mode, lambda *_: pytest.fail("Unvalidated operation executed"))
    assert budget.requests == 0


def test_targeted_stop_reaps_child_before_returning(workspace):
    launch, journal, _ = workspace
    stop = Event()

    def request(*_):
        stop.set()
        return {"granted": True}

    with pytest.raises(TurnCancelledError):
        launch("worker", "complete", request, stop=stop)
    terminal = journal.read(journal.path)[-1]
    assert terminal["event"] == "orchestration/worker_process_ended"
    assert terminal["exit_code"] is not None


def test_silent_worker_is_reaped_at_deadline(workspace):
    launch, journal, _ = workspace
    with pytest.raises(TimeoutError, match="deadline"):
        launch("worker", "silent", lambda *_: pytest.fail("Unexpected Worker request"), timeout=1)
    terminal = journal.read(journal.path)[-1]
    assert terminal["event"] == "orchestration/worker_process_ended"
    assert terminal["exit_code"] is not None


def test_child_exit_interrupts_a_pending_host_operation(workspace):
    launch, _, _ = workspace
    reached = Event()

    def request(scope, _):
        reached.set()
        assert scope.cancellation_event.wait(5)
        scope.check_cancelled()

    with pytest.raises(WorkerProcessError) as error:
        launch("worker", "crash_during_operation", request)
    assert reached.is_set()
    assert error.value.exit_code == 9
