"""Access presets must apply to actual tools and survive session restoration."""

import asyncio
from unittest.mock import Mock

import pytest

from pepsicode.approval import ApprovalDecision, ApprovalKind, ApprovalOutcome
from pepsicode.cli.cli_commands import try_handle_local_command
from pepsicode.core.session import create_new_session, load_session, save_session
from pepsicode.permissions import PermissionManager, PermissionMode
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.edit_file import edit_file_tool
from pepsicode.tools.run_command import run_command_tool
from pepsicode.tui.app import (
    ScreenState,
    TtyAppArgs,
    _handle_input,
    _handle_normal_mode_return,
    _handle_settings_picker_event,
)
from pepsicode.tui.input_parser import KeyEvent
from pepsicode.tui.settings_picker import render_settings_picker


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")


def test_auto_edits_multiple_files_without_prompt_but_checks_external_paths(tmp_path):
    seen = []
    manager = PermissionManager(str(tmp_path), mode="auto", prompt=lambda req: seen.append(req) or {"decision": "allow_once"})
    for name in ("a.py", "b.py"):
        assert manager.check_edit(str(tmp_path / name), "+ changed").is_allowed
    assert not seen
    external = str(tmp_path.parent / "outside.py")
    assert manager.check_edit(external, "+ changed").is_allowed
    assert manager.check_edit(external, "+ next change").is_allowed
    assert [req["kind"] for req in seen] == ["path"]
    assert manager.check_command("git", ["reset", "--hard"], str(tmp_path)).is_allowed
    assert seen[-1]["kind"] == "command"
    assert manager.check_mcp_tool("mcp__remote__write", {}).is_allowed
    assert seen[-1]["kind"] == "mcp"


def test_read_only_blocks_mutations_even_with_cached_allow(tmp_path):
    manager = PermissionManager(str(tmp_path), mode="read_only")
    target = tmp_path / "file.txt"
    target.write_text("before", encoding="utf-8")
    manager.store.record(ApprovalKind.EDIT, str(target), ApprovalOutcome(ApprovalDecision.ALLOW_ONCE))
    registry = ToolRegistry([edit_file_tool, run_command_tool])
    context = ToolContext(cwd=str(tmp_path), permissions=manager)
    result = registry.execute("edit_file", {"path": str(target), "old": "before", "new": "after"}, context)
    assert not result.ok
    assert "Read-only mode" in result.output
    assert not registry.execute("run_command", {"command": "echo", "args": ["hello"]}, context).ok
    assert target.read_text(encoding="utf-8") == "before"
    assert manager.check_path_access(str(target), "read").is_allowed
    assert manager.check_edit(str(target), "+ diff").is_denied
    assert manager.check_command("git", ["status"], str(tmp_path)).is_denied
    assert manager.check_mcp_tool("mcp__remote__write", {}).is_denied
    with pytest.raises(RuntimeError, match="Read-only"):
        manager.ensure_local_command_allowed("/worktree cleanup")


def test_full_access_skips_prompts_but_preserves_plan_boundary(tmp_path):
    def unexpected_prompt(_req):
        pytest.fail("Full access should not prompt")

    manager = PermissionManager(str(tmp_path), mode="full_access", prompt=unexpected_prompt)
    external = str(tmp_path.parent / "outside.py")
    assert manager.check_edit(external, "+ diff").is_allowed
    assert manager.check_command("rm", ["-rf", "x"], str(tmp_path.parent)).is_allowed
    assert manager.check_mcp_tool("mcp__remote__write", {}).is_allowed
    plan = manager.enter_plan_mode()
    assert manager.check_edit(plan, "+ plan").is_allowed
    assert manager.check_edit(external, "+ diff").is_denied
    assert manager.check_command("echo", ["hi"], str(tmp_path)).is_denied
    assert manager.check_mcp_tool("mcp__remote__write", {}).is_denied
    manager.set_access_mode("auto")
    assert manager.is_plan_mode
    assert manager.access_mode == PermissionMode.AUTO


def test_auto_respects_explicit_edit_denials(tmp_path):
    manager = PermissionManager(str(tmp_path), mode="auto")
    target = str(tmp_path / "protected.py")
    manager.store.record(ApprovalKind.EDIT, target, ApprovalOutcome(ApprovalDecision.DENY_ONCE))
    assert manager.check_edit(target, "+ diff").is_denied


def test_cli_switches_and_rejects_invalid_modes(tmp_path):
    manager = PermissionManager(str(tmp_path), mode="auto")
    assert "full_access" in try_handle_local_command("/permissions full_access", permissions=manager)
    assert manager.mode == PermissionMode.FULL_ACCESS
    assert "Usage" in try_handle_local_command("/permissions invalid", permissions=manager)
    assert manager.mode == PermissionMode.FULL_ACCESS


def test_server_persists_mode_and_rejects_changes_during_turn(tmp_path):
    client = ClientSession(str(tmp_path))
    client.permissions = PermissionManager(str(tmp_path), mode="auto")
    client.session_data = create_new_session(str(tmp_path))
    client._rebuild_system_prompt = Mock()

    async def run():
        server = PepsiCodeServer()
        loop = asyncio.get_running_loop()
        result = await server._dispatch("permissions/set", {"mode": "full_access"}, client, None, None, loop)
        assert result == {"access_mode": "full_access"}
        loaded = load_session(client.session_data.session_id)
        assert loaded.access_mode == "full_access"
        async with client._turn_lock:
            with pytest.raises(ValueError, match="current turn"):
                await server._dispatch("permissions/set", {"mode": "read_only"}, client, None, None, loop)
        client._worker_active.set()
        with pytest.raises(ValueError, match="current turn"):
            await server._dispatch("permissions/set", {"mode": "read_only"}, client, None, None, loop)

    asyncio.run(run())


def test_restoring_plan_keeps_access_mode(tmp_path):
    manager = PermissionManager(str(tmp_path), mode="full_access")
    plan = manager.enter_plan_mode()
    session = create_new_session(str(tmp_path))
    session.permission_mode = "plan"
    session.access_mode = manager.access_mode.value
    session.plan_file_path = plan
    save_session(session)
    loaded = load_session(session.session_id)
    restored = PermissionManager(str(tmp_path), mode="auto")
    restored.set_access_mode(loaded.access_mode)
    restored.restore_plan_state(loaded.permission_mode, loaded.plan_file_path)
    assert restored.is_plan_mode
    assert restored.previous_mode == PermissionMode.FULL_ACCESS


def test_permissions_menu_opens_from_slash_selection_and_marks_current(tmp_path, monkeypatch):
    from pepsicode.cli.cli_commands import find_matching_slash_commands

    monkeypatch.setattr("pepsicode.tui.app.save_history_entries", lambda _history: None)
    manager = PermissionManager(str(tmp_path), mode="auto")
    args = TtyAppArgs(runtime=None, tools=ToolRegistry([]), model=None, messages=[], cwd=str(tmp_path), permissions=manager)
    state = ScreenState(session=create_new_session(str(tmp_path)), input="/perm")
    # Selecting the command opens the picker directly, without inserting args.
    _handle_normal_mode_return(args, state, find_matching_slash_commands("/perm"), lambda: None)
    assert state.input == ""
    picker = state.settings_picker
    assert picker.kind == "permissions"
    assert picker.current == "auto"
    assert picker.selected == 1
    assert [option.value for option in picker.options] == ["read_only", "auto", "full_access"]
    screen = "\n".join(render_settings_picker(picker, 90, 20))
    assert "自动  √ 当前" in screen
    assert "保存为新会话默认" not in screen
    # Moving the highlight does not change the checked mode until Enter.
    _handle_settings_picker_event(args, state, KeyEvent("down", False, False), lambda: None)
    assert picker.current == "auto"
    assert manager.access_mode == PermissionMode.AUTO
    _handle_settings_picker_event(args, state, KeyEvent("return", False, False), lambda: None)
    assert manager.access_mode == PermissionMode.FULL_ACCESS
    assert state.session.access_mode == "full_access"
    assert state.settings_picker is None
    _handle_input(args, state, lambda: None, "/permissions")
    assert state.settings_picker.current == "full_access"
    assert "完全访问  √ 当前" in "\n".join(render_settings_picker(state.settings_picker, 90, 20))
    _handle_settings_picker_event(args, state, KeyEvent("up", False, False), lambda: None)
    _handle_settings_picker_event(args, state, KeyEvent("escape", False, False), lambda: None)
    assert state.settings_picker is None
    assert manager.access_mode == PermissionMode.FULL_ACCESS
