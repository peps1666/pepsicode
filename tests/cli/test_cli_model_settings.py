"""Verify terminal model commands affect the next real adapter request."""

import asyncio
import io
import json
import time
from unittest.mock import Mock

import pytest

from pepsicode.cli.cli_commands import find_matching_slash_commands, format_slash_commands, try_handle_local_command
from pepsicode.context.context_manager import ContextManager
from pepsicode.core.session import AutosaveManager, create_new_session, load_session
from pepsicode.core.state import create_app_store
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.main import _handle_local_command
from pepsicode.permissions import PermissionManager, PermissionMode
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.app import ScreenState, TtyAppArgs, _handle_input, _restore_session_into_state


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.tui.app.save_history_entries", lambda history: None)
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path)
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "sessions")
    runtime = {"model": "deepseek-flash", "baseUrl": "https://api.deepseek.com/anthropic", "apiKey": "test"}
    runtime.update(
        {
            "_catalogCheckedAt": time.monotonic(),
            "models": [
                {
                    "id": identifier,
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
        messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "previous turn"}],
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path)),
        context_manager=ContextManager(model=runtime["model"]),
    )
    session = create_new_session(str(tmp_path))
    state = ScreenState(session=session, autosave=AutosaveManager(session), app_state=create_app_store())
    return args, state


def command(args, text):
    return try_handle_local_command(
        text,
        tools=args.tools,
        permissions=args.permissions,
        runtime=args.runtime,
        model_adapter=args.model,
        messages=args.messages,
        context_manager=args.context_manager,
    )


def test_tty_switches_live_adapter_and_persists_session(terminal):
    args, state = terminal
    original_runtime, original_messages = args.runtime, args.messages
    args.messages.append({"role": "assistant_thinking", "blocks": [{"type": "thinking", "signature": "old"}]})
    _handle_input(args, state, lambda: None, "/model deepseek-v4-pro")
    _handle_input(args, state, lambda: None, "/effort max")
    assert args.runtime is original_runtime is args.model.runtime
    assert args.messages is original_messages
    assert [message["role"] for message in args.messages] == ["system", "user"]
    assert state.app_state.get_state().model == "deepseek-v4-pro"
    body = json.loads(args.model._build_stream_request(args.runtime["model"], "sys", []).data)
    assert body["model"] == "deepseek-v4-pro"
    assert body["thinking"] == {"type": "enabled"}
    assert body["output_config"] == {"effort": "max"}
    state.autosave.force_save()
    saved = load_session(state.session.session_id)
    assert saved.model_settings == {"model": "deepseek-v4-pro", "reasoningEffort": "max"}
    assert saved.messages[-1]["content"] == "previous turn"
    command(args, "/model deepseek-flash")
    _restore_session_into_state(args, state, saved)
    assert args.runtime["model"] == "deepseek-v4-pro"
    assert args.runtime["reasoningEffort"] == "max"
    assert args.context_manager.model == "deepseek-v4-pro"


def test_pipe_command_handler_updates_next_request(terminal):
    args, _ = terminal

    def piped(text):
        return _handle_local_command(
            text,
            args.tools,
            args.permissions,
            runtime=args.runtime,
            model_adapter=args.model,
            messages=args.messages,
            context_manager=args.context_manager,
        )

    assert "next turn" in piped("/model deepseek-v4-pro")
    assert "next turn" in piped("/effort low")
    request = json.loads(args.model._build_request(args.runtime["model"], "sys", []).data)
    assert request["model"] == "deepseek-v4-pro"
    assert request["output_config"] == {"effort": "low"}
    assert "reasoning effort: low" in piped("/effort")
    assert "current model: deepseek-v4-pro" in piped("/model")


def test_main_piped_input_switches_settings_between_turns(terminal, monkeypatch, capsys):
    import pepsicode.main as main_module

    args, _ = terminal
    monkeypatch.setattr("sys.argv", ["pepsicode"])
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("/model deepseek-v4-pro\n/effort max\nfirst question\n/effort off\nsecond question\n/exit\n"),
    )
    monkeypatch.delenv("PEPSI_CODE_MODEL_MODE", raising=False)
    monkeypatch.setattr("pepsicode.logging_config.setup_logging", lambda **kwargs: None)
    monkeypatch.setattr(main_module, "load_runtime_config", lambda cwd: args.runtime)
    monkeypatch.setattr(main_module, "create_default_tool_registry", lambda *a, **kw: args.tools)
    monkeypatch.setattr(main_module, "maybe_handle_management_command", lambda *a: False)
    hook = Mock()
    hook.drain_notifications.return_value = []
    monkeypatch.setattr(main_module, "create_hook_engine", lambda *a: hook)
    monkeypatch.setattr(main_module, "load_history_entries", lambda: [])
    monkeypatch.setattr(main_module, "save_history_entries", lambda history: None)
    monkeypatch.setattr(main_module, "build_session_prompt", lambda *a, **kw: "system")
    monkeypatch.setattr("pepsicode.context.context_manager.save_context_state", lambda manager: None)
    monkeypatch.setattr("pepsicode.context.memory.create_memory_manager", lambda cwd: Mock())
    monkeypatch.setattr(
        "pepsicode.worktree.WorktreeManager", Mock(return_value=Mock(restore_session=Mock(return_value=None)))
    )
    monkeypatch.setattr("pepsicode.tui.screen._restore_windows_console_mode", lambda: None)
    requests = []

    def run_turn(**kwargs):
        adapter = kwargs["model"]
        body = json.loads(adapter._build_request(adapter.runtime["model"], "sys", []).data)
        requests.append(body)
        return kwargs["messages"] + [{"role": "assistant", "content": "test response"}]

    monkeypatch.setattr(main_module, "run_agent_turn", run_turn)
    main_module.main()
    assert [request["model"] for request in requests] == ["deepseek-v4-pro", "deepseek-v4-pro"]
    assert requests[0]["output_config"] == {"effort": "max"}
    assert requests[1]["thinking"] == {"type": "disabled"}
    assert "output_config" not in requests[1]
    assert capsys.readouterr().out.count("test response") == 2


def test_model_list_and_effort_discovery_do_not_mutate_settings(terminal, monkeypatch):
    args, _ = terminal
    writer = Mock()
    monkeypatch.setattr("pepsicode.cli.model_commands.save_mini_code_settings", writer)
    previous = dict(args.runtime)
    assert "deepseek-v4-pro" in command(args, "/model list")
    assert "allowed levels: default, off, low, high, max" in command(args, "/effort")
    assert args.runtime == previous
    writer.assert_not_called()
    assert any("/effort" in usage for usage in find_matching_slash_commands("/ef"))
    assert "/effort" in format_slash_commands()


@pytest.mark.parametrize(
    "text", ["/effort unknown", "/effort medium", "/effort --save", "/model a b", "/model list --save"]
)
def test_invalid_command_keeps_active_settings(terminal, monkeypatch, text):
    args, _ = terminal
    writer = Mock()
    monkeypatch.setattr("pepsicode.cli.model_commands.save_mini_code_settings", writer)
    previous = dict(args.runtime)
    output = command(args, text)
    assert "usage:" in output or "unchanged" in output
    assert args.runtime == previous
    writer.assert_not_called()


def test_settings_are_session_only_unless_save_requested(terminal, monkeypatch):
    args, _ = terminal
    writer = Mock()
    monkeypatch.setattr("pepsicode.cli.model_commands.save_mini_code_settings", writer)
    command(args, "/model deepseek-v4-pro")
    command(args, "/effort high")
    writer.assert_not_called()
    assert "Saved as defaults" in command(args, "/effort max --save")
    writer.assert_called_once_with({"model": "deepseek-v4-pro", "reasoningEffort": "max"})
    assert args.runtime["reasoningEffort"] == "max"


def test_save_error_does_not_change_live_model(terminal, monkeypatch):
    args, _ = terminal
    monkeypatch.setattr("pepsicode.cli.model_commands.save_mini_code_settings", Mock(side_effect=OSError("read only")))
    previous = dict(args.runtime)
    assert "unchanged: read only" in command(args, "/model deepseek-v4-pro --save")
    assert args.runtime == previous


def test_model_change_resets_effort_and_unknown_model_rejects_depth(terminal):
    args, _ = terminal
    # Use a gateway where capabilities cannot be inferred from the official host.
    args.runtime["baseUrl"] = "https://gateway.example"
    command(args, "/effort max")
    command(args, "/model provider-alias")
    assert args.runtime["reasoningEffort"] == "default"
    assert "not supported" in command(args, "/effort high")
    assert "allowed levels: default" in command(args, "/effort")


def test_busy_and_mock_terminal_cannot_switch(terminal):
    args, state = terminal
    state.is_busy = True
    _handle_input(args, state, lambda: None, "/model deepseek-v4-pro")
    assert args.runtime["model"] == "deepseek-flash"
    state.is_busy = False
    args.model = MockModelAdapter()
    assert "unavailable" in command(args, "/effort max")
    assert args.runtime.get("reasoningEffort", "default") == "default"


def test_plan_mode_allows_queries_but_blocks_settings(terminal):
    args, _ = terminal
    args.permissions.mode = PermissionMode.PLAN
    assert "deepseek-v4-pro" in command(args, "/model list")
    assert "allowed levels" in command(args, "/effort")
    assert "current model" in command(args, "/model")
    for text in ["/model deepseek-v4-pro", "/effort max", "/effort\tmax", "/effort max --save"]:
        assert "Plan mode denied" in command(args, text)
    assert args.runtime["model"] == "deepseek-flash"


def test_status_uses_live_session_configuration(terminal):
    args, _ = terminal
    command(args, "/effort low")
    assert "reasoning effort: low" in command(args, "/status")


def test_obsolete_session_profile_does_not_prevent_resume(terminal):
    args, state = terminal
    args.runtime["baseUrl"] = "https://gateway.example"
    saved = create_new_session(args.cwd)
    saved.messages = [{"role": "user", "content": "saved conversation"}]
    saved.model_settings = {"model": "removed-alias", "reasoningEffort": "max"}
    _restore_session_into_state(args, state, saved)
    assert args.runtime["model"] == "deepseek-flash"
    assert args.messages == saved.messages


def test_desktop_slash_commands_share_settings_and_notify_client(terminal):
    args, state = terminal
    session = ClientSession(args.cwd)
    session.runtime, session.model, session.messages = args.runtime, args.model, args.messages
    session.tools, session.permissions = args.tools, args.permissions
    session.context_manager, session.session_data = args.context_manager, state.session
    events = []

    async def emit(name, data):
        events.append((name, data))

    async def run():
        return await PepsiCodeServer()._run_turn({"input": "/effort max"}, session, emit, asyncio.get_running_loop())

    assert "next turn" in asyncio.run(run())["output"]
    assert session.runtime["reasoningEffort"] == "max"
    assert events[0] == ("model/settings", session.model_settings())
    assert events[-1] == ("turn/end", {"status": "ok"})
