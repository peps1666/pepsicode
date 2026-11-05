"""Behavioral regressions from the 12fd294 harness review; no model service."""

import json
import sys

import pytest

from pepsicode.core import session as sessions
from pepsicode.core.session_tree import recording_session
from pepsicode.core.task_control import begin_task, infer_kind, snapshot, task_context
from pepsicode.core.verification import run_verified_turn
from pepsicode.execution.runtime import ExecutionScope, bind_scope
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.execution_status import execution_status_tool
from pepsicode.tools.task_state import task_state_tool


def test_uncapped_requests_can_overlap_without_oversubscribing_slots(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from pepsicode.execution.runtime import ModelRequestLimitError, RequestBudget
    from pepsicode.llm.cost_tracker import CostTracker

    budget = RequestBudget(CostTracker(), max_requests=2)
    scope = ExecutionScope(str(tmp_path), budget=budget)
    barrier = Barrier(2)

    def request(_):
        with budget.request(scope, {"model": "test"}):
            barrier.wait(timeout=2)
            budget.record("test", {"input_tokens": 1, "output_tokens": 1})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(request, range(2)))
    assert budget.requests == 2
    with pytest.raises(ModelRequestLimitError), budget.request(scope, {"model": "test"}):
        pass


def test_reconfirming_a_host_condition_preserves_its_author(tmp_path):
    scope = ExecutionScope(str(tmp_path))
    with bind_scope(scope):
        task = begin_task([{"role": "user", "content": "Implement feature"}], {})
        condition = {"id": "artifact", "kind": "file_exists", "path": "target.md"}
        scope.control["task"].update(criteria=[condition], criterion_sources={"artifact": {"source": "caller"}})
        assert (
            ToolRegistry([task_state_tool])
            .execute(
                "task_state",
                {"action": "define", "task_id": task["id"], "revision": task["revision"], "criteria": [condition]},
                ToolContext(str(tmp_path), execution=scope),
            )
            .ok
        )
        assert snapshot()["task"]["criterion_sources"]["artifact"]["source"] == "caller"


@pytest.mark.parametrize("behavior", [False, True])
def test_structured_user_requirements_have_provenance_and_cannot_be_downgraded(tmp_path, behavior):
    (tmp_path / "requested.md").write_text("artifact", encoding="utf-8")
    runtime = {"verificationMode": "off", "maxIterations": 1}

    def done(**options):
        return [*options["messages"], {"role": "assistant", "content": "Done"}]

    run_verified_turn(
        runtime=runtime,
        runner=done,
        model=object(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "按要求准备产物"}],
        cwd=str(tmp_path),
        task_requirements={"source": "user", "artifacts": ["requested.md"], "behavior": behavior},
    )
    task = runtime["_lastTask"]
    assert task["criterion_sources"]["required-0"]["source"] == "user"
    if behavior:
        assert task["status"] != "complete"
        assert runtime["_lastVerification"]["reason_code"] == "behavior_unverified"
    else:
        assert task["status"] == "complete" and task["assurance"] == "artifact_checked"


def test_readonly_looking_program_name_is_not_an_observation_grant(tmp_path):
    from pepsicode.execution.process import checked_argv

    requests = []
    permissions = PermissionManager(str(tmp_path), prompt=lambda req: requests.append(req) or {"decision": "deny_once"})
    with pytest.raises(PermissionError):
        checked_argv(
            ["echo", "could resolve through an untrusted PATH"],
            context=ToolContext(str(tmp_path), permissions=permissions),
        )
    assert len(requests) == 1


def test_later_denial_wins_over_an_old_task_grant_after_source_revert(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("original", encoding="utf-8")
    answers = iter(["allow_task", "deny_once"])
    permissions = PermissionManager(str(tmp_path), prompt=lambda _: {"decision": next(answers)})
    scope = ExecutionScope(str(tmp_path), permissions=permissions)
    with bind_scope(scope):
        begin_task([{"role": "user", "content": "Implement the feature"}], {})

        def check():
            return permissions.check_command(sys.executable, ["-m", "pytest"], str(tmp_path), "project check")

        assert check().is_allowed
        source.write_text("changed", encoding="utf-8")
        assert check().is_denied
        source.write_text("original", encoding="utf-8")
        assert check().is_denied


def test_changing_logs_does_not_count_as_task_progress(tmp_path):
    from pepsicode.core.loop_engine import VerificationResult, run_loop

    calls = []

    class Verifier:
        def verify(self, **_options):
            calls.append(True)
            return VerificationResult(
                False,
                "behavior failed",
                f"volatile log {len(calls)}",
                "failed",
                reason_code="behavior_failed",
                progress="same-requested-code-and-criteria",
            )

    def done(**options):
        return [*options["messages"], {"role": "assistant", "content": "done"}]

    result = run_loop(
        model=object(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=str(tmp_path),
        runner=done,
        verifier=Verifier(),
        max_iterations=5,
        no_progress_limit=2,
    )
    assert result.stop_reason == "no_progress" and len(calls) == 2


def test_usage_uncertainty_survives_a_new_physical_turn(tmp_path):
    from pepsicode.execution.runtime import current_scope, scoped_turn

    runtime = {"budgetLimitUsd": 1}

    @scoped_turn
    def interrupted(**options):
        current_scope().budget.unmetered = True
        return [{"role": "assistant", "content": "usage lost", "isError": True}]

    @scoped_turn
    def resumed(**options):
        with current_scope().budget.request(current_scope(), {"model": "test"}):
            pytest.fail("uncertain prior charges were ignored")

    interrupted(cwd=str(tmp_path), runtime=runtime, messages=[])
    assert runtime["_lastExecution"]["usage_uncertain"]
    with pytest.raises(RuntimeError, match="usage unavailable"):
        resumed(cwd=str(tmp_path), runtime=runtime, messages=[])


def test_reconciliation_cas_is_atomic_across_children(tmp_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    scope = ExecutionScope(str(tmp_path))
    scope.mark_unknown("one")
    scope.mark_unknown("two")
    revision = scope.recovery_version[0]
    monkeypatch.setattr(scope, "record", lambda *_args, **_kwargs: time.sleep(0.02))
    barrier = Barrier(2)

    def reconcile(call):
        barrier.wait(timeout=2)
        try:
            scope.reconcile(call, revision, {"observed": True}, "occurred")
            return True
        except PermissionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reconcile, ["one", "two"])) == 1
    assert len(scope.unresolved) == 1


def test_preparation_finishes_before_acceptance_accumulates_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "home/sessions")
    session = sessions.create_new_session(str(tmp_path))
    permissions = PermissionManager(str(tmp_path), mode="full_access")
    runtime = {
        "verificationMode": "off",
        "maxIterations": 1,
        "verificationPreparationCommands": [
            {"command": [sys.executable, "-c", "from pathlib import Path; Path('feature.py').write_text('VALUE = 42')"]}
        ],
    }

    def done(**options):
        return [*options["messages"], {"role": "assistant", "content": "Done"}]

    try:
        with recording_session(session):
            run_verified_turn(
                runtime=runtime,
                runner=done,
                model=object(),
                tools=ToolRegistry([]),
                messages=[{"role": "user", "content": "Implement feature.py"}],
                cwd=str(tmp_path),
                permissions=permissions,
                acceptance=[{"id": "artifact", "kind": "file_contains", "path": "feature.py", "text": "42"}],
            )
        assert session.task_control["task"]["status"] == "complete"
        assert session.task_control["task"]["acceptance"]["valid"]
    finally:
        permissions.close()


def test_explain_only_restriction_is_not_an_implementation_permission(tmp_path):
    from pepsicode.tools.write_file import write_file_tool

    scope = ExecutionScope(str(tmp_path))
    with bind_scope(scope):
        task = begin_task([{"role": "user", "content": "只解释如何修复，不要修改代码"}], {})
        context = ToolContext(str(tmp_path), execution=scope)
        result = ToolRegistry([write_file_tool]).execute("write_file", {"path": "changed.py", "content": "no"}, context)
        assert not result.ok and not (tmp_path / "changed.py").exists()
        assert (
            not ToolRegistry([task_state_tool])
            .execute(
                "task_state",
                {
                    "action": "classify",
                    "kind": "change",
                    "reason": "want to implement",
                    "task_id": task["id"],
                    "revision": task["revision"],
                },
                context,
            )
            .ok
        )


def test_recovery_file_observation_needs_no_process_or_extra_approval(tmp_path):
    from pepsicode.execution.process import checked_argv
    from pepsicode.tools.read_file import read_file_tool

    (tmp_path / "value.txt").write_text("observed", encoding="utf-8")
    requests = []
    permissions = PermissionManager(str(tmp_path), prompt=lambda req: requests.append(req) or {"decision": "deny_once"})
    scope = ExecutionScope(str(tmp_path), permissions=permissions)
    scope.mark_unknown("lost")
    context = ToolContext(str(tmp_path), permissions=permissions, execution=scope)
    with bind_scope(scope):
        for _ in range(3):
            assert ToolRegistry([read_file_tool]).execute("read_file", {"path": "value.txt"}, context).ok
        assert not requests and scope.has_unknown
        with pytest.raises(PermissionError):
            checked_argv([sys.executable, "-c", "print('read only')"], context=context)


def test_bounded_truncation_recovery_discards_partial_tools(tmp_path, monkeypatch):
    from pepsicode.core.agent_loop import run_agent_turn
    from pepsicode.core.verification import run_verified_turn
    from pepsicode.execution.runtime import RequestBudget
    from pepsicode.llm.cost_tracker import CostTracker
    from pepsicode.types import AgentStep, StepDiagnostics

    calls = []

    class Model:
        def next(self, messages):
            calls.append(messages)
            if len(calls) == 1:
                return AgentStep(
                    type="assistant", content="partial", diagnostics=StepDiagnostics(stopReason="max_tokens")
                )
            return AgentStep(type="assistant", content="complete answer")

    scope = ExecutionScope(str(tmp_path), budget=RequestBudget(CostTracker()))
    with bind_scope(scope):
        messages = run_verified_turn(
            runtime={},
            runner=run_agent_turn,
            model=Model(),
            tools=ToolRegistry([]),
            messages=[{"role": "user", "content": "Explain the algorithm"}],
            cwd=str(tmp_path),
        )
        assert len(calls) == 2 and not messages[-1].get("isError")
        assert snapshot()["task"]["status"] == "answered"
        recovery = next(m["content"] for m in messages if m.get("internal") and "bounded recovery" in m["content"])
        assert "smaller next step" in recovery and "small, complete edits" in recovery


@pytest.mark.parametrize("prompt", ["帮我支持 CSV 导出", "请把首页做成深色", "先解释原因再修改首页"])
def test_colloquial_actions_are_not_answers(prompt):
    assert infer_kind(prompt) == "change"


@pytest.mark.parametrize("prompt", ["解释怎么修复这个问题", "分析最近更新", "解释 CSV 导出的原理"])
def test_analysis_mentions_are_answers(prompt):
    assert infer_kind(prompt) == "answer"


def execute(tmp_path, monkeypatch, objective, criteria):
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path / "home")
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "home/sessions")
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.py").write_text("VALUE = 42\n", encoding="utf-8")
    (project / "README.md").write_text("existing unrelated readme", encoding="utf-8")
    session = sessions.create_new_session(str(project))
    permissions = PermissionManager(str(project), mode="full_access")
    runtime = {"maxIterations": 1, "verificationMode": "off"}

    def done(**options):
        return [*options["messages"], {"role": "assistant", "content": "Done"}]

    try:
        with recording_session(session):
            run_verified_turn(
                runtime=runtime,
                runner=done,
                model=object(),
                tools=ToolRegistry([]),
                messages=[{"role": "user", "content": objective}],
                cwd=str(project),
                permissions=permissions,
                acceptance=criteria,
            )
    finally:
        permissions.close()
    return session.task_control["task"], runtime


def test_later_command_cannot_keep_a_previous_green_command(tmp_path, monkeypatch):
    checks = [
        {
            "id": "behavior",
            "kind": "command",
            "argv": [sys.executable, "-c", "from value import VALUE; assert VALUE == 42"],
        },
        {
            "id": "mutation",
            "kind": "command",
            "argv": [sys.executable, "-c", "from pathlib import Path; Path('value.py').write_text('VALUE = 0\\n')"],
        },
    ]
    task, runtime = execute(tmp_path, monkeypatch, "Implement value.py", checks)
    assert task["status"] != "complete"
    assert runtime["_lastVerification"]["reason_code"] == "verification_state_changed"


def test_unrelated_readme_cannot_accept_an_explicit_missing_artifact(tmp_path, monkeypatch):
    task, _ = execute(
        tmp_path,
        monkeypatch,
        "Implement required_feature.py",
        [{"id": "unrelated", "kind": "file_exists", "path": "README.md"}],
    )
    assert task["status"] != "complete"


def test_recovery_is_per_call_and_rejects_forged_or_stale_observations(tmp_path):
    (tmp_path / "record").write_text("committed", encoding="utf-8")
    permissions = PermissionManager(str(tmp_path), mode="full_access")
    scope = ExecutionScope(str(tmp_path), permissions=permissions)
    scope.mark_unknown("first")
    scope.mark_unknown("second")
    context = ToolContext(str(tmp_path), execution=scope, permissions=permissions)
    tools = ToolRegistry([execution_status_tool])
    with bind_scope(scope):
        observation = json.loads(
            tools.execute("execution_status", {"action": "observe", "path": "record"}, context).output
        )
        args = {
            "action": "reconcile",
            "call_id": "first",
            "revision": observation["recovery_revision"],
            "observation_ids": ["forged"],
            "outcome": "occurred",
            "evidence": "record exists",
        }
        assert not tools.execute("execution_status", args, context).ok
        args["observation_ids"] = [observation["id"]]
        assert tools.execute("execution_status", args, context).ok
        assert set(scope.unresolved) == {"second"}
        assert not tools.execute("execution_status", {**args, "call_id": "second"}, context).ok
        assert scope.has_unknown


def test_task_command_grants_are_visible_and_bound_to_input_contents(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1", encoding="utf-8")
    prompts = []
    permissions = PermissionManager(
        str(tmp_path), prompt=lambda request: prompts.append(request) or {"decision": "allow_task"}
    )
    scope = ExecutionScope(str(tmp_path), permissions=permissions)
    with bind_scope(scope):
        begin_task([{"role": "user", "content": "Implement feature"}], {})

        def check():
            return permissions.check_command(sys.executable, ["-m", "pytest"], str(tmp_path), "execute project checks")

        assert check().is_allowed and check().is_allowed
        assert len(prompts) == 1 and any(c["decision"] == "allow_task" for c in prompts[0]["choices"])
        source.write_text("VALUE = 2", encoding="utf-8")
        assert check().is_allowed and len(prompts) == 2
        permissions.set_access_mode("read_only")
        assert not check().is_allowed
        permissions.set_access_mode("auto")
        assert check().is_allowed and len(prompts) == 3
        permissions.clear_task_grants()
        assert check().is_allowed and len(prompts) == 4
        assert permissions.approval_metrics["requests"] == 4


def test_answer_classification_can_be_corrected_during_first_normal_request(tmp_path):
    scope = ExecutionScope(str(tmp_path))
    with bind_scope(scope):
        task = begin_task([{"role": "user", "content": "support the export"}], {})
        assert task_context() is not None
        tools = ToolRegistry([task_state_tool])
        result = tools.execute(
            "task_state",
            {
                "action": "classify",
                "task_id": task["id"],
                "revision": task["revision"],
                "kind": "answer",
                "reason": "actually only explaining",
            },
            ToolContext(str(tmp_path), execution=scope),
        )
        assert not result.ok  # An explicit action is locked.
        task = begin_task([{"role": "user", "content": "CSV export please"}], {})
        assert tools.execute(
            "task_state",
            {
                "action": "classify",
                "task_id": task["id"],
                "revision": task["revision"],
                "kind": "change",
                "reason": "user requests implementation",
            },
            ToolContext(str(tmp_path), execution=scope),
        ).ok
        assert snapshot()["task"]["kind"] == "change"


def test_content_fingerprint_tracks_sources_tests_config_additions_and_deletions(tmp_path):
    import os

    from pepsicode.core.workspace_state import workspace_state

    for name in ("source.py", "test_public.py", "pyproject.toml"):
        path = tmp_path / name
        path.write_text("AAAA", encoding="utf-8")
        before = workspace_state(tmp_path)
        stamp = path.stat()
        path.write_text("BBBB", encoding="utf-8")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert workspace_state(tmp_path)["digest"] != before["digest"]
        before = workspace_state(tmp_path)
        path.unlink()
        assert workspace_state(tmp_path)["digest"] != before["digest"]
    before = workspace_state(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "generated.pyc").write_bytes(b"cache")
    assert workspace_state(tmp_path)["digest"] == before["digest"]


def test_generated_output_cannot_hide_an_explicitly_requested_artifact(tmp_path):
    from pepsicode.core.workspace_state import workspace_state

    output = tmp_path / "release/result.txt"
    output.parent.mkdir()
    output.write_text("first", encoding="utf-8")
    before = workspace_state(tmp_path, include_paths=["release/result.txt"])
    output.write_text("other", encoding="utf-8")
    assert workspace_state(tmp_path, include_paths=["release/result.txt"])["digest"] != before["digest"]


def test_continuation_retains_identity_and_cannot_discard_previous_conditions(tmp_path):
    scope = ExecutionScope(str(tmp_path))
    with bind_scope(scope):
        task = begin_task([{"role": "user", "content": "Implement required.py"}], {})
        task["status"] = "incomplete"
        scope.control["task"]["status"] = "incomplete"
        resumed = begin_task([{"role": "user", "content": "继续处理当前任务中的失败步骤：检查状态后继续"}], {})
        assert resumed["id"] == task["id"]
        assert (
            not ToolRegistry([task_state_tool])
            .execute(
                "task_state",
                {
                    "action": "revise",
                    "task_id": resumed["id"],
                    "revision": resumed["revision"],
                    "instruction_id": resumed["inputs"][-1],
                    "reason": "try to discard",
                    "criteria": [],
                },
                ToolContext(str(tmp_path), execution=scope),
            )
            .ok
        )
