from __future__ import annotations

import ast
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from pepsicode.core.agent_loop import run_agent_turn
from pepsicode.execution import process as processes
from pepsicode.execution.filesystem import FILES, StaleFileError
from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope, RequestBudget, bind_scope
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.permissions import PermissionManager
from pepsicode.sandbox import ConfinedArgs, SandboxBackend
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.docker_helper import docker_helper_tool
from pepsicode.tools.edit_file import edit_file_tool
from pepsicode.tools.multi_edit import multi_edit_tool
from pepsicode.tools.notebook_edit import notebook_edit_tool
from pepsicode.tools.read_file import read_file_tool
from pepsicode.tools.run_command import run_command_tool
from pepsicode.tools.run_with_debug import run_with_debug_tool
from pepsicode.tools.write_file import write_file_tool


def denying(root):
    return PermissionManager(str(root), mode="auto", prompt=lambda _request: {"decision": "deny_once"})


@pytest.mark.parametrize(
    "tool,arguments",
    [
        (write_file_tool, {"path": "../outside.txt", "content": "changed"}),
        (edit_file_tool, {"path": "../outside.txt", "search": "original", "replace": "changed"}),
        (multi_edit_tool, {"changes": [{"file": "../outside.txt", "old": "original", "new": "changed"}]}),
        (notebook_edit_tool, {"path": "../outside.txt", "cells": [{"source": "changed"}]}),
    ],
    ids=["write", "edit", "batch", "notebook"],
)
def test_every_file_writer_refuses_external_path_even_on_direct_tool_call(tmp_path, tool, arguments):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    context = ToolContext(str(workspace), permissions=denying(workspace))
    try:
        result = tool.run(tool.validator(arguments), context)
        assert not result.ok
    except PermissionError:
        pass
    assert outside.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(
    "tool,arguments",
    [
        (docker_helper_tool, {"action": "exec", "container": "test", "command": "echo no"}),
        (run_with_debug_tool, {"command": "echo no"}),
        (run_command_tool, {"command": sys.executable, "args": ["-c", "print('no')"]}),
    ],
    ids=["docker", "debug", "command"],
)
def test_process_denial_reaches_no_os_spawn(tmp_path, monkeypatch, tool, arguments):
    spawn = Mock(side_effect=AssertionError("An unapproved process reached the OS"))
    monkeypatch.setattr(processes.subprocess, "Popen", spawn)
    result = ToolRegistry([tool]).execute(
        tool.name, arguments, ToolContext(str(tmp_path), permissions=denying(tmp_path))
    )
    assert not result.ok
    assert not spawn.called


@pytest.mark.parametrize("mode", ["read_only", "plan"])
def test_low_level_services_refuse_mutation_without_registry_guards(tmp_path, mode):
    permissions = PermissionManager(str(tmp_path), mode="auto")
    if mode == "plan":
        permissions.enter_plan_mode()
    else:
        permissions.set_access_mode(mode)
    context = ToolContext(str(tmp_path), permissions=permissions)
    with pytest.raises(PermissionError):
        FILES.write_text(context, tmp_path / "forbidden.txt", "no")
    with pytest.raises(PermissionError):
        processes.run([sys.executable, "-c", "print('no')"], context=context, timeout=3)
    assert not (tmp_path / "forbidden.txt").exists()


def test_stale_read_cannot_overwrite_another_writer(tmp_path):
    path = tmp_path / "shared.py"
    path.write_text("first", encoding="utf-8")
    permissions = PermissionManager(str(tmp_path), mode="auto")
    context = ToolContext(str(tmp_path), permissions=permissions)
    read_file_tool.run({"path": "shared.py", "offset": 0, "limit": 100}, context)
    path.write_text("human edit", encoding="utf-8")
    with pytest.raises(StaleFileError):
        FILES.write_text(context, path, "stale overwrite")
    assert path.read_text(encoding="utf-8") == "human edit"


def test_internal_editor_reads_cannot_erase_an_older_session_read_version(tmp_path):
    path = tmp_path / "shared.py"
    path.write_text("name = 'old'\n", encoding="utf-8")
    permissions = PermissionManager(str(tmp_path), mode="auto")
    first_turn = ExecutionScope(str(tmp_path), permissions=permissions, budget=RequestBudget(CostTracker()))
    registry = ToolRegistry([read_file_tool, edit_file_tool])
    registry.execute(
        "read_file", {"path": "shared.py"}, ToolContext(str(tmp_path), permissions=permissions, execution=first_turn)
    )
    path.write_text("name = 'old'\n# concurrent change\n", encoding="utf-8")
    next_turn = ExecutionScope(str(tmp_path), permissions=permissions, budget=RequestBudget(CostTracker()))
    context = ToolContext(str(tmp_path), permissions=permissions, execution=next_turn)
    result = registry.execute("edit_file", {"path": "shared.py", "old": "old", "new": "new"}, context)
    assert result.status == "blocked" and not result.ok
    assert not next_turn.has_unknown
    assert "name = 'old'" in path.read_text(encoding="utf-8")
    assert registry.execute("read_file", {"path": "shared.py"}, context).ok
    assert registry.execute("edit_file", {"path": "shared.py", "old": "old", "new": "new"}, context).ok
    assert path.read_text(encoding="utf-8") == "name = 'new'\n# concurrent change\n"


def test_unreviewed_executable_override_never_reaches_spawn(tmp_path, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(processes.subprocess, "Popen", spawn)
    with pytest.raises(ValueError, match="bypass reviewed argv"):
        processes.Popen(["echo", "safe"], cwd=str(tmp_path), executable="unreviewed.exe")
    assert not spawn.called


def test_edit_version_is_rechecked_after_approval(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("before", encoding="utf-8")

    def approve(_request):
        path.write_text("concurrent", encoding="utf-8")
        return {"decision": "allow_once"}

    context = ToolContext(str(tmp_path), permissions=PermissionManager(str(tmp_path), prompt=approve))
    result = ToolRegistry([edit_file_tool]).execute(
        "edit_file", {"path": "source.py", "old": "before", "new": "after"}, context
    )
    assert not result.ok
    assert path.read_text(encoding="utf-8") == "concurrent"


def test_concurrent_read_modify_write_has_one_winner(tmp_path):
    path = tmp_path / "shared.py"
    path.write_text("before", encoding="utf-8")
    barrier = threading.Barrier(2)

    def writer(content):
        scope = ExecutionScope(str(tmp_path), budget=RequestBudget(CostTracker()))
        context = ToolContext(str(tmp_path), execution=scope)
        with bind_scope(scope):
            snapshot = FILES.snapshot(context, path)
            barrier.wait(timeout=3)
            try:
                FILES.write_text(context, path, content, expected=snapshot.version)
                return "written"
            except StaleFileError:
                return "stale"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(writer, ["writer a", "writer b"]))
    assert sorted(results) == ["stale", "written"]
    assert path.read_text(encoding="utf-8") in {"writer a", "writer b"}


def test_requested_sandbox_uses_confined_argv_and_fails_closed_when_unavailable(tmp_path, monkeypatch):
    class Backend(SandboxBackend):
        def confine(self, argv, policy):
            assert argv == ["original"]
            assert policy.mode.value == "workspace-write"
            return ConfinedArgs([sys.executable, "-c", "print('confined')"], enforcement="full")

    permissions = PermissionManager(
        str(tmp_path), mode="full_access", sandbox=Backend(), process_sandbox_mode="workspace-write"
    )
    result = processes.run(
        ["original"],
        context=ToolContext(str(tmp_path), permissions=permissions),
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.stdout.strip() == "confined"
    permissions = PermissionManager(str(tmp_path), mode="full_access", process_sandbox_mode="workspace-write")
    spawn = Mock()
    monkeypatch.setattr(processes.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="no OS isolation"):
        processes.run(["original"], context=ToolContext(str(tmp_path), permissions=permissions), timeout=3)
    assert not spawn.called


def test_allow_once_never_authorizes_another_command_or_cwd(tmp_path):
    requests = []
    manager = PermissionManager(
        str(tmp_path), prompt=lambda request: requests.append(request) or {"decision": "allow_once"}
    )
    for _ in range(2):
        assert manager.check_command("python", ["-c", "print(1)"], str(tmp_path)).is_allowed
    assert len(requests) == 2
    requests.clear()
    manager.prompt = lambda request: requests.append(request) or {"decision": "allow_always"}
    manager.check_command("python", ["-c", "one two"], str(tmp_path))
    manager.check_command("python", ["-c", "one", "two"], str(tmp_path))
    assert len(requests) == 2


def test_unknown_outcome_is_durable_and_never_replayed(tmp_path):
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    scope = ExecutionScope(str(tmp_path), journal=journal)

    def partial(_arguments, _context):
        raise OSError("lost response after side effect")

    tool = ToolDefinition("partial", "partial external action", {}, lambda value: value, partial)
    result = ToolRegistry([tool]).execute("partial", {}, ToolContext(str(tmp_path), execution=scope))
    assert result.status == "unknown"
    pending = ExecutionJournal.interrupted_calls(journal.path)
    assert len(pending) == 1 and pending[0]["state"] == "unknown"
    assert scope.state == "unknown"
    with pytest.raises(PermissionError):
        FILES.write_text(ToolContext(str(tmp_path), execution=scope), tmp_path / "next.txt", "no")


def test_journal_failure_prevents_dispatch(tmp_path, monkeypatch):
    scope = ExecutionScope(str(tmp_path), journal=ExecutionJournal(tmp_path / "journal.jsonl"))
    monkeypatch.setattr(scope.journal, "append", Mock(side_effect=OSError("disk full")))
    effect = Mock(return_value=ToolResult(True, "changed"))
    tool = ToolDefinition("effect", "effect", {}, lambda value: value, effect)
    result = ToolRegistry([tool]).execute("effect", {}, ToolContext(str(tmp_path), execution=scope))
    assert not result.ok
    assert not effect.called


def test_model_log_reconstructs_hook_context_and_schemas_without_auth_headers(tmp_path, monkeypatch):
    from pepsicode import config
    from pepsicode.execution.runtime import execution_journal_dir

    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path / "pepsi-home")

    class Response:
        status = 200

        def read(self):
            return json.dumps(
                {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "<final>done</final>"}],
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                }
            ).encode()

        def close(self):
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    registry = ToolRegistry([read_file_tool])
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "HEADER_SECRET"}, registry
    )
    hook = Mock()
    hook.drain_context_messages.return_value = ["HOOK_CONTEXT_SENTINEL"]
    tracker = CostTracker()
    result = run_agent_turn(
        model=adapter,
        tools=registry,
        messages=[{"role": "user", "content": "hi"}],
        cwd=str(tmp_path),
        cost_tracker=tracker,
        hook_engine=hook,
    )
    assert result[-1]["content"] == "done"
    journal_path = next(execution_journal_dir(str(tmp_path)).glob("*.jsonl"))
    records = ExecutionJournal.read(journal_path)
    request = next(record for record in records if record["event"] == "model/prepared")
    assert "HOOK_CONTEXT_SENTINEL" in request["body"]["system"]
    assert request["body"]["tools"][0]["name"] == "read_file"
    assert "HEADER_SECRET" not in journal_path.read_text(encoding="utf-8")
    assert tracker.get_total_calls() == 1


def test_summary_requests_share_the_same_budget_and_request_limit(tmp_path, monkeypatch):
    class Response:
        status = 200

        def read(self):
            return b'{"stop_reason":"end_turn","content":[{"type":"text","text":"checkpoint"}],"usage":{"input_tokens":100,"output_tokens":10}}'

        def close(self):
            pass

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    tracker = CostTracker()
    scope = ExecutionScope(str(tmp_path), budget=RequestBudget(tracker, max_requests=1))
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "x"}, ToolRegistry([])
    )
    with bind_scope(scope):
        assert adapter.summarize("history") == "checkpoint"
        with pytest.raises(RuntimeError, match="request limit"):
            adapter.summarize("next")
    assert tracker.get_total_calls() == 1


def test_tools_cannot_reintroduce_raw_effect_entrypoints():
    root = Path(__file__).resolve().parents[1] / "pepsicode" / "tools"
    violations = []
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            rendered = ast.unparse(node.func)
            if rendered in {"subprocess.run", "subprocess.Popen", "os.system", "os.popen", "urllib.request.urlopen"}:
                violations.append(f"{path.name}:{node.lineno} {rendered}")
            if (
                node.func.attr in {"write_text", "write_bytes", "unlink", "mkdir", "replace", "rename"}
                and not rendered.startswith("FILES.")
                and node.func.attr not in {"replace"}
            ):
                violations.append(f"{path.name}:{node.lineno} {rendered}")
    assert not violations, violations
