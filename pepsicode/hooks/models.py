from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class HookEvent(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    AGENT_START = "agent_start"
    AGENT_STOP = "agent_stop"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"
    SESSION_SAVE = "session_save"
    SESSION_RESUME = "session_resume"
    USER_INPUT = "user_input"
    ASSISTANT_OUTPUT = "assistant_output"
    CONTEXT_COMPACT = "context_compact"
    ERROR = "error"
    STARTUP = "startup"
    SHUTDOWN = "shutdown"


class HookActionType(str, Enum):
    DENY = "deny"
    NOTIFY = "notify"
    CONTEXT = "context"
    COMMAND = "command"


class HookSource(str, Enum):
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"
    PROGRAMMATIC = "programmatic"


@dataclass(slots=True)
class HookContext:
    event: HookEvent
    cwd: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    @property
    def event_name(self) -> str:
        return self.event.value

    @property
    def tool_name(self) -> str | None:
        value = self.data.get("tool_name")
        return str(value) if value else None

    @property
    def tool_input(self) -> Any:
        return self.data.get("tool_input")

    @property
    def tool_output(self) -> str | None:
        value = self.data.get("tool_output")
        return str(value) if value is not None else None

    @property
    def is_error(self) -> bool:
        return bool(self.data.get("is_error", False))

    @property
    def session_id(self) -> str | None:
        value = self.data.get("session_id")
        return str(value) if value else None

    @property
    def user_input(self) -> str | None:
        value = self.data.get("user_input")
        return str(value) if value is not None else None

    @property
    def assistant_output(self) -> str | None:
        value = self.data.get("assistant_output")
        return str(value) if value is not None else None

    @property
    def file_path(self) -> str:
        value = self.data.get("file_path") or self._lookup("tool_input.path") or self._lookup("tool_input.file_path")
        return str(value) if value else ""

    @property
    def permission_mode(self) -> str:
        return str(self.metadata.get("permission_mode", "default"))

    @property
    def agent_scope(self) -> str:
        return str(self.metadata.get("agent_scope", "main"))

    @property
    def call_index(self) -> int:
        try:
            return int(self.metadata.get("call_index", 0))
        except (TypeError, ValueError):
            return 0

    def _lookup(self, dotted: str) -> Any:
        value: Any = {"data": self.data, "metadata": self.metadata}
        if dotted.startswith("args."):
            dotted = "data.tool_input." + dotted[5:]
        elif dotted == "args":
            dotted = "data.tool_input"
        elif dotted.startswith("result."):
            dotted = "data.result." + dotted[7:]
        elif dotted in {"tool", "tool_name"}:
            return self.tool_name or ""
        elif dotted in {"path", "file_path"}:
            return self.file_path
        elif dotted == "event":
            return self.event.value
        elif dotted == "permission_mode":
            return self.permission_mode
        elif dotted == "agent_scope":
            return self.agent_scope
        elif dotted in self.data:
            return self.data[dotted]
        elif dotted in self.metadata:
            return self.metadata[dotted]
        elif not dotted.startswith(("data.", "metadata.")):
            dotted = "data." + dotted

        for part in dotted.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value

    def get_field(self, name: str) -> Any:
        return self._lookup(name)

    def expand(self, template: str) -> str:
        replacements = {
            "event": self.event.value,
            "tool": self.tool_name or "",
            "tool_name": self.tool_name or "",
            "path": self.file_path,
            "cwd": self.cwd,
            "permission_mode": self.permission_mode,
            "agent_scope": self.agent_scope,
            "error": str(self.data.get("error", "")),
            "session_id": self.session_id or "",
            "user_input": self.user_input or "",
            "assistant_output": self.assistant_output or "",
            "status": str(self.data.get("status", "")),
        }
        output = template
        for key, value in replacements.items():
            output = output.replace("{" + key + "}", str(value))
        tool_input = self.tool_input if isinstance(self.tool_input, dict) else {}
        for key, value in tool_input.items():
            output = output.replace("{args." + str(key) + "}", str(value))
        return output


@dataclass(frozen=True, slots=True)
class HookAction:
    type: HookActionType
    message: str = ""
    argv: tuple[str, ...] = ()
    timeout_seconds: int = 30
    background: bool = False
    expose_output_to_model: bool = False


@dataclass(slots=True)
class HookDefinition:
    id: str
    event: HookEvent
    action: HookAction
    condition: Any = None
    priority: int = 100
    scopes: tuple[str, ...] = ("main", "subagent")
    once: bool = False
    enabled: bool = True
    on_error: str = "warn"
    source: HookSource = HookSource.USER
    source_path: str = ""
    trusted: bool = True
    executed: bool = False
    call_count: int = 0
    total_duration_ms: int = 0
    last_called: float | None = None
    last_success: bool | None = None
    last_output: str = ""


@dataclass(frozen=True, slots=True)
class HookDecision:
    allowed: bool = True
    hook_id: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class HookActionResult:
    output: str = ""
    success: bool = True


@dataclass(frozen=True, slots=True)
class HookNotification:
    hook_id: str
    event: str
    output: str
    success: bool
    source: str = ""
    call_index: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class HookDiagnostic:
    path: str
    message: str
    severity: str = "error"


HookHandler = Callable[[HookContext], Any]
AsyncHookHandler = Callable[[HookContext], Any]


@dataclass(slots=True)
class HookRegistration:
    event: HookEvent
    handler: HookHandler
    is_async: bool = False
    enabled: bool = True
    description: str = ""
    created_at: float = field(default_factory=time.time)
    call_count: int = 0
    last_called: float | None = None
    total_duration_ms: int = 0


@dataclass(slots=True)
class HookLoadResult:
    hooks: list[HookDefinition] = field(default_factory=list)
    diagnostics: list[HookDiagnostic] = field(default_factory=list)
    untrusted_paths: list[Path] = field(default_factory=list)
