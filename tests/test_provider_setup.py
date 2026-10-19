"""Guided setup validates real metadata, masks keys and commits only on confirmation."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

import pepsicode.config as config
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.llm.provider_setup import (
    build_setup_candidate,
    probe_provider_setup,
    public_setup_result,
    save_provider_setup,
    validate_setup_field,
)
from pepsicode.llm.providers import configure_providers, provider_catalog
from pepsicode.permissions import PermissionManager
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.app import ScreenState, TtyAppArgs, _handle_input, _handle_normal_mode_event
from pepsicode.tui.input_parser import KeyEvent, TextEvent
from pepsicode.tui.provider_wizard import create_provider_wizard, render_provider_wizard, wizard_event


@pytest.fixture
def setup_api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path)
    monkeypatch.setattr(config, "PEPSI_CODE_SETTINGS_PATH", tmp_path / "settings.json")
    settings = {"hooks": {"enabled": False}, "providers": {"deepseek": {"apiKey": "keep-existing-key"}}}
    config.PEPSI_CODE_SETTINGS_PATH.write_text(json.dumps(settings))
    seen = []
    response = {
        "status": 200,
        "payload": {"data": [{"id": "opaque-a", "name": "First Model"}, {"id": "opaque-b", "name": "Newest Model"}]},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, dict(self.headers)))
            self.send_response(response["status"])
            self.end_headers()
            self.wfile.write(json.dumps(response["payload"]).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    draft = {
        "provider": "custom",
        "name": "我的网关",
        "baseUrl": f"http://127.0.0.1:{server.server_port}/anthropic",
        "apiKey": "never-print-this-key",
        "authType": "bearer",
        "modelsUrl": "",
    }
    try:
        yield draft, seen, response
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", " "),
        ("baseUrl", "ftp://host/path"),
        ("baseUrl", "https://user:password@host/anthropic"),
        ("baseUrl", "https://host/v1/messages"),
        ("baseUrl", "https://host?api_key=private"),
        ("apiKey", "has space"),
        ("apiKey", "bad\x1bcode"),
        ("model", "bad id"),
    ],
)
def test_setup_rejects_invalid_fields_without_echoing_inputs(field, value):
    with pytest.raises(ValueError) as error:
        validate_setup_field(field, value)
    assert "password" not in str(error.value) and "private" not in str(error.value)


@pytest.mark.parametrize(
    "auth,header,value",
    [("bearer", "Authorization", "Bearer never-print-this-key"), ("api_key", "X-Api-Key", "never-print-this-key")],
)
def test_setup_reads_real_models_and_selected_auth_without_exposing_key(setup_api, auth, header, value):
    draft, seen, _ = setup_api
    draft["authType"] = auth
    candidate = probe_provider_setup(draft)
    result = public_setup_result(candidate)
    assert result["models"][1]["name"] == "Newest Model"
    assert result["verified"]
    assert "never-print-this-key" not in json.dumps(result)
    assert seen[0][1][header] == value
    assert "custom" not in json.loads(config.PEPSI_CODE_SETTINGS_PATH.read_text())["providers"]


def test_confirm_saves_only_selected_provider_preserves_other_config_and_replaces_credentials(setup_api):
    draft, _, _ = setup_api
    candidate = probe_provider_setup(draft)
    runtime = {
        "provider": "custom",
        "model": "old",
        "providerName": "旧网关",
        "baseUrl": "http://old.example",
        "apiKey": "old-key",
        "authToken": "old-token",
        "_providerRuntimes": {},
    }
    save_provider_setup(runtime, {**draft, "model": "opaque-b"}, candidate)
    saved = json.loads(config.PEPSI_CODE_SETTINGS_PATH.read_text())
    assert saved["hooks"] == {"enabled": False}
    assert saved["providers"]["deepseek"]["apiKey"] == "keep-existing-key"
    assert saved["provider"] == "custom" and saved["model"] == "opaque-b"
    assert runtime["apiKey"] == draft["apiKey"] and runtime["authToken"] is None
    assert next(item for item in provider_catalog(runtime) if item["id"] == "custom")["name"] == "我的网关"
    reloaded = {}
    configure_providers(reloaded, saved, {})
    request = AnthropicModelAdapter(reloaded, ToolRegistry([]))._build_request(reloaded["model"], "sys", [])
    assert request.full_url == draft["baseUrl"] + "/v1/messages"
    assert request.get_header("Authorization") == "Bearer never-print-this-key"


def test_save_refuses_unlisted_models_changed_connections_and_failed_write(setup_api, monkeypatch):
    draft, _, _ = setup_api
    candidate = probe_provider_setup(draft)
    original = config.PEPSI_CODE_SETTINGS_PATH.read_bytes()
    runtime = {"model": "old", "apiKey": "old-key"}
    with pytest.raises(ValueError, match="接口返回"):
        save_provider_setup(runtime, {**draft, "model": "made-up"}, candidate)
    with pytest.raises(ValueError, match="配置已变化"):
        save_provider_setup(runtime, {**draft, "baseUrl": "http://changed.example", "model": "opaque-a"}, candidate)
    monkeypatch.setattr("pepsicode.llm.provider_setup.save_mini_code_settings", Mock(side_effect=OSError("read only")))
    with pytest.raises(OSError):
        save_provider_setup(runtime, {**draft, "model": "opaque-a"}, candidate)
    assert runtime == {"model": "old", "apiKey": "old-key"}
    assert config.PEPSI_CODE_SETTINGS_PATH.read_bytes() == original


def test_no_catalog_manual_id_is_explicit_and_requires_no_http(setup_api):
    draft, seen, _ = setup_api
    draft["modelsUrl"] = False
    candidate = probe_provider_setup(draft)
    assert not seen and not public_setup_result(candidate)["verified"]
    save_provider_setup({}, {**draft, "model": "console-id"}, candidate)
    saved = json.loads(config.PEPSI_CODE_SETTINGS_PATH.read_text())
    assert saved["providers"]["custom"]["models"] == ["console-id"]


def test_cross_origin_model_discovery_is_rejected_before_sending_credentials(setup_api):
    draft, seen, _ = setup_api
    with pytest.raises(ValueError, match="同源"):
        build_setup_candidate({**draft, "modelsUrl": "https://other.example/models"})
    assert not seen


def test_terminal_wizard_complete_flow_masks_key_and_does_not_write_until_confirmation(
    setup_api, tmp_path, monkeypatch
):
    draft, _, _ = setup_api
    runtime = {"model": "old", "baseUrl": "http://old.example", "apiKey": "old-key"}
    tools = ToolRegistry([])
    args = TtyAppArgs(
        runtime=runtime,
        tools=tools,
        model=AnthropicModelAdapter(runtime, tools),
        cwd=str(tmp_path),
        messages=[{"role": "user", "content": "keep me"}],
        permissions=PermissionManager(str(tmp_path)),
    )
    state = ScreenState()
    monkeypatch.setattr("pepsicode.tui.app.save_history_entries", lambda history: None)
    enter = KeyEvent(name="return", ctrl=False, meta=False)

    def send(event):
        _handle_normal_mode_event(args, state, event, lambda: None)

    def text(value):
        send(TextEvent(text=value, ctrl=False, meta=False))

    _handle_input(args, state, lambda: None, "/provider add")
    text(draft["name"])
    send(enter)
    text(draft["baseUrl"])
    send(enter)
    send(enter)
    text(draft["apiKey"])
    assert draft["apiKey"] not in "\n".join(render_provider_wizard(state.provider_wizard, 100, 25))
    send(enter)
    send(enter)
    assert state.provider_wizard.current == "model"
    send(KeyEvent(name="down", ctrl=False, meta=False))
    send(enter)
    assert state.provider_wizard.current == "review" and runtime["model"] == "old"
    assert "custom" not in json.loads(config.PEPSI_CODE_SETTINGS_PATH.read_text())["providers"]
    send(enter)
    assert state.provider_wizard is None and runtime["model"] == "opaque-b"
    assert args.messages == [{"role": "user", "content": "keep me"}]
    assert draft["apiKey"] not in str(state.history) + str(state.transcript)


def test_terminal_cancel_and_previous_step_do_not_save_or_expose_key(setup_api):
    draft, _, _ = setup_api
    wizard = create_provider_wizard()
    wizard.draft.update(draft)
    wizard.move(3)
    assert draft["apiKey"] not in "\n".join(render_provider_wizard(wizard, 80, 20))
    wizard_event(wizard, KeyEvent(name="left", ctrl=False, meta=True))
    assert wizard.current == "authType"
    assert wizard_event(wizard, KeyEvent(name="escape", ctrl=False, meta=False)) == "cancel"
    assert not wizard.draft and wizard.candidate is None
    assert "custom" not in json.loads(config.PEPSI_CODE_SETTINGS_PATH.read_text())["providers"]


def test_desktop_probe_cancel_and_confirm_bootstrap_initial_mock_without_losing_history(
    setup_api, tmp_path, monkeypatch
):
    draft, _, _ = setup_api
    session = ClientSession(str(tmp_path))
    session.model, session.tools = MockModelAdapter(), ToolRegistry([])
    session.tools.mutation_count = 1
    session.messages = [{"role": "user", "content": "keep me"}]
    monkeypatch.setattr("pepsicode.server.app.create_default_tool_registry", lambda *args, **kwargs: ToolRegistry([]))

    async def setup():
        server, loop = PepsiCodeServer(), asyncio.get_running_loop()
        probe = await server._dispatch("provider/probe", {"draft": draft}, session, None, None, loop)
        assert session.runtime is None
        assert draft["apiKey"] not in json.dumps(probe)
        await server._dispatch("provider/cancel", {}, session, None, None, loop)
        with pytest.raises(ValueError, match="读取模型"):
            await server._dispatch(
                "provider/save", {"token": probe["token"], "model": "opaque-a"}, session, None, None, loop
            )
        assert session.runtime is None
        probe = await server._dispatch("provider/probe", {"draft": draft}, session, None, None, loop)
        return await server._dispatch(
            "provider/save", {"token": probe["token"], "model": "opaque-b"}, session, None, None, loop
        )

    result = asyncio.run(setup())
    assert result["provider"] == "custom" and not result["isMock"]
    assert session.messages == [{"role": "user", "content": "keep me"}]
    assert session._provider_setup is None and isinstance(session.model, AnthropicModelAdapter)
    assert session.tools.mutation_count > session.tools.verified_mutation_count
    assert draft["apiKey"] not in json.dumps(result)


def test_setup_respects_plan_and_busy_session_boundaries(setup_api, tmp_path):
    draft, seen, _ = setup_api
    session = ClientSession(str(tmp_path))
    session.permissions = PermissionManager(str(tmp_path))
    session.permissions.enter_plan_mode()

    async def reject():
        server, loop = PepsiCodeServer(), asyncio.get_running_loop()
        with pytest.raises(RuntimeError, match="Plan mode"):
            await server._dispatch("provider/probe", {"draft": draft}, session, None, None, loop)
        session.permissions = None
        session._worker_active.set()
        with pytest.raises(ValueError, match="当前对话"):
            await server._dispatch("provider/probe", {"draft": draft}, session, None, None, loop)

    asyncio.run(reject())
    assert not seen


def test_first_run_terminal_menu_can_start_and_save_setup(setup_api, tmp_path, monkeypatch):
    draft, _, _ = setup_api
    args = TtyAppArgs(
        runtime=None,
        tools=ToolRegistry([]),
        model=MockModelAdapter(),
        cwd=str(tmp_path),
        messages=[],
        permissions=PermissionManager(str(tmp_path)),
    )
    state = ScreenState()
    monkeypatch.setattr("pepsicode.tui.app.save_history_entries", lambda history: None)
    monkeypatch.setattr("pepsicode.tui.app.create_default_tool_registry", lambda *args, **kwargs: ToolRegistry([]))
    enter = KeyEvent(name="return", ctrl=False, meta=False)
    _handle_input(args, state, lambda: None, "/provider")
    assert state.settings_picker.options[-1].value == "__add__"
    _handle_normal_mode_event(args, state, enter, lambda: None)
    assert state.provider_wizard is not None
    state.provider_wizard.draft.update({**draft, "model": "opaque-a"})
    state.provider_wizard.candidate = probe_provider_setup(draft)
    state.provider_wizard.move(6)
    _handle_normal_mode_event(args, state, enter, lambda: None)
    assert state.provider_wizard is None
    assert isinstance(args.model, AnthropicModelAdapter) and args.runtime["provider"] == "custom"


def test_empty_verified_catalog_requires_fixing_endpoint_instead_of_manual_guess(setup_api):
    draft, _, response = setup_api
    response["payload"] = {"data": []}
    wizard = create_provider_wizard()
    wizard.draft.update(draft)
    wizard.candidate = probe_provider_setup(draft)
    wizard.move(5)
    wizard.text = "made-up-id"
    assert wizard_event(wizard, KeyEvent(name="return", ctrl=False, meta=False)) is None
    assert wizard.current == "model" and "空列表" in wizard.error


def test_atomic_config_write_failure_preserves_existing_file(setup_api, monkeypatch):
    before = config.PEPSI_CODE_SETTINGS_PATH.read_bytes()
    monkeypatch.setattr(config.os, "replace", Mock(side_effect=OSError("read only")))
    with pytest.raises(OSError):
        config.save_mini_code_settings({"model": "new-id"})
    assert config.PEPSI_CODE_SETTINGS_PATH.read_bytes() == before


def test_all_presets_are_listed_with_deepseek_first_and_default(tmp_path):
    from pepsicode.llm.providers import DEFAULT_PROVIDER
    from pepsicode.tui.settings_picker import create_settings_picker

    assert DEFAULT_PROVIDER == "deepseek"
    picker = create_settings_picker({"model": "mock"}, "provider")
    assert [option.value for option in picker.options] == ["deepseek", "kimi", "glm", "qwen", "__add__"]
    assert picker.options[picker.selected].value == "deepseek"

    runtime = {}
    configure_providers(runtime, {"providers": {"kimi": {"apiKey": "k"}, "deepseek": {"apiKey": "d"}}}, {})
    assert runtime["provider"] == "deepseek"


def test_unconfigured_preset_opens_its_setup_at_the_api_key_step(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.tui.app.save_history_entries", lambda history: None)
    args = TtyAppArgs(
        runtime=None,
        tools=ToolRegistry([]),
        model=MockModelAdapter(),
        cwd=str(tmp_path),
        messages=[],
        permissions=PermissionManager(str(tmp_path)),
    )
    state = ScreenState()
    enter = KeyEvent(name="return", ctrl=False, meta=False)
    _handle_input(args, state, lambda: None, "/provider")
    _handle_normal_mode_event(args, state, enter, lambda: None)
    wizard = state.provider_wizard
    assert wizard.draft["provider"] == "deepseek" and wizard.current == "apiKey"
    assert wizard.draft["baseUrl"] == "https://api.deepseek.com/anthropic"

    state.provider_wizard = None
    _handle_input(args, state, lambda: None, "/provider kimi")
    assert state.provider_wizard.draft["provider"] == "kimi"
    _handle_input(args, state, lambda: None, "/provider add")
    assert state.provider_wizard.draft["provider"] == "custom" and state.provider_wizard.current == "name"
