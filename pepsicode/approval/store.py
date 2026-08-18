"""Persistent + session + turn caches for approval decisions.

Previously these 12 ``set`` containers lived directly on ``PermissionManager``.
Extracting them into a dedicated class lets the manager stay focused on
Plan-mode boundaries and composition, while the store handles the three
cache tiers:

* **Persistent** (``allow_always`` / ``deny_always``): written to
  ``~/.pepsi-code/permissions.json`` and reloaded on startup.
* **Session** (``allow_once`` / ``deny_once``): in-memory for the process.
* **Turn** (``allow_turn`` / ``allow_all_turn``): cleared at every
  ``begin_turn``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any

from pepsicode.approval.types import ApprovalDecision, ApprovalKind, ApprovalOutcome
from pepsicode.config import PEPSI_CODE_PERMISSIONS_PATH


def _normalize_path(target_path: str) -> str:
    return str(Path(target_path).resolve())


def _is_within_directory(root: str, target: str) -> bool:
    """Check if target is within root directory (case-insensitive on Windows)."""
    try:
        resolved_target = Path(target).resolve()
        resolved_root = Path(root).resolve()
        if sys.platform == "win32":
            target_str = str(resolved_target).lower()
            root_str = str(resolved_root).lower()
            return target_str == root_str or target_str.startswith(root_str + os.sep)
        resolved_target.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def _matches_directory_prefix(target_path: str, directories: set[str]) -> bool:
    return any(_is_within_directory(directory, target_path) for directory in directories)


class ApprovalStore:
    """Three-tier cache for approval decisions.

    The store is agnostic to *how* decisions are made — it only knows how
    to look them up and record them.  The :class:`PermissionManager` is
    responsible for constructing :class:`ApprovalRequest` objects and
    invoking an :class:`ApprovalBackend`.
    """

    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = _normalize_path(workspace_root)

        # Persistent (allow_always / deny_always) — backed by JSON file
        self.allowed_directory_prefixes: set[str] = set()
        self.denied_directory_prefixes: set[str] = set()
        self.allowed_command_patterns: set[str] = set()
        self.denied_command_patterns: set[str] = set()
        self.allowed_edit_patterns: set[str] = set()
        self.denied_edit_patterns: set[str] = set()

        # Session-level (allow_once / deny_once) — keyed by scope string
        self.session_allowed_paths: set[str] = set()
        self.session_denied_paths: set[str] = set()
        self.session_allowed_commands: set[str] = set()
        self.session_denied_commands: set[str] = set()
        self.session_allowed_edits: set[str] = set()
        self.session_denied_edits: set[str] = set()

        # Turn-level (allow_turn / allow_all_turn) — cleared each turn
        self.turn_allowed_edits: set[str] = set()
        self.turn_allow_all_edits: bool = False

        self._load()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def begin_turn(self) -> None:
        self.turn_allowed_edits.clear()
        self.turn_allow_all_edits = False

    def end_turn(self) -> None:
        self.begin_turn()

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #

    def lookup(self, kind: ApprovalKind, scope: str, *, intent: str = "") -> ApprovalOutcome | None:
        """Return a cached outcome for ``(kind, scope)``, or ``None`` if uncached.

        ``intent`` is used only for ``PATH`` lookups to pick the right scope
        directory (the file itself for ``list``/``command_cwd``, its parent
        otherwise).
        """
        if kind == ApprovalKind.PATH:
            return self._lookup_path(scope, intent)
        if kind == ApprovalKind.COMMAND:
            return self._lookup_command(scope)
        if kind == ApprovalKind.EDIT:
            return self._lookup_edit(scope)
        return None

    def _lookup_path(self, target_path: str, intent: str) -> ApprovalOutcome | None:
        normalized = _normalize_path(target_path)
        # Workspace paths are always allowed (no cache entry needed)
        if _is_within_directory(self.workspace_root, normalized):
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)

        scope_dir = normalized if intent in {"list", "command_cwd"} else str(Path(normalized).parent)

        if normalized in self.session_denied_paths or _matches_directory_prefix(
            normalized, self.denied_directory_prefixes
        ):
            return ApprovalOutcome(decision=ApprovalDecision.DENY_ONCE)
        if normalized in self.session_allowed_paths or _matches_directory_prefix(
            normalized, self.allowed_directory_prefixes
        ):
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)
        return None

    def _lookup_command(self, signature: str) -> ApprovalOutcome | None:
        if signature in self.session_denied_commands or signature in self.denied_command_patterns:
            return ApprovalOutcome(decision=ApprovalDecision.DENY_ONCE)
        if signature in self.session_allowed_commands or signature in self.allowed_command_patterns:
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)
        return None

    def _lookup_edit(self, target_path: str) -> ApprovalOutcome | None:
        normalized = _normalize_path(target_path)
        if normalized in self.session_denied_edits or normalized in self.denied_edit_patterns:
            return ApprovalOutcome(decision=ApprovalDecision.DENY_ONCE)
        if (
            normalized in self.session_allowed_edits
            or normalized in self.turn_allowed_edits
            or self.turn_allow_all_edits
            or normalized in self.allowed_edit_patterns
        ):
            return ApprovalOutcome(decision=ApprovalDecision.ALLOW_ONCE)
        return None

    # ------------------------------------------------------------------ #
    # Record
    # ------------------------------------------------------------------ #

    def record(self, kind: ApprovalKind, scope: str, outcome: ApprovalOutcome) -> None:
        """Record an outcome into the appropriate cache tier.

        For ``PATH`` decisions the scope used for ``allow_always`` /
        ``deny_always`` is the *directory* containing the target (or the
        target itself for ``list`` / ``command_cwd`` intents), matching the
        previous behaviour.
        """
        decision = outcome.decision
        if kind == ApprovalKind.PATH:
            self._record_path(scope, outcome)
        elif kind == ApprovalKind.COMMAND:
            self._record_command(scope, decision)
        elif kind == ApprovalKind.EDIT:
            self._record_edit(scope, decision)

    def _record_path(self, target_path: str, outcome: ApprovalOutcome) -> None:
        normalized = _normalize_path(target_path)
        # For path decisions we persist by parent directory (except list/cwd)
        scope_dir = str(Path(normalized).parent)
        decision = outcome.decision

        if decision == ApprovalDecision.ALLOW_ONCE:
            self.session_allowed_paths.add(normalized)
        elif decision == ApprovalDecision.ALLOW_ALWAYS:
            self.allowed_directory_prefixes.add(scope_dir)
            self._persist()
        elif decision == ApprovalDecision.DENY_ONCE:
            self.session_denied_paths.add(normalized)
        elif decision == ApprovalDecision.DENY_ALWAYS:
            self.denied_directory_prefixes.add(scope_dir)
            self._persist()

    def _record_command(self, signature: str, decision: ApprovalDecision) -> None:
        if decision == ApprovalDecision.ALLOW_ONCE:
            self.session_allowed_commands.add(signature)
        elif decision == ApprovalDecision.ALLOW_ALWAYS:
            self.allowed_command_patterns.add(signature)
            self._persist()
        elif decision == ApprovalDecision.DENY_ONCE:
            self.session_denied_commands.add(signature)
        elif decision == ApprovalDecision.DENY_ALWAYS:
            self.denied_command_patterns.add(signature)
            self._persist()

    def _record_edit(self, target_path: str, decision: ApprovalDecision) -> None:
        normalized = _normalize_path(target_path)
        if decision == ApprovalDecision.ALLOW_ONCE:
            self.session_allowed_edits.add(normalized)
        elif decision == ApprovalDecision.ALLOW_TURN:
            self.turn_allowed_edits.add(normalized)
        elif decision == ApprovalDecision.ALLOW_ALL_TURN:
            self.turn_allow_all_edits = True
        elif decision == ApprovalDecision.ALLOW_ALWAYS:
            self.allowed_edit_patterns.add(normalized)
            self._persist()
        elif decision == ApprovalDecision.DENY_ONCE:
            self.session_denied_edits.add(normalized)
        elif decision == ApprovalDecision.DENY_ALWAYS:
            self.denied_edit_patterns.add(normalized)
            self._persist()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        store = _read_permission_store()
        self.allowed_directory_prefixes |= {_normalize_path(item) for item in store.get("allowedDirectoryPrefixes", [])}
        self.denied_directory_prefixes |= {_normalize_path(item) for item in store.get("deniedDirectoryPrefixes", [])}
        self.allowed_command_patterns |= set(store.get("allowedCommandPatterns", []))
        self.denied_command_patterns |= set(store.get("deniedCommandPatterns", []))
        self.allowed_edit_patterns |= {_normalize_path(item) for item in store.get("allowedEditPatterns", [])}
        self.denied_edit_patterns |= {_normalize_path(item) for item in store.get("deniedEditPatterns", [])}

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

    # ------------------------------------------------------------------ #
    # Summary (for system prompt / UI)
    # ------------------------------------------------------------------ #

    def get_summary_lines(self) -> list[str]:
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

        return [
            "extra allowed dirs: " + _preview(self.allowed_directory_prefixes, 3),
            "dangerous allowlist: " + _preview(self.allowed_command_patterns, 2, max_chars=48),
        ] + (["trusted edit targets: " + _preview(self.allowed_edit_patterns, 2)] if self.allowed_edit_patterns else [])


# ---------------------------------------------------------------------- #
# Module-level JSON I/O (kept here so the store owns its own format)
# ---------------------------------------------------------------------- #


def _read_permission_store() -> dict[str, Any]:
    if not PEPSI_CODE_PERMISSIONS_PATH.exists():
        return {}
    try:
        data = json.loads(PEPSI_CODE_PERMISSIONS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        warnings.warn(f"Corrupted permissions file, resetting: {e}")
        return {}


def _write_permission_store(store: dict[str, Any]) -> None:
    """Persist the permission store atomically to avoid race conditions."""
    PEPSI_CODE_PERMISSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=PEPSI_CODE_PERMISSIONS_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, PEPSI_CODE_PERMISSIONS_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
