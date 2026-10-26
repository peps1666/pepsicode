from __future__ import annotations

import io
import json
import sys
from unittest.mock import Mock

import pytest

from pepsicode.core import session as sessions
from pepsicode.core.agent_loop import append_queued_inputs, run_agent_turn, run_agent_turn_stream
from pepsicode.core.input_queue import TurnInputQueue
from pepsicode.core.session_tree import record_messages, recording_session
from pepsicode.core.task_control import begin_task, snapshot
from pepsicode.core.verification import run_verified_turn
from pepsicode.execution.runtime import ExecutionScope, RequestBudget, bind_scope
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.ask_user import ask_user_tool
from pepsicode.tools.task_state import task_state_tool
from pepsicode.tools.todo_write import todo_write_tool


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "home" / "sessions")
    return sessions.create_new_session(str(tmp_path))


def answer(**options):
    return [*options["messages"], {"role": "assistant", "content": "Done"}]


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_truncated_provider_response_cannot_dispatch_even_complete_tool_arguments(tmp_path, monkeypatch, runner):
    invoked = Mock(return_value=ToolResult(True, "changed"))
    tools = ToolRegistry([ToolDefinition("effect", "mutation", {}, lambda v: v, invoked)])
    runtime = {"model": "test", "baseUrl": "https://example.invalid", "apiKey": "test"}
    adapter = AnthropicModelAdapter(runtime, tools)
    if runner is run_agent_turn:
        wire = json.dumps(
            {
                "stop_reason": "max_tokens",
                "content": [{"type": "tool_use", "id": "call", "name": "effect", "input": {}}],
            }
        ).encode()
    else:
        events = [
            (
                "content_block_start",
                {"index": 0, "content_block": {"type": "tool_use", "id": "call", "name": "effect"}},
            ),
            ("content_block_delta", {"delta": {"type": "input_json_delta", "partial_json": "{}"}}),
            ("message_delta", {"delta": {"stop_reason": "max_tokens"}}),
            ("message_stop", {}),
        ]
        wire = "".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in events).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: io.BytesIO(wire))
    messages = runner(
        model=adapter, tools=tools, messages=[{"role": "user", "content": "make a change"}], cwd=str(tmp_path)
    )
    assert not invoked.called
    assert messages[-1]["isError"] and messages[-1]["finishReason"] == "max_tokens"


@pytest.mark.parametrize("reason", [None, "end_turn"])
def test_stream_eof_is_incomplete_even_with_nonempty_text(tmp_path, monkeypatch, reason):
    wire = b'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"partial answer"}}\n\n'
    if reason:
        wire += b'event: message_delta\ndata: {"delta":{"stop_reason":"end_turn"}}\n\n'
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: io.BytesIO(wire))
    model = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.invalid", "apiKey": "test"}, ToolRegistry([])
    )
    messages = run_agent_turn_stream(model=model, tools=model.tools, messages=[], cwd=str(tmp_path))
    assert messages[-1]["isError"] and messages[-1]["finishReason"] == "stream_incomplete"


@pytest.mark.parametrize("green_checks", [False, True])
def test_plan_or_unrelated_green_tests_cannot_complete_an_implementation(session, green_checks):
    runtime = {"maxIterations": 2}
    if green_checks:
        runtime.update(
            verificationMode="always", verificationCommands=[{"command": [sys.executable, "-c", "assert True"]}]
        )
    permissions = PermissionManager(session.workspace, mode="full_access")
    with recording_session(session):
        messages = run_verified_turn(
            runtime=runtime,
            runner=answer,
            model=object(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "Implement required_feature.py"}],
            cwd=session.workspace,
            permissions=permissions,
        )
    assert messages[-1]["isError"]
    assert session.task_control["task"]["status"] == "incomplete"
    assert not (sessions.Path(session.workspace) / "required_feature.py").exists()
    assert runtime["_lastVerification"]["reason_code"] == "task_incomplete"
    permissions.close()


def test_acceptance_checks_real_artifact_and_persists_evidence(session):
    path = sessions.Path(session.workspace) / "feature.py"
    path.write_text("def value(): return 42\n", encoding="utf-8")
    with recording_session(session):
        result = run_verified_turn(
            runtime={},
            runner=answer,
            model=object(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "Implement feature.py"}],
            cwd=session.workspace,
            acceptance=[{"id": "value", "kind": "file_contains", "path": "feature.py", "text": "return 42"}],
        )
    assert session.task_control["task"]["status"] == "complete"
    assert result[-1]["task"]["status"] == "complete"
    loaded = sessions.load_session(session.session_id)
    assert loaded.task_control["task"]["evidence"][0]["version"]
    assert loaded.task_control["task"]["evidence"][0]["status"] == "satisfied"


def test_task_definition_uses_cas_and_cannot_weaken_conditions(tmp_path):
    scope = ExecutionScope(str(tmp_path), budget=RequestBudget(CostTracker()))
    context = ToolContext(str(tmp_path), execution=scope)
    registry = ToolRegistry([task_state_tool])
    with bind_scope(scope):
        task = begin_task([{"role": "user", "content": "Implement feature.py"}], {})
        args = {
            "action": "define",
            "task_id": task["id"],
            "revision": task["revision"],
            "criteria": [{"id": "artifact", "kind": "file_exists", "path": "feature.py"}],
        }
        assert registry.execute("task_state", args, context).ok
        assert not registry.execute("task_state", args, context).ok
        assert not scope.has_unknown
        current = snapshot()["task"]
        assert not registry.execute(
            "task_state", {**args, "revision": current["revision"], "kind": "answer"}, context
        ).ok
        assert snapshot()["task"]["criteria"][0]["path"] == "feature.py"


def test_accepted_inbox_survives_restart_and_claim_is_not_lost(session):
    queue = TurnInputQueue()
    queue.bind_session(session)
    queue.start()
    item = queue.enqueue("the next task", "follow_up")
    assert sessions.load_session(session.session_id).task_control["inbox"]["pending"][0]["id"] == item.id
    assert queue.next_or_close() == [item]
    recovered = TurnInputQueue()
    recovered.bind_session(sessions.load_session(session.session_id))
    recovered.start()
    assert recovered.next_or_close() == [item]


def test_admitted_inbox_claim_is_not_replayed_after_ack_crash(session):
    queue = TurnInputQueue()
    queue.bind_session(session)
    queue.start()
    item = queue.enqueue("accepted", "follow_up")
    queue.next_or_close()
    record_messages(session, [item.message()])
    sessions.save_session(session)  # crash before clearing the claimed record
    recovered = TurnInputQueue()
    recovered.bind_session(sessions.load_session(session.session_id))
    recovered.start()
    assert recovered.next_or_close() == []


def test_enqueue_disk_failure_never_acknowledges_or_publishes_acceptance(session, monkeypatch):
    queue = TurnInputQueue()
    queue.bind_session(session)
    updates = []
    queue.start(updates.append)
    monkeypatch.setattr(sessions, "save_session", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        queue.enqueue("must not be accepted")
    assert updates[-1]["pending"] == []
    assert session.task_control["inbox"]["pending"] == []


def test_auxiliary_index_failure_cannot_reject_an_already_committed_input(session, monkeypatch):
    queue = TurnInputQueue()
    queue.bind_session(session)
    queue.start()
    monkeypatch.setattr(sessions, "_save_session_index", Mock(side_effect=OSError("index locked")))
    item = queue.enqueue("accepted despite the unavailable listing index", "follow_up")
    assert sessions.load_session(session.session_id).task_control["inbox"]["pending"][0]["id"] == item.id
    assert queue.next_or_close() == [item]


def test_todos_are_session_scoped_stable_and_durable(session, tmp_path):
    context = ToolContext(session.workspace)
    with recording_session(session):
        todo_write_tool.run({"todos": [{"content": "first", "status": "pending"}]}, context)
        identity = snapshot()["todos"][0]["id"]
        todo_write_tool.run({"todos": [{"content": "first", "status": "completed"}]}, context)
        assert snapshot()["todos"][0]["id"] == identity
    other = sessions.create_new_session(str(tmp_path / "other"))
    with recording_session(other):
        todo_write_tool.run({"todos": [{"content": "second"}]}, ToolContext(other.workspace))
    assert sessions.load_session(session.session_id).task_control["todos"][0]["content"] == "first"
    assert other.task_control["todos"][0]["content"] == "second"


def test_pending_user_question_resumes_same_task_without_inventing_answer(session):
    with recording_session(session):
        scope = ExecutionScope(session.workspace, budget=RequestBudget(CostTracker()))
        with bind_scope(scope):
            task = begin_task([{"role": "user", "content": "Implement feature.py"}], {})
            ask_user_tool.run({"question": "Which format?"}, ToolContext(session.workspace, execution=scope))
            session.task_control["task"]["status"] = "waiting_user"
            assert snapshot()["questions"][0]["status"] == "pending"
            resumed = begin_task([{"role": "user", "content": "JSON"}], {})
            assert resumed["id"] == task["id"] and resumed["revision"] > task["revision"]
            assert snapshot()["questions"][0]["answer"] == "JSON"


def test_followup_has_distinct_task_identity_and_question_needs_no_file_changes(session):
    queue = TurnInputQueue()
    queue.bind_session(session)
    queue.start()
    queue.enqueue("Explain a second topic", "follow_up")
    with recording_session(session):
        run_verified_turn(
            runtime={},
            runner=answer,
            model=object(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "Explain the first topic"}],
            cwd=session.workspace,
            input_queue=queue,
        )
    state = session.task_control
    assert state["task"]["id"] != state["task_history"][0]["id"]
    assert state["task"]["status"] == "answered"
    assert state["task_history"][0]["status"] == "answered"


def test_steering_invalidates_evidence_and_authorizes_one_audited_revision(session):
    scope = ExecutionScope(session.workspace, budget=RequestBudget(CostTracker()))
    queue = TurnInputQueue()
    queue.bind_session(session)
    queue.start()
    scope.input_queue = queue
    registry = ToolRegistry([task_state_tool])
    context = ToolContext(session.workspace, execution=scope)
    with recording_session(session), bind_scope(scope):
        begin_task([{"role": "user", "content": "Implement the old format"}], {})
        task = snapshot()["task"]
        condition = {"id": "format", "kind": "file_contains", "path": "feature.py", "text": "old"}
        assert registry.execute(
            "task_state",
            {"action": "define", "task_id": task["id"], "revision": task["revision"], "criteria": [condition]},
            context,
        ).ok
        before = snapshot()["task"]
        assert not registry.execute(
            "task_state",
            {
                "action": "revise",
                "task_id": before["id"],
                "revision": before["revision"],
                "instruction_id": before["inputs"][-1],
                "reason": "drop work",
                "criteria": [],
            },
            context,
        ).ok
        item = queue.enqueue("Use the new format instead")
        revised = snapshot()["task"]
        assert revised["revision"] > before["revision"]
        assert revised["admitted_revision"] != revised["revision"]
        append_queued_inputs([], queue.take_steering(), cwd=session.workspace)
        assert registry.execute(
            "task_state",
            {
                "action": "revise",
                "task_id": revised["id"],
                "revision": revised["revision"],
                "instruction_id": item.id,
                "reason": "human changed the format",
                "criteria": [{**condition, "text": "new"}],
            },
            context,
        ).ok
        current = snapshot()["task"]
        assert current["criteria_revision"] == current["revision"]
        assert current["acceptance_changes"][0]["instruction_id"] == item.id


def test_regenerating_a_completed_answer_does_not_retain_success_on_failure(session):
    runtime = {}
    messages = [{"role": "user", "content": "Explain the result"}]
    with recording_session(session):
        run_verified_turn(
            runtime=runtime,
            runner=answer,
            model=object(),
            tools=ToolRegistry([]),
            messages=messages,
            cwd=session.workspace,
        )
        assert snapshot()["task"]["status"] == "answered"

        def failure(**options):
            return [*options["messages"], {"role": "assistant", "content": "model failed", "isError": True}]

        run_verified_turn(
            runtime=runtime,
            runner=failure,
            model=object(),
            tools=ToolRegistry([]),
            messages=messages,
            cwd=session.workspace,
        )
        assert snapshot()["task"]["status"] == "incomplete"


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_budget_exhaustion_keeps_the_user_task_unfinished(session, runner):
    from pepsicode.execution.runtime import ModelRequestLimitError

    class Exhausted:
        def next(self, messages):
            raise ModelRequestLimitError("request limit")

        def next_stream(self, messages):
            raise ModelRequestLimitError("request limit")
            yield  # generator contract

    with recording_session(session), pytest.raises(ModelRequestLimitError):
        run_verified_turn(
            runtime={},
            runner=runner,
            model=Exhausted(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "Implement feature.py"}],
            cwd=session.workspace,
        )
    assert session.task_control["task"]["status"] == "budget_exhausted"


def test_repeated_identical_results_remind_without_granting_new_permissions(tmp_path):
    scope = ExecutionScope(str(tmp_path), budget=RequestBudget(CostTracker()))
    registry = ToolRegistry(
        [ToolDefinition("probe", "read", {}, lambda v: v, lambda *_: ToolResult(True, "same"), affects_workspace=False)]
    )
    with bind_scope(scope):
        for index in range(3):
            result = registry.execute("probe", {}, ToolContext(str(tmp_path), execution=scope))
            assert ("identical calls" in result.output) == (index == 2)


def test_corrupt_task_contract_is_rejected_before_restoring_a_session(session):
    sessions.save_session(session)
    path = sessions.SESSIONS_DIR / f"{session.session_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["task_control"] = {"version": 99}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert sessions.load_session(session.session_id) is None


def test_current_task_todos_gate_acceptance_and_survive_restart(session):
    path = sessions.Path(session.workspace) / "feature.py"
    path.write_text("present", encoding="utf-8")

    def unfinished(**options):
        todo_write_tool.run(
            {"todos": [{"content": "check behavior", "status": "pending"}]}, ToolContext(session.workspace)
        )
        return answer(**options)

    with recording_session(session):
        run_verified_turn(
            runtime={"maxIterations": 1},
            runner=unfinished,
            model=object(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "Implement feature.py"}],
            cwd=session.workspace,
            acceptance=[{"id": "artifact", "kind": "file_exists", "path": "feature.py"}],
        )
    assert sessions.load_session(session.session_id).task_control["task"]["status"] == "incomplete"
    assert session.task_control["todos"][0]["status"] == "pending"


@pytest.mark.parametrize("change", ["file", "human"])
def test_acceptance_rejects_evidence_that_changed_during_command(session, change):
    from pepsicode.core.loop_engine import VerificationResult
    from pepsicode.core.task_verification import TaskVerifier

    path = sessions.Path(session.workspace) / "feature.py"
    path.write_text("old", encoding="utf-8")
    queue = TurnInputQueue()
    queue.bind_session(session)
    queue.start()
    scope = ExecutionScope(session.workspace, budget=RequestBudget(CostTracker()))

    def change_during_check(*_):
        if change == "file":
            path.write_text("changed", encoding="utf-8")
        else:
            queue.enqueue("Also implement the new format")
        return ToolResult(True, "passed", exit_code=0)

    project = Mock(cancellation_event=None, hook_engine=None)
    project.verify.return_value = VerificationResult(False, "not run", status="not_run")
    tools = ToolRegistry([ToolDefinition("run_command", "check", {}, lambda v: v, change_during_check)])
    with recording_session(session), bind_scope(scope):
        begin_task([{"role": "user", "content": "Implement feature.py"}], {})
        task = session.task_control["task"]
        task["criteria"] = [
            {"id": "file", "kind": "file_contains", "path": "feature.py", "text": "old"},
            {"id": "behavior", "kind": "command", "argv": ["probe"]},
        ]
        task["criteria_revision"] = task["revision"]
        report = TaskVerifier(project, {}).verify(messages=[], tools=tools, cwd=session.workspace, permissions=None)
        assert not report.ok
        assert snapshot()["task"]["status"] != "complete"
        if change == "human":
            assert snapshot()["task"]["evidence"] == []
        else:
            assert snapshot()["task"]["evidence"][0]["status"] == "unknown"


def test_branch_checkout_cannot_resume_a_different_branch_task(session):
    from pepsicode.core.session_tree import checkout_entry

    messages = [{"role": "user", "content": "Explain the first topic"}, {"role": "assistant", "content": "answer"}]
    record_messages(session, messages)
    with recording_session(session):
        original = begin_task(messages, {})
        checkout_entry(session, messages[-1]["entryId"])
        current = begin_task([*session.messages, {"role": "user", "content": "continue"}], {})
        assert current["id"] != original["id"]
