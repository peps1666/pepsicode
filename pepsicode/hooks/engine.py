from __future__ import annotations

import asyncio
import inspect
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

from pepsicode.context_artifacts import ContextArtifactStore, prepare_tool_result
from pepsicode.hooks.models import (
    HookActionResult,
    HookActionType,
    HookContext,
    HookDecision,
    HookDefinition,
    HookDiagnostic,
    HookEvent,
    HookHandler,
    HookNotification,
    HookRegistration,
)

MAX_CONTEXT_MESSAGE_CHARS = 2_000
MAX_CONTEXT_BATCH_CHARS = 6_000
MAX_NOTIFICATION_CHARS = 4_000
MAX_BACKGROUND_WORKERS = 4
CommandRunner = Callable[[tuple[str, ...], int, HookContext], HookActionResult]


class HookEngine:
    def __init__(
        self,
        hooks: list[HookDefinition] | None = None,
        *,
        diagnostics: list[HookDiagnostic] | None = None,
        command_runner: CommandRunner | None = None,
        enabled: bool = True,
    ) -> None:
        self.hooks = sorted(hooks or [], key=lambda item: (item.priority, item.id))
        self.diagnostics = list(diagnostics or [])
        self.command_runner = command_runner
        self._enabled = enabled
        self._lock = threading.RLock()
        self._registrations: dict[HookEvent, list[HookRegistration]] = {event: [] for event in HookEvent}
        self._notifications: list[HookNotification] = []
        self._context_messages: list[tuple[str, str]] = []
        self._executor = ThreadPoolExecutor(max_workers=MAX_BACKGROUND_WORKERS, thread_name_prefix="pepsi-hook")
        self._futures: set[Future[Any]] = set()
        self._cancel_events: dict[Future[Any], threading.Event] = {}
        self._reload_callback: Callable[[], tuple[list[HookDefinition], list[HookDiagnostic]]] | None = None
        self._trust_callback: Callable[[], str] | None = None

    def set_management_callbacks(
        self,
        *,
        reload_callback: Callable[[], tuple[list[HookDefinition], list[HookDiagnostic]]] | None = None,
        trust_callback: Callable[[], str] | None = None,
    ) -> None:
        self._reload_callback = reload_callback
        self._trust_callback = trust_callback

    def replace_hooks(self, hooks: list[HookDefinition], diagnostics: list[HookDiagnostic]) -> None:
        with self._lock:
            self.hooks = sorted(hooks, key=lambda item: (item.priority, item.id))
            self.diagnostics = list(diagnostics)

    def register(self, event: HookEvent, handler: HookHandler, description: str = "") -> Callable[[], None]:
        registration = HookRegistration(
            event=event,
            handler=handler,
            is_async=asyncio.iscoroutinefunction(handler),
            description=description,
        )
        with self._lock:
            self._registrations[event].append(registration)

        def unregister() -> None:
            with self._lock:
                if registration in self._registrations[event]:
                    self._registrations[event].remove(registration)

        return unregister

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _coerce_event(event: HookEvent | str) -> HookEvent:
        return event if isinstance(event, HookEvent) else HookEvent(str(event))

    def _matches(self, hook: HookDefinition, context: HookContext) -> bool:
        if not self._enabled or not hook.enabled or not hook.trusted or hook.event != context.event:
            return False
        scope = "subagent" if context.agent_scope.startswith("subagent") else "main"
        if scope not in hook.scopes:
            return False
        if hook.once and hook.executed:
            return False
        return hook.condition is None or hook.condition.evaluate(context)

    def find_matching_hooks(self, event: HookEvent | str, context: HookContext) -> list[HookDefinition]:
        target = self._coerce_event(event)
        if context.event != target:
            context = HookContext(
                event=target,
                cwd=context.cwd,
                data=dict(context.data),
                metadata=dict(context.metadata),
            )
        with self._lock:
            return [hook for hook in self.hooks if self._matches(hook, context)]

    def _claim(self, hook: HookDefinition, context: HookContext) -> bool:
        with self._lock:
            if not self._matches(hook, context):
                return False
            if hook.once:
                hook.executed = True
            return True

    def _queue_context(self, hook_id: str, message: str) -> None:
        bounded = message.strip()[:MAX_CONTEXT_MESSAGE_CHARS]
        if not bounded:
            return
        with self._lock:
            self._context_messages.append((hook_id, bounded))

    def _queue_notification(self, hook: HookDefinition, context: HookContext, result: HookActionResult) -> None:
        notification = HookNotification(
            hook_id=hook.id,
            event=context.event.value,
            output=result.output[:MAX_NOTIFICATION_CHARS],
            success=result.success,
            source=hook.source.value,
            call_index=context.call_index,
        )
        with self._lock:
            self._notifications.append(notification)

    def _run_action(self, hook: HookDefinition, context: HookContext) -> HookActionResult:
        action = hook.action
        if action.type == HookActionType.DENY:
            return HookActionResult(output=context.expand(action.message), success=True)
        if action.type == HookActionType.NOTIFY:
            return HookActionResult(output=context.expand(action.message), success=True)
        if action.type == HookActionType.CONTEXT:
            output = context.expand(action.message)
            self._queue_context(hook.id, output)
            return HookActionResult(output=output, success=True)
        if action.type == HookActionType.COMMAND:
            if context.permission_mode == "plan":
                return HookActionResult(output="Command hooks are disabled in Plan mode", success=False)
            if self.command_runner is None:
                return HookActionResult(output="No permission-aware command runner is configured", success=False)
            argv = tuple(context.expand(item) for item in action.argv)
            result = self.command_runner(argv, action.timeout_seconds, context)
            bounded = prepare_tool_result(
                tool_name=f"hook:{hook.id}",
                output=result.output,
                budget_chars=MAX_NOTIFICATION_CHARS,
                artifact_store=ContextArtifactStore.for_workspace(context.cwd) if context.cwd else None,
            )
            if action.expose_output_to_model:
                self._queue_context(hook.id, bounded)
            return HookActionResult(output=bounded, success=result.success)
        return HookActionResult(output=f"Unsupported hook action: {action.type}", success=False)

    def _record(self, hook: HookDefinition, context: HookContext, started: float, result: HookActionResult) -> None:
        elapsed = int((time.monotonic() - started) * 1_000)
        with self._lock:
            hook.call_count += 1
            hook.last_called = time.time()
            hook.total_duration_ms += elapsed
            hook.last_success = result.success
            hook.last_output = result.output[:500]
        if not result.success and hook.on_error == "ignore":
            return
        if (
            hook.action.type in {HookActionType.DENY, HookActionType.NOTIFY, HookActionType.COMMAND}
            or not result.success
        ):
            self._queue_notification(hook, context, result)

    def _execute_claimed(self, hook: HookDefinition, context: HookContext) -> HookActionResult:
        started = time.monotonic()
        try:
            result = self._run_action(hook, context)
        except Exception as error:  # noqa: BLE001 - hook failures are isolated from the agent
            result = HookActionResult(output=f"Hook execution error: {error}", success=False)
        self._record(hook, context, started, result)
        return result

    def _submit_background(self, hook: HookDefinition, context: HookContext) -> None:
        cancel_event = threading.Event()
        background_context = HookContext(
            event=context.event,
            cwd=context.cwd,
            data=dict(context.data),
            metadata={**context.metadata, "_hook_cancel_event": cancel_event},
            timestamp=context.timestamp,
        )
        future = self._executor.submit(self._execute_claimed, hook, background_context)
        with self._lock:
            self._futures.add(future)
            self._cancel_events[future] = cancel_event

        def _done(completed: Future[Any]) -> None:
            with self._lock:
                self._futures.discard(completed)
                self._cancel_events.pop(completed, None)

        future.add_done_callback(_done)

    def _run_configured(self, context: HookContext) -> list[HookActionResult]:
        results: list[HookActionResult] = []
        for hook in self.find_matching_hooks(context.event, context):
            if hook.action.type == HookActionType.DENY:
                continue
            if not self._claim(hook, context):
                continue
            if hook.action.background:
                self._submit_background(hook, context)
            else:
                results.append(self._execute_claimed(hook, context))
        return results

    def _run_programmatic_sync(self, context: HookContext) -> list[Any]:
        if not self._enabled:
            return []
        with self._lock:
            registrations = list(self._registrations[context.event])
        results: list[Any] = []
        for registration in registrations:
            if not registration.enabled or registration.is_async:
                continue
            started = time.monotonic()
            try:
                result = registration.handler(context)
            except Exception as error:  # noqa: BLE001
                result = f"Hook error: {error}"
            registration.call_count += 1
            registration.last_called = time.time()
            registration.total_duration_ms += int((time.monotonic() - started) * 1_000)
            results.append(result)
        return results

    def emit(self, event: HookEvent | str, context: HookContext | None = None, **kwargs: Any) -> list[Any]:
        target = self._coerce_event(event)
        if context is None:
            context = HookContext(event=target, data=kwargs)
        elif context.event != target:
            context = HookContext(
                event=target,
                cwd=context.cwd,
                data=dict(context.data),
                metadata=dict(context.metadata),
                timestamp=context.timestamp,
            )
        configured = self._run_configured(context)
        return [*configured, *self._run_programmatic_sync(context)]

    def fire_sync(self, event: HookEvent, **kwargs: Any) -> list[Any]:
        return self.emit(event, HookContext(event=event, data=kwargs))

    async def fire(self, event: HookEvent, **kwargs: Any) -> list[Any]:
        context = HookContext(event=event, data=kwargs)
        results: list[Any] = list(self._run_configured(context))
        with self._lock:
            registrations = list(self._registrations[event])
        for registration in registrations:
            if not registration.enabled:
                continue
            started = time.monotonic()
            try:
                value = registration.handler(context)
                result = await value if inspect.isawaitable(value) else value
            except Exception as error:  # noqa: BLE001
                result = f"Hook error: {error}"
            registration.call_count += 1
            registration.last_called = time.time()
            registration.total_duration_ms += int((time.monotonic() - started) * 1_000)
            results.append(result)
        return results

    def evaluate_pre_tool(self, context: HookContext) -> HookDecision:
        if context.event != HookEvent.PRE_TOOL_USE:
            raise ValueError("evaluate_pre_tool requires a pre_tool_use context")
        for hook in self.find_matching_hooks(HookEvent.PRE_TOOL_USE, context):
            if not self._claim(hook, context):
                continue
            result = self._execute_claimed(hook, context)
            if hook.action.type == HookActionType.DENY:
                return HookDecision(allowed=False, hook_id=hook.id, reason=result.output)
            if not result.success and hook.on_error == "deny":
                return HookDecision(allowed=False, hook_id=hook.id, reason=result.output)
        for result in self._run_programmatic_sync(context):
            if isinstance(result, HookDecision) and not result.allowed:
                return result
        return HookDecision()

    def drain_context_messages(self) -> list[str]:
        with self._lock:
            pending = list(self._context_messages)
            self._context_messages.clear()
        messages: list[str] = []
        used = 0
        for hook_id, content in pending:
            prefix = f'<hook-context hook_id="{hook_id}">\n'
            suffix = "\n</hook-context>"
            remaining = MAX_CONTEXT_BATCH_CHARS - used
            available_content = remaining - len(prefix) - len(suffix)
            if available_content <= 0:
                break
            wrapped = prefix + content[:available_content] + suffix
            messages.append(wrapped)
            used += len(wrapped)
        return messages

    def get_prompt_messages(self) -> list[str]:
        return self.drain_context_messages()

    def drain_notifications(self) -> list[HookNotification]:
        with self._lock:
            notifications = sorted(
                self._notifications, key=lambda item: (item.call_index, item.created_at, item.hook_id)
            )
            self._notifications.clear()
        return notifications

    def get_hook_stats(self, event: HookEvent | None = None) -> dict[str, Any]:
        configured = [hook for hook in self.hooks if event is None or hook.event == event]
        registrations = [
            registration
            for target, items in self._registrations.items()
            if event is None or target == event
            for registration in items
        ]
        return {
            "total_hooks": len(configured) + len(registrations),
            "enabled_hooks": sum(1 for hook in configured if hook.enabled)
            + sum(1 for registration in registrations if registration.enabled),
            "total_calls": sum(hook.call_count for hook in configured)
            + sum(registration.call_count for registration in registrations),
            "total_duration_ms": sum(hook.total_duration_ms for hook in configured)
            + sum(registration.total_duration_ms for registration in registrations),
            "background_tasks": len(self._futures),
        }

    def format_hook_status(self) -> str:
        lines = ["Hooks v2 Status", "=" * 60, f"engine: {'enabled' if self._enabled else 'disabled'}"]
        if not self.hooks and not any(self._registrations.values()):
            lines.append("No hooks configured.")
        for hook in self.hooks:
            status = "on" if hook.enabled else ("untrusted" if not hook.trusted else "off")
            last = "-" if hook.last_success is None else ("ok" if hook.last_success else "error")
            lines.append(
                f"{status:9} {hook.id:24} {hook.event.value:18} {hook.action.type.value:8} "
                f"calls={hook.call_count} last={last} source={hook.source.value}"
            )
        stats = self.get_hook_stats()
        lines.extend(
            [
                "-" * 60,
                f"hooks={stats['total_hooks']} enabled={stats['enabled_hooks']} calls={stats['total_calls']} "
                f"duration={stats['total_duration_ms']}ms background={stats['background_tasks']}",
                f"diagnostics={len(self.diagnostics)}",
            ]
        )
        return "\n".join(lines)

    def format_diagnostics(self) -> str:
        if not self.diagnostics:
            return "No hook configuration diagnostics."
        return "\n".join(f"[{item.severity}] {item.path}: {item.message}" for item in self.diagnostics)

    def handle_command(self, command: str) -> str:
        parts = command.strip().split()
        subcommand = parts[1].lower() if len(parts) > 1 else "list"
        if subcommand in {"list", "status"}:
            return self.format_hook_status()
        if subcommand == "errors":
            return self.format_diagnostics()
        if subcommand == "reload":
            if self._reload_callback is None:
                return "Hook reload is not available."
            hooks, diagnostics = self._reload_callback()
            self.replace_hooks(hooks, diagnostics)
            return f"Reloaded {len(hooks)} hook(s); {len(diagnostics)} diagnostic(s)."
        if subcommand == "trust":
            if self._trust_callback is None:
                return "Project hook trust management is not available."
            return self._trust_callback()
        if subcommand in {"enable", "disable"}:
            if len(parts) < 3:
                return f"Usage: /hooks {subcommand} <id>"
            hook_id = parts[2]
            with self._lock:
                hook = next((item for item in self.hooks if item.id == hook_id), None)
                if hook is None:
                    return f"Unknown hook: {hook_id}"
                if subcommand == "enable" and not hook.trusted:
                    return f"Hook '{hook_id}' is untrusted; run /hooks trust first."
                hook.enabled = subcommand == "enable"
            return f"Hook '{hook_id}' {subcommand}d for this process."
        return "Usage: /hooks [list|errors|reload|trust|enable <id>|disable <id>]"

    def close(self, timeout_seconds: float = 2.0) -> None:
        with self._lock:
            futures = list(self._futures)
        if futures:
            wait(futures, timeout=max(0.0, timeout_seconds))
            remaining = [future for future in futures if not future.done()]
            with self._lock:
                cancel_events = [self._cancel_events[future] for future in remaining if future in self._cancel_events]
            for cancel_event in cancel_events:
                cancel_event.set()
            if remaining:
                wait(remaining, timeout=1.0)
            for future in remaining:
                if not future.done():
                    future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)


class HookManager(HookEngine):
    """Backward-compatible name for the v2 engine."""


def hooks_enabled_from_environment() -> bool:
    return os.environ.get("PEPSI_CODE_HOOKS", "1").strip().lower() not in {"0", "false", "off", "no"}
