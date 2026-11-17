"""Event journal cursor replay and notification outbox dedup semantics."""

from __future__ import annotations

import pytest

from pepsicode.service.events import JOURNALED_EVENTS, EventJournal, EventLogError, SessionEventLog
from pepsicode.service.outbox import NotificationOutbox


def _log(tmp_path, name="s1"):
    return SessionEventLog(tmp_path / "events", name)


def test_append_and_cursor_replay(tmp_path):
    journal = EventJournal(tmp_path / "events")
    log = journal.for_session("abc")
    assert log.latest_cursor == 0
    first = log.append("message/end", {"content": "done"})
    second = log.append("turn/end", {"status": "ok"})
    assert (first, second) == (1, 2)
    log.append("progress/message", {"ignored": True})  # unknown name still journaled if caller asks

    events, cursor = log.read_from(0)
    assert [event["event"] for event in events] == ["message/end", "turn/end", "progress/message"]
    assert cursor == 3
    # Incremental replay from a client cursor.
    events, cursor = log.read_from(1)
    assert [event["sequence"] for event in events] == [2, 3]


def test_torn_tail_from_crash_is_ignored(tmp_path):
    log = _log(tmp_path)
    log.append("turn/end", {"status": "ok"})
    with open(log.path, "a", encoding="utf-8") as stream:
        stream.write('{"sequence": 2, "event": "turn/e')  # crash mid-append
    fresh = SessionEventLog(tmp_path / "events", "s1")
    events, cursor = fresh.read_from(0)
    assert len(events) == 1 and cursor == 1
    assert fresh.append("turn/end", {"status": "ok"}) == 2


def test_write_failure_surfaces_as_event_log_error(tmp_path, monkeypatch):
    log = _log(tmp_path)
    real_open = open

    def broken_open(file, mode="r", *args, **kwargs):
        if str(file) == str(log.path) and "a" in mode:
            raise OSError("disk full")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", broken_open)
    try:
        with pytest.raises(EventLogError, match="disk full"):
            log.append("turn/end", {"status": "ok"})
    finally:
        monkeypatch.undo()
    # After the fault clears, appends resume with a monotonic sequence.
    assert log.append("turn/end", {"status": "ok"}) == 1


def test_journaled_events_exclude_high_frequency_deltas():
    assert "message/delta" not in JOURNALED_EVENTS
    assert "message/end" in JOURNALED_EVENTS and "turn/end" in JOURNALED_EVENTS


# -- outbox ------------------------------------------------------------------


def test_outbox_dedups_by_key_and_updates_in_place(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.json")
    first = outbox.enqueue(dedup_key="question:q1", title="Worker A asks", body="...")
    again = outbox.enqueue(dedup_key="question:q1", title="Worker A asks (updated)")
    assert first["id"] == again["id"]
    items, cursor = outbox.fetch_after(0)
    assert len(items) == 1 and items[0]["title"] == "Worker A asks (updated)"
    assert cursor == items[0]["sequence"]


def test_outbox_fetch_after_cursor_and_uncertain_status(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.json")
    for index in range(3):
        outbox.enqueue(dedup_key=f"key-{index}", title=f"notice {index}")
    items, cursor = outbox.fetch_after(0, limit=2)
    assert len(items) == 2
    rest, cursor2 = outbox.fetch_after(cursor)
    assert [item["dedup_key"] for item in rest] == ["key-2"]

    outbox.mark_status(items[0]["id"], "uncertain")
    snapshot = outbox.snapshot()
    assert snapshot["uncertain"] == 1 and snapshot["pending"] == 2
    # Uncertain items are never auto-resolved — only a human action moves them.
    assert outbox.mark_status(items[0]["id"], "pending")["status"] == "pending"
    assert outbox.discard(items[0]["id"]) is True
    assert outbox.discard("missing") is False


def test_outbox_survives_restart_and_corruption(tmp_path):
    outbox = NotificationOutbox(tmp_path / "outbox.json")
    outbox.enqueue(dedup_key="k", title="persisted")
    reloaded = NotificationOutbox(tmp_path / "outbox.json")
    assert reloaded.snapshot()["total"] == 1

    (tmp_path / "outbox.json").write_text("{corrupt", encoding="utf-8")
    broken = NotificationOutbox(tmp_path / "outbox.json")
    assert broken.snapshot()["total"] == 0
