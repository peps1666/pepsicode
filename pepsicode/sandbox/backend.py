"""Sandbox backend interface — the seam between policy and execution."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pepsicode.sandbox.types import ConfinedArgs, SandboxPolicy


class SandboxBackend(ABC):
    """Confine an argv according to a :class:`SandboxPolicy`.

    Backends must be fail-closed: if the requested mode cannot be enforced,
    raise :class:`SandboxUnavailableError` rather than silently degrading.
    """

    @abstractmethod
    def confine(self, argv: list[str], policy: SandboxPolicy) -> ConfinedArgs:
        """Return the confined argv that should actually be spawned."""
        raise NotImplementedError


class SandboxUnavailableError(RuntimeError):
    """Raised when the requested sandbox mode cannot be enforced."""
