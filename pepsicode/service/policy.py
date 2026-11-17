"""Unattended (pre-authorization) policy for service mode.

Low-disturbance collaboration is "authorized-scope auto execution enforced by
host boundaries" — never a global approval skip.  The policy is a versioned,
strictly validated document persisted at
``<workspace>/.pepsi-code/service/policy.json``.  It records *who* authorized
what, for which repository/paths, up to when, and can be revoked at any time;
revocation and expiry take effect at the next side-effect checkpoint.

The policy can only ever *allow* actions inside its explicit scope.  Anything
not covered returns ``ask``; with no human attached the service denies and
files a merged notification instead of retrying silently.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

POLICY_VERSION = 1

# Fields every policy must carry.  Unknown keys are rejected so a typo cannot
# silently widen the authorization scope.
_REQUIRED_FIELDS = {
    "version",
    "user",
    "workspace",
    "allowed_paths",
    "allowed_tools",
    "allowed_commands",
    "network_targets",
    "limits",
    "external_actions",
    "expires_at",
}
_OPTIONAL_FIELDS = {"revoked", "revoked_at", "notes", "repositories"}
_ALL_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS

_LIMIT_FIELDS = {"max_cost_usd", "max_requests", "max_duration_seconds"}


class PolicyError(ValueError):
    """The policy document is malformed or semantically invalid."""


def policy_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".pepsi-code" / "service" / "policy.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _string_list(policy: dict, key: str) -> list[str]:
    values = policy.get(key, [])
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise PolicyError(f"policy.{key} must be an array of strings")
    return values


def validate_policy(policy: dict) -> dict:
    """Strictly validate a policy document; returns it unchanged when valid."""
    if not isinstance(policy, dict):
        raise PolicyError("policy must be an object")
    unknown = set(policy) - _ALL_FIELDS
    if unknown:
        raise PolicyError(f"unknown policy fields: {', '.join(sorted(unknown))}")
    missing = _REQUIRED_FIELDS - set(policy)
    if missing:
        raise PolicyError(f"missing policy fields: {', '.join(sorted(missing))}")
    if policy.get("version") != POLICY_VERSION:
        raise PolicyError(f"policy.version must be {POLICY_VERSION}")
    for key in ("user", "workspace", "expires_at"):
        if not isinstance(policy.get(key), str) or not policy[key].strip():
            raise PolicyError(f"policy.{key} must be a non-empty string")
    _parse_expires_at(policy["expires_at"])
    for key in ("allowed_paths", "allowed_tools", "allowed_commands", "network_targets", "external_actions"):
        _string_list(policy, key)
    limits = policy.get("limits")
    if not isinstance(limits, dict) or set(limits) - _LIMIT_FIELDS:
        raise PolicyError(f"policy.limits must be an object with fields: {', '.join(sorted(_LIMIT_FIELDS))}")
    for key, value in limits.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise PolicyError(f"policy.limits.{key} must be a positive number or null")
        if key == "max_requests" and not isinstance(value, int):
            raise PolicyError("policy.limits.max_requests must be an integer")
    if policy.get("revoked") is not None and type(policy.get("revoked")) is not bool:
        raise PolicyError("policy.revoked must be a boolean")
    if policy.get("repositories") is not None:
        _string_list(policy, "repositories")
    return policy


def _parse_expires_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PolicyError("policy.expires_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise PolicyError("policy.expires_at must include a timezone")
    return parsed


def load_policy(workspace: str | Path) -> dict | None:
    """Load and validate the workspace policy; ``None`` when not configured."""
    try:
        raw = json.loads(policy_path(workspace).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise PolicyError(f"policy.json is unreadable: {error}") from error
    return validate_policy(raw)


def save_policy(workspace: str | Path, policy: dict) -> dict:
    _atomic_write_json(policy_path(workspace), validate_policy(policy))
    return policy


def revoke_policy(workspace: str | Path, *, when: datetime | None = None) -> dict | None:
    """Mark the persisted policy revoked; effective at the next checkpoint."""
    policy = load_policy(workspace)
    if policy is None:
        return None
    stamp = (when or datetime.now(UTC)).isoformat(timespec="seconds")
    policy["revoked"] = True
    policy["revoked_at"] = stamp
    _atomic_write_json(policy_path(workspace), policy)
    return policy


def policy_effective(policy: dict | None, *, now: datetime | None = None) -> tuple[bool, str]:
    """Check revocation/expiry; returns ``(effective, reason)``.

    Every checkpoint (task acceptance, dispatch, side-effect entrance) must
    call this — a policy valid at acceptance may be revoked mid-run.
    """
    if policy is None:
        return False, "no unattended policy is configured"
    try:
        validate_policy(policy)
    except PolicyError as error:
        return False, str(error)
    if policy.get("revoked"):
        return False, f"policy revoked at {policy.get('revoked_at', 'unknown time')}"
    now = now or datetime.now(UTC)
    if _parse_expires_at(policy["expires_at"]) <= now:
        return False, f"policy expired at {policy['expires_at']}"
    return True, ""


def _request_text(request: dict) -> str:
    parts = [str(request.get("summary", ""))]
    parts.extend(str(item) for item in request.get("details", []) or [])
    return "\n".join(parts)


def _matches_prefix(text: str, prefixes: list[str]) -> bool:
    return any(text == prefix or text.startswith(prefix + " ") for value in prefixes if (prefix := value.strip()))


def _matches_network_target(url: str, targets: list[str]) -> bool:
    clean_url = url.strip()
    parsed = urlsplit(clean_url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    for raw in targets:
        entry = raw.strip()
        if not entry:
            continue
        if entry.startswith(("http://", "https://")):
            if clean_url == entry or clean_url.startswith(entry.rstrip("/") + "/") or clean_url.startswith(entry + "?"):
                return True
        elif entry.startswith("*."):
            suffix = entry[2:].lower()
            if host == suffix or host.endswith("." + suffix):
                return True
        else:
            lowered = entry.lower()
            if host == lowered or (parsed.port and f"{host}:{parsed.port}" == lowered):
                return True
    return False


def _command_candidates(command: str, argv: list[str] | None) -> list[str]:
    candidates: list[str] = []
    if command and command.strip():
        candidates.append(command.strip())
    if argv:
        items = [str(part) for part in argv]
        stem = Path(items[0]).stem
        name = Path(items[0]).name
        candidates.append(" ".join([stem, *items[1:]]))
        candidates.append(" ".join([name, *items[1:]]))
        candidates.append(" ".join(items))
        if stem.lower().startswith("python"):
            candidates.append(" ".join(["python", *items[1:]]))
    seen: list[str] = []
    for item in candidates:
        stripped = item.strip()
        if stripped and stripped not in seen:
            seen.append(stripped)
    return seen


def evaluate_request(policy: dict | None, request: dict) -> str:
    """Map an approval request to ``allow`` or ``ask``.

    ``ask`` means "outside the policy scope" — the caller decides whether a
    human is available.  The policy never widens access: anything not
    explicitly covered asks.
    """
    effective, _ = policy_effective(policy)
    if not effective or policy is None:
        return "ask"
    kind = str(request.get("kind", ""))
    text = _request_text(request)
    metadata = request.get("metadata") or {}
    allowed_tools = [t.strip() for t in policy.get("allowed_tools", []) if t.strip()]
    tool = metadata.get("tool") or request.get("tool")
    if tool and allowed_tools and not _matches_prefix(str(tool), allowed_tools):
        return "ask"
    if kind == "command":
        candidates = _command_candidates(metadata.get("command", text.strip()), metadata.get("argv"))
        if not candidates or not any(_matches_prefix(c, policy.get("allowed_commands", [])) for c in candidates):
            return "ask"
        cwd_raw = metadata.get("cwd")
        if cwd_raw:
            cwd = Path(str(cwd_raw)).resolve()
            roots = [
                Path(policy["workspace"]).resolve(),
                *(Path(root).resolve() for root in policy.get("allowed_paths", []) if root.strip()),
            ]
            if not any(cwd.is_relative_to(root) for root in roots):
                return "ask"
        return "allow"
    if kind in {"path", "edit"}:
        target = Path(metadata.get("target", text.strip())).resolve()
        return (
            "allow"
            if any(
                target.is_relative_to(Path(root).resolve()) for root in policy.get("allowed_paths", []) if root.strip()
            )
            else "ask"
        )
    if kind == "network":
        raw_url = str(metadata.get("target") or metadata.get("url") or text).strip()
        url = raw_url.splitlines()[0].strip() if raw_url else ""
        return "allow" if _matches_network_target(url, policy.get("network_targets", [])) else "ask"
    if kind in {"mcp", "external"}:
        operation = metadata.get("operation")
        raw_target = str(metadata.get("target") or "").strip()
        target_line = raw_target.splitlines()[0].strip() if raw_target else ""
        if operation:
            action_ok = _matches_prefix(str(operation), policy.get("external_actions", [])) or (
                bool(target_line) and _matches_prefix(f"{operation} {target_line}", policy.get("external_actions", []))
            )
            if not action_ok:
                return "ask"
            if target_line.startswith(("http://", "https://")) and not _matches_network_target(
                target_line, policy.get("network_targets", [])
            ):
                return "ask"
            return "allow"
        if tool:
            return (
                "allow"
                if (
                    _matches_prefix(str(tool), policy.get("allowed_tools", []))
                    or _matches_prefix(str(tool), policy.get("external_actions", []))
                )
                else "ask"
            )
        return "ask"
    return "ask"


def check_policy_checkpoint(policy: dict | None, **context) -> None:
    """Enforce revocation, expiry and scope boundaries at a side-effect entrance."""
    effective, reason = policy_effective(policy)
    if not effective or policy is None:
        raise PermissionError(f"Unattended policy is not effective: {reason}")
    if not context:
        return
    allowed_tools = [t.strip() for t in policy.get("allowed_tools", []) if t.strip()]
    tool = context.get("tool")
    if tool and allowed_tools and not _matches_prefix(str(tool), allowed_tools):
        raise PermissionError(f"Tool '{tool}' is outside unattended policy allowed_tools")
    kind = context.get("kind")
    if kind in {"path", "edit"} and context.get("path"):
        target = Path(str(context["path"])).resolve()
        workspace_internal = Path(policy["workspace"]).resolve() / ".pepsi-code"
        roots = [Path(root).resolve() for root in policy.get("allowed_paths", []) if root.strip()]
        if not target.is_relative_to(workspace_internal) and not any(target.is_relative_to(root) for root in roots):
            raise PermissionError(f"Path '{target}' is outside unattended policy allowed_paths")
    if kind == "command":
        candidates = _command_candidates(str(context.get("command") or ""), context.get("argv"))
        if candidates and not any(_matches_prefix(c, policy.get("allowed_commands", [])) for c in candidates):
            raise PermissionError(f"Command '{candidates[0]}' is outside unattended policy allowed_commands")
        if context.get("path"):
            cwd = Path(str(context["path"])).resolve()
            ws = Path(policy["workspace"]).resolve()
            roots = [ws, *(Path(root).resolve() for root in policy.get("allowed_paths", []) if root.strip())]
            if not any(cwd.is_relative_to(root) for root in roots):
                raise PermissionError(f"Command cwd '{cwd}' is outside unattended policy scope")
    if kind == "network" and context.get("url"):
        url = str(context["url"]).splitlines()[0].strip()
        if not _matches_network_target(url, policy.get("network_targets", [])):
            raise PermissionError(f"Network target '{url}' is outside unattended policy network_targets")
        if context.get("mutating") and context.get("action"):
            action = str(context["action"])
            if not (
                _matches_prefix(action, policy.get("external_actions", []))
                or _matches_prefix(f"{action} {url}", policy.get("external_actions", []))
            ):
                raise PermissionError(f"External action '{action}' is outside unattended policy external_actions")
    if kind == "external" and context.get("action"):
        action = str(context["action"])
        if not _matches_prefix(action, policy.get("external_actions", [])):
            raise PermissionError(f"External action '{action}' is outside unattended policy external_actions")
