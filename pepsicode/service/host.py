"""Service host: owns resident sessions, policy checkpoints and health.

The host never runs turns itself — turns still start through the server's
``run_verified_turn`` path.  The host owns the *lifecycle* around them:

- resident session registry with attach/detach bookkeeping,
- the durable event journal and notification outbox,
- the unattended-policy checkpoint (reloaded from disk on every evaluation
  so revocation takes effect at the next entrance),
- component health (storage / queue / model / workers / connectors /
  notifications) written to ``health.json`` for the CLI,
- external-event connectors with dedup and backoff.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from typing import Any

from pepsicode.core.session import list_sessions, load_session
from pepsicode.service import state as service_state
from pepsicode.service.connectors.base import SEEN_IDS_LIMIT, ConnectorManager, ExternalEvent, filter_allowed
from pepsicode.service.connectors.github import build_github_connector
from pepsicode.service.events import EventJournal, EventLogError
from pepsicode.service.outbox import NotificationOutbox
from pepsicode.service.policy import load_policy, policy_effective

HEALTH_VERSION = 1


class ServiceHost:
    """Stateful core of the resident service for one workspace."""

    def __init__(self, workspace: str, runtime: dict, *, token: str, port: int, generation: str) -> None:
        self.workspace = str(workspace)
        self.runtime = runtime or {}
        self.token = token
        self.port = int(port)
        self.generation = generation
        self.execution_mode = "service"
        self._session_lock = threading.Lock()
        self._sessions: dict[str, Any] = {}
        self._attachments: dict[str, int] = {}
        self._storage_error: str | None = None
        self._shutdown_requested = threading.Event()
        self._started_at = time.time()

        base = service_state.service_dir(self.workspace)
        self._external_path = base / "external-events.json"
        self._external_lock = threading.RLock()
        self.journal = EventJournal(base / "events")
        self.outbox = NotificationOutbox(base / "outbox.json")

        connector_settings = (self.runtime.get("service") or {}).get("connectors", {})
        github = build_github_connector(connector_settings.get("github", {}))
        self.connectors = ConnectorManager(base, [github] if github is not None else [])

    # -- resident sessions ---------------------------------------------------

    def register_session(self, session_id: str, session: Any) -> None:
        with self._session_lock:
            self._sessions[session_id] = session
            self._attachments.setdefault(session_id, 0)

    def get_session(self, session_id: str) -> Any | None:
        with self._session_lock:
            return self._sessions.get(session_id)

    def attached(self, session_id: str) -> int:
        with self._session_lock:
            return self._attachments.get(session_id, 0)

    def attach(self, session_id: str) -> None:
        with self._session_lock:
            self._attachments[session_id] = self._attachments.get(session_id, 0) + 1

    def detach(self, session_id: str) -> int:
        with self._session_lock:
            remaining = max(0, self._attachments.get(session_id, 0) - 1)
            self._attachments[session_id] = remaining
        return remaining

    def resident_session_ids(self) -> list[str]:
        with self._session_lock:
            return list(self._sessions)

    def drop_session(self, session_id: str) -> None:
        with self._session_lock:
            self._sessions.pop(session_id, None)
            self._attachments.pop(session_id, None)

    # -- policy checkpoint ---------------------------------------------------

    def load_policy(self) -> dict | None:
        """Reload the policy from disk on every checkpoint (cheap, and the
        only way revocation/expiry takes effect at the next entrance)."""
        try:
            return load_policy(self.workspace)
        except Exception:  # noqa: BLE001 — an unreadable policy must not crash the checkpoint
            return None

    def policy_effective(self) -> tuple[dict | None, bool, str]:
        policy = self.load_policy()
        effective, reason = policy_effective(policy)
        return policy, effective, reason

    # -- durable events ------------------------------------------------------

    def record_event(self, session_id: str, event_name: str, data: dict) -> None:
        try:
            self.journal.for_session(session_id).append(event_name, data)
            self._storage_error = None
        except EventLogError as error:
            self._storage_error = str(error)

    def storage_ok(self) -> bool:
        return self._storage_error is None

    # -- notifications -------------------------------------------------------

    def notify(self, *, dedup_key: str, title: str, body: str = "") -> dict:
        item = self.outbox.enqueue(dedup_key=dedup_key, title=title, body=body)
        # Session-independent notifications live in the service-level journal
        # (a literal "*" is an illegal filename on Windows).
        self.record_event(
            "_service",
            "notification",
            {"id": item["id"], "dedup_key": dedup_key, "title": title, "body": body},
        )
        return item

    # -- external events -----------------------------------------------------

    def deliver_external_events(self, events: list[ExternalEvent], allowed_repositories: list[str]) -> dict:
        """Route validated events into resident sessions or the outbox.

        Events steer an *active* coordination turn; with no active turn they
        are held as pending inputs (surfaced via ``notifications/fetch`` and
        delivered when the session's queue accepts input).  They never start
        work by themselves and never grant authorization.
        """
        kept, dropped = filter_allowed(events, allowed_repositories)
        delivered = held = 0
        sessions = {
            session_id: session.session_data
            for session_id in self.resident_session_ids()
            if (session := self.get_session(session_id)) is not None and session.session_data is not None
        }
        for metadata in list_sessions(workspace=self.workspace) if kept else ():
            if metadata.session_id not in sessions and (data := load_session(metadata.session_id)) is not None:
                sessions[metadata.session_id] = data
        with self._external_lock:
            stored = service_state._read_json(self._external_path) or {"seen": [], "heads": {}, "pending": {}}
            pending = stored["pending"]
            for event in kept:
                if event.external_id in stored["seen"]:
                    continue
                key = f"{event.repository.lower()}#{event.payload.get('pr')}"
                version = event.payload.get("pr_updated_at", "")
                if event.head_sha:
                    known = stored["heads"].get(key)
                    if known and known["head"] != event.head_sha and version <= known["version"]:
                        continue
                    if not known or version > known["version"]:
                        stored["heads"][key] = {"head": event.head_sha, "version": version}
                targets = [
                    session_id for session_id, data in sessions.items() if self._owns_external_event(data, event)
                ]
                if not targets:
                    self.notify(dedup_key=f"external:{event.external_id}", title=event.title, body=event.body[:500])
                for session_id in targets:
                    pending.setdefault(session_id, {})[event.external_id] = asdict(event)
                stored["seen"] = [*stored["seen"], event.external_id][-SEEN_IDS_LIMIT:]
                service_state._atomic_write_json(self._external_path, stored)
                for session_id in targets:
                    count = self.flush_pending_external(session_id)
                    delivered += count
                    if not count:
                        held += 1
                        self.notify(dedup_key=f"external:{event.external_id}", title=event.title, body=event.body[:500])
                if targets:
                    stored = service_state._read_json(self._external_path)
                    pending = stored["pending"]
        if dropped:
            self.notify(
                dedup_key="external:dropped",
                title=f"{dropped} external events rejected (source outside the allowlist)",
            )
        return {"delivered": delivered, "held": held, "dropped": dropped}

    @staticmethod
    def _owns_external_event(data, event: ExternalEvent) -> bool:
        with data._tree_lock:
            control = data.task_control
            task = control.get("task") or {}
            deliveries = (control.get("orchestration") or {}).get("deliveries", {}).values()
            return any(
                row["kind"] == "pull_request"
                and row["task_id"] == task.get("id")
                and row["target"]["repository"].lower() == event.repository.lower()
                and row["external_refs"].get("pr_number") == event.payload.get("pr")
                and event.payload.get("pr") is not None
                and (not row.get("head_sha") or not event.head_sha or row.get("head_sha") == event.head_sha)
                for row in deliveries
            )

    def flush_pending_external(self, session_id: str) -> int:
        """Move held external events into the session queue once it accepts input."""
        with self._external_lock:
            session = self.get_session(session_id)
            stored = service_state._read_json(self._external_path)
            if not session or not session.session_data or not stored or not session.input_queue.accepting:
                return 0
            pending = stored["pending"].get(session_id, {})
            delivered = 0
            for external_id, payload in list(pending.items()):
                event = ExternalEvent(**payload)
                key = f"{event.repository.lower()}#{event.payload.get('pr')}"
                head = stored["heads"].get(key)
                if (head and event.head_sha and event.head_sha != head["head"]) or not self._owns_external_event(
                    session.session_data, event
                ):
                    pending.pop(external_id)
                    continue
                try:
                    session.input_queue.enqueue(
                        f"[{event.kind}] {event.title}\n{event.body}".strip(), external_id=f"external:{external_id}"
                    )
                    delivered += 1
                except ValueError:
                    break
                pending.pop(external_id)
            if session_id in stored["pending"]:
                if not pending:
                    stored["pending"].pop(session_id)
                service_state._atomic_write_json(self._external_path, stored)
            return delivered

    # -- health --------------------------------------------------------------

    def health_snapshot(self) -> dict:
        components: dict[str, Any] = {}
        components["storage"] = {
            "ok": self.storage_ok(),
            "error": self._storage_error,
        }

        queue = {"sessions": 0, "queued_effects": 0, "active_effects": 0, "waiting_user": 0}
        workers = {"attempts": 0, "crashes": 0}
        for session_id in self.resident_session_ids():
            session = self.get_session(session_id)
            data = getattr(session, "session_data", None)
            if data is None:
                continue
            orchestration = data.task_control.get("orchestration") or {}
            rows = orchestration.get("work") or []
            queue["sessions"] += 1
            for row in rows:
                if row.get("state") == "queued":
                    queue["queued_effects"] += 1
                elif row.get("state") == "active":
                    queue["active_effects"] += 1
                elif row.get("state") == "waiting_user":
                    queue["waiting_user"] += 1
                if row.get("state") == "failed":
                    workers["crashes"] += 1
                workers["attempts"] += 1
        components["queue"] = queue
        runtime = self.runtime or {}
        components["model"] = {
            "ok": bool(runtime.get("model")) or bool(runtime.get("apiKey")) or bool(runtime.get("authToken")),
            "model": runtime.get("model", ""),
            "mock": (runtime.get("model") or "") == "mock",
        }
        components["workers"] = workers
        components["connectors"] = {
            "ok": all(item.get("last_error") is None for item in self.connectors.health()),
            "connectors": self.connectors.health(),
        }
        outbox_snapshot = self.outbox.snapshot()
        components["notifications"] = {"ok": True, **outbox_snapshot}
        healthy = all(component.get("ok", True) for component in components.values() if isinstance(component, dict))
        return {
            "version": HEALTH_VERSION,
            "generation": self.generation,
            "execution_mode": self.execution_mode,
            "workspace": self.workspace,
            "uptime_seconds": int(time.time() - self._started_at),
            "healthy": healthy,
            "components": components,
        }

    def write_health(self) -> None:
        try:
            service_state.write_health(self.workspace, self.health_snapshot())
        except Exception:  # noqa: BLE001 — health must never take the service down
            pass

    # -- credentials & shutdown ----------------------------------------------

    def rotate_token(self, new_token: str) -> dict:
        if not isinstance(new_token, str) or len(new_token.strip()) < 16:
            raise ValueError("Service token must be at least 16 characters")
        cleaned = new_token.strip()
        from pepsicode.service.cli import _write_token_file

        self.token = cleaned
        _write_token_file(self.workspace, self.token)
        service_state.write_running_state(
            self.workspace,
            port=self.port,
            token=self.token,
            execution_mode=self.execution_mode,
            generation=self.generation,
        )
        fingerprint = service_state.token_fingerprint(self.token)
        self.record_event(
            "_service",
            "service/notice",
            {"kind": "token_rotated", "token_fingerprint": fingerprint},
        )
        return {"rotated": True, "generation": self.generation, "token_fingerprint": fingerprint}

    def request_shutdown(self, *, reason: str = "shutdown") -> None:
        self._shutdown_requested.set()
        service_state.mark_manual_stop(self.workspace, reason=reason)

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested.is_set()
