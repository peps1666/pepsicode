"""Approval seam: typed request/outcome protocol for human-in-the-loop gating.

This package separates the "should this action proceed?" decision from both
the policy state (what's already allowed/denied) and the sandbox (how the
action is actually executed).  The :class:`ApprovalBackend` interface is
the single seam; implementations may bridge to a local TTY prompt, a remote
WebSocket client, or an automated policy engine.
"""

from pepsicode.approval.backend import ApprovalBackend
from pepsicode.approval.local_backend import LocalApprovalBackend
from pepsicode.approval.store import ApprovalStore
from pepsicode.approval.types import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalOutcome,
    ApprovalRequest,
)

__all__ = [
    "ApprovalBackend",
    "ApprovalDecision",
    "ApprovalKind",
    "ApprovalOutcome",
    "ApprovalRequest",
    "ApprovalStore",
    "LocalApprovalBackend",
]
