"""Durable notification outbox for low-disturbance human delivery.

Normal progress stays queryable in the panel; only key questions, blocking
states and deliveries become outbox notifications.  Entries are deduplicated
by a caller-supplied ``dedup_key`` (task id / question id / delivery version)
so an event storm never spams the IM channel, and a failed or timed-out send
becomes ``uncertain`` — never silently retried, matching the QQ bridge's
existing "will not auto-rerun" rule.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

OUTBOX_VERSION = 1
MAX_ITEMS = 512


class NotificationOutbox:
    """Persisted, deduplicated notification queue under the service directory."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError):
            return  # a corrupt outbox must not take the service down; start empty
        if (
            isinstance(payload, dict)
            and payload.get("version") == OUTBOX_VERSION
            and isinstance(payload.get("items"), list)
        ):
            self._items = [item for item in payload["items"] if isinstance(item, dict)]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._path.parent, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump({"version": OUTBOX_VERSION, "items": self._items}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    # -- operations ----------------------------------------------------------

    def enqueue(self, *, dedup_key: str, title: str, body: str = "") -> dict:
        """Insert a notification, or refresh the existing one for the key."""
        if not dedup_key or not isinstance(dedup_key, str):
            raise ValueError("dedup_key must be a non-empty string")
        with self._lock:
            for item in self._items:
                if item.get("dedup_key") == dedup_key:
                    item["title"] = title
                    item["body"] = body
                    item["updated_at"] = _now()
                    self._save()
                    return item
            item = {
                "id": uuid.uuid4().hex[:16],
                "sequence": (self._items[-1]["sequence"] + 1) if self._items else 1,
                "dedup_key": dedup_key,
                "title": title,
                "body": body,
                "status": "pending",
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._items.append(item)
            if len(self._items) > MAX_ITEMS:
                self._items = self._items[-MAX_ITEMS:]
                self._renumber()
            self._save()
            return item

    def _renumber(self) -> None:
        for index, item in enumerate(self._items, start=1):
            item["sequence"] = index

    def fetch_after(self, cursor: int, *, limit: int = 100) -> tuple[list[dict], int]:
        """Return items with ``sequence > cursor`` and the next cursor."""
        if not isinstance(cursor, int) or cursor < 0:
            cursor = 0
        with self._lock:
            items = [item for item in self._items if item["sequence"] > cursor][:limit]
            next_cursor = items[-1]["sequence"] if items else cursor
        return items, next_cursor

    def mark_status(self, item_id: str, status: str) -> dict | None:
        """Transition an item to ``pending`` or ``uncertain``.

        Delivery channels set ``uncertain`` when a send's outcome is unknown;
        resolution stays a human action (resend/discard) — no auto-retry.
        """
        if status not in {"pending", "uncertain"}:
            raise ValueError("status must be pending or uncertain")
        with self._lock:
            for item in self._items:
                if item.get("id") == item_id:
                    item["status"] = status
                    item["updated_at"] = _now()
                    self._save()
                    return item
        return None

    def discard(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if item.get("id") != item_id]
            changed = len(self._items) != before
            if changed:
                self._renumber()
                self._save()
            return changed

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total": len(self._items),
                "pending": sum(1 for item in self._items if item["status"] == "pending"),
                "uncertain": sum(1 for item in self._items if item["status"] == "uncertain"),
            }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
