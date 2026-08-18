"""Tests for the refactored PermissionManager.

Covers both the legacy ``ensure_*`` wrappers (which raise RuntimeError on
denial) and the new ``check_*`` methods (which return ApprovalOutcome).
"""

from pathlib import Path

import pytest

from pepsicode.approval import ApprovalDecision, ApprovalOutcome
from pepsicode.permissions import PermissionManager

# ---------------------------------------------------------------------------
# Legacy ensure_* API (backward compatibility)
# ---------------------------------------------------------------------------


def test_permission_manager_uses_prompt_for_external_path(tmp_path: Path) -> None:
    external = tmp_path.parent / "outside.txt"
    manager = PermissionManager(str(tmp_path), prompt=lambda request: {"decision": "allow_once"})
    manager.ensure_path_access(str(external), "read")


def test_permission_manager_denies_external_path_without_prompt(tmp_path: Path) -> None:
    external = tmp_path.parent / "outside.txt"
    manager = PermissionManager(str(tmp_path))
    with pytest.raises(RuntimeError):
        manager.ensure_path_access(str(external), "read")


# ---------------------------------------------------------------------------
# New check_* API
# ---------------------------------------------------------------------------


def test_check_path_access_allowed_via_prompt(tmp_path: Path) -> None:
    external = tmp_path.parent / "outside.txt"
    manager = PermissionManager(str(tmp_path), prompt=lambda request: {"decision": "allow_once"})
    outcome = manager.check_path_access(str(external), "read")
    assert outcome.is_allowed
    assert outcome.decision == ApprovalDecision.ALLOW_ONCE


def test_check_path_access_unavailable_without_prompt(tmp_path: Path) -> None:
    external = tmp_path.parent / "outside.txt"
    manager = PermissionManager(str(tmp_path))
    outcome = manager.check_path_access(str(external), "read")
    assert outcome.is_unavailable
    assert outcome.decision == ApprovalDecision.UNAVAILABLE


def test_check_path_within_workspace_auto_allowed(tmp_path: Path) -> None:
    """Paths inside the workspace should be allowed without prompting."""
    internal = tmp_path / "inside.txt"
    manager = PermissionManager(str(tmp_path))
    outcome = manager.check_path_access(str(internal), "read")
    assert outcome.is_allowed


def test_check_command_safe_command_allowed(tmp_path: Path) -> None:
    """Non-dangerous commands should be allowed without prompting."""
    manager = PermissionManager(str(tmp_path))
    outcome = manager.check_command("ls", [], str(tmp_path))
    assert outcome.is_allowed


def test_check_command_dangerous_without_prompt_is_unavailable(tmp_path: Path) -> None:
    manager = PermissionManager(str(tmp_path))
    outcome = manager.check_command("rm", ["-rf", "/"], str(tmp_path))
    assert outcome.is_unavailable


def test_check_command_dangerous_with_prompt(tmp_path: Path) -> None:
    manager = PermissionManager(str(tmp_path), prompt=lambda req: {"decision": "allow_once"})
    outcome = manager.check_command("rm", ["-rf", "subdir"], str(tmp_path))
    assert outcome.is_allowed


def test_check_command_denied_returns_outcome(tmp_path: Path) -> None:
    manager = PermissionManager(str(tmp_path), prompt=lambda req: {"decision": "deny_once"})
    outcome = manager.check_command("rm", ["-rf", "subdir"], str(tmp_path))
    assert outcome.is_denied
    assert "Denied" in outcome.denial_message(scope="rm -rf subdir")


def test_check_edit_without_prompt_is_unavailable(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    manager = PermissionManager(str(tmp_path))
    outcome = manager.check_edit(str(target), "+diff preview")
    assert outcome.is_unavailable


def test_check_edit_allowed_via_prompt(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    manager = PermissionManager(str(tmp_path), prompt=lambda req: {"decision": "allow_always"})
    outcome = manager.check_edit(str(target), "+diff preview")
    assert outcome.is_allowed
    # Second call should hit the persistent cache (no prompt needed)
    outcome2 = manager.check_edit(str(target), "+diff preview")
    assert outcome2.is_allowed


def test_denial_message_includes_feedback(tmp_path: Path) -> None:
    manager = PermissionManager(
        str(tmp_path), prompt=lambda req: {"decision": "deny_with_feedback", "feedback": "use safer approach"}
    )
    outcome = manager.check_command("rm", ["-rf", "x"], str(tmp_path))
    assert outcome.is_denied
    msg = outcome.denial_message(scope="rm -rf x")
    assert "rm -rf x" in msg
    assert "use safer approach" in msg


# ---------------------------------------------------------------------------
# ApprovalBackend injection
# ---------------------------------------------------------------------------


class _StubBackend:
    """Minimal ApprovalBackend stub for testing injection."""

    def __init__(self, decision: ApprovalDecision) -> None:
        self._decision = decision
        self.calls: list = []

    def request(self, req):
        self.calls.append(req)
        return ApprovalOutcome(decision=self._decision)


def test_custom_approval_backend_injection(tmp_path: Path) -> None:
    """PermissionManager should accept any ApprovalBackend via approval=."""
    backend = _StubBackend(ApprovalDecision.ALLOW_ONCE)
    manager = PermissionManager(str(tmp_path), approval=backend)
    external = tmp_path.parent / "outside.txt"
    outcome = manager.check_path_access(str(external), "read")
    assert outcome.is_allowed
    assert len(backend.calls) == 1
    assert backend.calls[0].kind.value == "path"
