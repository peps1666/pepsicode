import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pepsicode.tui import render_banner, render_panel, render_permission_prompt, render_transcript
from pepsicode.tui.types import TranscriptEntry


def test_render_panel_contains_title() -> None:
    rendered = render_panel("Demo", "body")
    assert "Demo" in rendered
    assert "body" in rendered


def test_render_banner_includes_model() -> None:
    rendered = render_banner(
        {"model": "claude-test", "baseUrl": "https://api.anthropic.com"},
        "/tmp/demo",
        ["cwd: /tmp/demo"],
        {"transcriptCount": 1, "messageCount": 2, "skillCount": 3, "mcpCount": 4},
    )
    assert "claude-test" in rendered
    assert "api.anthropic.com" in rendered


def test_render_transcript_shows_tool_entry() -> None:
    transcript = [
        TranscriptEntry(id=1, kind="user", body="hi"),
        TranscriptEntry(id=2, kind="tool", body="done", toolName="read_file", status="success"),
    ]
    rendered = render_transcript(transcript, scroll_offset=0)
    assert "Read file" in rendered
    assert "done" in rendered


@pytest.mark.parametrize("path", ["src/a/very/long/directory/module.py", r"C:\\项目\\很长的目录\\module.py"])
def test_tool_header_keeps_filename_visible_for_long_paths(path) -> None:
    from pepsicode.tui.chrome import string_display_width, strip_ansi
    from pepsicode.tui.transcript import render_tool_activity

    entry = TranscriptEntry(
        id=1, kind="tool", body="", toolName="read_file", toolSummary=f"read_file path={path}", status="success"
    )
    header = render_tool_activity(entry, 38)[0]
    assert "module.py" in strip_ansi(header)
    assert "…" in strip_ansi(header)
    assert string_display_width(header) <= 37


def test_command_target_keeps_prefix_when_truncated() -> None:
    from pepsicode.tui.chrome import strip_ansi
    from pepsicode.tui.transcript import render_tool_activity

    entry = TranscriptEntry(
        id=1,
        kind="tool",
        body="",
        toolName="run_command",
        toolSummary="run_command git status --porcelain=2",
        status="running",
    )
    assert "$ git" in strip_ansi(render_tool_activity(entry, 28)[0])


def test_tool_target_preserves_filename_before_optional_fields() -> None:
    from pepsicode.tui.chrome import string_display_width, strip_ansi
    from pepsicode.tui.transcript import render_tool_activity

    entry = TranscriptEntry(
        id=1,
        kind="tool",
        body="",
        toolName="read_file",
        status="success",
        toolSummary="read_file path=src/very long/目录/module.py offset=100 limit=200",
    )
    for width in (36, 44, 80):
        header = render_tool_activity(entry, width)[0]
        assert "module.py" in strip_ansi(header)
        assert string_display_width(header) <= width - 1
    assert "offset=100 limit=200" in strip_ansi(render_tool_activity(entry, 80)[0])


@pytest.mark.parametrize("no_color", [False, True])
def test_tool_path_color_respects_terminal_color_settings(no_color) -> None:
    environment = os.environ.copy()
    environment.update(CLICOLOR_FORCE="1", TERM="xterm", PYTHONIOENCODING="utf-8")
    if no_color:
        environment["NO_COLOR"] = "1"
    else:
        environment.pop("NO_COLOR", None)
    code = (
        "from pepsicode.tui.transcript import render_tool_activity; "
        "from pepsicode.tui.types import TranscriptEntry; "
        "print(render_tool_activity(TranscriptEntry(id=1, kind='tool', body='', "
        "toolName='read_file', toolSummary='read_file path=src/module.py', status='success'), 80)[0])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", env=environment, check=True
    )
    assert "src/module.py" in result.stdout
    assert ("\x1b[36m" in result.stdout) is not no_color
    if no_color:
        assert "\x1b[32m" not in result.stdout


def test_render_transcript_shows_intermediate_collapse_phase() -> None:
    transcript = [
        TranscriptEntry(
            id=1,
            kind="tool",
            body="full output here",
            toolName="run_command",
            status="success",
            collapsePhase=1,
        ),
    ]

    rendered = render_transcript(transcript, scroll_offset=0)

    assert "Run command" in rendered
    assert "collapsing" in rendered


def test_render_transcript_shows_collapsed_summary_when_fully_collapsed() -> None:
    transcript = [
        TranscriptEntry(
            id=1,
            kind="tool",
            body="full output here",
            toolName="run_command",
            status="success",
            collapsed=True,
            collapsedSummary="short summary",
            collapsePhase=3,
        ),
    ]

    rendered = render_transcript(transcript, scroll_offset=0)

    assert "Run command" in rendered
    assert "short summary" in rendered
    assert "full output here" not in rendered


def test_render_permission_prompt_lists_choices() -> None:
    rendered = render_permission_prompt(
        {
            "summary": "Need approval",
            "details": ["target: demo.txt"],
            "choices": [{"key": "1", "label": "allow once"}],
        }
    )
    assert "Need approval" in rendered
    assert "allow once" in rendered


def test_command_approval_details_are_visible_and_scrollable(monkeypatch) -> None:
    from pepsicode.tui import chrome

    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (48, 16))
    command = "git status " + "long-argument-" * 15 + "END"
    request = {
        "kind": "command",
        "summary": "Need approval",
        "details": ["cwd: C:/project", f"command: {command}", "reason: process"],
        "scope": "exact command",
        "choices": [{"key": "y", "label": "allow once"}],
    }
    first = chrome.strip_ansi(chrome.render_permission_prompt(request))
    assert "cwd: C:/project" in first
    assert "command: git status" in first
    assert "lines hidden" not in first
    offset = chrome.get_permission_prompt_max_scroll_offset(request)
    assert offset > 0
    last = chrome.strip_ansi(chrome.render_permission_prompt(request, scroll_offset=offset))
    assert "END" in last
    assert "scope: exact command" in last


@pytest.mark.parametrize("width", [0, 1, 2, 3, 8])
def test_truncation_fits_small_column_budgets(width) -> None:
    from pepsicode.tui.chrome import string_display_width, truncate_plain

    assert string_display_width(truncate_plain("中文 long text", width)) <= width


def test_slash_menu_keeps_last_selection_visible(monkeypatch) -> None:
    from pepsicode.tui import chrome

    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (36, 12))
    commands = [SimpleNamespace(usage=f"/command{i}", description="示例命令") for i in range(25)]
    rendered = chrome.render_slash_menu(commands, 24, max_rows=5)
    assert "> /command24" in chrome.strip_ansi(rendered)
    assert len(rendered.splitlines()) <= 5
    assert all(chrome.string_display_width(line) <= 36 for line in rendered.splitlines())


def test_long_colored_text_keeps_styles_on_wrapped_rows() -> None:
    from pepsicode.tui.chrome import string_display_width, strip_ansi, wrap_text

    rendered = wrap_text("\x1b[31m" + "中文" * 15 + "\x1b[0m", 12)
    rows = rendered.splitlines()
    assert strip_ansi("".join(rows)) == "中文" * 15
    assert len(rows) == 5
    assert all(row.startswith("\x1b[31m") and row.endswith("\x1b[0m") for row in rows)
    assert all(string_display_width(row) <= 12 for row in rows)


@pytest.mark.parametrize(
    ("variables", "styles", "colors"),
    [
        ({}, False, False),
        ({"CLICOLOR_FORCE": "1"}, True, True),
        ({"CLICOLOR_FORCE": "1", "NO_COLOR": ""}, True, False),
        ({"CLICOLOR_FORCE": "1", "TERM": "dumb"}, False, False),
    ],
)
def test_theme_respects_terminal_capabilities(variables, styles, colors) -> None:
    environment = os.environ.copy()
    for key in ("NO_COLOR", "CLICOLOR_FORCE", "TERM"):
        environment.pop(key, None)
    environment.update(variables)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from pepsicode.tui import theme; "
            "from pepsicode.tui.markdown import render_markdownish; "
            "print(json.dumps([bool(theme.REVERSE), bool(theme.ACCENT), render_markdownish('# Hello')]))",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    reverse, accent, markdown = json.loads(result.stdout)
    assert reverse is styles
    assert accent is colors
    if not styles:
        assert "\x1b" not in markdown
