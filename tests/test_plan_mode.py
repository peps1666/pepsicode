from __future__ import annotations

from pathlib import Path

import pytest

from pepsicode.context.prompt import build_system_prompt
from pepsicode.core.agent_loop import _execute_calls_in_order
from pepsicode.permissions import PermissionManager, PermissionMode
from pepsicode.tooling import ToolCapability, ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools import create_default_tool_registry


def _tool(name: str, *, read_only: bool = False, ran: list[str] | None = None) -> ToolDefinition:
    capabilities = {ToolCapability.READ_ONLY} if read_only else set()

    def run(_input, _context):
        if ran is not None:
            ran.append(name)
        return ToolResult(ok=True, output=name)

    return ToolDefinition(
        name=name,
        description="test tool",
        input_schema={"type": "object"},
        validator=lambda value: value or {},
        run=run,
        capabilities=capabilities,
    )


def test_plan_mode_allows_read_only_and_denies_unknown_write(tmp_path: Path) -> None:
    permissions = PermissionManager(str(tmp_path))
    permissions.enter_plan_mode()
    registry = ToolRegistry([_tool("read_x", read_only=True), _tool("write_x")])
    context = ToolContext(cwd=str(tmp_path), permissions=permissions)

    assert registry.execute("read_x", {}, context).ok is True
    denied = registry.execute("write_x", {}, context)
    assert denied.ok is False
    assert "Plan mode denied" in denied.output


@pytest.mark.parametrize("agent_type", ["general", "verification", "custom"])
def test_plan_mode_only_allows_read_only_subagent_types(tmp_path: Path, agent_type: str) -> None:
    permissions = PermissionManager(str(tmp_path))
    permissions.enter_plan_mode()
    registry = ToolRegistry([_tool("task")])
    context = ToolContext(cwd=str(tmp_path), permissions=permissions)

    result = registry.execute("task", {"agent_type": agent_type}, context)
    assert result.ok is False
    assert "explore or plan" in result.output


@pytest.mark.parametrize("agent_type", ["explore", "plan"])
def test_plan_mode_allows_explore_and_plan_subagents(tmp_path: Path, agent_type: str) -> None:
    permissions = PermissionManager(str(tmp_path))
    permissions.enter_plan_mode()
    registry = ToolRegistry([_tool("task")])

    result = registry.execute(
        "task",
        {"agent_type": agent_type},
        ToolContext(cwd=str(tmp_path), permissions=permissions),
    )
    assert result.ok is True


def test_only_exact_plan_file_can_be_written_without_edit_prompt(tmp_path: Path) -> None:
    permissions = PermissionManager(str(tmp_path))
    plan_path = permissions.enter_plan_mode()
    registry = create_default_tool_registry(str(tmp_path))
    context = ToolContext(cwd=str(tmp_path), permissions=permissions)

    written = registry.execute("write_file", {"path": plan_path, "content": "# Plan\n"}, context)
    assert written.ok is True
    assert Path(plan_path).read_text(encoding="utf-8") == "# Plan\n"

    same_name_elsewhere = tmp_path / "elsewhere" / Path(plan_path).name
    denied = registry.execute(
        "write_file",
        {"path": str(same_name_elsewhere), "content": "bad"},
        context,
    )
    assert denied.ok is False
    assert not same_name_elsewhere.exists()


def test_plan_approval_exits_mode_and_queues_execution(tmp_path: Path) -> None:
    permissions = PermissionManager(str(tmp_path), prompt=lambda _request: {"decision": "allow_once"})
    plan_path = Path(permissions.enter_plan_mode())
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("# Approved plan", encoding="utf-8")
    registry = create_default_tool_registry(str(tmp_path))

    result = registry.execute(
        "exit_plan_mode",
        {},
        ToolContext(cwd=str(tmp_path), permissions=permissions),
    )

    assert result.ok is True
    assert result.awaitUser is True
    assert permissions.mode == PermissionMode.DEFAULT
    assert "# Approved plan" in (permissions.consume_plan_followup() or "")
    assert permissions.consume_plan_followup() is None


def test_plan_feedback_keeps_mode_and_queues_feedback(tmp_path: Path) -> None:
    permissions = PermissionManager(
        str(tmp_path),
        prompt=lambda _request: {"decision": "deny_with_feedback", "feedback": "Add rollback details"},
    )
    plan_path = Path(permissions.enter_plan_mode())
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("# Draft", encoding="utf-8")
    registry = create_default_tool_registry(str(tmp_path))

    result = registry.execute(
        "exit_plan_mode",
        {},
        ToolContext(cwd=str(tmp_path), permissions=permissions),
    )

    assert result.awaitUser is True
    assert permissions.mode == PermissionMode.PLAN
    followup = permissions.consume_plan_followup() or ""
    assert followup.startswith("Revise the plan")
    assert "Add rollback details" in followup


def test_exit_plan_mode_requires_nonempty_plan(tmp_path: Path) -> None:
    permissions = PermissionManager(str(tmp_path), prompt=lambda _request: {"decision": "allow_once"})
    permissions.enter_plan_mode()
    registry = create_default_tool_registry(str(tmp_path))

    result = registry.execute(
        "exit_plan_mode",
        {},
        ToolContext(cwd=str(tmp_path), permissions=permissions),
    )
    assert result.ok is False
    assert "does not exist" in result.output
    assert permissions.mode == PermissionMode.PLAN


def test_exit_plan_mode_must_be_the_only_tool_call(tmp_path: Path) -> None:
    ran: list[str] = []
    registry = ToolRegistry([_tool("exit_plan_mode", ran=ran), _tool("write_x", ran=ran)])
    calls = [
        {"toolName": "exit_plan_mode", "input": {}},
        {"toolName": "write_x", "input": {}},
    ]

    results = _execute_calls_in_order(
        calls,
        registry,
        ToolContext(cwd=str(tmp_path), permissions=None),
        None,
        None,
    )
    assert ran == []
    assert all(result.ok is False for result in results)
    assert all("only tool call" in result.output for result in results)


@pytest.mark.parametrize(
    "command",
    [
        "/model new-model",
        "/transcript-save plan.txt",
        "/resume latest",
        "/worktree create feature",
        "/worktree enter feature",
        "/worktree exit feature",
        "/worktree cleanup",
        "/hooks trust",
        "/hooks  trust",
        "/hooks\ttrust",
    ],
)
def test_plan_mode_blocks_mutating_local_command_bypasses(tmp_path: Path, command: str) -> None:
    permissions = PermissionManager(str(tmp_path))
    permissions.enter_plan_mode()
    with pytest.raises(RuntimeError, match="Plan mode denied"):
        permissions.ensure_local_command_allowed(command)


def test_plan_prompt_contains_hard_boundary_and_plan_path(tmp_path: Path) -> None:
    permissions = PermissionManager(str(tmp_path))
    plan_path = permissions.enter_plan_mode()
    prompt = build_system_prompt(
        str(tmp_path),
        permissions.get_summary(),
        {"planMode": True, "planFilePath": plan_path},
    )
    assert "PLAN MODE IS ACTIVE" in prompt
    assert plan_path in prompt
    assert "exit_plan_mode" in prompt


def test_restore_plan_state_rejects_path_outside_workspace(tmp_path: Path) -> None:
    permissions = PermissionManager(str(tmp_path))
    permissions.restore_plan_state("plan", str(tmp_path.parent / "outside.md"))
    assert permissions.mode == PermissionMode.DEFAULT

    valid = tmp_path / ".pepsi-code" / "plans" / "saved.md"
    permissions.restore_plan_state("plan", str(valid))
    assert permissions.mode == PermissionMode.PLAN
    assert permissions.plan_file_path == str(valid.resolve())
