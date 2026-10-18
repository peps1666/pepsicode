import ctypes
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pepsicode.tui import app, screen
from pepsicode.tui.input_parser import KeyEvent, MouseEvent, TextEvent, parse_input_chunk


def dispatch(state, event):
    app._handle_event(SimpleNamespace(), state, event, lambda: None, threading.Event(), {})


@pytest.mark.parametrize("event", [KeyEvent("c", True, False), TextEvent("c", True, False)])
def test_copy_is_prioritized_over_exit_for_repeated_ctrl_c(monkeypatch, capsys, event):
    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    state = app.ScreenState()
    state.frame_renderer.draw(["hello world"], 30, 3)
    capsys.readouterr()
    for action, x in [("press", 0), ("drag", 4), ("release", 4)]:
        dispatch(state, MouseEvent(action, x, 0))
    for _ in range(3):
        dispatch(state, event)
        assert state.exit_requested_at is None
        assert state.frame_renderer.selection is not None
    assert copied == ["hello"] * 4


def test_single_ctrl_c_without_observable_selection_does_not_exit(monkeypatch):
    # Native terminal selections (e.g. Shift+drag) do not send mouse events.
    state = app.ScreenState()
    monkeypatch.setattr(app.time, "monotonic", lambda: 10.0)
    dispatch(state, parse_input_chunk("\x03").events[0])
    assert state.exit_requested_at == 10.0
    with pytest.raises(SystemExit):
        dispatch(state, parse_input_chunk("\x03").events[0])


def test_exit_confirmation_expires_and_other_input_disarms_it(monkeypatch):
    state = app.ScreenState()
    clock = [10.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    ctrl_c = KeyEvent("c", True, False)
    dispatch(state, ctrl_c)
    clock[0] += 3
    dispatch(state, ctrl_c)
    assert state.exit_requested_at == 13.0
    dispatch(state, TextEvent("x", False, False))
    assert state.exit_requested_at is None and state.input == "x"
    dispatch(state, ctrl_c)
    assert state.exit_requested_at == 13.0


def test_exit_command_waits_for_enter():
    state = app.ScreenState(input="/exit", cursor_offset=5)
    with pytest.raises(SystemExit):
        dispatch(state, KeyEvent("return", False, False))


@pytest.mark.parametrize("body_raises", [False, True])
def test_windows_raw_mode_routes_ctrl_c_as_input_and_restores_modes(monkeypatch, body_raises):
    original_mode = 0x0200 | 0x0040 | 0x0007

    def read_mode(handle, target):
        target._obj.value = original_mode
        return 1

    kernel32 = SimpleNamespace(
        GetStdHandle=Mock(return_value=123),
        GetConsoleMode=Mock(side_effect=read_mode),
        SetConsoleMode=Mock(return_value=1),
        GetConsoleOutputCP=Mock(return_value=936),
        SetConsoleOutputCP=Mock(),
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    monkeypatch.setattr(screen, "_enable_windows_vt_processing", lambda: None)
    monkeypatch.setattr(app.sys, "platform", "win32")
    try:
        with app._RawModeContext():
            raw_mode = kernel32.SetConsoleMode.call_args.args[1]
            assert raw_mode & 0x0007 == 0  # processed, line and echo input
            assert raw_mode & 0x0040 == 0  # QuickEdit
            assert raw_mode & 0x0200  # retain VT sequences for mouse and arrows
            if body_raises:
                raise RuntimeError("test teardown")
    except RuntimeError:
        assert body_raises
    assert kernel32.SetConsoleMode.call_args.args == (123, original_mode)
    assert kernel32.SetConsoleOutputCP.call_args.args == (936,)


def test_leaving_tui_restores_original_windows_input_mode(monkeypatch, capsys):
    kernel32 = SimpleNamespace(GetStdHandle=Mock(return_value=123), SetConsoleMode=Mock())
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    monkeypatch.setattr(screen, "_original_input_mode", 7)
    monkeypatch.setattr(screen, "_vt_enabled", True)
    monkeypatch.setattr(screen, "_is_dumb_terminal", lambda: False)
    screen.exit_alternate_screen()
    capsys.readouterr()
    kernel32.SetConsoleMode.assert_called_once_with(123, 7)
    assert screen._original_input_mode is None and not screen._vt_enabled
