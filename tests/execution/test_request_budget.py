from __future__ import annotations

import io
import json
import urllib.error
from contextlib import closing

import pytest

from pepsicode.core.agent_loop import _accumulate_stream_tokens
from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope, RequestBudget, bind_scope
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.cost_tracker import BudgetExceededError, CostTracker
from pepsicode.tooling import ToolRegistry


def response(stream):
    if not stream:
        return io.BytesIO(
            json.dumps(
                {
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                }
            ).encode()
        )
    events = [
        ("message_start", {"message": {"usage": {"input_tokens": 100, "output_tokens": 0}}}),
        ("content_block_delta", {"delta": {"type": "text_delta", "text": "done"}}),
        ("message_delta", {"usage": {"output_tokens": 10}}),
        ("message_stop", {}),
    ]
    return io.BytesIO("".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in events).encode())


@pytest.mark.parametrize("stream", [False, True], ids=["normal", "stream"])
@pytest.mark.parametrize("limit", [1, 2])
def test_every_transport_retry_spends_a_shared_request_slot(tmp_path, monkeypatch, stream, limit):
    attempts = []

    def send(request, **kwargs):
        attempts.append(request)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "rate limited", {}, io.BytesIO(b'{"error":{"message":"retry"}}')
            )
        return response(stream)

    monkeypatch.setattr("urllib.request.urlopen", send)
    monkeypatch.setattr("pepsicode.llm.anthropic_adapter._sleep", lambda *_: None)
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "x"}, ToolRegistry([])
    )
    scope = ExecutionScope(
        str(tmp_path),
        budget=RequestBudget(CostTracker(), max_requests=limit),
        journal=ExecutionJournal(tmp_path / "journal.jsonl"),
    )

    def invoke():
        return list(adapter.next_stream([])) if stream else adapter.next([])

    with bind_scope(scope):
        if limit == 1:
            with pytest.raises(RuntimeError, match="request limit"):
                invoke()
        else:
            invoke()
    assert len(attempts) == limit
    assert scope.budget.requests == limit
    prepared = [record for record in ExecutionJournal.read(scope.journal.path) if record["event"] == "model/prepared"]
    assert len(prepared) == limit
    assert len({record["request_id"] for record in prepared}) == limit
    assert (
        all(record["body"]["stream"] for record in prepared)
        if stream
        else all("stream" not in record["body"] for record in prepared)
    )


def test_closing_stream_at_done_preserves_metered_budget_for_next_request(tmp_path, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response(True))
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "x"}, ToolRegistry([])
    )
    scope = ExecutionScope(str(tmp_path), budget=RequestBudget(CostTracker(), limit=1.0, max_requests=2))
    with bind_scope(scope):
        for _ in range(2):
            with closing(adapter.next_stream([])) as tokens:
                assert _accumulate_stream_tokens(tokens)[0] == "done"
            assert not scope.budget.unmetered
    assert scope.budget.requests == 2
    assert scope.budget.tracker.get_total_calls() == 2


def test_uncertain_transport_charge_stops_further_priced_requests(tmp_path, monkeypatch):
    attempts = []

    def lost(request, **kwargs):
        attempts.append(request)
        raise urllib.error.URLError("lost response after dispatch")

    monkeypatch.setattr("urllib.request.urlopen", lost)
    monkeypatch.setattr("pepsicode.llm.anthropic_adapter._sleep", lambda *_: None)
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "x"}, ToolRegistry([])
    )
    scope = ExecutionScope(str(tmp_path), budget=RequestBudget(CostTracker(), limit=1.0))
    with bind_scope(scope), pytest.raises(RuntimeError, match="usage unavailable"):
        adapter.next([])
    assert len(attempts) == 1


def test_budget_preflight_can_refuse_before_transport(tmp_path, monkeypatch):
    send = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: send.append(args))
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "x"}, ToolRegistry([])
    )
    scope = ExecutionScope(str(tmp_path), budget=RequestBudget(CostTracker(), limit=0.0000001))
    with bind_scope(scope), pytest.raises(BudgetExceededError):
        adapter.next([])
    assert not send
