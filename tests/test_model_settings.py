"""Regression coverage for model selection, reasoning payloads, and session boundaries."""

import asyncio
import json
from unittest.mock import Mock

import pytest

import pepsicode.config as config
from pepsicode.context.context_manager import ContextManager
from pepsicode.core.session import create_new_session, load_session, save_session
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.llm.model_settings import model_capabilities, model_catalog, validate_model_settings
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolRegistry
from pepsicode.tools.task import _runtime_for_agent


@pytest.fixture
def session(tmp_path):
    client = ClientSession(str(tmp_path))
    client.runtime = {
        "model": "deepseek-v4-flash",
        "reasoningEffort": "default",
        "baseUrl": "https://gateway.example/anthropic",
        "apiKey": "secret",
        "maxOutputTokens": None,
        "models": [
            {"id": name, "reasoningMode": mode, "reasoningLevels": levels, "supportsThinkingOff": True}
            for name, mode, levels in [
                ("deepseek-v4-flash", "effort", ["low", "high", "max"]),
                ("deepseek-v4-pro", "effort", ["low", "high", "max"]),
                ("claude-sonnet-4-20250514", "budget", ["low", "medium", "high"]),
                ("claude-sonnet-4-6", "adaptive", ["low", "medium", "high", "max"]),
            ]
        ],
    }
    client.tools = ToolRegistry([])
    client.model = AnthropicModelAdapter(client.runtime, client.tools)
    client.context_manager = ContextManager(model=client.runtime["model"])
    client.cost_tracker = Mock()
    client.session_data = create_new_session(str(tmp_path))
    client.messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant_thinking", "blocks": [{"type": "thinking", "signature": "old-model"}]},
        {"role": "assistant", "content": "answer"},
    ]
    return client


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "model, effort, thinking, output_config",
    [
        ("deepseek-v4-pro", "max", {"type": "enabled"}, {"effort": "max"}),
        ("deepseek-v4-pro", "off", {"type": "disabled"}, None),
        ("deepseek-v4-pro", "default", None, None),
        ("claude-sonnet-4-6", "medium", {"type": "adaptive", "display": "summarized"}, {"effort": "medium"}),
        ("claude-sonnet-4-20250514", "low", {"type": "enabled", "budget_tokens": 1024}, None),
        ("claude-sonnet-4-20250514", "high", {"type": "enabled", "budget_tokens": 16384}, None),
        ("custom-model", "default", None, None),
    ],
)
def test_request_uses_model_specific_reasoning(session, stream, model, effort, thinking, output_config):
    session.update_model({"model": model, "reasoningEffort": effort})
    builder = session.model._build_stream_request if stream else session.model._build_request
    request = builder(model, "sys", [{"role": "user", "content": "hello"}])
    body = json.loads(request.data)
    assert body["model"] == model
    assert body.get("thinking") == thinking
    assert body.get("output_config") == output_config
    assert body.get("stream", False) == stream
    if thinking and thinking.get("budget_tokens"):
        assert body["max_tokens"] > thinking["budget_tokens"]
    if effort == "default":
        assert body["max_tokens"] == 8192  # Messages requires an explicit output limit.


def test_switch_preserves_conversation_services_and_shared_runtime(session, monkeypatch):
    persist = Mock()
    monkeypatch.setattr("pepsicode.server.app.save_mini_code_settings", persist)
    tracker, adapter, runtime = session.cost_tracker, session.model, session.runtime
    result = session.update_model({"model": "claude-sonnet-4-20250514", "reasoningEffort": "low"})
    assert session.cost_tracker is tracker
    assert session.model is adapter
    assert session.runtime is runtime is adapter.runtime
    assert session.context_manager.model == "claude-sonnet-4-20250514"
    assert session.context_manager.context_window == 200_000
    assert [message["content"] for message in session.messages] == ["system", "hello", "answer"]
    assert session.session_data.model_settings == {"model": result["model"], "reasoningEffort": "low"}
    assert "secret" not in json.dumps(result)
    persist.assert_not_called()
    inherited = _runtime_for_agent(runtime, Mock(model="inherit"))
    assert inherited["model"] == result["model"]
    assert inherited["reasoningEffort"] == "low"


def test_switch_resets_effort_and_rejects_invalid_settings_atomically(session):
    session.update_model({"reasoningEffort": "max"})
    old_runtime = dict(session.runtime)
    with pytest.raises(ValueError, match="not supported"):
        session.update_model({"model": "custom-model", "reasoningEffort": "high", "persist": True})
    assert session.runtime == old_runtime
    assert session.update_model({"model": "custom-model"})["reasoningEffort"] == "default"


def test_model_settings_reject_busy_and_mock_sessions(session):
    async def while_running():
        async with session._turn_lock:
            with pytest.raises(ValueError, match="current turn"):
                session.update_model({"reasoningEffort": "max"})

    asyncio.run(while_running())
    session._worker_active.set()
    with pytest.raises(ValueError, match="current turn"):
        session.update_model({"reasoningEffort": "max"})
    session._worker_active.clear()
    session.model = MockModelAdapter()
    assert session.model_settings()["isMock"] is True
    with pytest.raises(ValueError, match="mock mode"):
        session.update_model({"model": "real"})


def test_settings_api_and_custom_capabilities(session):
    session.runtime["models"].append(
        {
            "id": "provider-alias",
            "reasoningMode": "effort",
            "reasoningLevels": ["high", "max"],
        }
    )

    async def request():
        server = PepsiCodeServer()
        loop = asyncio.get_running_loop()
        preview = await server._dispatch("model/settings", {"model": "provider-alias"}, session, None, None, loop)
        assert preview["capabilities"]["reasoningLevels"] == ["default", "high", "max"]
        return await server._dispatch(
            "model/update", {"model": "provider-alias", "reasoningEffort": "max"}, session, None, None, loop
        )

    assert asyncio.run(request())["model"] == "provider-alias"


def test_save_defaults_preserves_unrelated_settings(session, tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "keep"}, "hooks": {"enabled": True}}))
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path)
    monkeypatch.setattr(config, "PEPSI_CODE_SETTINGS_PATH", settings_path)
    session.update_model({"model": "deepseek-v4-pro", "reasoningEffort": "max", "persist": True})
    saved = json.loads(settings_path.read_text())
    assert saved["model"] == "deepseek-v4-pro"
    assert saved["reasoningEffort"] == "max"
    assert saved["env"] == {"ANTHROPIC_API_KEY": "keep"}
    assert saved["hooks"] == {"enabled": True}


def test_failed_default_write_keeps_active_runtime(session, monkeypatch):
    monkeypatch.setattr("pepsicode.server.app.save_mini_code_settings", Mock(side_effect=OSError("read only")))
    previous = dict(session.runtime)
    with pytest.raises(OSError, match="read only"):
        session.update_model({"model": "deepseek-v4-pro", "persist": True})
    assert session.runtime == previous


def test_session_roundtrip_and_resume_restore_model_settings(session, tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path)
    session.update_model({"model": "deepseek-v4-pro", "reasoningEffort": "max"})
    session.session_data.messages = list(session.messages)
    save_session(session.session_data)
    loaded = load_session(session.session_data.session_id)
    assert loaded.model_settings == {"model": "deepseek-v4-pro", "reasoningEffort": "max"}
    session.update_model({"model": "custom"})
    monkeypatch.setattr(session, "initialize", lambda **kwargs: None)
    monkeypatch.setattr(session, "set_permission_handler", lambda handler: None)

    async def resume():
        return await PepsiCodeServer()._dispatch(
            "session/resume", {"session_id": loaded.session_id}, session, None, None, asyncio.get_running_loop()
        )

    result = asyncio.run(resume())
    assert result["model"] == "deepseek-v4-pro"
    assert result["reasoningEffort"] == "max"
    assert result["history"][-1]["content"] == "answer"


def test_budget_validation_and_fallback_do_not_send_incompatible_fields(session):
    session.runtime["maxOutputTokens"] = 4096
    with pytest.raises(ValueError, match="must exceed"):
        session.update_model({"model": "claude-sonnet-4-20250514", "reasoningEffort": "medium"})
    session.update_model({"reasoningEffort": "max"})
    body = json.loads(session.model._build_request("custom-fallback", "sys", []).data)
    assert "thinking" not in body and "output_config" not in body


def test_model_catalog_deduplicates_without_inventing_capabilities():
    runtime = {"model": "deepseek-v4-pro", "models": [{"id": "deepseek-v4-pro"}, "custom"]}
    assert [item["id"] for item in model_catalog(runtime)] == ["deepseek-v4-pro", "custom"]
    assert model_catalog(runtime)[0]["reasoningLevels"] == ["default"]
    assert model_capabilities({}, "unknown")["reasoningLevels"] == ["default"]
    assert (
        model_capabilities({"baseUrl": "https://api.deepseek.com/anthropic"}, "claude-sonnet-4-6")["reasoningMode"]
        == "none"
    )
    official = model_catalog({"model": "opus", "baseUrl": "https://api.deepseek.com/anthropic"})
    assert [item["id"] for item in official] == ["opus"]  # Only the local config is known before discovery.


@pytest.mark.parametrize(
    "model, effort", [("", "default"), ("with space", "default"), ("custom", "high"), (None, "default")]
)
def test_invalid_choices(model, effort):
    with pytest.raises(ValueError):
        validate_model_settings({}, model, effort)


def test_runtime_config_reads_reasoning_and_env_overrides(monkeypatch):
    monkeypatch.setattr(
        config,
        "load_effective_settings",
        lambda cwd: {
            "model": "deepseek-v4-pro",
            "reasoningEffort": "high",
            "models": ["deepseek-v4-pro"],
            "env": {"ANTHROPIC_API_KEY": "test"},
        },
    )
    monkeypatch.delenv("PEPSI_CODE_MODEL", raising=False)
    monkeypatch.delenv("PEPSI_CODE_REASONING_EFFORT", raising=False)
    assert config.load_runtime_config()["reasoningEffort"] == "high"
    monkeypatch.setenv("PEPSI_CODE_REASONING_EFFORT", "max")
    assert config.load_runtime_config()["reasoningEffort"] == "max"
    monkeypatch.setenv("PEPSI_CODE_REASONING_EFFORT", "bad value")
    with pytest.raises(ValueError, match="reasoningEffort"):
        config.load_runtime_config()
