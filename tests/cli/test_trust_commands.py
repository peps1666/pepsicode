import json

import pytest

from pepsicode.approval import ApprovalDecision, ApprovalKind, ApprovalOutcome
from pepsicode.core.session import create_new_session, load_session, save_session
from pepsicode.execution.process import checked_argv
from pepsicode.permissions import PermissionManager, PermissionMode
from pepsicode.tooling import ToolContext


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")


def test_trusted_commands_skip_process_prompt_but_not_other_approvals(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prompts = []
    manager = PermissionManager(
        str(workspace), mode="trust_commands", prompt=lambda req: prompts.append(req) or {"decision": "allow_once"}
    )
    context = ToolContext(str(workspace), permissions=manager)
    checked_argv(["git", "status", "--short"], context=context)
    assert not prompts
    assert manager.check_edit(str(workspace / "a.txt"), "+ test").is_allowed
    assert not prompts
    assert manager.check_edit(str(tmp_path / "external.txt"), "+ test").is_allowed
    assert manager.check_mcp_tool("mcp__server__write", {}).is_allowed
    assert [req["kind"] for req in prompts] == ["path", "mcp"]
    checked_argv(["git", "status"], context=context, cwd=str(tmp_path))
    assert prompts[-1]["kind"] == "path"


def test_trusted_commands_respects_exact_denial_and_read_only_boundaries(tmp_path):
    manager = PermissionManager(str(tmp_path), mode="trust_commands")
    key = json.dumps([str(tmp_path.resolve()), "git", ["status"]], ensure_ascii=False)
    manager.store.record(ApprovalKind.COMMAND, key, ApprovalOutcome(ApprovalDecision.DENY_ONCE))
    assert manager.check_command("git", ["status"], str(tmp_path)).is_denied
    assert manager.check_command("git", ["status", "--short"], str(tmp_path)).is_allowed
    manager.enter_plan_mode()
    assert manager.check_command("git", ["status", "--short"], str(tmp_path)).is_denied
    assert manager.access_mode == PermissionMode.TRUST_COMMANDS
    manager.set_access_mode("read_only")
    assert manager.check_command("git", ["status"], str(tmp_path)).is_denied


def test_rejected_command_stays_denied_when_switching_to_trusted_mode(tmp_path):
    prompts = []
    manager = PermissionManager(
        str(tmp_path), mode="auto", prompt=lambda req: prompts.append(req) or {"decision": "deny_once"}
    )
    context = ToolContext(str(tmp_path), permissions=manager)
    with pytest.raises(PermissionError):
        checked_argv(["git", "status"], context=context)
    manager.set_access_mode("trust_commands")
    with pytest.raises(PermissionError):
        checked_argv(["git", "status"], context=context)
    assert len(prompts) == 1


def test_trusted_mode_survives_session_and_plan_restore(tmp_path):
    manager = PermissionManager(str(tmp_path), mode="trust_commands")
    plan = manager.enter_plan_mode()
    session = create_new_session(str(tmp_path))
    session.access_mode = manager.access_mode.value
    session.permission_mode = manager.mode.value
    session.plan_file_path = plan
    save_session(session)
    saved = load_session(session.session_id)
    restored = PermissionManager(str(tmp_path), mode="auto")
    restored.set_access_mode(saved.access_mode)
    restored.restore_plan_state(saved.permission_mode, saved.plan_file_path)
    assert restored.is_plan_mode
    assert restored.access_mode == PermissionMode.TRUST_COMMANDS
    assert restored.check_command("git", ["status"], str(tmp_path)).is_denied
