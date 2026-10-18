import pytest

from pepsicode.tui.app import ScreenState, _finish_tool_entry, _push_transcript_entry, _render_stream_transcript
from pepsicode.tui.chrome import string_display_width, strip_ansi
from pepsicode.tui.transcript import format_transcript_text, render_tool_activity
from pepsicode.tui.types import TranscriptEntry


def test_completed_tool_preserves_operation_target_and_full_export():
    state = ScreenState()
    entry_id = _push_transcript_entry(
        state, kind="tool", toolName="read_file", status="running", body="read_file path=src/main.py"
    )
    output = "FILE: src/main.py\n" + "source code\n" * 50
    _finish_tool_entry(state, entry_id, "success", output)
    entry = state.transcript[0]
    lines = [strip_ansi(line) for line in render_tool_activity(entry, 80)]
    assert "Read file" in lines[0] and "src/main.py" in lines[0] and "done" in lines[0]
    assert "path=" not in lines[0]
    assert len(lines) == 2
    assert output.rstrip() in format_transcript_text([entry]).replace("\n  ", "\n")
    assert "Input: read_file path=src/main.py" in format_transcript_text([entry])


def test_failed_tool_stays_visible_even_if_previously_collapsed():
    state = ScreenState(
        transcript=[
            TranscriptEntry(id=1, kind="tool", toolName="run_command", body="pytest", status="running", collapsed=True)
        ]
    )
    _finish_tool_entry(state, 1, "error", "Permission denied\nTry a different path")
    entry = state.transcript[0]
    assert not entry.collapsed
    assert entry.collapsedSummary is None
    entry.collapsed = True  # Old saved sessions may have collapsed errors.
    rendered = strip_ansi("\n".join(render_tool_activity(entry, 80)))
    assert "failed" in rendered and "Permission denied" in rendered and "Try a different path" in rendered


@pytest.mark.parametrize("cols", [8, 16, 32, 80])
@pytest.mark.parametrize("status", ["running", "success", "error"])
def test_tool_activity_bounds_physical_rows_and_unicode_width(cols, status):
    entry = TranscriptEntry(
        id=1,
        kind="tool",
        toolName="run_command",
        status=status,
        toolSummary="run_command " + "中文路径" * 80,
        body="中文输出" * 1000,
    )
    lines = render_tool_activity(entry, cols)
    assert all(string_display_width(line) <= cols for line in lines)
    assert len(lines) <= 10
    if status == "running":
        assert len(lines) == 1
    else:
        assert "…" in strip_ansi(lines[-1])


def test_stream_and_panel_use_same_tool_rows():
    entry = TranscriptEntry(id=1, kind="tool", toolName="read_file", status="running", body="read_file path=README.md")
    lines, _ = _render_stream_transcript([entry], 0, 20, 80)
    assert lines[:-1] == render_tool_activity(entry, 80)


def test_old_transcript_entry_without_summary_still_renders():
    entry = TranscriptEntry(
        **{"id": 1, "kind": "tool", "toolName": "read_file", "status": "success", "body": "legacy output"}
    )
    assert "legacy output" in strip_ansi("\n".join(render_tool_activity(entry, 80)))
