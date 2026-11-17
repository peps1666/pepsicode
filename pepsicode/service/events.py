"""Per-session durable event journal with cursor replay.

Service mode persists every *key* event (message end, turn end, permission
request, verification update, input queue, worker notices) so a client that
reconnects can catch up on what happened while it was offline.  High-frequency
streaming deltas are deliberately not journaled — clients replay the state
from ``session/view`` plus these key events.

Each line is one JSON object with a workspace-monotonic ``sequence``.  A
corrupt trailing line (crash mid-append) is ignored on read; a write failure
raises :class:`EventLogError` so the host can flag the storage health
component and stop acknowledging delivery instead of silently dropping.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

MAX_FILE_ENTRIES = 5000
MAX_BATCH = 200

JOURNALED_EVENTS = frozenset(
    {
        "message/end",
        "turn/end",
        "permission/request",
        "permission/withdrawn",
        "verification/update",
        "input/queue",
        "cost/update",
        "agent/notice",
        "notification",
        "service/notice",
    }
)


class EventLogError(RuntimeError):
    """A journaled event could not be durably appended."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class SessionEventLog:
    """Append-only JSONL journal for one session's key events."""

    def __init__(self, directory: Path, session_id: str) -> None:
        self._path = Path(directory) / f"{session_id}.jsonl"
        self._lock = threading.Lock()
        self._sequence = self._scan_last_sequence()

    @property
    def path(self) -> Path:
        return self._path

    def _scan_last_sequence(self) -> int:
        if not self._path.exists():
            return 0
        last = 0
        try:
            lines = self._path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return 0
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn tail from a crash mid-append
            if isinstance(entry, dict) and isinstance(entry.get("sequence"), int):
                last = max(last, entry["sequence"])
        return last

    @property
    def latest_cursor(self) -> int:
        with self._lock:
            return self._sequence

    def append(self, event_name: str, data: dict) -> int:
        """Durably append one event; returns its sequence."""
        if not isinstance(data, dict):
            raise TypeError("event data must be a dict")
        with self._lock:
            sequence = self._sequence + 1
            entry = {"sequence": sequence, "event": event_name, "data": data, "ts": _now()}
            line = json.dumps(entry, ensure_ascii=False, default=str)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
                    stream.flush()
            except OSError as error:
                raise EventLogError(f"event journal write failed: {error}") from error
            self._sequence = sequence
            self._compact_if_needed()
        return sequence

    def _compact_if_needed(self) -> None:
        try:
            with open(self._path, encoding="utf-8", errors="replace") as stream:
                lines = stream.readlines()
        except OSError:
            return
        if len(lines) <= MAX_FILE_ENTRIES:
            return
        keep = [line for line in lines[-MAX_FILE_ENTRIES // 2 :] if line.strip()]
        temporary = self._path.with_suffix(".tmp")
        try:
            temporary.write_text("".join(keep), encoding="utf-8")
            temporary.replace(self._path)
        except OSError:
            temporary.unlink(missing_ok=True)

    def read_from(self, cursor: int, *, limit: int = MAX_BATCH) -> tuple[list[dict], int]:
        """Return events with ``sequence > cursor`` plus the next cursor.

        Tolerates a negative/garbage cursor by replaying from the start.
        """
        if not isinstance(cursor, int) or cursor < 0:
            cursor = 0
        with self._lock:
            try:
                lines = self._path.read_text(encoding="utf-8", errors="replace").splitlines()
            except FileNotFoundError:
                return [], self._sequence
            except OSError as error:
                raise EventLogError(f"event journal read failed: {error}") from error
        events: list[dict] = []
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("sequence", 0) <= cursor:
                continue
            events.append(entry)
            if len(events) >= limit:
                break
        next_cursor = events[-1]["sequence"] if events else cursor
        return events, next_cursor


class EventJournal:
    """Registry of per-session logs under the service directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._logs: dict[str, SessionEventLog] = {}
        self._lock = threading.Lock()

    def for_session(self, session_id: str) -> SessionEventLog:
        with self._lock:
            log = self._logs.get(session_id)
            if log is None:
                log = SessionEventLog(self._directory, session_id)
                self._logs[session_id] = log
            return log
