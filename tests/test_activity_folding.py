from types import SimpleNamespace

from pepsicode.tui import app, chrome
from pepsicode.tui.chrome import strip_ansi
from pepsicode.tui.input_parser import KeyEvent, MouseEvent
from pepsicode.tui.types import TranscriptEntry


def _turn(answer: str | None = "All tests pass.", *, tool_status: str = "success") -> list[TranscriptEntry]:
    entries = [
        TranscriptEntry(id=1, kind="user", body="fix tests"),
        TranscriptEntry(id=2, kind="progress", body="Let me look at the failing test."),
        TranscriptEntry(id=3, kind="tool", toolName="read_file", status="success", body="file body"),
        TranscriptEntry(id=4, kind="tool", toolName="run_command", status=tool_status, body="pytest output"),
    ]
    if answer is not None:
        entries.append(TranscriptEntry(id=5, kind="assistant", body=answer))
    return entries


def _plain(entries, **kwargs) -> str:
    return strip_ansi("\n".join(app._build_stream_transcript_lines(entries, 100, **kwargs)))


def test_live_activity_stays_expanded_until_the_answer_is_final():
    running = _plain(_turn(None, tool_status="running"))
    assert "Let me look at the failing test." in running and "Run command" in running
    assert "Activity" not in running

    # The final answer is still streaming in: keep showing the work.
    streaming = _plain(_turn(), streaming_entry_id=5)
    assert "Read file" in streaming and "Activity" not in streaming


def test_finished_turn_folds_activity_behind_one_header():
    folded = _plain(_turn())
    assert "▸ Activity  2 tools · 1 thought" in folded
    assert "Read file" not in folded and "Let me look" not in folded
    assert "fix tests" in folded and "All tests pass." in folded

    expanded = _plain(_turn(), expanded_groups={2})
    assert "▾ Activity" in expanded and "Read file" in expanded and "Let me look" in expanded


def test_folded_header_reports_failures():
    assert "1 failed" in _plain(_turn(tool_status="error"))


def _screen(monkeypatch, entries):
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (100, 30))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (100, 30))
    args = SimpleNamespace(
        runtime={"model": "test-model"}, cwd="E:/Code/demo", permissions=SimpleNamespace(is_plan_mode=False)
    )
    return args, app.ScreenState(transcript=entries)


def test_clicking_header_toggles_and_keeps_it_on_the_same_row(monkeypatch, capsys):
    args, state = _screen(monkeypatch, _turn())
    app._render_stream_screen(args, state)
    capsys.readouterr()
    [(row, group_id)] = state.activity_header_rows.items()
    assert group_id == 2

    for action in ("press", "release"):
        app._handle_selection_event(state, MouseEvent(action=action, x=4, y=row), lambda: None)
    assert state.expanded_activity_groups == {2}
    app._render_stream_screen(args, state)
    capsys.readouterr()
    assert state.activity_header_rows == {row: 2}
    assert "Read file" in strip_ansi("\n".join(state.frame_renderer._content))

    for action in ("press", "release"):
        app._handle_selection_event(state, MouseEvent(action=action, x=4, y=row), lambda: None)
    app._render_stream_screen(args, state)
    capsys.readouterr()
    assert state.expanded_activity_groups == set()
    assert "Read file" not in strip_ansi("\n".join(state.frame_renderer._content))


def test_dragging_across_header_selects_instead_of_toggling(monkeypatch, capsys):
    monkeypatch.setattr(app, "copy_to_clipboard", lambda text: None)
    args, state = _screen(monkeypatch, _turn())
    app._render_stream_screen(args, state)
    capsys.readouterr()
    [row] = state.activity_header_rows
    app._handle_selection_event(state, MouseEvent(action="press", x=2, y=row), lambda: None)
    app._handle_selection_event(state, MouseEvent(action="release", x=12, y=row), lambda: None)
    assert state.expanded_activity_groups == set()


def test_ctrl_o_toggles_the_latest_finished_turn(monkeypatch, capsys):
    second_turn = [
        TranscriptEntry(id=6, kind="user", body="and lint"),
        TranscriptEntry(id=7, kind="tool", toolName="run_command", status="success", body="ruff ok"),
        TranscriptEntry(id=8, kind="assistant", body="Lint is clean."),
    ]
    args, state = _screen(monkeypatch, _turn() + second_turn)
    app._render_stream_screen(args, state)
    capsys.readouterr()
    app._handle_normal_mode_event(args, state, KeyEvent(name="o", ctrl=True, meta=False), lambda: None)
    assert state.expanded_activity_groups == {7}
