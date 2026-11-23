"""WebSocket server exposing pepsicode's agent loop to external clients.

Runs the synchronous :func:`run_agent_turn_stream` in a background thread and
bridges its callbacks to asynchronous WebSocket events.  Permission prompts are
forwarded to the client as ``permission/request`` events; the client must reply
with a ``tool/approve`` request whose ``id`` matches the prompt id.

Usage::

    python -m pepsicode.server --port 8765

The server listens on ``ws://127.0.0.1:<port>`` and speaks the JSON-RPC
envelope defined in :mod:`pepsicode.server.app.protocol`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast

from pepsicode.agents.status import session_trace_view
from pepsicode.cli.cli_commands import expand_prompt_command, get_slash_commands, prompt_catalog
from pepsicode.config import (  # noqa: F401 - re-exported for test monkeypatching
    load_runtime_config,
    save_mini_code_settings,
)
from pepsicode.context.context_manager import ContextManager
from pepsicode.context.prompt import build_session_prompt  # noqa: F401 - re-exported for test monkeypatching
from pepsicode.core.agent_loop import run_agent_turn_stream  # noqa: F401 - re-exported for test monkeypatching
from pepsicode.core.session import (
    SessionData,
    list_sessions,
    load_session,
    normalize_workspace,
    same_workspace,
    save_session,
)
from pepsicode.core.session_tree import (
    branch_path,
    checkout_entry,
    copy_session,
    record_messages,
    tree_view,
)
from pepsicode.core.verification import (
    restore_verification_state,
    run_verified_turn,  # noqa: F401 - re-exported for test monkeypatching
    snapshot_verification_state,
)
from pepsicode.hooks import create_hook_engine
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.llm.model_discovery import refresh_model_catalog
from pepsicode.llm.model_settings import (
    apply_model_settings,
    model_capabilities,
    update_context_window,
    validate_model_settings,
)
from pepsicode.llm.provider_setup import probe_provider_setup, public_setup_result, save_provider_setup
from pepsicode.llm.providers import model_settings_snapshot, provider_runtime
from pepsicode.logging_config import get_logger, setup_logging
from pepsicode.server import protocol, turn_runner
from pepsicode.server.profiles import profile_prompt
from pepsicode.server.security import authenticate
from pepsicode.server.session_runtime import (
    ClientSession,
    ConnectionSession,
    RemotePermissionBridge,
    ServiceApprovalBackend,
)
from pepsicode.service.events import JOURNALED_EVENTS
from pepsicode.tools import create_default_tool_registry
from pepsicode.types import ChatMessage
from pepsicode.usage import usage_snapshot

__all__ = [
    "ClientSession",
    "ConnectionSession",
    "PepsiCodeServer",
    "RemotePermissionBridge",
    "ServiceApprovalBackend",
    "main",
]

logger = get_logger("server")

# Methods that can be served before any session is attached (service mode
# starts connections with no session at all).
_SESSIONLESS_METHODS = frozenset(
    {
        "session/create",
        "session/list",
        "session/resume",
        "service/health",
        "service/status",
        "service/rotate_token",
        "service/shutdown",
        "notifications/fetch",
    }
)

# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


class PepsiCodeServer:
    def __init__(
        self,
        port: int = 8765,
        host: str = "127.0.0.1",
        *,
        token: str | None = None,
        allowed_origins=None,
        service_host: Any | None = None,
    ) -> None:
        self.port = port
        self.host = host
        self.token = token or os.environ.get("PEPSI_CODE_SERVER_TOKEN") or secrets.token_urlsafe(32)
        if allowed_origins is None:
            allowed_origins = json.loads(os.environ.get("PEPSI_CODE_SERVER_ORIGINS", '["null", "file://"]'))
        if not isinstance(allowed_origins, (list, set, tuple)) or not all(
            isinstance(origin, str) and origin and origin != "*" for origin in allowed_origins
        ):
            raise ValueError("Server origins must be an explicit array of origins")
        self.allowed_origins = set(allowed_origins)
        self._sessions: set[ClientSession] = set()
        self._bound_port: int | None = None
        # Service mode (M3): sessions are owned by the ServiceHost and survive
        # client disconnects.  None means the classic per-connection mode.
        self.service_host = service_host
        self._shutdown_event: asyncio.Event | None = None
        self._shutdown_task: asyncio.Task | None = None

    @property
    def service_mode(self) -> bool:
        return self.service_host is not None

    def _require_service_host(self) -> Any:
        if self.service_host is None:
            raise ValueError("Service mode is not active")
        return self.service_host

    async def _close_session(self, session: ClientSession, bridge: RemotePermissionBridge) -> None:
        session._closing = True
        session._cancellation_event.set()
        bridge.cancel_pending(close=True)
        if session._close_task is None:

            async def drain():
                try:
                    if session._background_tasks:
                        await asyncio.gather(*list(session._background_tasks), return_exceptions=True)
                finally:
                    try:
                        # Cancelling the asyncio wrapper doesn't terminate its
                        # executor thread. Wait for the actual worker to exit.
                        while session._worker_active.is_set():
                            await asyncio.sleep(0.02)
                        await asyncio.to_thread(session.dispose)
                    except Exception as error:  # noqa: BLE001
                        logger.warning("Session resource cleanup failed: %s", error)
                    self._sessions.discard(session)

            session._close_task = asyncio.create_task(drain())
        try:
            await asyncio.wait_for(asyncio.shield(session._close_task), timeout=5)
        except TimeoutError:
            # Keep cleanup attached to the still-running worker; never dispose
            # its tools while it can still be using them.
            logger.warning("Cancelled session worker is still finishing; cleanup remains scheduled")

    async def _detach_session(self, session: ClientSession, bridge: RemotePermissionBridge) -> None:
        """Service-mode detach: unsubscribe without cancelling owned work.

        Closing a control connection only removes this client's subscription —
        the authorized turn keeps running, prompts fall back to the policy
        backend, and a reconnecting client reads the journal replay.
        """
        host = self._require_service_host()
        session._closing = False  # the session outlives this connection
        bridge.cancel_pending()  # deny prompts queued for the dead socket
        perms = session.permissions
        data = session.session_data
        approval = perms.approval if perms is not None else None
        if isinstance(approval, ServiceApprovalBackend) and approval._bridge is bridge:
            # Fall back to the clientless policy backend while nobody is attached.
            assert perms is not None
            perms.approval = approval.for_client(None, data.session_id if data is not None else None)
        elif approval is bridge and data is not None:
            assert perms is not None
            perms.approval = ServiceApprovalBackend(host, None, data.session_id)
        session._bridge = None
        if data is not None:
            host.detach(data.session_id)
            host.record_event(
                data.session_id,
                "service/notice",
                {"kind": "client_detached", "policy_fallback": True},
            )

    # -- service-mode helpers ------------------------------------------------

    def _bind_control_connection(self, session: ClientSession, bridge: RemotePermissionBridge | None) -> None:
        """Bind a control connection to a (resident) session.

        Approvals always flow through :class:`ServiceApprovalBackend` in
        service mode: policy-covered actions are auto-allowed (audited), the
        rest is forwarded to this client while it stays attached.
        """
        session._bridge = bridge
        if bridge is not None:
            bridge._cancellation_event = session._cancellation_event
        if self.service_mode:
            session_id = session.session_data.session_id if session.session_data else None
            session.require_permissions().approval = ServiceApprovalBackend(self.service_host, bridge, session_id)
        elif bridge is not None:
            session.set_permission_handler(bridge)

    def _register_service_session(self, session: ClientSession) -> None:
        host = self._require_service_host()
        data = session.require_session_data()
        host.register_session(data.session_id, session)
        host.attach(data.session_id)
        host.record_event(data.session_id, "service/notice", {"kind": "client_attached"})

    def _session_event_cursor(self, session: ClientSession) -> int:
        host = self._require_service_host()
        data = session.require_session_data()
        return int(host.journal.for_session(data.session_id).latest_cursor)

    def _orchestration_pending_only(self, session: ClientSession) -> bool:
        """True when the session has queued orchestration work and nothing
        that requires reconciliation (claimed or recovery_pending)."""
        data = session.session_data
        if data is None:
            return False
        orchestration = data.task_control.get("orchestration") or {}
        effects = orchestration.get("effects") or []
        if not effects:
            return False
        return all(effect.get("state") == "pending" for effect in effects)

    def _hydrate_loaded_session(
        self, session: ClientSession, loaded: SessionData, bridge: RemotePermissionBridge | None
    ) -> None:
        """Bind a freshly loaded session into a (possibly headless) ClientSession.

        A resumed conversation owns independent permissions, cost, hook,
        model, and tool state even when it shares the same workspace.
        """
        session.initialize(force=session.tools is not None)
        self._bind_control_connection(session, bridge)
        session.session_data = loaded
        session.input_queue.bind_session(loaded)
        session.messages = list(cast(list[ChatMessage], loaded.messages))
        session.tools.restore_loaded_tools(loaded.loaded_tools)
        restore_verification_state(session.tools, session.runtime, loaded.verification_state, loaded.messages)
        if loaded.model_settings and session.runtime and not isinstance(session.model, MockModelAdapter):
            try:
                session.update_model(loaded.model_settings)
            except ValueError as error:
                logger.warning("Could not restore session model settings: %s", error)
        if session.permissions:
            session.permissions.set_access_mode(loaded.access_mode)
            session.permissions.restore_plan_state(loaded.permission_mode, loaded.plan_file_path)
        session._rebuild_system_prompt()
        record_messages(loaded, cast(list[dict[str, Any]], session.messages))

    def _headless_emit(self, session: ClientSession) -> Any:
        """Emit closure for client-less turns: journal key events only."""
        host = self._require_service_host()

        async def emit(event_name: str, data: dict[str, Any]) -> None:
            if session.session_data is not None and event_name in JOURNALED_EVENTS:
                host.record_event(session.session_data.session_id, event_name, data)

        return emit

    async def _startup_auto_resume(self) -> None:
        """B09: resume accepted work after a service restart with no client.

        Only sessions whose orchestration effects are all still ``pending``
        resume autonomously; anything claimed or awaiting reconciliation is
        left for explicit human continuation and surfaced as a notification.
        Resumes run sequentially so the per-workspace orchestration lock is
        never contended between two resident sessions.
        """
        host = self._require_service_host()
        _policy, effective, reason = host.policy_effective()
        if not effective:
            logger.info("Service startup resume skipped: %s", reason)
            return
        try:
            candidates = await asyncio.to_thread(list_sessions, workspace=host.workspace)
        except Exception as error:  # noqa: BLE001 — a scan failure must not stop the service
            logger.warning("Service startup resume scan failed: %s", error)
            return
        for meta in candidates:
            if host.shutdown_requested:
                return
            if host.get_session(meta.session_id) is not None:
                continue  # already resident (client attached first)
            data = await asyncio.to_thread(load_session, meta.session_id)
            if data is None or not same_workspace(data.workspace or "", host.workspace):
                continue
            orchestration = data.task_control.get("orchestration") or {}
            if not orchestration.get("work"):
                continue
            effects = orchestration.get("effects") or []
            if not effects or not all(effect.get("state") == "pending" for effect in effects):
                host.notify(
                    dedup_key=f"resume:{meta.session_id}",
                    title="Queued work awaits explicit continuation",
                    body="Effects need reconciliation after the service restart; reconnect and continue.",
                )
                continue
            try:
                session = ClientSession(normalize_workspace(data.workspace or host.workspace))
                host.register_session(meta.session_id, session)
                await asyncio.to_thread(self._hydrate_loaded_session, session, data, None)
                host.attach(meta.session_id)
                host.record_event(meta.session_id, "service/notice", {"kind": "service_resume_started"})
                await self._run_turn_background(
                    0,
                    {"input": "继续"},
                    session,
                    self._headless_emit(session),
                    None,
                    asyncio.get_running_loop(),
                )
                host.detach(meta.session_id)
            except Exception as error:  # noqa: BLE001 — one session must not block the rest
                logger.warning("Service startup resume failed for %s: %s", meta.session_id, error)
                host.notify(
                    dedup_key=f"resume:{meta.session_id}",
                    title="Automatic resume failed",
                    body=str(error)[:300],
                )

    def _maybe_service_resume(self, session: ClientSession, emit: Any, loop: Any) -> None:
        """Auto-resume accepted work after a service restart (M3 §19.1).

        With an effective unattended policy, queued orchestration work is
        resumed by driving the same continuation path a user would trigger —
        as a background task on the resident session.  Anything requiring
        reconciliation (claimed/recovery_pending effects, unknown side
        effects) is left for explicit human continuation.
        """
        host = self._require_service_host()
        if session.session_data is None or session._worker_active.is_set() or session._background_tasks:
            return
        orchestration = session.session_data.task_control.get("orchestration") or {}
        if not orchestration.get("work"):
            return
        _policy, effective, reason = host.policy_effective()
        if not effective:
            logger.info("Service resume skipped for %s: %s", session.session_data.session_id, reason)
            host.notify(
                dedup_key=f"resume:{session.session_data.session_id}",
                title="Queued work awaits continuation",
                body=f"Automatic resume is unavailable: {reason}. Reconnect and continue explicitly.",
            )
            return
        if not self._orchestration_pending_only(session):
            logger.info(
                "Service resume needs explicit continuation for %s (reconciliation pending)",
                session.session_data.session_id,
            )
            return
        host.record_event(session.session_data.session_id, "service/notice", {"kind": "service_resume_started"})
        task = asyncio.create_task(self._run_turn_background(0, {"input": "继续"}, session, emit, None, loop))
        session._background_tasks.add(task)
        task.add_done_callback(session._background_tasks.discard)

    async def _graceful_shutdown(self, reason: str) -> None:
        """Drain resident sessions, persist the stop marker, stop the loop."""
        host = self._require_service_host()
        host.request_shutdown(reason=reason)
        logger.info("Service shutdown requested (%s); draining sessions", reason)
        for session in [host.get_session(sid) for sid in host.resident_session_ids()]:
            if session is None:
                continue
            session._cancellation_event.set()
            session._closing = True
            if session._bridge is not None:
                session._bridge.cancel_pending(close=True)
        # Give running turns a bounded window to converge.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if all(
                not session._worker_active.is_set()
                for session in (host.get_session(sid) for sid in host.resident_session_ids())
                if session is not None
            ):
                break
            await asyncio.sleep(0.05)
        for sid in host.resident_session_ids():
            session = host.get_session(sid)
            if session is None:
                continue
            try:
                await asyncio.to_thread(session.dispose)
            except Exception as error:  # noqa: BLE001
                logger.warning("Session dispose failed during shutdown: %s", error)
            host.drop_session(sid)
        host.write_health()
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a single raw TCP client (fallback when websockets is absent)."""
        # This path is a minimal SSE-style fallback; primary path uses websockets.
        pass

    async def handle_ws(self, websocket: Any) -> None:
        """Main WebSocket message loop for one client connection."""
        auth = await authenticate(websocket, self.token, self.allowed_origins)
        if auth is None:
            return
        cwd = self.service_host.workspace if self.service_host is not None else str(Path.cwd())
        attachment = ConnectionSession(
            None if self.service_mode else ClientSession(cwd), orchestration=auth.get("orchestration") == 1
        )
        loop = asyncio.get_running_loop()

        async def emit(event_name: str, data: dict[str, Any]) -> None:
            msg = protocol.make_event(event_name, data)
            try:
                await websocket.send(json.dumps(msg, ensure_ascii=False, default=str))
            except Exception as error:  # noqa: BLE001
                logger.warning("Failed to send event %s: %s", event_name, error)
            if self.service_host is not None:
                # Service mode journals key events so detached clients can
                # catch up after reconnecting.
                attached = attachment.session
                if attached is not None and attached.session_data is not None and event_name in JOURNALED_EVENTS:
                    self.service_host.record_event(attached.session_data.session_id, event_name, data)

        cancellation = attachment.session._cancellation_event if attachment.session is not None else None
        bridge = RemotePermissionBridge(loop, emit, cancellation)
        if attachment.session is not None:
            attachment.session._bridge = bridge
            self._sessions.add(attachment.session)

        # Defer initialization until the first session/create request so the
        # client can pick a cwd.  But we need a default cwd to start.
        try:
            from pepsicode.build_info import build_identity
            from pepsicode.version import VERSION

            ready = {"cwd": cwd, "version": VERSION, "build": build_identity(), "capabilities": {"orchestration": 1}}
            if self.service_host is not None:
                ready.update(
                    {
                        "service": True,
                        "execution_mode": self.service_host.execution_mode,
                        "generation": self.service_host.generation,
                    }
                )
            await emit("connection/ready", ready)
        except Exception:
            pass

        async def publish_agents():
            last = None
            while True:
                await asyncio.sleep(0.25)
                session = attachment.session
                if session is None:
                    continue
                data = session.session_data
                if data is None:
                    continue
                if (data.session_id, data.agent_traces.sequence) == last:
                    continue
                view = await asyncio.to_thread(session_trace_view, data)
                key = (view["session_id"], view["sequence"])
                current = attachment.session
                if current is not None and data is current.session_data and key != last:
                    last = key
                    await emit("agent/update", view)

        publisher = asyncio.create_task(publish_agents())
        try:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send(
                        json.dumps(
                            protocol.make_response(
                                0, error=protocol.make_error(protocol.ERR_PARSE_ERROR, "Invalid JSON")
                            )
                        )
                    )
                    continue

                kind = message.get("kind", "request")
                if kind == "request":
                    await self._handle_request(message, attachment, bridge, emit, websocket, loop)
                elif kind == "response":
                    # Client replying to a permission request
                    prompt_id = message.get("id")
                    result = message.get("result", {})
                    if prompt_id and isinstance(result, dict):
                        bridge.resolve(str(prompt_id), result)
        finally:
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
            if attachment.session is not None:
                if self.service_mode:
                    await self._detach_session(attachment.session, bridge)
                else:
                    await self._close_session(attachment.session, bridge)

    async def _handle_request(
        self,
        request: dict[str, Any],
        target: ClientSession | ConnectionSession,
        bridge: RemotePermissionBridge,
        emit: Any,
        websocket: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        req_id = request.get("id", 0)
        method = request.get("method", "")
        params = request.get("params", {})
        # Direct callers (tests, embeddings) may pass a bare ClientSession;
        # the WebSocket loop passes the per-connection attachment holder.
        attachment = target if isinstance(target, ConnectionSession) else ConnectionSession(target)
        session = attachment.session

        # turn/run is long-running: don't block the WebSocket loop.
        # Run it as a background task and send the response when done.
        if method in {"turn/run", "turn/regenerate"}:
            if session is None or session._closing or session._background_tasks or session._turn_lock.locked():
                await websocket.send(
                    json.dumps(
                        protocol.make_response(
                            req_id,
                            error=protocol.make_error(
                                protocol.ERR_INTERNAL, "A turn is already running or the connection is closing"
                            ),
                        )
                    )
                )
                return
            session._cancellation_event.clear()
            if method == "turn/regenerate":
                params = {"regenerate_entry_id": params.get("entry_id")}
            params = {**params, "_orchestration_client": attachment.orchestration}

            def on_queue_update(update: dict[str, Any]) -> None:
                asyncio.run_coroutine_threadsafe(emit("input/queue", update), loop)

            session.input_queue.start(on_queue_update)
            task = asyncio.create_task(self._run_turn_background(req_id, params, session, emit, websocket, loop))
            session._background_tasks.add(task)
            task.add_done_callback(session._background_tasks.discard)
            return

        try:
            result = await self._dispatch(method, params, attachment, bridge, emit, loop)
            response = protocol.make_response(req_id, result=result)
        except Exception as error:  # noqa: BLE001
            logger.exception("Error handling %s", method)
            response = protocol.make_response(req_id, error=protocol.make_error(protocol.ERR_INTERNAL, str(error)))
        try:
            await websocket.send(json.dumps(response, ensure_ascii=False, default=str))
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to send response for %s: %s", method, error)
        if method == "session/detach" and "error" not in response:
            # An explicit detach ends this control connection cleanly.
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    async def _run_turn_background(
        self,
        req_id: int | str,
        params: dict[str, Any],
        session: ClientSession,
        emit: Any,
        websocket: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Run a turn in the background, send response when complete."""
        await turn_runner.run_turn_background(self, req_id, params, session, emit, websocket, loop)

    async def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        target: ClientSession | ConnectionSession,
        bridge: RemotePermissionBridge,
        emit: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> Any:
        attachment = target if isinstance(target, ConnectionSession) else ConnectionSession(target)
        session: ClientSession | None = attachment.session
        if session is None and method not in _SESSIONLESS_METHODS:
            raise ValueError("Create or resume a session first")

        if method == "status":
            if session is None:
                return {"status": "ok", "cwd": str(Path.cwd())}
            return {"status": "ok", "cwd": session.cwd}

        if method == "service/health":
            return self._require_service_host().health_snapshot()

        if method == "service/status":
            host = self._require_service_host()
            return {
                "generation": host.generation,
                "execution_mode": host.execution_mode,
                "workspace": host.workspace,
                "sessions": host.resident_session_ids(),
                "shutdown_requested": host.shutdown_requested,
                "health": host.health_snapshot(),
            }

        if method == "service/rotate_token":
            host = self._require_service_host()
            result = host.rotate_token(params.get("new_token", ""))
            self.token = host.token
            return result

        if method == "service/shutdown":
            # Explicit service stop: persists the manual-stop marker (blocking
            # watchdog restarts), then drains gracefully in the background.
            host = self._require_service_host()
            reason = params.get("reason", "shutdown")
            self._shutdown_task = asyncio.create_task(self._graceful_shutdown(reason=str(reason)))
            return {"status": "shutting_down", "generation": host.generation}

        if method == "notifications/fetch":
            host = self._require_service_host()
            items, next_cursor = host.outbox.fetch_after(params.get("cursor", 0), limit=int(params.get("limit", 100)))
            return {"notifications": items, "cursor": next_cursor}

        if method == "session/events":
            host = self._require_service_host()
            if session is None or session.session_data is None:
                raise ValueError("Create or resume a session first")
            cursor = params.get("cursor", 0)
            if type(cursor) is not int or cursor < 0:
                raise ValueError("cursor must be a non-negative integer")
            events, next_cursor = host.journal.for_session(session.session_data.session_id).read_from(
                cursor, limit=int(params.get("limit", 200))
            )
            return {"events": events, "cursor": next_cursor}

        if method == "session/detach":
            if session is None or session.session_data is None:
                return {"detached": False}
            return {"detached": True, "session_id": session.session_data.session_id}

        if method == "session/list":
            workspace = params.get("workspace") or (session.cwd if session else str(Path.cwd()))
            sessions = list_sessions(workspace=workspace)
            return {
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "created_at": s.created_at,
                        "updated_at": s.updated_at,
                        "first_message": s.first_message,
                        "message_count": s.message_count,
                        "workspace": s.workspace,
                    }
                    for s in sessions
                ]
            }

        if method == "agent/snapshot":
            assert session is not None
            data = session.session_data
            if data is None or params.get("session_id", data.session_id) != data.session_id:
                raise ValueError("Agent status is available for the active session only")
            offset, trace_id = params.get("offset", 0), params.get("trace_id")
            if type(offset) is not int or offset < 0:
                raise ValueError("offset must be a non-negative integer")
            if trace_id is not None and (not isinstance(trace_id, str) or not 0 < len(trace_id) <= 64):
                raise ValueError("Invalid trace_id")
            return await asyncio.to_thread(session_trace_view, data, offset=offset, trace_id=trace_id)

        if method == "agent/cancel":
            assert session is not None
            data = session.session_data
            trace_id = params.get("trace_id")
            if data is None or params.get("session_id", data.session_id) != data.session_id:
                raise ValueError("Worker cancellation is available for the active session only")
            if not isinstance(trace_id, str) or not 0 < len(trace_id) <= 64:
                raise ValueError("Invalid trace_id")
            state = await asyncio.to_thread(session.input_queue.stop_worker, trace_id)
            return {"trace_id": trace_id, "state": state}

        if method == "session/tree":
            assert session is not None
            data = session.require_session_data()
            if not session._worker_active.is_set():
                record_messages(data, cast(list[dict[str, Any]], session.messages))
            return tree_view(data)

        if method in {"session/checkout", "session/fork", "session/clone"}:
            assert session is not None
            if session._turn_lock.locked() or session._worker_active.is_set() or session._background_tasks:
                raise ValueError("Wait for the current turn to finish before changing branches")
            async with session._turn_lock:
                return await self._change_branch(method, params, session, emit)

        if method == "session/create":
            if session is not None and (session._turn_lock.locked() or session._worker_active.is_set()):
                raise ValueError("Wait for the current turn to finish before creating a session")
            profile = params.get("profile", "default")
            profile_prompt(profile)  # Validate before changing the current session.
            host = self.service_host
            cwd = normalize_workspace(params.get("cwd", host.workspace if host is not None else str(Path.cwd())))
            if host is not None and not same_workspace(cwd, host.workspace):
                raise ValueError("Create a session in the workspace owned by this service")
            previous = session
            if host is not None or session is None:
                session = ClientSession(cwd)
            session.cwd = cwd
            session.initialize(force=session.tools is not None)
            if host is not None:
                if previous is not None:
                    await self._detach_session(previous, bridge)
                attachment.session = session
            self._bind_control_connection(session, bridge)
            data = session.require_session_data()
            data.profile = profile
            session._rebuild_system_prompt()
            if profile != "default":
                session.snapshot()
            if host is not None:
                self._register_service_session(session)
                self._maybe_service_resume(session, emit, loop)
            return {
                "session_id": data.session_id,
                "agents": session_trace_view(data),
                "cwd": session.cwd,
                **(
                    {
                        "event_cursor": self._session_event_cursor(session),
                        "service": True,
                        "generation": host.generation,
                        "execution_mode": host.execution_mode,
                    }
                    if host is not None
                    else {}
                ),
                **session.model_settings(),
                "tools": [t.name for t in session.tools.list()],
                "permissions": session.permissions.get_summary() if session.permissions else [],
                "access_mode": session.permissions.access_mode.value if session.permissions else "auto",
                "profile": data.profile,
            }

        if method == "session/resume":
            session_id = params.get("session_id", "")
            host = self.service_host
            if host is not None:
                resident: ClientSession | None = host.get_session(session_id)
                if resident is not None:
                    # Re-attach to a live resident session.  This is the
                    # reconnect path: it must work even while a turn runs.
                    if host.attached(session_id) > 0:
                        raise PermissionError("Another control client is attached to this session")
                    attachment.session = resident
                    self._bind_control_connection(resident, bridge)
                    host.attach(session_id)
                    host.flush_pending_external(session_id)
                    self._maybe_service_resume(resident, emit, loop)
                    resident_data = resident.require_session_data()
                    return {
                        "session_id": session_id,
                        **resident.conversation_view(),
                        "workspace": resident.cwd,
                        "profile": resident_data.profile,
                        **resident.model_settings(),
                        "permission_mode": resident.permissions.mode.value if resident.permissions else "default",
                        "access_mode": resident.permissions.access_mode.value if resident.permissions else "auto",
                        "plan_file": resident.permissions.plan_file_path if resident.permissions else None,
                        "event_cursor": self._session_event_cursor(resident),
                        "service": True,
                    }
            if session is not None and (session._turn_lock.locked() or session._worker_active.is_set()):
                raise ValueError("Wait for the current turn to finish before resuming a session")
            loaded = load_session(session_id)
            if loaded is None:
                raise ValueError(f"Session not found: {session_id}")
            profile_prompt(loaded.profile)
            expected_workspace = params.get("workspace")
            if expected_workspace and not same_workspace(loaded.workspace, expected_workspace):
                raise ValueError("Session does not belong to the current workspace")
            loaded_workspace = normalize_workspace(loaded.workspace or (session.cwd if session else str(Path.cwd())))
            if host is not None:
                if session is not None:
                    raise ValueError("This connection already controls a session; detach first")
                session = ClientSession(loaded_workspace)
                attachment.session = session
                host.register_session(loaded.session_id, session)
            elif session is None:
                session = ClientSession(loaded_workspace)
                attachment.session = session
            session.cwd = loaded_workspace
            self._hydrate_loaded_session(session, loaded, bridge)
            if host is not None:
                self._register_service_session(session)
                self._maybe_service_resume(session, emit, loop)
            return {
                "session_id": loaded.session_id,
                "messages": len(session.messages),
                "message_count": len(session.messages),
                "history": protocol.serialize_session_history([node["message"] for node in branch_path(loaded)]),
                "tree": tree_view(loaded),
                "verification": (session.runtime or {}).get("_lastVerification"),
                "pending_inputs": loaded.task_control.get("inbox", {}).get("pending", []),
                "task": loaded.task_control.get("task"),
                "agents": session_trace_view(loaded),
                "workspace": session.cwd,
                "profile": loaded.profile,
                **session.model_settings(),
                "permission_mode": session.permissions.mode.value if session.permissions else "default",
                "access_mode": session.permissions.access_mode.value if session.permissions else "auto",
                "plan_file": session.permissions.plan_file_path if session.permissions else None,
                **({"event_cursor": self._session_event_cursor(session)} if host is not None else {}),
            }

        assert session is not None

        if method == "permissions/set_workspace_default":
            if session._turn_lock.locked() or session._worker_active.is_set():
                raise ValueError("Wait for the current turn to finish before changing permissions")
            from pepsicode.workspace_permissions import set_workspace_default

            mode = params.get("mode", "")
            set_workspace_default(session.cwd, mode)
            return {"workspace_default": mode}

        if method == "permissions/set":
            if session._turn_lock.locked() or session._worker_active.is_set():
                raise ValueError("Wait for the current turn to finish before changing permissions")
            permissions = session.require_permissions()
            data = session.require_session_data()
            permissions.set_access_mode(params.get("mode", ""))
            data.access_mode = permissions.access_mode.value
            data.permission_mode = permissions.mode.value
            data.plan_file_path = permissions.plan_file_path
            session._rebuild_system_prompt()
            data.messages = list(cast(list[dict[str, Any]], session.messages))
            save_session(data)
            return {"access_mode": permissions.access_mode.value}

        if method == "model/settings":
            candidate = provider_runtime(session.runtime or {"model": "mock"}, params.get("provider"))
            if (
                (params.get("refresh") or params.get("provider"))
                and session.runtime
                and not isinstance(session.model, MockModelAdapter)
            ):
                await asyncio.to_thread(refresh_model_catalog, candidate, force=bool(params.get("refresh")))
                if candidate is session.runtime:
                    update_context_window(candidate, session.context_manager)
            settings = session.model_settings(params.get("provider"))
            model = params.get("model")
            if model is not None:
                validate_model_settings(candidate, model, "default")
                settings["capabilities"] = model_capabilities(candidate, model)
            return settings

        if method == "model/update":
            if params.get("provider") and session.runtime:
                await asyncio.to_thread(refresh_model_catalog, provider_runtime(session.runtime, params["provider"]))
            return session.update_model(params)

        if method in {"provider/probe", "provider/save", "provider/cancel"}:
            if session._turn_lock.locked() or session._worker_active.is_set():
                raise ValueError("请等待当前对话结束后再配置厂商")
            if session.permissions:
                session.permissions.ensure_local_command_allowed("/provider add")
            if method == "provider/cancel":
                session._provider_setup = None
                return {"cancelled": True}
            if method == "provider/probe":
                session._provider_setup = None
                draft = params.get("draft")
                if not isinstance(draft, dict):
                    raise ValueError("配置内容必须为对象")
                candidate = await asyncio.to_thread(probe_provider_setup, dict(draft))
                token = uuid.uuid4().hex
                session._provider_setup = {"token": token, "draft": dict(draft), "candidate": candidate}
                return {"token": token, **public_setup_result(candidate)}
            pending = session._provider_setup
            if not pending or params.get("token") != pending["token"]:
                raise ValueError("请先读取模型列表，再确认保存")
            draft = {**pending["draft"], "model": params.get("model")}
            runtime = session.runtime if session.runtime is not None else {"model": ""}
            save_provider_setup(runtime, draft, pending["candidate"])
            session.runtime = runtime
            if isinstance(session.model, MockModelAdapter) or session.model is None:
                old_tools = session.tools
                verification = snapshot_verification_state(old_tools, session.runtime)
                session.tools = create_default_tool_registry(
                    session.cwd, runtime=runtime, cost_tracker=session.cost_tracker, trace_manager=session.trace_manager
                )
                session.model = AnthropicModelAdapter(runtime, session.tools)
                restore_verification_state(session.tools, runtime, verification)
                if old_tools:
                    old_tools.dispose()
            session.messages[:] = [
                message for message in session.messages if message.get("role") != "assistant_thinking"
            ]
            if session.context_manager is None:
                session.context_manager = ContextManager(model=runtime["model"])
            apply_model_settings(runtime, runtime["model"], "default", context_manager=session.context_manager)
            session.context_manager.summarizer = session.model.summarize
            if session.session_data:
                session.session_data.model_settings = model_settings_snapshot(runtime)
            session._provider_setup = None
            return session.model_settings()

        if method == "session/delete":
            from pepsicode.core.session import delete_session

            session_id = params.get("session_id", "")
            deleted = delete_session(session_id)
            return {"deleted": deleted}

        if method == "turn/run":
            return await self._run_turn(params, session, emit, loop)

        if method == "turn/cancel":
            if session._background_tasks or (session._current_turn_task and not session._current_turn_task.done()):
                session._cancellation_event.set()
                handler = session._bridge or bridge
                if handler is not None:
                    handler.cancel_pending()
                return {"cancelled": True}
            return {"cancelled": False}

        if method == "turn/enqueue":
            if session.session_data is None:
                raise ValueError("Session is still initializing; input has not been accepted")
            if session._closing or session._cancellation_event.is_set():
                raise ValueError("The task is stopping; keep this input for the next task")
            raw_input = params.get("input", "")
            if not isinstance(raw_input, str):
                raise ValueError("Input must contain text")
            expanded = expand_prompt_command(raw_input.strip(), session.cwd)
            item = session.input_queue.enqueue(
                expanded["content"] if expanded else raw_input,
                params.get("mode", "steer"),
                display_content=raw_input if expanded else None,
                command_path=expanded["commandPath"] if expanded else None,
                answer_to=params.get("answer_to", ()),
            )
            if item is None:
                raise ValueError("Input was not queued")
            return {"id": item.id, "mode": item.mode}

        if method == "turn/dequeue":
            return {"removed": session.input_queue.remove(params.get("id", ""))}

        if method == "tool/approve":
            prompt_id = params.get("prompt_id", "")
            decision = params.get("decision", "deny_once")
            feedback = params.get("feedback", "")
            result = {"decision": decision}
            if feedback:
                result["feedback"] = feedback
            resolved = bridge.resolve(prompt_id, result)
            return {"resolved": resolved}

        if method == "hooks/list":
            if session.hook_engine is None:
                return {"hooks": []}
            return {"hooks": session.hook_engine.list_hooks() if hasattr(session.hook_engine, "list_hooks") else []}

        if method == "hooks/reload":
            if session.hook_engine is None:
                return {"reloaded": False}
            # Re-create hook engine

            session.hook_engine = create_hook_engine(session.cwd, session.permissions)
            return {"reloaded": True}

        if method == "plan/enter":
            assert session.permissions is not None
            plan_path = session.permissions.enter_plan_mode()
            session._rebuild_system_prompt()
            return {"plan_file": plan_path}

        if method == "plan/exit":
            assert session.permissions is not None
            session.permissions.mode = session.permissions.previous_mode
            session._rebuild_system_prompt()
            return {"mode": session.permissions.mode.value}

        if method == "plan/approve":
            assert session.permissions is not None
            result = session.permissions.request_plan_approval()
            session._rebuild_system_prompt()
            return result

        if method == "cost/query":
            return session.cost_snapshot()

        if method == "context/query":
            if session.context_manager is None:
                return {"total_tokens": 0, "usage_percentage": 0, "messages_count": len(session.messages)}
            # During a turn the worker owns the current (possibly compacted)
            # message list; session.messages still contains the pre-turn copy.
            if not session._worker_active.is_set() and not session._turn_lock.locked():
                session.context_manager.messages = list(cast(list[dict[str, Any]], session.messages))
            stats = session.context_manager.get_stats()
            return {
                "total_tokens": stats.total_tokens,
                "usage_percentage": stats.usage_percentage,
                "messages_count": stats.messages_count,
                "max_tokens": stats.context_window,
                "context_window": stats.context_window,
                "tool_calls_count": stats.tool_calls_count,
                "is_near_limit": stats.is_near_limit,
                "should_compact": stats.should_compact,
                **(usage_snapshot(session.context_manager, None)["context"] or {}),
            }

        if method == "tools/list":
            if session.tools is None:
                return {"tools": []}
            return {
                "tools": [
                    {"name": t.name, "description": t.description, "is_read_only": t.is_read_only}
                    for t in session.tools.list()
                ]
            }

        if method == "commands/list":
            catalog = prompt_catalog(session.cwd, refresh=bool(params.get("refresh")))
            return {
                "commands": [
                    {
                        "name": command.name,
                        "usage": command.usage,
                        "description": command.description,
                        "source": command.source,
                    }
                    for command in get_slash_commands(session.cwd)
                ],
                "diagnostics": list(catalog.diagnostics),
            }

        if method == "config/validate":
            from pepsicode.config import validate_config

            valid, messages = validate_config(session.cwd)
            return {"valid": valid, "messages": messages}

        raise ValueError(f"Unknown method: {method}")

    async def _run_turn(
        self,
        params: dict[str, Any],
        session: ClientSession,
        emit: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> dict[str, Any]:
        """Run an agent turn in a worker thread, streaming events back."""
        return await turn_runner.run_turn(self, params, session, emit, loop)

    def _record_service_turn_notifications(self, session: ClientSession, task: dict[str, Any] | None) -> None:
        turn_runner.record_service_turn_notifications(self, session, task)

    async def _change_branch(
        self, method: str, params: dict[str, Any], session: ClientSession, emit: Any
    ) -> dict[str, Any]:
        session.snapshot()
        data = session.require_session_data()
        if method == "session/checkout":
            entry_id = params.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id:
                raise ValueError("Missing entry_id")
            draft = checkout_entry(data, entry_id)
        else:
            session.session_data, draft = copy_session(
                data, entry_id=params.get("entry_id"), clone=method == "session/clone"
            )
            data = session.require_session_data()
        session.messages = list(cast(list[ChatMessage], data.messages))
        session.tools.restore_loaded_tools(data.loaded_tools)
        session.input_queue.bind_session(data)
        restore_verification_state(session.tools, session.runtime, data.verification_state, data.messages)
        session._rebuild_system_prompt()
        if session.context_manager:
            session.context_manager.select_branch(
                cast(list[dict[str, Any]], session.messages), session.cwd, session.permissions
            )
        record_messages(data, cast(list[dict[str, Any]], session.messages))
        save_session(data)
        view = session.conversation_view(draft)
        await emit("session/view", view)
        await emit("session/saved", {"session_id": data.session_id})
        return view

    async def _handle_local_command(self, user_input: str, session: ClientSession, emit: Any) -> dict[str, Any]:
        """Handle slash commands like /plan, /hooks, /tools."""
        if session.permissions:
            session.permissions.ensure_local_command_allowed(user_input)
        if user_input.partition(" ")[0] == "/agents":
            from pepsicode.agents.status import format_trace_view

            token = user_input.partition(" ")[2].strip()
            paged = token.isdecimal() and len(token) < 9
            view = await asyncio.to_thread(
                session_trace_view,
                session.session_data,
                trace_id=token if token and not paged else None,
                offset=int(token) if paged else 0,
                limit=5,
            )
            output = format_trace_view(view)
            if view["next_offset"] is not None:
                output += f"\nMore: /agents {view['next_offset']}"
            return {"output": output}
        if user_input.split()[:2] == ["/skills", "install"]:
            from pepsicode.cli.local_tool_shortcuts import SKILL_INSTALL_USAGE, skill_install_shortcut
            from pepsicode.tooling import ToolContext

            shortcut = skill_install_shortcut(user_input)
            if shortcut is None:
                return {"output": SKILL_INSTALL_USAGE}
            result = await asyncio.to_thread(
                session.tools.execute,
                shortcut["toolName"],
                shortcut["input"],
                context=ToolContext(
                    cwd=session.cwd,
                    permissions=session.permissions,
                    hooks=session.hook_engine,
                    memory=session.memory_manager,
                    cancellation_event=session._cancellation_event,
                ),
            )
            if result.ok:
                session.tools.refresh_skills(session.cwd)
            return {"output": result.output + ("\nSkills refreshed. Use /skills to view them." if result.ok else "")}
        if user_input == "/plan" or user_input.startswith("/plan "):
            assert session.permissions is not None
            plan_path = session.permissions.enter_plan_mode()
            task = user_input[len("/plan") :].strip()
            session._rebuild_system_prompt()
            return {"status": "plan_mode", "plan_file": plan_path, "task": task}

        if user_input == "/tools" and session.tools:
            return {"tools": [{"name": t.name, "description": t.description} for t in session.tools.list()]}

        if user_input == "/hooks" or user_input.startswith("/hooks "):
            if session.hook_engine and hasattr(session.hook_engine, "handle_command"):
                result = session.hook_engine.handle_command(user_input)
                return {"output": result}
            return {"output": "Hooks unavailable."}

        # Fall through: try cli_commands
        try:
            from pepsicode.cli.cli_commands import try_handle_local_command

            result = try_handle_local_command(
                user_input,
                tools=session.tools,
                permissions=session.permissions,
                runtime=session.runtime,
                model_adapter=session.model,
                messages=session.messages,
                context_manager=session.context_manager,
                cwd=session.cwd,
            )
            if result is not None:
                if user_input.split()[0] == "/permissions":
                    permissions = session.require_permissions()
                    data = session.require_session_data()
                    session._rebuild_system_prompt()
                    data.access_mode = permissions.access_mode.value
                    data.permission_mode = permissions.mode.value
                    data.plan_file_path = permissions.plan_file_path
                    data.messages = list(cast(list[dict[str, Any]], session.messages))
                    save_session(data)
                    await emit("permissions/settings", {"access_mode": permissions.access_mode.value})
                if user_input.split()[0] in {"/model", "/effort", "/provider"}:
                    if session.session_data and session.runtime and not isinstance(session.model, MockModelAdapter):
                        session.session_data.model_settings = model_settings_snapshot(session.runtime)
                    await emit("model/settings", session.model_settings())
                return {"output": result}
        except Exception as error:  # noqa: BLE001
            logger.warning("Local command failed: %s", error)

        return {"status": "unknown_command", "input": user_input}

    async def start(self) -> None:
        """Start the WebSocket server."""
        try:
            import websockets
        except ImportError:
            logger.error("The 'websockets' package is required. Install it with: pip install websockets")
            sys.exit(1)

        async def handler(websocket: Any) -> None:
            try:
                await self.handle_ws(websocket)
            except Exception as error:  # noqa: BLE001
                if "connection" not in str(error).lower():
                    logger.exception("Client handler error")

        logger.info("Starting pepsicode server on ws://%s:%d", self.host, self.port)
        shutdown_event = asyncio.Event()
        self._shutdown_event = shutdown_event
        background_tasks: set[asyncio.Task[Any]] = set()
        try:
            async with websockets.serve(
                handler, self.host, self.port, origins=[None, *self.allowed_origins], max_size=50 * 1024 * 1024
            ) as listener:
                bound_port = next(iter(listener.sockets)).getsockname()[1]
                self._bound_port = bound_port
                print(f"pepsicode server listening on ws://{self.host}:{bound_port}", flush=True)
                if self.service_mode:
                    background_tasks.add(asyncio.create_task(self._connector_loop()))
                    background_tasks.add(asyncio.create_task(self._health_loop()))
                    background_tasks.add(asyncio.create_task(self._startup_auto_resume()))
                run_forever: asyncio.Future[Any] = asyncio.ensure_future(asyncio.Future())
                shutdown_wait = asyncio.create_task(shutdown_event.wait())
                try:
                    await asyncio.wait({run_forever, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    run_forever.cancel()
                    shutdown_wait.cancel()
        finally:
            for task in background_tasks:
                task.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
            await asyncio.gather(
                *(
                    self._close_session(session, session._bridge)
                    for session in list(self._sessions)
                    if session._bridge is not None
                )
            )

    async def _connector_loop(self) -> None:
        """Poll external connectors and route validated events (M3 §19.3).

        Polling is host-side Python — the Orchestrator model never polls.
        Events steer an active coordination turn or are held pending; they
        never start work or grant authorization on their own.
        """
        host = self._require_service_host()
        shutdown_event = self._shutdown_event
        settings = (host.runtime.get("service") or {}).get("connectors", {})
        intervals = {name: float((settings.get(name) or {}).get("pollIntervalSeconds", 60)) for name in ("github",)}
        while not host.shutdown_requested:
            try:

                def commit(fresh: Any) -> None:
                    policy = host.load_policy()
                    allowed = (policy or {}).get("repositories") or []
                    host.deliver_external_events(fresh, allowed)

                await asyncio.to_thread(host.connectors.poll_once, commit=commit)
            except Exception as error:  # noqa: BLE001 — a connector failure must not kill the loop
                logger.warning("Connector poll failed: %s", error)
            if shutdown_event is None:
                break
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=min(intervals.values(), default=60))
            except TimeoutError:
                continue

    async def _health_loop(self) -> None:
        """Periodically persist the component-health snapshot for the CLI."""
        host = self._require_service_host()
        shutdown_event = self._shutdown_event
        while not host.shutdown_requested:
            await asyncio.to_thread(host.write_health)
            if shutdown_event is None:
                break
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=5)
            except TimeoutError:
                continue


def main() -> None:
    parser = argparse.ArgumentParser(description="pepsicode WebSocket server")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    if not os.environ.get("PEPSI_CODE_SERVER_TOKEN"):
        parser.error("Set PEPSI_CODE_SERVER_TOKEN before starting the standalone server")

    setup_logging(level=args.log_level)
    server = PepsiCodeServer(port=args.port, host=args.host)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Server shutdown requested")
    finally:
        logger.info("Server stopped")


if __name__ == "__main__":
    main()
