from types import SimpleNamespace

import pytest

from pepsicode.core.session import create_new_session, load_session
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolRegistry
from pepsicode.tui import app
from pepsicode.tui.chrome import string_display_width, strip_ansi
from pepsicode.tui.command_picker import CommandOption, CommandPicker, render_command_picker
from pepsicode.tui.input_parser import KeyEvent


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "home" / "sessions")
    monkeypatch.setattr("pepsicode.workspace_permissions.WORKSPACE_PERMISSIONS_PATH", tmp_path / "defaults.json")
    monkeypatch.setattr(app, "save_history_entries", lambda _history: None)
    args = app.TtyAppArgs(
        runtime=None,
        tools=ToolRegistry([]),
        model=MockModelAdapter(),
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path), mode="auto"),
        messages=[
            {"role": "system", "content": "test"},
            {"role": "user", "content": "第一个问题"},
            {"role": "assistant", "content": "第一个回答"},
            {"role": "user", "content": "第二个问题"},
            {"role": "assistant", "content": "第二个回答"},
        ],
    )
    state = app.ScreenState(session=create_new_session(str(tmp_path)))
    return args, state


def press(args, state, name):
    app._handle_normal_mode_event(args, state, KeyEvent(name=name, ctrl=False, meta=False), lambda: None)


def test_tree_selects_reply_and_fork_returns_user_draft(terminal):
    args, state = terminal
    original_id = state.session.session_id
    app._handle_input(args, state, lambda: None, "/tree")
    assert len(state.command_picker.options) == 4
    assert "第一个回答" in state.command_picker.options[1].label
    press(args, state, "down")
    press(args, state, "return")
    assert args.messages[-1]["content"] == "第一个回答"
    assert state.command_picker is None
    app._handle_input(args, state, lambda: None, "/fork")
    assert all("助手" not in option.label for option in state.command_picker.options)
    press(args, state, "return")
    assert state.session.session_id != original_id and state.input == "第一个问题", [e.body for e in state.transcript]
    assert load_session(original_id) is not None


def test_slash_completion_opens_tree_picker_without_entering_id(terminal):
    args, state = terminal
    state.input = "/tr"
    state.cursor_offset = len(state.input)
    press(args, state, "return")
    assert state.command_picker is not None and state.command_picker.title == "选择对话位置"
    press(args, state, "escape")
    assert state.command_picker is None


def test_default_permission_selection_preserves_current_mode(terminal):
    from pepsicode.workspace_permissions import get_workspace_default

    args, state = terminal
    app._handle_input(args, state, lambda: None, "/permissions default")
    press(args, state, "down")
    press(args, state, "down")
    assert "高风险" in state.command_picker.options[2].label
    press(args, state, "return")
    assert get_workspace_default(args.cwd) == "trust_commands"
    assert args.permissions.access_mode.value == "auto"


def test_default_permission_picker_is_discoverable_from_permissions_menu(terminal):
    args, state = terminal
    app._handle_input(args, state, lambda: None, "/permissions")
    assert state.settings_picker.options[-1].value == "__workspace_default__"
    state.settings_picker.selected = len(state.settings_picker.options) - 1
    press(args, state, "return")
    assert state.settings_picker is None
    assert state.command_picker.title == "工作区新会话默认权限"


def test_hooks_selection_uses_existing_trust_check(terminal):
    from pepsicode.hooks.engine import HookEngine
    from pepsicode.hooks.models import HookAction, HookActionType, HookDefinition, HookEvent

    args, state = terminal
    hook = HookDefinition(
        id="format-hook",
        event=HookEvent.AGENT_START,
        action=HookAction(HookActionType.NOTIFY),
        enabled=False,
        trusted=False,
    )
    args.hook_engine = HookEngine([hook])
    try:
        app._handle_input(args, state, lambda: None, "/hooks enable")
        assert "未信任" in state.command_picker.options[0].description
        press(args, state, "return")
        assert "untrusted" in state.transcript[-1].body and not hook.enabled
    finally:
        args.hook_engine.close()


def test_worktree_choice_requires_explicit_keep_or_remove(terminal, monkeypatch):
    args, state = terminal
    import pepsicode.worktree

    calls = []

    class Manager:
        def __init__(self, repo_root):
            assert repo_root == args.cwd

        def list_worktrees(self):
            return [SimpleNamespace(name="feature", path="E:/feature")]

        def restore_session(self):
            return None

        def exit(self, name, action):
            calls.append((name, action))

    monkeypatch.setattr(pepsicode.worktree, "WorktreeManager", Manager)
    app._handle_input(args, state, lambda: None, "/worktree exit")
    press(args, state, "return")
    assert [o.value for o in state.command_picker.options] == [
        "/worktree exit feature keep",
        "/worktree exit feature remove",
    ]
    press(args, state, "escape")
    assert state.command_picker is None and calls == []
    app._handle_input(args, state, lambda: None, "/worktree exit")
    press(args, state, "return")
    press(args, state, "return")
    assert calls == [("feature", "keep")]


def test_plan_mode_cannot_use_picker_to_enter_worktree(terminal):
    args, state = terminal
    args.permissions.enter_plan_mode()
    app._handle_input(args, state, lambda: None, "/worktree enter")
    assert state.command_picker is None and "Plan mode denied" in state.transcript[-1].body


@pytest.mark.parametrize("cols,rows", [(32, 8), (80, 24)])
def test_picker_fits_terminal_and_keeps_selection_visible(cols, rows):
    picker = CommandPicker("选择对话位置", [CommandOption(f"第{i}项 中文内容", str(i)) for i in range(30)], selected=20)
    lines = render_command_picker(picker, cols, rows)
    assert len(lines) <= rows and all(string_display_width(strip_ansi(line)) <= cols for line in lines)
    assert any("第20项" in line for line in lines)
