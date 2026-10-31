"""Turn an explicitly named local image into a portable user message."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from pepsicode.types import ChatMessage

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_COMMAND = re.compile(r"^/image\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))(?:\s+(.*))?$", re.DOTALL)


def image_message(command: str, cwd: str) -> ChatMessage:
    match = _IMAGE_COMMAND.fullmatch(command)
    if match is None:
        raise ValueError("Usage: /image <path> [message] (quote paths containing spaces)")
    name = next(part for part in match.groups()[:3] if part is not None)
    return {
        "role": "user",
        "content": (match.group(4) or "请描述这张图片。").strip() or "请描述这张图片。",
        "displayContent": command,
        "images": [image_from_path(name, cwd)],
    }


def image_from_path(name: str, cwd: str) -> dict[str, str]:
    path = Path(name).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        if not path.is_file():
            raise ValueError(f"Image file not found: {path}")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 5 MB limit")
        data = path.read_bytes()
    except OSError as error:
        raise ValueError(f"Cannot read image: {error}") from error
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the 5 MB limit")
    return image_payload(data)


def pasted_image_path(text: str, cwd: str) -> dict[str, str] | None:
    """Recognize a pasted local image filename, leaving ordinary text alone."""
    name = text.strip().strip("\"'")
    if name.startswith("file://"):
        parsed = urlparse(name)
        if parsed.netloc not in ("", "localhost"):
            return None
        name = unquote(parsed.path)
        if len(name) > 2 and name[0] == "/" and name[2] == ":":
            name = name[1:]
    if Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return None
    path = Path(name).expanduser()
    if not path.is_absolute():
        path = Path(cwd) / path
    if not path.is_file():
        return None
    return image_from_path(name, cwd)


def image_payload(data: bytes) -> dict[str, str]:
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the 5 MB limit")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        media_type = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        media_type = "image/webp"
    else:
        raise ValueError("Unsupported image format (use PNG, JPEG, GIF, or WebP)")
    return {"media_type": media_type, "data": base64.b64encode(data).decode("ascii")}
