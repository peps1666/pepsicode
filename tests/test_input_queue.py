import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pepsicode.core.agent_loop import run_agent_turn, run_agent_turn_stream
from pepsicode.core.input_queue import TurnInputQueue
from pepsicode.core.loop_engine import VerificationResult
from pepsicode.core.verification import run_verified_turn
from pepsicode.input_limits import MAX_EXPANDED_INPUT_CHARACTERS, MAX_INPUT_CHARACTERS
from pepsicode.tooling import ToolDefinition, ToolRegistry, ToolResult
from pepsicode.types import AgentStep, StreamToken


def test_queue_prioritizes_steering_and_preserves_follow_up_order():
    updates = []
    queue = TurnInputQueue()
    queue.start(updates.append)
    first = queue.enqueue("first task", "follow_up")
    steer = queue.enqueue("change direction")
    second = queue.enqueue("second task", "follow_up")
    assert queue.take_steering() == [steer]
    assert queue.next_or_close() == [first]
    assert queue.next_or_close() == [second]
    assert queue.next_or_close() == []
    with pytest.raises(ValueError, match="finishing"):
        queue.enqueue("too late")
    assert [item["content"] for update in updates for item in update["delivered"]] == [
        "change direction",
        "first task",
        "second task",
    ]


def test_remove_and_cancel_return_pending_input_once():
    updates = []
    queue = TurnInputQueue()
    queue.start(updates.append)
    first = queue.enqueue("take me back")
    second = queue.enqueue("keep for later", "follow_up")
    assert queue.remove(first.id)
    assert not queue.remove(first.id)
    queue.close()
    queue.close()
    assert [item["id"] for update in updates for item in update["returned"]] == [first.id, second.id]
    queue.start(updates.append)
    assert queue.next_or_close() == []


def test_queue_capacity_rejects_new_input_without_discarding_existing_items():
    queue = TurnInputQueue()
    updates = []
    queue.start(updates.append)
    for index in range(32):
        queue.enqueue(f"task {index}", "follow_up")
    with pytest.raises(ValueError, match="full"):
        queue.enqueue("overflow")
    queue.close()
    assert len(updates[-1]["returned"]) == 32


@pytest.mark.parametrize(
    "content,mode", [("", "steer"), (None, "steer"), ("/model", "steer"), ("text", "bad"), ("x" * 32001, "steer")]
)
def test_invalid_input_never_enters_queue(content, mode):
    queue = TurnInputQueue()
    queue.start()
    with pytest.raises(ValueError):
        queue.enqueue(content, mode)
    assert queue.next_or_close() == []


def test_expanded_commands_use_their_own_bound_without_relaxing_raw_input():
    queue = TurnInputQueue()
    queue.start()
    content = "x" * (MAX_INPUT_CHARACTERS + 1)
    command = queue.enqueue(content, command_path="custom.md", display_content="/custom")
    assert queue.take_steering() == [command]
    with pytest.raises(ValueError, match=str(MAX_INPUT_CHARACTERS)):
        queue.enqueue(content)
    with pytest.raises(ValueError, match=str(MAX_EXPANDED_INPUT_CHARACTERS)):
        queue.enqueue("x" * (MAX_EXPANDED_INPUT_CHARACTERS + 1), command_path="custom.md", display_content="/custom")
    with pytest.raises(ValueError, match="invocation"):
        queue.enqueue("expanded", command_path="custom.md", display_content="x" * (MAX_INPUT_CHARACTERS + 1))
    assert queue.next_or_close() == []


def test_queue_admission_and_settlement_are_atomic():
    # Either settlement sees the admitted input or admission rejects it. There
    # must never be accepted input stranded after the worker decides to exit.
    for _ in range(40):
        updates = []
        queue = TurnInputQueue()
        queue.start(updates.append)
        gate = threading.Barrier(2)

        def submit():
            gate.wait()
            try:
                return queue.enqueue("racing input")
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(submit)
            gate.wait()
            delivered = queue.next_or_close()
            accepted = future.result(timeout=2)
        queue.close()
        returned = [item for update in updates for item in update["returned"]]
        assert accepted is None or accepted in delivered or accepted.id in {item["id"] for item in returned}


class ScriptedModel:
    def __init__(self, steps, after_step=None):
        self.steps = iter(steps)
        self.after_step = after_step
        self.requests = []

    def next(self, messages):
        self.requests.append([dict(message) for message in messages])
        step = next(self.steps)
        if self.after_step:
            self.after_step(len(self.requests))
        return step

    def next_stream(self, messages):
        step = self.next(messages)
        if step.content:
            yield StreamToken(type="text", content=step.content)
        for call in step.calls:
            yield StreamToken(type="tool_use", tool_name=call["toolName"], tool_id=call["id"], tool_input_partial="{}")
        yield StreamToken(type="done")


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_steering_waits_for_entire_tool_batch(tmp_path, runner):
    queue = TurnInputQueue()
    queue.start()
    executed = []

    def execute(data, context):
        executed.append(len(executed) + 1)
        if len(executed) == 1:
            queue.enqueue("avoid database changes")
        return ToolResult(True, "done")

    tool = ToolDefinition(
        "inspect", "inspect", {"type": "object"}, lambda value: value, execute, affects_workspace=False
    )
    model = ScriptedModel(
        [
            AgentStep(type="tool_calls", calls=[{"id": str(i), "toolName": "inspect", "input": {}} for i in range(2)]),
            AgentStep(type="assistant", content="adjusted"),
        ]
    )
    result = runner(
        model=model,
        tools=ToolRegistry([tool]),
        messages=[{"role": "user", "content": "work"}],
        cwd=str(tmp_path),
        input_queue=queue,
    )
    assert executed == [1, 2]
    assert [message["role"] for message in model.requests[1]] == [
        "user",
        "assistant_tool_call",
        "assistant_tool_call",
        "tool_result",
        "tool_result",
        "user",
    ]
    assert model.requests[1][-1]["content"] == "avoid database changes"
    assert result[-1]["content"] == "adjusted"


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_steering_after_final_response_continues_same_run(tmp_path, runner):
    queue = TurnInputQueue()
    queue.start()
    model = ScriptedModel(
        [AgentStep(type="assistant", content="initial answer"), AgentStep(type="assistant", content="revised answer")],
        lambda index: queue.enqueue("also cover Windows") if index == 1 else None,
    )
    messages = runner(
        model=model,
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "explain"}],
        cwd=str(tmp_path),
        input_queue=queue,
    )
    assert model.requests[1][-1]["content"] == "also cover Windows"
    assert model.requests[1][-1]["source"]["kind"] == "steering"
    assert messages[-1]["content"] == "revised answer"


def test_follow_up_waits_until_failed_verification_is_fixed(tmp_path, monkeypatch):
    queue = TurnInputQueue()
    queue.start()
    queue.enqueue("write documentation", "follow_up")
    requests, order = [], []

    def runner(**options):
        requests.append(options["messages"])
        order.append("agent")
        return [*options["messages"], {"role": "assistant", "content": "done"}]

    def verify(self, **options):
        order.append("verify")
        failed = order.count("verify") == 1
        return VerificationResult(not failed, "check", status="failed" if failed else "passed")

    monkeypatch.setattr("pepsicode.core.verification.ProjectVerifier.verify", verify)
    run_verified_turn(
        runtime={},
        runner=runner,
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "explain the first topic"}],
        cwd=str(tmp_path),
        input_queue=queue,
    )
    assert order == ["agent", "verify", "agent", "verify", "agent", "verify"]
    assert not any(message.get("content") == "write documentation" for message in requests[1])
    assert requests[2][-1]["content"] == "write documentation"
    assert not queue.accepting


@pytest.mark.parametrize("reason", ["cancelled", "error", "awaiting_user", "blocked", "failed"])
def test_unfinished_task_returns_follow_ups_instead_of_running_them(tmp_path, monkeypatch, reason):
    updates = []
    queue = TurnInputQueue()
    queue.start(updates.append)
    queue.enqueue("must wait", "follow_up")
    event = threading.Event()
    calls = []

    def runner(**options):
        calls.append(True)
        if reason == "cancelled":
            event.set()
        message = {"role": "assistant", "content": "stopped"}
        if reason == "error":
            message["isError"] = True
        if reason == "awaiting_user":
            message["awaitingUser"] = True
        return [*options["messages"], message]

    monkeypatch.setattr(
        "pepsicode.core.verification.ProjectVerifier.verify",
        lambda self, **options: VerificationResult(False, "stop", status=reason),
    )
    run_verified_turn(
        runtime={"maxIterations": 1},
        runner=runner,
        model=object(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=str(tmp_path),
        input_queue=queue,
        cancellation_event=event,
    )
    assert len(calls) == 1
    assert [item["content"] for update in updates for item in update["returned"]] == ["must wait"]


def test_steering_during_verification_runs_before_follow_up(tmp_path, monkeypatch):
    queue = TurnInputQueue()
    queue.start()
    queue.enqueue("later task", "follow_up")
    requests = []

    def runner(**options):
        requests.append(options["messages"])
        return [*options["messages"], {"role": "assistant", "content": "done"}]

    def verify(self, **options):
        if len(requests) == 1:
            queue.enqueue("adjust current task")
        return VerificationResult(True, "passed", status="passed")

    monkeypatch.setattr("pepsicode.core.verification.ProjectVerifier.verify", verify)
    run_verified_turn(
        runtime={},
        runner=runner,
        model=object(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=str(tmp_path),
        input_queue=queue,
    )
    assert [request[-1]["content"] for request in requests[1:]] == ["adjust current task", "later task"]
