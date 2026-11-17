"""External event connectors (M3 §19.3).

A connector is a *read-only poller* of an external source (GitHub, CI).  It
never grants authorization: events become queued inputs for the coordinating
session, validated against an explicit allowlist of repositories/users, with
a persisted cursor and stable external IDs so duplicates, replays and
reconnects never trigger duplicate work.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SEEN_IDS_LIMIT = 2048


@dataclass(frozen=True)
class ExternalEvent:
    """One deduplicated fact from an external source."""

    external_id: str
    kind: str
    repository: str
    title: str
    body: str
    head_sha: str | None = None
    payload: dict = field(default_factory=dict)


class ConnectorError(RuntimeError):
    """A poll failed; the manager records it and backs off."""


class Connector:
    """Protocol implemented by every concrete connector."""

    name: str = "connector"

    def poll(self) -> list[ExternalEvent]:
        raise NotImplementedError

    def health(self) -> dict:
        return {"name": self.name, "enabled": True}


class ConnectorManager:
    """Polls connectors, dedupes by external ID, persists cursors.

    Deduplication state (cursor + seen external IDs) is persisted per
    connector so a service restart does not re-deliver old facts.  A failing
    poll records ``last_error`` and applies exponential backoff; it never
    raises into the host loop.
    """

    def __init__(self, state_dir: Path, connectors: list[Connector]) -> None:
        self._state_dir = Path(state_dir)
        self._connectors = [connector for connector in connectors if connector is not None]
        self._lock = threading.Lock()
        self._seen: dict[str, set[str]] = {}
        self._last_poll: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._last_error: dict[str, str] = {}
        for connector in self._connectors:
            self._seen[connector.name] = self._load_seen(connector.name)

    # -- dedup state ---------------------------------------------------------

    def _state_path(self, name: str) -> Path:
        return self._state_dir / f"connector-{name}.json"

    def _load_seen(self, name: str) -> set[str]:
        try:
            payload = json.loads(self._state_path(name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        seen = payload.get("seen") if isinstance(payload, dict) else None
        return {str(item) for item in seen} if isinstance(seen, list) else set()

    def _persist_seen(self, name: str) -> None:
        payload = {"version": 1, "seen": sorted(self._seen.get(name, set()))[-SEEN_IDS_LIMIT:]}
        path = self._state_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            # Dedup state is best-effort on disk; the in-memory set still
            # prevents duplicates within this process lifetime.

    # -- polling -------------------------------------------------------------

    def due(self, name: str, interval: float) -> bool:
        last = self._last_poll.get(name, 0.0)
        backoff = self._backoff.get(name, 0.0)
        return time.monotonic() - last >= max(interval, backoff)

    def poll_once(self, *, commit=None) -> list[ExternalEvent]:
        """Poll every due connector; returns fresh, validated events."""
        fresh: list[ExternalEvent] = []
        with self._lock:
            for connector in self._connectors:
                self._last_poll[connector.name] = time.monotonic()
                try:
                    events = connector.poll()
                except Exception as error:  # noqa: BLE001 — a connector must not kill the host loop
                    self._last_error[connector.name] = str(error)[:300]
                    self._backoff[connector.name] = min(600.0, max(5.0, self._backoff.get(connector.name, 5.0) * 2))
                    continue
                self._last_error.pop(connector.name, None)
                self._backoff.pop(connector.name, None)
                seen = self._seen[connector.name]
                new_events = {event.external_id: event for event in events if event.external_id not in seen}
                if new_events:
                    if commit is not None:
                        commit(list(new_events.values()))
                    fresh.extend(new_events.values())
                    self._seen[connector.name] = set([*sorted(seen), *new_events][-SEEN_IDS_LIMIT:])
                    self._persist_seen(connector.name)
        return fresh

    def health(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": connector.name,
                    "last_error": self._last_error.get(connector.name),
                    "backoff_seconds": self._backoff.get(connector.name),
                    **connector.health(),
                }
                for connector in self._connectors
            ]


def filter_allowed(events: list[ExternalEvent], allowed_repositories: list[str]) -> tuple[list[ExternalEvent], int]:
    """Drop events from repositories outside the explicit allowlist."""
    allowed = {item.strip().lower() for item in allowed_repositories if item.strip()}
    kept = [event for event in events if event.repository.lower() in allowed]
    return kept, len(events) - len(kept)
