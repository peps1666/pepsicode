from __future__ import annotations

from pathlib import Path
from typing import Any

from pepsicode.hooks.engine import HookEngine, hooks_enabled_from_environment
from pepsicode.hooks.loader import hook_config_paths, load_hooks
from pepsicode.hooks.models import HookActionResult, HookContext, HookSource
from pepsicode.hooks.trust import HookTrustStore


def _build_command_runner(permissions: Any):
    def _run(argv: tuple[str, ...], timeout_seconds: int, context: HookContext) -> HookActionResult:
        if not argv:
            return HookActionResult(output="Hook command argv is empty", success=False)
        from pepsicode.tooling import ToolContext
        from pepsicode.tools.run_command import run_command_tool

        parsed = run_command_tool.validator(
            {
                "command": argv[0],
                "args": list(argv[1:]),
                "cwd": context.cwd,
                "timeout": timeout_seconds,
            }
        )
        result = run_command_tool.run(
            parsed,
            ToolContext(
                cwd=context.cwd,
                permissions=permissions,
                hooks=None,
                agent_scope=context.agent_scope,
                suppress_hooks=True,
                cancellation_event=context.metadata.get("_hook_cancel_event"),
            ),
        )
        return HookActionResult(output=result.output, success=result.ok)

    return _run


def create_hook_engine(cwd: str, permissions: Any = None) -> HookEngine:
    if not hooks_enabled_from_environment():
        return HookEngine(command_runner=_build_command_runner(permissions), enabled=False)

    trust_store = HookTrustStore()
    trusted_once: set[str] = set()

    initial = load_hooks(cwd, trust_store=trust_store)
    if initial.untrusted_paths and permissions is not None and getattr(permissions, "prompt", None) is not None:
        for path in initial.untrusted_paths:
            result = permissions.prompt(
                {
                    "kind": "hook_trust",
                    "summary": "pepsicode found project Hooks v2 configuration",
                    "details": [
                        f"path: {path}",
                        "Project hooks can deny tools, inject model context, or run approved commands.",
                    ],
                    "scope": str(path),
                    "choices": [
                        {"key": "1", "label": "trust this version", "decision": "allow_always"},
                        {"key": "2", "label": "trust once", "decision": "allow_once"},
                        {"key": "3", "label": "keep disabled", "decision": "deny_once"},
                    ],
                }
            )
            result = result or {}
            if result.get("decision") == "allow_always":
                trust_store.trust(path)
            elif result.get("decision") == "allow_once":
                trusted_once.add(str(path.resolve()))
        initial = load_hooks(cwd, trust_store=trust_store, trusted_once=trusted_once)

    engine = HookEngine(
        initial.hooks,
        diagnostics=initial.diagnostics,
        command_runner=_build_command_runner(permissions),
        enabled=True,
    )

    def _reload() -> tuple[list, list]:
        loaded = load_hooks(cwd, trust_store=trust_store, trusted_once=trusted_once)
        return loaded.hooks, loaded.diagnostics

    def _trust() -> str:
        trusted: list[str] = []
        for path, source in hook_config_paths(cwd):
            if source != HookSource.USER and path.is_file():
                trust_store.trust(path)
                trusted.append(str(Path(path).resolve()))
        hooks, diagnostics = _reload()
        engine.replace_hooks(hooks, diagnostics)
        if not trusted:
            return "No project hook files found."
        return "Trusted and reloaded:\n" + "\n".join(trusted)

    engine.set_management_callbacks(reload_callback=_reload, trust_callback=_trust)
    return engine
