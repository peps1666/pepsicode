"""Loop-engineering orchestration layer.

Wraps :func:`pepsicode.core.agent_loop.run_agent_turn` with the three "connectors"
that turn a single agent turn into a self-closing loop:

* **Verifier** — independently checks whether the agent's output actually
  meets the goal (default impl runs the project's tests via the
  ``test_runner`` tool).
* **Iteration Policy** — when verification fails, nudges the agent to retry,
  escalating the instruction as retries mount, and stops early when the same
  failure repeats (no-progress convergence).
* **Budget Guard** — raises :class:`BudgetExceededError` once accumulated
  spend crosses a configurable cap; enforced both inside the agent turn
  (per step) and here (per iteration).

Verifier and budget are both opt-in (``verifier=None`` / ``cost_limit_usd=None``
by default) so wiring this layer into an existing call site changes nothing
unless the caller asks for it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pepsicode.core.agent_loop import run_agent_turn
from pepsicode.core.task_tracker import Task, TaskManager
from pepsicode.llm.cost_tracker import BudgetExceededError, CostTracker
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.types import ChatMessage, ModelAdapter

# Re-export so callers can import the budget error from the loop module too.
__all__ = [
    "BudgetExceededError",
    "Verifier",
    "VerificationResult",
    "TestRunnerVerifier",
    "IterationStrategy",
    "LoopResult",
    "run_loop",
]


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """Outcome of verifying an agent's work for one iteration."""

    ok: bool
    reason: str
    details: str = ""
    status: str = ""  # passed | failed | not_run | blocked | cancelled
    reason_code: str = ""

    @property
    def fingerprint(self) -> str:
        # Durations change between otherwise identical failures.
        details = re.sub(r"\b\d+(?:\.\d+)?s\b", "<duration>", self.details)
        return self.reason + "\n" + details


class Verifier(Protocol):
    """Checks whether the agent's output satisfies the goal.

    Implementations are deliberately simple callables so the loop can stay
    agnostic of *how* verification is done (tests, a rubric, an LLM judge,
    a type-checker, ...).
    """

    def verify(
        self,
        *,
        messages: list[ChatMessage],
        tools: ToolRegistry,
        cwd: str,
        permissions: PermissionManager | None,
    ) -> VerificationResult: ...


class TestRunnerVerifier:
    """Default verifier: run the project's tests via the ``test_runner`` tool.

    A directory with no tests returns ``status="not_run"`` so callers can
    distinguish unverified work from passing tests. A genuine test failure
    yields ``ok=False`` with the tool's output as ``details``.
    """

    # Tell pytest this is not a test class despite the ``Test`` prefix.
    __test__ = False

    _NO_TESTS_MARKER = "No test files found"

    def __init__(self, path: str = ".") -> None:
        self.path = path

    def verify(
        self,
        *,
        messages: list[ChatMessage],
        tools: ToolRegistry,
        cwd: str,
        permissions: PermissionManager | None,
    ) -> VerificationResult:
        if tools.find("test_runner") is None:
            # No test_runner registered -> nothing to verify against.
            return VerificationResult(ok=False, reason="no test_runner tool available", status="not_run")

        context = ToolContext(cwd=cwd, permissions=permissions)
        result = tools.execute("test_runner", {"path": self.path}, context)

        if self._NO_TESTS_MARKER in (result.output or ""):
            return VerificationResult(ok=False, reason="no tests found, skipping", status="not_run")
        if result.ok:
            return VerificationResult(ok=True, reason="tests passed", details=result.output)
        return VerificationResult(ok=False, reason="tests failed", details=result.output)


# ---------------------------------------------------------------------------
# Iteration policy
# ---------------------------------------------------------------------------


class IterationStrategy:
    """Produces the nudge fed back to the agent after a failed verification.

    The nudge escalates with ``retry_count`` so the agent is pushed toward
    deeper diagnosis rather than repeating the same surface fix.
    """

    @staticmethod
    def nudge(retry_count: int, reason: str) -> str:
        if retry_count <= 1:
            return f"Verification failed: {reason}. Fix the issue and retry the task."
        if retry_count == 2:
            return f"{reason}. Investigate the root cause before retrying; do not repeat the same fix that just failed."
        return (
            f"{reason}. Previous retries did not resolve this. Reconsider the "
            "approach entirely, or conclude the task cannot be completed."
        )


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class LoopResult:
    """Final outcome of a :func:`run_loop` call."""

    ok: bool
    iterations: int
    messages: list[ChatMessage]
    verification: VerificationResult | None = None
    task: Task | None = None
    stop_reason: str = ""  # verified | completed | no_progress | max_iterations


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_loop(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    messages: list[ChatMessage],
    cwd: str,
    task: Task | None = None,
    task_manager: TaskManager | None = None,
    cost_tracker: CostTracker | None = None,
    cost_limit_usd: float | None = None,
    max_iterations: int = 3,
    no_progress_limit: int = 2,
    verifier: Verifier | None = None,
    permissions: PermissionManager | None = None,
    context_manager=None,
    memory: Any | None = None,
    on_iteration: Callable[[int, VerificationResult], None] | None = None,
    on_verify: Callable[[VerificationResult], None] | None = None,
    runner: Callable | None = None,
    turn_options: dict | None = None,
    cancellation_event: Any | None = None,
) -> LoopResult:
    """Drive an agent toward a verifiable goal with bounded retries.

    Each iteration runs one agent turn, then (if a verifier is configured)
    checks the result. On success the task is marked complete; on failure the
    agent is nudged to retry with an escalating instruction. The loop stops
    when the goal is verified, when the same failure repeats (no progress),
    when ``max_iterations`` is exhausted, or when the budget cap is crossed.
    """
    for name, value in (("max_iterations", max_iterations), ("no_progress_limit", no_progress_limit)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    messages = list(messages)
    task = task if task is not None else Task(id="1", description="loop task")
    task.start()

    last_reason: str | None = None
    repeated_failures = 0
    last_verification: VerificationResult | None = None

    for iteration in range(1, max_iterations + 1):
        # Loop-level budget guard (the turn also checks per-step). Raising
        # here lets callers catch budget exhaustion uniformly, whether it
        # trips before a turn or mid-turn via run_agent_turn.
        if cost_tracker is not None and cost_limit_usd is not None:
            if cost_tracker.total_cost_usd >= cost_limit_usd:
                raise BudgetExceededError(
                    limit=cost_limit_usd,
                    spent=cost_tracker.total_cost_usd,
                )

        if cancellation_event is not None and cancellation_event.is_set():
            task.cancel()
            return LoopResult(False, iteration - 1, messages, stop_reason="cancelled")
        messages = (runner or run_agent_turn)(
            model=model,
            tools=tools,
            messages=messages,
            cwd=cwd,
            permissions=permissions,
            context_manager=context_manager,
            cost_tracker=cost_tracker,
            cost_limit_usd=cost_limit_usd,
            memory=memory,
            **({"cancellation_event": cancellation_event} if cancellation_event is not None else {}),
            **(turn_options or {}),
        )

        if cancellation_event is not None and cancellation_event.is_set():
            task.cancel()
            return LoopResult(False, iteration, messages, stop_reason="cancelled")
        if messages and messages[-1].get("isError"):
            task.fail(messages[-1].get("content", "Agent turn failed"))
            return LoopResult(False, iteration, messages, stop_reason="agent_error")

        if messages and messages[-1].get("awaitingUser"):
            # Clarification is a deliberate pause, even if earlier tool calls
            # changed files. Verification must not invent the user's answer.
            paused = VerificationResult(
                False, "Waiting for user clarification", status="not_run", reason_code="awaiting_user"
            )
            if on_verify is not None:
                on_verify(paused)
            return LoopResult(False, iteration, messages, paused, task, "awaiting_user")

        # No verifier configured -> an agent turn is "done" by definition.
        if verifier is None:
            task.complete()
            return LoopResult(
                ok=True,
                iterations=iteration,
                messages=messages,
                verification=None,
                task=task,
                stop_reason="completed",
            )

        last_verification = verifier.verify(
            messages=messages,
            tools=tools,
            cwd=cwd,
            permissions=permissions,
        )
        if on_verify is not None:
            on_verify(last_verification)

        if last_verification.status in {"not_run", "blocked", "cancelled"}:
            if last_verification.status == "cancelled":
                task.cancel()
            elif last_verification.status == "blocked":
                task.fail(last_verification.reason)
            else:
                task.complete()
            return LoopResult(False, iteration, messages, last_verification, task, last_verification.status)

        if last_verification.ok:
            task.complete()
            return LoopResult(
                ok=True,
                iterations=iteration,
                messages=messages,
                verification=last_verification,
                task=task,
                stop_reason="verified",
            )

        # Convergence: same failure as last iteration -> stop spinning.
        repeated_failures = repeated_failures + 1 if last_verification.fingerprint == last_reason else 1
        if repeated_failures >= no_progress_limit:
            task.fail(f"no progress: {last_verification.reason}")
            return LoopResult(
                ok=False,
                iterations=iteration,
                messages=messages,
                verification=last_verification,
                task=task,
                stop_reason="no_progress",
            )
        last_reason = last_verification.fingerprint

        if on_iteration is not None:
            on_iteration(iteration, last_verification)

        task.increment_retry(last_verification.reason)

        # Feed the failure back to the agent as a user nudge for the next turn.
        nudge = IterationStrategy.nudge(task.retry_count, last_verification.reason)
        if last_verification.details:
            nudge += "\nVerification output:\n" + last_verification.details[:12000]
        if iteration < max_iterations:
            messages.append({"role": "user", "content": nudge})

    # Exhausted max_iterations without success.
    task.fail(f"max iterations ({max_iterations}) exceeded")
    return LoopResult(
        ok=False,
        iterations=max_iterations,
        messages=messages,
        verification=last_verification,
        task=task,
        stop_reason="max_iterations",
    )
