"""Supervision loop pass conditions (Orchestrator checklist 2026-11-16).

Scenario 1: one work item fails after committing partial work; the coordinator
receives the failure event, retries the same ``work_id``, the new attempt
continues from the committed output, the retest passes and the delivered
version passes parent acceptance — without user prompting.

Scenario 2: two candidates pass their own checks, the combined integration
fails, delivery is refused, the direct retry is refused, and a repair Worker
prepared from the failed integration baseline fixes the combined behavior;
the re-integration is delivered and accepted while the failed version keeps
its evidence.

Scenario 3: two passing candidates edit the same file and cause a real Git
cherry-pick conflict; integration aborts the pick and preserves both candidate
worktrees and records, a resolution Worker combines both candidates on a clean
checkout and passes checks, and the resolved integration is accepted and
delivered.

Only model decisions are scripted; Git repositories, checks and Worker
processes are real.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pepsicode import config
from pepsicode.core import session as sessions
from pepsicode.core.agent_loop import run_agent_turn, run_agent_turn_stream
from pepsicode.core.session_tree import recording_session
from pepsicode.core.verification import run_verified_turn
from pepsicode.execution.runtime import current_scope
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolRegistry
from pepsicode.tools.run_command import run_command_tool
from pepsicode.tools.task_state import task_state_tool
from pepsicode.tools.write_file import write_file_tool
from pepsicode.types import AgentStep, StreamToken

COMBINED_CHECK = """
from pathlib import Path
a, b = Path('alpha.txt'), Path('beta.txt')
if a.exists() and b.exists():
    assert a.read_text() == b.read_text(), 'candidate contracts disagree'
print('candidate contract passed')
"""

CONFLICT_CHECK = """
from pathlib import Path
shared = Path('shared.txt')
assert shared.exists(), 'missing shared.txt'
text = shared.read_text()
a, b = Path('alpha.txt'), Path('beta.txt')
if a.exists():
    assert 'alpha' in text, 'missing alpha entry in shared.txt'
if b.exists():
    assert 'beta' in text, 'missing beta entry in shared.txt'
print('conflict check passed')
"""

FEATURE_CHECK = """
from pathlib import Path
assert Path('feature.txt').read_text() == 'fixed', 'missing normalization'
print('normalization ok')
"""


def git(checkout, *args):
    return subprocess.run(
        ["git", *args], cwd=checkout, check=True, capture_output=True, text=True, timeout=15
    ).stdout.strip()


def commit_args(name):
    return ["-c", f"user.name={name}", "-c", f"user.email={name}@example.invalid"]


def build_project(tmp_path, monkeypatch, check_source):
    git(tmp_path, "init")
    (tmp_path / "check.py").write_text(check_source, encoding="utf-8")
    git(tmp_path, "add", "check.py")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "baseline")
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", config.PEPSI_CODE_DIR)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", config.PEPSI_CODE_DIR / "sessions")
    session = sessions.create_new_session(str(tmp_path))
    permissions = PermissionManager(str(tmp_path), mode="full_access")
    return session, permissions


@pytest.fixture
def feature_project(tmp_path, monkeypatch):
    session, permissions = build_project(tmp_path, monkeypatch, FEATURE_CHECK)
    try:
        yield session, permissions
    finally:
        permissions.close()


@pytest.fixture
def combined_project(tmp_path, monkeypatch):
    session, permissions = build_project(tmp_path, monkeypatch, COMBINED_CHECK)
    try:
        yield session, permissions
    finally:
        permissions.close()


@pytest.fixture
def conflict_project(tmp_path, monkeypatch):
    session, permissions = build_project(tmp_path, monkeypatch, CONFLICT_CHECK)
    try:
        yield session, permissions
    finally:
        permissions.close()


def calls(*items):
    return AgentStep(
        type="tool_calls",
        calls=[{"id": key, "toolName": name, "input": args} for key, name, args in items],
    )


def command(key, executable, *args):
    return key, "run_command", {"command": str(executable), "args": list(args), "timeout": 15}


def result_map(messages):
    return {msg.get("toolUseId"): msg for msg in messages if msg.get("role") == "tool_result"}


def orchestration_events(messages):
    return [m.get("content", "") for m in messages if (m.get("source") or {}).get("kind") == "orchestration-event"]


def run_single_worker_scenario(project, monkeypatch):
    """Pass condition 1: fail after a partial commit, repair, retest, deliver."""
    session, permissions = project
    seen = {"worker_launches": [], "checks": []}

    class Model:
        def next(self, messages):
            results = result_map(messages)
            scope = current_scope()
            if scope.parent_task_id:
                if "edit" not in results:
                    seen["worker_launches"].append(scope.trace_id)
                    if len(seen["worker_launches"]) == 2:
                        # The retry must continue from attempt 1's commit.
                        assert Path(scope.cwd, "feature.txt").read_text(encoding="utf-8") == "partial"
                    content = "partial" if len(seen["worker_launches"]) == 1 else "fixed"
                    return calls(("edit", "write_file", {"path": "feature.txt", "content": content}))
                assert not results["edit"]["isError"], results["edit"]
                if "add" not in results:
                    return calls(command("add", "git", "add", "feature.txt"))
                assert not results["add"]["isError"], results["add"]
                if "commit" not in results:
                    return calls(command("commit", "git", *commit_args("Worker"), "commit", "-m", "worker update"))
                assert not results["commit"]["isError"], results["commit"]
                if "check" not in results:
                    return calls(command("check", sys.executable, "-B", "check.py"))
                if "report" not in results:
                    check = results["check"]
                    seen["checks"].append({"failed": check["isError"], "output": str(check["content"])})
                    if check["isError"]:
                        assert "missing normalization" in check["content"], check
                        return calls(
                            (
                                "report",
                                "agent_report",
                                {"status": "failed", "summary": "check.py failed: missing normalization"},
                            )
                        )
                    return AgentStep(type="assistant", content="Normalization fixed; check passed.")
                assert not results["report"]["isError"], results["report"]
                return AgentStep(type="assistant", content="The implementation is not complete; check.py failed.")

            events = orchestration_events(messages)
            if "dispatch" not in results:
                return calls(
                    (
                        "dispatch",
                        "task_dispatch",
                        {
                            "work_key": "normalize",
                            "goal": "Fix feature.txt and run check.py",
                            "agent_type": "general",
                            "deliverable": "project",
                        },
                    )
                )
            assert not results["dispatch"]["isError"], results["dispatch"]
            receipt = json.loads(results["dispatch"]["content"])
            seen["receipt"] = receipt
            if any(": failed." in event for event in events) and "retry" not in results:
                return calls(("retry", "task_retry", {"attempt_id": receipt["attempt_id"]}))
            if "retry" in results and "integrate" not in results:
                assert not results["retry"]["isError"], results["retry"]
                if not any(": result_ready." in event for event in events):
                    return AgentStep(type="assistant", content="Yield to scheduler.")
                if "define" not in results:
                    return calls(
                        (
                            "define",
                            "task_state",
                            {
                                "action": "define",
                                "task_id": session.task_control["task"]["id"],
                                "revision": receipt["revision"],
                                "criteria": [
                                    {"id": "artifact", "kind": "file_contains", "path": "feature.txt", "text": "fixed"}
                                ],
                            },
                        )
                    )
                assert not results["define"]["isError"], results["define"]
                assert json.loads(results["define"]["content"])["revision"] == receipt["revision"]
                return calls(("integrate", "task_integrate", {"work_ids": [receipt["work_id"]]}))
            if "integrate" in results and "deliver" not in results:
                assert not results["integrate"]["isError"], results["integrate"]
                seen["integration"] = json.loads(results["integrate"]["content"])
                assert seen["integration"]["state"] == "accepted", seen["integration"]
                return calls(("deliver", "task_deliver", {"integration_id": seen["integration"]["integration_id"]}))
            if "deliver" in results:
                assert results["deliver"]["isError"] is False, results["deliver"]
                seen["delivery"] = json.loads(results["deliver"]["content"])
            return AgentStep(type="assistant", content="Coordination finished.")

    monkeypatch.setattr("pepsicode.orchestration.interactive.MockModelAdapter", Model)
    error = None
    try:
        with recording_session(session):
            run_verified_turn(
                runtime={
                    "orchestration": {"enabled": True, "workerIsolation": "workspace"},
                    "verificationMode": "off",
                    "verificationCommands": [
                        {"name": "combined contract", "command": [sys.executable, "-B", "check.py"]}
                    ],
                    "maxIterations": 1,
                    "taskTimeout": 120,
                },
                runner=run_agent_turn,
                model=Model(),
                tools=ToolRegistry([run_command_tool, write_file_tool, task_state_tool]),
                messages=[{"role": "user", "content": "Implement feature.txt with normalization and pass check.py."}],
                cwd=session.workspace,
                permissions=permissions,
                task_kind="change",
            )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    saved = sessions.load_session(session.session_id)
    assert saved is not None
    seen["error"] = error
    seen["state"] = saved.task_control["orchestration"]
    seen["task"] = saved.task_control["task"]
    seen["messages"] = saved.messages
    seen["workspace"] = session.workspace
    return seen


def test_empty_folder_builds_a_runnable_product_in_place(tmp_path, monkeypatch):
    project = tmp_path / "new-project"
    project.mkdir()
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", config.PEPSI_CODE_DIR)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", config.PEPSI_CODE_DIR / "sessions")
    session = sessions.create_new_session(str(project))
    permissions = PermissionManager(str(project), mode="full_access")
    registry = ToolRegistry([task_state_tool, write_file_tool, run_command_tool])

    class Model:
        def next(self, messages):
            results = result_map(messages)
            assert not current_scope().parent_task_id
            if "write" not in results:
                return calls(("write", "write_file", {"path": "converter.py", "content": "print(100 * 9 / 5 + 32)\n"}))
            assert not results["write"]["isError"]
            if "define" not in results:
                task = session.task_control["task"]
                return calls(
                    (
                        "define",
                        "task_state",
                        {
                            "action": "define",
                            "task_id": task["id"],
                            "revision": task["revision"],
                            "criteria": [
                                {"id": "run", "kind": "command", "argv": [sys.executable, "-B", "converter.py"]}
                            ],
                        },
                    )
                )
            return AgentStep(type="assistant", content="Run python converter.py.")

    try:
        with recording_session(session):
            output = run_verified_turn(
                runtime={"orchestration": {"enabled": True, "workerIsolation": "workspace"}},
                runner=run_agent_turn,
                model=Model(),
                tools=registry,
                messages=[{"role": "user", "content": "Make a runnable temperature converter in converter.py"}],
                cwd=str(project),
                permissions=permissions,
            )
    finally:
        permissions.close()
    assert not (project / ".git").exists()
    assert not session.task_control["orchestration"]["work"]
    # The parent registry records the editor operation and the verifier's command.
    assert registry.mutation_count == 2
    assert session.task_control["task"]["status"] == "complete", output[-1]
    assert subprocess.check_output([sys.executable, "-B", "converter.py"], cwd=project, text=True).strip() == "212.0"


def test_hardened_report_dispatch_from_subdirectory_stops_without_local_substitution(feature_project):
    parent, _ = feature_project
    folder = Path(parent.workspace) / "1"
    folder.mkdir()
    session = sessions.create_new_session(str(folder))
    permissions = PermissionManager(str(folder), mode="full_access")

    class Model:
        def next_stream(self, messages):
            assert not result_map(messages), "A blocked report-only dispatch must return control to the user"
            for key in ("temperature", "duration"):
                yield StreamToken(
                    type="tool_use",
                    tool_id=key,
                    tool_name="task_dispatch",
                    tool_input_partial=json.dumps(
                        {
                            "work_key": key,
                            "goal": key,
                            "agent_type": "general",
                            "deliverable": "worker_reports",
                        }
                    ),
                )
            yield StreamToken(type="done", stop_reason="tool_use")

    try:
        with recording_session(session):
            output = run_verified_turn(
                runtime={"orchestration": {"enabled": True, "workerIsolation": "hardened"}},
                runner=run_agent_turn_stream,
                model=Model(),
                tools=ToolRegistry([write_file_tool, run_command_tool]),
                messages=[{"role": "user", "content": "派两个 Worker 并行试做，成果留在各自工作区。"}],
                cwd=str(folder),
                permissions=permissions,
            )
    finally:
        permissions.close()
    assert output[-1]["awaitingUser"]
    assert f"Git 仓库根目录 {parent.workspace} 启动" in output[-1]["content"]
    assert "首次提交" not in output[-1]["content"]
    assert all(result_map(output)[key]["isError"] for key in ("temperature", "duration"))
    assert session.task_control["task"]["status"] == "waiting_user"
    assert not session.task_control["orchestration"]["work"]
    assert not (folder / "temperature").exists() and not (folder / "duration").exists()


def test_empty_non_git_folder_runs_parallel_directory_workers_under_auto_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "home" / "sessions")
    monkeypatch.setattr("pepsicode.config.PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    empty = tmp_path / "empty_folder"
    empty.mkdir()
    session = sessions.create_new_session(str(empty))
    prompts = []
    permissions = PermissionManager(
        str(empty), mode="auto", prompt=lambda req: prompts.append(req) or {"decision": "deny_once"}
    )

    class Model:
        tools = ToolRegistry([])

        def next(self, messages):
            results = result_map(messages)
            scope = current_scope()
            if scope.parent_task_id:
                assert "isolated workspace directory" in messages[0]["content"]
                assert "Do not run git commit" in messages[0]["content"]
                goal = messages[1]["content"]
                filename = "temp.py" if "celsius" in goal else "fmt.py"
                code = (
                    "def c_to_f(c):\n    return c * 9 / 5 + 32\nassert c_to_f(100) == 212\n"
                    if filename == "temp.py"
                    else "def fmt(s):\n    return f'{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}'\nassert fmt(3661) == '01:01:01'\n"
                )
                if "write" not in results:
                    return calls(("write", "write_file", {"path": filename, "content": code}))
                if "test" not in results:
                    return calls(command("test", sys.executable, "-B", filename))
                assert not results["test"]["isError"], results["test"]["content"]
                return AgentStep(type="assistant", content=f"{filename} passed in isolated checkout")

            events = [m["content"] for m in messages if m.get("source", {}).get("kind") == "orchestration-event"]
            if not events and not results:
                return calls(
                    (
                        "w1",
                        "task_dispatch",
                        {
                            "work_key": "celsius",
                            "goal": "Implement celsius converter",
                            "agent_type": "general",
                            "deliverable": "worker_reports",
                        },
                    ),
                    (
                        "w2",
                        "task_dispatch",
                        {
                            "work_key": "seconds",
                            "goal": "Implement seconds formatter",
                            "agent_type": "general",
                            "deliverable": "worker_reports",
                        },
                    ),
                )
            if not events:
                return AgentStep(type="assistant", content="Two directory-isolated Workers dispatched.")
            return AgentStep(type="assistant", content="Both Workers finished in their own workspaces.")

    monkeypatch.setattr("pepsicode.orchestration.interactive.MockModelAdapter", Model)
    try:
        with recording_session(session):
            output = run_verified_turn(
                runtime={"orchestration": {"enabled": True, "workerIsolation": "workspace", "maxConcurrentWorkers": 2}},
                runner=run_agent_turn,
                model=Model(),
                tools=ToolRegistry([write_file_tool, run_command_tool]),
                messages=[{"role": "user", "content": "派两个 Worker 并行试做，样例留在各自工作区并汇报结果。"}],
                cwd=str(empty),
                permissions=permissions,
            )
    finally:
        permissions.close()

    assert not prompts, f"Codex-style auto mode should not prompt for normal workspace work: {prompts}"
    task = session.task_control["task"]
    assert task["status"] == "complete" and task["assurance"] == "worker_reports"
    envs = session.task_control["orchestration"]["environments"]
    assert len(envs) == 2
    assert all(env["workspace_kind"] == "directory" for env in envs.values())
    checkouts = [Path(env["worktree"]) for env in envs.values()]
    assert any((c / "temp.py").is_file() for c in checkouts)
    assert any((c / "fmt.py").is_file() for c in checkouts)
    assert not (empty / "temp.py").exists() and not (empty / "fmt.py").exists()
    assert output[-1]["verification"]["reason_code"] == "worker_reports_collected"


def test_non_git_directory_workers_integrate_deliver_and_detect_conflicts(tmp_path, monkeypatch):
    from threading import Event

    from pepsicode.core.task_control import begin_task
    from pepsicode.execution.runtime import (
        ExecutionJournal,
        ExecutionScope,
        RequestBudget,
        bind_scope,
        execution_journal_dir,
    )
    from pepsicode.llm.cost_tracker import CostTracker
    from pepsicode.orchestration.environment import prepare_owned
    from pepsicode.orchestration.external_delivery import deliver_integration
    from pepsicode.orchestration.integration import integrate_candidates, run_integration_acceptance
    from pepsicode.orchestration.store import admit_work, claim_launch, settle_launch

    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "home" / "sessions")
    monkeypatch.setattr("pepsicode.config.PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    folder = tmp_path / "plain_project"
    folder.mkdir()
    (folder / "base.txt").write_text("base\n", encoding="utf-8")
    session = sessions.create_new_session(str(folder))
    permissions = PermissionManager(str(folder), mode="auto")
    journal = ExecutionJournal(execution_journal_dir(str(folder)) / "dir_integ.jsonl")
    scope = ExecutionScope(
        str(folder),
        permissions=permissions,
        cancellation_event=Event(),
        budget=RequestBudget(CostTracker()),
        journal=journal,
    )
    with recording_session(session), bind_scope(scope):
        task = begin_task([{"role": "user", "content": "Build two modules"}], {})
        w1 = admit_work(
            session,
            task_id=task["id"],
            revision=1,
            work_key="mod-a",
            specification={"goal": "a", "agent_type": "general", "deliverable": "project"},
        )
        w2 = admit_work(
            session,
            task_id=task["id"],
            revision=1,
            work_key="mod-b",
            specification={"goal": "b", "agent_type": "general", "deliverable": "project"},
        )
        env1 = prepare_owned(session, w1["work_id"], scope=scope, isolation="workspace")
        env2 = prepare_owned(session, w2["work_id"], scope=scope, isolation="workspace")
        assert env1["workspace_kind"] == "directory" and env2["workspace_kind"] == "directory"
        assert (Path(env1["worktree"]) / "base.txt").read_text(encoding="utf-8") == "base\n"
        assert claim_launch(session, generation="host") and claim_launch(session, generation="host")
        (Path(env1["worktree"]) / "a.py").write_text("A = 1\n", encoding="utf-8")
        (Path(env2["worktree"]) / "b.py").write_text("B = 2\n", encoding="utf-8")
        assert settle_launch(
            session, attempt_id=w1["attempt_id"], generation="host", outcome="result_ready", summary="ok"
        )
        assert settle_launch(
            session, attempt_id=w2["attempt_id"], generation="host", outcome="result_ready", summary="ok"
        )

        integrated = integrate_candidates(session, scope, [w1["work_id"], w2["work_id"]])
        assert integrated["state"] == "integrated" and integrated["workspace_kind"] == "directory"
        runtime = {
            "verificationCommands": [
                {"name": "check", "command": [sys.executable, "-c", "import a, b; assert a.A + b.B == 3"]}
            ]
        }
        accepted = run_integration_acceptance(session, scope, integrated["integration_id"], runtime=runtime)
        assert accepted["state"] == "accepted" and accepted["acceptance"]["valid"] is True

        with pytest.raises(ValueError, match="Git repository"):
            deliver_integration(session, scope, accepted["integration_id"], push=True, runtime=runtime)
        delivered = deliver_integration(session, scope, accepted["integration_id"], runtime=runtime)
        assert delivered["status"] == "local"
        assert (folder / "a.py").read_text(encoding="utf-8") == "A = 1\n"
        assert (folder / "b.py").read_text(encoding="utf-8") == "B = 2\n"

        # Conflict check: two directory Workers modifying the same file differently
        (Path(env1["worktree"]) / "shared.py").write_text("X = 1\n", encoding="utf-8")
        (Path(env2["worktree"]) / "shared.py").write_text("X = 2\n", encoding="utf-8")
        conflicted = integrate_candidates(session, scope, [w1["work_id"], w2["work_id"]])
        assert conflicted["state"] == "conflict"
        assert conflicted["conflicts"] and "shared.py" in conflicted["conflicts"][0]["files"]
    permissions.close()


def test_reports_only_accepts_worker_results_without_integrating(feature_project, monkeypatch):
    session, permissions = feature_project
    quote = "这次成果各自留在工作区，给我看看两边跑出来的结果就行"
    result_batches = []

    class Model:
        tools = ToolRegistry([])

        def next(self, messages):
            results = result_map(messages)
            scope = current_scope()
            if scope.parent_task_id:
                assert "Do not commit" in messages[0]["content"]
                if "write" not in results:
                    return calls(("write", "write_file", {"path": "feature.txt", "content": "fixed"}))
                if "test" not in results:
                    return calls(command("test", sys.executable, "-B", "check.py"))
                assert not results["test"]["isError"], results["test"]
                return AgentStep(type="assistant", content="Created feature.txt; check.py passed.")
            if "dispatch" not in results:
                return calls(
                    (
                        "dispatch",
                        "task_dispatch",
                        {
                            "work_key": "normalize",
                            "goal": "Write feature.txt and run check.py",
                            "agent_type": "general",
                            "deliverable": "worker_reports",
                        },
                    ),
                    (
                        "second",
                        "task_dispatch",
                        {
                            "work_key": "other",
                            "goal": "Try another version",
                            "agent_type": "general",
                            "deliverable": "worker_reports",
                        },
                    ),
                )
            events = orchestration_events(messages)
            if events:
                assert self.tools.find("task_integrate") is None
                assert self.tools.find("task_deliver") is None
                assert not any(m.get("source", {}).get("kind") == "agent-notice" for m in messages)
                result_batches.append(len(events))
            return AgentStep(type="assistant", content="\n".join(events) or "Queued.")

    monkeypatch.setattr("pepsicode.orchestration.interactive.MockModelAdapter", Model)
    with recording_session(session):
        output = run_verified_turn(
            runtime={
                "orchestration": {"enabled": True, "workerIsolation": "workspace", "maxConcurrentWorkers": 1},
                "maxIterations": 1,
            },
            runner=run_agent_turn,
            model=Model(),
            tools=ToolRegistry([run_command_tool, write_file_tool]),
            messages=[
                {
                    "role": "user",
                    "content": f"让两个人分别实现 feature.txt、运行 check.py。{quote}。",
                }
            ],
            cwd=session.workspace,
            permissions=permissions,
            task_kind="change",
        )
    saved = sessions.load_session(session.session_id)
    task = saved.task_control["task"]
    orchestration = saved.task_control["orchestration"]
    worktree = Path(task["acceptance"]["workers"][0]["worktree"])
    assert task["status"] == "complete" and task["assurance"] == "worker_reports"
    assert result_batches == [2]
    assert task["orchestration_output"]["kind"] == "worker_reports"
    assert len(task["acceptance"]["workers"]) == 2
    assert output[-1]["verification"]["reason_code"] == "worker_reports_collected"
    assert str(worktree) in output[-1]["content"] and "check.py passed" in output[-1]["content"]
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "fixed"
    assert git(worktree, "rev-parse", "HEAD") == git(session.workspace, "rev-parse", "HEAD")
    assert not (Path(session.workspace) / "feature.txt").exists()
    assert not orchestration.get("integrations") and not orchestration.get("deliveries")


def test_failed_worker_repairs_retests_and_delivers(feature_project, monkeypatch):
    seen = run_single_worker_scenario(feature_project, monkeypatch)
    assert seen["error"] is None, seen["error"]
    # The failure was settled truthfully: the check really failed.
    assert seen["checks"][0]["failed"], seen["checks"]
    # The Worker's explicit failed report reached the coordinator as a failure
    # event, not as a result_ready.
    assert any(event["kind"] == "failed" for event in seen["state"]["events"]), seen["state"]["events"]
    assert not any(event["kind"] == "result_ready" for event in seen["state"]["events"][:1]), seen["state"]["events"]
    evidence = json.loads(seen["state"]["work"][0]["summary"].split("\n", 1)[0])
    assert evidence["stage"] == "failed" and evidence["class"] == "code", evidence
    assert evidence["check"] == "run_command" and "missing normalization" in evidence["output"], evidence
    # Same work item, two distinct attempts, both attributed.
    work_ids = [row["work_id"] for row in seen["state"]["work"]]
    assert len(work_ids) == 2 and len(set(work_ids)) == 1, seen["state"]["work"]
    assert len({row["attempt_id"] for row in seen["state"]["work"]}) == 2, seen["state"]["work"]
    assert [row["state"] for row in seen["state"]["work"]] == ["failed", "result_ready"], seen["state"]["work"]
    # The retry continued from the committed partial work: attempt 1's commit
    # is recorded as attributable output and stays an ancestor of the final head.
    environment = seen["state"]["environments"][work_ids[0]]
    updates = environment["head_updates"]
    assert [row["attempt_id"] for row in updates] == [row["attempt_id"] for row in seen["state"]["work"]], updates
    final_head = git(environment["worktree"], "rev-parse", "HEAD")
    assert environment["expected_head"] != final_head, environment
    assert git(environment["worktree"], "merge-base", "--is-ancestor", updates[0]["to"], final_head) == ""
    assert (
        len(
            git(
                environment["worktree"], "rev-list", "--first-parent", f"{environment['base_sha']}..{final_head}"
            ).splitlines()
        )
        == 2
    )
    # The retest passed without user prompting.
    assert seen["checks"][-1]["failed"] is False, seen["checks"]
    assert not any(message.get("awaitingUser") for message in seen["messages"]), seen["messages"][-3:]
    # Delivery and parent acceptance are bound to the delivered version.
    assert seen["delivery"]["status"] == "local", seen["delivery"]
    assert seen["task"]["status"] == "complete", seen["task"]
    acceptance = seen["task"]["acceptance"]
    assert acceptance["valid"] is True, acceptance
    assert acceptance["delivered"]["head_sha"] == seen["integration"]["head_sha"], acceptance
    # The orchestrator's own workspace never held the artifact.
    assert not (Path(seen["workspace"]) / "feature.txt").exists()


def run_two_candidate_scenario(project, monkeypatch):
    """Pass condition 2: candidates pass, combined check fails, repair delivers."""
    session, permissions = project
    seen = {"worker_checks": {}, "repair_head": None}

    class Model:
        def next(self, messages):
            results = [m for m in messages if m.get("role") == "tool_result"]
            scope = current_scope()
            role = messages[1]["content"]
            if role.startswith("repair"):
                if not results:
                    seen["repair_head"] = git(scope.cwd, "rev-parse", "HEAD")
                    seen["repair_has_candidates"] = {
                        name: (Path(scope.cwd) / name).exists() for name in ("alpha.txt", "beta.txt")
                    }
                    return calls(
                        ("fix", "write_file", {"path": "alpha.txt", "content": "beta"}),
                        command("add", "git", "add", "alpha.txt"),
                        command("commit", "git", *commit_args("Repair"), "commit", "-m", "align contract"),
                        command("check", sys.executable, "-B", "check.py"),
                    )
                assert not any(message.get("isError") for message in results), results
                assert "candidate contract passed" in results[-1]["content"], results
                return AgentStep(type="assistant", content="Contract aligned; combined check passed.")
            if results:
                assert len(results) == 4, results
                seen["worker_checks"][role] = [message["content"] for message in results]
                assert not any(message.get("isError") for message in results), results
                assert "candidate contract passed" in results[-1]["content"], results
                return AgentStep(type="assistant", content=f"{role} candidate completed with a passing check.")
            filename = f"{role}.txt"
            content = "same" if role == "alpha" else role
            return calls(
                ("write", "write_file", {"path": filename, "content": content}),
                command("add", "git", "add", filename),
                command("commit", "git", *commit_args(role.title()), "commit", "-m", role),
                command("check", sys.executable, "-B", "check.py"),
            )

    monkeypatch.setattr("pepsicode.orchestration.interactive.MockModelAdapter", Model)

    def coordinate(**kw):
        scope = current_scope()

        def ctx():
            import uuid

            from pepsicode.tooling import ToolContext

            return ToolContext(
                str(session.workspace), permissions=permissions, execution=scope, tool_use_id=uuid.uuid4().hex
            )

        tools = kw["tools"]
        history = kw["messages"]
        events = orchestration_events(history)
        seen["events"] = events
        ready = {event.split("attempt ", 1)[1].split(",", 1)[0] for event in events if ": result_ready." in event}
        if "dispatch" not in seen:
            seen["dispatch"] = {}
            for role in ("alpha", "beta"):
                result = tools.execute(
                    "task_dispatch",
                    {"work_key": role, "goal": role, "agent_type": "general", "deliverable": "project"},
                    ctx(),
                )
                assert result.ok, result.output
                seen["dispatch"][role] = result.details
            return [*history, {"role": "assistant", "content": "Dispatched both candidates."}]
        if "integration" not in seen:
            expected = {row["attempt_id"] for row in seen["dispatch"].values()}
            if not expected.issubset(ready):
                # A sibling may still be running; events can arrive one by one.
                return [*history, {"role": "assistant", "content": "Waiting for the remaining candidate."}]
            result = tools.execute(
                "task_integrate", {"work_ids": [row["work_id"] for row in seen["dispatch"].values()]}, ctx()
            )
            assert result.ok, result.output
            seen["integration"] = result.details
            assert seen["integration"]["state"] == "acceptance_failed", seen["integration"]
            assert seen["integration"]["acceptance"]["commands"][0]["exit_code"] == 1, seen["integration"]
            delivery = tools.execute("task_deliver", {"integration_id": seen["integration"]["integration_id"]}, ctx())
            seen["delivery_refused"] = not delivery.ok
            previous = seen["dispatch"]["alpha"]["attempt_id"]
            retry = tools.execute("task_retry", {"attempt_id": previous}, ctx())
            seen["retry_refused"] = not retry.ok
            repair = tools.execute(
                "task_dispatch",
                {
                    "work_key": "repair",
                    "goal": "repair the combined contract",
                    "agent_type": "general",
                    "deliverable": "project",
                },
                ctx(),
            )
            assert repair.ok, repair.output
            seen["repair"] = repair.details
            return [*history, {"role": "assistant", "content": "Scheduled the repair attempt."}]
        if "repair_integration" not in seen and seen["repair"]["attempt_id"] in ready:
            result = tools.execute("task_integrate", {"work_ids": [seen["repair"]["work_id"]]}, ctx())
            assert result.ok, result.output
            seen["repair_integration"] = result.details
            delivery = tools.execute(
                "task_deliver", {"integration_id": seen["repair_integration"]["integration_id"]}, ctx()
            )
            assert delivery.ok, delivery.output
            seen["repair_delivery"] = delivery.details
            return [*history, {"role": "assistant", "content": "Delivered the repaired contract."}]
        return [*history, {"role": "assistant", "content": "Coordination checkpoint."}]

    error = None
    try:
        with recording_session(session):
            run_verified_turn(
                runtime={
                    "orchestration": {"enabled": True, "workerIsolation": "workspace"},
                    "verificationMode": "off",
                    "verificationCommands": [
                        {"name": "combined contract", "command": [sys.executable, "-B", "check.py"]}
                    ],
                    "maxIterations": 1,
                    "taskTimeout": 150,
                },
                runner=coordinate,
                model=Model(),
                tools=ToolRegistry([run_command_tool, write_file_tool]),
                messages=[{"role": "user", "content": "Implement the agreed candidate contract and deliver it."}],
                cwd=session.workspace,
                permissions=permissions,
                task_kind="change",
                acceptance=[{"id": "alpha", "kind": "file_exists", "path": "alpha.txt"}],
            )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    saved = sessions.load_session(session.session_id)
    assert saved is not None
    seen["error"] = error
    seen["state"] = saved.task_control["orchestration"]
    seen["task"] = saved.task_control["task"]
    seen["workspace"] = session.workspace
    return seen


def test_failed_integration_repairs_on_failed_baseline(combined_project, monkeypatch):
    seen = run_two_candidate_scenario(combined_project, monkeypatch)
    assert seen["error"] is None, seen["error"]
    # Both candidates really passed their own checks before integration.
    assert len(seen["worker_checks"]) == 2, seen["worker_checks"]
    assert all("candidate contract passed" in texts[-1] for texts in seen["worker_checks"].values())
    # The combined acceptance failed and could not be delivered.
    assert seen["integration"]["state"] == "acceptance_failed", seen["integration"]
    assert seen["delivery_refused"] and seen["retry_refused"], seen
    # The repair Worker was prepared from the failed integration baseline and
    # found both candidates' committed output in its checkout.
    assert seen["repair_head"] == seen["integration"]["head_sha"], (seen["repair_head"], seen["integration"])
    assert seen["repair_has_candidates"] == {"alpha.txt": True, "beta.txt": True}, seen["repair_has_candidates"]
    # The failed version keeps its evidence and was never delivered.
    failed = seen["state"]["integrations"][seen["integration"]["integration_id"]]
    assert failed["state"] == "acceptance_failed" and failed["acceptance"]["commands"][0]["exit_code"] == 1, failed
    assert not any(
        record["integration_id"] == failed["integration_id"] for record in seen["state"].get("deliveries", {}).values()
    ), seen["state"].get("deliveries")
    # The repair was re-integrated on top of the failed baseline and delivered.
    assert seen["repair_integration"]["state"] == "accepted", seen["repair_integration"]
    assert seen["repair_delivery"]["status"] == "local", seen["repair_delivery"]
    # Parent acceptance ran against the delivered repaired version.
    assert seen["task"]["status"] == "complete", seen["task"]
    acceptance = seen["task"]["acceptance"]
    assert acceptance["valid"] is True, acceptance
    assert acceptance["delivered"]["head_sha"] == seen["repair_integration"]["head_sha"], acceptance
    assert not (Path(seen["workspace"]) / "alpha.txt").exists()


def run_conflict_resolution_scenario(project, monkeypatch):
    """Pass condition 3: real Git merge conflict preserves candidates and resolves cleanly."""
    session, permissions = project
    seen = {"worker_checks": {}, "resolve_base_head": None}

    class Model:
        def next(self, messages):
            results = [m for m in messages if m.get("role") == "tool_result"]
            scope = current_scope()
            role = messages[1]["content"]
            if role.startswith("resolve:"):
                if not results:
                    seen["resolve_base_head"] = git(scope.cwd, "rev-parse", "HEAD")
                    alpha_branch, beta_branch = role.split(":", 1)[1].split(",")
                    return calls(
                        command("pick_alpha", "git", *commit_args("Resolve"), "cherry-pick", alpha_branch),
                        command("checkout_beta", "git", "checkout", beta_branch, "--", "beta.txt"),
                        ("write_shared", "write_file", {"path": "shared.txt", "content": "alpha\nbeta\n"}),
                        command("add", "git", "add", "alpha.txt", "beta.txt", "shared.txt"),
                        command(
                            "commit", "git", *commit_args("Resolve"), "commit", "-m", "resolve shared.txt conflict"
                        ),
                        command("check", sys.executable, "-B", "check.py"),
                    )
                assert not any(message.get("isError") for message in results), results
                assert "conflict check passed" in results[-1]["content"], results
                return AgentStep(type="assistant", content="Resolved Git conflict; checks passed.")
            if results:
                assert len(results) == 5, results
                seen["worker_checks"][role] = [message["content"] for message in results]
                assert not any(message.get("isError") for message in results), results
                assert "conflict check passed" in results[-1]["content"], results
                return AgentStep(type="assistant", content=f"{role} candidate completed with a passing check.")
            filename = f"{role}.txt"
            return calls(
                ("write_role", "write_file", {"path": filename, "content": role}),
                ("write_shared", "write_file", {"path": "shared.txt", "content": f"{role}\n"}),
                command("add", "git", "add", filename, "shared.txt"),
                command("commit", "git", *commit_args(role.title()), "commit", "-m", role),
                command("check", sys.executable, "-B", "check.py"),
            )

    monkeypatch.setattr("pepsicode.orchestration.interactive.MockModelAdapter", Model)

    def coordinate(**kw):
        scope = current_scope()

        def ctx():
            import uuid

            from pepsicode.tooling import ToolContext

            return ToolContext(
                str(session.workspace), permissions=permissions, execution=scope, tool_use_id=uuid.uuid4().hex
            )

        tools = kw["tools"]
        history = kw["messages"]
        events = orchestration_events(history)
        seen["events"] = events
        ready = {event.split("attempt ", 1)[1].split(",", 1)[0] for event in events if ": result_ready." in event}
        if "dispatch" not in seen:
            seen["dispatch"] = {}
            for role in ("alpha", "beta"):
                result = tools.execute(
                    "task_dispatch",
                    {"work_key": role, "goal": role, "agent_type": "general", "deliverable": "project"},
                    ctx(),
                )
                assert result.ok, result.output
                seen["dispatch"][role] = result.details
            return [*history, {"role": "assistant", "content": "Dispatched both candidates."}]
        if "integration" not in seen:
            expected = {row["attempt_id"] for row in seen["dispatch"].values()}
            if not expected.issubset(ready):
                return [*history, {"role": "assistant", "content": "Waiting for the remaining candidate."}]
            result = tools.execute(
                "task_integrate", {"work_ids": [row["work_id"] for row in seen["dispatch"].values()]}, ctx()
            )
            assert result.ok, result.output
            seen["integration"] = result.details
            assert seen["integration"]["state"] == "conflict", seen["integration"]
            assert seen["integration"]["conflicts"] == [
                {"work_id": seen["dispatch"]["beta"]["work_id"], "files": ["shared.txt"]}
            ], seen["integration"]
            delivery = tools.execute("task_deliver", {"integration_id": seen["integration"]["integration_id"]}, ctx())
            seen["delivery_refused"] = not delivery.ok
            resolve = tools.execute(
                "task_dispatch",
                {
                    "work_key": "resolve",
                    "deliverable": "project",
                    "goal": (
                        f"resolve:codex/m2-{seen['dispatch']['alpha']['work_id']},"
                        f"codex/m2-{seen['dispatch']['beta']['work_id']}"
                    ),
                    "agent_type": "general",
                },
                ctx(),
            )
            assert resolve.ok, resolve.output
            seen["resolve"] = resolve.details
            return [*history, {"role": "assistant", "content": "Scheduled conflict resolution."}]
        if "resolved_integration" not in seen and seen["resolve"]["attempt_id"] in ready:
            result = tools.execute("task_integrate", {"work_ids": [seen["resolve"]["work_id"]]}, ctx())
            assert result.ok, result.output
            seen["resolved_integration"] = result.details
            delivery = tools.execute(
                "task_deliver", {"integration_id": seen["resolved_integration"]["integration_id"]}, ctx()
            )
            assert delivery.ok, delivery.output
            seen["resolved_delivery"] = delivery.details
            return [*history, {"role": "assistant", "content": "Delivered resolved integration."}]
        return [*history, {"role": "assistant", "content": "Coordination checkpoint."}]

    error = None
    try:
        with recording_session(session):
            run_verified_turn(
                runtime={
                    "orchestration": {"enabled": True, "workerIsolation": "workspace"},
                    "verificationMode": "off",
                    "verificationCommands": [
                        {"name": "conflict contract", "command": [sys.executable, "-B", "check.py"]}
                    ],
                    "maxIterations": 1,
                    "taskTimeout": 150,
                },
                runner=coordinate,
                model=Model(),
                tools=ToolRegistry([run_command_tool, write_file_tool]),
                messages=[
                    {"role": "user", "content": "Implement both candidates, resolve any conflicts, and deliver."}
                ],
                cwd=session.workspace,
                permissions=permissions,
                task_kind="change",
                acceptance=[
                    {"id": "alpha", "kind": "file_exists", "path": "alpha.txt"},
                    {"id": "beta", "kind": "file_exists", "path": "beta.txt"},
                    {"id": "shared", "kind": "file_contains", "path": "shared.txt", "text": "alpha\nbeta\n"},
                ],
            )
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    saved = sessions.load_session(session.session_id)
    assert saved is not None
    seen["error"] = error
    seen["state"] = saved.task_control["orchestration"]
    seen["task"] = saved.task_control["task"]
    seen["workspace"] = session.workspace
    return seen


def test_conflicted_integration_preserves_candidates_and_delivers_resolved_version(conflict_project, monkeypatch):
    seen = run_conflict_resolution_scenario(conflict_project, monkeypatch)
    assert seen["error"] is None, seen["error"]
    assert len(seen["worker_checks"]) == 2, seen["worker_checks"]
    assert all("conflict check passed" in texts[-1] for texts in seen["worker_checks"].values())
    assert seen["integration"]["state"] == "conflict", seen["integration"]
    assert seen["delivery_refused"] is True, seen

    alpha_id = seen["dispatch"]["alpha"]["work_id"]
    beta_id = seen["dispatch"]["beta"]["work_id"]
    envs = seen["state"]["environments"]
    assert (Path(envs[alpha_id]["worktree"]) / "shared.txt").read_text(encoding="utf-8") == "alpha\n"
    assert (Path(envs[beta_id]["worktree"]) / "shared.txt").read_text(encoding="utf-8") == "beta\n"
    candidates = seen["state"]["candidates"]
    assert candidates[alpha_id]["state"] == "recorded", candidates[alpha_id]
    assert candidates[beta_id]["state"] == "recorded", candidates[beta_id]

    conflicted = seen["state"]["integrations"][seen["integration"]["integration_id"]]
    assert conflicted["state"] == "conflict", conflicted
    assert conflicted["conflicts"] == [{"work_id": beta_id, "files": ["shared.txt"]}], conflicted

    assert seen["resolved_integration"]["state"] == "accepted", seen["resolved_integration"]
    assert seen["resolved_delivery"]["status"] == "local", seen["resolved_delivery"]
    assert seen["task"]["status"] == "complete", seen["task"]
    acceptance = seen["task"]["acceptance"]
    assert acceptance["valid"] is True, acceptance
    assert acceptance["delivered"]["head_sha"] == seen["resolved_integration"]["head_sha"], acceptance
    assert not (Path(seen["workspace"]) / "shared.txt").exists()
