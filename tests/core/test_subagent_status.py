import pytest

from pepsicode.agents.trace import TraceManager
from pepsicode.tooling import ToolContext
from pepsicode.tools.task import create_task_tool


@pytest.mark.parametrize(
    ("turn_status", "expected"),
    [
        ("completed", "completed"),
        ("cancelled", "cancelled"),
        ("timed_out", "timed_out"),
        ("max_steps", "blocked"),
        ("unknown", "blocked"),
    ],
)
def test_child_status_matches_result_and_trace(monkeypatch, tmp_path, turn_status, expected):
    from pepsicode.core import agent_loop

    monkeypatch.setattr(
        agent_loop,
        "run_agent_turn",
        lambda **kwargs: [{"role": "assistant", "content": "result", "turnStatus": turn_status}],
    )
    trace = TraceManager()
    tool = create_task_tool(str(tmp_path), None, model_factory=lambda registry: object(), trace_manager=trace)
    result = tool.run({"agent_type": "explore", "task": "inspect"}, ToolContext(cwd=str(tmp_path)))
    assert result.status == expected
    assert result.ok is (expected == "completed")
    host_status = {"unknown": "unknown", "max_steps": "budget_exhausted"}.get(turn_status, expected)
    assert f"Explore {host_status}" in result.output
    assert trace.all_nodes()[0].status == host_status
    assert result.details["trace_id"] == trace.all_nodes()[0].trace_id


def test_model_setup_failure_does_not_report_unknown(tmp_path):
    def fail(_registry):
        raise RuntimeError("model setup failed")

    trace = TraceManager("session")
    tool = create_task_tool(str(tmp_path), None, model_factory=fail, trace_manager=trace)
    result = tool.run({"agent_type": "general", "task": "work"}, ToolContext(cwd=str(tmp_path)))
    assert not result.ok
    assert result.status == "failed"
    node = trace.all_nodes()[0]
    assert node.session_id == "session"
    assert node.task == "work"
    assert node.status == "failed"
    assert node.end_reason == "model setup failed"


def test_trace_reads_are_snapshots():
    trace = TraceManager("session")
    created = trace.create(agent_type="explore", name="Explore", parent_task_id="task", spawn_revision=2)
    created.name = "changed"
    trace.record_tokens(created.trace_id, 3, 4)
    snapshot = trace.get(created.trace_id)
    assert snapshot.name == "Explore"
    assert snapshot.parent_task_id == "task"
    assert snapshot.spawn_revision == 2
    snapshot.input_tokens = 999
    trace.all_nodes()[0].output_tokens = 999
    assert trace.get_total(created.trace_id)["input_tokens"] == 3
    assert trace.get_total(created.trace_id)["output_tokens"] == 4


def test_cancelled_turn_records_cancelled_status():
    from pepsicode.core.agent_loop import _finish_cancelled_turn

    messages = _finish_cancelled_turn([], None, ".", None, "subagent:Explore", None)
    assert messages[-1]["turnStatus"] == "cancelled"


def test_unknown_child_operation_is_not_duplicated(monkeypatch, tmp_path):
    from pepsicode.core import agent_loop
    from pepsicode.execution.runtime import ExecutionScope, bind_scope, current_scope
    from pepsicode.tooling import ToolRegistry

    def run(**kwargs):
        current_scope().mark_unknown("child-call", tool="run_command")
        return [{"role": "assistant", "content": "Inspect child-call", "turnStatus": "unknown"}]

    monkeypatch.setattr(agent_loop, "run_agent_turn", run)
    tool = create_task_tool(str(tmp_path), None, model_factory=lambda registry: object())
    scope = ExecutionScope(str(tmp_path))
    with bind_scope(scope):
        result = ToolRegistry([tool]).execute(
            "task", {"agent_type": "general", "task": "work"}, ToolContext(str(tmp_path), tool_use_id="parent-call")
        )
    assert result.status == "blocked"
    assert list(scope.unresolved) == ["child-call"]


def test_child_journal_isolates_reused_tool_ids(tmp_path):
    from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope

    journal = ExecutionJournal(tmp_path / "calls.jsonl")
    parent = ExecutionScope(str(tmp_path), journal=journal)
    first, second = parent.child(), parent.child()
    first.trace_id, second.trace_id = "first", "second"
    first.record("tool/started", call_id="same", tool="read_file")
    second.record("tool/started", call_id="same", tool="run_command")
    first.record("tool/settled", call_id="same", state="completed")
    second.mark_unknown("same", tool="run_command")
    second.record("tool/settled", call_id="same", state="unknown")

    pending = journal.interrupted_calls(journal.path)
    assert [(row["trace_id"], row["call_id"], row["tool_use_id"]) for row in pending] == [
        ("second", "second:same", "same")
    ]
    assert list(parent.unresolved) == ["second:same"]


def test_parallel_activity_removal_and_ready_projection():
    from pepsicode.agents.status import observe_activity, trace_view
    from pepsicode.agents.trace import AgentReport

    manager = TraceManager("session")
    first, second = [
        manager.create(agent_type="explore", name="Explore", parent_task_id="task", spawn_revision=2) for _ in range(2)
    ]
    for node in (first, second):
        for call in ("a", "b"):
            observe_activity(
                manager,
                node.trace_id,
                "tool/call",
                {
                    "tool": "read_file",
                    "call_id": call,
                    "input": {"path": f"{call}.txt"},
                },
            )
    observe_activity(
        manager,
        first.trace_id,
        "tool/result",
        {
            "tool": "read_file",
            "call_id": "a",
            "input": {"path": "a.txt"},
            "output": "output",
            "status": "completed",
            "duration_ms": 10,
        },
    )
    assert [activity.id for activity in manager.get(first.trace_id).activities] == ["b"]
    assert [activity.id for activity in manager.get(second.trace_id).activities] == ["a", "b"]
    manager.record_report(first.trace_id, AgentReport("r", "ready", "Inspected files", 2, False))
    manager.complete(first.trace_id, reason="Child returned")
    manager.activity(first.trace_id, "late", manager.get(second.trace_id).activities[0])
    task = {
        "id": "task",
        "revision": 2,
        "status": "blocked",
        "criteria": [{"id": "check"}],
        "acceptance": {"valid": True, "task_revision": 1, "acceptance_run_id": "old", "workspace": {"digest": "abc"}},
        "evidence": [
            {
                "criterion_id": "check",
                "status": "satisfied",
                "task_revision": 1,
                "acceptance_run_id": "old",
                "workspace_digest": "abc",
            }
        ],
    }
    rows = {row["trace_id"]: row for row in trace_view(manager, {"task": task})["records"]}
    assert rows[first.trace_id]["stage"] == "pending_verification"
    assert rows[first.trace_id]["activities"] == []
    assert rows[first.trace_id]["acceptance"]["passed"] == 0
    assert rows[first.trace_id]["acceptance"]["total"] == 1
    assert rows[first.trace_id]["recent_tools"][0]["output"] == "output"
    assert rows[second.trace_id]["stage"] == "running"
    current = {
        **task,
        "status": "complete",
        "acceptance": {**task["acceptance"], "task_revision": 2, "acceptance_run_id": "current"},
        "evidence": [{**task["evidence"][0], "task_revision": 2, "acceptance_run_id": "current"}],
    }
    verified = trace_view(manager, {"task": current}, trace_id=first.trace_id)["records"][0]
    assert verified["stage"] == "verified"
    assert verified["acceptance"]["passed"] == verified["acceptance"]["total"] == 1
    assert verified["status"] == "completed"
    invalid = {**current, "evidence": [{**current["evidence"][0], "workspace_digest": "outdated"}]}
    assert (
        trace_view(manager, {"task": invalid}, trace_id=first.trace_id)["records"][0]["stage"] == "pending_verification"
    )
    stale = trace_view(manager, {"task": {**task, "revision": 3}}, trace_id=first.trace_id)["records"][0]
    assert stale["stage"] == "stale_result"
    assert stale["report"]["revision"] == 2


@pytest.mark.parametrize("mode", ["auto", "plan", "read_only"])
def test_worker_reports_question_once_and_keeps_working(tmp_path, monkeypatch, mode):
    import json

    from pepsicode.agents.notices import deliver_agent_notices
    from pepsicode.agents.status import session_trace_view
    from pepsicode.core import session as sessions
    from pepsicode.core.session_tree import record_messages, recording_session
    from pepsicode.core.task_control import begin_task
    from pepsicode.execution.runtime import ExecutionScope, bind_scope
    from pepsicode.permissions import PermissionManager
    from pepsicode.tooling import ToolRegistry
    from pepsicode.tools.agent_status import create_agent_status
    from pepsicode.types import AgentStep

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    session = sessions.create_new_session(str(tmp_path))
    (tmp_path / "sample.txt").write_text("independent check", encoding="utf-8")
    messages = [{"role": "user", "content": "Inspect the project"}]
    record_messages(session, messages)
    permissions = PermissionManager(str(tmp_path))
    if mode == "plan":
        permissions.enter_plan_mode()
    else:
        permissions.set_access_mode(mode)
    steps = iter(
        AgentStep(type="tool_calls", calls=[{"id": key, "toolName": tool, "input": data}])
        for key, tool, data in [
            (
                "same",
                "agent_report",
                {"status": "progress", "summary": "Started", "question": "Optional?", "required": False},
            ),
            ("read", "read_file", {"path": "sample.txt"}),
            (
                "same",
                "agent_report",
                {"status": "progress", "summary": "Started", "question": "Optional?", "required": False},
            ),
            ("different", "agent_report", {"status": "blocked", "summary": "Need a choice", "question": "Required?"}),
        ]
    )

    class Model:
        last_usage = {"input_tokens": 5, "output_tokens": 2}

        def next(self, _messages):
            return next(steps)

    task_tool = create_task_tool(str(tmp_path), None, model_factory=lambda registry: Model())
    with recording_session(session), bind_scope(ExecutionScope(str(tmp_path), permissions)):
        begin_task(messages, {})
        result = task_tool.run({"agent_type": "explore", "task": "inspect"}, ToolContext(str(tmp_path)))
        assert result.awaitUser
        assert result.status == "awaiting_user"
        row = session_trace_view(session)["records"][0]
        assert row["stage"] == "waiting_user"
        assert row["input_tokens"] == 20
        assert row["tool_call_count"] == 4
        assert any("independent check" in item["output"] for item in row["recent_tools"])
        assert len(row["questions"]) == 2
        assert all(q["actionable"] for q in row["questions"])
        query = ToolRegistry([create_agent_status(session.agent_traces)]).execute(
            "agent_status", {"trace_id": row["trace_id"]}, ToolContext(str(tmp_path), permissions=permissions)
        )
        assert query.ok
        assert json.loads(query.output)["records"][0]["stage"] == "waiting_user"
        deliver_agent_notices(messages)
        count = len(messages)
        assert count > 1
        deliver_agent_notices(messages)
        assert len(messages) == count

        question = next(q for q in row["questions"] if q["required"])
        messages.append({"role": "user", "content": "Answer", "source": {"id": "reply", "answerTo": [question["id"]]}})
        begin_task(messages, {})
        row = session_trace_view(session)["records"][0]
        assert row["stage"] == "awaiting_continuation"
        assert row["stale"]
        assert [q["status"] for q in row["questions"]] == ["pending", "answered"]

    loaded = sessions.load_session(session.session_id)
    row = session_trace_view(loaded)["records"][0]
    assert row["trace_id"] == result.details["trace_id"]
    assert row["questions"][-1]["answer_input_id"] == "reply"


def test_running_trace_is_interrupted_on_reload(tmp_path, monkeypatch):
    from pepsicode.agents.status import session_trace_view
    from pepsicode.core import session as sessions

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    session = sessions.create_new_session(str(tmp_path))
    done = session.agent_traces.create(agent_type="general", name="Worker")
    session.agent_traces.complete(done.trace_id, result_summary="Preserved result")
    running = session.agent_traces.create(agent_type="general", name="Worker")
    session.agent_traces.set_scope(running.trace_id, "scope")
    sessions.save_session(session)
    loaded = sessions.load_session(session.session_id)
    rows = {row["trace_id"]: row for row in session_trace_view(loaded)["records"]}
    assert rows[running.trace_id]["stage"] == "recovery_pending"
    assert rows[running.trace_id]["activities"] == []
    assert rows[done.trace_id]["stage"] == "completed"
    assert rows[done.trace_id]["result_summary"] == "Preserved result"


def test_report_save_failure_rolls_back_and_next_session_commit_clears_error(tmp_path, monkeypatch):
    from pepsicode.agents.status import session_trace_view
    from pepsicode.core import session as sessions
    from pepsicode.core.session_tree import recording_session
    from pepsicode.execution.runtime import ExecutionScope, bind_scope
    from pepsicode.tools.agent_report import create_agent_report

    def fail(*_args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    session = sessions.create_new_session(str(tmp_path))
    manager = session.agent_traces
    trace_id = manager.create(agent_type="general", name="General").trace_id
    parent = ExecutionScope(str(tmp_path))
    with recording_session(session), bind_scope(parent):
        tool = create_agent_report(manager, trace_id, ToolContext(str(tmp_path), execution=parent))
        child = parent.child()
        child.trace_id = trace_id
        with bind_scope(child), monkeypatch.context() as failure:
            failure.setattr(sessions, "_atomic_write", fail)
            result = tool.run(
                tool.validator({"status": "blocked", "summary": "Need input", "question": "Which file?"}),
                ToolContext(str(tmp_path), execution=child, tool_use_id="report"),
            )

    assert not result.ok
    assert not result.awaitUser
    assert "questions" not in session.task_control
    assert manager.get(trace_id).reports == ()
    failed = session_trace_view(session)
    assert failed["persistence_error"] == "disk unavailable"
    sessions.save_session(session)
    recovered = session_trace_view(session)
    assert recovered["persistence_error"] is None
    assert recovered["sequence"] == failed["sequence"] + 1
    sessions.save_session(session)
    assert session_trace_view(session)["sequence"] == recovered["sequence"]


def test_old_settled_traces_page_from_the_session_archive(tmp_path, monkeypatch):
    from pepsicode.agents import trace
    from pepsicode.agents.status import session_trace_view
    from pepsicode.core import session as sessions

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    monkeypatch.setattr(trace, "RECENT_TRACE_LIMIT", 2)
    session = sessions.create_new_session(str(tmp_path))
    manager = session.agent_traces
    ids = []
    for n in range(4):
        node = manager.create(agent_type="explore", name="Explore", task=str(n))
        manager.complete(node.trace_id, result_summary=f"result {n}")
        ids.append(node.trace_id)
        sessions.save_session(session)
    live = manager.create(agent_type="explore", name="Explore", task="current")
    sessions.save_session(session)
    assert {node.trace_id for node in manager.snapshot(include_history=False)[1]} == {*ids[-2:], live.trace_id}
    assert manager.get(ids[0]).result_summary == "result 0"
    reloaded = sessions.load_session(session.session_id)
    page = session_trace_view(reloaded, offset=3, limit=2)
    assert [row["trace_id"] for row in page["records"]] == ids[:2][::-1]
    assert page["next_offset"] is None


def test_retries_keep_distinct_history_and_ignore_late_events(tmp_path, monkeypatch):
    from pepsicode.agents.status import observe_activity, session_trace_view
    from pepsicode.core import agent_loop
    from pepsicode.core import session as sessions
    from pepsicode.core.session_tree import recording_session
    from pepsicode.core.task_control import begin_task, edit_control
    from pepsicode.execution.runtime import ExecutionScope, bind_scope
    from pepsicode.types import AgentStep

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    session = sessions.create_new_session(str(tmp_path))
    callbacks = []
    original = agent_loop.run_agent_turn

    def capture(**kwargs):
        callbacks.append(kwargs["on_tool_event"])
        return original(**kwargs)

    monkeypatch.setattr(agent_loop, "run_agent_turn", capture)

    class Model:
        def next(self, _messages):
            if len(callbacks) == 1:
                with edit_control(session=session) as control:
                    control["task"]["revision"] += 1
            return AgentStep(type="assistant", content="Finished inspection", kind="final")

    tool = create_task_tool(str(tmp_path), None, model_factory=lambda _: Model())
    with recording_session(session), bind_scope(ExecutionScope(str(tmp_path))):
        begin_task([{"role": "user", "content": "Inspect the project"}], {})
        results = [tool.run({"agent_type": "explore", "task": "Inspect"}, ToolContext(str(tmp_path))) for _ in range(3)]
    ids = [result.details["trace_id"] for result in results]
    assert len(set(ids)) == 3
    assert [result.status for result in results] == ["blocked", "completed", "completed"]
    before = session_trace_view(session)
    for callback in callbacks:
        callback("tool/call", {"call_id": "late", "tool": "read_file", "input": {"path": "late.txt"}})
        callback(
            "tool/result",
            {
                "call_id": "late",
                "tool": "read_file",
                "status": "failed",
                "output": "late error",
                "duration_ms": 1,
            },
        )
    observe_activity(session.agent_traces, ids[0], "process/started", {"persistent": True, "pid": 1234})
    observe_activity(session.agent_traces, ids[1], "background/end", {"pid": 1234})
    assert session_trace_view(session) == before
    rows = {row["trace_id"]: row for row in session_trace_view(sessions.load_session(session.session_id))["records"]}
    assert [rows[key]["spawn_revision"] for key in ids] == [1, 2, 2]
    assert rows[ids[0]]["stale"]
    assert {row["name"] for row in rows.values()} == {"Explore"}
    assert len({row["execution_scope_id"] for row in rows.values()}) == 3


def test_background_end_after_worker_exit_clears_the_original_activity():
    from pepsicode.agents.status import observe_activity, trace_view

    manager = TraceManager()
    trace_id = manager.create(agent_type="general", name="Worker").trace_id
    observe_activity(manager, trace_id, "process/started", {"persistent": True, "pid": 1234})
    manager.complete(trace_id)
    assert trace_view(manager, {})["records"][0]["stage"] == "running"
    observe_activity(manager, trace_id, "background/end", {"pid": 1234})
    assert trace_view(manager, {})["records"][0]["stage"] == "completed"


@pytest.mark.parametrize("scenario", ["cancelled", "timed_out", "budget_exhausted"])
def test_live_worker_stop_reason_is_preserved(tmp_path, scenario):
    import time

    from pepsicode.agents.status import trace_view
    from pepsicode.execution.runtime import ModelRequestLimitError, current_scope

    class Model:
        def next(self, _messages):
            scope = current_scope()
            if scenario == "budget_exhausted":
                raise ModelRequestLimitError("Worker request quota reached")
            if scenario == "cancelled":
                scope.cancellation_event.set()
            else:
                scope.deadline = time.monotonic() - 1
            scope.check_cancelled()

    trace = TraceManager()
    tool = create_task_tool(str(tmp_path), None, model_factory=lambda _: Model(), trace_manager=trace)
    result = tool.run({"agent_type": "explore", "task": "Inspect"}, ToolContext(str(tmp_path)))
    row = trace_view(trace, {})["records"][0]
    assert not result.ok
    assert row["stage"] == row["status"] == scenario
    assert row["activities"] == []
    assert row["end_reason"]


@pytest.mark.parametrize("timeout", [False, True])
def test_worker_command_outcomes_reach_trace_and_recovery(tmp_path, timeout):
    import sys

    from pepsicode.agents.status import trace_view
    from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope, bind_scope
    from pepsicode.permissions import PermissionManager, PermissionMode
    from pepsicode.types import AgentStep

    class Model:
        def next(self, messages):
            if any(m["role"] == "tool_result" for m in messages):
                return AgentStep(type="assistant", content="Command checked", kind="final")
            script = "import time; time.sleep(30)" if timeout else "import sys; print('check failed'); sys.exit(7)"
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "command",
                        "toolName": "run_command",
                        "input": {"command": sys.executable, "args": ["-c", script], "timeout": 1 if timeout else 15},
                    }
                ],
            )

    permissions = PermissionManager(str(tmp_path), mode=PermissionMode.FULL_ACCESS)
    journal = ExecutionJournal(tmp_path / "calls.jsonl")
    parent = ExecutionScope(str(tmp_path), permissions, journal=journal)
    trace = TraceManager()
    tool = create_task_tool(str(tmp_path), None, model_factory=lambda _: Model(), trace_manager=trace)
    with bind_scope(parent):
        result = tool.run({"agent_type": "general", "task": "Check command"}, ToolContext(str(tmp_path)))
    row = trace_view(trace, {}, unresolved=parent.snapshot()["unknown_trace_ids"])["records"][0]
    preview = row["recent_tools"][0]
    if timeout:
        assert result.status == "blocked"
        assert row["status"] == "unknown"
        assert row["stage"] == "recovery_pending"
        assert preview["status"] == "timed_out"
        assert [call["trace_id"] for call in journal.interrupted_calls(journal.path)] == [row["trace_id"]]
    else:
        assert result.ok
        assert row["status"] == "completed"
        assert preview["status"] == "failed"
        assert "check failed" in preview["output"]
        assert not journal.interrupted_calls(journal.path)


def test_long_session_bounds_previews_notifications_and_live_records(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    from pepsicode.agents import trace
    from pepsicode.agents.notices import deliver_agent_notices
    from pepsicode.agents.status import PAGE_SIZE, observe_activity, session_trace_view
    from pepsicode.core import session as sessions
    from pepsicode.core.session_tree import recording_session

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    clock = time.time()
    monkeypatch.setattr(trace, "time", SimpleNamespace(time=lambda: clock))
    session = sessions.create_new_session(str(tmp_path))
    manager = session.agent_traces
    live = manager.create(agent_type="explore", name="Long-running Worker")
    for i in range(512):
        clock += 30
        call = {"call_id": str(i), "tool": "read_file"}
        observe_activity(manager, live.trace_id, "tool/call", {**call, "input": {"path": "x" * 5000}})
        observe_activity(
            manager,
            live.trace_id,
            "tool/result",
            {
                **call,
                "status": "completed",
                "output": "y" * 5000,
                "duration_ms": 1,
            },
        )
    history = []
    for _ in range(trace.RECENT_TRACE_LIMIT + 4):
        node = manager.create(agent_type="explore", name="Explore")
        history.append(node.trace_id)
        manager.complete(node.trace_id, result_summary="z" * 5000)
    sessions.save_session(session)
    assert len(manager.snapshot(include_history=False)[1]) == trace.RECENT_TRACE_LIMIT + 1
    view = session_trace_view(session)
    assert len(view["records"]) == view["next_offset"] == PAGE_SIZE
    detail = session_trace_view(session, trace_id=live.trace_id)["records"][0]
    assert detail["elapsed_seconds"] >= 4 * 3600
    assert detail["tool_call_count"] == 512
    assert detail["activities"] == []
    assert [preview["id"] for preview in detail["recent_tools"]] == [str(i) for i in range(504, 512)]
    assert all(len(p["input_preview"]) == len(p["output"]) == 500 for p in detail["recent_tools"])
    messages = []
    with recording_session(session):
        deliver_agent_notices(messages)
        assert len(messages) == 8
        deliver_agent_notices(messages)
        assert len({message["source"]["id"] for message in messages}) == len(messages) == 16
    loaded = sessions.load_session(session.session_id)
    assert len(loaded.agent_traces.all_nodes()) == len(history) + 1
    assert len(loaded.agent_traces.get(history[0]).result_summary) == trace.SUMMARY_LIMIT
