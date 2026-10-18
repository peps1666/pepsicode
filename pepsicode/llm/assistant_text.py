"""Decode assistant channel wrappers without treating ordinary text as markup."""

from __future__ import annotations

from typing import Literal

AssistantKind = Literal["final", "progress"]
_MARKERS: tuple[tuple[str, AssistantKind, str | None], ...] = (
    ("<final>", "final", "</final>"),
    ("[FINAL]", "final", None),
    ("<progress>", "progress", "</progress>"),
    ("[PROGRESS]", "progress", None),
)


def parse_assistant_text(content: str) -> tuple[str, AssistantKind | None]:
    trimmed = content.strip()
    for prefix, kind, closing in _MARKERS:
        if trimmed.startswith(prefix):
            body = trimmed[len(prefix) :].strip()
            if closing and body.endswith(closing):
                body = body[: -len(closing)].rstrip()
            return body, kind
    return trimmed, None


class AssistantTextStream:
    """Hold only possible wrapper fragments before forwarding text deltas."""

    def __init__(self) -> None:
        self._pending = ""
        self._decided = False
        self._closing: str | None = None

    def feed(self, text: str) -> str:
        self._pending += text
        if not self._decided:
            probe = self._pending.lstrip()
            if not probe or any(prefix.startswith(probe) and probe != prefix for prefix, _, _ in _MARKERS):
                # The opening marker may be split across arbitrary SSE deltas.
                return ""
            for prefix, _, closing in _MARKERS:
                if probe.startswith(prefix):
                    self._pending = probe[len(prefix) :]
                    self._closing = closing
                    break
            self._decided = True

        if self._closing:
            probe = self._pending.rstrip()
            for size in range(min(len(probe), len(self._closing)), 0, -1):
                if probe.endswith(self._closing[:size]):
                    start = len(probe) - size
                    visible, self._pending = self._pending[:start], self._pending[start:]
                    return visible
        visible, self._pending = self._pending, ""
        return visible

    def finish(self) -> str:
        pending, self._pending = self._pending, ""
        if self._closing and pending.rstrip().endswith(self._closing):
            return pending.rstrip()[: -len(self._closing)]
        return pending
