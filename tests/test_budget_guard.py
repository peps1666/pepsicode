"""Tests for cost-tracker wiring and the budget guard inside run_agent_turn."""

import pytest

from pepsicode.agent_loop import run_agent_turn
from pepsicode.cost_tracker import BudgetExceededError, CostTracker
from pepsicode.tooling import ToolRegistry
from pepsicode.types import AgentStep, ChatMessage, ModelAdapter


class FinalModel(ModelAdapter):
    """Returns one final assistant step, optionally carrying last_usage."""

    def __init__(self, last_usage: dict | None = None) -> None:
        self.calls = 0
        self.last_usage = last_usage

    def next(self, messages: list[ChatMessage]) -> AgentStep:
        self.calls += 1
        return AgentStep(type="assistant", content="done")


def _base_messages() -> list[ChatMessage]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


def test_run_agent_turn_records_usage_to_cost_tracker() -> None:
    tracker = CostTracker()
    model = FinalModel(
        last_usage={
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 5000,
            "output_tokens": 3000,
            "cache_read_tokens": 2000,
        }
    )

    run_agent_turn(
        model=model,
        tools=ToolRegistry([]),
        messages=_base_messages(),
        cwd=".",
        cost_tracker=tracker,
    )

    assert tracker.get_total_calls() == 1
    assert tracker.total_cost_usd > 0
    usage = tracker.get_model_usage("claude-sonnet-4-20250514")
    assert usage.input_tokens == 5000
    assert usage.output_tokens == 3000
    assert usage.cache_read_tokens == 2000


def test_run_agent_turn_raises_budget_exceeded() -> None:
    tracker = CostTracker()
    tracker.add_usage(model="claude-sonnet-4-20250514", input_tokens=5000, output_tokens=3000)
    cap = tracker.total_cost_usd / 2  # below current spend
    model = FinalModel()

    with pytest.raises(BudgetExceededError) as excinfo:
        run_agent_turn(
            model=model,
            tools=ToolRegistry([]),
            messages=_base_messages(),
            cwd=".",
            cost_tracker=tracker,
            cost_limit_usd=cap,
        )

    assert excinfo.value.limit == cap
    assert excinfo.value.spent == tracker.total_cost_usd
    # The model must never have been invoked: the guard trips before next().
    assert model.calls == 0


def test_run_agent_turn_no_cost_tracker_unchanged() -> None:
    # No cost_tracker / cost_limit_usd -> identical to prior behaviour.
    model = FinalModel()

    messages = run_agent_turn(
        model=model,
        tools=ToolRegistry([]),
        messages=_base_messages(),
        cwd=".",
    )

    assert messages[-1] == {"role": "assistant", "content": "done"}
    assert model.calls == 1
