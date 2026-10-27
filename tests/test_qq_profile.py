"""QQ presentation must survive persistence without changing normal sessions."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from pepsicode.core.session import create_new_session, load_session, save_session
from pepsicode.core.session_tree import copy_session
from pepsicode.permissions import PermissionManager
from pepsicode.server import app
from pepsicode.server import profiles as profiles_module
from pepsicode.server.profiles import profile_prompt
from pepsicode.tooling import ToolRegistry


@pytest.fixture
def profile_client(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    monkeypatch.setattr(app, "build_session_prompt", lambda *args, **kwargs: "Base project prompt")

    def initialize(client, *, force=False):
        client.tools = ToolRegistry([])
        client.permissions = PermissionManager(client.cwd, mode="auto")
        client.session_data = create_new_session(client.cwd)
        client.model = object()
        client.input_queue.bind_session(client.session_data)

    monkeypatch.setattr(app.ClientSession, "initialize", initialize)
    client = app.ClientSession(str(tmp_path))
    client.model_settings = Mock(return_value={})
    return client


def dispatch(client, method, params):
    async def run():
        return await app.PepsiCodeServer()._dispatch(method, params, client, None, None, asyncio.get_running_loop())

    return asyncio.run(run())


def test_qq_profile_is_saved_resumed_and_kept_when_prompt_rebuilds(profile_client):
    client = profile_client
    result = dispatch(client, "session/create", {"cwd": client.cwd, "profile": "qq_private"})
    assert result["profile"] == "qq_private"
    assert "You are Pepsi" in client.messages[0]["content"]
    loaded = load_session(result["session_id"])
    assert loaded.profile == "qq_private"
    client._rebuild_system_prompt()
    assert client.messages[0]["content"].count("You are Pepsi") == 1
    restored = dispatch(client, "session/resume", {"session_id": loaded.session_id, "workspace": client.cwd})
    assert restored["profile"] == "qq_private"
    assert "You are Pepsi" in client.messages[0]["content"]
    assert client.permissions.access_mode.value == "auto"


def test_normal_sessions_keep_the_original_prompt_and_reject_unknown_profiles_before_mutation(profile_client):
    client = profile_client
    result = dispatch(client, "session/create", {"cwd": client.cwd})
    assert result["profile"] == "default"
    assert client.messages[0]["content"] == "Base project prompt"
    before = client.session_data.session_id
    with pytest.raises(ValueError, match="Unknown session profile"):
        dispatch(client, "session/create", {"profile": "arbitrary privileged prompt"})
    assert client.session_data.session_id == before


def test_clones_and_forks_retain_qq_profile_without_changing_permissions(profile_client):
    client = profile_client
    dispatch(client, "session/create", {"cwd": client.cwd, "profile": "qq_private"})
    client.session_data.messages = [*client.messages, {"role": "user", "content": "hello"}]
    save_session(client.session_data)
    child, _ = copy_session(client.session_data, clone=True)
    assert child.profile == "qq_private"
    assert child.access_mode == client.session_data.access_mode
    assert child.session_id != client.session_data.session_id


def test_qq_profile_is_only_presentation_not_a_permission_grant():
    assert "permission system" in profile_prompt("qq_private")
    assert profile_prompt("default") == ""
    for invalid in [None, {}, [], "full_access"]:
        with pytest.raises(ValueError):
            profile_prompt(invalid)


def test_qq_profile_supplies_fresh_time_for_simple_date_questions(monkeypatch):
    clock = Mock()
    clock.now.side_effect = [
        datetime(2026, 10, 27, 8, 0, tzinfo=UTC),
        datetime(2026, 10, 28, 8, 0, tzinfo=UTC),
    ]
    monkeypatch.setattr(profiles_module, "datetime", clock)
    first, second = profile_prompt("qq_private"), profile_prompt("qq_private")
    assert "2026-10-27" in first and "Tuesday" in first
    assert "2026-10-28" in second and "Wednesday" in second
    assert "Do not run a shell command solely" in second
    assert profile_prompt("default") == ""
