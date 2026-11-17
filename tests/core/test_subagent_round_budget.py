import pytest

from pepsicode.agents.status import trace_view
from pepsicode.agents.trace import TraceManager
from pepsicode.core.agent_loop import run_agent_turn, run_agent_turn_stream
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.task import create_task_tool
from pepsicode.types import AgentStep, ModelAdapter, StreamToken


def test_last_round_report_is_preserved_when_worker_does_not_finish(tmp_path):
    requests = []

    class Model:
        def next(self, messages):
            requests.append(list(messages))
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": str(len(requests)),
                        "toolName": "agent_report",
                        "input": {"status": "ready", "summary": "Found the frontend state entry point"},
                    }
                ],
            )

    manager = TraceManager()
    tool = create_task_tool(str(tmp_path), None, model_factory=lambda _: Model(), trace_manager=manager)
    result = tool.run({"agent_type": "explore", "task": "Inspect"}, ToolContext(str(tmp_path)))
    assert len(requests) == 5
    assert not result.ok
    assert result.status == "blocked"
    row = trace_view(manager, {})["records"][0]
    assert row["stage"] == "budget_exhausted"
    assert row["result_summary"] == row["report"]["summary"]
    assert row["report"]["summary"] in result.output
    assert "partial and unverified" in result.output
    assert "Remaining model rounds including this request: 1" in requests[-1][-1]["content"]


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_worker_can_finish_within_budget_and_countdown_does_not_accumulate(tmp_path, runner):
    requests = []

    class Model(ModelAdapter):
        def next(self, messages):
            requests.append(list(messages))
            if len(requests) == 1:
                return AgentStep(type="assistant", content="Investigating", kind="progress")
            return AgentStep(type="assistant", content="Partial findings; still need to inspect X", kind="final")

        def next_stream(self, messages):
            step = self.next(messages)
            yield StreamToken(type="text", content=f"<{step.kind}>{step.content}</{step.kind}>")
            yield StreamToken(type="done", stop_reason="end_turn")

    result = runner(
        model=Model(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "Inspect"}],
        cwd=str(tmp_path),
        max_steps=2,
        agent_scope="subagent:Explore",
    )
    assert len(requests) == 2
    assert "final available model round" in requests[-1][-1]["content"]
    assert all(sum("worker-round-budget" in m.get("content", "") for m in request) == 1 for request in requests)
    assert not any("worker-round-budget" in m.get("content", "") for m in result)
    assert result[-1]["turnStatus"] == "completed"
    assert "still need" in result[-1]["content"]


def test_budget_exception_keeps_existing_worker_report(tmp_path):
    from pepsicode.execution.runtime import ModelRequestLimitError

    class Model:
        def next(self, messages):
            if any(m.get("role") == "tool_result" for m in messages):
                raise ModelRequestLimitError("Shared request budget exhausted")
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "report",
                        "toolName": "agent_report",
                        "input": {"status": "progress", "summary": "Located provider.tsx"},
                    }
                ],
            )

    manager = TraceManager()
    tool = create_task_tool(str(tmp_path), None, model_factory=lambda _: Model(), trace_manager=manager)
    result = tool.run({"agent_type": "explore", "task": "Inspect"}, ToolContext(str(tmp_path)))
    assert not result.ok
    assert "Located provider.tsx" in result.output
    assert manager.all_nodes()[0].result_summary == "Located provider.tsx"
