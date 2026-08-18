"""Sandbox policy and confinement result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SandboxMode(str, Enum):
    """How aggressively a command should be confined.

    Mirrors the dsh ``SandboxMode`` vocabulary so future backends can map
    directly:

    * ``READ_ONLY`` — no writes allowed at all.
    * ``WORKSPACE_WRITE`` — writes only inside the workspace root.
    * ``DANGER_FULL_ACCESS`` — no confinement (current default).
    """

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


@dataclass(frozen=True)
class SandboxPolicy:
    """A confinement policy passed to :meth:`SandboxBackend.confine`."""

    mode: SandboxMode
    workspace_root: str
    extra_allowed_dirs: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ConfinedArgs:
    """The result of confining an argv.

    ``enforcement`` describes how strongly the confinement was applied:

    * ``"full"`` — process-level isolation (e.g. bwrap, seatbelt).
    * ``"partial"`` — some restrictions applied (e.g. env scrubbing).
    * ``"none"`` — argv returned unchanged (current default backend).
    """

    argv: list[str]
    enforcement: str = "none"
    denial_signatures: list[str] = field(default_factory=list)
