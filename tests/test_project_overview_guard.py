from unittest.mock import Mock

from pepsicode.core.task_control import begin_task, snapshot
from pepsicode.execution.runtime import ExecutionScope, RequestBudget, bind_scope
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.read_file import read_file_tool
from pepsicode.tools.run_command import run_command_tool
from pepsicode.tools.task_state import task_state_tool
from pepsicode.tools.write_file import write_file_tool


def test_project_overview_does_not_become_change_when_model_tries_commands_or_writes(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("A project", encoding="utf-8")
    spawn = Mock(side_effect=AssertionError("informational task must not start a process"))
    monkeypatch.setattr("pepsicode.execution.process.subprocess.Popen", spawn)
    permissions = PermissionManager(str(tmp_path), mode="trust_commands")
    scope = ExecutionScope(str(tmp_path), permissions=permissions, budget=RequestBudget(CostTracker()))
    context = ToolContext(str(tmp_path), permissions=permissions, execution=scope)
    registry = ToolRegistry([read_file_tool, run_command_tool, write_file_tool])
    with bind_scope(scope):
        task = begin_task([{"role": "user", "content": "项目里有啥"}], {})
        assert task["kind"] == "answer" and task["classification"]["read_only_request"]
        assert registry.execute("read_file", {"path": "README.md"}, context).ok
        assert not registry.execute("run_command", {"command": "git", "args": ["status"]}, context).ok
        assert not registry.execute("write_file", {"path": "new.txt", "content": "no"}, context).ok
        assert snapshot()["task"]["kind"] == "answer"
        assert snapshot()["task"]["revision"] == task["revision"]
    assert not spawn.called
    assert not (tmp_path / "new.txt").exists()


def test_overview_with_explicit_modification_can_still_write(tmp_path):
    permissions = PermissionManager(str(tmp_path), mode="auto")
    scope = ExecutionScope(str(tmp_path), permissions=permissions, budget=RequestBudget(CostTracker()))
    context = ToolContext(str(tmp_path), permissions=permissions, execution=scope)
    with bind_scope(scope):
        task = begin_task([{"role": "user", "content": "帮我看看项目里有什么，然后添加 hello.txt"}], {})
        assert task["kind"] == "change"
        assert not task["classification"]["read_only_request"]
        assert ToolRegistry([write_file_tool]).execute("write_file", {"path": "hello.txt", "content": "ok"}, context).ok
        assert snapshot()["task"]["kind"] == "change"
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "ok"


def test_overview_with_explicit_test_execution_is_not_blocked_as_read_only(tmp_path):
    run = Mock(return_value=ToolResult(True, "passed"))
    tool = ToolDefinition("run_command", "test", {}, lambda data: data, run)
    scope = ExecutionScope(str(tmp_path), budget=RequestBudget(CostTracker()))
    context = ToolContext(str(tmp_path), execution=scope)
    with bind_scope(scope):
        task = begin_task([{"role": "user", "content": "项目里有什么并运行测试"}], {})
        assert not task["classification"]["read_only_request"]
        assert ToolRegistry([tool]).execute("run_command", {"command": "pytest"}, context).ok
    assert run.call_count == 1


def test_overview_rejects_model_defined_acceptance_but_mixed_request_can_define_it(tmp_path):
    scope = ExecutionScope(str(tmp_path), budget=RequestBudget(CostTracker()))
    context = ToolContext(str(tmp_path), execution=scope)
    registry = ToolRegistry([task_state_tool])
    criterion = {"id": "file", "kind": "file_exists", "path": "hello.txt"}
    with bind_scope(scope):
        overview = begin_task([{"role": "user", "content": "项目里有啥"}], {})
        request = {
            "action": "define",
            "task_id": overview["id"],
            "revision": overview["revision"],
            "criteria": [criterion],
        }
        result = registry.execute("task_state", request, context)
        assert not result.ok
        assert snapshot()["task"]["criteria"] == []
        assert snapshot()["task"]["revision"] == overview["revision"]

        change = begin_task([{"role": "user", "content": "帮我看看项目里有什么，然后添加 hello.txt"}], {})
        request.update(task_id=change["id"], revision=change["revision"])
        assert registry.execute("task_state", request, context).ok
        assert snapshot()["task"]["criteria"] == [criterion]
