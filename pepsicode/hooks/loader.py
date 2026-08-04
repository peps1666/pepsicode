from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pepsicode.config import PEPSI_CODE_DIR
from pepsicode.hooks.conditions import HookConditionError, parse_conditions
from pepsicode.hooks.models import (
    HookAction,
    HookActionType,
    HookDefinition,
    HookDiagnostic,
    HookEvent,
    HookLoadResult,
    HookSource,
)
from pepsicode.hooks.trust import HookTrustStore

USER_HOOKS_PATH = PEPSI_CODE_DIR / "hooks.json"
PROJECT_HOOKS_FILE = Path(".pepsi-code") / "hooks.json"
LOCAL_HOOKS_FILE = Path(".pepsi-code") / "hooks.local.json"
_HOOK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VALID_SCOPES = {"main", "subagent"}
_VALID_ERROR_POLICIES = {"ignore", "warn", "deny"}


class HookConfigError(ValueError):
    pass


def _boolean(value: Any, *, label: str, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise HookConfigError(f"{label}: {field} must be a boolean")
    return value


def hook_config_paths(cwd: str | Path) -> list[tuple[Path, HookSource]]:
    workspace = Path(cwd).resolve()
    return [
        (USER_HOOKS_PATH, HookSource.USER),
        (workspace / PROJECT_HOOKS_FILE, HookSource.PROJECT),
        (workspace / LOCAL_HOOKS_FILE, HookSource.LOCAL),
    ]


def _parse_action(raw: Any, *, event: HookEvent, label: str) -> HookAction:
    if not isinstance(raw, dict):
        raise HookConfigError(f"{label}: 'action' must be an object")
    try:
        action_type = HookActionType(str(raw.get("type", "")))
    except ValueError as error:
        valid = ", ".join(item.value for item in HookActionType)
        raise HookConfigError(f"{label}: invalid action type; expected one of: {valid}") from error

    message = raw.get("message", "")
    if not isinstance(message, str):
        raise HookConfigError(f"{label}: action.message must be a string")
    argv_raw = raw.get("argv", [])
    if not isinstance(argv_raw, list) or not all(isinstance(item, str) for item in argv_raw):
        raise HookConfigError(f"{label}: action.argv must be an array of strings")
    if action_type == HookActionType.COMMAND and not argv_raw:
        raise HookConfigError(f"{label}: command action requires a non-empty argv")
    if action_type != HookActionType.COMMAND and argv_raw:
        raise HookConfigError(f"{label}: argv is only valid for command actions")
    if action_type in {HookActionType.DENY, HookActionType.NOTIFY, HookActionType.CONTEXT} and not message.strip():
        raise HookConfigError(f"{label}: {action_type.value} action requires a message")
    if action_type == HookActionType.DENY and event != HookEvent.PRE_TOOL_USE:
        raise HookConfigError(f"{label}: deny action is only valid for pre_tool_use")
    if event == HookEvent.PRE_TOOL_USE and action_type == HookActionType.COMMAND:
        raise HookConfigError(f"{label}: command actions are not allowed for pre_tool_use")

    timeout = raw.get("timeoutSeconds", 30)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
        raise HookConfigError(f"{label}: timeoutSeconds must be an integer from 1 to 300")
    background = _boolean(raw.get("background"), label=label, field="action.background", default=False)
    if event == HookEvent.PRE_TOOL_USE and background:
        raise HookConfigError(f"{label}: pre_tool_use actions cannot run in the background")
    expose_output = _boolean(
        raw.get("exposeOutputToModel"),
        label=label,
        field="action.exposeOutputToModel",
        default=False,
    )
    if expose_output and action_type != HookActionType.COMMAND:
        raise HookConfigError(f"{label}: exposeOutputToModel is only valid for command actions")
    return HookAction(
        type=action_type,
        message=message,
        argv=tuple(argv_raw),
        timeout_seconds=timeout,
        background=background,
        expose_output_to_model=expose_output,
    )


def _parse_hook(entry: Any, *, index: int, source: HookSource, path: Path, trusted: bool) -> HookDefinition:
    label = f"hook #{index + 1}"
    if not isinstance(entry, dict):
        raise HookConfigError(f"{label}: must be an object")
    hook_id = entry.get("id")
    if not isinstance(hook_id, str) or not _HOOK_ID_PATTERN.fullmatch(hook_id):
        raise HookConfigError(f"{label}: id must match {_HOOK_ID_PATTERN.pattern}")
    label = f"hook '{hook_id}'"
    try:
        event = HookEvent(str(entry.get("event", "")))
    except ValueError as error:
        valid = ", ".join(item.value for item in HookEvent)
        raise HookConfigError(f"{label}: invalid event; expected one of: {valid}") from error
    try:
        condition = parse_conditions(entry.get("when"))
    except HookConditionError as error:
        raise HookConfigError(f"{label}: {error}") from error
    action = _parse_action(entry.get("action"), event=event, label=label)

    priority = entry.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool) or not -10_000 <= priority <= 10_000:
        raise HookConfigError(f"{label}: priority must be an integer from -10000 to 10000")
    scopes_raw = entry.get("scope", ["main", "subagent"])
    if isinstance(scopes_raw, str):
        scopes_raw = [scopes_raw]
    if (
        not isinstance(scopes_raw, list)
        or not scopes_raw
        or not all(isinstance(item, str) and item in _VALID_SCOPES for item in scopes_raw)
    ):
        raise HookConfigError(f"{label}: scope must contain only: main, subagent")
    on_error = entry.get("onError", "warn")
    if not isinstance(on_error, str):
        raise HookConfigError(f"{label}: onError must be a string")
    if on_error not in _VALID_ERROR_POLICIES:
        raise HookConfigError(f"{label}: onError must be one of: ignore, warn, deny")
    if on_error == "deny" and event != HookEvent.PRE_TOOL_USE:
        raise HookConfigError(f"{label}: onError=deny is only valid for pre_tool_use")

    return HookDefinition(
        id=hook_id,
        event=event,
        action=action,
        condition=condition,
        priority=priority,
        scopes=tuple(scopes_raw),
        once=_boolean(entry.get("once"), label=label, field="once", default=False),
        enabled=_boolean(entry.get("enabled"), label=label, field="enabled", default=True) and trusted,
        on_error=on_error,
        source=source,
        source_path=str(path),
        trusted=trusted,
    )


def _load_file(path: Path, source: HookSource, trusted: bool) -> tuple[list[HookDefinition], list[HookDiagnostic]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [], [HookDiagnostic(path=str(path), message=f"Invalid JSON: {error}")]
    if isinstance(raw, list):
        raw = {"version": 1, "hooks": raw}
    if not isinstance(raw, dict):
        return [], [HookDiagnostic(path=str(path), message="Root must be an object or hook array")]
    version = raw.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        return [], [HookDiagnostic(path=str(path), message="Unsupported hooks config version")]
    entries = raw.get("hooks", [])
    if not isinstance(entries, list):
        return [], [HookDiagnostic(path=str(path), message="'hooks' must be an array")]

    hooks: list[HookDefinition] = []
    diagnostics: list[HookDiagnostic] = []
    for index, entry in enumerate(entries):
        try:
            hooks.append(_parse_hook(entry, index=index, source=source, path=path, trusted=trusted))
        except HookConfigError as error:
            diagnostics.append(HookDiagnostic(path=str(path), message=str(error)))
    try:
        strict = _boolean(raw.get("strict"), label="config", field="strict", default=False)
    except HookConfigError as error:
        diagnostics.append(HookDiagnostic(path=str(path), message=str(error)))
        return [], diagnostics
    if strict and diagnostics:
        return [], diagnostics
    return hooks, diagnostics


def load_hooks(
    cwd: str | Path,
    *,
    trust_store: HookTrustStore | None = None,
    trusted_once: set[str] | None = None,
) -> HookLoadResult:
    trust_store = trust_store or HookTrustStore()
    trusted_once = {str(Path(path).resolve()) for path in (trusted_once or set())}
    result = HookLoadResult()
    merged: dict[str, HookDefinition] = {}
    for path, source in hook_config_paths(cwd):
        if not path.is_file():
            continue
        resolved = str(path.resolve())
        trusted = source == HookSource.USER or resolved in trusted_once or trust_store.is_trusted(path)
        hooks, diagnostics = _load_file(path, source, trusted)
        result.diagnostics.extend(diagnostics)
        if not trusted:
            result.untrusted_paths.append(path.resolve())
            result.diagnostics.append(
                HookDiagnostic(
                    path=resolved,
                    message="Project hook configuration is not trusted; hooks from this file are disabled",
                    severity="warning",
                )
            )
        for hook in hooks:
            existing = merged.get(hook.id)
            if not hook.trusted and existing is not None and existing.trusted:
                result.diagnostics.append(
                    HookDiagnostic(
                        path=resolved,
                        message=f"Untrusted hook '{hook.id}' cannot override a trusted hook",
                        severity="warning",
                    )
                )
                continue
            merged[hook.id] = hook
    result.hooks = sorted(merged.values(), key=lambda item: (item.priority, item.id))
    return result
