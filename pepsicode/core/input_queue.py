"""Thread-safe input admitted during an interactive agent run."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal

from pepsicode.input_limits import MAX_EXPANDED_INPUT_CHARACTERS, MAX_INPUT_CHARACTERS, MAX_PENDING_INPUTS
from pepsicode.logging_config import get_logger
from pepsicode.types import ChatMessage

logger = get_logger("input_queue")
InputMode = Literal["steer", "follow_up"]


@dataclass(frozen=True)
class QueuedInput:
    id: str
    content: str
    mode: InputMode
    display_content: str | None = None
    command_path: str | None = None

    def message(self) -> ChatMessage:
        message: ChatMessage = {"role": "user", "content": self.content}
        if self.display_content is not None:
            message["displayContent"] = self.display_content
        if self.command_path is not None:
            message["commandPath"] = self.command_path
        return message


class TurnInputQueue:
    """Serialize admission, delivery and settlement so accepted input cannot vanish.

    The listener receives ordered snapshots. It must only schedule UI updates,
    never wait for the agent or mutate this queue.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[QueuedInput] = []
        self._accepting = False
        self._listener: Callable[[dict], None] | None = None

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def start(self, listener: Callable[[dict], None] | None = None) -> None:
        with self._lock:
            if self._accepting:
                raise ValueError("An input queue is already running")
            self._listener = listener
            self._accepting = True
            self._publish()

    def enqueue(
        self,
        content: str,
        mode: InputMode = "steer",
        *,
        display_content: str | None = None,
        command_path: str | None = None,
    ) -> QueuedInput:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Input must contain text")
        content = content.strip()
        if mode not in ("steer", "follow_up"):
            raise ValueError("Input mode must be steer or follow_up")
        if content.startswith("/") and command_path is None:
            raise ValueError("Run slash commands after the current task finishes")
        expanded = command_path is not None
        limit = MAX_EXPANDED_INPUT_CHARACTERS if expanded else MAX_INPUT_CHARACTERS
        if len(content) > limit:
            raise ValueError(f"Queued input exceeds {limit} characters")
        if display_content is not None and len(display_content) > MAX_INPUT_CHARACTERS:
            raise ValueError(f"Queued invocation exceeds {MAX_INPUT_CHARACTERS} characters")
        with self._lock:
            if not self._accepting:
                raise ValueError("The task is finishing; send this input as a new task")
            if len(self._pending) >= MAX_PENDING_INPUTS:
                raise ValueError(f"The input queue is full ({MAX_PENDING_INPUTS} messages)")
            item = QueuedInput(uuid.uuid4().hex, content, mode, display_content, command_path)
            self._pending.append(item)
            self._publish()
            return item

    def take_steering(self) -> list[QueuedInput]:
        with self._lock:
            selected = [item for item in self._pending if item.mode == "steer"]
            if selected:
                self._pending = [item for item in self._pending if item.mode != "steer"]
                self._publish(delivered=selected)
            return selected

    def next_or_close(self) -> list[QueuedInput]:
        """After verification, take steering or one follow-up; otherwise close."""
        with self._lock:
            selected = [item for item in self._pending if item.mode == "steer"]
            if selected:
                self._pending = [item for item in self._pending if item.mode != "steer"]
            elif self._pending:
                selected = [self._pending.pop(0)]
            else:
                self._accepting = False
            self._publish(delivered=selected)
            return selected

    def remove(self, item_id: str) -> bool:
        with self._lock:
            selected = [item for item in self._pending if item.id == item_id]
            if not selected:
                return False
            self._pending = [item for item in self._pending if item.id != item_id]
            self._publish(returned=selected)
            return True

    def close(self) -> None:
        with self._lock:
            if not self._accepting and not self._pending:
                return
            self._accepting = False
            returned, self._pending = self._pending, []
            self._publish(returned=returned)

    def _publish(self, *, delivered=(), returned=()) -> None:
        if self._listener is not None:
            try:
                self._listener(
                    {
                        "accepting": self._accepting,
                        "pending": [asdict(item) for item in self._pending],
                        "delivered": [asdict(item) for item in delivered],
                        "returned": [asdict(item) for item in returned],
                    }
                )
            except Exception:
                logger.exception("Input queue listener failed")
