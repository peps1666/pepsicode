import pytest

from pepsicode.cli.cli_commands import try_handle_local_command
from pepsicode.core.session import create_new_session
from pepsicode.permissions import PermissionManager
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.workspace_permissions import get_workspace_default, set_workspace_default


@pytest.fixture(autouse=True)
def isolated_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.workspace_permissions.WORKSPACE_PERMISSIONS_PATH", tmp_path / "defaults.json")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")


def test_workspace_default_affects_only_new_sessions(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    old = create_new_session(str(workspace))
    manager = PermissionManager(str(workspace), mode="auto")
    result = try_handle_local_command("/permissions default full_access", permissions=manager)
    assert "当前会话不变" in result
    assert manager.access_mode.value == old.access_mode == "auto"
    assert create_new_session(str(workspace / ".")).access_mode == "full_access"
    assert create_new_session(str(other)).access_mode == "auto"
    set_workspace_default(str(workspace), "ask")
    assert get_workspace_default(str(workspace)) == "ask"
    set_workspace_default(str(workspace), "auto")
    assert get_workspace_default(str(workspace)) == "auto"


def test_legacy_workspace_defaults_migrate_safely(tmp_path):
    import json
    import os

    path = tmp_path / "defaults.json"
    key = os.path.normcase(str(tmp_path.resolve()))
    path.write_text(json.dumps({"workspaceDefaults": {key: "trust_commands"}}), encoding="utf-8")
    assert get_workspace_default(str(tmp_path)) == "auto"
    path.write_text(json.dumps({"workspaceDefaults": {key: "read_only"}}), encoding="utf-8")
    assert get_workspace_default(str(tmp_path)) == "ask"


def test_invalid_and_corrupt_defaults_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "defaults.json"
    with pytest.raises(ValueError):
        set_workspace_default(str(tmp_path), "plan")
    with pytest.raises(ValueError):
        set_workspace_default(str(tmp_path), "trust_commands")
    path.write_text("{broken", encoding="utf-8")
    assert get_workspace_default(str(tmp_path)) == "auto"
    with pytest.raises(ValueError):
        set_workspace_default(str(tmp_path), "full_access")


def test_server_workspace_default_does_not_change_current_session(tmp_path):
    client = ClientSession(str(tmp_path))
    client.permissions = PermissionManager(str(tmp_path), mode="auto")

    server = PepsiCodeServer()
    dispatch = server._dispatch("permissions/set_workspace_default", {"mode": "ask"}, client, None, None, None)
    with pytest.raises(StopIteration) as completed:
        dispatch.send(None)
    assert completed.value.value == {"workspace_default": "ask"}
    assert client.permissions.access_mode.value == "auto"
    assert create_new_session(str(tmp_path)).access_mode == "ask"
