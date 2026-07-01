"""Tests for the loop-engineering orchestration layer (pepsicode.loop_engine)."""

import pytest

from pepsicode.cost_tracker import BudgetExceededError, CostTracker
from pepsicode.loop_engine import (
    LoopResult,
    TestRunnerVerifier,
    VerificationResult,
    run_loop,
)
from pepsicode.task_tracker import Task, TaskStatus
from pepsicode.tooling import ToolRegistry
from pepsicode.tools.test_runner import test_runner_tool
from pepsicode.types import AgentStep, ChatMessage, ModelAdapter

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FinalModel(ModelAdapter):
    """Always returns a single final assistant step.

    Each :func:`run_agent_turn` call consumes exactly one ``next()`` and then
    returns, so the number of iterations in a loop maps 1:1 to ``calls``.
    """

    def __init__(self) -> None:
        self.calls = 0

    def next(self, messages: list[ChatMessage]) -> AgentStep:
        self.calls += 1
        return AgentStep(type="assistant", content="done")


class FakeVerifier:
    """Returns a scripted sequence of verification results, one per call."""

    def __init__(self, results: list[VerificationResult]) -> None:
        self._results = list(results)
        self.calls = 0

    def verify(self, *, messages, tools, cwd, permissions) -> VerificationResult:
        result = self._results[self.calls]
        self.calls += 1
        return result


def _base_messages() -> list[ChatMessage]:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fix the bug"},
    ]


# ---------------------------------------------------------------------------
# run_loop — no verifier
# ---------------------------------------------------------------------------


def test_run_loop_no_verifier_completes_first_try() -> None:
    result = run_loop(
        model=FinalModel(),
        tools=ToolRegistry([]),
        messages=_base_messages(),
        cwd=".",
    )

    assert isinstance(result, LoopResult)
    assert result.ok is True
    assert result.iterations == 1
    assert result.stop_reason == "completed"
    assert result.task is not None
    assert result.task.status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# run_loop — verifier pass
# ---------------------------------------------------------------------------


def test_run_loop_verifier_passes() -> None:
    verifier = FakeVerifier([VerificationResult(ok=True, reason="tests passed")])

    result = run_loop(
        model=FinalModel(),
        tools=ToolRegistry([]),
        messages=_base_messages(),
        cwd=".",
        verifier=verifier,
    )

    assert result.ok is True
    assert result.iterations == 1
    assert result.stop_reason == "verified"
    assert result.verification is not None
    assert result.verification.ok is True
    assert result.task is not None
    assert result.task.status == TaskStatus.COMPLETED
    assert verifier.calls == 1


# ---------------------------------------------------------------------------
# run_loop — iteration on failure then success
# ---------------------------------------------------------------------------


def test_run_loop_verifier_fails_then_passes() -> None:
    verifier = FakeVerifier(
        [
            VerificationResult(ok=False, reason="boom"),
            VerificationResult(ok=True, reason="fixed"),
        ]
    )

    result = run_loop(
        model=FinalModel(),
        tools=ToolRegistry([]),
        messages=_base_messages(),
        cwd=".",
        max_iterations=3,
        verifier=verifier,
    )

    assert result.ok is True
    assert result.iterations == 2
    assert result.stop_reason == "verified"
    assert verifier.calls == 2
    # A nudge must have been fed back to the agent after the failed iteration.
    user_nudges = [m for m in result.messages if m["role"] == "user"]
    assert any("Verification failed" in m["content"] for m in user_nudges)


# ---------------------------------------------------------------------------
# run_loop — max iterations exhausted
# ---------------------------------------------------------------------------


def test_run_loop_max_iterations_exhausted() -> None:
    # Distinct reasons so the convergence guard does not trip early.
    verifier = FakeVerifier(
        [
            VerificationResult(ok=False, reason="err1"),
            VerificationResult(ok=False, reason="err2"),
            VerificationResult(ok=False, reason="err3"),
        ]
    )

    result = run_loop(
        model=FinalModel(),
        tools=ToolRegistry([]),
        messages=_base_messages(),
        cwd=".",
        max_iterations=3,
        verifier=verifier,
    )

    assert result.ok is False
    assert result.iterations == 3
    assert result.stop_reason == "max_iterations"
    assert result.task is not None
    assert result.task.status == TaskStatus.FAILED
    assert verifier.calls == 3


# ---------------------------------------------------------------------------
# run_loop — convergence / no-progress
# ---------------------------------------------------------------------------


def test_run_loop_no_progress_convergence() -> None:
    verifier = FakeVerifier(
        [
            VerificationResult(ok=False, reason="same"),
            VerificationResult(ok=False, reason="same"),
            VerificationResult(ok=False, reason="same"),
        ]
    )

    result = run_loop(
        model=FinalModel(),
        tools=ToolRegistry([]),
        messages=_base_messages(),
        cwd=".",
        max_iterations=3,
        verifier=verifier,
    )

    assert result.ok is False
    assert result.iterations == 2  # stops on the second identical failure
    assert result.stop_reason == "no_progress"
    assert result.task is not None
    assert result.task.status == TaskStatus.FAILED
    assert verifier.calls == 2


# ---------------------------------------------------------------------------
# run_loop — budget guard raises
# ---------------------------------------------------------------------------


def test_run_loop_budget_exceeded_raises() -> None:
    tracker = CostTracker()
    # Pre-charge the tracker past a tiny cap.
    tracker.add_usage(model="claude-sonnet-4-20250514", input_tokens=5000, output_tokens=3000)
    assert tracker.total_cost_usd > 0

    with pytest.raises(BudgetExceededError):
        run_loop(
            model=FinalModel(),
            tools=ToolRegistry([]),
            messages=_base_messages(),
            cwd=".",
            cost_tracker=tracker,
            cost_limit_usd=tracker.total_cost_usd / 2,  # cap below current spend
        )


# ---------------------------------------------------------------------------
# TestRunnerVerifier
# ---------------------------------------------------------------------------


def test_test_runner_verifier_no_tests_passes(tmp_path) -> None:
    registry = ToolRegistry([test_runner_tool])
    verifier = TestRunnerVerifier(path=".")

    result = verifier.verify(
        messages=[],
        tools=registry,
        cwd=str(tmp_path),
        permissions=None,
    )

    assert result.ok is True
    assert "no tests" in result.reason


def test_test_runner_verifier_failing_tests_fails(tmp_path) -> None:
    (tmp_path / "test_fail.py").write_text(
        "import unittest\n"
        "class _FailingTest(unittest.TestCase):\n"
        "    def test_boom(self):\n"
        "        self.fail('intentional failure')\n",
        encoding="utf-8",
    )
    registry = ToolRegistry([test_runner_tool])
    verifier = TestRunnerVerifier(path=".")

    result = verifier.verify(
        messages=[],
        tools=registry,
        cwd=str(tmp_path),
        permissions=None,
    )

    assert result.ok is False
    assert result.reason == "tests failed"


# ---------------------------------------------------------------------------
# Task retry bookkeeping
# ---------------------------------------------------------------------------


def test_task_increment_retry() -> None:
    task = Task(id="1", description="demo", max_retries=2)

    # First failure: one retry remains.
    assert task.increment_retry("err-a") is True
    assert task.retry_count == 1
    assert task.last_error == "err-a"
    assert task.is_blocked is False

    # Second failure: cap reached.
    assert task.increment_retry("err-b") is False
    assert task.retry_count == 2
    assert task.is_blocked is True
