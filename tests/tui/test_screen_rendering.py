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
        + terminal_screen.SAVE_DISABLE_ALTERNATE_SCROLL
        + terminal_screen.ENTER_ALT_SCREEN
        + terminal_screen.ERASE_SCREEN_AND_HOME
        + terminal_screen.ENABLE_MOUSE_TRACKING
        + "\x1b[?2004h"
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
    assert (
        capsys.readouterr().out
        == "\x1b[?2004l"
        + terminal_screen.DISABLE_MOUSE_TRACKING
        + terminal_screen.EXIT_ALT_SCREEN
        + terminal_screen.RESTORE_ALTERNATE_SCROLL
    )


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
        assert "BUILD auto" in lines[-2] and "test-model" in lines[-2]
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
    # Releasing keeps the selection highlighted without auto-copying.
    assert copied == []
    assert state.frame_renderer.selection is not None and not state.frame_renderer.selection.is_empty
    assert state.selection_notice is None

    assert app._handle_selection_event(state, KeyEvent(name="c", ctrl=True, meta=False), lambda: None)
    assert copied == ["line\nsecond"]
    assert state.selection_notice
    assert state.frame_renderer.selection is not None
    # Repeated copying must never fall through to the exit shortcut.
    assert app._handle_selection_event(state, KeyEvent(name="c", ctrl=True, meta=False), lambda: None)
    assert copied == ["line\nsecond", "line\nsecond"]

    # The next ordinary key clears the selection and notice and is still processed normally.
    assert not app._handle_selection_event(state, TextEvent(text="x", ctrl=False, meta=False), lambda: None)
    assert state.selection_notice is None
    assert state.frame_renderer.selection is None
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
    assert [line[:-1] for line in terminal.lines()[1:6]] == [line[:-1] for line in before[1:6]]
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
    assert any("first question" in line for line in terminal.lines()[:4])


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


@pytest.mark.parametrize("cols,rows", [(32, 8), (80, 24), (124, 32), (180, 50)])
def test_home_composer_fills_width_and_stays_at_bottom(monkeypatch, capsys, cols, rows):
    from pepsicode.tui import app

    args = _stream_args()
    state = app.ScreenState()
    terminal = TerminalGrid(cols, rows)
    for user_input in ("", "/", "/permissions", "中文输入" * cols):
        state.input = user_input
        state.cursor_offset = len(user_input)
        app._render_home_screen(args, state, cols, rows)
        terminal.apply(capsys.readouterr().out)
        lines = terminal.lines()
        assert len(lines) == rows
        if cols >= 36 and rows >= 11:
            assert lines[rows - 5].strip() == f"╭{'─' * (cols - 6)}╮"
            assert lines[rows - 4].strip().startswith("│  >") and lines[rows - 4].strip().endswith("│")
            assert lines[rows - 3].strip() == f"╰{'─' * (cols - 6)}╯"
            menu_end = rows - 5
        else:
            assert lines[rows - 4].strip() == "─" * (cols - 4)
            assert lines[rows - 3].startswith("   > ")
            menu_end = rows - 4
        assert "BUILD auto" in lines[-2]
        assert "demo" in lines[-1]
        assert all(string_display_width(line) <= cols for line in lines)
        if user_input == "/permissions":
            # The selected command remains visible directly above the input.
            assert "/permissions" in "\n".join(lines[:menu_end])


def test_home_resize_reflows_input_and_clears_old_positions(monkeypatch, capsys):
    from pepsicode.tui import app

    args = _stream_args()
    state = app.ScreenState(input="/permissions", cursor_offset=len("/permissions"))
    app._render_home_screen(args, state, 80, 24)
    capsys.readouterr()
    for cols, rows in ((140, 40), (40, 10)):
        app._render_home_screen(args, state, cols, rows)
        terminal = TerminalGrid(cols, rows)
        terminal.apply(capsys.readouterr().out)
        lines = terminal.lines()
        if cols >= 36 and rows >= 11:
            assert lines[rows - 5].strip() == f"╭{'─' * (cols - 6)}╮"
            assert "> /permissions" in lines[rows - 4]
            assert lines[rows - 3].strip() == f"╰{'─' * (cols - 6)}╯"
        else:
            assert lines[rows - 4].strip() == "─" * (cols - 4)
            assert "> /permissions" in lines[rows - 3]
        assert "BUILD auto" in lines[-2]
        assert "demo" in lines[-1]


def test_scrollbar_renders_only_when_scrollable_and_respects_final_column(monkeypatch, capsys):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.types import TranscriptEntry

    monkeypatch.setattr(app, "_get_terminal_size", lambda: (40, 14))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (40, 14))
    args = _stream_args()

    # Short transcript fits within the viewport: no scrollbar.
    short_state = app.ScreenState(transcript=[TranscriptEntry(id=1, kind="assistant", body="short")])
    app._render_stream_screen(args, short_state)
    capsys.readouterr()
    assert short_state.scrollbar_rect is None

    # Long transcript overflows viewport: scrollbar appears on cols - 2 (leaving cols - 1 unused).
    long_state = app.ScreenState(
        transcript=[TranscriptEntry(id=1, kind="assistant", body="\n".join(f"line {n}" for n in range(60)))]
    )
    app._render_stream_screen(args, long_state)
    terminal = TerminalGrid(40, 14)
    terminal.apply(capsys.readouterr().out)
    assert long_state.scrollbar_rect is not None
    sb_col, sb_top, sb_height, thumb_top, thumb_height = long_state.scrollbar_rect
    assert sb_col == 38
    assert sb_height == long_state.transcript_view_height
    assert thumb_top + thumb_height == sb_height
    sb_glyphs = [terminal.cells[sb_top + i][sb_col] for i in range(sb_height)]
    assert "┃" in sb_glyphs and "│" in sb_glyphs
    assert all(terminal.cells[sb_top + i][39] == " " for i in range(sb_height))


def test_scrollbar_track_click_and_thumb_drag_scroll_transcript(monkeypatch, capsys):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.input_parser import MouseEvent
    from pepsicode.tui.types import TranscriptEntry

    monkeypatch.setattr(app, "_get_terminal_size", lambda: (40, 16))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (40, 16))
    args = _stream_args()
    state = app.ScreenState(
        transcript=[TranscriptEntry(id=1, kind="assistant", body="\n".join(f"line {n}" for n in range(80)))]
    )
    app._render_stream_screen(args, state)
    capsys.readouterr()
    assert state.scrollbar_rect is not None
    sb_col, sb_top, sb_height, thumb_top, thumb_height = state.scrollbar_rect
    assert state.transcript_scroll_offset == 0

    def rerender():
        app._render_stream_screen(args, state)
        capsys.readouterr()

    # Clicking near the top of the track jumps scroll offset toward the top without starting a text selection.
    app._handle_selection_event(state, MouseEvent(action="press", x=sb_col, y=sb_top), rerender, args=args)
    assert state.scrollbar_dragging
    assert state.transcript_selection is None and state.frame_renderer.selection is None
    assert state.transcript_scroll_offset > 0
    max_offset = app._get_stream_max_scroll_offset(args, state)
    assert state.transcript_scroll_offset == max_offset

    # Dragging down to the bottom returns to scroll offset 0; releasing stops dragging.
    app._handle_selection_event(
        state, MouseEvent(action="drag", x=sb_col, y=sb_top + sb_height - 1), rerender, args=args
    )
    assert state.transcript_scroll_offset == 0
    app._handle_selection_event(
        state, MouseEvent(action="release", x=sb_col, y=sb_top + sb_height - 1), rerender, args=args
    )
    assert not state.scrollbar_dragging

    # Grabbing the thumb itself and dragging upward scrolls smoothly without jumping first.
    _, _, _, thumb_top, _ = state.scrollbar_rect
    app._handle_selection_event(state, MouseEvent(action="press", x=sb_col, y=sb_top + thumb_top), rerender, args=args)
    assert state.transcript_scroll_offset == 0
    app._handle_selection_event(state, MouseEvent(action="drag", x=sb_col, y=sb_top + 1), rerender, args=args)
    assert 0 < state.transcript_scroll_offset <= max_offset
    app._handle_selection_event(state, MouseEvent(action="release", x=sb_col, y=sb_top + 1), rerender, args=args)
    assert not state.scrollbar_dragging


@pytest.mark.parametrize(
    "shortcut",
    [
        "\x03",  # Ctrl+C
        "\x1b[67;6u",  # Ctrl+Shift+C (CSI u)
        "\x1b[27;6;67~",  # Ctrl+Shift+C (modifyOtherKeys)
        "\x1b[2;5~",  # Ctrl+Insert
        "\x1bc",  # Alt+C
    ],
)
def test_transcript_selection_copies_only_on_shortcuts_and_clears_on_escape(monkeypatch, capsys, shortcut):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.input_parser import KeyEvent, MouseEvent, parse_input_chunk
    from pepsicode.tui.types import TranscriptEntry

    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (40, 14))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (40, 14))
    args = _stream_args()
    state = app.ScreenState(transcript=[TranscriptEntry(id=1, kind="assistant", body="alpha beta\ngamma delta")])
    app._render_stream_screen(args, state)
    capsys.readouterr()

    # Locate the screen rows containing "alpha beta" and "gamma delta".
    lines = [strip_ansi(line) for line in state.frame_renderer._content]
    row_alpha = next(i for i, line in enumerate(lines) if "alpha beta" in line)
    col_start = lines[row_alpha].index("alpha")

    def rerender():
        app._render_stream_screen(args, state)
        capsys.readouterr()

    app._handle_selection_event(state, MouseEvent(action="press", x=col_start, y=row_alpha), rerender, args=args)
    app._handle_selection_event(state, MouseEvent(action="drag", x=col_start + 9, y=row_alpha + 1), rerender, args=args)
    app._handle_selection_event(
        state, MouseEvent(action="release", x=col_start + 9, y=row_alpha + 1), rerender, args=args
    )

    assert copied == []
    assert state.transcript_selection is not None and not state.transcript_selection.is_empty
    assert state.frame_renderer.row_highlights

    shortcut_event = parse_input_chunk(shortcut).events[0]
    assert app._handle_selection_event(state, shortcut_event, rerender, args=args)
    assert copied == ["alpha beta\n  gamma delt"]
    assert state.selection_notice == f"Copied {len(copied[0])} chars"
    assert state.transcript_selection is not None

    # Escape clears the active selection without leaking to normal mode.
    assert app._handle_selection_event(state, KeyEvent(name="escape", ctrl=False, meta=False), rerender, args=args)
    assert state.transcript_selection is None
    assert state.frame_renderer.row_highlights is None
    assert state.selection_notice is None


def test_cross_viewport_drag_autoscrolls_and_copies_offscreen_lines(monkeypatch, capsys):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.input_parser import KeyEvent, MouseEvent, WheelEvent
    from pepsicode.tui.types import TranscriptEntry

    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (40, 14))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (40, 14))
    args = _stream_args()
    state = app.ScreenState(
        transcript=[TranscriptEntry(id=1, kind="assistant", body="\n".join(f"item {n:02d}" for n in range(40)))]
    )
    app._render_stream_screen(args, state)
    capsys.readouterr()

    def rerender():
        app._render_stream_screen(args, state)
        capsys.readouterr()

    lines = [strip_ansi(line) for line in state.frame_renderer._content]
    row_38 = next(i for i, line in enumerate(lines) if "item 38" in line)
    col_end = lines[row_38].index("item 38") + len("item 38") - 1

    # Start dragging near the bottom of the transcript, then drag up past the top edge.
    app._handle_selection_event(state, MouseEvent(action="press", x=col_end, y=row_38), rerender, args=args)
    assert state.transcript_scroll_offset == 0
    app._handle_selection_event(state, MouseEvent(action="drag", x=2, y=state.transcript_top_row), rerender, args=args)
    assert state.transcript_scroll_offset > 0
    scroll_after_first_drag = state.transcript_scroll_offset

    # Holding the pointer above/on the top edge auto-scrolls continuously on idle ticks.
    app._handle_selection_event(state, MouseEvent(action="drag", x=2, y=0), rerender, args=args)
    now = state.selection_autoscroll_at
    for _ in range(5):
        now += 0.1
        if app._tick_selection_autoscroll(args, state, now):
            rerender()
    assert state.transcript_scroll_offset > scroll_after_first_drag

    # Mouse wheel while dragging keeps the selection alive and extends it further up.
    scroll_before_wheel = state.transcript_scroll_offset
    assert not app._handle_selection_event(state, WheelEvent(direction="up"), rerender, args=args)
    assert app._handle_normal_mode_wheel(args, state, WheelEvent(direction="up"), rerender)
    assert state.transcript_scroll_offset > scroll_before_wheel
    assert state.transcript_selection is not None

    # Release does not auto-copy; row_highlights skips the scroll banner row and scrollbar column.
    app._handle_selection_event(
        state, MouseEvent(action="release", x=2, y=state.transcript_top_row + 1), rerender, args=args
    )
    assert copied == []
    assert state.frame_renderer.row_highlights
    assert state.transcript_top_row not in state.frame_renderer.row_highlights
    sb_col = state.scrollbar_rect[0]
    assert all(span[1] <= sb_col for span in state.frame_renderer.row_highlights.values())

    # Copy shortcut copies all selected lines across the scrolled range without banner/scrollbar glyphs.
    assert app._handle_selection_event(state, KeyEvent(name="c", ctrl=True, meta=False), rerender, args=args)
    assert len(copied) == 1
    text = copied[0]
    assert "item 38" in text
    assert text.splitlines()[0].strip().startswith("item ")
    assert len(text.splitlines()) > state.transcript_view_height
    assert "│" not in text and "┃" not in text and "↑" not in text


def test_busy_spinner_ticks_on_timer_without_token_output():
    from pepsicode.tui import app

    state = app.ScreenState()
    assert not app._tick_busy_spinner(state, 10.0)
    assert state._animation_frame == 0

    state.is_busy = True
    assert app._tick_busy_spinner(state, 10.0)
    assert state._animation_frame == 1
    assert not app._tick_busy_spinner(state, 10.05)
    assert state._animation_frame == 1
    assert app._tick_busy_spinner(state, 10.11)
    assert state._animation_frame == 2

    state.pending_approval = app.PendingApproval(request={"summary": "ok", "choices": []}, resolve=lambda _: None)
    assert not app._tick_busy_spinner(state, 10.50)
    assert state._animation_frame == 2


@pytest.mark.parametrize("size", [(80, 24), (30, 10)])
def test_busy_indicator_distinguishes_thinking_and_working_phases(monkeypatch, capsys, size):
    from pepsicode.tui import app, chrome
    from pepsicode.tui.types import TranscriptEntry

    monkeypatch.setattr(app, "_get_terminal_size", lambda: size)
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: size)
    args = _stream_args()
    state = app.ScreenState(transcript=[TranscriptEntry(id=1, kind="user", body="hello")], is_busy=True)

    def render_plain() -> str:
        app._render_stream_screen(args, state)
        capsys.readouterr()
        return strip_ansi("\n".join(state.frame_renderer._content))

    # Waiting for first token -> Thinking…
    assert "Thinking…" in render_plain()

    # Active thinking stream -> Thinking…
    state.thinking_entry_id = 2
    assert "Thinking…" in render_plain()

    # Streaming answer text -> Working…
    state.thinking_entry_id = None
    state.streaming_entry_id = 3
    plain = render_plain()
    assert "Working…" in plain and "Thinking…" not in plain

    # Running a tool -> Working…
    state.streaming_entry_id = None
    state.active_tool = "run_command"
    state.active_tool_count = 1
    plain = render_plain()
    assert "Working…" in plain and "Thinking…" not in plain

    # Concurrent tools where one finished (active_tool cleared, one still running) -> Working…
    state.active_tool = None
    state.active_tool_count = 1
    plain = render_plain()
    assert "Working…" in plain and "Thinking…" not in plain

    # All tools finished, awaiting next model turn -> back to Thinking…
    state.active_tool_count = 0
    assert "Thinking…" in render_plain()
