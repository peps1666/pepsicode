import io
import json
import threading
import urllib.error
from unittest.mock import Mock

import pytest

from pepsicode.core.agent_loop import _execute_calls_in_order, run_agent_turn
from pepsicode.core.verification import ProjectVerifier, run_verified_turn
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.server.protocol import serialize_session_history
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.types import AgentStep


def test_tool_events_preserve_ids_results_and_recorded_timing(tmp_path):
    tools = ToolRegistry(
        [ToolDefinition("read", "Read", {}, lambda x: x, lambda data, ctx: ToolResult(True, data["path"]))]
    )
    calls = [
        {"id": "first", "toolName": "read", "input": {"path": "a"}},
        {"id": "second", "toolName": "read", "input": {"path": "b"}},
    ]
    events = []
    result = _execute_calls_in_order(
        calls, tools, ToolContext(str(tmp_path)), None, None, lambda event, data: events.append((event, data))
    )
    assert [item.output for item in result] == ["a", "b"]
    assert [data["call_id"] for event, data in events if event == "tool/result"] == ["first", "second"]
    assert all(data["started_at"] > 0 for _, data in events)
    assert all(data["duration_ms"] >= 0 for event, data in events if event == "tool/result")


def test_history_preserves_tool_correlation_timing_and_verification():
    report = {"status": "not_run", "reason_code": "no_checks", "reason": "No checks", "ok": False}
    history = serialize_session_history(
        [
            {"role": "assistant_tool_call", "toolUseId": "original", "toolName": "read", "input": {}},
            {
                "role": "tool_result",
                "toolUseId": "original",
                "toolName": "read",
                "content": "error",
                "isError": True,
                "startedAt": 1234,
                "durationMs": 25,
                "toolStatus": "failed",
            },
            {"role": "assistant", "content": "Done", "verification": report},
        ]
    )
    assert history[0]["call_id"] == history[1]["call_id"] == "original"
    assert history[1]["duration_ms"] == 25 and history[1]["started_at"] == 1234
    assert history[2]["verification"] == report


def test_missing_checks_is_structured_and_does_not_append_a_progress_warning(tmp_path):
    tools = ToolRegistry([])
    report = ProjectVerifier({"verificationMode": "always"}, tools).verify(
        messages=[], tools=tools, cwd=str(tmp_path), permissions=None
    )
    assert report.status == "not_run" and report.reason_code == "no_checks"
    progress, reports = [], []
    messages = run_verified_turn(
        runtime={"verificationMode": "always"},
        runner=lambda **kwargs: [{"role": "assistant", "content": "Answer"}],
        model=object(),
        tools=tools,
        messages=[],
        cwd=str(tmp_path),
        on_progress_message=progress.append,
        on_verification=reports.append,
    )
    assert progress == []
    assert messages[-1]["content"] == "Answer" and messages[-1]["verification"]["reason_code"] == "no_checks"


@pytest.mark.parametrize("status,retries", [(503, 1), (429, 1), (401, 0)])
def test_streaming_requests_retry_transient_headers_but_not_authentication(monkeypatch, status, retries):
    error = urllib.error.HTTPError(
        "https://test.invalid",
        status,
        "failure",
        {},
        io.BytesIO(json.dumps({"error": {"message": "test failure"}}).encode()),
    )
    response = io.BytesIO(
        b'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"ok"}}\n\nevent: message_stop\ndata: {}\n\n'
    )
    request = Mock(side_effect=[error, response])
    monkeypatch.setattr("urllib.request.urlopen", request)
    monkeypatch.setattr("pepsicode.llm.anthropic_adapter._sleep", lambda ms: None)
    monkeypatch.setenv("PEPSI_CODE_MAX_RETRIES", "1")
    model = AnthropicModelAdapter(
        {"baseUrl": "https://test.invalid", "apiKey": "test", "model": "opaque"}, ToolRegistry([])
    )
    if retries:
        tokens = list(model.next_stream([]))
        assert sum(token.type == "retry" for token in tokens) == 1
        assert any(token.type == "text" and token.content == "ok" for token in tokens)
        assert request.call_count == 2
    else:
        with pytest.raises(RuntimeError):
            list(model.next_stream([]))
        assert request.call_count == 1


def test_failed_tool_results_include_recovery_guidance_without_replaying_writes(tmp_path):
    invoked = []
    tools = ToolRegistry(
        [
            ToolDefinition(
                "edit",
                "Edit",
                {},
                lambda x: x,
                lambda data, ctx: (invoked.append(data) or ToolResult(False, "File changed")),
            )
        ]
    )

    class Model:
        count = 0

        def next(self, messages):
            self.count += 1
            if self.count == 1:
                return AgentStep(type="tool_calls", calls=[{"id": "edit-1", "toolName": "edit", "input": {}}])
            assert any(item.get("internal") and "tool failed" in item.get("content", "").lower() for item in messages)
            return AgentStep(type="assistant", content="Need a fresh read before editing.")

    messages = run_agent_turn(model=Model(), tools=tools, messages=[], cwd=str(tmp_path))
    assert len(invoked) == 1 and messages[-1]["content"] == "Need a fresh read before editing."


def test_parallel_events_observe_completion_order_but_history_keeps_call_order(tmp_path):
    second_finished = threading.Event()

    def execute(data, context):
        if data["path"] == "first":
            assert second_finished.wait(2)
        return ToolResult(True, data["path"])

    tools = ToolRegistry([ToolDefinition("read", "Read", {}, lambda x: x, execute, concurrency_safe=True)])
    calls = [{"id": name, "toolName": "read", "input": {"path": name}} for name in ("first", "second")]
    completed = []

    def observe(event, data):
        if event == "tool/result":
            completed.append(data["call_id"])
            if data["call_id"] == "second":
                second_finished.set()

    results = _execute_calls_in_order(calls, tools, ToolContext(str(tmp_path)), None, None, observe)
    assert completed == ["second", "first"]
    assert [result.output for result in results] == ["first", "second"]


def test_stream_retry_exhaustion_is_bounded_and_partial_streams_are_not_replayed(monkeypatch):
    model = AnthropicModelAdapter(
        {"baseUrl": "https://test.invalid", "apiKey": "test", "model": "opaque"}, ToolRegistry([])
    )
    monkeypatch.setenv("PEPSI_CODE_MAX_RETRIES", "1")
    monkeypatch.setattr("pepsicode.llm.anthropic_adapter._sleep", lambda ms: None)
    open_request = Mock(side_effect=urllib.error.URLError("offline"))
    monkeypatch.setattr("urllib.request.urlopen", open_request)
    with pytest.raises(RuntimeError):
        list(model.next_stream([]))
    assert open_request.call_count == 2

    class Interrupted:
        def __iter__(self):
            yield b'event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"partial"}}\n\n'
            raise ConnectionError("stream interrupted")

        def close(self):
            pass

    open_request = Mock(return_value=Interrupted())
    monkeypatch.setattr("urllib.request.urlopen", open_request)
    with pytest.raises(ConnectionError):
        list(model.next_stream([]))
    assert open_request.call_count == 1
