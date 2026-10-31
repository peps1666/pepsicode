from __future__ import annotations

import base64
import json

import pytest

from pepsicode.context.context_manager import estimate_message_tokens
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter, _to_anthropic_messages
from pepsicode.llm.model_discovery import parse_model_metadata
from pepsicode.llm.model_settings import image_input_error
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.app import _restore_transcript_entry
from pepsicode.tui.clipboard_image import _dib_to_png
from pepsicode.tui.image_input import image_message


def test_image_command_sends_image_block_with_prompt_and_quoted_path(tmp_path):
    image = tmp_path / "screen shot.png"
    data = b"\x89PNG\r\n\x1a\n" + b"example"
    image.write_bytes(data)

    message = image_message('/image "screen shot.png" 这是什么界面？', str(tmp_path))
    assert message["content"] == "这是什么界面？"
    assert message["displayContent"] == '/image "screen shot.png" 这是什么界面？'
    _, converted = _to_anthropic_messages([message])
    assert converted == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这是什么界面？"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                },
            ],
        }
    ]
    assert estimate_message_tokens(message) >= 1600
    adapter = AnthropicModelAdapter(
        {"model": "vision-test", "baseUrl": "https://example.test/v1", "apiKey": "test"}, ToolRegistry([])
    )
    request = adapter._build_stream_request("vision-test", "system", converted)
    body = json.loads(request.data)
    assert body["messages"][0]["content"][1]["source"]["data"] == base64.b64encode(data).decode("ascii")


def test_image_command_rejects_missing_invalid_and_oversized_files(tmp_path):
    with pytest.raises(ValueError, match="Usage"):
        image_message("/image", str(tmp_path))
    with pytest.raises(ValueError, match="not found"):
        image_message("/image missing.png", str(tmp_path))
    invalid = tmp_path / "invalid.png"
    invalid.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="Unsupported"):
        image_message("/image invalid.png", str(tmp_path))
    invalid.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024))
    with pytest.raises(ValueError, match="5 MB"):
        image_message("/image invalid.png", str(tmp_path))


def test_windows_clipboard_dib_converts_to_png():
    import struct
    import zlib

    header = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 24, 0, 4, 0, 0, 0, 0)
    png = _dib_to_png(header + b"\x00\x00\xff\x00")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    idat_length = struct.unpack_from(">I", png, 33)[0]
    assert zlib.decompress(png[41 : 41 + idat_length]) == b"\x00\xff\x00\x00"


def test_text_only_model_rejects_image_before_request():
    runtime = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "baseUrl": "https://example.test/v1",
        "apiKey": "test",
    }
    assert "deepseek-flash" in image_input_error(runtime)
    adapter = AnthropicModelAdapter(runtime, ToolRegistry([]))
    with pytest.raises(ValueError, match="不处理图片"):
        adapter._build_stream_request(
            runtime["model"], "system", [{"role": "user", "content": [{"type": "image", "source": {}}]}]
        )
    assert parse_model_metadata({"id": "vision", "input_modalities": ["text", "image"]})["inputModalities"] == [
        "text",
        "image",
    ]
    assert image_input_error({"model": "text-only", "models": [{"id": "text-only", "inputModalities": ["text"]}]})


def test_saved_transcript_with_old_thumbnail_field_can_still_load():
    entry = _restore_transcript_entry(
        {"id": 1, "kind": "user", "body": "看图 [图片 ×1]", "imagePreviews": [["old thumbnail"]]}
    )
    assert entry.body == "看图 [图片 ×1]"
