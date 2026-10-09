import re
from types import SimpleNamespace

import pytest

from pepsicode.permissions import PermissionManager
from pepsicode.tui.app import (
    _apply_tool_result_visual_state,
    _format_history,
    _mark_unfinished_tools,
    _render_stream_input,
    _render_stream_transcript,
    _save_transcript,
    summarize_tool_input,
    summarize_tool_output,
)
from pepsicode.tui.chrome import strip_ansi
from pepsicode.tui.transcript import format_transcript_text
from pepsicode.tui.types import TranscriptEntry


def _initial_frame_text(output: str) -> str:
    frame = strip_ansi(output).replace("\x1b[K", "")
    return re.sub(r"\x1b\[\d+;1H", "\n", frame).removeprefix("\n")


def test_summarize_tool_output_prefers_first_meaningful_line() -> None:
    output = "\n\nFILE: README.md\nOFFSET: 0\nEND: 100"
    assert summarize_tool_output("read_file", output).startswith("FILE: README.md")


def test_summarize_tool_output_truncates_long_lines() -> None:
    output = "x" * 400
    summary = summarize_tool_output("run_command", output)
    assert len(summary) < 200
    assert summary.endswith("...")


def test_format_history_shows_recent_entries_with_numbers() -> None:
    rendered = _format_history(["/help", "build parser", "/cmd pytest -q"], limit=2)
    assert rendered == "2. build parser\n3. /cmd pytest -q"


def test_save_transcript_writes_plain_text(tmp_path) -> None:
    state_entries = [
        TranscriptEntry(id=1, kind="user", body="hello"),
        TranscriptEntry(id=2, kind="assistant", body="world"),
    ]
    permissions = PermissionManager(str(tmp_path), prompt=lambda request: {"decision": "allow_once"})

    path = _save_transcript(
        type("State", (), {"transcript": state_entries})(),
        str(tmp_path),
        permissions,
        "logs/session.txt",
    )

    assert path.endswith("logs\\session.txt") or path.endswith("logs/session.txt")
    assert (tmp_path / "logs" / "session.txt").read_text(
        encoding="utf-8"
    ) == "you\n  hello\n\n---\n\nassistant\n  world"


def test_format_transcript_text_uses_clean_separator() -> None:
    rendered = format_transcript_text(
        [
            TranscriptEntry(id=1, kind="user", body="one"),
            TranscriptEntry(id=2, kind="assistant", body="two"),
        ]
    )

    assert "\n\n---\n\n" in rendered


def test_summarize_tool_input_formats_patch_file() -> None:
    summary = summarize_tool_input(
        "patch_file",
        {"path": "demo.txt", "replacements": [{"search": "a", "replace": "b"}, {"search": "c", "replace": "d"}]},
    )

    assert summary == "patch_file path=demo.txt replacements=2"


def test_mark_unfinished_tools_marks_running_entries_as_errors() -> None:
    state = type(
        "State",
        (),
        {
            "transcript": [
                TranscriptEntry(id=1, kind="tool", body="running", toolName="run_command", status="running")
            ],
            "recent_tools": [],
            "pending_tool_runs": {"run_command": [{"entry": "placeholder"}]},
            "active_tool": "run_command",
        },
    )()

    count = _mark_unfinished_tools(state)

    assert count == 1
    assert state.transcript[0].status == "error"
    assert "did not report a final result" in state.transcript[0].body
    assert state.recent_tools == [{"name": "run_command", "status": "error"}]
    assert state.pending_tool_runs == {}
    assert state.active_tool is None


def test_error_tool_entry_stays_expanded_for_visibility() -> None:
    entry = TranscriptEntry(id=1, kind="tool", body="boom", toolName="run_command", status="running")
    _apply_tool_result_visual_state(entry, "run_command", "boom", is_error=True)

    assert entry.status == "error"
    assert entry.collapsed is False
    assert entry.collapsedSummary is None


def test_success_tool_entry_collapses_to_summary() -> None:
    entry = TranscriptEntry(id=1, kind="tool", body="running", toolName="read_file", status="running")
    _apply_tool_result_visual_state(entry, "read_file", "FILE: README.md\nhello", is_error=False)

    assert entry.status == "success"
    assert entry.collapsed is True
    assert entry.collapsedSummary == "FILE: README.md"
    assert entry.collapsePhase == 3


def test_stream_input_uses_opencode_style_prompt_card() -> None:
    state = type("State", (), {"input": "", "cursor_offset": 0})()

    rendered = "\n".join(_render_stream_input(state, "claude-test", 100))
    plain = strip_ansi(rendered)

    assert ">" in plain
    assert "Ask anything..." in plain
    assert "claude-test" in plain


def test_stream_transcript_renders_conversation_timeline() -> None:
    entries = [
        TranscriptEntry(id=1, kind="user", body="fix tests"),
        TranscriptEntry(id=2, kind="assistant", body="Sure, I will check them."),
        TranscriptEntry(
            id=3,
            kind="tool",
            body="pytest passed",
            toolName="pytest",
            status="success",
            collapsed=True,
            collapsedSummary="pytest passed",
        ),
    ]

    rendered_lines, total = _render_stream_transcript(entries, scroll_offset=0, height=20, cols=100)
    rendered = "\n".join(rendered_lines)
    plain = strip_ansi(rendered)

    assert "fix tests" in plain
    assert "Sure, I will check them." in plain
    assert "pytest success" in plain
    assert "pytest passed" in plain


@pytest.mark.parametrize("cols,rows", [(32, 8), (32, 10), (40, 16), (96, 30)])
@pytest.mark.parametrize("command_menu", [False, True])
def test_home_keeps_composer_visible_in_small_windows(monkeypatch, capsys, cols, rows, command_menu) -> None:
    from pepsicode.tui import chrome
    from pepsicode.tui.app import ScreenState, _render_home_screen

    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (cols, rows))
    args = SimpleNamespace(runtime={"model": "test-model"}, cwd="E:/Code/demo")
    state = ScreenState(input="/" if command_menu else "", selected_slash_index=999)
    _render_home_screen(args, state, cols, rows)
    # Remove cursor movement/erase codes, leaving only the visible frame.
    frame = _initial_frame_text(capsys.readouterr().out)
    assert "test-model" in frame
    assert "demo" in frame
    assert "Ask anything..." in frame if not command_menu else "> /" in frame
    assert len(frame.splitlines()) <= rows
    assert all(chrome.string_display_width(line) <= cols for line in frame.splitlines())


@pytest.mark.parametrize("kind", ["user", "assistant", "progress", "tool"])
def test_stream_scroll_counts_physical_wrapped_rows(kind) -> None:
    from pepsicode.tui.chrome import string_display_width

    entries = [TranscriptEntry(id=1, kind=kind, body="中文内容" * 30, toolName="read_file", status="running")]
    lines, total = _render_stream_transcript(entries, scroll_offset=0, height=200, cols=32)
    assert total == len(lines)
    assert all("\n" not in line and string_display_width(line) <= 32 for line in lines)
    top, _ = _render_stream_transcript(entries, scroll_offset=total, height=4, cols=32)
    assert len(top) <= 4


def test_stream_screen_anchors_input_and_limits_menu(monkeypatch, capsys) -> None:
    from pepsicode.tui import app, chrome

    monkeypatch.setattr(app, "_get_terminal_size", lambda: (40, 12))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (40, 12))
    args = SimpleNamespace(
        runtime={"model": "test-model"}, cwd="E:/Code/demo", permissions=SimpleNamespace(is_plan_mode=False)
    )
    state = app.ScreenState(input="/", transcript=[TranscriptEntry(id=1, kind="user", body="hello")])
    app._render_stream_screen(args, state)
    frame = _initial_frame_text(capsys.readouterr().out)
    assert len(frame.splitlines()) == 12
    assert "demo" in frame.splitlines()[-1]
    assert "> /" in frame
