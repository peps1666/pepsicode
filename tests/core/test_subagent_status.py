import pytest

from pepsicode.agents.trace import TraceManager
from pepsicode.tooling import ToolContext
from pepsicode.tools.task import create_task_tool


@pytest.mark.parametrize(
    ("turn_status", "expected"),
    [
        ("completed", "completed"),
        ("cancelled", "cancelled"),
        ("timed_out", "timed_out"),
        ("max_steps", "failed"),
        ("unknown", "blocked"),
    ],
)
def test_child_status_matches_result_and_trace(monkeypatch, tmp_path, turn_status, expected):
    from pepsicode.core import agent_loop

    monkeypatch.setattr(
        agent_loop,
        "run_agent_turn",
        lambda **kwargs: [{"role": "assistant", "content": "result", "turnStatus": turn_status}],
    )
    trace = TraceManager()
    tool = create_task_tool(str(tmp_path), None, model_factory=lambda registry: object(), trace_manager=trace)
    result = tool.run({"agent_type": "explore", "task": "inspect"}, ToolContext(cwd=str(tmp_path)))
    assert result.status == expected
    assert result.ok is (expected == "completed")
    assert f"Explore {expected}" in result.output
    assert trace.all_nodes()[0].status == expected


def test_model_setup_failure_does_not_report_unknown(tmp_path):
    def fail(_registry):
        raise RuntimeError("model setup failed")

    tool = create_task_tool(str(tmp_path), None, model_factory=fail)
    result = tool.run({"agent_type": "general", "task": "work"}, ToolContext(cwd=str(tmp_path)))
    assert not result.ok
    assert result.status == "failed"


def test_cancelled_turn_records_cancelled_status():
    from pepsicode.core.agent_loop import _finish_cancelled_turn

    messages = _finish_cancelled_turn([], None, ".", None, "subagent:Explore", None)
    assert messages[-1]["turnStatus"] == "cancelled"


def test_unknown_child_operation_is_not_duplicated(monkeypatch, tmp_path):
    from pepsicode.core import agent_loop
    from pepsicode.execution.runtime import ExecutionScope, bind_scope, current_scope
    from pepsicode.tooling import ToolRegistry

    def run(**kwargs):
        current_scope().mark_unknown("child-call", tool="run_command")
        return [{"role": "assistant", "content": "Inspect child-call", "turnStatus": "unknown"}]

    monkeypatch.setattr(agent_loop, "run_agent_turn", run)
    tool = create_task_tool(str(tmp_path), None, model_factory=lambda registry: object())
    scope = ExecutionScope(str(tmp_path))
    with bind_scope(scope):
        result = ToolRegistry([tool]).execute(
            "task", {"agent_type": "general", "task": "work"}, ToolContext(str(tmp_path), tool_use_id="parent-call")
        )
    assert result.status == "blocked"
    assert list(scope.unresolved) == ["child-call"]
