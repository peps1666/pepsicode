"""Plain QQ replies must not inherit project verification debt or bypass effects."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from pepsicode.core import session as sessions
from pepsicode.core.session_tree import recording_session
from pepsicode.core.verification import run_verified_turn
from pepsicode.execution.runtime import current_scope
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.ask_user import ask_user_tool


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "home" / "sessions")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    session = sessions.create_new_session(str(tmp_path))
    session.profile = "qq_private"
    scan = Mock(side_effect=ValueError("Verification snapshot exceeds 512 MiB"))
    monkeypatch.setattr("pepsicode.core.task_verification.workspace_state", scan)
    return session, scan


def answer(**options):
    return [*options["messages"], {"role": "assistant", "content": "你好！"}]


def run(session, tools, runtime, *, text="你好", runner=answer):
    with recording_session(session):
        return run_verified_turn(
            runtime=runtime,
            runner=runner,
            model=object(),
            tools=tools,
            messages=[{"role": "user", "content": text}],
            cwd=session.workspace,
            permissions=PermissionManager(session.workspace, mode="auto"),
        )


@pytest.mark.parametrize("pending_changes", [0, 1])
def test_ordinary_qq_reply_does_not_scan_or_validate_the_workspace(conversation, pending_changes):
    session, scan = conversation
    tools = ToolRegistry([])
    tools.mutation_count = pending_changes
    tools.verified_mutation_count = 0
    runtime = {"verificationMode": "always"}
    messages = run(session, tools, runtime)
    assert messages[-1]["content"] == "你好！"
    assert session.task_control["task"]["status"] == "answered"
    assert runtime["_lastVerification"]["reason_code"] == "conversation_answer"
    assert runtime["_lastVerification"]["status"] == "not_run"
    assert not runtime["_lastVerification"]["ok"]  # No project check is claimed as passed.
    assert not scan.called
    assert tools.mutation_count == pending_changes
    assert tools.verified_mutation_count == 0  # Prior verification debt is preserved.


@pytest.mark.parametrize("profile,text", [("default", "你好"), ("qq_private", "Implement feature.py")])
def test_project_requests_and_other_profiles_keep_their_verification(conversation, profile, text):
    session, scan = conversation
    session.profile = profile
    runtime = {"verificationMode": "always"}
    run(session, ToolRegistry([]), runtime, text=text)
    assert scan.called
    assert session.task_control["task"]["status"] == "blocked"
    assert runtime["_lastVerification"]["reason_code"] == "verification_snapshot_unavailable"


def test_qq_answer_that_actually_changes_a_file_still_requires_verification(conversation):
    session, scan = conversation
    target = Path(session.workspace) / "changed.txt"

    def mutate(_input, _context):
        target.write_text("changed", encoding="utf-8")
        return ToolResult(True, "changed")

    tools = ToolRegistry([ToolDefinition("write_probe", "write", {}, lambda value: value, mutate)])

    def unexpected_effect(**options):
        result = tools.execute("write_probe", {}, ToolContext(session.workspace, permissions=options["permissions"]))
        assert result.ok
        return answer(**options)

    runtime = {"verificationMode": "always"}
    run(session, tools, runtime, runner=unexpected_effect)
    assert target.read_text(encoding="utf-8") == "changed"
    assert scan.called
    assert session.task_control["task"]["status"] == "blocked"
    assert runtime["_lastVerification"]["reason_code"] == "verification_snapshot_unavailable"


def test_qq_conversation_keeps_unknown_effects_blocked(conversation):
    session, scan = conversation

    def unknown_effect(**options):
        current_scope().mark_unknown("uncertain-operation", tool="probe")
        return answer(**options)

    runtime = {"verificationMode": "always"}
    run(session, ToolRegistry([]), runtime, runner=unknown_effect)
    assert session.task_control["task"]["status"] == "unknown"
    assert runtime["_lastVerification"]["reason_code"] == "unknown_outcome"
    assert not scan.called


def test_qq_conversation_still_waits_for_required_user_answers(conversation):
    session, scan = conversation

    def ask(**options):
        ask_user_tool.run({"question": "你想讨论哪个话题？"}, ToolContext(session.workspace))
        return answer(**options)

    runtime = {"verificationMode": "always"}
    messages = run(session, ToolRegistry([]), runtime, runner=ask)
    assert session.task_control["task"]["status"] == "waiting_user"
    assert runtime["_lastVerification"]["reason_code"] == "awaiting_user"
    assert messages[-1]["awaitingUser"]
    assert not scan.called
