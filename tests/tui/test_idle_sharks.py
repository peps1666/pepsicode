from types import SimpleNamespace

import pytest

from pepsicode.tui import app
from pepsicode.tui.chrome import strip_ansi
from pepsicode.tui.idle_sharks import IdleSharks, compose_ocean, render_ocean
from pepsicode.tui.screen import FrameRenderer
from pepsicode.tui.types import TranscriptEntry


class Capture:
    selection = None

    def draw(self, frame, cols, rows):
        self.frame = frame


@pytest.fixture
def home(monkeypatch):
    monkeypatch.delenv("PEPSI_CODE_SHARKS", raising=False)
    args = SimpleNamespace(runtime={"model": "mock"}, cwd="E:/Code/demo", messages=[])
    state = app.ScreenState(frame_renderer=Capture())
    return args, state


def _occupied(rows):
    return [(x, y) for y, row in enumerate(rows) for x, char in enumerate(row) if char != " "]


def test_ocean_moves_with_bounded_block_output():
    first = render_ocean(100, 25, 0)
    assert len(first) == 25 and all(len(row) == 100 for row in first)
    assert first != render_ocean(100, 25, 2)
    assert first == render_ocean(100, 25, 0)
    assert len(_occupied(first)) > 40
    large = render_ocean(240, 80, 12)
    assert len(large) == 80 and all(len(row) == 240 for row in large)
    assert render_ocean(10000, 10000, 36000) == []
    for width, height in [(0, 0), (-1, 10), (49, 20), (80, 5)]:
        assert render_ocean(width, height, 0) == []


def test_shark_keeps_its_shape_and_stays_in_the_water():
    boxes = []
    for step in range(60):
        rows = render_ocean(100, 25, step * 0.5)
        cells = _occupied(rows)
        assert cells, "the shark should always be on screen"
        xs = [x for x, _ in cells]
        ys = [y for _, y in cells]
        assert min(xs) >= 0 and max(xs) < 100
        assert min(ys) >= 0 and max(ys) < 25
        boxes.append((min(xs), max(xs), min(ys), max(ys)))

    widths = [right - left for left, right, _, _ in boxes]
    heights = [bottom - top for _, _, top, bottom in boxes]
    # A body with fins is far bigger than a speck and never a full-screen smear.
    assert min(widths) >= 14 and max(widths) <= 50
    assert min(heights) >= 6 and max(heights) <= 24
    # It moves, and it patrols the width of the frame.
    assert len(set(boxes)) > 20
    assert min(left for left, _, _, _ in boxes) < 30
    assert max(right for _, right, _, _ in boxes) > 70


def test_clock_limits_idle_refresh_to_ten_fps():
    clock = IdleSharks()
    assert clock.due(100)
    clock.render(80, 8, 100)
    assert not clock.due(100.05)
    assert clock.due(100.11)


def test_home_motion_preserves_brand_and_composer(home, monkeypatch):
    args, state = home
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 100))
    app._render_home_screen(args, state, 100, 26)
    first = list(state.frame_renderer.frame)
    assert state.home_sharks_visible
    assert not app._home_sharks_tick_due(args, state, 100.05)
    assert app._home_sharks_tick_due(args, state, 100.11)
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 102))
    app._render_home_screen(args, state, 100, 26)
    second = state.frame_renderer.frame
    assert first[-4:] == second[-4:]
    assert first[:-4] != second[:-4]
    assert len(second) == 26
    assert all(len(strip_ansi(row)) <= 100 for row in second)
    monkeypatch.setenv("PEPSI_CODE_SHARKS", "0")
    app._render_home_screen(args, state, 100, 26)
    baseline = state.frame_renderer.frame
    for original, composed in zip(baseline, second):
        if strip_ansi(original).strip():
            assert strip_ansi(composed).startswith(strip_ansi(original))


@pytest.mark.parametrize("cols,rows", [(80, 24), (100, 26), (160, 40)])
def test_home_animation_does_not_add_ellipsis_at_right_edge(home, monkeypatch, capsys, cols, rows):
    args, state = home
    state.frame_renderer = FrameRenderer()
    for now in (100, 102, 105):
        monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: now))
        app._render_home_screen(args, state, cols, rows)
        assert state.home_sharks_visible
        for line in state.frame_renderer._content[:-4]:
            assert len(strip_ansi(line)) <= cols - 1
            assert not strip_ansi(line).endswith("...")
    capsys.readouterr()


@pytest.mark.parametrize("cols,rows", [(32, 24), (80, 12), (32, 8)])
def test_small_home_has_no_animation_or_refresh(home, cols, rows):
    args, state = home
    app._render_home_screen(args, state, cols, rows)
    assert not state.home_sharks_visible
    assert not app._home_sharks_tick_due(args, state, 100)
    assert state.home_sharks.started_at is None


def test_first_question_removes_ocean_and_stops_refresh(home, monkeypatch):
    args, state = home
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (100, 26))
    app._render_stream_screen(args, state)
    assert state.home_sharks_visible
    state.transcript.append(TranscriptEntry(id=1, kind="user", body="First question"))
    state.is_busy = True
    app._render_stream_screen(args, state)
    assert not state.home_sharks_visible
    assert not app._home_sharks_tick_due(args, state, 1000)
    assert any("First question" in strip_ansi(row) for row in state.frame_renderer.frame)
    state.is_busy = False
    assert not app._home_sharks_tick_due(args, state, 1000)
    # Even an older restored session lacking transcript entries has asked already.
    state.transcript.clear()
    args.messages = [{"role": "user", "content": "Earlier question"}]
    app._render_home_screen(args, state, 100, 26)
    assert not state.home_sharks_visible


@pytest.mark.parametrize("reason", ["command", "wizard", "picker", "resume", "approval", "busy", "exit", "disabled"])
def test_no_ocean_behind_menus_or_overlays(home, monkeypatch, reason):
    args, state = home
    if reason == "command":
        state.input = "/help"
    elif reason == "wizard":
        state.provider_wizard = object()
    elif reason == "picker":
        state.settings_picker = object()
    elif reason == "resume":
        state.resume_picker_sessions = []
    elif reason == "approval":
        state.pending_approval = object()
    elif reason == "busy":
        state.is_busy = True
    elif reason == "exit":
        state.exit_requested_at = 100
    else:
        monkeypatch.setenv("PEPSI_CODE_SHARKS", "0")
    assert not app._home_sharks_enabled(args, state)
    assert not app._home_sharks_tick_due(args, state, 1000)


def test_selecting_terminal_text_freezes_ocean(home, monkeypatch):
    args, state = home
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 100))
    app._render_home_screen(args, state, 100, 26)
    first = list(state.frame_renderer.frame)
    state.frame_renderer.selection = object()
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 102))
    assert not app._home_sharks_tick_due(args, state, 102)
    app._render_home_screen(args, state, 100, 26)
    assert first == state.frame_renderer.frame


def test_composer_and_protected_rows_mask_a_shark(monkeypatch):
    from pepsicode.tui import idle_sharks

    monkeypatch.setattr(
        idle_sharks,
        "_cells",
        lambda w, h, t: [[("@", "")] * w for _ in range(h)],
    )
    foreground = ["  \x1b[97mpepsicode\x1b[0m", "", "中文 input", "  > abc ", "status"]
    foreground += [""] * 7
    composed = compose_ocean(foreground, 30, 12, 0, {10, 11})
    assert composed[0].startswith(foreground[0])
    assert strip_ansi(composed[0]) == "  pepsicode" + "@" * 19
    assert strip_ansi(composed[1]) == "@" * 30
    assert composed[2].startswith(foreground[2])
    assert strip_ansi(composed[2]) == "中文 input" + "@" * 20
    assert composed[10:] == foreground[10:]
