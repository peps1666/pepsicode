import threading
from types import SimpleNamespace

import pytest

from pepsicode.agents.loader import AgentLoader
from pepsicode.core.agent_loop import _execute_calls_in_order
from pepsicode.core.session_tree import current_session, recording_session
from pepsicode.execution.runtime import ExecutionScope, bind_scope
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.task import create_task_tool


def test_child_revision_change_blocks_further_execution(tmp_path):
    control = {"task": {"id": "t", "revision": 1, "kind": "change", "evidence": []}}
    parent = ExecutionScope(str(tmp_path), control=control)
    with bind_scope(parent):
        child = parent.child()
    assert child.control["task"]["revision"] == 1
    child.control["task"]["revision"] = 99
    assert control["task"]["revision"] == 1
    control["task"]["revision"] = 2
    with pytest.raises(PermissionError, match="revision changed"):
        child.check_cancelled()


def test_child_write_after_parent_revision_is_blocked(tmp_path):
    control = {"task": {"id": "t", "revision": 1, "kind": "change", "evidence": []}}
    parent = ExecutionScope(str(tmp_path), control=control)
    with bind_scope(parent):
        child = parent.child()
    called = []
    tool = ToolDefinition("write", "test", {}, lambda value: value, lambda value, context: called.append(1))
    control["task"]["revision"] = 2
    result = ToolRegistry([tool]).execute("write", {}, ToolContext(str(tmp_path), execution=child))
    assert not result.ok
    assert not called


@pytest.mark.parametrize(
    "frontmatter",
    [
        'isReadOnly: "false"\nallowedTools:\n  - read_file',
        "maxTurns: abc\nallowedTools:\n  - read_file",
        "allowedTools:\n  - typo_tool",
        "name: custom",
    ],
)
def test_invalid_custom_definition_is_rejected(tmp_path, frontmatter):
    folder = tmp_path / ".pepsi-code" / "agents"
    folder.mkdir(parents=True)
    (folder / "custom.md").write_text(f"---\n{frontmatter}\n---\nPrompt", encoding="utf-8")
    with pytest.raises(ValueError):
        AgentLoader(tmp_path).get("custom")


def test_parallel_workers_inherit_context_and_join_in_order(tmp_path):
    session = SimpleNamespace(task_control={}, _tree_lock=threading.RLock())
    barrier = threading.Barrier(3, timeout=3)

    def run(value, _context):
        assert current_session() is session
        barrier.wait()
        return ToolResult(True, str(value["n"]))

    tool = ToolDefinition("observe", "test", {}, lambda x: x, run, concurrency_safe=True)
    calls = [{"id": str(n), "toolName": "observe", "input": {"n": n}} for n in range(3)]
    with recording_session(session):
        results = _execute_calls_in_order(calls, ToolRegistry([tool]), ToolContext(str(tmp_path)), None, None)
    assert [item.output for item in results] == ["0", "1", "2"]


def test_only_builtin_readonly_tasks_are_concurrent(tmp_path):
    tool = create_task_tool(str(tmp_path), None)
    assert tool.concurrency_safe(tool.validator({"agent_type": "explore", "task": "x"}))
    assert not tool.concurrency_safe(tool.validator({"agent_type": "general", "task": "x"}))
    assert not tool.concurrency_safe(tool.validator({"agent_type": "verification", "task": "x"}))
    assert not tool.modifies_workspace(tool.validator({"agent_type": "plan", "task": "x"}))


def test_custom_readonly_review_is_allowed_in_plan_mode(tmp_path):
    folder = tmp_path / ".pepsi-code" / "agents"
    folder.mkdir(parents=True)
    (folder / "review.md").write_text(
        "---\nisReadOnly: true\nallowedTools:\n  - read_file\n---\nReview the code.", encoding="utf-8"
    )
    tool = create_task_tool(str(tmp_path), None)
    parsed = tool.validator({"agent_type": "review", "task": "inspect"})
    permissions = PermissionManager(str(tmp_path))
    permissions.enter_plan_mode()
    permissions.ensure_tool_allowed(tool, parsed)
    assert not tool.concurrency_safe(parsed)


def test_explore_tasks_execute_concurrently_and_join(tmp_path):
    from pepsicode.types import AgentStep

    barrier = threading.Barrier(3, timeout=5)

    class Model:
        def next(self, messages):
            barrier.wait()
            return AgentStep(type="assistant", content=next(m["content"] for m in messages if m.get("role") == "user"))

    tool = create_task_tool(str(tmp_path), None, model_factory=lambda registry: Model())
    calls = [
        {"id": str(n), "toolName": "task", "input": {"agent_type": "explore", "task": f"item {n}"}} for n in range(3)
    ]
    results = _execute_calls_in_order(calls, ToolRegistry([tool]), ToolContext(str(tmp_path)), None, None)
    assert all(result.ok for result in results)
    assert all(f"item {n}" in result.output for n, result in enumerate(results))


def test_child_request_quota_shares_parent_budget(tmp_path):
    from pepsicode.execution.runtime import ModelRequestLimitError, RequestBudget
    from pepsicode.llm.cost_tracker import CostTracker

    budget = RequestBudget(CostTracker(), max_requests=4)
    child = ExecutionScope(str(tmp_path), budget=budget, parent_task_id="parent", request_limit=1)
    with budget.request(child, {}):
        pass
    with pytest.raises(ModelRequestLimitError, match="Sub-agent"), budget.request(child, {}):
        pass
    assert budget.requests == 1
    assert child.requests == 1


def test_child_approval_identifies_execution(tmp_path):
    from pepsicode.approval.types import ApprovalDecision, ApprovalKind, ApprovalRequest

    seen = []
    permissions = PermissionManager(
        str(tmp_path), prompt=lambda payload: seen.append(payload) or {"decision": "deny_once"}
    )
    scope = ExecutionScope(str(tmp_path), parent_task_id="parent", agent_scope="subagent:General")
    request = ApprovalRequest(ApprovalKind.EDIT, "edit", [], "file", [])
    with bind_scope(scope):
        outcome = permissions._request_approval(request)
    assert outcome.decision == ApprovalDecision.DENY_ONCE
    assert seen[0]["metadata"]["execution_id"] == scope.task_id
    assert "subagent:General" in seen[0]["details"][0]


def test_approval_prompts_are_serialized(tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from pepsicode.approval.types import ApprovalKind, ApprovalRequest

    state = {"active": 0, "max": 0}
    guard = threading.Lock()

    def prompt(payload):
        with guard:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.03)
        with guard:
            state["active"] -= 1
        return {"decision": "deny_once"}

    permissions = PermissionManager(str(tmp_path), prompt=prompt)
    request = ApprovalRequest(ApprovalKind.EDIT, "edit", [], "file", [])
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(permissions._request_approval, [request] * 3))
    assert len(results) == 3
    assert state["max"] == 1


def test_subagent_result_has_structured_host_details(tmp_path):
    from pepsicode.types import AgentStep

    class Model:
        def next(self, messages):
            return AgentStep(type="assistant", content="Done")

    tool = create_task_tool(str(tmp_path), None, model_factory=lambda registry: Model())
    events = []
    calls = [{"id": "1", "toolName": "task", "input": {"agent_type": "explore", "task": "inspect"}}]
    results = _execute_calls_in_order(
        calls,
        ToolRegistry([tool]),
        ToolContext(str(tmp_path)),
        None,
        None,
        on_tool_event=lambda name, data: events.append((name, data)),
    )
    assert results[0].details["scope_id"]
    assert results[0].details["report_is_acceptance_evidence"] is False
    event = next(data for name, data in events if name == "tool/result")
    assert event["details"] == results[0].details


def test_child_cannot_obtain_tools_missing_from_parent_registry(tmp_path):
    from pepsicode.types import AgentStep

    available = []

    class Model:
        def next(self, messages):
            return AgentStep(type="assistant", content="Done")

    def factory(registry):
        available.extend(tool.name for tool in registry.list())
        return Model()

    task = create_task_tool(str(tmp_path), None, model_factory=factory)
    result = ToolRegistry([task]).execute(
        "task", {"agent_type": "general", "task": "inspect"}, ToolContext(str(tmp_path))
    )
    assert result.ok
    assert available == []
