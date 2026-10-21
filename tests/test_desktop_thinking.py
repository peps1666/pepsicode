"""Public provider thinking is separate from answers and signed replay data."""

from copy import deepcopy

from pepsicode.server.protocol import serialize_session_history
from pepsicode.server.thinking import ThinkingStream
from pepsicode.types import StreamToken


def test_public_thinking_stream_keeps_blocks_and_answers_separate():
    events = []
    stream = ThinkingStream(lambda name, data: events.append((name, data)))
    stream.consume(StreamToken(type="thinking", content="先读取"))
    stream.consume(StreamToken(type="usage"))
    stream.consume(StreamToken(type="thinking", content="项目说明。"))
    block = {"type": "thinking", "thinking": "先读取项目说明。", "signature": "PRIVATE_SIGNATURE"}
    stream.consume(StreamToken(type="thinking", thinking_block=block))
    stream.consume(StreamToken(type="text", content="答案"))
    stream.consume(StreamToken(type="thinking", content="再检查结果。"))
    stream.consume(StreamToken(type="tool_use"))
    stream.finish()
    assert [name for name, _ in events] == [
        "thinking/start",
        "thinking/delta",
        "thinking/delta",
        "thinking/end",
        "thinking/start",
        "thinking/delta",
        "thinking/end",
    ]
    assert events[1][1]["text"] + events[2][1]["text"] == "先读取项目说明。"
    assert events[0][1]["id"] != events[4][1]["id"]
    assert "PRIVATE_SIGNATURE" not in str(events) and "答案" not in str(events)
    assert block["signature"] == "PRIVATE_SIGNATURE"


def test_redacted_empty_and_unsupported_thinking_create_no_placeholder():
    events = []
    stream = ThinkingStream(lambda name, data: events.append((name, data)))
    stream.consume(StreamToken(type="thinking", content=""))
    stream.consume(StreamToken(type="thinking", thinking_block={"type": "redacted_thinking", "data": "SECRET"}))
    stream.consume(StreamToken(type="text", content="Answer"))
    stream.finish("cancelled")
    assert events == []


def test_interrupted_thinking_closes_once():
    events = []
    stream = ThinkingStream(lambda name, data: events.append((name, data)))
    stream.consume(StreamToken(type="thinking", content="Partial"))
    stream.finish("cancelled")
    stream.finish("interrupted")
    assert events[-1][1]["status"] == "cancelled"
    assert sum(name == "thinking/end" for name, _ in events) == 1


def test_history_only_projects_public_thinking_without_mutating_replay_blocks():
    messages = [
        {"role": "user", "content": "Question"},
        {
            "role": "assistant_thinking",
            "blocks": [
                {"type": "thinking", "thinking": "First", "signature": "SIGNATURE"},
                {"type": "redacted_thinking", "data": "REDACTED"},
                {"type": "thinking", "thinking": "Second"},
            ],
        },
        {"role": "assistant_thinking", "internal": True, "blocks": [{"type": "thinking", "thinking": "INTERNAL"}]},
        {"role": "assistant", "content": "Answer"},
    ]
    original = deepcopy(messages)
    history = serialize_session_history(messages)
    assert [item["role"] for item in history] == ["user", "thinking", "thinking", "assistant"]
    assert [item["content"] for item in history] == ["Question", "First", "Second", "Answer"]
    assert all(secret not in str(history) for secret in ["SIGNATURE", "REDACTED", "INTERNAL"])
    assert messages == original
