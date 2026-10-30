"""Cross-provider requests use independent credentials, catalogs and session settings."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import pepsicode.config as config
from pepsicode.cli.model_commands import handle_model_command, update_session_model_settings
from pepsicode.context.context_manager import ContextManager
from pepsicode.core.session import create_new_session, load_session, save_session
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.model_discovery import refresh_model_catalog
from pepsicode.llm.model_settings import apply_model_settings
from pepsicode.llm.providers import (
    PROVIDERS,
    configure_providers,
    model_settings_snapshot,
    provider_catalog,
    provider_runtime,
)
from pepsicode.permissions import PermissionManager
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.app import ScreenState, TtyAppArgs, _handle_input, _handle_normal_mode_event
from pepsicode.tui.input_parser import KeyEvent


@pytest.fixture
def four_providers(monkeypatch):
    for variable in ("PEPSI_CODE_PROVIDER", "PEPSI_CODE_MODEL", "PEPSI_CODE_REASONING_EFFORT"):
        monkeypatch.delenv(variable, raising=False)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Authorization"), None))
            self.send_response(200)
            self.end_headers()
            identifier = self.path.split("/")[1]
            self.wfile.write(
                json.dumps(
                    {
                        "data": [
                            {
                                "id": "same-api-id",
                                "name": f"{identifier} Future Model",
                                "effort": {"supported_levels": ["native-level"]},
                                "context_window": 700000,
                            }
                        ]
                    }
                ).encode()
            )

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(("POST", self.path, self.headers.get("Authorization"), body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps({"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}).encode()
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = {
        "provider": "deepseek",
        "providers": {
            identifier: {
                "apiKey": f"private-{identifier}-key",
                "baseUrl": f"http://127.0.0.1:{server.server_port}/{identifier}",
                "modelsUrl": "catalog",
            }
            for identifier in PROVIDERS
        },
    }
    runtime = {"model": "foreign-alias", "apiKey": "foreign-key", "fallbackModel": "foreign-fallback"}
    configure_providers(runtime, settings, {})
    try:
        yield runtime, settings, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_four_provider_switches_use_actual_catalog_ids_and_own_keys(four_providers):
    runtime, _, seen = four_providers
    adapter = AnthropicModelAdapter(runtime, ToolRegistry([]))
    for identifier in PROVIDERS:
        candidate = provider_runtime(runtime, identifier)
        refresh_model_catalog(candidate)
        update_session_model_settings(
            runtime=runtime, model_adapter=adapter, provider=identifier, model="same-api-id", effort="native-level"
        )
        assert adapter.runtime is runtime
        assert runtime["fallbackModel"] is None
        assert adapter.next([{"role": "user", "content": "hello"}]).content == "done"
    for method, path, authorization, body in seen:
        identifier = path.split("/")[1]
        assert authorization == f"Bearer private-{identifier}-key"
        if method == "POST":
            assert body["model"] == "same-api-id"
            assert body["output_config"]["effort"] == "native-level"
            assert body["max_tokens"] > 0
    assert [row[1] for row in seen if row[0] == "POST"] == [f"/{identifier}/v1/messages" for identifier in PROVIDERS]


def test_desktop_preview_does_not_change_active_provider_or_leak_credentials(four_providers, tmp_path):
    runtime, _, _ = four_providers
    session = ClientSession(str(tmp_path))
    session.runtime = runtime
    session.model = AnthropicModelAdapter(runtime, ToolRegistry([]))
    session.context_manager = ContextManager(model="old")
    session.messages = [
        {"role": "user", "content": "history"},
        {"role": "assistant_thinking", "blocks": [{"type": "thinking", "signature": "old-provider"}]},
    ]

    async def switch():
        server = PepsiCodeServer()
        loop = asyncio.get_running_loop()
        preview = await server._dispatch("model/settings", {"provider": "kimi"}, session, None, None, loop)
        assert runtime["provider"] == "deepseek"
        assert preview["models"][0]["name"] == "kimi Future Model"
        assert all("private-" not in json.dumps(item) for item in [preview, session.model_settings()])
        assert {item["id"] for item in preview["providers"]} == set(PROVIDERS)
        return await server._dispatch(
            "model/update", {"provider": "kimi", "model": "same-api-id"}, session, None, None, loop
        )

    assert asyncio.run(switch())["provider"] == "kimi"
    assert session.context_manager.context_window == 700000
    assert session.messages == [{"role": "user", "content": "history"}]
    assert session.model.runtime is runtime


def test_terminal_provider_menu_requires_model_confirmation_and_cancel_preserves_active(
    four_providers, tmp_path, monkeypatch
):
    runtime, _, _ = four_providers
    adapter = AnthropicModelAdapter(runtime, ToolRegistry([]))
    refresh_model_catalog(runtime)
    update_session_model_settings(runtime=runtime, model_adapter=adapter, model="same-api-id", effort="native-level")
    args = TtyAppArgs(
        runtime=runtime,
        tools=adapter.tools,
        model=adapter,
        permissions=PermissionManager(str(tmp_path)),
        cwd=str(tmp_path),
        messages=[],
    )
    state = ScreenState()
    monkeypatch.setattr("pepsicode.tui.app.save_history_entries", lambda history: None)
    enter = KeyEvent(name="return", ctrl=False, meta=False)
    _handle_input(args, state, lambda: None, "/provider")
    state.settings_picker.selected = next(
        i for i, item in enumerate(state.settings_picker.options) if item.value == "glm"
    )
    _handle_normal_mode_event(args, state, enter, lambda: None)
    assert state.settings_picker.kind == "model"
    assert runtime["provider"] == "deepseek"
    _handle_normal_mode_event(args, state, KeyEvent(name="escape", ctrl=False, meta=False), lambda: None)
    assert runtime["provider"] == "deepseek"
    _handle_input(args, state, lambda: None, "/provider glm")
    _handle_normal_mode_event(args, state, enter, lambda: None)
    assert runtime["provider"] == "glm" and runtime["model"] == "same-api-id"
    assert runtime["reasoningEffort"] == "default"


def test_failed_switch_and_missing_key_do_not_borrow_previous_credentials(four_providers):
    runtime, _, _ = four_providers
    adapter = AnthropicModelAdapter(runtime, ToolRegistry([]))
    refresh_model_catalog(provider_runtime(runtime, "kimi"))
    with pytest.raises(ValueError, match="没有"):
        update_session_model_settings(
            runtime=runtime, model_adapter=adapter, provider="kimi", model="unlisted-id", effort="default"
        )
    assert runtime["provider"] == "deepseek"
    runtime["_providerRuntimes"]["glm"]["apiKey"] = None
    assert not next(item for item in provider_catalog(runtime) if item["id"] == "glm")["configured"]
    with pytest.raises(ValueError, match="独立 API Key"):
        provider_runtime(runtime, "glm")
    assert runtime["apiKey"] == "private-deepseek-key"


def test_pipe_commands_and_saved_defaults_restore_provider_without_credentials(four_providers, tmp_path, monkeypatch):
    runtime, settings, _ = four_providers
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path)
    monkeypatch.setattr(config, "PEPSI_CODE_SETTINGS_PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    adapter = AnthropicModelAdapter(runtime, ToolRegistry([]))
    result = handle_model_command("/provider qwen same-api-id --save", runtime=runtime, model_adapter=adapter)
    assert "已切换" in result
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["provider"] == "qwen" and saved["providers"] == settings["providers"]
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path)
    session = create_new_session(str(tmp_path))
    session.messages = [{"role": "user", "content": "hello"}]
    session.model_settings = model_settings_snapshot(runtime)
    save_session(session)
    restored = load_session(session.session_id).model_settings
    assert restored == {"provider": "qwen", "model": "same-api-id", "reasoningEffort": "default"}
    assert "private-" not in json.dumps(restored)
    apply_model_settings(runtime, "same-api-id", "default", provider="deepseek")
    apply_model_settings(runtime, restored["model"], restored["reasoningEffort"], provider=restored["provider"])
    assert runtime["apiKey"] == "private-qwen-key"


def test_provider_settings_ignore_claude_model_and_generic_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PEPSI_CODE_SETTINGS_PATH", tmp_path / "own.json")
    monkeypatch.setattr(config, "CLAUDE_SETTINGS_PATH", tmp_path / "claude.json")
    monkeypatch.setattr(config, "PEPSI_CODE_MCP_PATH", tmp_path / "mcp.json")
    (tmp_path / "claude.json").write_text(
        json.dumps(
            {"model": "foreign-alias", "reasoningEffort": "foreign-effort", "env": {"ANTHROPIC_API_KEY": "foreign-key"}}
        )
    )
    (tmp_path / "own.json").write_text(
        json.dumps({"provider": "kimi", "providers": {"kimi": {"apiKeyEnv": "TEST_KIMI_KEY"}}})
    )
    monkeypatch.setenv("TEST_KIMI_KEY", "own-key")
    for variable in ("PEPSI_CODE_PROVIDER", "PEPSI_CODE_MODEL", "PEPSI_CODE_REASONING_EFFORT"):
        monkeypatch.delenv(variable, raising=False)
    runtime = config.load_runtime_config(tmp_path)
    assert runtime["model"] == "" and runtime["reasoningEffort"] == "default"
    assert runtime["apiKey"] == "own-key"
    assert runtime["baseUrl"] == PROVIDERS["kimi"]["baseUrl"]


def test_only_four_provider_types_and_invalid_config_rejected():
    with pytest.raises(ValueError, match="支持的服务商"):
        configure_providers({}, {"providers": {"foreign": {"apiKey": "x"}}}, {})
    with pytest.raises(ValueError, match="maxOutputTokens"):
        configure_providers({}, {"providers": {"qwen": {"maxOutputTokens": -1}}}, {})


def test_output_environment_override_applies_across_providers(monkeypatch):
    monkeypatch.delenv("PEPSI_CODE_PROVIDER", raising=False)
    runtime = {}
    settings = {
        "providers": {
            "deepseek": {"apiKey": "one", "model": "a", "maxOutputTokens": 1000},
            "kimi": {"apiKey": "two", "model": "b", "maxOutputTokens": 2000},
        }
    }
    configure_providers(runtime, settings, {"PEPSI_CODE_MAX_OUTPUT_TOKENS": "16000"})
    assert runtime["maxOutputTokens"] == 16000
    assert provider_runtime(runtime, "kimi")["maxOutputTokens"] == 16000


def test_provider_without_catalog_uses_explicit_models_without_network(four_providers):
    runtime, _, seen = four_providers
    candidate = provider_runtime(runtime, "qwen")
    candidate.update({"modelsUrl": False, "models": ["console-confirmed-id"]})
    refresh_model_catalog(candidate)
    assert not seen
    assert "显式配置 models" in candidate["catalogError"]
    adapter = AnthropicModelAdapter(runtime, ToolRegistry([]))
    update_session_model_settings(
        runtime=runtime, model_adapter=adapter, provider="qwen", model="console-confirmed-id", effort="default"
    )
    assert runtime["provider"] == "qwen"


def test_output_effort_profile_does_not_send_unsupported_thinking_switch(four_providers):
    runtime, _, _ = four_providers
    candidate = provider_runtime(runtime, "kimi")
    candidate["models"] = [{"id": "documented-id", "reasoningMode": "output_effort", "reasoningLevels": ["native"]}]
    adapter = AnthropicModelAdapter(runtime, ToolRegistry([]))
    update_session_model_settings(
        runtime=runtime, model_adapter=adapter, provider="kimi", model="documented-id", effort="native"
    )
    body = json.loads(adapter._build_request(runtime["model"], "sys", []).data)
    assert body["output_config"] == {"effort": "native"}
    assert "thinking" not in body


def test_plan_mode_blocks_terminal_and_desktop_provider_changes(four_providers, tmp_path):
    runtime, _, _ = four_providers
    permissions = PermissionManager(str(tmp_path))
    permissions.enter_plan_mode()
    permissions.ensure_local_command_allowed("/provider")
    permissions.ensure_local_command_allowed("/provider list")
    with pytest.raises(RuntimeError, match="Plan mode"):
        permissions.ensure_local_command_allowed("/provider kimi same-api-id")
    session = ClientSession(str(tmp_path))
    session.runtime, session.model, session.permissions = (
        runtime,
        AnthropicModelAdapter(runtime, ToolRegistry([])),
        permissions,
    )
    with pytest.raises(RuntimeError, match="Plan mode"):
        session.update_model({"provider": "kimi", "model": "same-api-id"})
