"""Host-controlled Git entry for managed orchestration work (M2/M4).

Every git invocation from managed preparation, integration and delivery goes
through :func:`run_git`: a sanitized environment (no user config, no hooks,
no terminal prompt), journaled through the execution layer so cancellations,
deadlines and unknown outcomes are handled like any other command. Mutation
commands that touch the shared ``.git`` metadata must be serialized by the
caller with :func:`pepsicode.orchestration.runtime.own_workspace`.
"""

from __future__ import annotations

import os

from pepsicode.execution import process
from pepsicode.tooling import ToolContext

_GIT_ENV_KEYS = ("PATH", "SystemRoot", "WINDIR", "SystemDrive", "PATHEXT")


def run_git(context: ToolContext, cwd, *arguments, allowed=(0,), timeout=60):
    """Run one git command under the sanitized managed environment."""
    env = {name: os.environ[name] for name in _GIT_ENV_KEYS if name in os.environ}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    result = process.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", *arguments],
        context=context,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if result.returncode not in allowed:
        raise RuntimeError(f"Git {arguments[0]} failed ({result.returncode}): {result.stderr.strip()[:800]}")
    return result
