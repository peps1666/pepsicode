"""Approval backend interface — the single seam between policy and UI."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pepsicode.approval.types import ApprovalOutcome, ApprovalRequest


class ApprovalBackend(ABC):
    """Ask an answerer (human or automated) to decide on a request.

    Implementations MUST be fail-closed: when no answerer is available,
    return :attr:`ApprovalDecision.UNAVAILABLE` rather than raising.  This
    lets callers render a structured error instead of catching exceptions.
    """

    @abstractmethod
    def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        """Synchronously request approval and return the outcome."""
        raise NotImplementedError
