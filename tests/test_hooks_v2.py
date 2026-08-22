from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pepsicode.core.agent_loop import run_agent_turn
from pepsicode.hooks import (
    HookAction,
    HookActionResult,
    HookActionType,
    HookContext,
    HookDefinition,
    HookEngine,
    HookEvent,
    HookTrustStore,
    load_hooks,
    parse_conditions,
)
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.types import AgentStep, ChatMessage
from pepsicode.version import VERSION


def _hook(
    hook_id: str,
    event: HookEvent,
    action_type: HookActionType,
    message: str = "",
    **kwargs,
) -> HookDefinition:
    return HookDefinition(
        id=hook_id,
        event=event,
        action=HookAction(type=action_type, message=message),
        **kwargs,
    )


def test_structured_conditions_support_nested_fields() -> None:
    condition = parse_conditions(
        {
            "tool": "write_file",
            "args.path": {"glob": "src/*.py"},
            "permission_mode": {"in": ["default", "accept_edits"]},
        }
    )
    context = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        data={"tool_name": "write_file", "tool_input": {"path": "src/app.py"}},
        metadata={"permission_mode": "default"},
    )

    assert condition.evaluate(context)


def test_emit_uses_explicit_event_when_context_event_differs() -> None:
    seen: list[HookEvent] = []
    engine = HookEngine()
    engine.register(HookEvent.STARTUP, lambda context: seen.append(context.event))
    engine.register(HookEvent.SHUTDOWN, lambda context: seen.append(context.event))

    engine.emit(HookEvent.STARTUP, HookContext(event=HookEvent.SHUTDOWN))

    engine.close()
    assert seen == [HookEvent.STARTUP]


def test_pre_tool_deny_stops_execution() -> None:
    calls: list[dict] = []
    engine = HookEngine([_hook("protect-env", HookEvent.PRE_TOOL_USE, HookActionType.DENY, "protected")])
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="write_file",
                description="write",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=lambda value, _context: calls.append(value) or ToolResult(ok=True, output="written"),
            )
        ]
    )

    result = registry.execute(
        "write_file",
        {"path": ".env"},
        ToolContext(cwd=".", hooks=engine),
    )

    engine.close()
    assert not result.ok
    assert "protect-env" in result.output
    assert calls == []


def test_post_and_error_hook_failures_do_not_replace_tool_results() -> None:
    class BrokenHooks:
        @staticmethod
        def evaluate_pre_tool(_context):
            from pepsicode.hooks import HookDecision

            return HookDecision()

        @staticmethod
        def emit(_event, _context):
            raise RuntimeError("broken hook")

    successful = ToolRegistry(
        [
            ToolDefinition(
                name="success",
                description="success",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=lambda _value, _context: ToolResult(ok=True, output="original result"),
            )
        ]
    )
    failing = ToolRegistry(
        [
            ToolDefinition(
                name="failure",
                description="failure",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=lambda _value, _context: (_ for _ in ()).throw(ValueError("original failure")),
            )
        ]
    )
    context = ToolContext(cwd=".", hooks=BrokenHooks())

    success_result = successful.execute("success", {}, context)
    failure_result = failing.execute("failure", {}, context)

    assert success_result == ToolResult(ok=True, output="original result")
    assert not failure_result.ok
    assert "original failure" in failure_result.output


def test_once_hook_is_atomic_across_parallel_calls() -> None:
    engine = HookEngine([_hook("only-once", HookEvent.PRE_TOOL_USE, HookActionType.DENY, "one call", once=True)])

    def evaluate(index: int) -> bool:
        return engine.evaluate_pre_tool(
            HookContext(
                event=HookEvent.PRE_TOOL_USE,
                data={"tool_name": "read_file", "tool_input": {"path": str(index)}},
                metadata={"call_index": index},
            )
        ).allowed

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(evaluate, range(16)))

    engine.close()
    assert decisions.count(False) == 1


def test_context_action_is_ephemeral_and_lifecycle_events_fire() -> None:
    class CapturingModel:
        def __init__(self) -> None:
            self.calls: list[list[ChatMessage]] = []

        def next(self, messages: list[ChatMessage]) -> AgentStep:
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                return AgentStep(type="tool_calls", calls=[{"id": "1", "toolName": "echo", "input": {}}])
            return AgentStep(type="assistant", content="done")

    engine = HookEngine(
        [_hook("add-guidance", HookEvent.POST_TOOL_USE, HookActionType.CONTEXT, "Review {tool} output")]
    )
    lifecycle: list[HookEvent] = []
    engine.register(HookEvent.AGENT_START, lambda context: lifecycle.append(context.event))
    engine.register(HookEvent.AGENT_STOP, lambda context: lifecycle.append(context.event))
    registry = ToolRegistry(
        [
            ToolDefinition(
                name="echo",
                description="echo",
                input_schema={"type": "object"},
                validator=lambda value: value,
                run=lambda _value, _context: ToolResult(ok=True, output="ok"),
            )
        ]
    )
    model = CapturingModel()

    result = run_agent_turn(
        model=model,
        tools=registry,
        messages=[{"role": "system", "content": "base"}],
        cwd=".",
        hook_engine=engine,
    )

    engine.close()
    injected = [message for message in model.calls[1] if message.get("role") == "system"]
    assert any("<hook-context" in message.get("content", "") for message in injected)
    assert not any("<hook-context" in message.get("content", "") for message in result)
    assert lifecycle == [HookEvent.AGENT_START, HookEvent.AGENT_STOP]


def test_context_batch_truncation_preserves_closing_tags() -> None:
    hooks = [
        HookDefinition(
            id=f"context-{index}",
            event=HookEvent.STARTUP,
            action=HookAction(type=HookActionType.CONTEXT, message="x" * 2_000),
        )
        for index in range(3)
    ]
    engine = HookEngine(hooks)
    engine.emit(HookEvent.STARTUP, HookContext(event=HookEvent.STARTUP))

    messages = engine.drain_context_messages()
    engine.close()

    assert sum(len(message) for message in messages) <= 6_000
    assert all(message.endswith("</hook-context>") for message in messages)


def test_plan_mode_blocks_command_action_before_runner(tmp_path: Path) -> None:
    called = False

    def runner(_argv, _timeout, _context) -> HookActionResult:
        nonlocal called
        called = True
        return HookActionResult(output="ran")

    command = HookDefinition(
        id="post-command",
        event=HookEvent.POST_TOOL_USE,
        action=HookAction(type=HookActionType.COMMAND, argv=("python", "--version")),
    )
    engine = HookEngine([command], command_runner=runner)
    permissions = PermissionManager(str(tmp_path))
    permissions.enter_plan_mode()
    engine.emit(
        HookEvent.POST_TOOL_USE,
        HookContext(
            event=HookEvent.POST_TOOL_USE,
            cwd=str(tmp_path),
            metadata={"permission_mode": "plan"},
        ),
    )

    notifications = engine.drain_notifications()
    engine.close()
    assert not called
    assert notifications and not notifications[0].success
    assert "Plan mode" in notifications[0].output


def test_background_hook_shutdown_signals_cancellation() -> None:
    started = threading.Event()

    def runner(_argv, _timeout, context) -> HookActionResult:
        started.set()
        cancelled = context.metadata["_hook_cancel_event"].wait(5)
        return HookActionResult(output="cancelled" if cancelled else "finished", success=not cancelled)

    command = HookDefinition(
        id="background-command",
        event=HookEvent.SHUTDOWN,
        action=HookAction(type=HookActionType.COMMAND, argv=("slow",), background=True),
    )
    engine = HookEngine([command], command_runner=runner)
    engine.emit(HookEvent.SHUTDOWN, HookContext(event=HookEvent.SHUTDOWN))
    assert started.wait(1)

    before = time.monotonic()
    engine.close(timeout_seconds=0.01)

    assert time.monotonic() - before < 1
    assert engine.drain_notifications()[0].output == "cancelled"


def test_project_hooks_require_content_bound_trust(monkeypatch, tmp_path: Path) -> None:
    from pepsicode.hooks import loader

    monkeypatch.setattr(loader, "USER_HOOKS_PATH", tmp_path / "missing-user-hooks.json")
    workspace = tmp_path / "workspace"
    config_path = workspace / ".pepsi-code" / "hooks.json"
    config_path.parent.mkdir(parents=True)
    config = {
        "version": 1,
        "hooks": [
            {
                "id": "project-notice",
                "event": "startup",
                "action": {"type": "notify", "message": "ready"},
            }
        ],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    trust = HookTrustStore(tmp_path / "trust.json")

    untrusted = load_hooks(workspace, trust_store=trust)
    assert untrusted.untrusted_paths == [config_path.resolve()]
    assert not untrusted.hooks[0].enabled

    trust.trust(config_path)
    trusted = load_hooks(workspace, trust_store=trust)
    assert trusted.hooks[0].enabled

    config["hooks"][0]["action"]["message"] = "changed"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    changed = load_hooks(workspace, trust_store=trust)
    assert not changed.hooks[0].enabled


def test_release_is_second_major_version() -> None:
    assert VERSION == "2.0.0"
