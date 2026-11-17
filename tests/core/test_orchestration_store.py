from __future__ import annotations

import json
import subprocess
import sys
from threading import Event

import pytest

from pepsicode import config
from pepsicode.core import session as sessions
from pepsicode.core.agent_loop import run_agent_turn, run_agent_turn_stream
from pepsicode.core.input_queue import TurnInputQueue
from pepsicode.core.session_tree import recording_session
from pepsicode.core.task_control import admit_steering, begin_task, edit_control
from pepsicode.core.verification import run_verified_turn
from pepsicode.execution.runtime import (
    ExecutionJournal,
    ExecutionScope,
    RequestBudget,
    bind_scope,
    current_scope,
    execution_journal_dir,
)
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.orchestration.delivery import deliver_pending
from pepsicode.orchestration.runtime import own_session, own_workspace, run_pending
from pepsicode.orchestration.store import admit_work, claim_launch, recover_launches, retry_work, settle_launch
from pepsicode.orchestration.worker import WorkerExecutor
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.write_file import write_file_tool
from pepsicode.types import AgentStep, StreamToken


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "home" / "sessions")
    result = sessions.create_new_session(str(tmp_path))
    with recording_session(result):
        begin_task([{"role": "user", "content": "Inspect this project"}], {})
    return result


def dispatch(session, **kwargs):
    task_id = session.task_control["task"]["id"]
    return admit_work(session, task_id=task_id, revision=1, work_key="one", specification={"goal": "inspect"}, **kwargs)


def test_completed_worker_report_followup_is_a_single_read_only_assignment(session, worker_scope):
    from pepsicode.orchestration.coordinator import coordinator_turn

    original = dispatch(session)
    claimed = claim_launch(session, generation="host")
    assert claimed["attempt_id"] == original["attempt_id"]
    assert settle_launch(
        session, attempt_id=original["attempt_id"], generation="host", outcome="result_ready", summary="Found file"
    )
    with bind_scope(worker_scope), coordinator_turn(session, cancel=lambda *_: None) as tools:
        context = ToolContext(session.workspace, execution=worker_scope)
        status = tools.execute("agent_status", {}, context)
        assert status.ok and len(session.task_control["orchestration"]["work"]) == 1
        args = {"attempt_id": original["attempt_id"], "note": "Which file?"}
        first = tools.execute("task_request_report", args, ToolContext(session.workspace, execution=worker_scope))
        duplicate = tools.execute("task_request_report", args, ToolContext(session.workspace, execution=worker_scope))
    assert first.ok and duplicate.ok
    assert first.details["attempt_id"] == duplicate.details["attempt_id"]
    original, followup = session.task_control["orchestration"]["work"]
    assert original["state"] == "result_ready"
    assert followup["specification"]["agent_type"] == "explore"
    assert "Which file?" in followup["specification"]["goal"]


@pytest.fixture
def worker_scope(session, monkeypatch):
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", sessions.PEPSI_CODE_DIR)
    permissions = PermissionManager(session.workspace, mode="full_access")
    try:
        yield ExecutionScope(
            session.workspace,
            permissions,
            Event(),
            RequestBudget(CostTracker()),
            ExecutionJournal(execution_journal_dir(session.workspace) / "test-worker.jsonl"),
        )
    finally:
        permissions.close()


@pytest.mark.parametrize("live_reply", [False, True])
def test_managed_worker_question_is_delivered_while_sibling_runs(session, worker_scope, live_reply):
    sibling_started = Event()
    release_sibling = Event()
    seen = {}
    inbox = TurnInputQueue()
    inbox.start()
    task_id = session.task_control["task"]["id"]
    for key in ("question", "sibling"):
        seen[key] = admit_work(
            session, task_id=task_id, revision=1, work_key=key, specification={"agent_type": "explore", "goal": key}
        )

    class WorkerModel:
        def next(self, messages):
            scope = current_scope()
            assert scope.parent_task_id == worker_scope.task_id
            assert scope.journal is worker_scope.journal
            assert scope.budget is worker_scope.budget
            assert not scope.allow_background_processes
            with scope.budget.request(scope, {}):
                if any("Use settings.py" in message["content"] for message in messages):
                    return AgentStep(type="assistant", content="Continued with the directed reply.")
                if messages[1]["content"] == "sibling":
                    sibling_started.set()
                    assert release_sibling.wait(5)
                    return AgentStep(type="assistant", content="Sibling complete.")
                assert sibling_started.wait(5)
                return AgentStep(
                    type="tool_calls",
                    calls=[
                        {
                            "id": "ask",
                            "toolName": "agent_report",
                            "input": {
                                "status": "blocked",
                                "summary": "Need target.",
                                "question": "Which file?",
                                "required": True,
                            },
                        }
                    ],
                )

    def coordinate(current, tools, attempt_id):
        assert current_scope() is worker_scope
        if attempt_id is None:
            selected = inbox.take_steering()
            assert len(selected) == 1 and selected[0].answer_to
            admit_steering(selected)
            inbox.acknowledge(selected)
            resumed = tools.execute(
                "task_retry", {"attempt_id": seen["question"]["attempt_id"]}, ToolContext(current.workspace)
            )
            assert resumed.ok, resumed.output
            assert resumed.details["revision"] == 2
            assert not release_sibling.is_set()
            release_sibling.set()
            return
        assert len(deliver_pending(current)) == 1
        if attempt_id == seen["question"]["attempt_id"]:
            assert sibling_started.is_set() and not release_sibling.is_set()
            assert current.task_control["questions"][0]["question"] == "Which file?"
            assert "Which file?" in current.messages[-1]["content"]
            if live_reply:
                inbox.bind_session(current)
                inbox.enqueue("Use settings.py", answer_to=(current.task_control["questions"][0]["id"],))
            else:
                release_sibling.set()

    worker = WorkerExecutor(worker_scope, ToolRegistry([]), model_factory=lambda _: WorkerModel())
    run_pending(session.session_id, worker=worker, coordinate=coordinate, input_queue=inbox)
    recovered = sessions.load_session(session.session_id)
    state = recovered.task_control["orchestration"]
    if live_reply:
        assert [item["state"] for item in state["work"]] == ["waiting_user", "stale", "result_ready"]
        assert recovered.task_control["questions"][0]["status"] == "answered"
        assert "Continued with the directed reply." in state["work"][-1]["summary"]
    else:
        assert [item["state"] for item in state["work"]] == ["waiting_user", "result_ready"]
        assert recovered.agent_traces.get(seen["sibling"]["trace_id"]).status == "completed"
    assert worker_scope.budget.requests == (3 if live_reply else 2)
    assert recovered.execution_state["journal"] == str(worker_scope.journal.path)
    assert recovered.task_control["questions"][0]["trace_id"] == seen["question"]["trace_id"]
    assert all(message.get("internal") for message in recovered.messages)


def test_managed_global_stop_cancels_queue_and_skips_result_callback(session, worker_scope):
    task_id = session.task_control["task"]["id"]
    for key in ("one", "two"):
        admit_work(
            session, task_id=task_id, revision=1, work_key=key, specification={"agent_type": "explore", "goal": key}
        )

    class WorkerModel:
        def next(self, messages):
            assert messages[1]["content"] == "one"
            worker_scope.cancellation_event.set()
            current_scope().check_cancelled()

    def unexpected(*args):
        pytest.fail("Global stop triggered a coordinator callback")

    worker = WorkerExecutor(worker_scope, ToolRegistry([]), model_factory=lambda _: WorkerModel())
    run_pending(session.session_id, worker=worker, coordinate=unexpected, max_workers=1)
    work = sessions.load_session(session.session_id).task_control["orchestration"]["work"]
    assert [item["state"] for item in work] == ["cancelled", "cancelled"]


def test_targeted_stop_only_cancels_selected_live_worker(session, worker_scope):
    target_started, sibling_started, release_sibling = Event(), Event(), Event()
    task_id = session.task_control["task"]["id"]
    inbox = TurnInputQueue()
    items = {
        key: admit_work(
            session, task_id=task_id, revision=1, work_key=key, specification={"agent_type": "explore", "goal": key}
        )
        for key in ("coordinator trigger", "target", "sibling")
    }

    class WorkerModel:
        def next(self, messages):
            goal = messages[1]["content"]
            if goal == "target":
                target_started.set()
                assert current_scope().cancellation_event.wait(8)
                current_scope().check_cancelled()
            elif goal == "sibling":
                sibling_started.set()
                assert release_sibling.wait(8)
                current_scope().check_cancelled()
            else:
                assert target_started.wait(8) and sibling_started.wait(8)
            return AgentStep(type="assistant", content="Done.")

    def coordinate(current, tools, attempt_id):
        deliver_pending(current)
        if attempt_id == items["coordinator trigger"]["attempt_id"]:
            arguments = {"attempt_id": items["target"]["attempt_id"]}
            assert inbox.stop_worker(items["target"]["trace_id"]) == "stop_requested"
            for _ in range(2):
                result = tools.execute("task_cancel", arguments, ToolContext(current.workspace))
                assert result.ok and result.details["state"] == "stop_requested"
        elif attempt_id == items["target"]["attempt_id"]:
            release_sibling.set()

    worker = WorkerExecutor(worker_scope, ToolRegistry([]), model_factory=lambda _: WorkerModel())
    run_pending(session.session_id, worker=worker, coordinate=coordinate, input_queue=inbox)
    work = sessions.load_session(session.session_id).task_control["orchestration"]["work"]
    assert [item["state"] for item in work] == ["result_ready", "cancelled", "result_ready"]
    assert not worker_scope.cancellation_event.is_set()
    with pytest.raises(ValueError, match="No active orchestration"):
        inbox.stop_worker(items["sibling"]["trace_id"])


def test_managed_restart_restores_budget_from_durable_journal(session, worker_scope):
    worker_scope.budget.max_requests = 2
    worker_scope.budget.limit = 1
    worker = WorkerExecutor(worker_scope, ToolRegistry([]), model_factory=lambda _: None)

    def initial(current, tools, attempt_id):
        worker_scope.record("model/prepared", request_id="one", body={"model": "default"})
        worker_scope.record("model/settled", request_id="one", usage={"input_tokens": 100, "output_tokens": 20})
        worker_scope.record("model/prepared", request_id="two", body={"model": "default"})

    run_pending(session.session_id, worker=worker, coordinate=initial, start_turn=True)

    resumed = ExecutionScope(
        session.workspace,
        worker_scope.permissions,
        Event(),
        RequestBudget(CostTracker(), max_requests=100),
        ExecutionJournal(worker_scope.journal.path.with_name("fresh.jsonl")),
    )

    def verify(current, tools, attempt_id):
        assert resumed.journal.path == worker_scope.journal.path
        assert resumed.budget.requests == resumed.budget.max_requests == 2
        assert resumed.budget.limit == 1 and resumed.budget.unmetered
        assert resumed.budget.tracker.total_cost_usd > 0
        with pytest.raises(RuntimeError, match="usage unavailable"):
            resumed.budget.check()

    restarted = WorkerExecutor(resumed, ToolRegistry([]), model_factory=lambda _: None)
    run_pending(session.session_id, worker=restarted, coordinate=verify, start_turn=True)


def test_directed_reply_retries_only_matching_worker_at_new_revision(session):
    task_id = session.task_control["task"]["id"]
    items = [
        admit_work(
            session, task_id=task_id, revision=1, work_key=key, specification={"agent_type": "explore", "goal": key}
        )
        for key in ("one", "two")
    ]
    for item in items:
        claim_launch(session, generation="run")
        settle_launch(
            session, attempt_id=item["attempt_id"], generation="run", outcome="waiting_user", summary="Partial work"
        )
    with edit_control(session=session) as control:
        control["task"]["status"] = "waiting_user"
        control["questions"] = [
            {
                "id": f"question-{index}",
                "task_id": task_id,
                "revision": 1,
                "trace_id": item["trace_id"],
                "question": f"Which file for {index}?",
                "required": True,
                "status": "pending",
            }
            for index, item in enumerate(items)
        ]
    with recording_session(session):
        task = begin_task(
            [
                {
                    "role": "user",
                    "content": "Use app.py",
                    "source": {"kind": "human", "id": "reply", "answerTo": ["question-0"]},
                }
            ],
            {},
        )
    assert task["id"] == task_id and task["revision"] == 2
    args = {"task_id": task_id, "revision": 2}
    continued = retry_work(session, attempt_id=items[0]["attempt_id"], **args)
    assert retry_work(session, attempt_id=items[0]["attempt_id"], **args) == continued
    assert continued["work_id"] == items[0]["work_id"] and continued["trace_id"] != items[0]["trace_id"]
    assert continued["continuation"] == [{"question": "Which file for 0?", "answer": "Use app.py"}]
    with pytest.raises(ValueError, match="directed answer"):
        retry_work(session, attempt_id=items[1]["attempt_id"], **args)


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_verified_session_wakes_for_steering_before_worker_finishes(session, worker_scope, monkeypatch, runner):
    inbox = TurnInputQueue()
    inbox.bind_session(session)
    inbox.start()
    release = Event()

    class Model:
        def next(self, messages):
            scope = current_scope()
            if scope.parent_task_id:
                inbox.enqueue("Keep this investigation read-only.")
                assert release.wait(8)
                return AgentStep(type="assistant", content="Old result.")
            if any(message.get("source", {}).get("kind") == "steering" for message in messages):
                assert not release.is_set()
                release.set()
                return AgentStep(type="assistant", content="Steering received.")
            if any(message.get("role") == "tool_result" for message in messages):
                return AgentStep(type="assistant", content="Queued.")
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "dispatch",
                        "toolName": "task_dispatch",
                        "input": {"work_key": "one", "goal": "Inspect", "agent_type": "explore"},
                    }
                ],
            )

        def next_stream(self, messages):
            step = self.next(messages)
            if step.type == "tool_calls":
                for call in step.calls:
                    yield StreamToken(
                        type="tool_use",
                        tool_id=call["id"],
                        tool_name=call["toolName"],
                        tool_input_partial=json.dumps(call["input"]),
                    )
                yield StreamToken(type="done", stop_reason="tool_use")
            else:
                yield StreamToken(type="text", content=step.content)
                yield StreamToken(type="done", stop_reason="end_turn")

    monkeypatch.setattr("pepsicode.orchestration.interactive.MockModelAdapter", Model)
    with recording_session(session):
        messages = run_verified_turn(
            runtime={"orchestration": {"enabled": True}, "verificationMode": "off"},
            runner=runner,
            input_queue=inbox,
            model=Model(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "What is in this project?"}],
            cwd=session.workspace,
            permissions=worker_scope.permissions,
        )
    assert release.is_set()
    assert any(message.get("content") == "Steering received." for message in messages)
    assert session.task_control["orchestration"]["work"][0]["state"] == "stale"


def test_interactive_continue_closes_confirmed_claim_without_replaying_it(session, worker_scope, monkeypatch):
    task_id = session.task_control["task"]["id"]
    worker = WorkerExecutor(worker_scope, ToolRegistry([]), model_factory=lambda _: None)
    run_pending(session.session_id, worker=worker, coordinate=lambda *_: None)
    session = sessions.load_session(session.session_id)
    old = admit_work(
        session, task_id=task_id, revision=1, work_key="old", specification={"agent_type": "explore", "goal": "Old"}
    )
    claim_launch(session, generation="terminated-host")

    class Model:
        def next(self, messages):
            assert current_scope().parent_task_id is None
            return AgentStep(type="assistant", content="Resumed after recovery.")

    monkeypatch.setattr("pepsicode.orchestration.interactive.MockModelAdapter", Model)
    with recording_session(session):
        messages = run_verified_turn(
            runtime={"orchestration": {"enabled": True}, "verificationMode": "off"},
            runner=run_agent_turn,
            model=Model(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "继续"}],
            cwd=session.workspace,
            permissions=worker_scope.permissions,
        )
    state = sessions.load_session(session.session_id).task_control["orchestration"]
    assert len(state["work"]) == 1
    assert state["work"][0]["attempt_id"] == old["attempt_id"]
    assert state["work"][0]["outcome"] == "interrupted"
    assert any(message.get("content") == "Resumed after recovery." for message in messages)


@pytest.mark.parametrize("committed", [False, True])
def test_continue_uses_only_committed_dispatch_receipts(session, worker_scope, monkeypatch, committed):
    task_id = session.task_control["task"]["id"]
    arguments = {"work_key": "accepted", "goal": "Inspect", "agent_type": "explore"}
    worker = WorkerExecutor(worker_scope, ToolRegistry([]), model_factory=lambda _: None)

    def interrupt(current, tools, attempt_id):
        record = worker_scope.record

        def lose_ack(event, **fields):
            if event == "tool/settled":
                raise OSError("Lost journal acknowledgement")
            record(event, **fields)

        if committed:
            with monkeypatch.context() as patch:
                patch.setattr(worker_scope, "record", lose_ack)
                result = tools.execute(
                    "task_dispatch", arguments, ToolContext(current.workspace, tool_use_id="dispatch-call")
                )
            assert result.status == "unknown"
        else:
            worker_scope.record(
                "tool/started",
                call_id="dispatch-call",
                tool="task_dispatch",
                arguments=["accepted", {"agent_type": "explore", "goal": "Inspect"}],
            )
            worker_scope.mark_unknown("dispatch-call", tool="task_dispatch")

    run_pending(session.session_id, worker=worker, coordinate=interrupt, start_turn=True)
    assert ExecutionJournal.interrupted_calls(worker_scope.journal.path)
    session = sessions.load_session(session.session_id)
    before = session.task_control["orchestration"]["work"]
    launches = []

    class Model:
        def next(self, messages):
            if current_scope().parent_task_id:
                launches.append(current_scope().trace_id)
            return AgentStep(type="assistant", content="Inspection finished.")

    monkeypatch.setattr("pepsicode.orchestration.interactive.MockModelAdapter", Model)
    with recording_session(session):
        messages = run_verified_turn(
            runtime={"orchestration": {"enabled": True}, "verificationMode": "off"},
            runner=run_agent_turn,
            model=Model(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "继续"}],
            cwd=session.workspace,
            permissions=worker_scope.permissions,
        )
    task = session.task_control["task"]
    assert task["id"] == task_id
    if committed:
        assert task["revision"] == 1
        assert launches == [before[0]["trace_id"]]
        assert before[0]["state"] == "result_ready"
        assert not ExecutionJournal.interrupted_calls(worker_scope.journal.path)
    else:
        assert not launches
        assert ExecutionJournal.interrupted_calls(worker_scope.journal.path)
        assert messages[-1]["isError"]


@pytest.mark.parametrize("creates_artifact", [False, True])
def test_interactive_worker_report_cannot_replace_acceptance(session, worker_scope, monkeypatch, creates_artifact):
    class Model:
        def next(self, messages):
            had_tool = any(message["role"] == "tool_result" for message in messages)
            if current_scope().parent_task_id:
                if creates_artifact and not had_tool:
                    return AgentStep(
                        type="tool_calls",
                        calls=[
                            {
                                "id": "write",
                                "toolName": "write_file",
                                "input": {"path": "feature.py", "content": "value = 42\n"},
                            }
                        ],
                    )
                return AgentStep(type="assistant", content="Work completed.")
            if had_tool:
                return AgentStep(type="assistant", content="Worker returned.")
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "dispatch",
                        "toolName": "task_dispatch",
                        "input": {"work_key": "one", "goal": "Create feature.py", "agent_type": "general"},
                    }
                ],
            )

    monkeypatch.setattr("pepsicode.orchestration.interactive.MockModelAdapter", Model)
    tools = ToolRegistry([write_file_tool])
    with recording_session(session):
        run_verified_turn(
            runtime={"orchestration": {"enabled": True}, "verificationMode": "off", "maxIterations": 1},
            runner=run_agent_turn,
            model=Model(),
            tools=tools,
            messages=[{"role": "user", "content": "Implement feature.py"}],
            cwd=session.workspace,
            permissions=worker_scope.permissions,
            task_kind="change",
            acceptance=[{"id": "artifact", "kind": "file_contains", "path": "feature.py", "text": "value = 42"}],
        )
    assert session.task_control["orchestration"]["work"][0]["state"] == "result_ready"
    assert (session.task_control["task"]["status"] == "complete") is creates_artifact
    assert tools.mutation_count == int(creates_artifact)


def test_admission_is_durable_and_idempotent(session):
    first = dispatch(session)
    for _ in range(9):
        assert dispatch(session) == first
    state = sessions.load_session(session.session_id).task_control["orchestration"]
    assert len(state["work"]) == len(state["effects"]) == 1
    assert state["effects"][0]["attempt_id"] == first["attempt_id"]


def test_conflict_and_stale_revision_do_not_enqueue(session):
    dispatch(session)
    task_id = session.task_control["task"]["id"]
    with pytest.raises(ValueError, match="conflicts"):
        admit_work(session, task_id=task_id, revision=1, work_key="one", specification={"goal": "change"})
    with pytest.raises(ValueError, match="revision"):
        admit_work(session, task_id=task_id, revision=2, work_key="two", specification={})
    assert len(session.task_control["orchestration"]["work"]) == 1


def test_failed_commit_rolls_back_work_and_effect(session, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sessions, "_atomic_write", fail)
    with pytest.raises(OSError, match="disk full"):
        dispatch(session)
    assert "orchestration" not in session.task_control


def test_completion_persists_one_event(session):
    attempt = dispatch(session)["attempt_id"]
    assert claim_launch(session, generation="first")["attempt_id"] == attempt
    assert claim_launch(session, generation="first") is None
    result = {"attempt_id": attempt, "outcome": "result_ready", "summary": "Observed"}
    assert not settle_launch(session, generation="other", **result)
    assert settle_launch(session, generation="first", **result)
    assert not settle_launch(session, generation="first", **result)
    state = sessions.load_session(session.session_id).task_control["orchestration"]
    assert state["work"][0]["summary"] == "Observed"
    assert [event["attempt_id"] for event in state["events"]] == [attempt]


def test_restart_fences_abandoned_claim_without_replaying(session):
    attempt = dispatch(session)["attempt_id"]
    claim_launch(session, generation="first")
    task_id = session.task_control["task"]["id"]
    admit_work(session, task_id=task_id, revision=1, work_key="two", specification={})
    session = sessions.load_session(session.session_id)
    assert recover_launches(session, generation="second") == [attempt]
    assert claim_launch(session, generation="second") is None
    assert not settle_launch(session, attempt_id=attempt, generation="first", outcome="failed", summary="Late")
    assert session.task_control["orchestration"]["work"][0]["state"] == "recovery_pending"
    assert session.task_control["orchestration"]["work"][1]["state"] == "queued"

    def unexpected(*args):
        pytest.fail("Recovery pending must prevent launch and coordinator wakeup")

    run_pending(session.session_id, execute=unexpected, on_result=unexpected)


def test_old_revision_does_not_fill_current_queue(session):
    dispatch(session, limit=1)
    with edit_control(session=session) as control:
        control["task"]["revision"] = 2
    task_id = session.task_control["task"]["id"]
    current = admit_work(session, task_id=task_id, revision=2, work_key="new", specification={}, limit=1)
    assert claim_launch(session, generation="first")["attempt_id"] == current["attempt_id"]
    assert session.task_control["orchestration"]["work"][0]["state"] == "stale"


def test_result_callback_runs_while_another_worker_is_active(session):
    first = dispatch(session)["attempt_id"]
    task_id = session.task_control["task"]["id"]
    admit_work(session, task_id=task_id, revision=1, work_key="two", specification={"goal": "second"})
    sibling_started = Event()
    release_sibling = Event()
    third = None

    def execute(item):
        if item["attempt_id"] == first:
            assert sibling_started.wait(5)
        elif item["work_key"] == "two":
            sibling_started.set()
            assert release_sibling.wait(5)
        return "result_ready", "Done"

    def on_result(current, attempt_id):
        nonlocal third
        assert len(deliver_pending(current)) == 1
        if attempt_id == first:
            assert len(current.task_control["orchestration"]["events"]) == 1
            third = admit_work(current, task_id=task_id, revision=1, work_key="three", specification={})["attempt_id"]
        if attempt_id == third:
            release_sibling.set()

    run_pending(session.session_id, execute=execute, on_result=on_result, is_read_only=lambda item: True)
    recovered = sessions.load_session(session.session_id)
    assert len(recovered.task_control["orchestration"]["events"]) == 3
    assert len(recovered.messages) == 3
    assert deliver_pending(recovered) == []


def test_coordinator_tool_queues_work_and_runs_again_before_sibling_finishes(session):
    sibling_started = Event()
    release_sibling = Event()
    queued = {}
    expired_tools = None

    def dispatch(current, tools, keys, *, event=False):
        assert {tool.name for tool in tools.list()} == {
            "task_dispatch",
            "agent_status",
            "task_integrate",
            "task_deliver",
        }

        class ScriptedModel:
            def __init__(self):
                self.started = False

            def next(self, messages):
                if self.started:
                    return AgentStep(type="assistant", content="Assignments queued.")
                self.started = True
                if event:
                    assert "Observed" in messages[-1]["content"]
                return AgentStep(
                    type="tool_calls",
                    calls=[
                        {
                            "id": key,
                            "toolName": "task_dispatch",
                            "input": {"work_key": key, "goal": f"Inspect {key}", "agent_type": "explore"},
                        }
                        for key in keys
                    ],
                )

        before = len(current.messages)
        with bind_scope(ExecutionScope(current.workspace)):
            result = run_agent_turn(
                model=ScriptedModel(), tools=tools, messages=current.messages, cwd=current.workspace
            )
        receipts = [json.loads(message["content"]) for message in result[before:] if message["role"] == "tool_result"]
        assert len(receipts) == len(keys)
        assert all(receipt["accepted"] and receipt["state"] == "queued" for receipt in receipts)
        queued.update(zip(keys, (receipt["attempt_id"] for receipt in receipts), strict=True))

    def coordinate(current, tools, attempt_id):
        nonlocal expired_tools
        if attempt_id is None:
            expired_tools = (current, tools)
            dispatch(current, tools, ["first", "second"])
            assert not sibling_started.is_set()
            status = tools.execute("agent_status", {}, ToolContext(current.workspace))
            status_view = json.loads(status.output)
            assert len(status_view["records"]) == 2
            assert all(row["stage"] == "queued" and row["started_at"] == 0 for row in status_view["records"])
            rows = status_view["orchestration_work"]["records"]
            assert [(row["attempt_id"], row["state"]) for row in rows] == [
                (queued["first"], "queued"),
                (queued["second"], "queued"),
            ]
            return
        assert len(deliver_pending(current)) == 1
        if attempt_id == queued["first"]:
            assert sibling_started.is_set() and not release_sibling.is_set()
            dispatch(current, tools, ["follow-up"], event=True)
        elif attempt_id == queued.get("follow-up"):
            release_sibling.set()

    def execute(item):
        if item["work_key"] == "first":
            assert sibling_started.wait(5)
        if item["work_key"] == "second":
            sibling_started.set()
            assert release_sibling.wait(5)
        return "result_ready", "Observed"

    run_pending(
        session.session_id, execute=execute, coordinate=coordinate, start_turn=True, is_read_only=lambda _: True
    )
    assert len(queued) == 3
    recovered = sessions.load_session(session.session_id)
    assert sum(message.get("source", {}).get("kind") == "orchestration-event" for message in recovered.messages) == 3
    current, tools = expired_tools
    with recording_session(current):
        result = tools.execute(
            "task_dispatch",
            {"work_key": "late", "goal": "Inspect late", "agent_type": "explore"},
            ToolContext(session.workspace),
        )
    assert not result.ok and result.status == "blocked"


@pytest.mark.parametrize("mode", ["read_only", "plan"])
def test_coordinator_dispatch_rejects_restricted_permissions(session, mode):
    def coordinate(current, tools, attempt_id):
        permissions = PermissionManager(current.workspace, mode=mode)
        try:
            result = tools.find("task_dispatch").run(
                ("one", {"goal": "Inspect", "agent_type": "explore"}),
                ToolContext(current.workspace, permissions=permissions),
            )
            assert not result.ok and result.status == "blocked"
        finally:
            permissions.close()

    def execute(item):
        pytest.fail("Restricted dispatch started a worker")

    run_pending(session.session_id, execute=execute, coordinate=coordinate, start_turn=True)
    assert "orchestration" not in sessions.load_session(session.session_id).task_control


def test_coordinator_dispatch_reports_commit_failure_without_starting_worker(session, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    def coordinate(current, tools, attempt_id):
        with monkeypatch.context() as patch:
            patch.setattr(sessions, "_atomic_write", fail)
            result = tools.execute(
                "task_dispatch",
                {"work_key": "one", "goal": "Inspect", "agent_type": "explore"},
                ToolContext(current.workspace),
            )
        assert not result.ok and result.status == "failed"
        assert result.details is None

    def execute(item):
        pytest.fail("Uncommitted dispatch started a worker")

    run_pending(session.session_id, execute=execute, coordinate=coordinate, start_turn=True)
    assert "orchestration" not in sessions.load_session(session.session_id).task_control


def test_revision_change_archives_late_result_without_notifying_coordinator(session):
    first = dispatch(session)["attempt_id"]
    task_id = session.task_control["task"]["id"]
    admit_work(session, task_id=task_id, revision=1, work_key="two", specification={})
    revised = Event()
    delivered = []

    def execute(item):
        if item["attempt_id"] != first:
            assert revised.wait(5)
        return "result_ready", "Done"

    def on_result(current, attempt_id):
        assert attempt_id == first
        delivered.extend(deliver_pending(current))
        with edit_control(session=current) as control:
            control["task"]["revision"] = 2
        revised.set()

    run_pending(session.session_id, execute=execute, on_result=on_result, is_read_only=lambda _: True)
    recovered = sessions.load_session(session.session_id)
    state = recovered.task_control["orchestration"]
    assert len(delivered) == len(state["events"]) == 1
    assert state["work"][1]["state"] == "stale"
    assert state["work"][1]["outcome"] == "result_ready"


def test_failed_delivery_does_not_confirm_or_append_history(session, monkeypatch):
    attempt = dispatch(session)["attempt_id"]
    claim_launch(session, generation="first")
    settle_launch(session, attempt_id=attempt, generation="first", outcome="failed", summary="Observed failure")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(sessions, "_atomic_write", fail)
        with pytest.raises(OSError, match="disk full"):
            deliver_pending(session)
    assert not session.messages
    assert session.task_control["orchestration"]["events"][0]["state"] == "pending"
    recovered = sessions.load_session(session.session_id)
    assert len(deliver_pending(recovered)) == 1
    assert deliver_pending(recovered) == []


def test_second_orchestration_owner_is_refused(session):
    dispatch(session)
    with (
        own_session(session.session_id),
        pytest.raises(RuntimeError, match="ownership"),
        own_session(session.session_id),
    ):
        pytest.fail("Acquired the same session twice")


def test_scheduler_refuses_a_second_session_in_same_workspace(session):
    dispatch(session)
    other = sessions.create_new_session(session.workspace)
    with recording_session(other):
        begin_task([{"role": "user", "content": "Inspect the same project"}], {})
    dispatch(other)

    def unexpected(*args):
        pytest.fail("Contending scheduler ran a callback")

    def execute(item):
        with pytest.raises(RuntimeError, match="ownership for workspace"):
            run_pending(other.session_id, execute=unexpected, on_result=unexpected)
        return "result_ready", "Done"

    run_pending(session.session_id, execute=execute, on_result=lambda *_: None)
    assert sessions.load_session(other.session_id).task_control["orchestration"]["work"][0]["state"] == "queued"
    run_pending(other.session_id, execute=lambda _: ("result_ready", "Done"), on_result=lambda *_: None)
    assert sessions.load_session(other.session_id).task_control["orchestration"]["work"][0]["state"] == "result_ready"


def test_workspace_ownership_is_visible_to_another_process(tmp_path):
    probe = """
import sys
from pepsicode.orchestration.runtime import own_workspace
try:
    with own_workspace(sys.argv[1]):
        pass
except RuntimeError as error:
    sys.exit(str(error))
"""
    argv = [sys.executable, "-c", probe, str(tmp_path)]
    with own_workspace(tmp_path / "."):
        blocked = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert blocked.returncode == 1
    assert "ownership for workspace" in blocked.stderr
    acquired = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert acquired.returncode == 0, acquired.stderr


def test_host_restart_delivers_committed_result_without_running_worker(session):
    attempt = dispatch(session)["attempt_id"]
    claim_launch(session, generation="old")
    settle_launch(session, attempt_id=attempt, generation="old", outcome="result_ready", summary="Already ran")

    def execute(item):
        pytest.fail("The completed attempt was executed again")

    def on_result(current, attempt_id):
        assert attempt_id == attempt
        assert len(deliver_pending(current)) == 1

    run_pending(session.session_id, execute=execute, on_result=on_result)
    recovered = sessions.load_session(session.session_id)
    assert recovered.task_control["orchestration"]["events"][0]["state"] == "delivered"


def test_host_restart_drains_multiple_result_batches(session):
    task_id = session.task_control["task"]["id"]
    for number in range(9):
        item = admit_work(session, task_id=task_id, revision=1, work_key=str(number), specification={})
        claim_launch(session, generation="old")
        settle_launch(session, attempt_id=item["attempt_id"], generation="old", outcome="result_ready", summary="Done")
    batches = []

    def execute(item):
        pytest.fail("The completed attempt was executed again")

    run_pending(
        session.session_id,
        execute=execute,
        on_result=lambda current, _: batches.append(deliver_pending(current)),
    )
    assert [len(batch) for batch in batches] == [8, 1]
    recovered = sessions.load_session(session.session_id)
    assert len(recovered.messages) == 9
    assert deliver_pending(recovered) == []


def test_executor_failure_preserves_confirmed_sibling_result(session):
    first = dispatch(session)["attempt_id"]
    task_id = session.task_control["task"]["id"]
    second = admit_work(session, task_id=task_id, revision=1, work_key="two", specification={})["attempt_id"]

    def execute(item):
        if item["attempt_id"] == first:
            raise RuntimeError("lost worker response")
        return "result_ready", "Sibling completed"

    with pytest.raises(RuntimeError, match="lost worker response"):
        run_pending(session.session_id, execute=execute, on_result=lambda *_: None, is_read_only=lambda _: True)
    recovered = sessions.load_session(session.session_id)
    assert [event["attempt_id"] for event in recovered.task_control["orchestration"]["events"]] == [second]
    assert recover_launches(recovered, generation="new") == [first]
