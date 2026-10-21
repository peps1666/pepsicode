"""Project public thinking deltas without exposing signed provider metadata."""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from pepsicode.types import StreamToken


class ThinkingStream:
    def __init__(self, emit: Callable[[str, dict[str, Any]], Any]) -> None:
        self.emit = emit
        self.active_id: str | None = None

    def consume(self, token: StreamToken) -> None:
        if token.type == "thinking":
            if token.content:
                if self.active_id is None:
                    self.active_id = uuid4().hex
                    self.emit("thinking/start", {"id": self.active_id})
                self.emit("thinking/delta", {"id": self.active_id, "text": token.content})
            if token.thinking_block is not None:
                self.finish()
        elif token.type in {"text", "tool_use", "done"}:
            self.finish()

    def finish(self, status: str = "completed") -> None:
        if self.active_id is not None:
            self.emit("thinking/end", {"id": self.active_id, "status": status})
            self.active_id = None
