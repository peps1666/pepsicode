"""Default approval backend that wraps the existing ``PromptHandler`` callable.

This keeps the TTY/CLI behaviour unchanged while exposing the typed
:class:`ApprovalBackend` interface to the rest of the codebase.
"""

from __future__ import annotations

from typing import Any, Callable

from pepsicode.approval.backend import ApprovalBackend
from pepsicode.approval.types import ApprovalDecision, ApprovalOutcome, ApprovalRequest

# Re-exported so callers can import the alias from one place.
PromptHandler = Callable[[dict[str, Any]], dict[str, Any]]


class LocalApprovalBackend(ApprovalBackend):
    """Bridges the legacy ``prompt(request_dict) -> response_dict`` protocol.

    When ``prompt`` is ``None`` (no interactive terminal attached), every
    request fails closed with :attr:`ApprovalDecision.UNAVAILABLE`.
    """

    def __init__(self, prompt: PromptHandler | None) -> None:
        self._prompt = prompt

    @property
    def prompt(self) -> PromptHandler | None:
        return self._prompt

    @prompt.setter
    def prompt(self, value: PromptHandler | None) -> None:
        self._prompt = value

    def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        if self._prompt is None:
            return ApprovalOutcome(decision=ApprovalDecision.UNAVAILABLE)

        raw = self._prompt(req.to_payload())
        if not isinstance(raw, dict):
            return ApprovalOutcome(decision=ApprovalDecision.DENY_ONCE)

        decision_str = str(raw.get("decision", "deny_once"))
        try:
            decision = ApprovalDecision(decision_str)
        except ValueError:
            decision = ApprovalDecision.DENY_ONCE

        feedback = str(raw.get("feedback", "")).strip()
        return ApprovalOutcome(decision=decision, feedback=feedback)
