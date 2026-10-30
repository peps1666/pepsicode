import threading

import pytest

from pepsicode.core.session import SessionMetadata, create_new_session, save_session
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolRegistry
from pepsicode.tui import app
from pepsicode.tui.chrome import string_display_width, strip_ansi
from pepsicode.tui.input_parser import KeyEvent, WheelEvent


@pytest.fixture
def saved_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "home" / "sessions")
    monkeypatch.setattr(app, "save_history_entries", lambda _: None)
    workspace = tmp_path / "project"
    workspace.mkdir()
    sessions = []
    for topic in ("修复登录页面", "分析项目结构"):
        session = create_new_session(str(workspace))
        session.messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "internal": True, "content": "internal recovery prompt"},
            {"role": "user", "content": topic},
            {"role": "assistant", "content": f"关于{topic}的回答"},
        ]
        save_session(session)
        sessions.append(session)
    foreign = create_new_session(str(tmp_path / "other"))
    foreign.messages = [{"role": "user", "content": "其他目录的对话"}]
    save_session(foreign)
    args = app.TtyAppArgs(
        runtime=None,
        tools=ToolRegistry([]),
        model=MockModelAdapter(),
        messages=[{"role": "system", "content": "current"}],
        cwd=str(workspace),
        permissions=PermissionManager(str(workspace), mode="auto"),
    )
    return args, app.ScreenState(), sessions


def send(args, state, event):
    app._handle_event(args, state, event, lambda: None, threading.Event(), {})


def test_slash_completion_lists_topics_and_resumes_selected_conversation(saved_sessions):
    args, state, sessions = saved_sessions
    state.input, state.cursor_offset = "/res", 4
    send(args, state, KeyEvent("return", False, False))
    options = state.resume_picker_sessions
    assert {item.first_message for item in options} == {"分析项目结构", "修复登录页面"}
    frame = strip_ansi("\n".join(app._render_resume_picker_lines(options, 0, 80, 16)))
    assert "分析项目结构" in frame and "修复登录页面" in frame
    assert "internal recovery" not in frame and sessions[0].session_id not in frame
    if options[0].session_id != sessions[0].session_id:
        send(args, state, KeyEvent("down", False, False))
    send(args, state, KeyEvent("return", False, False))
    assert state.resume_picker_sessions is None
    assert state.session.session_id == sessions[0].session_id
    assert args.messages[-1]["content"] == "关于修复登录页面的回答"


@pytest.mark.parametrize("cols,rows", [(32, 7), (40, 9), (80, 15)])
def test_resume_list_keeps_selected_topic_visible_in_small_terminal(cols, rows):
    sessions = [SessionMetadata(str(i), 0, 0, first_message=f"主题{i} 中文内容") for i in range(40)]
    rendered = app._render_resume_picker_lines(sessions, 27, cols, rows)
    assert len(rendered) <= rows
    assert all(string_display_width(strip_ansi(line)) <= cols for line in rendered)
    assert any("> 主题27" in strip_ansi(line) for line in rendered)


def test_mouse_wheel_moves_selection_and_escape_leaves_session_unchanged(saved_sessions):
    args, state, _ = saved_sessions
    app._handle_input(args, state, lambda: None, "/resume")
    send(args, state, WheelEvent("down"))
    assert state.resume_picker_index == 1
    send(args, state, KeyEvent("escape", False, False))
    assert state.resume_picker_sessions is None and state.session is None


def test_empty_sessions_are_not_persisted_and_old_empty_entries_are_hidden(saved_sessions):
    import json

    from pepsicode.core import session as store

    args, state, sessions = saved_sessions
    empty = create_new_session(args.cwd)
    empty.messages = [{"role": "system", "content": "large system prompt"}]
    empty.model_settings = {"model": "example", "reasoningEffort": "default"}
    save_session(empty)
    assert not store._session_file(empty.session_id).exists()
    # Simulate a file written by an old version; hiding it is not deletion.
    store._save_session_locked(empty)
    old_path = store._session_file(empty.session_id)
    assert old_path.exists()
    index_path = store._session_index_file()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index[empty.session_id].pop("has_visible_content")
    index[empty.session_id]["first_message"] = "misleading internal prompt"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    app._handle_input(args, state, lambda: None, "/resume")
    assert {item.session_id for item in state.resume_picker_sessions} == {s.session_id for s in sessions}
    assert old_path.exists()


def test_incomplete_work_without_user_message_is_still_saved_and_shown(saved_sessions):
    from pepsicode.core import session as store

    args, state, _ = saved_sessions
    pending = create_new_session(args.cwd)
    pending.messages = [{"role": "system", "content": "system"}]
    pending.execution_state = {"state": "unknown", "journal": "unresolved.jsonl"}
    save_session(pending)
    assert store._session_file(pending.session_id).exists()
    app._handle_input(args, state, lambda: None, "/resume")
    assert pending.session_id in {entry.session_id for entry in state.resume_picker_sessions}
    rows = app._render_resume_picker_lines(state.resume_picker_sessions, 0, 80, 20)
    assert "待恢复的会话状态" in strip_ansi("\n".join(rows))


def test_empty_qq_bridge_session_remains_resumable_without_cluttering_history(saved_sessions):
    from pepsicode.core import session as store

    args, state, _ = saved_sessions
    bridge = create_new_session(args.cwd)
    bridge.profile = "qq_private"
    bridge.messages = [{"role": "system", "content": "system"}]
    save_session(bridge)
    assert store.load_session(bridge.session_id).profile == "qq_private"
    app._handle_input(args, state, lambda: None, "/resume")
    assert bridge.session_id not in {entry.session_id for entry in state.resume_picker_sessions}


def test_archived_branches_are_not_lost_when_current_branch_is_empty(saved_sessions):
    from pepsicode.core import session as store

    args, _, sessions = saved_sessions
    saved = sessions[0]
    saved.messages = [{"role": "system", "content": "system"}]
    save_session(saved)
    reloaded = store.load_session(saved.session_id)
    assert reloaded is not None and reloaded.tree["nodes"]
    assert saved.session_id in {item.session_id for item in store.list_sessions(args.cwd)}
