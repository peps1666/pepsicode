import asyncio
import sys
from unittest.mock import Mock

import pytest

from pepsicode.core.session import AutosaveManager, create_new_session, load_session, save_session
from pepsicode.core.verification import restore_verification_state, run_verified_turn, snapshot_verification_state
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.permissions import PermissionManager
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolCapability, ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.write_file import write_file_tool
from pepsicode.tui import app


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_tui_saved_pending_draft_is_verified_after_restart_and_confirmation(workspace):
    marker = workspace / "verified"
    runtime = {
        "verificationCommands": [
            {"command": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}
        ]
    }
    tools = ToolRegistry([write_file_tool])
    tools.execute("write_file", {"path": "draft.py", "content": "value = 1\n"}, ToolContext(str(workspace)))
    session = create_new_session(str(workspace))
    args = app.TtyAppArgs(
        runtime=runtime,
        model=MockModelAdapter(),
        tools=tools,
        cwd=str(workspace),
        permissions=PermissionManager(str(workspace), mode="auto"),
        messages=[{"role": "assistant", "content": "Which workflow?", "awaitingUser": True}],
    )
    state = app.ScreenState(session=session, autosave=AutosaveManager(session))
    app._snapshot_session(args, state)
    save_session(session)
    restored = load_session(session.session_id)
    assert restored.verification_state["pending"]
    args.tools = ToolRegistry([write_file_tool])
    app._restore_session_into_state(args, state, restored)
    assert args.tools.mutation_count == 1
    run_verified_turn(
        runtime=runtime,
        runner=lambda **opts: [*opts["messages"], {"role": "assistant", "content": "Confirmed"}],
        model=object(),
        tools=args.tools,
        messages=[*args.messages, {"role": "user", "content": "Use the draft"}],
        cwd=str(workspace),
    )
    assert marker.exists() and runtime["_lastVerification"]["status"] == "passed"
    app._snapshot_session(args, state)
    save_session(state.session)
    assert not load_session(session.session_id).verification_state["pending"]


def test_passed_checkpoint_and_report_round_trip_without_rechecking(workspace):
    tools = ToolRegistry([])
    tools.mutation_count = tools.verified_mutation_count = 5
    runtime = {"_lastVerification": {"ok": True, "status": "passed", "reason": "tests passed"}}
    saved = create_new_session(str(workspace))
    saved.verification_state = snapshot_verification_state(tools, runtime)
    save_session(saved)
    new_tools = ToolRegistry([])
    new_runtime = {}
    restore_verification_state(new_tools, new_runtime, load_session(saved.session_id).verification_state)
    assert new_tools.mutation_count == new_tools.verified_mutation_count == 0
    assert new_runtime["_lastVerification"]["status"] == "passed"


def test_legacy_sessions_with_edits_are_conservatively_rechecked(workspace):
    tools = ToolRegistry([write_file_tool])
    restore_verification_state(
        tools, {}, None, [{"role": "assistant_tool_call", "toolName": "write_file", "input": {"path": "old.py"}}]
    )
    assert tools.mutation_count == 1


@pytest.mark.parametrize("writes", [False, True])
def test_legacy_restore_uses_dynamic_impact_with_validated_arguments(workspace, writes):
    from types import SimpleNamespace

    tool = ToolDefinition(
        name="arbitrary-integration",
        description="Variable impact",
        input_schema={},
        validator=lambda raw: SimpleNamespace(writes=raw["writes"]),
        run=lambda arguments, context: ToolResult(ok=True, output="done"),
        affects_workspace=lambda arguments: arguments.writes,
    )
    tools = ToolRegistry([tool])
    arguments = {"writes": writes}
    tools.execute(tool.name, arguments, ToolContext(cwd=str(workspace)))
    assert tools.mutation_count == int(writes)
    tools.mutation_count = 0
    restore_verification_state(
        tools, {}, None, [{"role": "assistant_tool_call", "toolName": tool.name, "input": arguments}]
    )
    assert tools.mutation_count == int(writes)


def test_legacy_restore_respects_explicit_impact_and_rechecks_incompatible_old_inputs(workspace):
    tool = ToolDefinition(
        name="custom-tool",
        description="Explicit impact",
        input_schema={},
        validator=lambda raw: raw["required"],
        run=lambda arguments, context: ToolResult(ok=True, output="done"),
        capabilities={ToolCapability.READ_ONLY},
        affects_workspace=True,
    )
    tools = ToolRegistry([tool])
    for arguments in ({"required": "value"}, {"old_schema": "value"}):
        restore_verification_state(
            tools, {}, None, [{"role": "assistant_tool_call", "toolName": tool.name, "input": arguments}]
        )
        assert tools.mutation_count == 1


def test_desktop_resume_restores_pending_verification(workspace):
    saved = create_new_session(str(workspace))
    saved.verification_state = {"version": 1, "pending": True, "last_report": None}
    save_session(saved)

    async def scenario():
        session = ClientSession(str(workspace))

        def initialize(**options):
            session.tools = ToolRegistry([write_file_tool])
            session.permissions = PermissionManager(str(workspace))
            session.runtime = {}

        session.initialize = initialize
        session.model_settings = Mock(return_value={})
        session._rebuild_system_prompt = Mock()
        await PepsiCodeServer()._dispatch(
            "session/resume", {"session_id": saved.session_id}, session, None, None, asyncio.get_running_loop()
        )
        assert session.tools.mutation_count > session.tools.verified_mutation_count

    asyncio.run(scenario())
