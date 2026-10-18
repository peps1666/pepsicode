"""Use real local HTTP endpoints to verify provider-driven discovery end to end."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pepsicode.cli.model_commands import handle_model_command, update_session_model_settings
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.model_discovery import parse_model_metadata, refresh_model_catalog
from pepsicode.llm.model_settings import model_capabilities, model_catalog, model_selection_error
from pepsicode.permissions import PermissionManager
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.app import ScreenState, TtyAppArgs, _handle_input, _handle_normal_mode_event
from pepsicode.tui.input_parser import KeyEvent, TextEvent
from pepsicode.tui.settings_picker import create_settings_picker


@pytest.fixture
def provider():
    seen = []
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, dict(self.headers)))
            status, payload, headers = routes.get(self.path, (404, {}, {}))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = {
        "baseUrl": f"http://127.0.0.1:{server.server_port}/proxy/anthropic",
        "apiKey": "private-test-token",
        "model": "old-alias",
    }
    routes["/proxy/models"] = (
        200,
        {
            "data": [
                {
                    "id": "provider-new-model-2099",
                    "name": "Provider Latest 9.7",
                    "effort": {"supported_levels": ["balanced", "ultra"], "default_level": "balanced"},
                    "context_window": 999999,
                    "max_output_tokens": 5000,
                }
            ]
        },
        {},
    )
    try:
        yield runtime, routes, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_discovery_uses_api_name_and_id_without_builtin_models(provider):
    runtime, _, seen = provider
    refresh_model_catalog(runtime)
    models = model_catalog(runtime)
    assert [item["id"] for item in models] == ["provider-new-model-2099"]
    assert models[0]["name"] == "Provider Latest 9.7"
    assert models[0]["reasoningLevels"] == ["default", "balanced", "ultra"]
    assert models[0]["defaultEffort"] == "balanced"
    assert models[0]["contextWindow"] == 999999
    assert [path for path, _ in seen] == ["/proxy/anthropic/v1/models", "/proxy/models"]
    assert seen[-1][1]["Authorization"] == "Bearer private-test-token"
    assert "old-alias" in model_selection_error(runtime)
    assert model_selection_error(runtime, "provider-new-model-2099") is None
    picker = create_settings_picker(runtime, "model")
    assert picker.options[0].label == "Provider Latest 9.7"
    assert picker.options[0].value == "provider-new-model-2099"


def test_endpoint_refresh_uses_new_ids_and_native_effort_levels(provider):
    runtime, routes, seen = provider
    refresh_model_catalog(runtime)
    previous_calls = len(seen)
    refresh_model_catalog(runtime)
    assert len(seen) == previous_calls
    routes["/proxy/models"] = (
        200,
        {"data": [{"id": "brand-new-id", "display_name": "Fresh Name", "effort": {"supported_levels": ["turbo"]}}]},
        {},
    )
    refresh_model_catalog(runtime, force=True)
    adapter = AnthropicModelAdapter(runtime, ToolRegistry([]))
    update_session_model_settings(runtime=runtime, model_adapter=adapter, model="brand-new-id", effort="turbo")
    body = json.loads(adapter._build_request(runtime["model"], "sys", []).data)
    assert body["model"] == "brand-new-id"
    assert body["output_config"] == {"effort": "turbo"}
    assert model_catalog(runtime)[0]["name"] == "Fresh Name"
    assert handle_model_command("/model list", runtime=runtime, model_adapter=adapter).find("Fresh Name") >= 0


def test_tui_reads_names_and_refreshes_from_current_endpoint(provider, tmp_path, monkeypatch):
    runtime, routes, _ = provider
    tools = ToolRegistry([])
    adapter = AnthropicModelAdapter(runtime, tools)
    args = TtyAppArgs(
        runtime=runtime,
        tools=tools,
        model=adapter,
        cwd=str(tmp_path),
        permissions=PermissionManager(str(tmp_path)),
        messages=[],
    )
    state = ScreenState()
    monkeypatch.setattr("pepsicode.tui.app.save_history_entries", lambda history: None)
    _handle_input(args, state, lambda: None, "/model")
    assert state.settings_picker.options[0].label == "Provider Latest 9.7"
    routes["/proxy/models"] = (200, {"data": [{"id": "new-next-id", "name": "Next Release"}]}, {})
    _handle_normal_mode_event(args, state, TextEvent(text="r", ctrl=False, meta=False), lambda: None)
    assert state.settings_picker.options[0].label == "Next Release"
    _handle_normal_mode_event(args, state, KeyEvent(name="return", ctrl=False, meta=False), lambda: None)
    assert runtime["model"] == "new-next-id"
    assert state.settings_picker is None


def test_desktop_refresh_and_update_use_current_endpoint_metadata(provider, tmp_path):
    runtime, _, _ = provider
    session = ClientSession(str(tmp_path))
    session.runtime = runtime
    session.model = AnthropicModelAdapter(runtime, ToolRegistry([]))

    async def select_model():
        server = PepsiCodeServer()
        loop = asyncio.get_running_loop()
        result = await server._dispatch("model/settings", {"refresh": True}, session, None, None, loop)
        assert result["models"][0]["name"] == "Provider Latest 9.7"
        assert result["models"][0]["reasoningLevels"] == ["default", "balanced", "ultra"]
        assert result["catalogSource"] == "api"
        assert "private-test-token" not in json.dumps(result)
        return await server._dispatch(
            "model/update",
            {"model": "provider-new-model-2099", "reasoningEffort": "ultra"},
            session,
            None,
            None,
            loop,
        )

    result = asyncio.run(select_model())
    assert result["model"] == "provider-new-model-2099"
    body = json.loads(session.model._build_request(runtime["model"], "sys", []).data)
    assert body["model"] == "provider-new-model-2099"
    assert body["output_config"]["effort"] == "ultra"


def test_failed_refresh_preserves_last_server_list(provider):
    runtime, routes, _ = provider
    refresh_model_catalog(runtime)
    routes["/proxy/models"] = (503, {"error": "private-test-token"}, {})
    refresh_model_catalog(runtime, force=True)
    assert model_catalog(runtime)[0]["name"] == "Provider Latest 9.7"
    assert "HTTP 503" in runtime["catalogError"]
    assert "private-test-token" not in runtime["catalogError"]


def test_auth_failure_does_not_try_other_paths_or_invent_candidates(provider):
    runtime, routes, seen = provider
    routes["/proxy/anthropic/v1/models"] = (401, {"secret": "private-test-token"}, {})
    refresh_model_catalog(runtime)
    assert len(seen) == 1
    assert "HTTP 401" in runtime["catalogError"]
    assert [item["id"] for item in model_catalog(runtime)] == ["old-alias"]
    assert "private-test-token" not in runtime["catalogError"]


def test_discovery_follows_all_provider_pages(provider):
    runtime, routes, _ = provider
    routes["/proxy/models"] = (200, {"data": [{"id": "page-one"}], "has_more": True, "last_id": "page-one"}, {})
    routes["/proxy/models?after_id=page-one"] = (200, {"data": [{"id": "page-two"}], "has_more": False}, {})
    refresh_model_catalog(runtime)
    assert [item["id"] for item in model_catalog(runtime)] == ["page-one", "page-two"]


def test_explicit_models_url_preserves_gateway_prefix(provider):
    runtime, routes, seen = provider
    runtime["modelsUrl"] = "/custom/catalog"
    routes["/custom/catalog"] = routes["/proxy/models"]
    refresh_model_catalog(runtime)
    assert [path for path, _ in seen] == ["/custom/catalog"]


def test_no_cross_origin_models_url_or_redirect_forwarding(provider):
    runtime, routes, seen = provider
    runtime["modelsUrl"] = "https://other.example/models"
    refresh_model_catalog(runtime)
    assert not seen and "同源" in runtime["catalogError"]
    runtime.pop("modelsUrl")
    routes["/proxy/anthropic/v1/models"] = (302, {}, {"Location": "/credential-trap"})
    refresh_model_catalog(runtime, force=True)
    assert len(seen) == 1
    assert "HTTP 302" in runtime["catalogError"]


def test_empty_list_and_missing_capabilities_are_not_guessed(provider):
    runtime, routes, _ = provider
    routes["/proxy/models"] = (200, {"data": []}, {})
    refresh_model_catalog(runtime)
    assert model_catalog(runtime) == []
    assert create_settings_picker(runtime, "model").options == []
    assert model_capabilities(runtime, "arbitrary-name")["reasoningLevels"] == ["default"]


def test_messages_capabilities_are_parsed_without_name_heuristics():
    profile = parse_model_metadata(
        {
            "id": "completely-unknown-provider-id",
            "display_name": "Release X",
            "capabilities": {
                "thinking": {"types": {"adaptive": {"supported": True}, "disabled": {"supported": True}}},
                "effort": {
                    "supported": True,
                    "medium": {"supported": True},
                    "future-level": {"supported": True},
                    "low": {"supported": False},
                },
            },
        }
    )
    assert profile["reasoningMode"] == "adaptive"
    runtime = {"model": profile["id"], "_discoveredModels": [profile]}
    assert model_capabilities(runtime, profile["id"])["reasoningLevels"] == ["default", "off", "medium", "future-level"]


def test_api_metadata_overrides_local_guesses():
    runtime = {
        "model": "x",
        "models": [{"id": "x", "reasoningMode": "budget", "reasoningLevels": ["high"]}],
        "_discoveredModels": [
            parse_model_metadata({"id": "x", "name": "Provider Name", "effort": {"supported_levels": ["native"]}})
        ],
    }
    assert model_capabilities(runtime, "x")["reasoningLevels"] == ["default", "native"]


def test_untrusted_metadata_cannot_inject_terminal_control_codes():
    with pytest.raises(ValueError):
        parse_model_metadata({"id": "bad\x1b[2J"})
    assert "\x1b" not in parse_model_metadata({"id": "safe", "name": "hello\x1b"})["name"]
