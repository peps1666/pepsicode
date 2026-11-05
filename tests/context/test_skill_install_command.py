import asyncio
from types import SimpleNamespace

import pytest

from pepsicode.cli import local_tool_shortcuts


def test_skill_install_shortcut_uses_portable_argv():
    result = local_tool_shortcuts.parse_local_tool_shortcut("/skills install find-skills --global")
    assert result["toolName"] == "run_command"
    assert result["input"]["command"] == "npx"
    assert result["input"]["args"] == [
        "--yes",
        "skills",
        "add",
        "vercel-labs/skills",
        "-a",
        "claude-code",
        "-y",
        "--copy",
        "--skill",
        "find-skills",
        "--global",
    ]


@pytest.mark.parametrize("mode,expected", [("plan", "切换到 Build"), ("read_only", "/permissions")])
def test_install_blocked_before_desktop_tool_dispatch(tmp_path, mode, expected):
    from unittest.mock import Mock

    from pepsicode.permissions import PermissionManager
    from pepsicode.server.app import PepsiCodeServer

    permissions = PermissionManager(str(tmp_path))
    if mode == "plan":
        permissions.enter_plan_mode()
    else:
        permissions.set_access_mode(mode)
    tools = Mock()
    session = SimpleNamespace(permissions=permissions, tools=tools)
    coroutine = PepsiCodeServer._handle_local_command(None, "/skills install find-skills --global", session, None)
    with pytest.raises(RuntimeError, match=expected):
        coroutine.send(None)
    tools.execute.assert_not_called()
    assert permissions.is_plan_mode == (mode == "plan")
    permissions.ensure_local_command_allowed("/skills")


def test_skill_install_project_and_reject_shell_input():
    result = local_tool_shortcuts.parse_local_tool_shortcut("/skills install owner/repo example")
    assert result["input"]["command"] == "npx"
    assert "--global" not in result["input"]["args"]
    assert local_tool_shortcuts.parse_local_tool_shortcut("/skills install owner/repo bad;command") is None
    assert local_tool_shortcuts.parse_local_tool_shortcut("/skills install owner/repo") is None


def test_desktop_install_uses_tool_registry(monkeypatch):
    from pepsicode.server.app import PepsiCodeServer

    calls = []

    async def run_inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)

    def execute(name, data, context):
        calls.append((name, data, context))
        return SimpleNamespace(ok=True, output="installed")

    session = SimpleNamespace(
        permissions=None,
        tools=SimpleNamespace(execute=execute, refresh_skills=lambda cwd: calls.append(("refresh", cwd))),
        cwd=".",
        hook_engine=None,
        memory_manager=None,
        _cancellation_event=None,
    )
    coroutine = PepsiCodeServer._handle_local_command(None, "/skills install find-skills", session, None)
    with pytest.raises(StopIteration) as completed:
        coroutine.send(None)
    result = completed.value.value
    assert calls[0][0] == "run_command"
    assert "vercel-labs/skills" in calls[0][1]["args"]
    assert calls[-1] == ("refresh", ".")
    assert "Skills refreshed" in result["output"]


def test_install_github_url_matches_shorthand():
    assert local_tool_shortcuts.skill_install_shortcut(
        "/skills install https://github.com/vercel-labs/skills.git find-skills"
    ) == local_tool_shortcuts.skill_install_shortcut("/skills install find-skills")


@pytest.mark.parametrize(
    "text",
    [
        "/skills install",
        "/skills install owner/repo",
        "/skills install -bad/repo name",
        "/skills install owner/repo --all",
        "/skills install owner/repo name;echo",
        "/skills install https://example.com/a/b name",
        "/skills install find-skills --bad",
    ],
)
def test_invalid_install_shows_usage(text):
    from pepsicode.cli.cli_commands import try_handle_local_command

    assert "Usage: /skills install" in try_handle_local_command(text)


def test_skills_command_refreshes_without_restart(tmp_path, monkeypatch):
    from pepsicode.cli.cli_commands import try_handle_local_command
    from pepsicode.context import skills
    from pepsicode.tooling import ToolRegistry

    monkeypatch.setattr(skills, "_home_dir", lambda: tmp_path / "home")
    registry = ToolRegistry([])
    skill = tmp_path / ".claude/skills/new-skill/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# New\n\nReady to load", encoding="utf-8")
    assert "new-skill" in try_handle_local_command("/skills", tools=registry, cwd=str(tmp_path))
    assert registry.get_skills()[0]["name"] == "new-skill"
