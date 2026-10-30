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
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pepsicode.agents.trace import TraceManager
from pepsicode.approval import ApprovalBackend, ApprovalDecision, ApprovalOutcome, ApprovalRequest
from pepsicode.cli.cli_commands import expand_prompt_command, get_slash_commands, prompt_catalog
from pepsicode.config import load_runtime_config, save_mini_code_settings
from pepsicode.context.context_manager import ContextManager
from pepsicode.context.prompt import build_session_prompt
from pepsicode.core.agent_loop import run_agent_turn_stream
from pepsicode.core.input_queue import TurnInputQueue
from pepsicode.core.session import (
    SessionData,
    create_new_session,
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
    format_tree,
    prepare_regeneration,
    record_messages,
    recording_session,
    tree_view,
)
from pepsicode.core.verification import restore_verification_state, run_verified_turn, snapshot_verification_state
from pepsicode.hooks import HookContext, HookEvent, create_hook_engine
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.llm.model_discovery import refresh_model_catalog
from pepsicode.llm.model_settings import (
    apply_model_settings,
    model_capabilities,
    model_catalog,
    model_selection_error,
    update_context_window,
    validate_model_settings,
)
from pepsicode.llm.provider_setup import probe_provider_setup, public_setup_result, save_provider_setup
from pepsicode.llm.providers import model_settings_snapshot, provider_catalog, provider_runtime
from pepsicode.logging_config import get_logger, setup_logging
from pepsicode.permissions import PermissionManager
from pepsicode.server import protocol
from pepsicode.server.profiles import profile_prompt
from pepsicode.server.security import authenticate
from pepsicode.server.thinking import ThinkingStream
from pepsicode.tools import create_default_tool_registry
from pepsicode.types import ChatMessage
from pepsicode.usage import cache_hit_percentage, usage_snapshot

logger = get_logger("server")

# ---------------------------------------------------------------------------
# Remote permission handler
# ---------------------------------------------------------------------------


class RemotePermissionBridge(ApprovalBackend):
    """Bridges synchronous approval requests to async WebSocket events.

    The agent loop runs in a worker thread and calls :meth:`request`
    synchronously.  We stash an ``asyncio.Future`` keyed by a prompt id,
    emit a ``permission/request`` event to the client, and block the worker
    thread on ``future.result()`` until the client replies with
    ``tool/approve``.

    Implements :class:`ApprovalBackend` so the server can be injected
    directly into :class:`PermissionManager` via the ``approval=`` parameter,
    without going through the legacy ``prompt`` callable.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, emit: Any, cancellation_event=None) -> None:
        self._loop = loop
        self._emit = emit  # async callable: emit(event_name, data)
        self._pending: dict[str, Future[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._cancellation_event = cancellation_event
        self._closed = False

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        """Legacy callable entry point — kept for backward compatibility."""
        return self._raw_prompt(request)

    def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        """Typed entry point required by :class:`ApprovalBackend`."""
        raw = self._raw_prompt(req.to_payload())
        decision_str = str(raw.get("decision", "deny_once"))
        try:
            decision = ApprovalDecision(decision_str)
        except ValueError:
            decision = ApprovalDecision.DENY_ONCE
        feedback = str(raw.get("feedback", "")).strip()
        return ApprovalOutcome(decision=decision, feedback=feedback)

    def _raw_prompt(self, request: dict[str, Any]) -> dict[str, Any]:
        """Shared synchronous implementation used by both entry points."""
        prompt_id = uuid.uuid4().hex[:12]
        future: Future[dict[str, Any]] = Future()
        with self._lock:
            if self._closed or (self._cancellation_event is not None and self._cancellation_event.is_set()):
                return {"decision": "deny_once"}
            self._pending[prompt_id] = future

        # Emit the permission request to the client (thread-safe schedule).
        payload = {
            "prompt_id": prompt_id,
            "kind": request.get("kind", "unknown"),
            "summary": request.get("summary", ""),
            "details": request.get("details", []),
            "scope": request.get("scope", ""),
            "choices": request.get("choices", []),
        }
        asyncio.run_coroutine_threadsafe(self._emit("permission/request", payload), self._loop)

        # Block the worker thread until the client responds.
        try:
            return future.result(timeout=300)
        except TimeoutError:
            logger.warning("Permission prompt %s timed out after 300s", prompt_id)
            return {"decision": "deny_once"}
        finally:
            with self._lock:
                self._pending.pop(prompt_id, None)

    def resolve(self, prompt_id: str, response: dict[str, Any]) -> bool:
        """Resolve a pending permission prompt (called from async context)."""
        with self._lock:
            future = self._pending.get(prompt_id)
            if future is None or future.done():
                return False
            future.set_result(response)
        return True

    def cancel_pending(self, *, close: bool = False) -> None:
        with self._lock:
            self._closed = self._closed or close
            for future in self._pending.values():
                if not future.done():
                    future.set_result({"decision": "deny_once", "feedback": "Turn cancelled or connection closed"})


# ---------------------------------------------------------------------------
# Session context (one per client connection)
# ---------------------------------------------------------------------------


class ClientSession:
    """Holds all mutable state for a single connected client."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.session_data: SessionData | None = None
        self.messages: list[ChatMessage] = []
        self.runtime: dict[str, Any] | None = None
        self.model: Any = None
        self.tools: Any = None
        self.permissions: PermissionManager | None = None
        self.hook_engine: Any = None
        self.context_manager: ContextManager | None = None
        self.cost_tracker: CostTracker | None = None
        self.trace_manager: TraceManager | None = None
        self.memory_manager: Any = None
        self._turn_lock = asyncio.Lock()
        self._current_turn_task: asyncio.Task | None = None
        self._worker_active = threading.Event()
        self._cancellation_event = threading.Event()
        self._provider_setup: dict | None = None
        self._closing = False
        self._background_tasks: set[asyncio.Task] = set()
        self._close_task: asyncio.Task | None = None
        self._bridge: RemotePermissionBridge | None = None
        self.input_queue = TurnInputQueue()

    def initialize(self, *, force: bool = False) -> None:
        """Initialize workspace-bound services, rebuilding them when requested."""
        if self.tools is not None and not force:
            return
        if self.tools is not None:
            try:
                self.tools.dispose()
            except Exception as error:  # noqa: BLE001
                logger.warning("Failed to dispose tools while switching workspace: %s", error)

        self.messages = []
        self._provider_setup = None
        self.runtime = None
        self.model = None
        self.tools = None
        self.permissions = None
        self.hook_engine = None
        self.context_manager = None
        self.cost_tracker = None
        self.trace_manager = None
        self.memory_manager = None
        try:
            self.runtime = load_runtime_config(self.cwd)
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to load runtime config: %s", error)
            self.runtime = None

        self.cost_tracker = CostTracker()
        if self.runtime is not None and os.environ.get("PEPSI_CODE_MODEL_MODE") != "mock":
            refresh_model_catalog(self.runtime)
        self.trace_manager = TraceManager()
        self.tools = create_default_tool_registry(
            self.cwd, runtime=self.runtime, cost_tracker=self.cost_tracker, trace_manager=self.trace_manager
        )
        from pepsicode.workspace_permissions import get_workspace_default

        self.permissions = PermissionManager(
            self.cwd,
            prompt=None,
            mode=get_workspace_default(self.cwd),
            process_sandbox_mode=(self.runtime or {}).get("processSandboxMode", "danger-full-access"),
        )  # set later
        self.hook_engine = create_hook_engine(self.cwd, self.permissions)
        self.hook_engine.emit(
            HookEvent.STARTUP,
            HookContext(
                event=HookEvent.STARTUP,
                cwd=self.cwd,
                metadata={"permission_mode": self.permissions.mode.value, "agent_scope": "main"},
            ),
        )

        if self.runtime is None or os.environ.get("PEPSI_CODE_MODEL_MODE") == "mock":
            self.model = MockModelAdapter()
        else:
            self.model = AnthropicModelAdapter(self.runtime, self.tools)

        if self.runtime:
            self.context_manager = ContextManager(model=self.runtime.get("model", "default"))
            update_context_window(self.runtime, self.context_manager)
            summarize = getattr(self.model, "summarize", None)
            if summarize is not None:
                self.context_manager.summarizer = summarize

        self.session_data = create_new_session(self.cwd)
        self.input_queue.bind_session(self.session_data)
        from pepsicode.context.memory import create_memory_manager

        self.memory_manager = create_memory_manager(self.cwd)
        self._rebuild_system_prompt()

    def _rebuild_system_prompt(self) -> None:
        if not self.permissions or not self.tools:
            return
        prompt = build_session_prompt(
            self.cwd,
            self.permissions.get_summary(),
            skills=self.tools.get_skills(),
            mcp_servers=self.tools.get_mcp_servers(),
            memory_manager=self.memory_manager,
            plan_mode=self.permissions.is_plan_mode,
            plan_file_path=self.permissions.plan_file_path,
        )
        presentation = profile_prompt(self.session_data.profile if self.session_data else "default")
        if presentation:
            prompt += "\n\n" + presentation
        # Replace existing system prompt or prepend
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": prompt}
        else:
            self.messages.insert(0, {"role": "system", "content": prompt})

    def snapshot(self) -> None:
        if self.session_data is None:
            raise ValueError("Create a session first")
        record_messages(self.session_data, self.messages)
        self.session_data.verification_state = snapshot_verification_state(self.tools, self.runtime)
        if self.permissions:
            self.session_data.permission_mode = self.permissions.mode.value
            self.session_data.access_mode = self.permissions.access_mode.value
            self.session_data.plan_file_path = self.permissions.plan_file_path
        if self.runtime and not isinstance(self.model, MockModelAdapter):
            self.session_data.model_settings = model_settings_snapshot(self.runtime)
        save_session(self.session_data)

    def conversation_view(self, draft: str = "") -> dict:
        data = self.session_data
        view = tree_view(data)
        return {
            "session_id": data.session_id,
            "workspace": self.cwd,
            "history": protocol.serialize_session_history([node["message"] for node in branch_path(data)]),
            "tree": view,
            "draft": draft,
            "verification": (self.runtime or {}).get("_lastVerification"),
            "execution": self.session_data.execution_state,
            "task": self.session_data.task_control.get("task"),
            "pending_inputs": self.session_data.task_control.get("inbox", {}).get("pending", []),
        }

    def set_permission_handler(self, handler: RemotePermissionBridge) -> None:
        assert self.permissions is not None
        # RemotePermissionBridge now implements ApprovalBackend directly.
        self.permissions.approval = handler
        self._bridge = handler

    def dispose(self) -> None:
        try:
            if self.tools is not None:
                self.tools.dispose()
        finally:
            try:
                if self.hook_engine is not None:
                    self.hook_engine.close()
            finally:
                if self.permissions is not None:
                    self.permissions.close()

    def cost_snapshot(self) -> dict[str, Any]:
        tracker = self.cost_tracker
        entries = [{"model": model, **vars(usage)} for model, usage in (tracker.model_usage.items() if tracker else [])]
        total_input = sum(entry["input_tokens"] for entry in entries)
        total_read = sum(entry["cache_read_tokens"] for entry in entries)
        total_write = sum(entry["cache_write_tokens"] for entry in entries)
        return {
            "total_cost_usd": tracker.total_cost_usd if tracker else 0,
            "total_tokens": tracker.get_total_tokens() if tracker else 0,
            "total_input_tokens": total_input,
            "total_output_tokens": sum(entry["output_tokens"] for entry in entries),
            "total_cache_read_tokens": total_read,
            "total_cache_write_tokens": total_write,
            "total_prompt_tokens": total_input + total_read + total_write,
            "cache_hit_percentage": cache_hit_percentage(getattr(self.model, "last_usage", None)),
            "total_calls": tracker.get_total_calls() if tracker else 0,
            "total_errors": tracker.get_total_errors() if tracker else 0,
            "entries": entries,
            "approval_metrics": dict(self.permissions.approval_metrics) if self.permissions else {},
        }

    def model_settings(self, provider: str | None = None) -> dict[str, Any]:
        """Return public model settings only; credentials never cross the socket."""
        active = self.runtime or {"model": "mock"}
        runtime = provider_runtime(active, provider)
        return {
            "provider": runtime.get("provider", ""),
            "providers": provider_catalog(active),
            "model": runtime["model"],
            "reasoningEffort": runtime.get("reasoningEffort", "default"),
            "models": model_catalog(runtime),
            "catalogError": runtime.get("catalogError", ""),
            "catalogSource": "api" if runtime.get("_catalogAuthoritative") else "config",
            "isMock": isinstance(self.model, MockModelAdapter),
        }

    def update_model(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._turn_lock.locked() or self._worker_active.is_set():
            raise ValueError("Wait for the current turn to finish before changing model settings")
        if not self.runtime or isinstance(self.model, MockModelAdapter):
            raise ValueError("Model settings are unavailable in mock mode. Configure an API and restart the server.")
        provider = params.get("provider")
        candidate = provider_runtime(self.runtime, provider)
        model = params.get("model", candidate["model"])
        # A model change resets effort unless the caller explicitly picks a compatible level.
        effort = params.get(
            "reasoningEffort",
            "default"
            if model != self.runtime["model"] or candidate is not self.runtime
            else self.runtime.get("reasoningEffort", "default"),
        )
        if self.permissions:
            self.permissions.ensure_local_command_allowed(f"/model {model}")
        validate_model_settings(candidate, model, effort)
        problem = model_selection_error(candidate, model)
        if problem:
            raise ValueError(problem)
        updates = {"model": model, "reasoningEffort": effort}
        if candidate.get("provider"):
            updates["provider"] = candidate["provider"]
        persist = params.get("persist", False)
        if not isinstance(persist, bool):
            raise ValueError("persist must be a boolean")
        if persist:
            save_mini_code_settings(updates)
        apply_model_settings(
            self.runtime, model, effort, messages=self.messages, context_manager=self.context_manager, provider=provider
        )
        if self.session_data:
            self.session_data.model_settings = updates
        return self.model_settings()


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


class PepsiCodeServer:
    def __init__(
        self, port: int = 8765, host: str = "127.0.0.1", *, token: str | None = None, allowed_origins=None
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

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a single raw TCP client (fallback when websockets is absent)."""
        # This path is a minimal SSE-style fallback; primary path uses websockets.
        pass

    async def handle_ws(self, websocket: Any) -> None:
        """Main WebSocket message loop for one client connection."""
        if not await authenticate(websocket, self.token, self.allowed_origins):
            return
        cwd = str(Path.cwd())
        session = ClientSession(cwd)
        loop = asyncio.get_running_loop()

        async def emit(event_name: str, data: dict[str, Any]) -> None:
            msg = protocol.make_event(event_name, data)
            try:
                await websocket.send(json.dumps(msg, ensure_ascii=False, default=str))
            except Exception as error:  # noqa: BLE001
                logger.warning("Failed to send event %s: %s", event_name, error)

        bridge = RemotePermissionBridge(loop, emit, session._cancellation_event)
        session._bridge = bridge
        self._sessions.add(session)

        # Defer initialization until the first session/create request so the
        # client can pick a cwd.  But we need a default cwd to start.
        try:
            from pepsicode.build_info import build_identity
            from pepsicode.version import VERSION

            await emit("connection/ready", {"cwd": cwd, "version": VERSION, "build": build_identity()})
        except Exception:
            pass

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
                    await self._handle_request(message, session, bridge, emit, websocket, loop)
                elif kind == "response":
                    # Client replying to a permission request
                    prompt_id = message.get("id")
                    result = message.get("result", {})
                    if prompt_id and isinstance(result, dict):
                        bridge.resolve(str(prompt_id), result)
        finally:
            await self._close_session(session, bridge)

    async def _handle_request(
        self,
        request: dict[str, Any],
        session: ClientSession,
        bridge: RemotePermissionBridge,
        emit: Any,
        websocket: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        req_id = request.get("id", 0)
        method = request.get("method", "")
        params = request.get("params", {})

        # turn/run is long-running: don't block the WebSocket loop.
        # Run it as a background task and send the response when done.
        if method in {"turn/run", "turn/regenerate"}:
            if session._closing or session._background_tasks or session._turn_lock.locked():
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
            session.input_queue.start(
                lambda update: asyncio.run_coroutine_threadsafe(emit("input/queue", update), loop)
            )
            task = asyncio.create_task(self._run_turn_background(req_id, params, session, emit, websocket, loop))
            session._background_tasks.add(task)
            task.add_done_callback(session._background_tasks.discard)
            return

        try:
            result = await self._dispatch(method, params, session, bridge, emit, loop)
            response = protocol.make_response(req_id, result=result)
        except Exception as error:  # noqa: BLE001
            logger.exception("Error handling %s", method)
            response = protocol.make_response(req_id, error=protocol.make_error(protocol.ERR_INTERNAL, str(error)))
        await websocket.send(json.dumps(response, ensure_ascii=False, default=str))

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
        try:
            result = await self._run_turn(params, session, emit, loop)
            response = protocol.make_response(req_id, result=result)
        except Exception as error:  # noqa: BLE001
            logger.exception("Error in turn/run")
            session.input_queue.close()
            await emit(
                "turn/end",
                {
                    "status": "error",
                    "error": str(error),
                    "task": session.session_data.task_control.get("task") if session.session_data else None,
                },
            )
            response = protocol.make_response(req_id, error=protocol.make_error(protocol.ERR_INTERNAL, str(error)))
        finally:
            session.input_queue.close()
        try:
            await websocket.send(json.dumps(response, ensure_ascii=False, default=str))
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to send turn/run response: %s", error)

    async def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        session: ClientSession,
        bridge: RemotePermissionBridge,
        emit: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> Any:
        if method == "status":
            return {"status": "ok", "cwd": session.cwd}

        if method == "session/tree":
            if session.session_data is None:
                raise ValueError("Create a session first")
            if not session._worker_active.is_set():
                record_messages(session.session_data, session.messages)
            return tree_view(session.session_data)

        if method in {"session/checkout", "session/fork", "session/clone"}:
            if session._turn_lock.locked() or session._worker_active.is_set() or session._background_tasks:
                raise ValueError("Wait for the current turn to finish before changing branches")
            async with session._turn_lock:
                return await self._change_branch(method, params, session, emit)

        if method == "session/create":
            if session._turn_lock.locked() or session._worker_active.is_set():
                raise ValueError("Wait for the current turn to finish before creating a session")
            profile = params.get("profile", "default")
            profile_prompt(profile)  # Validate before changing the current session.
            cwd = normalize_workspace(params.get("cwd", str(Path.cwd())))
            session.cwd = cwd
            session.initialize(force=session.tools is not None)
            session.set_permission_handler(bridge)
            assert session.session_data is not None
            session.session_data.profile = profile
            session._rebuild_system_prompt()
            if profile != "default":
                session.snapshot()
            return {
                "session_id": session.session_data.session_id,
                "cwd": session.cwd,
                **session.model_settings(),
                "tools": [t.name for t in session.tools.list()],
                "permissions": session.permissions.get_summary() if session.permissions else [],
                "access_mode": session.permissions.access_mode.value if session.permissions else "auto",
                "profile": session.session_data.profile,
            }

        if method == "session/list":
            workspace = params.get("workspace") or session.cwd
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

        if method == "session/resume":
            if session._turn_lock.locked() or session._worker_active.is_set():
                raise ValueError("Wait for the current turn to finish before resuming a session")
            session_id = params.get("session_id", "")
            loaded = load_session(session_id)
            if loaded is None:
                raise ValueError(f"Session not found: {session_id}")
            profile_prompt(loaded.profile)
            expected_workspace = params.get("workspace")
            if expected_workspace and not same_workspace(loaded.workspace, expected_workspace):
                raise ValueError("Session does not belong to the current workspace")
            loaded_workspace = normalize_workspace(loaded.workspace or session.cwd)
            session.cwd = loaded_workspace
            # A resumed conversation owns independent permissions, cost, hook,
            # model, and tool state even when it shares the same workspace.
            session.initialize(force=session.tools is not None)
            session.set_permission_handler(bridge)
            session.session_data = loaded
            session.input_queue.bind_session(loaded)
            session.messages = list(loaded.messages)
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
            record_messages(loaded, session.messages)
            return {
                "session_id": loaded.session_id,
                "messages": len(session.messages),
                "message_count": len(session.messages),
                "history": protocol.serialize_session_history([node["message"] for node in branch_path(loaded)]),
                "tree": tree_view(loaded),
                "verification": (session.runtime or {}).get("_lastVerification"),
                "pending_inputs": loaded.task_control.get("inbox", {}).get("pending", []),
                "task": loaded.task_control.get("task"),
                "workspace": session.cwd,
                "profile": loaded.profile,
                **session.model_settings(),
                "permission_mode": session.permissions.mode.value if session.permissions else "default",
                "access_mode": session.permissions.access_mode.value if session.permissions else "auto",
                "plan_file": session.permissions.plan_file_path if session.permissions else None,
            }

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
            assert session.permissions is not None
            session.permissions.set_access_mode(params.get("mode", ""))
            session.session_data.access_mode = session.permissions.access_mode.value
            session.session_data.permission_mode = session.permissions.mode.value
            session.session_data.plan_file_path = session.permissions.plan_file_path
            session._rebuild_system_prompt()
            session.session_data.messages = session.messages
            save_session(session.session_data)
            return {"access_mode": session.permissions.access_mode.value}

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
            )
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
                session.context_manager.messages = list(session.messages)
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
        if params.get("task_kind") is not None and params["task_kind"] not in {"answer", "change", "operation"}:
            raise ValueError("task_kind must be answer, change, or operation")
        if params.get("acceptance") is not None:
            from pepsicode.core.task_acceptance import validate_criteria

            validate_criteria(params["acceptance"])
        answer_to = params.get("answer_to", [])
        if params.get("task_requirements") is not None:
            from pepsicode.core.task_acceptance import validate_requirements

            validate_requirements(params["task_requirements"])
        if not isinstance(answer_to, list) or not all(isinstance(item, str) and item for item in answer_to):
            raise ValueError("answer_to must be an array of question IDs")
        if session.model is None:
            raise RuntimeError("Session not initialized. Call session/create first.")

        async with session._turn_lock:
            if session._closing:
                raise ValueError("Connection is closing")
            if session._cancellation_event.is_set():
                await emit("turn/end", {"status": "cancelled"})
                return {"status": "cancelled", "messages": len(session.messages)}
            if session._worker_active.is_set():
                raise ValueError("The previous turn is still finishing")
            user_input = params.get("input", "").strip()
            regeneration = "regenerate_entry_id" in params or user_input == "/retry"
            regenerated_prompt = None
            if regeneration:
                session.snapshot()
                regenerated_prompt = prepare_regeneration(session.session_data, params.get("regenerate_entry_id"))
                session.messages = list(session.session_data.messages)
                if session.context_manager:
                    session.context_manager.select_branch(session.messages, session.cwd, session.permissions)
                user_input = regenerated_prompt.get("displayContent", regenerated_prompt["content"])
            if not user_input:
                raise ValueError("Missing 'input' parameter")

            if not regeneration and user_input.split()[0] in {"/tree", "/fork", "/clone"}:
                parts = user_input.split()
                if len(parts) > (1 if parts[0] == "/clone" else 2):
                    raise ValueError("Usage: /tree [entry-id], /fork [user-entry-id], /clone")
                if len(parts) == 1 and parts[0] != "/clone":
                    session.snapshot()
                    output = format_tree(session.session_data, users_only=parts[0] == "/fork")
                    await emit("message/start", {})
                    await emit("message/end", {"content": output})
                    await emit("turn/end", {"status": "ok"})
                    return {"output": output}
                method = {"/tree": "session/checkout", "/fork": "session/fork", "/clone": "session/clone"}[parts[0]]
                result = await self._change_branch(
                    method, {"entry_id": parts[1] if len(parts) > 1 else None}, session, emit
                )
                await emit("turn/end", {"status": "ok"})
                return result

            prompt_message = regenerated_prompt or expand_prompt_command(user_input, session.cwd)
            if user_input.startswith("/") and prompt_message is None:
                result = await self._handle_local_command(user_input, session, emit)
                content = result.get("output")
                if result.get("status") == "unknown_command":
                    content = f"Unknown command: {user_input.split()[0]}. Use /help or /commands."
                if result.get("status") == "plan_mode":
                    content = f"Plan mode active. Plan file: {result['plan_file']}"
                if content:
                    await emit("message/start", {})
                    await emit(
                        "message/end", {"content": content, "is_error": result.get("status") == "unknown_command"}
                    )
                await emit("turn/end", {"status": "ok"})
                return result

            session.messages.append(prompt_message or {"role": "user", "content": user_input})
            if answer_to:
                session.messages[-1].setdefault("source", {"kind": "human", "id": uuid.uuid4().hex})["answerTo"] = (
                    answer_to
                )
            if session.session_data:
                record_messages(session.session_data, session.messages)
            if regeneration:
                if session.runtime is not None:
                    session.runtime.pop("_lastVerification", None)
                await emit("session/view", session.conversation_view())
            if not session.input_queue.accepting:
                if session.session_data:
                    session.input_queue.bind_session(session.session_data)
                session.input_queue.start(
                    lambda update: asyncio.run_coroutine_threadsafe(emit("input/queue", update), loop)
                )
            session.permissions.begin_turn()
            session._rebuild_system_prompt()
            if session.context_manager is not None:
                session.context_manager.messages = list(session.messages)
            await emit("usage/update", usage_snapshot(session.context_manager, None))

            # Token streaming: accumulate and emit deltas
            message_started = False
            thinking = ThinkingStream(lambda name, data: asyncio.run_coroutine_threadsafe(emit(name, data), loop))

            def on_token(token: Any) -> None:
                nonlocal message_started
                thinking.consume(token)
                if token.type == "usage":
                    asyncio.run_coroutine_threadsafe(
                        emit("usage/update", usage_snapshot(session.context_manager, token.usage)), loop
                    )
                    return
                if token.type != "text":
                    return
                if not message_started:
                    message_started = True
                    asyncio.run_coroutine_threadsafe(emit("message/start", {}), loop)
                asyncio.run_coroutine_threadsafe(emit("message/delta", {"text": token.content}), loop)

            def on_tool_event(event_name: str, data: dict) -> None:
                thinking.finish()
                if isinstance(data.get("output"), str):
                    data = {**data, "output": data["output"][:10000]}
                asyncio.run_coroutine_threadsafe(emit(event_name, data), loop)

            def on_tool_start(tool_name: str, tool_input: dict) -> None:
                asyncio.run_coroutine_threadsafe(
                    emit("tool/call", {"tool": tool_name, "input": tool_input, "status": "running"}), loop
                )

            def on_tool_result(tool_name: str, output: str, is_error: bool) -> None:
                asyncio.run_coroutine_threadsafe(
                    emit(
                        "tool/result",
                        {"tool": tool_name, "output": output[:5000], "is_error": is_error, "status": "done"},
                    ),
                    loop,
                )

            def on_assistant_message(content: str) -> None:
                finish_assistant_message(content, is_error=False)

            def on_error_message(content: str) -> None:
                finish_assistant_message(content, is_error=True)

            def finish_assistant_message(content: str, *, is_error: bool) -> None:
                nonlocal message_started
                thinking.finish("interrupted" if is_error else "completed")
                if not message_started:
                    message_started = True
                    asyncio.run_coroutine_threadsafe(emit("message/start", {}), loop)
                asyncio.run_coroutine_threadsafe(emit("message/end", {"content": content, "is_error": is_error}), loop)
                message_started = False

            def on_progress_message(content: str) -> None:
                nonlocal message_started
                thinking.finish("cancelled" if session._cancellation_event.is_set() else "completed")
                if message_started:
                    asyncio.run_coroutine_threadsafe(
                        emit("message/end", {"content": content, "role": "progress"}), loop
                    )
                    message_started = False
                else:
                    asyncio.run_coroutine_threadsafe(emit("progress/message", {"content": content}), loop)

            def on_usage(usage: dict) -> None:
                asyncio.run_coroutine_threadsafe(emit("cost/update", session.cost_snapshot()), loop)
                asyncio.run_coroutine_threadsafe(
                    emit("usage/update", usage_snapshot(session.context_manager, usage)), loop
                )

            def run_turn_sync() -> list[ChatMessage]:
                try:
                    with recording_session(session.session_data):
                        return run_verified_turn(
                            runtime=session.runtime,
                            task_kind=params.get("task_kind"),
                            acceptance=params.get("acceptance"),
                            task_requirements=params.get("task_requirements"),
                            runner=run_agent_turn_stream,
                            input_queue=session.input_queue,
                            cancellation_event=session._cancellation_event,
                            on_verification=lambda report: asyncio.run_coroutine_threadsafe(
                                emit("verification/update", report), loop
                            ),
                            model=session.model,
                            tools=session.tools,
                            messages=list(session.messages),
                            cwd=session.cwd,
                            permissions=session.permissions,
                            context_manager=session.context_manager,
                            cost_tracker=session.cost_tracker,
                            on_token=on_token,
                            on_tool_event=on_tool_event,
                            on_tool_start=on_tool_start,
                            on_tool_result=on_tool_result,
                            on_assistant_message=on_assistant_message,
                            on_error_message=on_error_message,
                            on_progress_message=on_progress_message,
                            on_usage=on_usage,
                            hook_engine=session.hook_engine,
                            memory=session.memory_manager,
                        )
                finally:
                    thinking.finish("cancelled" if session._cancellation_event.is_set() else "interrupted")
                    session._worker_active.clear()

            executor = ThreadPoolExecutor(max_workers=1)
            session._worker_active.set()
            try:
                future = loop.run_in_executor(executor, run_turn_sync)
            except Exception:
                session._worker_active.clear()
                executor.shutdown(wait=False)
                raise
            session._current_turn_task = asyncio.wrap_future(future)

            try:
                new_messages = await session._current_turn_task
                session.messages = new_messages
            except asyncio.CancelledError:
                await emit("turn/end", {"status": "cancelled"})
                return {"status": "cancelled", "messages": len(session.messages)}
            finally:
                session.permissions.end_turn()
                executor.shutdown(wait=False)

            # Persist session
            if session.session_data:
                session.session_data.messages = list(session.messages)
                session.session_data.verification_state = snapshot_verification_state(session.tools, session.runtime)
                session.session_data.permission_mode = session.permissions.mode.value
                session.session_data.access_mode = session.permissions.access_mode.value
                session.session_data.plan_file_path = session.permissions.plan_file_path
                if session.runtime and not isinstance(session.model, MockModelAdapter):
                    session.session_data.model_settings = model_settings_snapshot(session.runtime)
                try:
                    save_session(session.session_data)
                    await emit("session/saved", {"session_id": session.session_data.session_id})
                except Exception as error:  # noqa: BLE001
                    logger.warning("Failed to save session: %s", error)

            if session.session_data:
                await emit("session/view", session.conversation_view())

            execution = (
                (session.runtime or {}).get("_lastExecution")
                or (session.session_data.execution_state if session.session_data else None)
                or {}
            )
            turn_status = execution.get("state", "cancelled" if session._cancellation_event.is_set() else "completed")
            if turn_status == "succeeded":
                turn_status = "completed"
            task = session.session_data.task_control.get("task") if session.session_data else None
            if task and task["status"] not in {"complete", "answered", "active"}:
                turn_status = task["status"]
            await emit(
                "turn/end",
                {
                    "status": turn_status,
                    "execution": execution,
                    "task": task,
                    "messages": len(session.messages),
                    "verification": (session.runtime or {}).get("_lastVerification"),
                },
            )

            last_assistant = next((m for m in reversed(session.messages) if m.get("role") == "assistant"), None)
            return {
                "status": turn_status,
                "messages": len(session.messages),
                "last_message": last_assistant.get("content", "") if last_assistant else "",
            }

    async def _change_branch(self, method, params, session, emit):
        session.snapshot()
        if method == "session/checkout":
            draft = checkout_entry(session.session_data, params.get("entry_id"))
        else:
            session.session_data, draft = copy_session(
                session.session_data, entry_id=params.get("entry_id"), clone=method == "session/clone"
            )
        session.messages = list(session.session_data.messages)
        session.input_queue.bind_session(session.session_data)
        restore_verification_state(
            session.tools, session.runtime, session.session_data.verification_state, session.messages
        )
        session._rebuild_system_prompt()
        if session.context_manager:
            session.context_manager.select_branch(session.messages, session.cwd, session.permissions)
        record_messages(session.session_data, session.messages)
        save_session(session.session_data)
        view = session.conversation_view(draft)
        await emit("session/view", view)
        await emit("session/saved", {"session_id": session.session_data.session_id})
        return view

    async def _handle_local_command(self, user_input: str, session: ClientSession, emit: Any) -> dict[str, Any]:
        """Handle slash commands like /plan, /hooks, /tools."""
        if session.permissions:
            session.permissions.ensure_local_command_allowed(user_input)
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
                    session._rebuild_system_prompt()
                    session.session_data.access_mode = session.permissions.access_mode.value
                    session.session_data.permission_mode = session.permissions.mode.value
                    session.session_data.plan_file_path = session.permissions.plan_file_path
                    session.session_data.messages = session.messages
                    save_session(session.session_data)
                    await emit("permissions/settings", {"access_mode": session.permissions.access_mode.value})
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
        try:
            async with websockets.serve(
                handler, self.host, self.port, origins=[None, *self.allowed_origins], max_size=50 * 1024 * 1024
            ) as listener:
                bound_port = listener.sockets[0].getsockname()[1]
                print(f"pepsicode server listening on ws://{self.host}:{bound_port}", flush=True)
                await asyncio.Future()  # run forever
        finally:
            await asyncio.gather(
                *(
                    self._close_session(session, session._bridge)
                    for session in list(self._sessions)
                    if session._bridge is not None
                )
            )


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
