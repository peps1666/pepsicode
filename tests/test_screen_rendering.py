import re
from types import SimpleNamespace

import pytest

from pepsicode.tui.chrome import char_display_width, string_display_width, strip_ansi
from pepsicode.tui.input import render_input_prompt
from pepsicode.tui.screen import FrameRenderer


@pytest.fixture
def terminal_screen(monkeypatch):
    from pepsicode.tui import screen

    # Exercise emitted terminal modes without changing the host console.
    monkeypatch.setattr(screen, "_enable_windows_vt_processing", lambda: None)
    monkeypatch.setattr(screen, "_is_dumb_terminal", lambda: False)
    monkeypatch.delenv("PEPSI_CODE_MOUSE", raising=False)
    return screen


@pytest.mark.parametrize("value", [None, "", "1", "true", " on "])
def test_mouse_capture_is_on_by_default_for_wheel_and_drag(terminal_screen, monkeypatch, capsys, value):
    if value is not None:
        monkeypatch.setenv("PEPSI_CODE_MOUSE", value)
    terminal_screen.enter_alternate_screen()
    output = capsys.readouterr().out
    assert output == (
        terminal_screen.DISABLE_MOUSE_TRACKING
        + terminal_screen.ENTER_ALT_SCREEN
        + terminal_screen.ERASE_SCREEN_AND_HOME
        + terminal_screen.ENABLE_MOUSE_TRACKING
    )
    # Button-event tracking reports drags so the app can select text.
    assert "\x1b[?1002h" in terminal_screen.ENABLE_MOUSE_TRACKING


@pytest.mark.parametrize("value", ["0", "false", "NO", " off "])
def test_mouse_capture_can_be_disabled_for_native_selection(terminal_screen, monkeypatch, capsys, value):
    monkeypatch.setenv("PEPSI_CODE_MOUSE", value)
    terminal_screen.enter_alternate_screen()
    assert terminal_screen.ENABLE_MOUSE_TRACKING not in capsys.readouterr().out


def test_exit_disables_mouse_capture(terminal_screen, monkeypatch, capsys):
    terminal_screen.enter_alternate_screen()
    capsys.readouterr()
    terminal_screen.exit_alternate_screen()
    assert capsys.readouterr().out == terminal_screen.DISABLE_MOUSE_TRACKING + terminal_screen.EXIT_ALT_SCREEN


def test_dumb_terminal_does_not_emit_mouse_modes(terminal_screen, monkeypatch, capsys):
    monkeypatch.setenv("PEPSI_CODE_MOUSE", "1")
    monkeypatch.setattr(terminal_screen, "_is_dumb_terminal", lambda: True)
    terminal_screen.enter_alternate_screen()
    terminal_screen.exit_alternate_screen()
    assert capsys.readouterr().out == ""


class TerminalGrid:
    """Apply renderer output with raw-mode newline/autowrap semantics."""

    def __init__(self, cols, rows):
        self.cols = cols
        self.cells = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0

    def apply(self, output):
        pos = 0
        while pos < len(output):
            mode = re.match(r"\x1b\[\?\d+[hl]", output[pos:])
            if mode:
                # DEC private modes such as synchronized output draw nothing.
                pos += len(mode[0])
                continue
            match = re.match(r"\x1b\[([0-9;]*)([mHK])", output[pos:])
            if match:
                parameters, command = match.groups()
                if command == "H":
                    row, col = map(int, parameters.split(";"))
                    self.row, self.col = row - 1, col - 1
                elif command == "K":
                    self.cells[self.row][self.col :] = [" "] * (self.cols - self.col)
                pos += len(match[0])
                continue
            char = output[pos]
            assert char not in "\r\n", "Newline output can scroll or retain the wrong column in raw mode"
            width = char_display_width(char)
            assert self.col + width < self.cols, "Writing the final column may trigger terminal autowrap"
            if width:
                self.cells[self.row][self.col] = char
                if width == 2:
                    self.cells[self.row][self.col + 1] = ""
                self.col += width
            pos += 1

    def lines(self):
        return ["".join(line).rstrip() for line in self.cells]


def test_unchanged_frame_does_not_write(capsys):
    renderer = FrameRenderer()
    renderer.draw(["header", "body", "input"], 32, 4)
    capsys.readouterr()
    renderer.draw(["header", "body", "input"], 32, 4)
    assert capsys.readouterr().out == ""


def test_only_changed_row_is_painted_before_erasing_suffix(capsys):
    renderer = FrameRenderer()
    renderer.draw(["header", "long body", "input"], 32, 4)
    terminal = TerminalGrid(32, 4)
    terminal.apply(capsys.readouterr().out)
    renderer.draw(["header", "short", "input"], 32, 4)
    output = capsys.readouterr().out
    assert re.findall(r"\x1b\[(\d+);1H", output) == ["2"]
    assert output.index("short") < output.index("\x1b[K")
    assert "\x1b[2J" not in output and "\x1b[J" not in output
    terminal.apply(output)
    assert terminal.lines() == ["header", "short", "input", ""]


def test_removed_rows_are_cleared_without_screen_erase(capsys):
    renderer = FrameRenderer()
    terminal = TerminalGrid(32, 4)
    renderer.draw(["header", "old menu", "old footer"], 32, 4)
    terminal.apply(capsys.readouterr().out)
    renderer.draw(["header"], 32, 4)
    terminal.apply(capsys.readouterr().out)
    assert terminal.lines() == ["header", "", "", ""]


def test_resize_invalidates_previous_frame(capsys):
    renderer = FrameRenderer()
    renderer.draw(["same"], 40, 6)
    capsys.readouterr()
    renderer.draw(["same"], 24, 4)
    output = capsys.readouterr().out
    assert len(re.findall(r"\x1b\[\d+;1H", output)) == 4
    terminal = TerminalGrid(24, 4)
    terminal.apply(output)
    assert terminal.lines() == ["same", "", "", ""]


def test_bottom_row_never_reaches_autowrap_column(capsys):
    renderer = FrameRenderer()
    terminal = TerminalGrid(12, 3)
    renderer.draw(["x" * 12] * 3, 12, 3)
    terminal.apply(capsys.readouterr().out)
    assert all(string_display_width(line) <= 11 for line in terminal.lines())


@pytest.mark.parametrize("text", ["0123456789" * 12, "中文输入内容" * 20, "prefix\nmultiline\ttext" * 10])
@pytest.mark.parametrize("position", ["start", "middle", "end"])
def test_long_input_keeps_cursor_visible_without_losing_input(monkeypatch, text, position):
    from pepsicode.tui import input as input_ui

    monkeypatch.setattr(input_ui, "REVERSE", "\x1b[7m")
    monkeypatch.setattr(input_ui, "BOLD", "\x1b[1m")
    monkeypatch.setattr(input_ui, "RESET", "\x1b[0m")
    offset = {"start": 0, "middle": len(text) // 2, "end": len(text)}[position]
    rendered = render_input_prompt(text, offset, width=26)
    cursor = re.search(r"\x1b\[7m\x1b\[1m(.)\x1b\[0m", rendered)
    assert cursor is not None
    expected = text[offset] if offset < len(text) else " "
    assert cursor[1] == expected.translate(str.maketrans({"\n": "↵", "\r": "↵", "\t": " "}))
    assert string_display_width(rendered) <= 26
    assert "\n" not in rendered and "\r" not in rendered
    assert "..." not in strip_ansi(rendered)


def test_streaming_updates_keep_footer_and_input_on_their_rows(monkeypatch, capsys):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.types import TranscriptEntry

    monkeypatch.setattr(app, "_get_terminal_size", lambda: (32, 12))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (32, 12))
    args = SimpleNamespace(
        runtime={"model": "test-model"},
        cwd="E:/Code/demo",
        permissions=SimpleNamespace(is_plan_mode=False),
    )
    entry = TranscriptEntry(id=1, kind="assistant", body="")
    state = app.ScreenState(input="很长的中文输入" * 10, cursor_offset=70, transcript=[entry])
    terminal = TerminalGrid(32, 12)
    for _ in range(100):
        entry.body += "中文内容"
        app._render_stream_screen(args, state)
        terminal.apply(capsys.readouterr().out)
        lines = terminal.lines()
        assert "demo" in lines[-1]
        assert "test-model" in lines[-2]
        assert ">" in lines[-3] and "..." not in lines[-3]


def test_display_width_is_consistent_for_combining_marks_and_hangul():
    text = "e\u0301한글"
    assert string_display_width(text) == sum(char_display_width(char) for char in text) == 5


def test_tabbed_output_is_expanded_before_wrapping():
    from pepsicode.tui.chrome import wrap_text

    rows = wrap_text("\tlong_identifier\t中文内容", 12).splitlines()
    assert all("\t" not in row and string_display_width(row) <= 12 for row in rows)


def test_stream_resize_reflows_content_and_repaints_all_rows(monkeypatch, capsys):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.types import TranscriptEntry

    size = [80, 24]
    monkeypatch.setattr(app, "_get_terminal_size", lambda: tuple(size))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: tuple(size))
    args = SimpleNamespace(
        runtime={"model": "test-model"}, cwd="E:/Code/demo", permissions=SimpleNamespace(is_plan_mode=False)
    )
    state = app.ScreenState(transcript=[TranscriptEntry(id=1, kind="assistant", body="中文长回复" * 60)])
    app._render_stream_screen(args, state)
    capsys.readouterr()
    size[:] = [32, 12]
    app._render_stream_screen(args, state)
    output = capsys.readouterr().out
    terminal = TerminalGrid(32, 12)
    terminal.apply(output)
    assert len(re.findall(r"\x1b\[\d+;1H", output)) == 12
    assert "demo" in terminal.lines()[-1]


def _stream_args():
    return SimpleNamespace(
        runtime={"model": "test-model"}, cwd="E:/Code/demo", permissions=SimpleNamespace(is_plan_mode=False)
    )


def test_frame_updates_are_wrapped_in_synchronized_output(capsys):
    from pepsicode.tui import screen

    renderer = FrameRenderer()
    renderer.draw(["a", "b"], 20, 3)
    output = capsys.readouterr().out
    assert output.startswith(screen.BEGIN_SYNCHRONIZED_UPDATE)
    assert output.endswith(screen.END_SYNCHRONIZED_UPDATE)


def test_sgr_mouse_reports_press_drag_release_and_wheel():
    from pepsicode.tui.input_parser import MouseEvent, WheelEvent, parse_input_chunk

    events = parse_input_chunk("\x1b[<0;5;3M\x1b[<32;9;4M\x1b[<0;9;4m\x1b[<64;1;1M\x1b[<65;1;1M\x1b[<2;1;1M").events
    assert events == [
        MouseEvent(action="press", x=4, y=2),
        MouseEvent(action="drag", x=8, y=3),
        MouseEvent(action="release", x=8, y=3),
        WheelEvent(direction="up"),
        WheelEvent(direction="down"),
    ]


def test_legacy_mouse_reports_press_and_release():
    from pepsicode.tui.input_parser import MouseEvent, parse_input_chunk

    events = parse_input_chunk("\x1b[M" + chr(32) + chr(38) + chr(35) + "\x1b[M" + chr(35) + chr(40) + chr(35)).events
    assert events == [MouseEvent(action="press", x=5, y=2), MouseEvent(action="release", x=7, y=2)]


def test_selection_is_highlighted_and_copied_as_plain_text(capsys):
    from pepsicode.tui.screen import Selection

    renderer = FrameRenderer()
    renderer.selection = Selection(anchor=(0, 2), focus=(1, 3))
    renderer.draw(["\x1b[1mab中文\x1b[0mxyz", "hello world"], 30, 3)
    terminal = TerminalGrid(30, 3)
    output = capsys.readouterr().out
    terminal.apply(output)
    assert terminal.lines()[:2] == ["ab中文xyz", "hello world"]
    assert "\x1b[7m" in output
    assert renderer.selected_text() == "中文xyz\nhell"


def test_backwards_drag_selects_the_same_text():
    from pepsicode.tui.screen import Selection

    renderer = FrameRenderer()
    renderer.draw(["one two three"], 30, 2)
    renderer.selection = Selection(anchor=(0, 6), focus=(0, 4))
    assert renderer.selected_text() == "two"


def test_drag_copies_selection_and_ctrl_c_copies_instead_of_quitting(monkeypatch, capsys):
    from pepsicode.tui import app
    from pepsicode.tui.input_parser import KeyEvent, MouseEvent, TextEvent

    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    state = app.ScreenState()
    state.frame_renderer.draw(["first line", "second line"], 30, 3)
    capsys.readouterr()

    for action, x, y in [("press", 6, 0), ("drag", 2, 1), ("release", 5, 1)]:
        assert app._handle_selection_event(state, MouseEvent(action=action, x=x, y=y), lambda: None)
    assert copied == ["line\nsecond"]
    assert state.selection_notice

    assert app._handle_selection_event(state, KeyEvent(name="c", ctrl=True, meta=False), lambda: None)
    assert copied == ["line\nsecond", "line\nsecond"]
    assert state.frame_renderer.selection is not None
    # Repeated copying must never fall through to the exit shortcut.
    assert app._handle_selection_event(state, KeyEvent(name="c", ctrl=True, meta=False), lambda: None)
    assert copied == ["line\nsecond"] * 3

    # The next ordinary key clears the notice and is still processed normally.
    assert not app._handle_selection_event(state, TextEvent(text="x", ctrl=False, meta=False), lambda: None)
    assert state.selection_notice is None
    assert not app._handle_selection_event(state, KeyEvent(name="c", ctrl=True, meta=False), lambda: None)


def test_click_without_drag_copies_nothing(monkeypatch):
    from pepsicode.tui import app
    from pepsicode.tui.input_parser import MouseEvent

    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    state = app.ScreenState()
    app._handle_selection_event(state, MouseEvent(action="press", x=1, y=1), lambda: None)
    app._handle_selection_event(state, MouseEvent(action="release", x=1, y=1), lambda: None)
    assert copied == [] and state.frame_renderer.selection is None


def test_scrolled_back_view_stays_put_while_output_streams(monkeypatch, capsys):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.types import TranscriptEntry

    monkeypatch.setattr(app, "_get_terminal_size", lambda: (40, 12))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (40, 12))
    args = _stream_args()
    entry = TranscriptEntry(id=1, kind="assistant", body="\n".join(f"line {n}" for n in range(60)))
    state = app.ScreenState(transcript=[entry])
    app._render_stream_screen(args, state)
    assert app._scroll_transcript_by(args, state, 20)
    app._render_stream_screen(args, state)
    terminal = TerminalGrid(40, 12)
    terminal.apply(capsys.readouterr().out)
    before = terminal.lines()[:6]

    entry.body += "\n" + "\n".join(f"new {n}" for n in range(10))
    app._render_stream_screen(args, state)
    terminal.apply(capsys.readouterr().out)
    assert terminal.lines()[1:6] == before[1:6]
    assert state.user_scrolled_away


def test_scrolling_reaches_the_first_transcript_line(monkeypatch, capsys):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.types import TranscriptEntry

    monkeypatch.setattr(app, "_get_terminal_size", lambda: (40, 12))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (40, 12))
    args = _stream_args()
    state = app.ScreenState(
        transcript=[
            TranscriptEntry(id=1, kind="user", body="first question"),
            TranscriptEntry(id=2, kind="assistant", body="\n".join(f"line {n}" for n in range(60))),
        ]
    )
    app._render_stream_screen(args, state)
    while app._scroll_transcript_by(args, state, 3):
        app._render_stream_screen(args, state)
    terminal = TerminalGrid(40, 12)
    terminal.apply(capsys.readouterr().out)
    assert any("You" in line for line in terminal.lines()[:3])


def test_finished_tool_output_is_never_drawn_expanded():
    from pepsicode.tui import app
    from pepsicode.tui.types import TranscriptEntry

    entry = TranscriptEntry(id=1, kind="tool", toolName="run_command", status="running", body="ls")
    state = app.ScreenState(transcript=[entry])
    app._finish_tool_entry(state, 1, "success", "a.txt\n" + "x\n" * 40)
    assert entry.collapsed and entry.collapsedSummary == "a.txt" and entry.status == "success"


def test_permission_prompt_keeps_transcript_on_screen(monkeypatch, capsys):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.types import TranscriptEntry

    monkeypatch.setattr(app, "_get_terminal_size", lambda: (60, 24))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (60, 24))
    args = _stream_args()
    state = app.ScreenState(transcript=[TranscriptEntry(id=1, kind="user", body="please list files")])
    app._render_stream_screen(args, state)
    terminal = TerminalGrid(60, 24)
    terminal.apply(capsys.readouterr().out)
    state.pending_approval = app.PendingApproval(
        request={
            "summary": "Run command: ls",
            "choices": [{"key": "1", "label": "Allow once", "decision": "allow_once"}],
        },
        resolve=lambda _: None,
    )
    app._render_screen(args, state)
    output = capsys.readouterr().out
    terminal.apply(output)
    text = "\n".join(terminal.lines())
    assert "please list files" in text and "Allow once" in text
    # Only the composer area changes; the transcript rows are not repainted.
    assert "\x1b[1;1H" not in output and "\x1b[2;1H" not in output
