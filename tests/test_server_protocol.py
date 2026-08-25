"""Tests for desktop WebSocket protocol projections."""

from pepsicode.server.protocol import serialize_session_history


def test_serialize_session_history_maps_agent_roles() -> None:
    messages = [
        {"role": "system", "content": "hidden prompt"},
        {"role": "user", "content": "Inspect the project"},
        {"role": "assistant_progress", "content": "Looking..."},
        {
            "role": "assistant_tool_call",
            "toolUseId": "tool-1",
            "toolName": "read_file",
            "input": {"path": "README.md"},
        },
        {
            "role": "tool_result",
            "toolUseId": "tool-1",
            "toolName": "read_file",
            "content": "project docs",
            "isError": False,
        },
        {"role": "assistant", "content": "Done"},
        {"role": "assistant", "content": "Provider failed", "isError": True},
    ]

    history = serialize_session_history(messages)

    assert [item["role"] for item in history] == [
        "user",
        "progress",
        "tool_call",
        "tool_result",
        "assistant",
        "assistant",
    ]
    assert history[2]["tool_input"] == {"path": "README.md"}
    assert history[3]["is_error"] is False
    assert history[-1]["is_error"] is True
    assert all(item.get("content") != "hidden prompt" for item in history)


def test_serialize_session_history_ignores_non_display_messages() -> None:
    history = serialize_session_history(
        [
            {"role": "assistant_thinking", "blocks": [{"type": "thinking"}]},
            {"role": "unknown", "content": "not supported"},
        ]
    )

    assert history == []
