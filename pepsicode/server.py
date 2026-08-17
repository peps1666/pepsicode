"""WebSocket server exposing pepsicode's agent loop to external clients.

Runs the synchronous :func:`run_agent_turn_stream` in a background thread and
bridges its callbacks to asynchronous WebSocket events.  Permission prompts are
forwarded to the client as ``permission/request`` events; the client must reply
with a ``tool/approve`` request whose ``id`` matches the prompt id.

Usage::

    python -m pepsicode.server --port 8765

The server listens on ``ws://127.0.0.1:<port>`` and speaks the JSON-RPC
envelope defined in :mod:`pepsicode.protocol`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pepsicode import protocol
from pepsicode.agent_loop import run_agent_turn_stream
from pepsicode.anthropic_adapter import AnthropicModelAdapter
from pepsicode.config import load_runtime_config
from pepsicode.context_manager import ContextManager
from pepsicode.cost_tracker import CostTracker
from pepsicode.hooks import HookContext, HookEvent, create_hook_engine
from pepsicode.logging_config import get_logger, setup_logging
from pepsicode.mock_model import MockModelAdapter
from pepsicode.permissions import PermissionManager, PermissionMode
from pepsicode.prompt import build_system_prompt
from pepsicode.session import SessionData, create_new_session, save_session
from pepsicode.tools import create_default_tool_registry
from pepsicode.types import ChatMessage
from pepsicode.agents.trace import TraceManager

logger = get_logger("server")

# ---------------------------------------------------------------------------
# Remote permission handler
# ---------------------------------------------------------------------------


class RemotePermissionBridge:
    """Bridges synchronous ``PermissionManager.prompt`` calls to async WS.

    The agent loop runs in a worker thread and calls ``prompt(request_dict)``
    synchronously.  We stash an ``asyncio.Future`` keyed by a prompt id, emit a
    ``permission/request`` event to the client, and block the worker thread on
    ``future.result()`` until the client replies with ``tool/approve``.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, emit: Any) -> None:
        self._loop = loop
        self._emit = emit  # async callable: emit(event_name, data)
        self._pending: dict[str, Future[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        """Synchronous entry point invoked from the worker thread."""
        prompt_id = uuid.uuid4().hex[:12]
        future: Future[dict[str, Any]] = Future()
        with self._lock:
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
        if future is None:
            return False
        future.set_result(response)
        return True


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
        self._turn_lock = asyncio.Lock()
        self._current_turn_task: asyncio.Task | None = None

    def initialize(self) -> None:
        """Lazy-initialize heavy objects on first use."""
        if self.tools is not None:
            return
        try:
            self.runtime = load_runtime_config(self.cwd)
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to load runtime config: %s", error)
            self.runtime = None

        self.cost_tracker = CostTracker()
        self.trace_manager = TraceManager()
        self.tools = create_default_tool_registry(
            self.cwd, runtime=self.runtime, cost_tracker=self.cost_tracker, trace_manager=self.trace_manager
        )
        self.permissions = PermissionManager(self.cwd, prompt=None)  # set later
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
            summarize = getattr(self.model, "summarize", None)
            if summarize is not None:
                self.context_manager.summarizer = summarize

        self.session_data = create_new_session(self.cwd)
        self._rebuild_system_prompt()

    def _rebuild_system_prompt(self) -> None:
        if not self.permissions or not self.tools:
            return
        prompt = build_system_prompt(
            self.cwd,
            self.permissions.get_summary(),
            {
                "skills": self.tools.get_skills(),
                "mcpServers": self.tools.get_mcp_servers(),
                "planMode": self.permissions.is_plan_mode,
                "planFilePath": self.permissions.plan_file_path,
            },
        )
        # Replace existing system prompt or prepend
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": prompt}
        else:
            self.messages.insert(0, {"role": "system", "content": prompt})

    def set_permission_handler(self, handler: RemotePermissionBridge) -> None:
        assert self.permissions is not None
        self.permissions.prompt = handler


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


class PepsiCodeServer:
    def __init__(self, port: int = 8765, host: str = "127.0.0.1") -> None:
        self.port = port
        self.host = host
        self._clients: set[asyncio.Task] = set()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a single raw TCP client (fallback when websockets is absent)."""
        # This path is a minimal SSE-style fallback; primary path uses websockets.
        pass

    async def handle_ws(self, websocket: Any) -> None:
        """Main WebSocket message loop for one client connection."""
        cwd = str(Path.cwd())
        session = ClientSession(cwd)
        loop = asyncio.get_running_loop()

        async def emit(event_name: str, data: dict[str, Any]) -> None:
            msg = protocol.make_event(event_name, data)
            try:
                await websocket.send(json.dumps(msg, ensure_ascii=False, default=str))
            except Exception as error:  # noqa: BLE001
                logger.warning("Failed to send event %s: %s", event_name, error)

        bridge = RemotePermissionBridge(loop, emit)

        # Defer initialization until the first session/create request so the
        # client can pick a cwd.  But we need a default cwd to start.
        try:
            await emit("connection/ready", {"cwd": cwd, "version": "2.0.0"})
        except Exception:
            pass

        async for raw in websocket:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send(
                    json.dumps(protocol.make_response(0, error=protocol.make_error(protocol.ERR_PARSE_ERROR, "Invalid JSON")))
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
        if method == "turn/run":
            asyncio.create_task(
                self._run_turn_background(req_id, params, session, emit, websocket, loop)
            )
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
            await emit("turn/end", {"status": "error", "error": str(error)})
            response = protocol.make_response(
                req_id, error=protocol.make_error(protocol.ERR_INTERNAL, str(error))
            )
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

        if method == "session/create":
            cwd = params.get("cwd", str(Path.cwd()))
            session.cwd = cwd
            session.initialize()
            session.set_permission_handler(bridge)
            assert session.session_data is not None
            return {
                "session_id": session.session_data.session_id,
                "cwd": session.cwd,
                "model": session.runtime.get("model") if session.runtime else "mock",
                "tools": [t.name for t in session.tools.list()],
                "permissions": session.permissions.get_summary() if session.permissions else [],
            }

        if method == "session/list":
            from pepsicode.session import list_sessions

            sessions = list_sessions()
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
            from pepsicode.session import load_session

            session_id = params.get("session_id", "")
            loaded = load_session(session_id)
            if loaded is None:
                raise ValueError(f"Session not found: {session_id}")
            session.cwd = loaded.workspace or session.cwd
            session.initialize()
            session.set_permission_handler(bridge)
            session.messages = list(loaded.messages)
            if session.permissions and loaded.permission_mode == PermissionMode.PLAN.value:
                session.permissions.restore_plan_state(loaded.permission_mode, loaded.plan_file_path)
            session._rebuild_system_prompt()
            return {
                "session_id": loaded.session_id,
                "messages": len(session.messages),
                "workspace": session.cwd,
                "permission_mode": session.permissions.mode.value if session.permissions else "default",
            }

        if method == "session/delete":
            from pepsicode.session import delete_session

            session_id = params.get("session_id", "")
            deleted = delete_session(session_id)
            return {"deleted": deleted}

        if method == "turn/run":
            return await self._run_turn(params, session, emit, loop)

        if method == "turn/cancel":
            if session._current_turn_task and not session._current_turn_task.done():
                session._current_turn_task.cancel()
                return {"cancelled": True}
            return {"cancelled": False}

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
            from pepsicode.hooks import HookContext, HookEvent

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
            if session.cost_tracker is None:
                return {"total_cost_usd": 0, "entries": []}
            return {
                "total_cost_usd": session.cost_tracker.total_cost_usd,
                "total_input_tokens": session.cost_tracker.total_input_tokens,
                "total_output_tokens": session.cost_tracker.total_output_tokens,
                "entries": [
                    {
                        "model": e.model,
                        "input_tokens": e.input_tokens,
                        "output_tokens": e.output_tokens,
                        "cost_usd": e.cost_usd,
                    }
                    for e in session.cost_tracker.entries
                ],
            }

        if method == "context/query":
            if session.context_manager is None:
                return {"total_tokens": 0, "usage_percentage": 0, "messages_count": len(session.messages)}
            session.context_manager.messages = list(session.messages)
            stats = session.context_manager.get_stats()
            return {
                "total_tokens": stats.total_tokens,
                "usage_percentage": stats.usage_percentage,
                "messages_count": stats.messages_count,
                "max_tokens": stats.max_tokens,
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
        if session.model is None:
            raise RuntimeError("Session not initialized. Call session/create first.")

        async with session._turn_lock:
            user_input = params.get("input", "").strip()
            if not user_input:
                raise ValueError("Missing 'input' parameter")

            # Handle slash commands
            if user_input.startswith("/"):
                return await self._handle_local_command(user_input, session, emit)

            session.messages.append({"role": "user", "content": user_input})
            session.permissions.begin_turn()
            session._rebuild_system_prompt()

            # Token streaming: accumulate and emit deltas
            token_buffer: list[str] = []
            message_started = False

            def on_token(token: Any) -> None:
                nonlocal message_started, token_buffer
                if not message_started:
                    message_started = True
                    asyncio.run_coroutine_threadsafe(emit("message/start", {}), loop)
                if token.type == "text":
                    token_buffer.append(token.content)
                    asyncio.run_coroutine_threadsafe(emit("message/delta", {"text": token.content}), loop)

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
                asyncio.run_coroutine_threadsafe(emit("message/end", {"content": content}), loop)

            def on_progress_message(content: str) -> None:
                asyncio.run_coroutine_threadsafe(emit("progress/message", {"content": content}), loop)

            def on_usage(usage: dict) -> None:
                asyncio.run_coroutine_threadsafe(emit("cost/update", usage), loop)

            def run_turn_sync() -> list[ChatMessage]:
                return run_agent_turn_stream(
                    model=session.model,
                    tools=session.tools,
                    messages=list(session.messages),
                    cwd=session.cwd,
                    permissions=session.permissions,
                    context_manager=session.context_manager,
                    cost_tracker=session.cost_tracker,
                    on_token=on_token,
                    on_tool_start=on_tool_start,
                    on_tool_result=on_tool_result,
                    on_assistant_message=on_assistant_message,
                    on_progress_message=on_progress_message,
                    on_usage=on_usage,
                    hook_engine=session.hook_engine,
                )

            executor = ThreadPoolExecutor(max_workers=1)
            future = loop.run_in_executor(executor, run_turn_sync)
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
                session.session_data.permission_mode = session.permissions.mode.value
                session.session_data.plan_file_path = session.permissions.plan_file_path
                try:
                    save_session(session.session_data)
                    await emit("session/saved", {"session_id": session.session_data.session_id})
                except Exception as error:  # noqa: BLE001
                    logger.warning("Failed to save session: %s", error)

            await emit("turn/end", {"status": "completed", "messages": len(session.messages)})

            last_assistant = next(
                (m for m in reversed(session.messages) if m.get("role") == "assistant"), None
            )
            return {
                "status": "completed",
                "messages": len(session.messages),
                "last_message": last_assistant.get("content", "") if last_assistant else "",
            }

    async def _handle_local_command(
        self, user_input: str, session: ClientSession, emit: Any
    ) -> dict[str, Any]:
        """Handle slash commands like /plan, /hooks, /tools."""
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
            from pepsicode.cli_commands import try_handle_local_command

            result = try_handle_local_command(user_input, tools=session.tools, permissions=session.permissions)
            if result is not None:
                return {"output": result}
        except Exception as error:  # noqa: BLE001
            logger.warning("Local command failed: %s", error)

        return {"status": "unknown_command", "input": user_input}

    async def start(self) -> None:
        """Start the WebSocket server."""
        try:
            import websockets
        except ImportError:
            logger.error(
                "The 'websockets' package is required. Install it with: pip install websockets"
            )
            sys.exit(1)

        async def handler(websocket: Any) -> None:
            try:
                await self.handle_ws(websocket)
            except Exception as error:  # noqa: BLE001
                if "connection" not in str(error).lower():
                    logger.exception("Client handler error")

        logger.info("Starting pepsicode server on ws://%s:%d", self.host, self.port)
        async with websockets.serve(handler, self.host, self.port, max_size=50 * 1024 * 1024):
            print(f"pepsicode server listening on ws://{self.host}:{self.port}", flush=True)
            await asyncio.Future()  # run forever


def main() -> None:
    parser = argparse.ArgumentParser(description="pepsicode WebSocket server")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

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
