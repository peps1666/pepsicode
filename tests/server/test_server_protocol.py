"""Tests for desktop WebSocket protocol projections."""

from pepsicode.server.protocol import serialize_session_history


def test_serialize_session_history_maps_agent_roles() -> None:
    messages = [
        {"role": "system", "content": "hidden prompt"},
        {"role": "user", "content": "Inspect the project"},
        {"role": "assistant_progress", "content": "Looking..."},
        {
            "role": "assistant_tool_call",
            "toolUseId": "tool-1",
            "toolName": "read_file",
            "input": {"path": "README.md"},
        },
        {
            "role": "tool_result",
            "toolUseId": "tool-1",
            "toolName": "read_file",
            "content": "project docs",
            "isError": False,
        },
        {"role": "assistant", "content": "Done"},
        {"role": "assistant", "content": "Provider failed", "isError": True},
    ]

    history = serialize_session_history(messages)

    assert [item["role"] for item in history] == [
        "user",
        "progress",
        "tool_call",
        "tool_result",
        "assistant",
        "assistant",
    ]
    assert history[2]["tool_input"] == {"path": "README.md"}
    assert history[3]["is_error"] is False
    assert history[-1]["is_error"] is True
    assert all(item.get("content") != "hidden prompt" for item in history)


def test_serialize_session_history_ignores_non_display_messages() -> None:
    history = serialize_session_history(
        [
            {"role": "assistant_thinking", "blocks": [{"type": "thinking"}]},
            {"role": "unknown", "content": "not supported"},
        ]
    )

    assert history == []


def test_agent_snapshot_is_scoped_to_active_session(tmp_path) -> None:
    import asyncio

    import pytest

    from pepsicode.core.session import create_new_session
    from pepsicode.server.app import ClientSession, PepsiCodeServer

    async def scenario():
        client = ClientSession(str(tmp_path))
        client.session_data = create_new_session(str(tmp_path))
        manager = client.session_data.agent_traces
        node = manager.create(agent_type="explore", name="Explore")
        server = PepsiCodeServer()
        snapshot = await server._dispatch(
            "agent/snapshot", {"trace_id": node.trace_id}, client, None, None, asyncio.get_running_loop()
        )
        assert snapshot["version"] == 1
        assert snapshot["session_id"] == client.session_data.session_id
        assert snapshot["records"][0]["trace_id"] == node.trace_id
        with pytest.raises(ValueError, match="active session"):
            await server._dispatch(
                "agent/snapshot", {"session_id": "other-session"}, client, None, None, asyncio.get_running_loop()
            )

    asyncio.run(scenario())


def test_agent_cancel_is_scoped_to_active_session(tmp_path, monkeypatch) -> None:
    import asyncio

    import pytest

    from pepsicode.core.session import create_new_session
    from pepsicode.server.app import ClientSession, PepsiCodeServer

    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", inline)
    client = ClientSession(str(tmp_path))
    client.session_data = create_new_session(str(tmp_path))
    seen = []
    monkeypatch.setattr(client.input_queue, "stop_worker", lambda trace_id: seen.append(trace_id) or "stop_requested")
    server = PepsiCodeServer()
    with pytest.raises(ValueError, match="active session"):
        server._dispatch(
            "agent/cancel", {"session_id": "another", "trace_id": "worker"}, client, None, None, None
        ).send(None)
    with pytest.raises(StopIteration) as result:
        server._dispatch("agent/cancel", {"trace_id": "worker"}, client, None, None, None).send(None)
    assert result.value.value == {"trace_id": "worker", "state": "stop_requested"}
    assert seen == ["worker"]


def test_worker_cancellation_withdraws_only_its_approval(tmp_path, monkeypatch):
    import asyncio
    from threading import Event

    import pytest

    from pepsicode.cancellation import TurnCancelledError
    from pepsicode.execution.runtime import CombinedCancellation, ExecutionScope, bind_scope
    from pepsicode.server.app import RemotePermissionBridge

    root_stop, worker_stop = Event(), Event()
    events = []

    async def emit(event, payload):
        events.append((event, payload))
        if event == "permission/request":
            worker_stop.set()

    def inline(coroutine, loop):
        with pytest.raises(StopIteration):
            coroutine.send(None)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", inline)
    bridge = RemotePermissionBridge(None, emit, root_stop)
    scope = ExecutionScope(
        str(tmp_path), parent_task_id="parent", cancellation_event=CombinedCancellation(root_stop, worker_stop)
    )
    with bind_scope(scope), pytest.raises(TurnCancelledError):
        bridge({"kind": "command", "summary": "Local command"})
    assert [event for event, _ in events] == ["permission/request", "permission/withdrawn"]
    assert events[0][1]["prompt_id"] == events[1][1]["prompt_id"]
    assert not root_stop.is_set()
    assert not bridge.resolve(events[0][1]["prompt_id"], {"decision": "allow_once"})
