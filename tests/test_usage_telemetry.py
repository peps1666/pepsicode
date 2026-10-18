"""Usage travels from provider events to live UI without counting cache twice."""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from pepsicode.context.context_manager import ContextManager
from pepsicode.core.agent_loop import run_agent_turn_stream
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter, _normalize_usage, _process_sse_event
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.app import ScreenState, TtyAppArgs, _handle_input, _render_usage_status
from pepsicode.tui.chrome import string_display_width, strip_ansi
from pepsicode.usage import cache_hit_percentage, format_usage_status, usage_snapshot


def stream_response(events):
    return io.BytesIO("".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in events).encode())


@pytest.fixture
def adapter():
    return AnthropicModelAdapter(
        {"model": "test-model", "baseUrl": "https://provider.example", "apiKey": "test"}, ToolRegistry([])
    )


def test_stream_updates_before_first_text_and_merges_final_output(adapter, monkeypatch):
    events = [
        ("message_start", {"message": {"usage": {
            "input_tokens": 100, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 800, "output_tokens": 0,
        }}}),
        ("content_block_delta", {"delta": {"type": "text_delta", "text": "Done"}}),
        ("message_delta", {"usage": {"output_tokens": 20}}),
        ("message_stop", {}),
    ]
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: stream_response(events))
    cm = ContextManager(model="test-model", context_window=2000)
    tracker = CostTracker()
    live = []
    final = []

    def observe(token):
        if token.type == "usage":
            live.append(usage_snapshot(cm, token.usage))

    run_agent_turn_stream(
        model=adapter, tools=adapter.tools, messages=[{"role": "user", "content": "Hi"}],
        cwd=".", context_manager=cm, cost_tracker=tracker, on_token=observe, on_usage=final.append,
    )
    assert len(live) == 2
    assert live[0]["cache_hit_percentage"] == 80
    assert live[0]["context"]["total_tokens"] == 1000
    assert live[0]["context"]["usage_percentage"] == 50
    assert live[1]["context"]["total_tokens"] == 1020
    assert adapter.last_usage == {"input_tokens": 100, "cache_write_tokens": 100, "cache_read_tokens": 800, "output_tokens": 20}
    assert tracker.get_total_calls() == 1
    assert tracker.get_model_usage("test-model").cache_read_tokens == 800
    assert len(final) == 1


def test_stream_usage_accumulators_are_independent():
    first, second = {}, {}
    for state, tokens in ((first, 120), (second, 700)):
        list(_process_sse_event({"event": "message_start", "data": {"message": {"usage": {"input_tokens": tokens}}}}, usage_state=state))
    list(_process_sse_event({"event": "message_delta", "data": {"usage": {"output_tokens": 10}}}, usage_state=first))
    assert first == {"input_tokens": 120, "output_tokens": 10}
    assert second == {"input_tokens": 700}


def test_request_without_usage_clears_previous_report(adapter, monkeypatch):
    adapter.last_usage = {"input_tokens": 20, "cache_read_tokens": 80}
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: stream_response([("message_stop", {})]))
    assert list(adapter.next_stream([]))[-1].type == "done"
    assert adapter.last_usage is None
    monkeypatch.setattr(adapter, "_send", lambda _request: ({"content": [{"type": "text", "text": "Done"}]}, 200))
    adapter.last_usage = {"input_tokens": 20, "cache_read_tokens": 80}
    adapter.next([])
    assert adapter.last_usage is None


def test_nonstream_maps_anthropic_cache_fields(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_send", lambda _request: ({
        "content": [{"type": "text", "text": "Done"}],
        "usage": {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 800},
    }, 200))
    adapter.next([])
    assert cache_hit_percentage(adapter.last_usage) == 80


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 100}, None),
    ({"input_tokens": 0, "cache_read_tokens": 0}, None),
    ({"input_tokens": 100, "cache_read_tokens": 0}, 0),
    ({"input_tokens": 100, "cache_write_tokens": 100, "cache_read_tokens": 800}, 80),
    ({"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200}, 80),
])
def test_cache_rate_distinguishes_missing_from_zero(usage, expected):
    assert cache_hit_percentage(_normalize_usage(usage)) == expected


def test_estimates_and_missing_data_are_visible_in_status():
    assert format_usage_status(usage_snapshot(None, None)) == "缓存命中 未提供 · 上下文 未提供"
    cm = ContextManager(model="unknown", messages=[{"role": "user", "content": "Hi"}])
    snapshot = usage_snapshot(cm, None)
    assert snapshot["context"]["window_is_estimate"] is True
    assert "约" in format_usage_status(snapshot)
    args = SimpleNamespace(context_manager=cm, messages=cm.messages)
    state = ScreenState(latest_usage={"input_tokens": 20, "cache_read_tokens": 80})
    assert "缓存命中 80.0%" in strip_ansi(_render_usage_status(args, state, 100))
    compact = strip_ansi(_render_usage_status(args, state, 28))
    assert "Cache 80%" in compact and "Ctx" in compact
    assert string_display_width(compact) <= 28


def test_server_context_query_preserves_active_worker_messages():
    client = ClientSession(".")
    client.context_manager = ContextManager(model="test", context_window=2000)
    client.context_manager.messages = [{"role": "user", "content": "live worker"}]
    client.context_manager.update_usage(1000, 20)
    client.messages = [{"role": "user", "content": "stale session"}]
    client._worker_active.set()
    client.model = SimpleNamespace(last_usage={"input_tokens": 100, "cache_read_tokens": 900})
    client.cost_tracker = CostTracker()
    client.cost_tracker.add_usage("test", 100, 20, cache_read_tokens=900)

    async def query():
        return await PepsiCodeServer()._dispatch("context/query", {}, client, None, None, asyncio.get_running_loop())

    result = asyncio.run(query())
    assert client.context_manager.messages[0]["content"] == "live worker"
    assert result["usage_percentage"] == 51
    assert client.cost_snapshot()["cache_hit_percentage"] == 90
    assert client.cost_snapshot()["total_prompt_tokens"] == 1000


def test_server_pushes_usage_during_generation_before_turn_end(tmp_path, monkeypatch):
    from pepsicode.permissions import PermissionManager
    from pepsicode.types import StreamToken

    client = ClientSession(str(tmp_path))
    client.permissions = PermissionManager(str(tmp_path), mode="auto")
    client.context_manager = ContextManager(model="test", context_window=2000)
    client.model = SimpleNamespace(last_usage=None)
    client.tools = ToolRegistry([])
    client.cost_tracker = CostTracker()
    # No session_data: this test exercises the stream, without disk writes.
    client._rebuild_system_prompt = lambda: None
    events = []

    async def emit(name, payload):
        events.append((name, payload))

    def fake_turn(**kwargs):
        usage = {"input_tokens": 100, "cache_read_tokens": 900, "output_tokens": 20}
        client.model.last_usage = usage
        client.context_manager.update_usage(1000, 20)
        kwargs["on_token"](StreamToken(type="usage", usage=usage))
        client.cost_tracker.add_usage("test", 100, 20, cache_read_tokens=900)
        kwargs["on_usage"](usage)
        kwargs["on_assistant_message"]("Done")
        return kwargs["messages"] + [{"role": "assistant", "content": "Done"}]

    monkeypatch.setattr("pepsicode.server.app.run_agent_turn_stream", fake_turn)

    async def run():
        await PepsiCodeServer()._run_turn({"input": "Hi"}, client, emit, asyncio.get_running_loop())
        await asyncio.sleep(0)

    asyncio.run(run())
    usage_events = [payload for name, payload in events if name == "usage/update"]
    assert usage_events[-1]["cache_hit_percentage"] == 90
    assert usage_events[-1]["context"]["usage_percentage"] == 51
    assert next(i for i, (name, _) in enumerate(events) if name == "cost/update") < next(
        i for i, (name, _) in enumerate(events) if name == "turn/end"
    )
    cost = next(payload for name, payload in events if name == "cost/update")
    assert cost["total_prompt_tokens"] == 1000
    assert cost["total_calls"] == 1


def test_tty_passes_usage_observer_and_cost_tracker(tmp_path, monkeypatch):
    from pepsicode.permissions import PermissionManager

    cm = ContextManager(model="test", context_window=2000)
    tracker = CostTracker()
    args = TtyAppArgs(runtime=None, tools=ToolRegistry([]), model=None, messages=[{"role": "system", "content": "test"}], cwd=str(tmp_path),
                      permissions=PermissionManager(str(tmp_path), mode="auto"), context_manager=cm)
    state = ScreenState(cost_tracker=tracker)
    monkeypatch.setattr("pepsicode.tui.app.save_history_entries", lambda _history: None)
    monkeypatch.setattr("pepsicode.context.context_manager.save_context_state", lambda _manager: None)

    def fake_turn(**kwargs):
        assert kwargs["cost_tracker"] is tracker
        kwargs["on_usage"]({"input_tokens": 20, "cache_read_tokens": 80})
        return kwargs["messages"]

    monkeypatch.setattr("pepsicode.tui.app.run_agent_turn_stream", fake_turn)
    _handle_input(args, state, lambda: None, "Hi")
    state.agent_thread.join(timeout=2)
    assert not state.agent_thread.is_alive()
    assert state.latest_usage == {"input_tokens": 20, "cache_read_tokens": 80}
