"""Reproductions and acceptance checks from docs/project-issues.md (af533e9)."""

import sys
from pathlib import Path

import pytest

from pepsicode.core import session as sessions
from pepsicode.core.input_queue import QueuedInput, TurnInputQueue
from pepsicode.core.session_tree import recording_session
from pepsicode.core.task_control import admit_steering, begin_task, snapshot
from pepsicode.core.verification import run_verified_turn
from pepsicode.execution.runtime import ExecutionScope, bind_scope
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.task_state import task_state_tool


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "home/sessions")
    root = tmp_path / "project"
    root.mkdir()
    return sessions.create_new_session(str(root))


def finish(project, objective, criteria, *, prepare=None, requirements=None):
    runtime = {"maxIterations": 1, "verificationMode": "off"}
    permissions = PermissionManager(project.workspace, mode="full_access")
    try:
        with recording_session(project):
            run_verified_turn(
                runtime=runtime,
                runner=prepare or (lambda **o: [*o["messages"], {"role": "assistant", "content": "Done"}]),
                model=object(),
                tools=ToolRegistry([]),
                messages=[{"role": "user", "content": objective}],
                cwd=project.workspace,
                permissions=permissions,
                acceptance=criteria,
                task_requirements=requirements,
            )
    finally:
        permissions.close()
    return project.task_control["task"], runtime


@pytest.mark.parametrize("directory", ["build", "dist", "release"])
def test_issue001_later_command_invalidates_generated_code_evidence(project, directory):
    root = Path(project.workspace)
    (root / directory).mkdir()
    path = f"{directory}/value.py"
    (root / path).write_text("VALUE = 42\n", encoding="utf-8")
    task, runtime = finish(
        project,
        "Implement the calculation behavior",
        [
            {
                "id": "behavior",
                "kind": "command",
                "argv": [sys.executable, "-c", f"import runpy; assert runpy.run_path('{path}')['VALUE'] == 42"],
            },
            {
                "id": "later",
                "kind": "command",
                "argv": [sys.executable, "-c", f"from pathlib import Path; Path('{path}').write_text('VALUE = 0\\n')"],
            },
        ],
    )
    assert task["status"] != "complete"
    assert runtime["_lastVerification"]["reason_code"] == "verification_state_changed"


def test_issue002_migration_does_not_require_deleted_input(project):
    root = Path(project.workspace)
    (root / "old.py").write_text("VALUE = 42\n", encoding="utf-8")

    def migrate(**options):
        (root / "old.py").rename(root / "new.py")
        return [*options["messages"], {"role": "assistant", "content": "Migrated"}]

    task, _ = finish(
        project,
        "Implement the migration from old.py to new.py; remove old.py.",
        [
            {
                "id": "migration",
                "kind": "command",
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('new.py').exists() and not Path('old.py').exists()",
                ],
            }
        ],
        prepare=migrate,
    )
    assert task["status"] == "complete"


def test_issue002_explicit_deletion_is_actionable_and_accepts_absence(project):
    root = Path(project.workspace)
    (root / "old.py").write_text("obsolete", encoding="utf-8")

    def remove(**options):
        task = snapshot()["task"]
        assert task["classification"]["locked"]
        (root / "old.py").unlink()
        return [*options["messages"], {"role": "assistant", "content": "Removed"}]

    task, _ = finish(
        project, "Delete old.py", [{"id": "removed", "kind": "file_absent", "path": "old.py"}], prepare=remove
    )
    assert task["status"] == "complete"


def test_issue003_delivered_human_action_replaces_explain_only(project):
    scope = ExecutionScope(project.workspace)
    with recording_session(project), bind_scope(scope):
        begin_task([{"role": "user", "content": "只解释如何修改首页，不要修改代码"}], {})
        admit_steering([QueuedInput("actual-human", "现在修改首页，添加深色主题", "steer")])
        task = snapshot()["task"]
        assert task["kind"] == task["classification"]["kind"] == "change"
        assert not task["classification"]["read_only_request"]
        result = ToolRegistry([task_state_tool]).execute(
            "task_state",
            {"action": "define", "task_id": task["id"], "revision": task["revision"], "kind": "change"},
            ToolContext(project.workspace, execution=scope),
        )
        assert result.ok, result.output
        from pepsicode.tools.write_file import write_file_tool

        permissions = PermissionManager(project.workspace, mode="full_access")
        try:
            changed = ToolRegistry([write_file_tool]).execute(
                "write_file",
                {"path": "page.txt", "content": "dark"},
                ToolContext(project.workspace, permissions=permissions, execution=scope),
            )
            assert changed.ok, changed.output
            assert (Path(project.workspace) / "page.txt").read_text(encoding="utf-8") == "dark"
        finally:
            permissions.close()


def test_issue003_narrowing_blocks_at_acceptance_but_widening_waits_for_delivery(project):
    with recording_session(project):
        begin_task([{"role": "user", "content": "修改首页"}], {})
        queue = TurnInputQueue()
        queue.bind_session(project)
        queue.start()
        stop = queue.enqueue("停止修改，只分析首页")
        assert snapshot()["task"]["classification"]["read_only_request"]
        admit_steering(queue.take_steering())
        queue.acknowledge([stop])
        later = queue.enqueue("现在修改首页，添加深色主题")
        assert snapshot()["task"]["classification"]["read_only_request"]
        admit_steering(queue.take_steering())
        assert not snapshot()["task"]["classification"]["read_only_request"]
        queue.acknowledge([later])
        queue.close()


def test_issue001_explicit_directory_overrides_cache_exclusions_and_tracks_missing(tmp_path):
    import os

    from pepsicode.core.workspace_state import workspace_state

    cached = tmp_path / "node_modules/example"
    cached.mkdir(parents=True)
    file = cached / "value.js"
    file.write_text("AAAA", encoding="utf-8")
    ordinary = workspace_state(tmp_path)
    before = workspace_state(tmp_path, include_paths=["node_modules/example", "missing/tree"])
    stamp = file.stat()
    file.write_text("BBBB", encoding="utf-8")
    os.utime(file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert workspace_state(tmp_path)["digest"] == ordinary["digest"]
    assert (
        workspace_state(tmp_path, include_paths=["node_modules/example", "missing/tree"])["digest"] != before["digest"]
    )
    file.unlink()
    after = workspace_state(tmp_path, include_paths=["node_modules/example", "missing/tree"])
    assert "node_modules/example/value.js" not in after["entries"]
    assert after["entries"]["missing/tree"] == "missing"
    assert after["policy"]["explicit_inputs"] == ["missing/tree", "node_modules/example"]


def test_issue001_normal_cache_activity_does_not_invalidate_acceptance(project):
    task, _ = finish(
        project,
        "Implement calculation behavior",
        [
            {
                "id": "cache",
                "kind": "command",
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; p=Path('__pycache__'); p.mkdir(exist_ok=True); (p/'test.pyc').write_bytes(b'cache')",
                ],
            }
        ],
    )
    assert task["status"] == "complete"
    assert task["acceptance"]["workspace"]["policy_digest"]


@pytest.mark.parametrize("mutate", [False, True])
def test_issue002_absent_and_unchanged_conditions_use_pre_execution_baselines(project, mutate):
    root = Path(project.workspace)
    (root / "protected.txt").write_text("original", encoding="utf-8")

    def work(**options):
        if mutate:
            (root / "protected.txt").write_text("changed", encoding="utf-8")
        return [*options["messages"], {"role": "assistant", "content": "Done"}]

    task, _ = finish(
        project,
        "Implement cleanup; do not modify protected.txt",
        [
            {"id": "gone", "kind": "file_absent", "path": "old.py"},
            {"id": "kept", "kind": "file_unchanged", "path": "protected.txt"},
        ],
        prepare=work,
    )
    assert (task["status"] == "complete") is not mutate


def test_issue002_reference_and_protected_mentions_are_not_new_outputs():
    from pepsicode.core.file_contract import artifact_requirements, file_mentions

    text = (
        "Implement the migration from old.py to new.py; remove old.py; do not modify protected.py; reference example.py"
    )
    assert {item["path"]: item["role"] for item in file_mentions(text)} == {
        "old.py": "delete_target",
        "new.py": "output",
        "protected.py": "protected",
        "example.py": "reference",
    }
    assert [item["path"] for item in artifact_requirements(text)] == ["new.py"]
    assert not artifact_requirements("Implement the change; do not create counterexample.py")
    assert not artifact_requirements("实现功能；不要生成 example.md")


def test_issue002_inference_correction_is_audited_but_explicit_constraints_are_protected(project):
    scope = ExecutionScope(project.workspace)
    context = ToolContext(project.workspace, execution=scope)
    tools = ToolRegistry([task_state_tool])
    with recording_session(project), bind_scope(scope):
        task = begin_task([{"role": "user", "content": "Implement old.py"}], {})
        request = {
            "action": "correct_files",
            "task_id": task["id"],
            "revision": task["revision"],
            "reason": "The mentioned path is the migration input",
            "file_roles": [{"path": "old.py", "role": "input"}],
        }
        assert tools.execute("task_state", request, context).ok
        current = snapshot()["task"]
        assert current["required_artifacts"] == []
        assert current["file_role_changes"][0]["before"]["role"] == "output"
        from pepsicode.core.task_control import edit_control

        with edit_control() as control:
            control["task"]["required_artifacts"] = [{"path": "old.py", "source": "user"}]
        result = tools.execute("task_state", {**request, "revision": current["revision"]}, context)
        assert not result.ok
        assert snapshot()["task"]["required_artifacts"][0]["source"] == "user"


def test_issue002_legacy_inference_migrates_without_dropping_explicit_constraints(project):
    from pepsicode.core.file_contract import migrate_file_contract

    with recording_session(project):
        task = begin_task([{"role": "user", "content": "Implement legacy.py"}], {})
    task.pop("file_contract_version")
    task["required_artifacts"].append({"path": "required.py", "source": "caller"})
    assert migrate_file_contract(task)
    assert task["required_artifacts"] == [{"path": "required.py", "source": "caller"}]
    assert task["file_mentions"][0]["confirmed"] is False
    assert not migrate_file_contract(task)


@pytest.mark.parametrize("mode", ["read_only", "plan"])
def test_issue003_new_intent_never_changes_permission_mode(project, mode):
    from pepsicode.tools.write_file import write_file_tool

    permissions = PermissionManager(project.workspace, mode=mode)
    scope = ExecutionScope(project.workspace, permissions=permissions)
    try:
        with recording_session(project), bind_scope(scope):
            begin_task([{"role": "user", "content": "只解释，不要修改代码"}], {})
            item = QueuedInput("change", "现在修改首页，添加深色主题", "steer")
            admit_steering([item])
            revision = snapshot()["task"]["revision"]
            admit_steering([item])
            assert snapshot()["task"]["revision"] == revision
            result = ToolRegistry([write_file_tool]).execute(
                "write_file",
                {"path": "page.txt", "content": "dark"},
                ToolContext(project.workspace, permissions=permissions, execution=scope),
            )
            assert not result.ok
            assert not (Path(project.workspace) / "page.txt").exists()
    finally:
        permissions.close()


def test_issue003_unrelated_supplement_and_returned_input_do_not_widen_intent(project):
    with recording_session(project):
        begin_task([{"role": "user", "content": "只解释，不要修改代码"}], {})
        admit_steering([QueuedInput("supplement", "背景：目前用户很多", "steer")])
        assert snapshot()["task"]["classification"]["read_only_request"]
        queue = TurnInputQueue()
        queue.bind_session(project)
        queue.start()
        action = queue.enqueue("现在修改首页")
        assert queue.remove(action.id)
        assert snapshot()["task"]["classification"]["read_only_request"]
        assert snapshot()["task"]["admitted_revision"] == snapshot()["task"]["revision"]
        queue.close()


def test_issue003_reference_supplement_does_not_discard_a_requested_output(project):
    with recording_session(project):
        begin_task([{"role": "user", "content": "Create requested.md"}], {})
        admit_steering([QueuedInput("extra", "Background information about requested.md", "steer")])
        assert snapshot()["task"]["required_artifacts"][0]["path"] == "requested.md"


def test_issue003_supplement_does_not_undo_a_host_effect_contract_upgrade(project):
    from pepsicode.tools.write_file import write_file_tool

    permissions = PermissionManager(project.workspace, mode="full_access")
    scope = ExecutionScope(project.workspace, permissions=permissions)
    try:
        with recording_session(project), bind_scope(scope):
            begin_task([{"role": "user", "content": "Tell me about the homepage"}], {})
            assert (
                ToolRegistry([write_file_tool])
                .execute(
                    "write_file",
                    {"path": "page.txt", "content": "changed"},
                    ToolContext(project.workspace, permissions=permissions, execution=scope),
                )
                .ok
            )
            admit_steering([QueuedInput("supplement", "Background information about the homepage", "steer")])
            task = snapshot()["task"]
            assert task["kind"] == task["classification"]["kind"] == "change"
            assert task["classification"]["locked"]
    finally:
        permissions.close()


def test_issue003_failed_delivery_rolls_back_messages_tree_and_intent(project, monkeypatch):
    import copy

    from pepsicode.core.agent_loop import append_queued_inputs

    with recording_session(project):
        begin_task([{"role": "user", "content": "只解释，不要修改代码"}], {})
        before = snapshot()
        tree = copy.deepcopy(project.tree)
        messages = [{"role": "user", "content": "original"}]
        monkeypatch.setattr(sessions, "save_session", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError, match="disk full"):
            append_queued_inputs(messages, [QueuedInput("actual", "现在修改首页", "steer")], cwd=project.workspace)
        assert messages == [{"role": "user", "content": "original"}]
        assert snapshot() == before
        assert project.tree == tree


def test_issue003_narrowed_task_can_answer_without_running_old_checks(project):
    def explain(**options):
        admit_steering([QueuedInput("stop", "停止修改，只分析首页", "steer")])
        return [*options["messages"], {"role": "assistant", "content": "Here is the analysis"}]

    task, runtime = finish(
        project,
        "Implement the homepage",
        [{"id": "old-action", "kind": "command", "argv": [sys.executable, "-c", "raise RuntimeError('must not run')"]}],
        prepare=explain,
    )
    assert task["status"] == "answered"
    assert task["assurance"] == "explanation_only"
    assert not task.get("acceptance")
    assert runtime["_lastVerification"]["reason_code"] == "explanation_only"


def test_issue009_snapshot_metrics_and_limits_are_explicit(tmp_path, monkeypatch):
    import pepsicode.core.workspace_state as states

    (tmp_path / "source.py").write_bytes(b"data")
    scope = ExecutionScope(str(tmp_path))
    with bind_scope(scope):
        states.workspace_state(tmp_path, reason="acceptance_start")
        states.workspace_state(tmp_path, reason="acceptance_final")
        metrics = scope.snapshot()["workspace_scans"]
        assert metrics["calls"] == 2 and metrics["bytes"] == 8
        assert [item["reason"] for item in metrics["recent"]] == ["acceptance_start", "acceptance_final"]
        monkeypatch.setattr(states, "MAX_BYTES", 3)
        with pytest.raises(ValueError, match="select the relevant project/package"):
            states.workspace_state(tmp_path)
