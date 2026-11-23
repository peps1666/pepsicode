"""Client session state and approval bridges for the WebSocket server."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Any, cast

from pepsicode.agents.status import session_trace_view
from pepsicode.agents.trace import TraceManager
from pepsicode.approval import ApprovalBackend, ApprovalDecision, ApprovalOutcome, ApprovalRequest
from pepsicode.cancellation import TurnCancelledError
from pepsicode.config import load_runtime_config, save_mini_code_settings
from pepsicode.context.context_manager import ContextManager
from pepsicode.context.prompt import PROMPT_FORMAT_VERSION, build_session_prompt
from pepsicode.core.input_queue import TurnInputQueue
from pepsicode.core.session import SessionData, create_new_session, save_session
from pepsicode.core.session_tree import branch_path, record_messages, tree_view
from pepsicode.core.verification import snapshot_verification_state
from pepsicode.hooks import HookContext, HookEvent, create_hook_engine
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.llm.model_discovery import refresh_model_catalog
from pepsicode.llm.model_settings import (
    apply_model_settings,
    model_catalog,
    model_selection_error,
    update_context_window,
    validate_model_settings,
)
from pepsicode.llm.providers import model_settings_snapshot, provider_catalog, provider_runtime
from pepsicode.logging_config import get_logger
from pepsicode.permissions import PermissionManager
from pepsicode.server import protocol
from pepsicode.server.profiles import profile_prompt
from pepsicode.service.policy import evaluate_request, policy_effective
from pepsicode.tools import create_default_tool_registry
from pepsicode.types import ChatMessage
from pepsicode.usage import cache_hit_percentage

logger = get_logger("server")


def app_attr(name: str, default: Any) -> Any:
    """Resolve patchable server symbols via ``pepsicode.server.app`` for test compatibility."""
    app_mod = sys.modules.get("pepsicode.server.app")
    if app_mod is not None and hasattr(app_mod, name):
        return getattr(app_mod, name)
    return default


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

    def __init__(self, loop: asyncio.AbstractEventLoop, emit: Any, cancellation_event: Any = None) -> None:
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
        from pepsicode.execution.runtime import current_scope

        scope = current_scope()
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
        if request.get("metadata"):
            payload["metadata"] = request["metadata"]
        asyncio.run_coroutine_threadsafe(self._emit("permission/request", payload), self._loop)

        deadline = time.monotonic() + 300
        try:
            while True:
                if scope:
                    scope.check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("Permission prompt %s timed out after 300s", prompt_id)
                    break
                try:
                    return future.result(timeout=min(0.05, remaining) if scope else remaining)
                except TimeoutError:
                    continue
        except (TurnCancelledError, PermissionError):
            asyncio.run_coroutine_threadsafe(self._emit("permission/withdrawn", {"prompt_id": prompt_id}), self._loop)
            raise
        finally:
            with self._lock:
                self._pending.pop(prompt_id, None)
        asyncio.run_coroutine_threadsafe(self._emit("permission/withdrawn", {"prompt_id": prompt_id}), self._loop)
        return {"decision": "deny_once"}

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


class ServiceApprovalBackend(ApprovalBackend):
    """Policy-first approval backend for service mode (M3 §19.2).

    Requests inside the *effective* unattended policy are allowed
    automatically and recorded for audit — the policy is reloaded from disk on
    every request so revocation and expiry take effect at the next
    side-effect entrance.  Everything else asks the attached control client;
    with no client attached it is denied and filed as a merged outbox
    notification — never retried silently, never surfaced as a plain model
    error.
    """

    def __init__(self, host: Any, bridge: RemotePermissionBridge | None, session_id: str | None) -> None:
        self._host = host
        self._bridge = bridge
        self._session_id = session_id

    def for_client(self, bridge: RemotePermissionBridge | None, session_id: str | None) -> ServiceApprovalBackend:
        return ServiceApprovalBackend(self._host, bridge, session_id)

    def request(self, req: ApprovalRequest) -> ApprovalOutcome:
        payload = req.to_payload()
        policy = self._host.load_policy()
        effective, _reason = policy_effective(policy)
        if effective and policy is not None and evaluate_request(policy, payload) == "allow":
            self._host.record_event(
                self._session_id or "*",
                "service/notice",
                {
                    "kind": "policy_allow",
                    "approval_kind": payload.get("kind", ""),
                    "summary": str(payload.get("summary", ""))[:200],
                },
            )
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE, feedback="allowed by unattended policy")
        if self._bridge is not None:
            return self._bridge.request(req)
        self._host.notify(
            dedup_key=f"approval:{self._session_id or 'service'}:{payload.get('kind', 'unknown')}",
            title=f"Service session needs approval: {payload.get('kind', 'unknown')}",
            body=str(payload.get("summary", ""))[:300],
        )
        return ApprovalOutcome(
            decision=ApprovalDecision.DENY_ONCE,
            feedback="No control client is attached and the action is outside the unattended policy",
        )


class ConnectionSession:
    """Per-connection view of the attached :class:`ClientSession`.

    Interactive mode attaches exactly one session created for the connection;
    service mode attaches a resident session owned by the service host, so
    the same object must be re-bound on every request.
    """

    def __init__(self, session: ClientSession | None = None, *, orchestration: bool = True) -> None:
        self.session = session
        self.orchestration = orchestration


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
        self._current_turn_task: asyncio.Future[Any] | None = None
        self._worker_active = threading.Event()
        self._cancellation_event = threading.Event()
        self._provider_setup: dict[str, Any] | None = None
        self._closing = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[Any] | None = None
        self._bridge: RemotePermissionBridge | None = None
        self.input_queue = TurnInputQueue()

    def require_session_data(self) -> SessionData:
        if self.session_data is None:
            raise ValueError("Create or resume a session first")
        return self.session_data

    def require_permissions(self) -> PermissionManager:
        if self.permissions is None:
            raise RuntimeError("Session not initialized. Call session/create first.")
        return self.permissions

    def require_tools(self) -> Any:
        if self.tools is None:
            raise RuntimeError("Session not initialized. Call session/create first.")
        return self.tools

    def require_trace_manager(self) -> TraceManager:
        if self.trace_manager is None:
            raise RuntimeError("Session not initialized. Call session/create first.")
        return self.trace_manager

    def require_cost_tracker(self) -> CostTracker:
        if self.cost_tracker is None:
            raise RuntimeError("Session not initialized. Call session/create first.")
        return self.cost_tracker

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
            self.runtime = app_attr("load_runtime_config", load_runtime_config)(self.cwd)
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to load runtime config: %s", error)
            self.runtime = None

        self.cost_tracker = CostTracker()
        if self.runtime is not None and os.environ.get("PEPSI_CODE_MODEL_MODE") != "mock":
            refresh_model_catalog(self.runtime)
        self.trace_manager = TraceManager()
        tool_factory = app_attr("create_default_tool_registry", create_default_tool_registry)
        self.tools = tool_factory(
            self.cwd, runtime=self.runtime, cost_tracker=self.cost_tracker, trace_manager=self.trace_manager
        )
        from pepsicode.workspace_permissions import get_workspace_default

        self.permissions = PermissionManager(
            self.cwd,
            prompt=None,
            mode=get_workspace_default(self.cwd),
            process_sandbox_mode=(self.runtime or {}).get("processSandboxMode", "danger-full-access"),
        )  # set later
        self.hook_engine = app_attr("create_hook_engine", create_hook_engine)(self.cwd, self.permissions)
        self.hook_engine.emit(
            HookEvent.STARTUP,
            HookContext(
                event=HookEvent.STARTUP,
                cwd=self.cwd,
                metadata={"permission_mode": self.permissions.mode.value, "agent_scope": "main"},
            ),
        )

        mock_cls = app_attr("MockModelAdapter", MockModelAdapter)
        adapter_cls = app_attr("AnthropicModelAdapter", AnthropicModelAdapter)
        if self.runtime is None or os.environ.get("PEPSI_CODE_MODEL_MODE") == "mock":
            self.model = mock_cls()
        else:
            self.model = adapter_cls(self.runtime, self.tools)

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
        profile = self.session_data.profile if self.session_data else "default"
        if (
            self.messages
            and self.messages[0].get("role") == "system"
            and self.messages[0].get("_prompt_format") == PROMPT_FORMAT_VERSION
            and self.messages[0].get("_prompt_profile") == profile
        ):
            return
        prompt = app_attr("build_session_prompt", build_session_prompt)(
            self.cwd,
            self.permissions.get_summary(),
            skills=self.tools.get_skills(),
            mcp_servers=self.tools.get_mcp_servers(),
            memory_manager=self.memory_manager,
            plan_mode=self.permissions.is_plan_mode,
            plan_file_path=self.permissions.plan_file_path,
            dynamic_sections=[],
        )
        presentation = profile_prompt(profile)
        if presentation:
            prompt += "\n\n" + presentation
        # Replace existing system prompt or prepend
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {
                "role": "system",
                "content": prompt,
                "_prompt_format": PROMPT_FORMAT_VERSION,
                "_prompt_profile": profile,
            }
        else:
            self.messages.insert(
                0,
                {
                    "role": "system",
                    "content": prompt,
                    "_prompt_format": PROMPT_FORMAT_VERSION,
                    "_prompt_profile": profile,
                },
            )

    def snapshot(self) -> None:
        data = self.require_session_data()
        record_messages(data, cast(list[dict[str, Any]], self.messages))
        data.verification_state = snapshot_verification_state(self.tools, self.runtime)
        if self.permissions:
            data.permission_mode = self.permissions.mode.value
            data.access_mode = self.permissions.access_mode.value
            data.plan_file_path = self.permissions.plan_file_path
        if self.runtime and not isinstance(self.model, app_attr("MockModelAdapter", MockModelAdapter)):
            data.model_settings = model_settings_snapshot(self.runtime)
        save_session(data)

    def conversation_view(self, draft: str = "") -> dict[str, Any]:
        data = self.require_session_data()
        view = tree_view(data)
        return {
            "session_id": data.session_id,
            "workspace": self.cwd,
            "history": protocol.serialize_session_history([node["message"] for node in branch_path(data)]),
            "tree": view,
            "draft": draft,
            "verification": (self.runtime or {}).get("_lastVerification"),
            "execution": data.execution_state,
            "task": data.task_control.get("task"),
            "agents": session_trace_view(data),
            "pending_inputs": data.task_control.get("inbox", {}).get("pending", []),
        }

    def set_permission_handler(self, handler: RemotePermissionBridge) -> None:
        permissions = self.require_permissions()
        # RemotePermissionBridge now implements ApprovalBackend directly.
        permissions.approval = handler
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
            "isMock": isinstance(self.model, app_attr("MockModelAdapter", MockModelAdapter)),
        }

    def update_model(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._turn_lock.locked() or self._worker_active.is_set():
            raise ValueError("Wait for the current turn to finish before changing model settings")
        if not self.runtime or isinstance(self.model, app_attr("MockModelAdapter", MockModelAdapter)):
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
            app_attr("save_mini_code_settings", save_mini_code_settings)(updates)
        apply_model_settings(
            self.runtime,
            model,
            effort,
            messages=cast(list[dict[str, Any]], self.messages),
            context_manager=self.context_manager,
            provider=provider,
        )
        if self.session_data:
            self.session_data.model_settings = updates
        return self.model_settings()
