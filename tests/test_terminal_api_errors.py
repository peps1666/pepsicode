"""Provider validation errors must not compact context or corrupt the terminal UI."""

import io
import json
import logging
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pepsicode.context.context_manager import ContextManager
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter, ContextOverflowError, _is_context_overflow
from pepsicode.logging_config import get_logger, setup_logging
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.app import ScreenState, TtyAppArgs, _handle_input, _render_stream_screen
from pepsicode.tui.chrome import strip_ansi

INVALID_MODEL = "The supported API model names are deepseek-flash, deepseek-v4-pro, but you passed opus."


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (400, {"error": {"message": INVALID_MODEL}}, False),
        (400, {"error": {"message": "thinking options type cannot be disabled when reasoning_effort is set"}}, False),
        (400, {"error": {"message": "max_tokens must be greater than thinking.budget_tokens"}}, False),
        (400, {"error": {"type": "invalid_request_error", "message": "Invalid API key"}}, False),
        (400, {"error": {"message": "prompt is too long: 210000 tokens > 200000 maximum"}}, True),
        (400, {"error": {"message": "This model's maximum context length is 128000 tokens"}}, True),
        (400, {"error": {"message": "input tokens exceed the limit"}}, True),
        (400, {"error": {"message": "request too large"}}, True),
        (400, {"error": {"code": "context_length_exceeded"}}, True),
        (400, {"error": {"type": "prompt_too_long"}}, True),
        (400, {"error": {"type": [], "code": {}}}, False),
        (400, {}, False),
        (413, {}, True),
        (401, {"error": {"message": "prompt too long"}}, False),
    ],
)
def test_context_overflow_requires_evidence(status, payload, expected):
    assert _is_context_overflow(payload, status) is expected


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("message,overflow", [(INVALID_MODEL, False), ("prompt is too long", True)])
def test_http_400_is_classified_in_both_request_paths(monkeypatch, stream, message, overflow):
    error = urllib.error.HTTPError(
        "https://provider.example/v1/messages",
        400,
        "Bad Request",
        {},
        io.BytesIO(json.dumps({"error": {"message": message}}).encode()),
    )
    request = Mock(side_effect=error)
    monkeypatch.setattr("urllib.request.urlopen", request)
    adapter = AnthropicModelAdapter(
        {"model": "opus", "baseUrl": "https://provider.example", "apiKey": "test"}, ToolRegistry([])
    )
    with pytest.raises(ContextOverflowError if overflow else RuntimeError, match=message):
        list(adapter.next_stream([])) if stream else adapter.next([])
    assert request.call_count == 1


@pytest.fixture
def isolated_logging(tmp_path, monkeypatch):
    import pepsicode.logging_config as config

    logger = logging.getLogger("pepsicode")
    previous_handlers, previous_level, previous_propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = []
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path)
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "pepsicode.log")
    try:
        yield config.LOG_FILE
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.handlers = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


def test_file_only_logging_does_not_escape_to_root_console(isolated_logging, capsys):
    # Simulate an embedding process that already has a console handler.
    root = logging.getLogger()
    console = logging.StreamHandler()
    root.addHandler(console)
    try:
        setup_logging(log_to_console=False)
        get_logger("agent_loop").error("diagnostic stays in file")
    finally:
        root.removeHandler(console)
        console.close()
    assert capsys.readouterr().err == ""
    assert "diagnostic stays in file" in isolated_logging.read_text(encoding="utf-8")


def test_disabled_logging_does_not_use_last_resort_console(isolated_logging, capsys):
    setup_logging(log_to_file=False, log_to_console=False)
    get_logger("agent_loop").error("must not print beneath composer")
    assert capsys.readouterr().err == ""


def test_noninteractive_logging_keeps_console_diagnostics(isolated_logging, capsys):
    setup_logging(log_to_file=False, log_to_console=True)
    get_logger("agent_loop").warning("pipe diagnostic")
    assert "pipe diagnostic" in capsys.readouterr().err


@pytest.mark.parametrize("is_tty", [False, True])
def test_main_selects_console_logging_for_terminal_mode(monkeypatch, is_tty):
    from pepsicode.main import main

    configured = Mock()
    monkeypatch.setattr("sys.argv", ["pepsicode", "--validate-config"])
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: is_tty))
    monkeypatch.setattr("pepsicode.logging_config.setup_logging", configured)
    monkeypatch.setattr("pepsicode.config.format_config_diagnostic", lambda: "checked")
    main()
    configured.assert_called_once_with(level="WARNING", log_to_console=not is_tty)


def test_invalid_model_is_rendered_above_composer_without_compaction_or_console_logs(
    tmp_path, monkeypatch, capsys, isolated_logging
):
    from pepsicode.tui import app, chrome

    setup_logging(log_to_console=False)
    monkeypatch.setattr(app, "save_history_entries", lambda history: None)
    monkeypatch.setattr("pepsicode.context.context_manager.save_context_state", lambda context: None)
    monkeypatch.setattr(app, "_get_terminal_size", lambda: (110, 24))
    monkeypatch.setattr(chrome, "_cached_terminal_size", lambda: (110, 24))
    request = Mock(
        side_effect=urllib.error.HTTPError(
            "https://api.deepseek.com/anthropic/v1/messages",
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps({"error": {"message": INVALID_MODEL}}).encode()),
        )
    )
    monkeypatch.setattr("urllib.request.urlopen", request)
    # A gateway can still return a model validation error after local checks.
    runtime = {"model": "opus", "baseUrl": "https://provider.example/anthropic", "apiKey": "test"}
    context = ContextManager(model="opus")
    context.compact_messages = Mock(side_effect=AssertionError("must not compact invalid-model errors"))
    tools = ToolRegistry([])
    args = TtyAppArgs(
        runtime=runtime,
        model=AnthropicModelAdapter(runtime, tools),
        tools=tools,
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path)),
        context_manager=context,
        messages=[{"role": "system", "content": "system"}],
    )
    state = ScreenState()
    _handle_input(args, state, lambda: None, "hello")
    state.agent_thread.join(timeout=5)
    assert not state.agent_thread.is_alive()
    assert not state.is_busy
    assert state.transcript[-1].status == "error"
    assert INVALID_MODEL in state.transcript[-1].body
    assert "Context too large" not in state.transcript[-1].body
    context.compact_messages.assert_not_called()
    request.assert_called_once()
    _render_stream_screen(args, state)
    output = capsys.readouterr()
    frame = strip_ansi(output.out)
    assert "Error" in frame
    assert frame.index("passed opus") < frame.index("Ask anything...")
    assert "Thinking" not in frame
    assert output.err == ""
    assert INVALID_MODEL in isolated_logging.read_text(encoding="utf-8")
