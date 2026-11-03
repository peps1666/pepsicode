import os
import shutil
from unittest.mock import Mock

import pytest

from pepsicode.execution import process
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.run_command import run_command_tool


@pytest.fixture
def batch(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows batch execution")
    directory = tmp_path / "tools with spaces"
    directory.mkdir()
    script = directory / "demo.cmd"
    script.write_text("@echo off\necho [%~1] [%~2]\n", encoding="utf-8")
    return script


def test_batch_lookup_and_literal_arguments(batch, tmp_path):
    environment = {**os.environ, "PATH": str(batch.parent) + os.pathsep + os.environ["PATH"]}
    result = process.run(
        ["demo", "hello world", ""],
        cwd=str(tmp_path),
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "[hello world] []"


def test_batch_resolution_uses_child_cwd_and_environment(batch, tmp_path, monkeypatch):
    from pepsicode.execution.windows import resolve_argv

    monkeypatch.chdir(batch.parent)
    child = tmp_path / "child"
    child.mkdir()
    environment = {**os.environ, "PATH": "", "PATHEXT": ".CMD"}
    # demo.cmd exists only in the host cwd; it must not be selected.
    assert resolve_argv(["demo"], child, environment) == ["demo"]
    environment["PATH"] = str(batch.parent) + os.pathsep + os.environ["PATH"]
    assert str(batch).casefold() in resolve_argv(["demo"], child, environment)[-1].casefold()


@pytest.mark.parametrize("argument", ["a&echo injected", "%PATH%", 'a"b', "hello\nworld", "!PATH!", "a|b"])
def test_batch_rejects_implicit_shell_code(batch, tmp_path, argument, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(process.subprocess, "Popen", spawn)
    with pytest.raises(ValueError, match="shell control"):
        process.run([str(batch), argument], cwd=str(tmp_path))
    spawn.assert_not_called()


def test_batch_approval_displays_resolved_command_and_denial_never_spawns(batch, tmp_path, monkeypatch):
    requests = []

    def deny(request):
        requests.append(request)
        return {"decision": "deny_once"}

    spawn = Mock()
    monkeypatch.setattr(process.subprocess, "Popen", spawn)
    permissions = PermissionManager(str(tmp_path), prompt=deny)
    with pytest.raises(PermissionError):
        process.run([str(batch), "hello"], context=ToolContext(str(tmp_path), permissions=permissions))
    assert str(batch) in "\n".join(requests[0]["details"])
    assert "cmd.exe" in str(requests)
    spawn.assert_not_called()


def test_npx_version_through_real_run_command(tmp_path):
    if os.name != "nt" or not shutil.which("npx.cmd"):
        pytest.skip("Requires Windows and Node.js/npm")
    result = run_command_tool.run({"command": "npx --version"}, ToolContext(str(tmp_path)))
    assert result.ok, result.output
    assert result.output.strip()[0].isdigit()


def test_missing_npx_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "run", Mock(side_effect=FileNotFoundError("missing")))
    result = ToolRegistry([run_command_tool]).execute(
        "run_command", {"command": "npx", "args": ["--version"]}, ToolContext(str(tmp_path))
    )
    assert not result.ok
    assert "Node.js" in result.output
    assert "PATH" in result.output
    assert "ToolDefinition error" not in result.output
