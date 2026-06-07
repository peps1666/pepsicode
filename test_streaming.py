"""Test the SSE parser and StreamToken generator without hitting the network."""
import sys
import json
from typing import Any, Generator
from unittest.mock import MagicMock, patch

from pepsicode.anthropic_adapter import (
    _parse_sse_event,
    _process_sse_event,
    _consume_pending_usage,
)
from pepsicode.agent_loop import _accumulate_stream_tokens, run_agent_turn_stream
from pepsicode.types import StreamToken, ChatMessage, AgentStep


def test_parse_sse_event():
    """Verify basic event parsing."""
    event_str = (
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Hello"}}\n\n'
    )
    parsed = _parse_sse_event(event_str)
    assert parsed is not None
    assert parsed["event"] == "content_block_delta"
    assert parsed["data"]["delta"]["text"] == "Hello"
    print("test_parse_sse_event: PASSED")


def test_parse_sse_event_done():
    """Verify [DONE] payloads are returned as strings (not actionable)."""
    event_str = "data: [DONE]\n\n"
    parsed = _parse_sse_event(event_str)
    # [DONE] has no event type, so this returns None - expected.
    assert parsed is None
    print("test_parse_sse_event_done: PASSED")


def test_process_text_delta():
    """Verify text deltas produce text tokens."""
    event = {
        "event": "content_block_delta",
        "data": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": " world"},
        },
    }
    tokens = list(_process_sse_event(event))
    assert len(tokens) == 1
    assert tokens[0].type == "text"
    assert tokens[0].content == " world"
    print("test_process_text_delta: PASSED")


def test_process_tool_use_start():
    """Verify tool_use blocks produce a token with name and id."""
    event = {
        "event": "content_block_start",
        "data": {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_123",
                "name": "read_file",
            },
        },
    }
    tokens = list(_process_sse_event(event))
    assert len(tokens) == 1
    assert tokens[0].type == "tool_use"
    assert tokens[0].tool_name == "read_file"
    assert tokens[0].tool_id == "toolu_123"
    print("test_process_tool_use_start: PASSED")


def test_process_tool_input_delta():
    """Verify tool input JSON deltas produce incremental tokens."""
    event = {
        "event": "content_block_delta",
        "data": {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
        },
    }
    tokens = list(_process_sse_event(event))
    assert len(tokens) == 1
    assert tokens[0].type == "tool_use"
    assert tokens[0].tool_input_partial == '{"path":'
    print("test_process_tool_input_delta: PASSED")


def test_process_message_stop():
    """Verify message_stop yields a done token."""
    event = {
        "event": "message_stop",
        "data": {"type": "message_stop"},
    }
    tokens = list(_process_sse_event(event))
    assert len(tokens) == 1
    assert tokens[0].type == "done"
    print("test_process_message_stop: PASSED")


def test_process_message_delta_usage():
    """Verify message_delta stashes usage for later consumption."""
    # Reset
    try:
        delattr(_process_sse_event, "_pending_usage")
    except AttributeError:
        pass

    event = {
        "event": "message_delta",
        "data": {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    }
    tokens = list(_process_sse_event(event))
    assert len(tokens) == 0
    usage = _consume_pending_usage()
    assert usage is not None
    assert usage == {"input_tokens": 100, "output_tokens": 50}
    print("test_process_message_delta_usage: PASSED")


def test_end_to_end_text_stream():
    """Simulate a full text-only response and verify the token stream."""
    sse = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"id":"msg_1"}}\n\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Hello"}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":" world"}}\n\n'
        'event: content_block_stop\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"input_tokens":10,"output_tokens":2}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )

    # Drive the parser like next_stream would.
    buffer = ""
    tokens: list[StreamToken] = []
    raw_bytes = sse.encode("utf-8")
    # Simulate chunked reading: feed a few bytes at a time to also test the
    # cross-chunk event boundary.
    step = 17
    for i in range(0, len(raw_bytes), step):
        chunk = raw_bytes[i:i + step]
        buffer += chunk.decode("utf-8")
        while "\n\n" in buffer:
            event_str, buffer = buffer.split("\n\n", 1)
            event = _parse_sse_event(event_str)
            if event is None:
                continue
            for token in _process_sse_event(event):
                tokens.append(token)
                if token.type == "done":
                    break

    text_tokens = [t for t in tokens if t.type == "text"]
    done_tokens = [t for t in tokens if t.type == "done"]
    assert len(text_tokens) == 2
    assert "".join(t.content for t in text_tokens) == "Hello world"
    assert len(done_tokens) == 1
    print("test_end_to_end_text_stream: PASSED")


def test_accumulate_text_only():
    """Verify text-only stream accumulates correctly."""
    tokens = [
        StreamToken(type="text", content="Hello"),
        StreamToken(type="text", content=" world"),
        StreamToken(type="done"),
    ]

    def gen():
        yield from tokens

    text, calls = _accumulate_stream_tokens(gen())
    assert text == "Hello world"
    assert calls is None
    print("test_accumulate_text_only: PASSED")


def test_accumulate_single_tool_call():
    """Verify a single tool call is accumulated correctly."""
    tokens = [
        StreamToken(type="text", content="Let me read the file."),
        StreamToken(type="tool_use", tool_name="read_file", tool_id="toolu_123"),
        StreamToken(type="tool_use", tool_input_partial='{"path":'),
        StreamToken(type="tool_use", tool_input_partial='"test.py"}'),
        StreamToken(type="done"),
    ]

    def gen():
        yield from tokens

    text, calls = _accumulate_stream_tokens(gen())
    assert text == "Let me read the file."
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["id"] == "toolu_123"
    assert calls[0]["toolName"] == "read_file"
    assert calls[0]["input"] == {"path": "test.py"}
    print("test_accumulate_single_tool_call: PASSED")


def test_accumulate_multiple_tool_calls():
    """Verify multiple tool calls are accumulated correctly."""
    tokens = [
        StreamToken(type="tool_use", tool_name="read_file", tool_id="id1"),
        StreamToken(type="tool_use", tool_input_partial='{"path":"a.py"}'),
        StreamToken(type="tool_use", tool_name="list_files", tool_id="id2"),
        StreamToken(type="tool_use", tool_input_partial='{"dir":"."}'),
        StreamToken(type="done"),
    ]

    def gen():
        yield from tokens

    text, calls = _accumulate_stream_tokens(gen())
    assert text == ""
    assert calls is not None
    assert len(calls) == 2
    assert calls[0]["toolName"] == "read_file"
    assert calls[0]["input"] == {"path": "a.py"}
    assert calls[1]["toolName"] == "list_files"
    assert calls[1]["input"] == {"dir": "."}
    print("test_accumulate_multiple_tool_calls: PASSED")


def test_accumulate_with_on_token_callback():
    """Verify on_token callback is invoked for each token."""
    received: list[str] = []
    tokens = [
        StreamToken(type="text", content="A"),
        StreamToken(type="text", content="B"),
        StreamToken(type="done"),
    ]

    def gen():
        yield from tokens

    def on_token(t: StreamToken):
        received.append(f"{t.type}:{t.content}")

    _accumulate_stream_tokens(gen(), on_token=on_token)
    assert received == ["text:A", "text:B", "done:"]
    print("test_accumulate_with_on_token_callback: PASSED")


def test_accumulate_tool_with_no_input():
    """Verify a tool call with no input tokens is still captured."""
    tokens = [
        StreamToken(type="tool_use", tool_name="ask_user", tool_id="id3"),
        StreamToken(type="done"),
    ]

    def gen():
        yield from tokens

    text, calls = _accumulate_stream_tokens(gen())
    assert text == ""
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["toolName"] == "ask_user"
    assert calls[0]["input"] is None  # no input fragments arrived
    print("test_accumulate_tool_with_no_input: PASSED")


def test_run_agent_turn_stream_end_to_end():
    """End-to-end test: verify run_agent_turn_stream invokes on_token correctly."""
    # Create a mock model that yields tokens via next_stream.
    class MockModel:
        last_usage = {"input_tokens": 10, "output_tokens": 5}
        def next_stream(self, messages):
            return iter([
                StreamToken(type="text", content="Hello"),
                StreamToken(type="text", content=" world"),
                StreamToken(type="done"),
            ])

    class MockTools:
        def list(self):
            return []
        def get_skills(self):
            return []
        def get_mcp_servers(self):
            return {}
        def get_mcp_config(self):
            return {}
        def find(self, name):
            return None

    class MockPermissions:
        def begin_turn(self):
            pass
        def end_turn(self):
            pass
        def get_summary(self):
            return {}

    received_tokens: list[str] = []
    received_messages: list[str] = []

    def on_token(t: StreamToken):
        if t.type == "text":
            received_tokens.append(t.content)

    def on_assistant_message(content: str):
        received_messages.append(content)

    messages: list[ChatMessage] = [
        {"role": "system", "content": "You are a test assistant."},
        {"role": "user", "content": "Say hello"},
    ]

    result = run_agent_turn_stream(
        model=MockModel(),
        tools=MockTools(),
        messages=messages,
        cwd=".",
        permissions=MockPermissions(),
        on_token=on_token,
        on_assistant_message=on_assistant_message,
        max_steps=1,
    )

    # Verify tokens were received in order.
    assert received_tokens == ["Hello", " world"]
    # Verify final assistant message was emitted.
    assert "Hello world" in received_messages
    # Verify the returned message list contains the assistant response.
    assert any(m.get("content") == "Hello world" for m in result)
    print("test_run_agent_turn_stream_end_to_end: PASSED")


def main() -> int:
    tests = [
        test_parse_sse_event,
        test_parse_sse_event_done,
        test_process_text_delta,
        test_process_tool_use_start,
        test_process_tool_input_delta,
        test_process_message_stop,
        test_process_message_delta_usage,
        test_end_to_end_text_stream,
        test_accumulate_text_only,
        test_accumulate_single_tool_call,
        test_accumulate_multiple_tool_calls,
        test_accumulate_with_on_token_callback,
        test_accumulate_tool_with_no_input,
        test_run_agent_turn_stream_end_to_end,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as e:
            print(f"{test.__name__}: FAILED - {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"{test.__name__}: ERROR - {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
