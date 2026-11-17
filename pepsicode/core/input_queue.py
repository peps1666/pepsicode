"""Thread-safe input admitted during an interactive agent run."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
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
    answer_to: tuple[str, ...] = ()
    external: bool = False

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id or self.mode not in {"steer", "follow_up"}:
            raise ValueError("Invalid queued input identity")
        if (
            not isinstance(self.content, str)
            or not self.content.strip()
            or len(self.content) > MAX_EXPANDED_INPUT_CHARACTERS
        ):
            raise ValueError("Invalid queued input content")
        if self.display_content is not None and (
            not isinstance(self.display_content, str) or len(self.display_content) > MAX_INPUT_CHARACTERS
        ):
            raise ValueError("Invalid queued input display text")
        if self.command_path is not None and not isinstance(self.command_path, str):
            raise ValueError("Invalid queued command source")
        if (
            not isinstance(self.answer_to, (tuple, list))
            or len(self.answer_to) > 12
            or any(not isinstance(item, str) or not 0 < len(item) <= 128 for item in self.answer_to)
            or len(set(self.answer_to)) != len(self.answer_to)
            or (self.answer_to and self.mode != "steer")
        ):
            raise ValueError("Invalid queued question IDs")
        object.__setattr__(self, "answer_to", tuple(self.answer_to))

    def payload(self):
        result = asdict(self)
        if not self.answer_to:
            result.pop("answer_to")
        if not self.external:
            result.pop("external")
        return result

    def message(self) -> ChatMessage:
        message: ChatMessage = {
            "role": "user",
            "content": self.content,
            "source": {"kind": "steering" if self.mode == "steer" else "human", "id": self.id, "mode": self.mode},
        }
        if self.display_content is not None:
            message["displayContent"] = self.display_content
        if self.command_path is not None:
            message["commandPath"] = self.command_path
        if self.answer_to:
            message["source"]["answerTo"] = list(self.answer_to)
        if self.external:
            message.update(internal=True, source={"kind": "external-event", "id": self.id})
        return message


class TurnInputQueue:
    """Serialize admission, delivery and settlement so accepted input cannot vanish.

    The listener receives ordered snapshots. It must only schedule UI updates,
    never wait for the agent or mutate this queue.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: list[QueuedInput] = []
        self._accepting = False
        self._listener: Callable[[dict], None] | None = None
        self._session = None
        self._claimed: dict[str, QueuedInput] = {}
        self._returned: list[QueuedInput] = []
        self._returned_notified: set[str] = set()
        self._steering_wakeup: Callable[[], None] | None = None
        self._worker_stop: Callable[[str], str] | None = None

    @contextmanager
    def wake_on_steering(self, callback, *, stop_worker):
        with self._lock:
            self._steering_wakeup = callback
            self._worker_stop = stop_worker
        try:
            yield
        finally:
            with self._lock:
                self._steering_wakeup = None
                self._worker_stop = None

    def stop_worker(self, trace_id: str) -> str:
        with self._lock:
            callback = self._worker_stop
        if callback is None:
            raise ValueError("No active orchestration run")
        return callback(trace_id)

    @property
    def has_steering(self):
        with self._lock:
            return any(item.mode == "steer" for item in self._pending)

    def bind_session(self, session):
        with self._lock:
            if session is self._session:
                return
            if self._accepting and self._session is not None:
                raise ValueError("Cannot switch the durable inbox while it is running")
            saved = session.task_control.get("inbox", {})
            admitted = {m.get("source", {}).get("id") for m in session.messages}
            admitted.update(n["message"].get("source", {}).get("id") for n in session.tree.get("nodes", []))
            pending = [QueuedInput(**item) for item in saved.get("pending", [])]
            pending += [QueuedInput(**item) for item in saved.get("claimed", []) if item["id"] not in admitted]
            if len({item.id for item in pending}) != len(pending):
                raise ValueError("Duplicate persisted inbox identity")
            unbound = self._session is None
            self._session = session
            self._pending = pending + self._pending if unbound else pending
            self._claimed = {}
            self._returned = [QueuedInput(**item) for item in saved.get("returned", [])]
            self._returned_notified = set()

    @contextmanager
    def _mutation(self):
        with self._lock:
            before = (list(self._pending), dict(self._claimed), list(self._returned), self._accepting, self._listener)
            try:
                yield
            except BaseException:
                self._pending, self._claimed, self._returned, self._accepting, self._listener = before
                raise

    def _persist(self):
        if self._session is None:
            return
        from pepsicode.core.task_control import edit_control
        from pepsicode.core.task_intent import apply_user_instruction, return_user_instructions

        with edit_control(session=self._session) as control:
            control["inbox"] = {
                "pending": [item.payload() for item in self._pending],
                "claimed": [item.payload() for item in self._claimed.values()],
                "returned": [item.payload() for item in self._returned],
            }
            task = control.get("task")
            if task:
                for item in self._pending:
                    if item.mode == "steer" and not item.external:
                        apply_user_instruction(task, item.id, item.content, delivered=False)
                return_user_instructions(task, {item.id for item in self._returned})

    def acknowledge(self, items):
        with self._mutation():
            for item in items:
                self._claimed.pop(item.id, None)
            self._persist()

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def start(self, listener: Callable[[dict], None] | None = None) -> None:
        with self._mutation():
            if self._accepting:
                raise ValueError("An input queue is already running")
            self._listener = listener
            self._accepting = True
            self._publish()
            if self._returned:
                self._publish(returned=self._returned, remember_returned=False)

    def enqueue(
        self,
        content: str,
        mode: InputMode = "steer",
        *,
        display_content: str | None = None,
        command_path: str | None = None,
        answer_to=(),
        external_id: str | None = None,
    ) -> QueuedInput | None:
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
        with self._mutation():
            if external_id is not None:
                for item in [*self._pending, *self._claimed.values()]:
                    if item.id == external_id:
                        return item
                if self._session and any(
                    node["message"].get("source", {}).get("id") == external_id
                    for node in self._session.tree.get("nodes", [])
                ):
                    return None
            if not self._accepting:
                raise ValueError("The task is finishing; send this input as a new task")
            if len(self._pending) >= MAX_PENDING_INPUTS:
                raise ValueError(f"The input queue is full ({MAX_PENDING_INPUTS} messages)")
            item = QueuedInput(
                external_id or uuid.uuid4().hex,
                content,
                mode,
                display_content,
                command_path,
                answer_to,
                external=external_id is not None,
            )
            if item.answer_to:
                self._check_question_targets(item)
            self._pending.append(item)
            self._publish()
            if mode == "steer" and self._steering_wakeup is not None:
                self._steering_wakeup()
            return item

    def _check_question_targets(self, item):
        if self._worker_stop is None or self._session is None:
            raise ValueError("Wait for the current turn before answering a Worker")
        from pepsicode.core.task_control import pending_questions

        with self._session._tree_lock:
            control = self._session.task_control
            task_id = (control.get("task") or {}).get("id")
            waiting = {
                row["trace_id"]
                for row in (control.get("orchestration") or {}).get("work", [])
                if row["task_id"] == task_id and row["state"] == "waiting_user"
            }
            pending = {
                question["id"]
                for question in pending_questions(control, task_id)
                if question.get("trace_id") in waiting
            }
        reserved = {key for entry in [*self._pending, *self._claimed.values()] for key in entry.answer_to}
        if not set(item.answer_to) <= pending - reserved:
            raise ValueError("A question is no longer pending or already has a queued answer")

    def take_steering(self) -> list[QueuedInput]:
        with self._mutation():
            selected = [item for item in self._pending if item.mode == "steer"]
            if selected:
                self._pending = [item for item in self._pending if item.mode != "steer"]
                self._publish(delivered=selected)
            return selected

    def next_or_close(self) -> list[QueuedInput]:
        """After verification, take steering or one follow-up; otherwise close."""
        with self._mutation():
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
        with self._mutation():
            selected = [item for item in self._pending if item.id == item_id]
            if not selected:
                return False
            self._pending = [item for item in self._pending if item.id != item_id]
            self._publish(returned=selected)
            return True

    def close(self) -> None:
        with self._mutation():
            if not self._accepting and not self._pending:
                return
            self._accepting = False
            returned = [item for item in self._pending if not item.external]
            self._pending = [item for item in self._pending if item.external]
            self._publish(returned=returned)

    def _publish(self, *, delivered=(), returned=(), remember_returned=True) -> None:
        for item in delivered:
            self._claimed[item.id] = item
        if remember_returned:
            known = {item.id for item in self._returned}
            self._returned.extend(item for item in returned if item.id not in known)
        self._persist()
        returned = [item for item in returned if item.id not in self._returned_notified]
        self._returned_notified.update(item.id for item in returned)
        if self._listener is not None:
            try:
                self._listener(
                    {
                        "accepting": self._accepting,
                        "pending": [item.payload() for item in self._pending],
                        "delivered": [item.payload() for item in delivered],
                        "returned": [item.payload() for item in returned],
                    }
                )
            except Exception:
                logger.exception("Input queue listener failed")
