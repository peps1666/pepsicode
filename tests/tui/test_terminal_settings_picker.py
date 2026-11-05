"""Exercise keyboard pickers and recovery from an inherited incompatible model."""

import json
import time
from unittest.mock import Mock

import pytest

from pepsicode.config import load_effective_settings, load_runtime_config
from pepsicode.context.context_manager import ContextManager
from pepsicode.core.session import AutosaveManager, create_new_session
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.permissions import PermissionManager, PermissionMode
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.app import ScreenState, TtyAppArgs, _handle_input, _handle_normal_mode_event, _render_stream_screen
from pepsicode.tui.chrome import string_display_width, strip_ansi
from pepsicode.tui.input_parser import KeyEvent, TextEvent
from pepsicode.tui.settings_picker import create_settings_picker, render_settings_picker


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.tui.app.save_history_entries", lambda history: None)
    runtime = {
        "model": "deepseek-v4-pro",
        "baseUrl": "https://api.deepseek.com/anthropic",
        "apiKey": "dummy",
        "reasoningEffort": "high",
    }
    runtime.update(
        {
            "_catalogCheckedAt": time.monotonic(),
            "_catalogAuthoritative": True,
            "_discoveredModels": [
                {
                    "id": identifier,
                    "name": identifier,
                    "reasoningMode": "effort",
                    "reasoningLevels": ["low", "high", "max"],
                    "supportsThinkingOff": True,
                }
                for identifier in ["deepseek-flash", "deepseek-v4-pro"]
            ],
        }
    )
    tools = ToolRegistry([])
    args = TtyAppArgs(
        runtime=runtime,
        tools=tools,
        model=AnthropicModelAdapter(runtime, tools),
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path)),
        context_manager=ContextManager(model=runtime["model"]),
        messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "previous"}],
    )
    session = create_new_session(str(tmp_path))
    return args, ScreenState(session=session, autosave=AutosaveManager(session))


def key(args, state, name):
    _handle_normal_mode_event(args, state, KeyEvent(name=name, ctrl=False, meta=False), lambda: None)


def test_effort_opens_all_concrete_options_with_current_check(terminal, monkeypatch):
    args, state = terminal
    request = Mock(side_effect=AssertionError("no model call when choosing settings"))
    monkeypatch.setattr("urllib.request.urlopen", request)
    _handle_input(args, state, lambda: None, "/effort")
    picker = state.settings_picker
    assert [option.value for option in picker.options] == ["default", "off", "low", "high", "max"]
    assert picker.options[picker.selected].value == "high"
    frame = strip_ansi("\n".join(render_settings_picker(picker, 110, 24)))
    for label in ["默认", "关闭思考", "低", "高", "最高", "√ 当前", "Enter 应用"]:
        assert label in frame
    assert "[level]" not in frame
    key(args, state, "down")
    key(args, state, "return")
    assert state.settings_picker is None
    assert args.runtime["reasoningEffort"] == "max"
    assert state.session.model_settings["reasoningEffort"] == "max"
    _handle_input(args, state, lambda: None, "/effort")
    assert state.settings_picker.options[state.settings_picker.selected].value == "max"
    request.assert_not_called()


def test_selecting_slash_command_opens_picker_without_a_placeholder(terminal):
    args, state = terminal
    state.input, state.cursor_offset = "/ef", 3
    key(args, state, "return")
    assert state.settings_picker.kind == "effort"
    assert state.input == ""


def test_escape_leaves_settings_unchanged(terminal):
    args, state = terminal
    _handle_input(args, state, lambda: None, "/effort")
    key(args, state, "down")
    key(args, state, "escape")
    assert state.settings_picker is None
    assert args.runtime["reasoningEffort"] == "high"


def test_save_is_explicit_and_keeps_picker_open_on_failure(terminal, monkeypatch):
    args, state = terminal
    writer = Mock(side_effect=OSError("read only"))
    monkeypatch.setattr("pepsicode.cli.model_commands.save_mini_code_settings", writer)
    _handle_input(args, state, lambda: None, "/effort")
    key(args, state, "down")
    _handle_normal_mode_event(args, state, TextEvent(text="s", ctrl=False, meta=False), lambda: None)
    key(args, state, "return")
    assert "read only" in state.settings_picker.error
    assert args.runtime["reasoningEffort"] == "high"
    writer.side_effect = None
    key(args, state, "return")
    writer.assert_called_with({"model": "deepseek-v4-pro", "reasoningEffort": "max"})
    assert state.settings_picker is None


def test_model_recovery_preserves_unsent_question_and_avoids_bad_request(terminal, monkeypatch):
    args, state = terminal
    args.runtime.update(model="opus", modelSource="C:/Users/test/.claude/settings.json")
    request = Mock()
    monkeypatch.setattr("urllib.request.urlopen", request)
    _handle_input(args, state, lambda: None, "帮我检查这个项目")
    assert state.settings_picker.kind == "model"
    assert ".claude/settings.json" in state.settings_picker.notice
    assert [option.value for option in state.settings_picker.options] == ["deepseek-flash", "deepseek-v4-pro"]
    key(args, state, "down")
    key(args, state, "return")
    assert args.runtime["model"] == "deepseek-v4-pro"
    assert state.input == "帮我检查这个项目"
    assert args.messages[-1]["content"] == "previous"
    request.assert_not_called()


def test_model_picker_applies_model_and_resets_effort(terminal):
    args, state = terminal
    _handle_input(args, state, lambda: None, "/model")
    assert state.settings_picker.kind == "model"
    key(args, state, "down")
    key(args, state, "return")
    assert args.runtime["model"] == "deepseek-flash"
    assert args.runtime["reasoningEffort"] == "default"


def test_custom_model_lists_only_declared_levels(terminal):
    args, state = terminal
    args.runtime.update(
        {
            "baseUrl": "https://gateway.example",
            "model": "custom",
            "models": [
                {
                    "id": "custom",
                    "reasoningMode": "effort",
                    "reasoningLevels": ["medium", "xhigh"],
                    "supportsThinkingOff": False,
                }
            ],
        }
    )
    args.runtime["_discoveredModels"] = []
    args.runtime["_catalogAuthoritative"] = False
    _handle_input(args, state, lambda: None, "/effort")
    assert [option.value for option in state.settings_picker.options] == ["default", "medium", "xhigh"]


def test_plan_mode_picker_cannot_apply_changes(terminal):
    args, state = terminal
    args.permissions.mode = PermissionMode.PLAN
    _handle_input(args, state, lambda: None, "/effort")
    key(args, state, "down")
    key(args, state, "return")
    assert "Plan mode denied" in state.settings_picker.error
    assert args.runtime["reasoningEffort"] == "high"


@pytest.mark.parametrize("cols,rows", [(32, 8), (40, 12), (110, 24)])
def test_picker_fits_small_terminal_and_keeps_selection_visible(terminal, cols, rows):
    args, _ = terminal
    picker = create_settings_picker(args.runtime, "effort")
    picker.selected = len(picker.options) - 1
    frame = render_settings_picker(picker, cols, rows)
    assert len(frame) <= rows
    assert all(string_display_width(line) < cols for line in frame)
    assert "最高" in strip_ansi("\n".join(frame))


def test_frame_renders_picker_instead_of_manual_input(terminal, monkeypatch, capsys):
    from pepsicode.tui import app

    args, state = terminal
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (110, 24))
    _handle_input(args, state, lambda: None, "/effort")
    _render_stream_screen(args, state)
    frame = strip_ansi(capsys.readouterr().out)
    assert "选择思考强度" in frame
    assert "√ 当前" in frame
    assert "Ask anything" not in frame


@pytest.mark.parametrize("stream", [False, True])
def test_known_invalid_official_model_never_reaches_network(terminal, monkeypatch, stream):
    args, _ = terminal
    args.runtime.update(model="opus", modelSource=".claude/settings.json")
    request = Mock()
    monkeypatch.setattr("urllib.request.urlopen", request)
    with pytest.raises(RuntimeError, match="模型列表中没有.*opus"):
        list(args.model.next_stream(args.messages)) if stream else args.model.next(args.messages)
    request.assert_not_called()


def test_runtime_records_inherited_model_source(tmp_path, monkeypatch):
    import pepsicode.config as config

    compat, local = tmp_path / "claude.json", tmp_path / "settings.json"
    compat.write_text(json.dumps({"model": "opus", "env": {"ANTHROPIC_API_KEY": "dummy"}}))
    local.write_text("{}")
    monkeypatch.setattr(config, "CLAUDE_SETTINGS_PATH", compat)
    monkeypatch.setattr(config, "PEPSI_CODE_SETTINGS_PATH", local)
    monkeypatch.delenv("PEPSI_CODE_MODEL", raising=False)
    assert load_effective_settings(tmp_path)["_modelSource"] == str(compat)
    assert load_runtime_config(tmp_path)["modelSource"] == str(compat)
