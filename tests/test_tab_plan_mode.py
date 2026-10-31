import threading
from unittest.mock import Mock

import pytest

from pepsicode.core.session import create_new_session, load_session, save_session
from pepsicode.main import _prepare_tty_startup_permissions
from pepsicode.permissions import PermissionManager, PermissionMode
from pepsicode.tooling import ToolRegistry
from pepsicode.tui import app
from pepsicode.tui.input_parser import KeyEvent


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    permissions = PermissionManager(str(tmp_path), mode="auto")
    args = app.TtyAppArgs(
        runtime=None, tools=ToolRegistry([]), model=None, messages=[], cwd=str(tmp_path), permissions=permissions
    )
    state = app.ScreenState(session=create_new_session(str(tmp_path)), input="保留 draft", cursor_offset=3)
    state.autosave = Mock()
    return args, state


def press_tab(args, state):
    app._handle_event(args, state, KeyEvent("tab", False, False), lambda: None, threading.Event(), {})


def test_new_tty_session_starts_in_plan_without_saving_an_empty_session(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "session-state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "session-state" / "sessions")
    session = create_new_session(str(tmp_path))
    permissions = PermissionManager(str(tmp_path), mode="auto")

    app._initialize_tty_session_permissions(permissions, session, fresh=True)

    assert permissions.is_plan_mode
    assert permissions.access_mode.value == session.access_mode
    assert session.permission_mode == "plan"
    assert session.plan_file_path == permissions.plan_file_path
    save_session(session)
    assert load_session(session.session_id) is None


def test_existing_tty_session_keeps_its_saved_mode(tmp_path):
    session = create_new_session(str(tmp_path))
    permissions = PermissionManager(str(tmp_path), mode="auto")
    app._initialize_tty_session_permissions(permissions, session, fresh=False)
    assert not permissions.is_plan_mode

    session.plan_file_path = permissions.enter_plan_mode()
    session.permission_mode = "plan"
    restored = PermissionManager(str(tmp_path), mode="auto")
    app._initialize_tty_session_permissions(restored, session, fresh=False)
    assert restored.is_plan_mode
    assert restored.plan_file_path == session.plan_file_path


def test_startup_permissions_match_new_and_resumed_tty_sessions(tmp_path, monkeypatch):
    fresh = PermissionManager(str(tmp_path), mode="auto")
    _prepare_tty_startup_permissions(fresh, str(tmp_path), None)
    assert fresh.is_plan_mode

    saved = create_new_session(str(tmp_path))
    monkeypatch.setattr("pepsicode.main.load_session", lambda _: saved)
    restored = PermissionManager(str(tmp_path), mode="auto")
    _prepare_tty_startup_permissions(restored, str(tmp_path), saved.session_id)
    assert not restored.is_plan_mode


def test_plan_and_build_labels_use_distinct_colors(terminal, monkeypatch):
    from pepsicode.tui import chrome

    args, _ = terminal
    monkeypatch.setattr(app, "BRIGHT_CYAN", "<plan>")
    monkeypatch.setattr(app, "ACCENT", "<build>")
    monkeypatch.setattr(chrome, "BRIGHT_CYAN", "<plan>")
    monkeypatch.setattr(chrome, "ACCENT", "<build>")
    assert app._permission_mode_label(args).startswith("<build>BUILD")
    assert chrome.render_footer_bar(None, False, False).startswith("<build>BUILD")
    args.permissions.enter_plan_mode()
    assert app._permission_mode_label(args).startswith("<plan>PLAN")
    assert chrome.render_footer_bar(None, False, False, plan=True).startswith("<plan>PLAN")


@pytest.mark.parametrize("access", ["auto", "read_only", "trust_commands", "full_access"])
def test_tab_toggles_without_approving_and_preserves_draft_and_access(terminal, access):
    args, state = terminal
    args.permissions.set_access_mode(access)
    press_tab(args, state)
    assert args.permissions.is_plan_mode
    assert args.permissions.access_mode.value == access
    assert state.session.permission_mode == "plan"
    path = args.permissions.plan_file_path
    from pathlib import Path

    plan = Path(path)
    plan.parent.mkdir(parents=True)
    plan.write_text("plan draft", encoding="utf-8")
    press_tab(args, state)
    assert args.permissions.mode.value == access
    assert state.session.access_mode == access
    assert state.session.permission_mode == access
    assert state.session.plan_file_path is None
    assert args.permissions.consume_plan_followup() is None
    assert plan.read_text(encoding="utf-8") == "plan draft"
    assert (state.input, state.cursor_offset) == ("保留 draft", 3)
    assert state.autosave.mark_dirty.call_count == 2


def test_tab_completes_slash_and_does_not_toggle_during_busy_or_approval(terminal):
    args, state = terminal
    state.input = "/perm"
    state.cursor_offset = len(state.input)
    press_tab(args, state)
    assert state.input == "/permissions "
    assert args.permissions.mode == PermissionMode.AUTO
    state.input = "draft"
    state.is_busy = True
    press_tab(args, state)
    assert args.permissions.mode == PermissionMode.AUTO
    state.is_busy = False
    state.pending_approval = app.PendingApproval({"choices": []}, Mock())
    press_tab(args, state)
    assert args.permissions.mode == PermissionMode.AUTO


def test_tab_mode_survives_session_restore(terminal, tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "session-state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "session-state" / "sessions")
    args, state = terminal
    args.permissions.set_access_mode("read_only")
    press_tab(args, state)
    save_session(state.session)
    loaded = load_session(state.session.session_id)
    restored = PermissionManager(str(tmp_path), mode="auto")
    restored.set_access_mode(loaded.access_mode)
    restored.restore_plan_state(loaded.permission_mode, loaded.plan_file_path)
    assert restored.is_plan_mode and restored.access_mode == PermissionMode.READ_ONLY
    restored.leave_plan_mode()
    assert restored.mode == PermissionMode.READ_ONLY
