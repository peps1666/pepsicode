from __future__ import annotations

import threading
from collections.abc import Generator
from typing import Any

import pytest

from pepsicode.context.context_manager import ContextManager
from pepsicode.core.agent_loop import run_agent_turn, run_agent_turn_stream
from pepsicode.core.input_queue import TurnInputQueue
from pepsicode.llm.anthropic_adapter import ContextOverflowError
from pepsicode.llm.cost_tracker import BudgetExceededError, CostTracker
from pepsicode.tooling import ToolDefinition, ToolRegistry, ToolResult
from pepsicode.types import AgentStep, ChatMessage, StepDiagnostics, StreamToken


class DualModeScriptedModel:
    """Scripted model supporting both synchronous ``next`` and streaming ``next_stream``."""

    def __init__(self, handlers: list[Any]) -> None:
        self._handlers = list(handlers)
        self.calls = 0
        self.last_usage: dict[str, Any] | None = None

    def _pop(self, messages: list[ChatMessage]) -> Any:
        handler = self._handlers[self.calls]
        self.calls += 1
        return handler(messages) if callable(handler) else handler

    def next(self, messages: list[ChatMessage]) -> AgentStep:
        outcome = self._pop(messages)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def next_stream(self, messages: list[ChatMessage]) -> Generator[StreamToken, None, None]:
        outcome = self._pop(messages)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, AgentStep)
        for block in outcome.thinkingBlocks:
            yield StreamToken(type="thinking", thinking_block=dict(block))
        if outcome.content:
            raw = outcome.content
            if outcome.kind == "progress" or outcome.contentKind == "progress":
                raw = f"<progress>{raw}</progress>"
            yield StreamToken(type="text", content=raw)
        for call in outcome.calls:
            import json

            yield StreamToken(type="tool_use", tool_name=call["toolName"], tool_id=call["id"])
            yield StreamToken(type="tool_use", tool_input_partial=json.dumps(call.get("input") or {}))
        stop_reason = outcome.diagnostics.stopReason if outcome.diagnostics else "end_turn"
        yield StreamToken(type="done", stop_reason=stop_reason)


def _echo_registry(log: list[str] | None = None) -> ToolRegistry:
    def run_echo(input_data: dict[str, Any], _context: Any) -> ToolResult:
        if log is not None:
            log.append(str(input_data.get("text", "")))
        return ToolResult(ok=True, output=f"echo:{input_data.get('text', '')}")

    return ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo tool",
                input_schema={"type": "object"},
                validator=lambda v: v,
                run=run_echo,
            )
        ]
    )


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_parity_multi_step_tool_call_and_final_answer(runner, tmp_path):
    tool_calls: list[str] = []
    assistants: list[str] = []
    progress: list[str] = []
    model = DualModeScriptedModel(
        [
            AgentStep(
                type="tool_calls",
                content="checking",
                contentKind="progress",
                calls=[{"id": "c1", "toolName": "echo", "input": {"text": "hello"}}],
            ),
            AgentStep(type="assistant", content="all done"),
        ]
    )
    history = runner(
        model=model,
        tools=_echo_registry(tool_calls),
        messages=[{"role": "user", "content": "run echo"}],
        cwd=str(tmp_path),
        on_assistant_message=assistants.append,
        on_progress_message=progress.append,
    )
    assert tool_calls == ["hello"]
    assert progress == ["checking"]
    assert assistants == ["all done"]
    roles = [m["role"] for m in history if not m.get("internal")]
    assert roles == ["user", "assistant_progress", "assistant_tool_call", "tool_result", "assistant"]
    assert history[-1]["content"] == "all done"


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_parity_empty_response_recovery(runner, tmp_path):
    model = DualModeScriptedModel(
        [
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content=""),
            AgentStep(type="assistant", content="recovered"),
        ]
    )
    history = runner(
        model=model,
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "hi"}],
        cwd=str(tmp_path),
    )
    assert model.calls == 3
    assert history[-1] == {"role": "assistant", "content": "recovered"}


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_parity_max_tokens_unfinished_does_not_dispatch_tools(runner, tmp_path):
    tool_calls: list[str] = []
    model = DualModeScriptedModel(
        [
            AgentStep(
                type="tool_calls",
                calls=[{"id": "c1", "toolName": "echo", "input": {"text": "unsafe"}}],
                diagnostics=StepDiagnostics(stopReason="max_tokens"),
            )
        ]
    )
    history = runner(
        model=model,
        tools=_echo_registry(tool_calls),
        messages=[{"role": "user", "content": "run"}],
        cwd=str(tmp_path),
    )
    assert tool_calls == []
    assert history[-1]["isError"] is True
    assert history[-1]["finishReason"] == "max_tokens"


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_parity_context_overflow_compacts_and_retries(runner, tmp_path):
    cm = ContextManager(model="default", context_window=400)
    cm.summarizer = lambda _text: "compact summary"
    initial_messages: list[ChatMessage] = [{"role": "system", "content": "sys"}]
    for i in range(8):
        initial_messages.append({"role": "user", "content": f"question {i} " + ("x" * 160)})
        initial_messages.append({"role": "assistant", "content": f"answer {i} " + ("y" * 160)})
    initial_messages.append({"role": "user", "content": "latest question"})
    model = DualModeScriptedModel(
        [
            ContextOverflowError("too many tokens"),
            AgentStep(type="assistant", content="succeeded after compact"),
        ]
    )
    history = runner(
        model=model,
        tools=ToolRegistry([]),
        messages=initial_messages,
        cwd=str(tmp_path),
        context_manager=cm,
    )
    assert model.calls == 2
    assert history[-1] == {"role": "assistant", "content": "succeeded after compact"}


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_parity_input_queue_steer_mid_turn(runner, tmp_path):
    queue = TurnInputQueue()
    queue.start()

    def first_step(_messages: list[ChatMessage]) -> AgentStep:
        queue.enqueue("also check X", mode="steer")
        return AgentStep(type="assistant", content="initial answer")

    model = DualModeScriptedModel(
        [
            first_step,
            AgentStep(type="assistant", content="final with X"),
        ]
    )
    history = runner(
        model=model,
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "start"}],
        cwd=str(tmp_path),
        input_queue=queue,
    )
    assert model.calls == 2
    assert any(m.get("role") == "user" and m.get("content") == "also check X" for m in history)
    assert history[-1] == {"role": "assistant", "content": "final with X"}


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_parity_cancellation_and_budget_exceeded(runner, tmp_path):
    cancel = threading.Event()

    def cancel_mid_step(_messages: list[ChatMessage]) -> AgentStep:
        cancel.set()
        return AgentStep(type="assistant", content="never")

    model = DualModeScriptedModel([cancel_mid_step])
    cancelled_history = runner(
        model=model,
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "hi"}],
        cwd=str(tmp_path),
        cancellation_event=cancel,
    )
    assert model.calls == 1
    assert cancelled_history[-1] == {"role": "assistant", "content": "Task stopped by user."}

    tracker = CostTracker()
    tracker.add_usage(model="claude-sonnet-4-20250514", input_tokens=1_000_000, output_tokens=100_000)
    with pytest.raises(BudgetExceededError):
        runner(
            model=model,
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "hi"}],
            cwd=str(tmp_path),
            cost_tracker=tracker,
            cost_limit_usd=0.01,
        )
