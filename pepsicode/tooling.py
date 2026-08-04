from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool metadata (inspired by Claude Code's Tool type)
# ---------------------------------------------------------------------------


class ToolCapability(str, Enum):
    """Tool capability flags."""

    READ_ONLY = "read_only"
    DESTRUCTIVE = "destructive"
    CONCURRENCY_SAFE = "concurrency_safe"
    REQUIRES_PERMISSION = "requires_permission"


@dataclass
class ToolMetadata:
    """Tool metadata for classification and discovery.

    Inspired by Claude Code's Tool type definition.
    """

    name: str
    description: str
    capabilities: set[ToolCapability] = field(default_factory=set)
    input_schema: dict[str, Any] = field(default_factory=dict)
    is_enabled: bool = True
    max_result_size_chars: int = 10_000
    tags: list[str] = field(default_factory=list)

    @property
    def is_read_only(self) -> bool:
        """Check if tool is read-only."""
        return ToolCapability.READ_ONLY in self.capabilities

    @property
    def is_destructive(self) -> bool:
        """Check if tool can modify/delete data."""
        return ToolCapability.DESTRUCTIVE in self.capabilities

    @property
    def is_concurrency_safe(self) -> bool:
        """Check if tool is safe for concurrent execution."""
        return ToolCapability.CONCURRENCY_SAFE in self.capabilities


# ---------------------------------------------------------------------------
# Tool Protocol (inspired by Claude Code's Tool interface)
# ---------------------------------------------------------------------------


class Tool(Protocol):
    """Tool protocol defining a complete tool lifecycle.

    Inspired by Claude Code's Tool type which includes:
    - call: Execution logic
    - description: Dynamic description generation
    - validate_input: Input validation
    - check_permissions: Permission checking
    - Metadata: is_read_only, is_destructive, etc.
    """

    @property
    def name(self) -> str: ...

    @property
    def description_template(self) -> str: ...

    def get_description(self, args: dict[str, Any], options: dict[str, Any] | None = None) -> str: ...
    def validate_input(self, args: dict[str, Any]) -> tuple[bool, str]: ...
    def check_permissions(self, args: dict[str, Any], context: ToolContext) -> tuple[bool, str]: ...
    def call(
        self,
        args: dict[str, Any],
        context: ToolContext,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> ToolResult: ...
    def is_enabled(self) -> bool: ...
    def is_read_only(self, args: dict[str, Any]) -> bool: ...
    def is_destructive(self, args: dict[str, Any]) -> bool: ...


@dataclass(slots=True)
class BackgroundTaskResult:
    taskId: str
    type: str
    command: str
    pid: int
    status: str
    startedAt: int


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: str
    backgroundTask: BackgroundTaskResult | None = None
    awaitUser: bool = False


@dataclass(slots=True)
class ToolContext:
    cwd: str
    permissions: Any | None = None
    hooks: Any | None = None
    tool_use_id: str = ""
    call_index: int = 0
    agent_scope: str = "main"
    suppress_hooks: bool = False
    cancellation_event: Any | None = None


Validator = Callable[[Any], Any]
Runner = Callable[[Any, ToolContext], ToolResult]


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    validator: Validator
    run: Runner
    capabilities: set[ToolCapability] = field(default_factory=set)
    max_result_size_chars: int = 10_000
    # Read-only tools with no side effects can run concurrently with each
    # other.  Tools that write files, run commands, or otherwise mutate state
    # must run exclusively (default).  See agent_loop._execute_calls.
    concurrency_safe: bool = False

    @property
    def is_read_only(self) -> bool:
        return ToolCapability.READ_ONLY in self.capabilities


class ToolRegistry:
    def __init__(
        self,
        tools: list[ToolDefinition],
        skills: list[dict[str, Any]] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        disposer: Callable[[], Any] | None = None,
    ) -> None:
        self._tools = tools
        self._skills = skills or []
        self._mcp_servers = mcp_servers or []
        self._disposer = disposer

    def list(self) -> list[ToolDefinition]:
        return list(self._tools)

    def get_skills(self) -> list[dict[str, Any]]:
        return list(self._skills)

    def get_mcp_servers(self) -> list[dict[str, Any]]:
        return list(self._mcp_servers)

    def find(self, name: str) -> ToolDefinition | None:
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None

    def execute(self, tool_name: str, input_data: Any, context: ToolContext) -> ToolResult:
        tool = self.find(tool_name)
        if tool is None:
            return ToolResult(ok=False, output=f"Unknown tool: {tool_name}")

        try:
            parsed = tool.validator(input_data)
            if context.hooks is not None and not context.suppress_hooks:
                from pepsicode.hooks import HookContext, HookEvent

                decision = context.hooks.evaluate_pre_tool(
                    HookContext(
                        event=HookEvent.PRE_TOOL_USE,
                        cwd=context.cwd,
                        data={"tool_name": tool_name, "tool_input": parsed},
                        metadata=self._hook_metadata(context),
                    )
                )
                if not decision.allowed:
                    reason = decision.reason or "blocked by a pre-tool hook"
                    return ToolResult(ok=False, output=f"Hook '{decision.hook_id}' denied {tool_name}: {reason}")

            permissions = context.permissions
            if permissions is not None and hasattr(permissions, "ensure_tool_allowed"):
                permissions.ensure_tool_allowed(tool, parsed)
            result = tool.run(parsed, context)
            if context.hooks is not None and not context.suppress_hooks:
                from pepsicode.hooks import HookContext, HookEvent

                try:
                    context.hooks.emit(
                        HookEvent.POST_TOOL_USE,
                        HookContext(
                            event=HookEvent.POST_TOOL_USE,
                            cwd=context.cwd,
                            data={
                                "tool_name": tool_name,
                                "tool_input": parsed,
                                "tool_output": result.output,
                                "result": {"ok": result.ok, "output": result.output},
                                "is_error": not result.ok,
                            },
                            metadata=self._hook_metadata(context),
                        ),
                    )
                except Exception as hook_error:  # noqa: BLE001 - preserve a completed tool result
                    logger.warning("Post-tool hook failed for %s: %s", tool_name, hook_error)
            return result
        except (KeyboardInterrupt, SystemExit):
            # 这些异常应该向上传播，不应该被捕获
            raise
        except Exception as error:  # noqa: BLE001
            if context.hooks is not None and not context.suppress_hooks:
                from pepsicode.hooks import HookContext, HookEvent

                try:
                    context.hooks.emit(
                        HookEvent.ERROR,
                        HookContext(
                            event=HookEvent.ERROR,
                            cwd=context.cwd,
                            data={
                                "error": str(error),
                                "error_type": type(error).__name__,
                                "tool_name": tool_name,
                                "tool_input": input_data,
                            },
                            metadata=self._hook_metadata(context),
                        ),
                    )
                except Exception as hook_error:  # noqa: BLE001 - preserve the original tool failure
                    logger.warning("Error hook failed for %s: %s", tool_name, hook_error)
            return ToolResult(ok=False, output=f"{type(tool).__name__} error: {error}")

    @staticmethod
    def _hook_metadata(context: ToolContext) -> dict[str, Any]:
        mode = getattr(context.permissions, "mode", "default")
        return {
            "permission_mode": getattr(mode, "value", str(mode)),
            "agent_scope": context.agent_scope,
            "tool_use_id": context.tool_use_id,
            "call_index": context.call_index,
        }

    def dispose(self) -> None:
        if self._disposer is not None:
            self._disposer()
