from __future__ import annotations

import json
import logging
import math
import re
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from typing import Any, Protocol

from pepsicode.cancellation import TurnCancelledError
from pepsicode.llm.cost_tracker import BudgetExceededError

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


class ToolStatus(str, Enum):
    SUCCEEDED = "succeeded"
    COMPLETED = "completed"
    FAILED = "failed"
    ERROR = "error"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"
    RUNNING = "running"
    AWAITING_USER = "awaiting_user"


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: str
    backgroundTask: BackgroundTaskResult | None = None
    awaitUser: bool = False
    status: ToolStatus | None = None
    exit_code: int | None = None
    execution_id: str | None = field(default=None, compare=False)

    def __post_init__(self):
        if self.status is not None:
            self.status = ToolStatus(self.status)


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
    environment: dict[str, str] | None = None
    command_is_argv: bool = False
    # Session memory manager, when the caller has one.  save_memory writes
    # through this instance so the next system prompt sees the new entry.
    memory: Any | None = None
    execution: Any | None = None
    external_grant: tuple[str, str] | None = None
    effect_state: str = "none"
    control: dict = field(default_factory=dict)
    for_verification: bool = False


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
    # Unknown integrations conservatively trigger verification. Tools that
    # only update UI/session state can explicitly opt out.
    affects_workspace: bool | Callable[[Any], bool] | None = None
    always_loaded: bool = False
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    observation_trust: str = "unreviewed"

    def __post_init__(self):
        original = self.run

        @wraps(original)
        def scoped(arguments, context):
            from pepsicode.execution.runtime import ExecutionScope, bind_scope, bind_tool_context, current_scope

            active = context.execution or current_scope()
            if active is None:
                active = ExecutionScope(context.cwd, context.permissions, context.cancellation_event)
            if context.permissions is None:
                context.permissions = active.permissions
            operation = (
                context.permissions.operation()
                if context.permissions is not None and hasattr(context.permissions, "operation")
                else nullcontext()
            )
            with bind_scope(active), bind_tool_context(context), operation:
                try:
                    active.check_cancelled()
                except TurnCancelledError:
                    return ToolResult(False, "Task stopped by user.", status=ToolStatus.CANCELLED)
                return original(arguments, context)

        if hasattr(original, "__self__"):
            scoped.__self__ = original.__self__
        self.run = scoped

    @property
    def is_read_only(self) -> bool:
        return ToolCapability.READ_ONLY in self.capabilities

    def modifies_workspace(self, arguments: Any) -> bool:
        """Evaluate workspace impact with the same parsed input as execution."""
        impact = self.affects_workspace(arguments) if callable(self.affects_workspace) else self.affects_workspace
        return not self.is_read_only if impact is None else bool(impact)


class ToolRegistry:
    def __init__(
        self,
        tools: list[ToolDefinition],
        skills: list[dict[str, Any]] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        disposer: Callable[[], Any] | None = None,
        deferred: bool = False,
        core_tools: set[str] | None = None,
        search_aliases: dict[str, str | list[str]] | None = None,
    ) -> None:
        inherited_search = any(
            tool.name == "search_tools" and isinstance(getattr(tool.run, "__self__", None), ToolRegistry)
            for tool in tools
        )
        # Filtered/copied registries must never retain a search callable bound
        # to the parent registry, which could advertise removed capabilities.
        if inherited_search:
            tools = [
                tool
                for tool in tools
                if not (tool.name == "search_tools" and isinstance(getattr(tool.run, "__self__", None), ToolRegistry))
            ]
        if deferred and any(tool.name == "search_tools" for tool in tools):
            raise ValueError("search_tools is reserved when deferred tool discovery is enabled")
        self._tools = list(tools)
        self._skills = skills or []
        self._mcp_servers = mcp_servers or []
        self._disposer = disposer
        self._lock = threading.RLock()
        self.mutation_count = 0
        self.verified_mutation_count = 0
        self.deferred = deferred
        self._search_aliases = {
            "数据库": "database sql db",
            "浏览器": "browser playwright",
            "测试": "test pytest",
            "代码": "code symbols references",
            "记忆": "memory",
            "容器": "docker",
            "版本": "git",
            "审查": "review",
            "网页": "web browser",
            "搜索": "search grep",
        }
        if search_aliases is not None:
            if not isinstance(search_aliases, dict) or not all(
                isinstance(key, str)
                and key
                and (isinstance(value, str) or isinstance(value, list) and all(isinstance(term, str) for term in value))
                for key, value in search_aliases.items()
            ):
                raise ValueError("toolSearchAliases must map phrases to strings or string arrays")
            self._search_aliases.update(search_aliases)
        if core_tools is not None and (
            not isinstance(core_tools, (set, list, tuple)) or not all(isinstance(name, str) for name in core_tools)
        ):
            raise ValueError("toolCoreTools must be an array of tool names")
        core = (
            core_tools
            if core_tools is not None
            else {
                "read_file",
                "list_files",
                "grep_files",
                "edit_file",
                "write_file",
                "run_command",
                "ask_user",
                "exit_plan_mode",
                "task",
                "load_skill",
                "code_intelligence",
            }
        )
        self._loaded = [tool.name for tool in tools if not deferred or tool.name in core or tool.always_loaded]
        if deferred or inherited_search:
            self._tools.append(
                ToolDefinition(
                    name="search_tools",
                    description="Find and load tools by capability, metadata, service or exact name. Use query '*' to browse the complete catalog, and offset to page through matches. Matching tools become callable on the next request.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                            "offset": {"type": "integer", "minimum": 0},
                        },
                        "required": ["query"],
                    },
                    validator=self._validate_search,
                    run=self._search,
                    capabilities={ToolCapability.READ_ONLY},
                )
            )
            self._loaded.append("search_tools")

    @staticmethod
    def _validate_search(value: Any) -> dict:
        if not isinstance(value, dict) or not isinstance(value.get("query"), str) or not value["query"].strip():
            raise ValueError("query must be a non-empty string")
        limit = value.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer from 1 to 10")
        offset = value.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        return {"query": value["query"].strip(), "limit": limit, "offset": offset}

    def _search(self, value: dict, _context: ToolContext) -> ToolResult:
        query = value["query"].lower()
        for term, expansion in self._search_aliases.items():
            if term.casefold() in value["query"].casefold():
                query += " " + (expansion if isinstance(expansion, str) else " ".join(expansion)).lower()
        terms = set(re.findall(r"[\w]+", query.replace("_", " ")))
        documents = [
            (
                tool,
                Counter(
                    re.findall(
                        r"[\w]+",
                        (
                            tool.name.replace("_", " ")
                            + " "
                            + tool.description
                            + " "
                            + " ".join(tool.tags + tool.aliases)
                            + " "
                            + " ".join(tool.input_schema.get("properties", {}))
                        )
                        .replace("_", " ")
                        .lower(),
                    )
                ),
            )
            for tool in self._tools
            if tool.name != "search_tools"
        ]
        ranked = []
        for tool, frequencies in documents:
            score = 1.0 if value["query"] == "*" else 0.0
            for term in terms:
                frequency = frequencies[term]
                if frequency:
                    count = sum(term in doc for _, doc in documents)
                    score += (
                        math.log(1 + (len(documents) - count + 0.5) / (count + 0.5)) * frequency / (frequency + 1.2)
                    )
                    if term in tool.name.lower():
                        score += 2
            if value["query"].lower() in [name.lower() for name in [tool.name, *tool.aliases]]:
                score += 100
            if score:
                ranked.append((score, tool))
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        start = value["offset"]
        selected = [tool for _, tool in ranked[start : start + value["limit"]]]
        with self._lock:
            for tool in selected:
                if tool.name not in self._loaded:
                    self._loaded.append(tool.name)
        return ToolResult(
            ok=True,
            output=(
                "\n".join(f"{tool.name}: {tool.description}" for tool in selected)
                + f"\nShowing {start + 1}-{start + len(selected)} of {len(ranked)} matches; next offset: {start + len(selected)}"
                if selected
                else "No matching tools at this offset. Try query '*' to browse, another keyword, or offset 0."
            ),
        )

    def model_tools(self, messages: list[dict] | None = None) -> list[ToolDefinition]:
        """Expose loaded schemas, retaining previously used tools after resume."""
        with self._lock:
            for message in messages or []:
                names = [message.get("toolName")]
                content = message.get("content")
                if isinstance(content, list):
                    names.extend(
                        block.get("name")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "tool_use"
                    )
                for name in names:
                    if name and name not in self._loaded and self.find(name) is not None:
                        self._loaded.append(name)
            by_name = {tool.name: tool for tool in self._tools}
            return [by_name[name] for name in self._loaded if name in by_name]

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
        from pepsicode.execution.runtime import bind_scope, current_scope

        scope = context.execution or current_scope()
        context.execution = scope
        if scope and context.permissions is None:
            context.permissions = scope.permissions
        context.tool_use_id = context.tool_use_id or uuid.uuid4().hex
        context.effect_state = "none"
        with bind_scope(scope):
            result = self._execute(tool_name, input_data, context)
            context.external_grant = None
            if (
                context.effect_state == "unknown" or (not result.ok and context.effect_state == "committed")
            ) and result.status not in {"cancelled", "timed_out"}:
                result.status = ToolStatus.UNKNOWN
                result.ok = False
            result.execution_id = context.tool_use_id
            if scope:
                from pepsicode.core.task_control import snapshot

                task = snapshot(context).get("task") or {}
                fingerprint = json.dumps(
                    [task.get("id"), task.get("revision"), tool_name, input_data, result.output],
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                )
                with self._lock:
                    count = scope.repetitions.get("count", 0) + 1 if scope.repetitions.get("key") == fingerprint else 1
                    scope.repetitions.update(key=fingerprint, count=count)
                if count in scope.repeat_thresholds:
                    result.output += f"\n[Harness: {count} identical calls produced the same result. Inspect the result and choose a different approach; do not bypass permission denial or cancellation.]"
                if result.status == "unknown" or context.effect_state == "unknown":
                    scope.mark_unknown(context.tool_use_id, tool=tool_name, arguments=input_data)
                try:
                    scope.record(
                        "tool/settled",
                        call_id=context.tool_use_id,
                        tool=tool_name,
                        state=result.status or ("succeeded" if result.ok else "failed"),
                        effect_state=context.effect_state,
                        output=result.output,
                    )
                except OSError as error:
                    scope.mark_unknown(context.tool_use_id, tool=tool_name, arguments=input_data)
                    return ToolResult(
                        False,
                        f"Execution outcome could not be committed: {type(error).__name__}; inspect state before retrying",
                        status="unknown",
                        execution_id=context.tool_use_id,
                    )
            return result

    def _execute(self, tool_name: str, input_data: Any, context: ToolContext) -> ToolResult:
        if context.cancellation_event is not None and context.cancellation_event.is_set():
            return ToolResult(ok=False, output="Task stopped by user; tool was not executed.", status="cancelled")
        tool = self.find(tool_name)
        if tool is None:
            return ToolResult(ok=False, output=f"Unknown tool: {tool_name}")

        dispatched = False
        try:
            parsed = tool.validator(input_data)
            from pepsicode.core.task_control import artifact_requirements, edit_control, snapshot

            task = snapshot(context).get("task")
            if task and not context.for_verification and tool.modifies_workspace(parsed):
                if task.get("classification", {}).get("read_only_request"):
                    return ToolResult(
                        False, "User requested explanation only; mutation was not executed", status="blocked"
                    )
                if (
                    task["kind"] == "answer"
                    and not (context.permissions and context.permissions.is_plan_mode)
                    and not tool_name.startswith("mcp__")
                    and tool_name not in {"save_memory", "enter_plan_mode", "exit_plan_mode"}
                ):
                    with edit_control(context) as control:
                        current = control["task"]
                        current.update(kind="change", status="active", evidence=[])
                        current["classification"] = {
                            **current.get("classification", {}),
                            "kind": "change",
                            "source": "host_tool_effect",
                            "locked": True,
                        }
                        current["delivered_intent"] = dict(current["classification"])
                        current["revision"] += 1
                        current["admitted_revision"] = current["revision"]
                        current["required_artifacts"] = artifact_requirements(current["objective"])
                        current.setdefault("classification_history", []).append(
                            {"source": "host_tool_effect", "tool": tool_name, "revision": current["revision"]}
                        )
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
                    return ToolResult(
                        ok=False, output=f"Hook '{decision.hook_id}' denied {tool_name}: {reason}", status="blocked"
                    )

            permissions = context.permissions
            if permissions is not None and hasattr(permissions, "ensure_tool_allowed"):
                decision = permissions.ensure_tool_allowed(tool, parsed)
                if tool_name.startswith("mcp__") and not tool.is_read_only and getattr(decision, "is_allowed", False):
                    context.external_grant = (tool_name, json.dumps(parsed, sort_keys=True, ensure_ascii=False))
            if context.cancellation_event is not None and context.cancellation_event.is_set():
                return ToolResult(ok=False, output="Task stopped by user; tool was not executed.", status="cancelled")
            affects_workspace = tool.modifies_workspace(parsed)
            if context.execution and context.execution.has_unknown and affects_workspace:
                return ToolResult(
                    False, "Inspect the previous unknown operation before further changes", status="blocked"
                )
            if context.execution:
                context.execution.check_cancelled()
                context.execution.record(
                    "tool/started",
                    call_id=context.tool_use_id,
                    tool=tool_name,
                    arguments=parsed,
                    mutating=affects_workspace,
                )
            # Count before execution: an integration can write and then raise.
            if affects_workspace:
                with self._lock:
                    self.mutation_count += 1
            dispatched = True
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
        except BudgetExceededError:
            raise
        except TurnCancelledError as error:
            return ToolResult(False, str(error), status="cancelled")
        except Exception as error:  # noqa: BLE001
            from pepsicode.execution.filesystem import StaleFileError

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
            return ToolResult(
                ok=False,
                output=f"{type(tool).__name__} error: {error}"
                + (
                    "; earlier file changes may already have committed; inspect state before retrying"
                    if context.effect_state == "committed"
                    else ""
                ),
                status="unknown"
                if context.effect_state == "committed"
                else "blocked"
                if isinstance(error, (PermissionError, StaleFileError))
                else "unknown"
                if dispatched and not tool.is_read_only
                else "error",
            )

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
