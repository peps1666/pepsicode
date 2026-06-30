from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pepsicode.config import PEPSI_CODE_PERMISSIONS_PATH

# Permission decision type - mirrors the TS PermissionDecision type
PermissionDecision = Literal[
    "allow_once",
    "allow_always",
    "allow_turn",
    "allow_all_turn",
    "deny_once",
    "deny_always",
    "deny_with_feedback",
]

PromptHandler = Callable[[dict[str, Any]], dict[str, Any]]


def _normalize_path(target_path: str) -> str:
    return str(Path(target_path).resolve())


def _is_within_directory(root: str, target: str) -> bool:
    """Check if target is within root directory.

    On Windows, uses case-insensitive comparison since NTFS paths are
    case-insensitive by default.
    """
    try:
        resolved_target = Path(target).resolve()
        resolved_root = Path(root).resolve()
        if sys.platform == "win32":
            # Windows: case-insensitive path comparison
            target_str = str(resolved_target).lower()
            root_str = str(resolved_root).lower()
            return target_str == root_str or target_str.startswith(root_str + os.sep)
        resolved_target.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def _matches_directory_prefix(target_path: str, directories: set[str]) -> bool:
    return any(_is_within_directory(directory, target_path) for directory in directories)


def _format_command_signature(command: str, args: list[str]) -> str:
    return " ".join([command, *args]).strip()


def _classify_dangerous_command(command: str, args: list[str]) -> str | None:
    normalized_args = [arg.strip() for arg in args if arg.strip()]
    signature = _format_command_signature(command, normalized_args)

    if command == "git":
        if "reset" in normalized_args and "--hard" in normalized_args:
            return f"git reset --hard can discard local changes ({signature})"
        if "clean" in normalized_args:
            return f"git clean can delete untracked files ({signature})"
        if "checkout" in normalized_args and "--" in normalized_args:
            return f"git checkout -- can overwrite working tree files ({signature})"
        if "push" in normalized_args and any(arg in {"--force", "-f"} for arg in normalized_args):
            return f"git push --force rewrites remote history ({signature})"
        if "restore" in normalized_args and any(arg.startswith("--source") for arg in normalized_args):
            return f"git restore --source can overwrite local files ({signature})"

    if command == "npm" and "publish" in normalized_args:
        return f"npm publish affects a registry outside this machine ({signature})"

    if command == "rm":
        # Combine all flags (supports -rf, -fr, -Rf, -r -f, etc.)
        combined_flags = "".join(arg for arg in normalized_args if arg.startswith("-")).lower()
        # Check whether both the recursive and force flags are present
        if "r" in combined_flags and "f" in combined_flags:
            # Check whether it targets the root directory or uses --no-preserve-root
            if any(arg in {"/", "/*"} for arg in normalized_args) or "--no-preserve-root" in normalized_args:
                return f"rm -rf can cause catastrophic data loss ({signature})"
            # Even when not targeting root, still flag it as dangerous
            return f"rm -rf can cause catastrophic data loss ({signature})"

    if command in {"dd", "mkfs", "mkfs.ext4", "mkfs.vfat", "fdisk", "format"}:
        return f"{command} can modify or destroy disk partitions ({signature})"

    if command == "chmod":
        if "777" in normalized_args or any(arg.endswith("777") for arg in normalized_args):
            return f"chmod 777 opens permissions to all users ({signature})"

    if command in {
        "node",
        "python",
        "python3",
        "pythonw",
        "bun",
        "bash",
        "sh",
        "zsh",
        "fish",
        "powershell",
        "pwsh",
    }:
        return f"{command} can execute arbitrary local code ({signature})"

    # macOS-specific dangerous commands
    if command == "diskutil":
        return f"diskutil can erase or partition disks ({signature})"
    if command == "csrutil":
        return f"csrutil modifies System Integrity Protection ({signature})"
    if command == "defaults" and "write" in normalized_args:
        return f"defaults write modifies system preferences ({signature})"
    if command == "launchctl" and any(arg in {"unload", "bootout", "disable"} for arg in normalized_args):
        return f"launchctl can disable system services ({signature})"
    if command == "dscl":
        return f"dscl can modify directory services and user accounts ({signature})"

    return None


def _read_permission_store() -> dict[str, Any]:
    if not PEPSI_CODE_PERMISSIONS_PATH.exists():
        return {}
    try:
        data = json.loads(PEPSI_CODE_PERMISSIONS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        # Corrupted file - return an empty store and log a warning
        import warnings

        warnings.warn(f"Corrupted permissions file, resetting: {e}")
        return {}


def _write_permission_store(store: dict[str, Any]) -> None:
    """Persist the permission store atomically to avoid race conditions."""
    import tempfile

    PEPSI_CODE_PERMISSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temporary file first
    fd, tmp_path = tempfile.mkstemp(dir=PEPSI_CODE_PERMISSIONS_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
            f.write("\n")
        # Atomic replace
        os.replace(tmp_path, PEPSI_CODE_PERMISSIONS_PATH)
    except Exception:
        # Clean up the temporary file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class PermissionManager:
    def __init__(self, workspace_root: str, prompt: PromptHandler | None = None) -> None:
        self.workspace_root = _normalize_path(workspace_root)
        self.prompt = prompt
        self.allowed_directory_prefixes: set[str] = set()
        self.denied_directory_prefixes: set[str] = set()
        self.session_allowed_paths: set[str] = set()
        self.session_denied_paths: set[str] = set()
        self.allowed_command_patterns: set[str] = set()
        self.denied_command_patterns: set[str] = set()
        self.session_allowed_commands: set[str] = set()
        self.session_denied_commands: set[str] = set()
        self.allowed_edit_patterns: set[str] = set()
        self.denied_edit_patterns: set[str] = set()
        self.session_allowed_edits: set[str] = set()
        self.session_denied_edits: set[str] = set()
        self.turn_allowed_edits: set[str] = set()
        self.turn_allow_all_edits = False
        self._initialize()

    def _initialize(self) -> None:
        store = _read_permission_store()
        self.allowed_directory_prefixes |= {_normalize_path(item) for item in store.get("allowedDirectoryPrefixes", [])}
        self.denied_directory_prefixes |= {_normalize_path(item) for item in store.get("deniedDirectoryPrefixes", [])}
        self.allowed_command_patterns |= set(store.get("allowedCommandPatterns", []))
        self.denied_command_patterns |= set(store.get("deniedCommandPatterns", []))
        self.allowed_edit_patterns |= {_normalize_path(item) for item in store.get("allowedEditPatterns", [])}
        self.denied_edit_patterns |= {_normalize_path(item) for item in store.get("deniedEditPatterns", [])}

    def begin_turn(self) -> None:
        self.turn_allowed_edits.clear()
        self.turn_allow_all_edits = False

    def end_turn(self) -> None:
        self.begin_turn()

    def get_summary(self) -> list[str]:
        def _preview(items: set[str], limit: int, max_chars: int = 64) -> str:
            if not items:
                return "none"
            ordered = sorted(items)
            shown: list[str] = []
            for item in ordered[:limit]:
                shown.append(item if len(item) <= max_chars else item[: max_chars - 3] + "...")
            remaining = len(ordered) - len(shown)
            suffix = f" (+{remaining} more)" if remaining > 0 else ""
            return ", ".join(shown) + suffix

        summary = [f"cwd: {self.workspace_root}"]
        summary.append("extra allowed dirs: " + _preview(self.allowed_directory_prefixes, 3))
        summary.append("dangerous allowlist: " + _preview(self.allowed_command_patterns, 2, max_chars=48))
        if self.allowed_edit_patterns:
            summary.append("trusted edit targets: " + _preview(self.allowed_edit_patterns, 2))
        return summary

    def _persist(self) -> None:
        _write_permission_store(
            {
                "allowedDirectoryPrefixes": sorted(self.allowed_directory_prefixes),
                "deniedDirectoryPrefixes": sorted(self.denied_directory_prefixes),
                "allowedCommandPatterns": sorted(self.allowed_command_patterns),
                "deniedCommandPatterns": sorted(self.denied_command_patterns),
                "allowedEditPatterns": sorted(self.allowed_edit_patterns),
                "deniedEditPatterns": sorted(self.denied_edit_patterns),
            }
        )

    def ensure_path_access(self, target_path: str, intent: str) -> None:
        normalized_target = _normalize_path(target_path)
        if _is_within_directory(self.workspace_root, normalized_target):
            return
        if normalized_target in self.session_denied_paths or _matches_directory_prefix(
            normalized_target, self.denied_directory_prefixes
        ):
            raise RuntimeError(f"Access denied for path outside cwd: {normalized_target}")
        if normalized_target in self.session_allowed_paths or _matches_directory_prefix(
            normalized_target, self.allowed_directory_prefixes
        ):
            return
        if self.prompt is None:
            raise RuntimeError(
                f"Path {normalized_target} is outside cwd {self.workspace_root}. Start pepsicode in TTY mode to approve it."
            )

        scope_directory = (
            normalized_target if intent in {"list", "command_cwd"} else str(Path(normalized_target).parent)
        )
        result = self.prompt(
            {
                "kind": "path",
                "summary": f"pepsicode wants {intent.replace('_', ' ')} access outside the current cwd",
                "details": [
                    f"cwd: {self.workspace_root}",
                    f"target: {normalized_target}",
                    f"scope directory: {scope_directory}",
                ],
                "scope": scope_directory,
                "choices": [
                    {"key": "y", "label": "allow once", "decision": "allow_once"},
                    {"key": "a", "label": "allow this directory", "decision": "allow_always"},
                    {"key": "n", "label": "deny once", "decision": "deny_once"},
                    {"key": "d", "label": "deny this directory", "decision": "deny_always"},
                ],
            }
        )
        decision = result.get("decision")
        if decision == "allow_once":
            self.session_allowed_paths.add(normalized_target)
            return
        if decision == "allow_always":
            self.allowed_directory_prefixes.add(scope_directory)
            self._persist()
            return
        if decision == "deny_always":
            self.denied_directory_prefixes.add(scope_directory)
            self._persist()
        else:
            self.session_denied_paths.add(normalized_target)
        raise RuntimeError(f"Access denied for path outside cwd: {normalized_target}")

    def ensure_command(
        self,
        command: str,
        args: list[str],
        command_cwd: str,
        force_prompt_reason: str | None = None,
    ) -> None:
        self.ensure_path_access(command_cwd, "command_cwd")
        reason = force_prompt_reason or _classify_dangerous_command(command, args)
        if not reason:
            return
        signature = _format_command_signature(command, args)
        if signature in self.session_denied_commands or signature in self.denied_command_patterns:
            raise RuntimeError(f"Command denied: {signature}")
        if signature in self.session_allowed_commands or signature in self.allowed_command_patterns:
            return
        if self.prompt is None:
            raise RuntimeError(f"Command requires approval: {signature}. Start pepsicode in TTY mode to approve it.")
        # Distinguish forced prompts (external trigger) from dangerous commands
        summary = (
            "pepsicode wants to run a dangerous command"
            if not force_prompt_reason
            else "pepsicode wants approval for this command"
        )
        result = self.prompt(
            {
                "kind": "command",
                "summary": summary,
                "details": [f"cwd: {command_cwd}", f"command: {signature}", f"reason: {reason}"],
                "scope": signature,
                "choices": [
                    {"key": "y", "label": "allow once", "decision": "allow_once"},
                    {"key": "a", "label": "always allow this command", "decision": "allow_always"},
                    {"key": "n", "label": "deny once", "decision": "deny_once"},
                    {"key": "d", "label": "always deny this command", "decision": "deny_always"},
                ],
            }
        )
        decision = result.get("decision")
        if decision == "allow_once":
            self.session_allowed_commands.add(signature)
            return
        if decision == "allow_always":
            self.allowed_command_patterns.add(signature)
            self._persist()
            return
        if decision == "deny_always":
            self.denied_command_patterns.add(signature)
            self._persist()
        else:
            self.session_denied_commands.add(signature)
        raise RuntimeError(f"Command denied: {signature}")

    def ensure_edit(self, target_path: str, diff_preview: str) -> None:
        normalized_target = _normalize_path(target_path)
        if normalized_target in self.session_denied_edits or normalized_target in self.denied_edit_patterns:
            raise RuntimeError(f"Edit denied: {normalized_target}")
        if (
            normalized_target in self.session_allowed_edits
            or normalized_target in self.turn_allowed_edits
            or self.turn_allow_all_edits
            or normalized_target in self.allowed_edit_patterns
        ):
            return
        if self.prompt is None:
            raise RuntimeError(
                f"Edit requires approval: {normalized_target}. Start pepsicode in TTY mode to review it."
            )
        result = self.prompt(
            {
                "kind": "edit",
                "summary": "pepsicode wants to apply a file modification",
                "details": [f"target: {normalized_target}", "", diff_preview],
                "scope": normalized_target,
                "choices": [
                    {"key": "1", "label": "apply once", "decision": "allow_once"},
                    {"key": "2", "label": "allow this file in this turn", "decision": "allow_turn"},
                    {"key": "3", "label": "allow all edits in this turn", "decision": "allow_all_turn"},
                    {"key": "4", "label": "always allow this file", "decision": "allow_always"},
                    {"key": "5", "label": "reject once", "decision": "deny_once"},
                    {"key": "6", "label": "reject and send guidance to model", "decision": "deny_with_feedback"},
                    {"key": "7", "label": "always reject this file", "decision": "deny_always"},
                ],
            }
        )
        decision = result.get("decision")
        if decision == "allow_once":
            self.session_allowed_edits.add(normalized_target)
            return
        if decision == "allow_turn":
            self.turn_allowed_edits.add(normalized_target)
            return
        if decision == "allow_all_turn":
            self.turn_allow_all_edits = True
            return
        if decision == "allow_always":
            self.allowed_edit_patterns.add(normalized_target)
            self._persist()
            return
        if decision == "deny_with_feedback":
            guidance = str(result.get("feedback", "")).strip()
            if guidance:
                raise RuntimeError(f"Edit denied: {normalized_target}\nUser guidance: {guidance}")
        if decision == "deny_always":
            self.denied_edit_patterns.add(normalized_target)
            self._persist()
        else:
            self.session_denied_edits.add(normalized_target)
        raise RuntimeError(f"Edit denied: {normalized_target}")
