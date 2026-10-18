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
