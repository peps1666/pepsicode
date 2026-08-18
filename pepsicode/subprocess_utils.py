"""Cross-platform helpers for subprocess calls.

On Windows, every ``subprocess.Popen`` / ``subprocess.run`` call without an
explicit ``startupinfo`` argument pops up a console window — even when the
parent process has no console (as is the case when launched from Electron).
This causes the "flashing black windows" users see during ``npm start``.

Usage::

    from pepsicode.subprocess_utils import run, Popen, hide_window_kwargs

    # Drop-in replacements:
    result = run(["git", "status"], capture_output=True, text=True)
    proc = Popen(["cmd", "/c", "echo hi"], stdout=PIPE)

    # Or apply to existing calls:
    result = subprocess.run(..., **hide_window_kwargs())
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Windows startupinfo singleton
# ---------------------------------------------------------------------------

_startupinfo: subprocess.STARTUPINFO | None = None
_creationflags: int = 0


def _ensure_win_startupinfo() -> tuple[subprocess.STARTUPINFO | None, int]:
    """Initialise (cached) Windows startupinfo + creationflags for hidden windows.

    Returns ``(None, 0)`` on non-Windows platforms.
    """
    global _startupinfo, _creationflags
    if sys.platform != "win32":
        return None, 0
    if _startupinfo is None:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        _startupinfo = si
        # CREATE_NO_WINDOW (0x08000000) is the strongest console-suppression
        # flag on Windows.  It prevents the child from inheriting or creating
        # a console, and also propagates to grandchildren spawned via cmd.exe.
        _creationflags = 0x08000000
    return _startupinfo, _creationflags


def hide_window_kwargs(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return kwargs that suppress console pop-ups on Windows.

    Merge the result into your ``subprocess.run`` / ``Popen`` call::

        result = subprocess.run(
            ["git", "status"],
            capture_output=True,
            text=True,
            **hide_window_kwargs(),
        )

    On non-Windows platforms this returns an empty dict (plus any ``extra``).
    """
    si, cf = _ensure_win_startupinfo()
    kwargs: dict[str, Any] = {}
    if si is not None:
        kwargs["startupinfo"] = si
    if cf:
        kwargs["creationflags"] = cf
    if extra:
        # Allow callers to override (e.g. MCP's DETACHED_PROCESS) — but be
        # careful: creationflags are OR-ed, startupinfo is replaced.
        if "creationflags" in extra and "creationflags" in kwargs:
            kwargs["creationflags"] = cf | extra.pop("creationflags")
        kwargs.update(extra)
    return kwargs


# ---------------------------------------------------------------------------
# Drop-in replacements for subprocess.run / Popen
# ---------------------------------------------------------------------------


def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` wrapper that hides console windows on Windows.

    If the caller already passed ``startupinfo`` or ``creationflags``, their
    values are OR-ed / merged rather than overwritten.
    """
    _apply_hide_defaults(kwargs)
    return subprocess.run(*args, **kwargs)


def Popen(*args: Any, **kwargs: Any) -> subprocess.Popen:  # noqa: N802
    """``subprocess.Popen`` wrapper that hides console windows on Windows."""
    _apply_hide_defaults(kwargs)
    return subprocess.Popen(*args, **kwargs)


def _apply_hide_defaults(kwargs: dict[str, Any]) -> None:
    """Merge hidden-window defaults into ``kwargs`` in place."""
    si, cf = _ensure_win_startupinfo()
    if si is not None and "startupinfo" not in kwargs:
        kwargs["startupinfo"] = si
    if cf and "creationflags" not in kwargs:
        kwargs["creationflags"] = cf
    elif cf and "creationflags" in kwargs:
        # OR the caller's flags with CREATE_NO_WINDOW so they still get
        # console suppression (e.g. MCP's DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP).
        kwargs["creationflags"] = cf | kwargs["creationflags"]
