"""Copy text to the system clipboard from the terminal UI.

Mouse capture means the terminal no longer copies selections itself, so the
TUI copies them: through the native clipboard where one is reachable, and
otherwise through OSC 52, which most modern terminals (including over SSH)
forward to the local clipboard.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys

from pepsicode.subprocess_utils import hide_window_kwargs


def _copy_windows(text: str) -> bool:
    import ctypes
    import ctypes.wintypes as wintypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    # Handles are pointer-sized; the default int restype truncates them on 64-bit.
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    data = ctypes.create_unicode_buffer(text.replace("\n", "\r\n"))
    size = ctypes.sizeof(data)
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        # The clipboard owns the memory once SetClipboardData succeeds.
        return True
    finally:
        user32.CloseClipboard()


def _copy_with_command(text: str) -> bool:
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for command in candidates:
        if shutil.which(command[0]) is None:
            continue
        try:
            completed = subprocess.run(  # noqa: S603
                command,
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                **hide_window_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return True
    return False


def _copy_osc52(text: str) -> None:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sys.stdout.write(f"\x1b]52;c;{payload}\x07")
    sys.stdout.flush()


def copy_to_clipboard(text: str) -> None:
    """Best-effort copy; must run on the thread that owns stdout."""
    try:
        if sys.platform == "win32":
            copied = _copy_windows(text)
        else:
            copied = _copy_with_command(text)
    except Exception:  # noqa: BLE001 - fall back to the terminal clipboard
        copied = False
    if not copied:
        _copy_osc52(text)
