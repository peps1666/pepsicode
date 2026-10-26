from __future__ import annotations

import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import Mock

import pytest

from pepsicode.core import session as sessions
from pepsicode.core.session_tree import recording_session
from pepsicode.execution import external, process
from pepsicode.execution.filesystem import FILES
from pepsicode.execution.runtime import (
    ExecutionJournal,
    ExecutionScope,
    RequestBudget,
    bind_scope,
    current_scope,
    scoped_turn,
)
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.api_tester import api_tester_tool
from pepsicode.tools.execution_status import execution_status_tool
from pepsicode.tools.run_command import run_command_tool
from pepsicode.tools.save_memory import save_memory_tool


def test_journal_distinguishes_torn_tail_from_committed_corruption(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = ExecutionJournal(path)
    journal.append("tool/started", call_id="lost", tool="remote")
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"torn":')
    assert ExecutionJournal.interrupted_calls(path)[0]["call_id"] == "lost"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="corrupt committed"):
        ExecutionJournal.interrupted_calls(path)


def test_child_unknown_outcome_blocks_parent_and_reconciliation_requires_approval(tmp_path):
    decisions = []
    permissions = PermissionManager(
        str(tmp_path), prompt=lambda request: decisions.append(request) or {"decision": "deny_once"}
    )
    root = ExecutionScope(str(tmp_path), permissions=permissions, journal=ExecutionJournal(tmp_path / "journal.jsonl"))
    child = root.child()
    action = ToolDefinition("remote", "remote mutation", {}, lambda v: v, Mock(side_effect=OSError("lost response")))
    registry = ToolRegistry([action, execution_status_tool])
    registry.execute("remote", {}, ToolContext(str(tmp_path), execution=child))
    assert root.has_unknown
    with pytest.raises(PermissionError):
        FILES.write_text(ToolContext(str(tmp_path), execution=root), tmp_path / "blocked", "no")
    context = ToolContext(str(tmp_path), execution=root, permissions=permissions)
    evidence = "Read the remote record: the update committed once"
    (tmp_path / "record").write_text("committed once", encoding="utf-8")
    import json

    observed = json.loads(registry.execute("execution_status", {"action": "observe", "path": "record"}, context).output)
    reconcile = {
        "action": "reconcile",
        "evidence": evidence,
        "call_id": next(iter(root.unresolved)),
        "revision": observed["recovery_revision"],
        "observation_ids": [observed["id"]],
        "outcome": "occurred",
    }
    result = registry.execute("execution_status", reconcile, context)
    assert not result.ok and root.has_unknown
    permissions.prompt = lambda request: decisions.append(request) or {"decision": "allow_once"}
    result = registry.execute("execution_status", reconcile, context)
    assert result.ok and not root.has_unknown
    assert len(decisions) == 2
    assert evidence in decisions[-1]["details"][-1]
    FILES.write_text(context, tmp_path / "after", "okay")
    assert ExecutionJournal.interrupted_calls(root.journal.path) == []


def test_session_persists_running_journal_before_dispatch_and_recovers_without_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "sessions-home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions-home" / "sessions")
    session = sessions.create_new_session(str(tmp_path))
    runtime = {}
    dispatched = []

    @scoped_turn
    def first(**options):
        persisted = sessions.load_session(session.session_id)
        assert persisted.execution_state["state"] == "running"
        assert persisted.messages[-1]["content"] == "make a change"
        current_scope().record("tool/started", call_id="interrupted", tool="remote", arguments={"id": 1})
        raise KeyboardInterrupt()

    with recording_session(session), pytest.raises(KeyboardInterrupt):
        first(cwd=str(tmp_path), runtime=runtime, messages=[{"role": "user", "content": "make a change"}])
    loaded = sessions.load_session(session.session_id)
    assert loaded.execution_state["journal"]

    @scoped_turn
    def resumed(**options):
        assert current_scope().has_unknown
        assert options["messages"][-1]["internal"]
        action = ToolDefinition(
            "remote",
            "remote mutation",
            {},
            lambda v: v,
            lambda *_: dispatched.append(True) or ToolResult(True, "changed"),
        )
        result = ToolRegistry([action]).execute("remote", {}, ToolContext(str(tmp_path), execution=current_scope()))
        assert result.status == "blocked"
        return [{"role": "assistant", "content": "done"}]

    with recording_session(loaded):
        result = resumed(cwd=str(tmp_path), runtime={}, messages=loaded.messages)
    assert not dispatched
    assert result[-1]["isError"]
    assert loaded.execution_state["state"] == "unknown"
    assert ExecutionJournal.interrupted_calls(loaded.execution_state["journal"])[0]["call_id"] == "interrupted"


def test_reserved_journal_is_not_editable_even_with_full_access(tmp_path):
    context = ToolContext(str(tmp_path), permissions=PermissionManager(str(tmp_path), mode="full_access"))
    with pytest.raises(PermissionError, match="owned by the harness"):
        FILES.write_text(context, tmp_path / ".pepsi-code" / "executions" / "audit.jsonl", "forged")


def test_approval_does_not_survive_a_mode_change(tmp_path):
    permissions = PermissionManager(str(tmp_path))

    def approve(_request):
        permissions.set_access_mode("read_only")
        return {"decision": "allow_once"}

    permissions.prompt = approve
    with pytest.raises(PermissionError):
        FILES.write_text(ToolContext(str(tmp_path), permissions=permissions), tmp_path / "target", "no")
    assert not (tmp_path / "target").exists()


def test_symlink_cannot_redirect_a_write_outside_the_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("original", encoding="utf-8")
    try:
        (root / "link").symlink_to(outside)
    except OSError:
        pytest.skip("This OS account cannot create symlinks")
    context = ToolContext(
        str(root), permissions=PermissionManager(str(root), prompt=lambda _: {"decision": "deny_once"})
    )
    with pytest.raises(PermissionError):
        FILES.write_text(context, root / "link", "no")
    assert outside.read_text(encoding="utf-8") == "original"


def test_external_denial_stops_before_network_and_lost_response_is_unknown(tmp_path, monkeypatch):
    open_network = Mock(side_effect=AssertionError("unapproved network access"))
    monkeypatch.setattr(external.urllib.request, "build_opener", open_network)
    context = ToolContext(
        str(tmp_path), permissions=PermissionManager(str(tmp_path), prompt=lambda _: {"decision": "deny_once"})
    )
    request = urllib.request.Request("https://example.test/record", data=b"change", method="POST")
    with pytest.raises(PermissionError):
        external.urlopen(request, context=context)
    assert not open_network.called

    opener = Mock()
    opener.open.side_effect = urllib.error.URLError("response lost after dispatch")
    monkeypatch.setattr(external.urllib.request, "build_opener", lambda *_: opener)
    permissions = PermissionManager(str(tmp_path), mode="full_access")
    scope = ExecutionScope(str(tmp_path), permissions=permissions, journal=ExecutionJournal(tmp_path / "journal.jsonl"))
    result = ToolRegistry([api_tester_tool]).execute(
        "api_tester",
        {"url": request.full_url, "method": "POST"},
        ToolContext(str(tmp_path), permissions=permissions, execution=scope),
    )
    assert result.status == "unknown" and scope.has_unknown


@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
def test_foreground_stop_drains_descendants_before_returning(tmp_path, cancel):
    sentinel = tmp_path / "escaped-child"
    ready = tmp_path / "ready"
    child = f"import time; from pathlib import Path; time.sleep(1.2); Path({str(sentinel)!r}).write_text('escaped')"
    parent = f"import subprocess,sys,time; from pathlib import Path; subprocess.Popen([sys.executable,'-c',{child!r}]); Path({str(ready)!r}).write_text('ready'); time.sleep(30)"
    permissions = PermissionManager(str(tmp_path), mode="full_access")
    event = threading.Event()
    context = ToolContext(str(tmp_path), permissions=permissions, cancellation_event=event)

    def stop_when_ready():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if cancel:
            event.set()

    watcher = threading.Thread(target=stop_when_ready, daemon=True)
    watcher.start()
    if cancel:
        from pepsicode.cancellation import TurnCancelledError

        expected = TurnCancelledError
    else:
        expected = subprocess.TimeoutExpired
    try:
        with pytest.raises(expected):
            process.run(
                [sys.executable, "-c", parent], context=context, capture_output=True, timeout=0.6 if not cancel else 8
            )
        assert context.effect_state == "unknown"
        watcher.join(timeout=5)
        assert ready.exists()
        time.sleep(1.3)
        assert not sentinel.exists()
        assert not permissions._processes
    finally:
        permissions.close()


def test_deadline_is_shared_by_the_actual_command_not_only_model_steps(tmp_path):
    runtime = {"taskTimeout": 1.0}
    permissions = PermissionManager(str(tmp_path), mode="full_access")

    @scoped_turn
    def turn(**options):
        scope = current_scope()
        result = ToolRegistry([run_command_tool]).execute(
            "run_command",
            {"command": sys.executable, "args": ["-c", "import time; time.sleep(30)"]},
            ToolContext(
                str(tmp_path),
                permissions=permissions,
                cancellation_event=options["cancellation_event"],
                execution=scope,
            ),
        )
        assert result.status in {"cancelled", "timed_out"}
        return [{"role": "assistant", "content": "stopped", "isError": True}]

    started = time.monotonic()
    turn(cwd=str(tmp_path), runtime=runtime, messages=[], permissions=permissions)
    assert time.monotonic() - started < 5
    assert runtime["_lastExecution"]["state"] == "unknown"
    assert not permissions._processes


def test_child_request_budget_and_cancellation_are_shared(tmp_path):
    event = threading.Event()
    root = ExecutionScope(str(tmp_path), cancellation_event=event, budget=RequestBudget(CostTracker(), max_requests=1))
    child = root.child()
    with child.budget.request(child, {"model": "test"}):
        pass
    with pytest.raises(RuntimeError, match="request limit"), root.budget.request(root, {"model": "test"}):
        pass
    event.set()
    from pepsicode.cancellation import TurnCancelledError

    with bind_scope(child), pytest.raises(TurnCancelledError):
        child.check_cancelled()


def test_child_local_cancel_handle_cannot_detach_parent_cancellation(tmp_path):
    from pepsicode.cancellation import TurnCancelledError

    parent_event = threading.Event()
    root = ExecutionScope(str(tmp_path), cancellation_event=parent_event)
    child = root.child(cancellation_event=threading.Event())
    parent_event.set()
    with pytest.raises(TurnCancelledError):
        child.check_cancelled()


def test_direct_memory_tool_cannot_write_or_update_prompt_memory_when_denied(tmp_path, monkeypatch):
    from pepsicode.context import memory_store
    from pepsicode.context.memory import MemoryManager, MemoryScope

    monkeypatch.setattr(memory_store, "PEPSI_CODE_DIR", tmp_path / "user-home")
    manager = MemoryManager(str(tmp_path))
    permissions = PermissionManager(str(tmp_path), mode="read_only")
    context = ToolContext(str(tmp_path), permissions=permissions, memory=manager)
    with pytest.raises(PermissionError):
        save_memory_tool.run(
            save_memory_tool.validator({"scope": "project", "category": "rules", "content": "forbidden"}), context
        )
    assert not (tmp_path / ".pepsi-code-memory").exists()
    assert manager.memories[MemoryScope.PROJECT].entries == []


def test_background_process_observes_parent_cancel_and_session_close(tmp_path):
    from pepsicode.cancellation import TurnCancelledError

    permissions = PermissionManager(str(tmp_path), mode="full_access")
    event = threading.Event()
    scope = ExecutionScope(str(tmp_path), permissions=permissions, cancellation_event=event)
    context = ToolContext(
        str(tmp_path), permissions=permissions, cancellation_event=event, execution=scope, tool_use_id="background"
    )
    child = process.Popen([sys.executable, "-c", "import time; time.sleep(30)"], context=context, persistent=True)
    event.set()
    child.wait(timeout=5)
    assert child.poll() is not None
    with pytest.raises(TurnCancelledError):
        scope.check_cancelled()
    permissions.close()
    assert not permissions._processes


def test_process_context_cannot_omit_its_scope_cancellation(tmp_path):
    from pepsicode.cancellation import TurnCancelledError

    permissions = PermissionManager(str(tmp_path), mode="full_access")
    event = threading.Event()
    scope = ExecutionScope(str(tmp_path), permissions=permissions, cancellation_event=event)
    context = ToolContext(str(tmp_path), execution=scope)
    timer = threading.Timer(0.25, event.set)
    timer.start()
    try:
        with pytest.raises(TurnCancelledError):
            process.run([sys.executable, "-c", "import time; time.sleep(30)"], context=context, timeout=5)
        assert context.cancellation_event is event
        assert not permissions._processes
    finally:
        timer.cancel()
        permissions.close()


@pytest.mark.parametrize("operation", ["mkdir", "symlink"])
def test_structural_file_services_recheck_permissions_after_approval(tmp_path, operation):
    permissions = PermissionManager(str(tmp_path))

    def approve(_request):
        permissions.set_access_mode("read_only")
        return {"decision": "allow_once"}

    permissions.prompt = approve
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    target = tmp_path / "target"
    with bind_scope(ExecutionScope(str(tmp_path), permissions=permissions)), pytest.raises(PermissionError):
        if operation == "mkdir":
            FILES.mkdir(target)
        else:
            FILES.symlink(source, target)
    assert not target.exists()
