from types import SimpleNamespace

import pytest

from pepsicode.agents.status import STEP_LIMIT_MESSAGE, format_trace_view, trace_view
from pepsicode.agents.trace import Activity, AgentReport, TraceManager
from pepsicode.core import session as sessions
from pepsicode.tui import app
from pepsicode.tui.chrome import string_display_width, strip_ansi
from pepsicode.tui.input_parser import MouseEvent
from pepsicode.tui.types import TranscriptEntry


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    session = sessions.create_new_session(str(tmp_path))
    frames = []
    state = app.ScreenState(
        session=session,
        is_busy=True,
        input="keep my draft",
        transcript=[TranscriptEntry(id=1, kind="user", body="调查前端和后端")],
    )
    state.frame_renderer.draw = lambda lines, *_: frames.append(lines)
    args = SimpleNamespace(
        cwd=str(tmp_path),
        runtime={"model": "test-model"},
        permissions=SimpleNamespace(is_plan_mode=False, access_mode=SimpleNamespace(value="auto")),
    )
    return args, state, frames


def add_worker(manager, name="Explore"):
    node = manager.create(agent_type="explore", name=name, task="只读调查。\n调查主题：前端状态展示")
    manager.activity(node.trace_id, "read", Activity("read", "tool", "读取 src/client/provider.tsx"))
    return node


@pytest.mark.parametrize("cols, rows", [(132, 30), (110, 20), (80, 24), (40, 12), (24, 8)])
def test_agents_appear_without_command_and_fit_resizes(terminal, monkeypatch, cols, rows):
    args, state, frames = terminal
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (cols, rows))
    app._render_stream_screen(args, state)
    assert "子 Agent" not in "\n".join(frames[-1])
    node = add_worker(state.session.agent_traces)
    app._render_stream_screen(args, state)
    frame = "\n".join(map(strip_ansi, frames[-1]))
    if rows >= 12:
        assert "子 Agent" in frame
    assert (state.agent_panel_start_col is not None) is (cols >= 110)
    assert len(frames[-1]) <= rows
    assert all(string_display_width(line) <= cols for line in frames[-1])
    assert state.input == "keep my draft"
    state.session.agent_traces.record_report(node.trace_id, AgentReport("r", "ready", "找到状态入口", None, False))
    state.session.agent_traces.complete(node.trace_id, "budget_exhausted", STEP_LIMIT_MESSAGE)
    app._render_stream_screen(args, state)
    if rows >= 20:
        assert "达到轮数上限" in strip_ansi("\n".join(frames[-1]))


def test_sidebar_click_and_session_switch(terminal, monkeypatch):
    args, state, frames = terminal
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (132, 30))
    node = add_worker(state.session.agent_traces)
    app._render_stream_screen(args, state)
    y = next(y for y, action in state.agent_panel_actions.items() if action == ("detail", node.trace_id))
    x = state.agent_panel_start_col + 1
    # A sidebar click must not fold the conversation's activity on the same row.
    state.activity_header_rows[y] = 999
    for action in ("press", "release"):
        app._handle_selection_event(state, MouseEvent(action, x, y), lambda: None)
    assert node.trace_id in state.transcript[-1].body
    assert "调查主题：前端状态展示" in state.transcript[-1].body
    assert not state.expanded_activity_groups
    assert state.input == "keep my draft"
    state.session = sessions.create_new_session(args.cwd)
    app._render_stream_screen(args, state)
    assert state.agent_panel_start_col is None
    assert not state.agent_panel_actions


def test_active_worker_stays_visible_ahead_of_many_finished_attempts(terminal, monkeypatch):
    args, state, frames = terminal
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (132, 30))
    node = add_worker(state.session.agent_traces, "Active")
    for _ in range(20):
        ended = add_worker(state.session.agent_traces, "Previous")
        state.session.agent_traces.complete(ended.trace_id)
    app._render_stream_screen(args, state)
    assert "Active" in "\n".join(frames[-1])
    assert ("detail", node.trace_id) in state.agent_panel_actions.values()
    y = next(y for y, action in state.agent_panel_actions.items() if action[0] == "page")
    for action in ("press", "release"):
        app._handle_selection_event(state, MouseEvent(action, state.agent_panel_start_col, y), lambda: None)
    app._render_stream_screen(args, state)
    assert state.agent_panel_offset > 0
    assert "Previous" in "\n".join(frames[-1])
    add_worker(state.session.agent_traces, "NewWorker")
    app._render_stream_screen(args, state)
    assert state.agent_panel_offset == 0
    assert "NewWorker" in "\n".join(frames[-1])


def test_status_text_preserves_report_and_deduplicates_old_limit_errors():
    manager = TraceManager()
    node = add_worker(manager)
    manager.record_report(node.trace_id, AgentReport("r", "ready", "前端状态由轮询更新", None, False))
    manager.complete(node.trace_id, "failed", STEP_LIMIT_MESSAGE, result_summary=STEP_LIMIT_MESSAGE)
    output = format_trace_view(trace_view(manager, {}))
    assert "达到轮数上限" in output
    assert "前端状态由轮询更新" in output
    assert "调查主题：前端状态展示" in output
    assert "只读调查" not in output
    assert STEP_LIMIT_MESSAGE not in output
    assert "failed" not in output
    manager.set_persistence_error("disk unavailable")
    assert "保存失败" in format_trace_view(trace_view(manager, {}, offset=10))
