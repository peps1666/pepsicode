"""Typed approval primitives.

These mirror the wire format already spoken with the Electron client
(``permission/request`` event + ``tool/approve`` response) but give the
Python side first-class, IDE-friendly types instead of bare dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ApprovalKind(str, Enum):
    """What kind of action is being gated."""

    PATH = "path"
    COMMAND = "command"
    EDIT = "edit"
    PLAN = "plan"


class ApprovalDecision(str, Enum):
    """All possible outcomes of an approval request.

    The string values are kept identical to the existing client protocol so
    the wire format does not change.
    """

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    ALLOW_TURN = "allow_turn"
    ALLOW_ALL_TURN = "allow_all_turn"
    DENY_ONCE = "deny_once"
    DENY_ALWAYS = "deny_always"
    DENY_WITH_FEEDBACK = "deny_with_feedback"
    # New, explicit "fail-closed" outcomes (not on the wire; internal only).
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"

    def is_allowed(self) -> bool:
        return self.value.startswith("allow")

    def is_denied(self) -> bool:
        return self.value.startswith("deny")

    def is_persistent(self) -> bool:
        """Whether the decision should be persisted to disk."""
        return self in (ApprovalDecision.ALLOW_ALWAYS, ApprovalDecision.DENY_ALWAYS)


@dataclass(frozen=True)
class ApprovalRequest:
    """An immutable description of an action that needs approval.

    The ``choices`` field preserves the existing list-of-dicts shape used by
    the client UI (``{"key", "label", "decision"}``) so no frontend change
    is required.
    """

    kind: ApprovalKind
    summary: str
    details: list[str]
    scope: str
    choices: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Convert to the wire payload consumed by the client."""
        return {
            "kind": self.kind.value,
            "summary": self.summary,
            "details": list(self.details),
            "scope": self.scope,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class ApprovalOutcome:
    """The result of an approval request.

    Carries enough structure that callers can distinguish "denied with a
    reason" from "no answerer available" without parsing exception strings.
    """

    decision: ApprovalDecision
    feedback: str = ""

    @property
    def is_allowed(self) -> bool:
        return self.decision.is_allowed()

    @property
    def is_denied(self) -> bool:
        return self.decision.is_denied()

    @property
    def is_unavailable(self) -> bool:
        return self.decision in (ApprovalDecision.UNAVAILABLE, ApprovalDecision.CANCELLED)

    @property
    def is_persistent(self) -> bool:
        return self.decision.is_persistent()

    def denial_message(self, *, scope: str = "") -> str:
        """Render a human-readable denial message (used as tool output).

        Returns an empty string for allowed outcomes so callers can use
        this method unconditionally in error-rendering code.
        """
        if not self.is_denied and not self.is_unavailable:
            return ""
        if self.is_unavailable:
            base = "Approval required but no interactive prompt is available"
        else:
            base = "Denied"
        if scope:
            base = f"{base}: {scope}"
        if self.feedback:
            base = f"{base}\nUser guidance: {self.feedback}"
        return base
