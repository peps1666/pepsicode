from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pepsicode.hooks.conditions import ConditionGroup, FieldCondition, HookConditionError, parse_conditions
from pepsicode.hooks.engine import HookEngine, HookManager
from pepsicode.hooks.loader import HookConfigError, hook_config_paths, load_hooks
from pepsicode.hooks.models import (
    AsyncHookHandler,
    HookAction,
    HookActionResult,
    HookActionType,
    HookContext,
    HookDecision,
    HookDefinition,
    HookDiagnostic,
    HookEvent,
    HookNotification,
    HookSource,
)
from pepsicode.hooks.runtime import create_hook_engine
from pepsicode.hooks.trust import HookTrustStore

_hook_manager = HookManager()


def get_hook_manager() -> HookManager:
    return _hook_manager


def register_hook(event: HookEvent, handler: Callable[[HookContext], Any], description: str = "") -> Callable[[], None]:
    return _hook_manager.register(event, handler, description)


async def fire_hook(event: HookEvent, **kwargs: Any) -> list[Any]:
    return await _hook_manager.fire(event, **kwargs)


def fire_hook_sync(event: HookEvent, **kwargs: Any) -> list[Any]:
    return _hook_manager.fire_sync(event, **kwargs)


def create_logging_hook(log_file: Path | None = None):
    def handler(context: HookContext) -> None:
        timestamp = time.strftime("%H:%M:%S", time.localtime(context.timestamp))
        message = f"[{timestamp}] {context.event.value}"
        if context.tool_name:
            message += f" tool={context.tool_name}"
        if context.session_id:
            message += f" session={context.session_id[:8]}"
        if log_file is None:
            return
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")

    return handler


def create_script_hook(script_path: Path) -> AsyncHookHandler:
    """Create the legacy programmatic script hook.

    Configuration-driven command hooks use the permission-aware v2 runner;
    this helper remains for applications that explicitly register trusted code.
    """

    async def handler(context: HookContext) -> str:
        try:
            script = str(script_path)
            suffix = script_path.suffix.lower()
            if sys.platform == "win32" and suffix in {".py", ".sh", ".bat", ".cmd", ".ps1"}:
                if suffix == ".py":
                    prefix = [sys.executable, script]
                elif suffix in {".bat", ".cmd"}:
                    prefix = ["cmd", "/c", script]
                elif suffix == ".ps1":
                    prefix = ["powershell", "-ExecutionPolicy", "Bypass", "-File", script]
                else:
                    prefix = ["bash", script]
            else:
                prefix = [script]
            process = await asyncio.create_subprocess_exec(
                *prefix,
                context.event.value,
                *(str(value) for value in context.data.values()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode == 0:
                return stdout.decode("utf-8", errors="replace")
            return f"Script failed: {stderr.decode('utf-8', errors='replace')}"
        except Exception as error:  # noqa: BLE001 - compatibility hook isolates failures
            return f"Script execution failed: {error}"

    return handler


__all__ = [
    "ConditionGroup",
    "AsyncHookHandler",
    "FieldCondition",
    "HookAction",
    "HookActionResult",
    "HookActionType",
    "HookConditionError",
    "HookConfigError",
    "HookContext",
    "HookDecision",
    "HookDefinition",
    "HookDiagnostic",
    "HookEngine",
    "HookEvent",
    "HookManager",
    "HookNotification",
    "HookSource",
    "HookTrustStore",
    "create_hook_engine",
    "create_logging_hook",
    "create_script_hook",
    "fire_hook",
    "fire_hook_sync",
    "get_hook_manager",
    "hook_config_paths",
    "load_hooks",
    "parse_conditions",
    "register_hook",
]
