"""Regression tests for the PepsiCode TUI visual redesign."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pepsicode.agents.trace import Activity
from pepsicode.core import session as sessions
from pepsicode.tui import app, chrome
from pepsicode.tui.chrome import (
    SPINNER_FRAMES,
    render_box,
    render_divider,
    render_pill,
    render_progress_bar,
    spinner_char,
    string_display_width,
    strip_ansi,
)
from pepsicode.tui.command_picker import CommandOption, CommandPicker, render_command_picker
from pepsicode.tui.input_parser import MouseEvent
from pepsicode.tui.provider_wizard import create_provider_wizard, render_provider_wizard
from pepsicode.tui.settings_picker import SettingOption, SettingsPicker, render_settings_picker
from pepsicode.tui.types import TranscriptEntry


@pytest.mark.parametrize("no_color", [False, True])
def test_brand_lime_palette_respects_no_color(no_color: bool) -> None:
    env = os.environ.copy()
    env.update(CLICOLOR_FORCE="1", TERM="xterm", PYTHONIOENCODING="utf-8")
    if no_color:
        env["NO_COLOR"] = "1"
    else:
        env.pop("NO_COLOR", None)
    code = (
        "import json; from pepsicode.tui import theme; "
        "print(json.dumps([theme.BRAND_LIME, theme.CODE_FG, theme.PLAN_ACCENT, theme.SURFACE_BG]))"
    )
    res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    tokens = json.loads(res.stdout)
    if no_color:
        assert tokens == ["", "", "", ""]
    else:
        assert tokens == ["\x1b[38;5;155m", "\x1b[38;5;117m", "\x1b[38;5;81m", "\x1b[48;5;235m"]


@pytest.mark.parametrize("width", [0, 1, 8, 16, 32, 80, 140])
def test_chrome_primitives_respect_cjk_widths(width: int) -> None:
    lines = render_box(
        ["中文长文本测试内容 abc", "第二行"],
        width,
        title="◆ 标题 Title",
        right_title="1/5",
        footer="↑/↓ 选择 · Esc 返回",
    )
    assert all(string_display_width(line) <= width for line in lines)
    if width >= 10:
        assert strip_ansi(lines[0]).startswith("╭") and strip_ansi(lines[0]).endswith("╮")
        assert strip_ansi(lines[-1]).startswith("╰") and strip_ansi(lines[-1]).endswith("╯")
        assert len({string_display_width(line) for line in lines}) == 1
    assert string_display_width(render_divider(width, "分组")) <= max(1, width)
    assert "█" in strip_ansi(render_progress_bar(50.0, width=6))
    assert "PLAN" in strip_ansi(render_pill("PLAN", ""))


def test_progress_bar_shows_subcell_fill_for_low_percentages() -> None:
    assert strip_ansi(render_progress_bar(0.0, width=6)) == "░░░░░░"
    low = strip_ansi(render_progress_bar(4.7, width=6))
    assert low == "▎░░░░░"
    assert string_display_width(low) == 6
    assert strip_ansi(render_progress_bar(50.0, width=6)) == "███░░░"
    assert strip_ansi(render_progress_bar(100.0, width=6)) == "██████"

    env = os.environ.copy()
    env.update(CLICOLOR_FORCE="1", TERM="xterm", PYTHONIOENCODING="utf-8")
    env.pop("NO_COLOR", None)
    code = (
        "import json; from pepsicode.tui.chrome import render_progress_bar, strip_ansi, string_display_width; "
        "bar = render_progress_bar(3.6, width=6); "
        "print(json.dumps([bar, strip_ansi(bar), string_display_width(bar)], ensure_ascii=False))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, encoding="utf-8", check=True
    )
    raw, plain, width = json.loads(res.stdout)
    assert "\x1b[48;5;237m" in raw
    assert "░" not in plain
    assert plain.startswith("▎") and width == 6


def test_home_hero_is_side_by_side_and_composer_animates_spinner(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PEPSI_CODE_SHARKS", "0")
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (100, 26))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (100, 26))
    args = SimpleNamespace(
        runtime={"model": "deepseek-v4-pro", "reasoningEffort": "high"},
        cwd="E:/Code/pepsicode",
        permissions=SimpleNamespace(is_plan_mode=False, access_mode=SimpleNamespace(value="auto")),
        messages=[],
    )
    state = app.ScreenState()
    app._render_home_screen(args, state, 100, 26)
    home_plain = strip_ansi("\n".join(state.frame_renderer._content))
    assert "pepsicode" in home_plain
    assert "模型" in home_plain and "deepseek-v4-pro" in home_plain
    assert "目录" in home_plain and "pepsicode" in home_plain
    assert "╭" in home_plain and "╰" in home_plain

    state.transcript = [TranscriptEntry(id=1, kind="user", body="hello")]
    state.is_busy = True
    state._animation_frame = 0
    app._render_stream_screen(args, state)
    frame0 = strip_ansi("\n".join(state.frame_renderer._content))
    assert spinner_char(0) in frame0 and "Thinking…" in frame0

    state._animation_frame = 1
    app._render_stream_screen(args, state)
    frame1 = strip_ansi("\n".join(state.frame_renderer._content))
    assert spinner_char(1) in frame1
    assert SPINNER_FRAMES[0] != SPINNER_FRAMES[1]
    capsys.readouterr()


def test_stream_header_and_hit_testing_with_activity_and_sidebar(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    session = sessions.create_new_session(str(tmp_path))
    node = session.agent_traces.create(agent_type="explore", name="Explore", task="检查渲染模块")
    session.agent_traces.activity(node.trace_id, "read", Activity("read", "tool", "读取 chrome.py"))

    entries = [
        TranscriptEntry(id=1, kind="user", body="fix"),
        TranscriptEntry(id=2, kind="tool", toolName="read_file", status="success", body="ok"),
        TranscriptEntry(id=3, kind="assistant", body="done"),
    ]
    args = SimpleNamespace(
        cwd=str(tmp_path),
        runtime={"model": "test-model"},
        permissions=SimpleNamespace(is_plan_mode=False, access_mode=SimpleNamespace(value="auto")),
    )
    state = app.ScreenState(session=session, transcript=entries)

    # Tall screen: top stream header appears at row 0 and shifts hit-test rows by +1.
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (132, 30))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (132, 30))
    app._render_stream_screen(args, state)
    capsys.readouterr()
    assert "pepsicode" in strip_ansi(state.frame_renderer._content[0])
    [(activity_row, group_id)] = state.activity_header_rows.items()
    assert activity_row >= 1 and group_id == 2

    # Clicking the shifted activity header row toggles expansion cleanly.
    for action in ("press", "release"):
        app._handle_selection_event(state, MouseEvent(action=action, x=4, y=activity_row), lambda: None)
    assert state.expanded_activity_groups == {2}

    # Clicking the shifted sidebar detail row still triggers the right agent trace.
    sidebar_y = next(y for y, act in state.agent_panel_actions.items() if act == ("detail", node.trace_id))
    assert sidebar_y >= 1
    for action in ("press", "release"):
        app._handle_selection_event(
            state, MouseEvent(action=action, x=state.agent_panel_start_col + 1, y=sidebar_y), lambda: None
        )
    assert node.trace_id in state.transcript[-1].body

    # Short screen (< 14 rows): top header is omitted to preserve transcript height.
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (40, 12))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (40, 12))
    app._render_stream_screen(args, state)
    capsys.readouterr()
    assert not strip_ansi(state.frame_renderer._content[0]).strip().startswith("pepsicode ·")


def test_markdown_fenced_code_block_has_uniform_width_and_handles_streaming() -> None:
    env = os.environ.copy()
    env.update(CLICOLOR_FORCE="1", TERM="xterm", PYTHONIOENCODING="utf-8")
    env.pop("NO_COLOR", None)
    code = (
        "import json; from pepsicode.tui.markdown import render_markdownish; "
        "from pepsicode.tui.chrome import string_display_width; "
        "closed = render_markdownish('```python\\na = 1\\nlonger_line = 222\\n```', width=50).splitlines(); "
        "streaming = render_markdownish('```python\\na = 1', width=50).splitlines(); "
        "print(json.dumps({'closed_w': [string_display_width(x) for x in closed], "
        "'closed_last': closed[-1], 'streaming_last': streaming[-1]}))"
    )
    res = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    assert len(set(data["closed_w"])) == 1
    assert data["closed_w"][0] <= 50
    assert "╰" in strip_ansi(data["closed_last"])
    assert "╰" not in strip_ansi(data["streaming_last"])


@pytest.mark.parametrize("cols,rows", [(32, 8), (100, 28)])
def test_modals_render_rounded_frames_within_bounds(cols: int, rows: int) -> None:
    sp = SettingsPicker(
        kind="model",
        model="m1",
        options=[SettingOption("m1", "Model One", "desc 1"), SettingOption("m2", "Model Two", "desc 2")],
        current="m1",
        selected=1,
    )
    cp = CommandPicker(
        title="选择位置",
        options=[CommandOption("第一项", "1", "描述"), CommandOption("第二项", "2", "描述")],
        selected=1,
        note="提示信息",
    )
    pw = create_provider_wizard("custom")
    for frame in (
        render_settings_picker(sp, cols, rows),
        render_command_picker(cp, cols, rows),
        render_provider_wizard(pw, cols, rows),
    ):
        assert len(frame) <= rows
        assert all(string_display_width(line) < cols for line in frame)
        joined = strip_ansi("\n".join(frame))
        assert "╭" in joined and "╰" in joined
