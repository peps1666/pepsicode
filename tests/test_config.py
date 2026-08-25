import pepsicode.config as config_module
from pepsicode.config import merge_settings, validate_config


def test_merge_settings_merges_env_and_mcp_servers() -> None:
    merged = merge_settings(
        {
            "env": {"A": "1"},
            "mcpServers": {"fs": {"command": "npx", "args": ["a"], "env": {"X": "1"}}},
        },
        {
            "env": {"B": "2"},
            "mcpServers": {
                "fs": {"command": "uvx", "env": {"Y": "2"}},
                "search": {"command": "python"},
            },
        },
    )

    assert merged["env"] == {"A": "1", "B": "2"}
    assert merged["mcpServers"]["fs"]["command"] == "uvx"
    assert merged["mcpServers"]["fs"]["args"] == ["a"]
    assert merged["mcpServers"]["fs"]["env"] == {"X": "1", "Y": "2"}
    assert merged["mcpServers"]["search"]["command"] == "python"


def test_validate_config_accepts_remote_http_mcp(monkeypatch) -> None:
    monkeypatch.setattr(
        config_module,
        "load_runtime_config",
        lambda cwd=None: {
            "model": "deepseek-chat",
            "mcpServers": {"remote-docs": {"url": "https://developers.openai.com/mcp"}},
        },
    )

    valid, messages = validate_config(".")

    assert valid
    assert messages == []
