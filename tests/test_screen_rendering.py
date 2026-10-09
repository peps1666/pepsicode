import re
from types import SimpleNamespace

import pytest

from pepsicode.tui.chrome import char_display_width, string_display_width, strip_ansi
from pepsicode.tui.input import render_input_prompt
from pepsicode.tui.screen import FrameRenderer


class TerminalGrid:
    """Apply renderer output with raw-mode newline/autowrap semantics."""

    def __init__(self, cols, rows):
        self.cols = cols
        self.cells = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0

    def apply(self, output):
        pos = 0
        while pos < len(output):
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
