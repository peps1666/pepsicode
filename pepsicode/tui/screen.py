from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from .chrome import char_display_width, strip_ansi, truncate_plain

# DEC mode 2026: the terminal buffers everything between begin/end and shows
# it in one paint, so multi-row updates never appear half-drawn. Terminals
# without support ignore the unknown private mode.
BEGIN_SYNCHRONIZED_UPDATE = "\x1b[?2026h"
END_SYNCHRONIZED_UPDATE = "\x1b[?2026l"
REVERSE_ON = "\x1b[7m"
REVERSE_OFF = "\x1b[27m"
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass
class Selection:
    """A text selection in 0-based screen cells; ``focus`` is inclusive."""

    anchor: tuple[int, int]
    focus: tuple[int, int]

    def ordered(self) -> tuple[tuple[int, int], tuple[int, int]]:
        return (self.anchor, self.focus) if self.anchor <= self.focus else (self.focus, self.anchor)

    @property
    def is_empty(self) -> bool:
        return self.anchor == self.focus

    def columns_for_row(self, row: int) -> tuple[int, int] | None:
        """Selected ``[start, end)`` columns on ``row``, or None."""
        (start_row, start_col), (end_row, end_col) = self.ordered()
        if row < start_row or row > end_row:
            return None
        start = start_col if row == start_row else 0
        end = end_col + 1 if row == end_row else 1 << 30
        return start, end


def highlight_columns(line: str, start: int, end: int) -> str:
    """Reverse-video display columns ``[start, end)`` while keeping styling."""
    out: list[str] = []
    col = 0
    inside = False
    i = 0
    while i < len(line):
        match = _CSI_RE.match(line, i)
        if match:
            out.append(match.group())
            # Style resets inside the range would cancel the highlight.
            if inside:
                out.append(REVERSE_ON)
            i = match.end()
            continue
        width = char_display_width(line[i])
        if not inside and start <= col < end and width:
            out.append(REVERSE_ON)
            inside = True
        elif inside and col >= end:
            out.append(REVERSE_OFF)
            inside = False
        out.append(line[i])
        col += width
        i += 1
    if inside:
        out.append(REVERSE_OFF)
    return "".join(out)


def slice_columns(text: str, start: int, end: int) -> str:
    """Plain characters whose display cell starts within ``[start, end)``."""
    out: list[str] = []
    col = 0
    for char in text:
        if col >= end:
            break
        if start <= col:
            out.append(char)
        col += char_display_width(char)
    return "".join(out)


class FrameRenderer:
    """Update changed physical rows without newline-driven scrolling."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._content: list[str] = []
        self._size: tuple[int, int] | None = None
        self.selection: Selection | None = None

    def draw(self, lines: list[str], cols: int, rows: int) -> None:
        cols, rows = max(2, cols), max(1, rows)
        # Leave the final column unused to avoid autowrap on the bottom row.
        content = [truncate_plain(line, cols - 1) for line in lines[:rows]]
        content += [""] * (rows - len(content))
        frame = list(content)
        if self.selection is not None and not self.selection.is_empty:
            for index, line in enumerate(frame):
                span = self.selection.columns_for_row(index)
                if span is not None:
                    frame[index] = highlight_columns(line, *span)
        previous = self._lines if self._size == (cols, rows) else []
        updates: list[str] = []
        for index, line in enumerate(frame):
            if index < len(previous) and previous[index] == line:
                continue
            # Paint before erasing the old suffix. Absolute row positioning
            # works even when raw mode disables LF -> CRLF conversion.
            updates.append(f"\x1b[{index + 1};1H\x1b[0m{line}\x1b[0m\x1b[K")
        if updates:
            sys.stdout.write(BEGIN_SYNCHRONIZED_UPDATE + "".join(updates) + END_SYNCHRONIZED_UPDATE)
            sys.stdout.flush()
        self._lines = frame
        self._content = content
        self._size = (cols, rows)

    def selected_text(self) -> str:
        """Plain text under the current selection, as last drawn."""
        if self.selection is None or self.selection.is_empty:
            return ""
        (start_row, _), (end_row, _) = self.selection.ordered()
        picked: list[str] = []
        for row in range(start_row, min(end_row, len(self._content) - 1) + 1):
            span = self.selection.columns_for_row(row)
            if span is not None:
                picked.append(slice_columns(strip_ansi(self._content[row]), *span).rstrip())
        return "\n".join(picked).strip("\n")


ENTER_ALT_SCREEN = "\u001b[?1049h"
EXIT_ALT_SCREEN = "\u001b[?1049l"
ERASE_SCREEN_AND_HOME = "\u001b[2J\u001b[H"
# Basic tracking (?1000) reports clicks and the wheel, button-event tracking
# (?1002) adds drags so the app can select and copy text itself, and SGR
# encoding (?1006) handles coordinates > 223. PEPSI_CODE_MOUSE=0 hands the
# mouse back to the terminal (native selection, but no wheel scrolling).
ENABLE_MOUSE_TRACKING = "\u001b[?1000h\u001b[?1002h\u001b[?1006h"
DISABLE_MOUSE_TRACKING = "\u001b[?1006l\u001b[?1002l\u001b[?1000l"

# Terminal types that do not support alternate screen or mouse tracking.
_DUMB_TERMS = frozenset({"dumb", "linux", ""})


# ---------------------------------------------------------------------------
# Windows VT processing
# ---------------------------------------------------------------------------

_vt_enabled = False
_original_input_mode: int | None = None


def _enable_windows_vt_processing() -> None:
    """Enable ANSI / VT escape sequence processing on Windows 10+.

    Without this call the console ignores escape codes for colours,
    alternate-screen, cursor visibility, mouse tracking, etc.
    The function is a no-op on non-Windows platforms or when the
    underlying API call is unavailable.
    """
    global _vt_enabled
    if _vt_enabled:
        return

    if sys.platform != "win32":
        _vt_enabled = True
        return

    try:
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        STD_OUTPUT_HANDLE = -11
        STD_ERROR_HANDLE = -12
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ENABLE_PROCESSED_OUTPUT = 0x0001

        for handle_id in (STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
            handle = kernel32.GetStdHandle(handle_id)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_PROCESSED_OUTPUT
                kernel32.SetConsoleMode(handle, new_mode)

        # Also enable VT input processing so the console sends ANSI
        # escape sequences for special keys instead of Windows-native
        # key events (useful for ConPTY / Windows Terminal).
        STD_INPUT_HANDLE = -10
        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
        h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode_in = wintypes.DWORD()
        if kernel32.GetConsoleMode(h_in, ctypes.byref(mode_in)):
            global _original_input_mode
            _original_input_mode = mode_in.value
            kernel32.SetConsoleMode(h_in, mode_in.value | ENABLE_VIRTUAL_TERMINAL_INPUT)

        _vt_enabled = True
    except Exception:
        # If ctypes is unavailable or the call fails (e.g. old Windows),
        # fall through silently — ANSI codes will simply not render.
        _vt_enabled = True


def _restore_windows_console_mode() -> None:
    """Restore the original Windows console input mode on exit.

    Must be called before the process exits, otherwise the console will keep
    sending ANSI escape sequences for special keys instead of normal Windows
    key events (arrow keys show as ^[[A instead of command history).
    """
    global _original_input_mode, _vt_enabled
    if _original_input_mode is None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        STD_INPUT_HANDLE = -10
        h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        kernel32.SetConsoleMode(h_in, _original_input_mode)
        _original_input_mode = None
        _vt_enabled = False
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def hide_cursor() -> None:
    _enable_windows_vt_processing()
    sys.stdout.write("\u001b[?25l")
    sys.stdout.flush()


def show_cursor() -> None:
    sys.stdout.write("\u001b[?25h")
    sys.stdout.flush()


def _is_dumb_terminal() -> bool:
    """Return True if the terminal likely doesn't support escape sequences."""
    if sys.platform == "win32":
        return False
    return os.environ.get("TERM", "") in _DUMB_TERMS


def mouse_tracking_enabled() -> bool:
    """Mouse capture is on unless PEPSI_CODE_MOUSE explicitly disables it."""
    return os.environ.get("PEPSI_CODE_MOUSE", "").strip().lower() not in {"0", "false", "no", "off"}


def enter_alternate_screen() -> None:
    _enable_windows_vt_processing()
    if _is_dumb_terminal():
        # Dumb terminals (e.g. 'linux' console, 'dumb', piped output)
        # don't support alternate screen or mouse tracking.
        return
    mouse_tracking = ENABLE_MOUSE_TRACKING if mouse_tracking_enabled() else ""
    sys.stdout.write(DISABLE_MOUSE_TRACKING + ENTER_ALT_SCREEN + ERASE_SCREEN_AND_HOME + mouse_tracking)
    sys.stdout.flush()


def exit_alternate_screen() -> None:
    if _is_dumb_terminal():
        return
    sys.stdout.write(DISABLE_MOUSE_TRACKING + EXIT_ALT_SCREEN)
    sys.stdout.flush()
    _restore_windows_console_mode()


def clear_screen() -> None:
    sys.stdout.write("\u001b[2J\u001b[H")
    sys.stdout.flush()
