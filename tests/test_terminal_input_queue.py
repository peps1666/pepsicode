import threading

import pytest

from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolRegistry
from pepsicode.tui import app
from pepsicode.tui.input_parser import KeyEvent, TerminalInputDecoder
from pepsicode.types import StreamToken


@pytest.mark.parametrize("cancel", [False, True])
def test_terminal_runs_queued_input_and_returns_unsent_drafts(tmp_path, monkeypatch, cancel):
    started, release = threading.Event(), threading.Event()
    requests = []
    monkeypatch.setattr(app, "save_history_entries", lambda entries: None)

    class Model(MockModelAdapter):
        def next_stream(self, messages):
            requests.append([dict(message) for message in messages])
            if len(requests) == 1:
                started.set()
                assert release.wait(3)
            yield StreamToken(type="text", content="done")
            yield StreamToken(type="done")

    args = app.TtyAppArgs(
        runtime={"verificationMode": "off"},
        model=Model(),
        tools=ToolRegistry([]),
        messages=[{"role": "system", "content": "test"}],
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path), mode="full_access"),
    )
    state = app.ScreenState()
    try:
        app._handle_input(args, state, lambda: None, "initial task")
        assert started.wait(2)
        state.input, state.cursor_offset = "adjust current task", 19
        app._handle_normal_mode_key(args, state, KeyEvent(name="return", ctrl=False, meta=False), [], lambda: None)
        state.input, state.cursor_offset = "next task", 9
        for event in TerminalInputDecoder().feed(b"\x1b\r"):
            app._handle_event(args, state, event, lambda: None, threading.Event(), {})
        assert state.input == "" and len(state.pending_inputs) == 2
        assert [item["mode"] for item in state.pending_inputs] == ["steer", "follow_up"]
        state.input, state.cursor_offset = "still editing", 13
        if cancel:
            state.cancellation_event.set()
        release.set()
        state.agent_thread.join(3)
        assert not state.agent_thread.is_alive()
        app._collect_agent_result(args, state)
        assert state.pending_inputs == []
        if cancel:
            assert state.input == "adjust current task\n\nnext task\n\nstill editing"
            assert len(requests) == 1
        else:
            assert state.input == "still editing"
            assert len(requests) == 3
            assert [entry.body for entry in state.transcript if entry.kind == "user"] == [
                "initial task",
                "adjust current task",
                "next task",
            ]
    finally:
        release.set()
        state.cancellation_event.set()
        if state.agent_thread:
            state.agent_thread.join(3)


def test_terminal_queue_prefix_and_take_back_keep_the_existing_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "save_history_entries", lambda entries: None)
    args = app.TtyAppArgs(
        runtime=None,
        model=MockModelAdapter(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path)),
    )
    state = app.ScreenState(is_busy=True, input="/queue next task")

    def update(data):
        state.pending_inputs = data["pending"]
        state.returned_inputs.extend(item["content"] for item in data["returned"])

    state.input_queue.start(update)
    app._handle_normal_mode_return(args, state, [], lambda: None)
    assert state.input == "" and state.pending_inputs[0]["mode"] == "follow_up"
    state.input = "draft"
    app._handle_normal_mode_key(args, state, KeyEvent(name="up", ctrl=False, meta=True), [], lambda: None)
    assert state.input == "next task\n\ndraft"
    assert not state.pending_inputs


def test_busy_slash_command_is_not_executed_or_discarded(tmp_path):
    args = app.TtyAppArgs(
        runtime=None,
        model=MockModelAdapter(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path)),
    )
    state = app.ScreenState(is_busy=True, input="/permissions full_access")
    state.input_queue.start()
    app._handle_normal_mode_return(args, state, [], lambda: None)
    assert state.input == "/permissions full_access"
    assert "slash commands" in state.status
    assert args.permissions.access_mode.value != "full_access"


@pytest.mark.parametrize("payload", [b"\x1b\r", b"\x1b\n", b"\x1b\r\n"])
def test_alt_enter_bytes_decode_as_one_modified_return(payload):
    assert TerminalInputDecoder().feed(payload) == [KeyEvent(name="return", ctrl=False, meta=True)]


@pytest.mark.parametrize("payload", [b"\x1b\r", b"\x1b\n"])
def test_alt_enter_split_across_reads_still_decodes_as_modified_return(payload):
    decoder = TerminalInputDecoder()
    events = []
    for byte in payload:
        events.extend(decoder.feed(bytes([byte])))
    assert events == [KeyEvent(name="return", ctrl=False, meta=True)]
