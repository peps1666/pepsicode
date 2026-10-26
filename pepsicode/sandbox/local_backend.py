"""Default sandbox backend: no process-level isolation.

This preserves the existing behaviour (commands run directly via
``subprocess``) while implementing the :class:`SandboxBackend` seam so
future backends can replace it.
"""

from __future__ import annotations

from pepsicode.sandbox.backend import SandboxBackend, SandboxUnavailableError
from pepsicode.sandbox.types import ConfinedArgs, SandboxMode, SandboxPolicy


class LocalSandboxBackend(SandboxBackend):
    """No-op confinement: returns the argv unchanged.

    Equivalent to dsh's ``danger-full-access`` mode.  The path-safety check
    that used to live inside ``ensure_command`` is now handled by the
    approval layer before :meth:`confine` is called.
    """

    def confine(self, argv: list[str], policy: SandboxPolicy) -> ConfinedArgs:  # noqa: ARG002
        if policy.mode != SandboxMode.DANGER_FULL_ACCESS:
            raise SandboxUnavailableError(
                "The local backend provides no OS isolation; restrictive execution was refused"
            )
        return ConfinedArgs(argv=list(argv), enforcement="none")
