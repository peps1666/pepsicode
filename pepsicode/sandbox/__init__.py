"""Sandbox seam: confinement policy + backend interface.

This package separates the "how is this command confined?" question from
the "should this command run?" question handled by :mod:`pepsicode.approval`.

Currently the only backend is :class:`LocalSandboxBackend`, which performs
no process-level isolation (matching the existing behaviour).  The seam
exists so that future backends (Docker, firejail, Windows restricted token,
remote execution) can be slotted in without touching the agent loop.
"""

from pepsicode.sandbox.backend import SandboxBackend
from pepsicode.sandbox.classifier import classify_dangerous_command, format_command_signature
from pepsicode.sandbox.local_backend import LocalSandboxBackend
from pepsicode.sandbox.types import ConfinedArgs, SandboxMode, SandboxPolicy

__all__ = [
    "ConfinedArgs",
    "LocalSandboxBackend",
    "SandboxBackend",
    "SandboxMode",
    "SandboxPolicy",
    "classify_dangerous_command",
    "format_command_signature",
]
