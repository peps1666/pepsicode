"""Exercise raw input and live session boundaries, rather than UI helpers alone."""

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pepsicode.core.session import AutosaveManager, create_new_session, load_session, save_session
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.run_command import run_command_tool
from pepsicode.tui import app
from pepsicode.tui.input_parser import PasteEvent, TerminalInputDecoder, parse_input_chunk
from pepsicode.tui.types import TranscriptEntry
from pepsicode.types import StreamToken


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    monkeypatch.setattr(app, "save_history_entries", lambda history: None)
    workspace = tmp_path / "project"
    workspace.mkdir()
    args = app.TtyAppArgs(
        runtime=None,
        tools=ToolRegistry([]),
        model=MockModelAdapter(),
        messages=[{"role": "system", "content": "system"}],
        cwd=str(workspace),
        permissions=PermissionManager(str(workspace), mode="auto"),
    )
    session = create_new_session(str(workspace))
    return args, app.ScreenState(session=session, autosave=AutosaveManager(session, interval=0))


def dispatch(args, state, text):
    for event in parse_input_chunk(text).events:
        app._handle_event(args, state, event, lambda: None, threading.Event(), {})


@pytest.mark.parametrize("shortcut,cursor", [("\x01", 0), ("\x05", 5)])
def test_ctrl_navigation_from_raw_input(terminal, shortcut, cursor):
    args, state = terminal
    state.input, state.cursor_offset = "draft", 2
    dispatch(args, state, shortcut)
    assert state.input == "draft"
    assert state.cursor_offset == cursor


def test_ctrl_clear_and_history_from_raw_input(terminal):
    args, state = terminal
    state.input, state.cursor_offset = "draft", 5
    state.history, state.history_index = ["previous"], 1
    dispatch(args, state, "\x10")
    assert state.input == "previous"
    dispatch(args, state, "\x0e")
    assert state.input == "draft"
    dispatch(args, state, "\x15")
    assert state.input == "" and state.cursor_offset == 0


def test_wheel_scrolls_transcript_without_changing_draft_or_history(terminal, monkeypatch):
    args, state = terminal
    state.input, state.cursor_offset = "draft", 5
    state.history, state.history_index = ["previous"], 1
    monkeypatch.setattr(app, "_get_stream_max_scroll_offset", lambda *_: 20)
    dispatch(args, state, "\x1b[<64;10;10M")
    assert state.transcript_scroll_offset > 0
    assert (state.input, state.cursor_offset, state.history_index) == ("draft", 5, 1)
    dispatch(args, state, "\x1b[<65;10;10M")
    assert state.transcript_scroll_offset == 0
    assert (state.input, state.cursor_offset, state.history_index) == ("draft", 5, 1)
    dispatch(args, state, "\x1b[A")
    assert state.input == "previous"


def test_busy_enter_keeps_draft_and_never_submits(terminal):
    args, state = terminal
    state.is_busy = True
    state.input, state.cursor_offset = "next request", 12
    dispatch(args, state, "\r")
    assert state.input == "next request" and state.cursor_offset == 12
    assert args.messages == [{"role": "system", "content": "system"}]


@pytest.mark.parametrize("bracketed", [False, True])
def test_multiline_paste_is_an_edit_until_explicit_enter(terminal, monkeypatch, bracketed):
    args, state = terminal
    pasted = "first line\r\nsecond line\r\n"
    if bracketed:
        pasted = "\x1b[200~" + pasted + "\x1b[201~"
    submit = Mock(return_value=False)
    monkeypatch.setattr(app, "_handle_input", submit)
    dispatch(args, state, pasted)
    assert state.input == "first line\nsecond line\n"
    submit.assert_not_called()
    dispatch(args, state, "\r")
    assert submit.call_args.args[3] == "first line\nsecond line\n"


def test_paste_boundaries_and_utf8_can_split_at_every_byte(terminal):
    args, state = terminal
    decoder = TerminalInputDecoder()
    payload = "\x1b[200~你好\n👋 second line\x1b[201~".encode()
    events = []
    for byte in payload:
        events.extend(decoder.feed(bytes([byte])))
    assert events == [PasteEvent("你好\n👋 second line")]
    app._handle_event(args, state, events[0], lambda: None, threading.Event(), {})
    assert state.input == "你好\n👋 second line"


def test_split_utf8_without_paste_reconstructs_text():
    decoder = TerminalInputDecoder()
    events = []
    for byte in "中文😀".encode():
        events.extend(decoder.feed(bytes([byte])))
    assert "".join(event.text for event in events) == "中文😀"


def test_paste_does_not_approve_permission(terminal):
    args, state = terminal
    state.pending_approval = app.PendingApproval({"choices": []}, Mock())
    approval = threading.Event()
    app._handle_event(args, state, PasteEvent("y\n"), lambda: None, approval, {})
    assert not approval.is_set()
    state.pending_approval.resolve.assert_not_called()


def test_foreign_session_is_rejected_before_changing_live_state(terminal, tmp_path):
    args, state = terminal
    foreign = create_new_session(str(tmp_path / "other"))
    foreign.messages = [{"role": "user", "content": "edit other project"}]
    save_session(foreign)
    original_session = state.session
    original_messages = list(args.messages)
    assert not app._resume_session_by_id(args, state, foreign.session_id)
    assert state.session is original_session
    assert args.messages == original_messages
    assert "Change to that directory" in state.status
    assert all(item.workspace == args.cwd for item in app._session_rows_for_picker(args.cwd))


def test_startup_resume_rejects_foreign_session_before_entering_raw_mode(terminal, tmp_path, monkeypatch, capsys):
    args, _ = terminal
    foreign = create_new_session(str(tmp_path / "other"))
    foreign.messages = [{"role": "user", "content": "existing conversation"}]
    save_session(foreign)
    raw = Mock(side_effect=AssertionError("must reject before opening the terminal"))
    monkeypatch.setattr(app, "_RawModeContext", raw)
    app.run_tty_app(
        runtime=None,
        tools=args.tools,
        model=args.model,
        messages=args.messages,
        cwd=args.cwd,
        permissions=args.permissions,
        resume_session=foreign.session_id,
        memory=Mock(),
    )
    assert "Change to that directory" in capsys.readouterr().out
    raw.assert_not_called()


def test_finished_turn_autosaves_messages_and_transcript_before_exit(terminal):
    args, state = terminal
    messages = [*args.messages, {"role": "user", "content": "hello"}, {"role": "assistant", "content": "answer"}]
    state.transcript = [TranscriptEntry(id=1, kind="assistant", body="answer")]
    state.history = ["hello"]
    state.agent_lock = threading.Lock()
    state.agent_result = {"done": True, "messages": messages}
    assert app._collect_agent_result(args, state)
    assert state.autosave.save_if_needed()
    saved = load_session(state.session.session_id)
    assert saved.messages == messages
    assert saved.transcript_entries[0]["body"] == "answer"
    assert saved.history == ["hello"]
    assert not app._collect_agent_result(args, state)


def test_snapshot_preserves_in_progress_transcript(terminal):
    args, state = terminal
    args.messages.append({"role": "user", "content": "question"})
    state.transcript = [TranscriptEntry(id=1, kind="assistant", body="partial answer")]
    app._snapshot_session(args, state)
    state.autosave.mark_dirty()
    assert state.autosave.save_if_needed()
    saved = load_session(state.session.session_id)
    assert saved.messages[-1]["content"] == "question"
    assert saved.transcript_entries[0]["body"] == "partial answer"
    state.transcript[0].body = "partial answer, continued"
    app._snapshot_session(args, state)
    assert state.autosave.save_if_needed()
    assert load_session(state.session.session_id).transcript_entries[0]["body"] == "partial answer, continued"


def test_ctrl_c_stops_stream_without_exiting_and_allows_next_turn(terminal):
    args, state = terminal
    ready = threading.Event()

    class StreamingModel:
        def next_stream(self, messages):
            yield StreamToken(type="text", content="partial")
            ready.set()
            assert state.cancellation_event.wait(2)
            yield StreamToken(type="tool_use", tool_name="must_not_run", tool_id="late", tool_input_partial="{}")
            yield StreamToken(type="done")

    run = Mock(return_value=ToolResult(True, "unexpected"))
    args.model = StreamingModel()
    args.tools = ToolRegistry([ToolDefinition("must_not_run", "", {}, lambda value: value, run)])
    app._handle_input(args, state, lambda: None, "start")
    assert ready.wait(2)
    dispatch(args, state, "\x03\x03")
    state.agent_thread.join(3)
    assert not state.agent_thread.is_alive()
    assert not state.is_busy and state.exit_requested_at is None
    run.assert_not_called()
    app._collect_agent_result(args, state)
    assert args.messages[-1]["content"] == "Task stopped by user."
    args.model = MockModelAdapter()
    app._handle_input(args, state, lambda: None, "hello")
    state.agent_thread.join(3)
    assert not state.cancellation_event.is_set()
    assert not state.agent_thread.is_alive()


def test_cancelled_tool_batch_skips_remaining_writes(terminal):
    from pepsicode.core.agent_loop import run_agent_turn
    from pepsicode.types import AgentStep

    args, state = terminal
    executed = []

    def first(_value, context):
        executed.append("first")
        context.cancellation_event.set()
        return ToolResult(True, "first finished")

    second = Mock(return_value=ToolResult(True, "second"))
    args.tools = ToolRegistry(
        [
            ToolDefinition("first", "", {}, lambda value: value, first),
            ToolDefinition("second", "", {}, lambda value: value, second),
        ]
    )
    model = SimpleNamespace(
        next=Mock(
            return_value=AgentStep(
                type="tool_calls",
                calls=[
                    {"id": "1", "toolName": "first", "input": {}},
                    {"id": "2", "toolName": "second", "input": {}},
                ],
            )
        )
    )
    result = run_agent_turn(
        model=model, tools=args.tools, messages=args.messages, cwd=args.cwd, cancellation_event=state.cancellation_event
    )
    assert executed == ["first"]
    second.assert_not_called()
    assert result[-1]["content"] == "Task stopped by user."
    assert len([message for message in result if message["role"] == "tool_result"]) == 2


def test_command_shortcut_stays_responsive_and_receives_cancel(terminal):
    args, state = terminal
    ready = threading.Event()

    def command(_value, context):
        ready.set()
        assert context.cancellation_event.wait(2)
        return ToolResult(False, "Command cancelled")

    args.tools = ToolRegistry([ToolDefinition("run_command", "", {}, lambda value: value, command)])
    app._handle_input(args, state, lambda: None, "/cmd fake-command")
    assert ready.wait(2)
    dispatch(args, state, "next draft")
    assert state.input == "next draft"
    dispatch(args, state, "\x03")
    state.agent_thread.join(3)
    assert not state.agent_thread.is_alive() and not state.is_busy
    app._collect_agent_result(args, state)
    state.autosave.save_if_needed()
    assert "Command cancelled" in load_session(state.session.session_id).transcript_entries[-1]["body"]


def test_stop_propagates_to_subagent_before_it_requests_model(terminal):
    from pepsicode.tools.task import create_task_tool

    args, state = terminal
    model = Mock()
    task = create_task_tool(args.cwd, None, model_factory=lambda registry: model)
    state.cancellation_event.set()
    result = task.run(
        {"agent_type": "explore", "task": "inspect"},
        ToolContext(cwd=args.cwd, cancellation_event=state.cancellation_event),
    )
    assert "Task stopped by user." in result.output
    model.next.assert_not_called()


def test_cancel_command_kills_its_child_tree(terminal, tmp_path):
    args, state = terminal
    started = tmp_path / "child-started"
    survived = tmp_path / "child-survived"
    child = (
        f"from pathlib import Path; import time; Path({str(started)!r}).write_text('ready'); "
        f"time.sleep(1.5); Path({str(survived)!r}).write_text('bad')"
    )
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}]); import time; time.sleep(30)"
    result = []
    context = ToolContext(cwd=args.cwd, cancellation_event=state.cancellation_event)
    thread = threading.Thread(
        target=lambda: result.append(
            ToolRegistry([run_command_tool]).execute(
                "run_command", {"command": sys.executable, "args": ["-c", parent]}, context
            )
        )
    )
    thread.start()
    try:
        for _ in range(200):
            if started.exists():
                break
            threading.Event().wait(0.01)
        assert started.exists()
        state.cancellation_event.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not result[0].ok and "cancelled" in result[0].output
        threading.Event().wait(1.6)
        assert not survived.exists()
    finally:
        state.cancellation_event.set()
        thread.join(5)


@pytest.mark.parametrize("stream", [False, True])
def test_cancel_interrupts_http_body_with_no_new_tokens(terminal, stream):
    from pepsicode.core.agent_loop import run_agent_turn, run_agent_turn_stream
    from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter

    args, state = terminal
    ready, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.end_headers()
            self.wfile.flush()
            ready.set()
            release.wait(5)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    model = AnthropicModelAdapter(
        {"model": "test", "baseUrl": f"http://127.0.0.1:{server.server_port}", "apiKey": "test"}, args.tools
    )
    run = run_agent_turn_stream if stream else run_agent_turn
    result = []
    worker = threading.Thread(
        target=lambda: result.extend(
            run(
                model=model,
                tools=args.tools,
                messages=args.messages,
                cwd=args.cwd,
                cancellation_event=state.cancellation_event,
            )
        )
    )
    worker.start()
    try:
        assert ready.wait(3)
        state.cancellation_event.set()
        worker.join(3)
        assert not worker.is_alive()
        assert result[-1]["content"] == "Task stopped by user."
    finally:
        state.cancellation_event.set()
        release.set()
        worker.join(3)
        server.shutdown()
        server.server_close()
