import threading
from unittest.mock import Mock

import pytest

from pepsicode.core.session import create_new_session, load_session, save_session
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
