"""Context window management for LLM conversations.

Tracks token usage, estimates context window consumption, and provides
auto-compaction to prevent context overflow in long conversations.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from pepsicode.config import PEPSI_CODE_DIR
from pepsicode.context_artifacts import ARTIFACT_REFERENCE_PATTERN

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default context window sizes (tokens)
DEFAULT_CONTEXT_WINDOWS = {
    "claude-sonnet-4-20250514": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-haiku-3-20240307": 100_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "mimo-v2.5": 1_000_000,
    "default": 128_000,  # Fallback
}

# Auto-compaction threshold (95% of context window)
AUTOCOMPACT_THRESHOLD = 0.95

# Estimated tokens per character (rough average for English/Code)
CHARS_PER_TOKEN = 4.0

# Minimum messages to keep after compaction
MIN_MESSAGES_TO_KEEP = 10

# System prompt is always kept (counts as 1 message)
SYSTEM_PROMPT_RESERVED = 1
COMPACT_TARGET_RATIO = 0.70
MINIMUM_COMPACTION_REDUCTION_RATIO = 0.20
PROTECTED_RECENT_GROUPS = 3
MAX_COMPACTION_ATTEMPTS = 3
MAX_ROLLING_SUMMARY_CHARS = 6_000
MAX_ROLLING_ARTIFACT_IDS = 50


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

import re

_CJK_PATTERN = re.compile(r"[\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]")


def estimate_tokens(text: str) -> int:
    """Improved token estimate with mixed Chinese/English support.

    - English/code: roughly 4 chars per token
    - Chinese/Japanese: roughly 1.5 chars per token
    - Mixed text: estimated using a split between the two

    Performance: a compiled regex counts CJK characters instead of
    iterating with ord() per character, which is 10-50x faster.
    """
    if not text:
        return 0

    # CJK char width estimation
    cjk_count = len(_CJK_PATTERN.findall(text))

    # CJK chars: ~1.5 chars/token; English: ~4 chars/token
    ascii_chars = len(text) - cjk_count

    return max(1, int(cjk_count / 1.5 + ascii_chars / 4.0))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate tokens for a single message."""
    tokens = 0

    # Role overhead
    role = message.get("role", "")
    if role == "system":
        tokens += 3  # System prompt overhead
    elif role == "user":
        tokens += 4  # User message overhead
    elif role == "assistant":
        tokens += 3  # Assistant overhead
    elif role == "assistant_tool_call":
        tokens += 7  # Tool call overhead
    elif role == "tool_result":
        tokens += 6  # Tool result overhead
    elif role == "assistant_progress":
        tokens += 3
    elif role == "assistant_thinking":
        tokens += 3
        for block in message.get("blocks", []):
            if isinstance(block, dict):
                tokens += estimate_tokens(block.get("thinking", ""))

    # Content tokens
    content = message.get("content", "")
    if isinstance(content, str):
        tokens += estimate_tokens(content)

    # Tool call input/output
    if "input" in message:
        input_str = json.dumps(message["input"]) if isinstance(message["input"], dict) else str(message["input"])
        tokens += estimate_tokens(input_str)

    return tokens


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens for a list of messages."""
    return sum(estimate_message_tokens(msg) for msg in messages)


# ---------------------------------------------------------------------------
# Summarization helpers (context-compression layers 2/3)
# ---------------------------------------------------------------------------

# Matches file paths like src/auth.py, tests\test_x.py, package/module.ts
_PATH_PATTERN = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,5}\b")
_ERROR_PATTERN = re.compile(r"(error|exception|failed|traceback|denied)", re.IGNORECASE)


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str) and content:
        return content
    if "input" in message:
        return json.dumps(message.get("input"))
    return content if isinstance(content, str) else ""


def _render_messages_for_summary(messages: list[dict[str, Any]]) -> str:
    """Flatten messages into a plain-text transcript for a summarizer prompt."""
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "")
        if role == "assistant_tool_call":
            parts.append(f"[tool_call {m.get('toolName')}] {json.dumps(m.get('input'))[:500]}")
        elif role == "tool_result":
            parts.append(f"[tool_result {m.get('toolName')}] {str(m.get('content', ''))[:500]}")
        else:
            parts.append(f"[{role}] {_message_text(m)[:1000]}")
    return "\n".join(parts)


def _heuristic_summary(messages: list[dict[str, Any]]) -> str:
    """Regex fallback: extract file paths, tools used, and error lines."""
    files: list[str] = []
    tools_used: list[str] = []
    errors: list[str] = []
    seen_files: set[str] = set()
    for m in messages:
        role = m.get("role", "")
        if role == "assistant_tool_call":
            name = m.get("toolName")
            if name and name not in tools_used:
                tools_used.append(name)
        text = _message_text(m)
        for path in _PATH_PATTERN.findall(text):
            if path not in seen_files:
                seen_files.add(path)
                files.append(path)
        for line in text.splitlines():
            if _ERROR_PATTERN.search(line) and len(errors) < 5:
                errors.append(line.strip()[:160])
    lines: list[str] = []
    if files:
        lines.append("- Files referenced: " + ", ".join(files[:15]))
    if tools_used:
        lines.append("- Tools used: " + ", ".join(tools_used[:15]))
    if errors:
        lines.append("- Notable errors/warnings:")
        lines.extend(f"  - {e}" for e in errors)
    return "\n".join(lines)


def _merge_rolling_summary(previous: str, current: str) -> str:
    combined = "\n\n".join(part.strip() for part in (previous, current) if part.strip())
    if len(combined) <= MAX_ROLLING_SUMMARY_CHARS:
        return combined
    tail_size = MAX_ROLLING_SUMMARY_CHARS // 4
    head_size = MAX_ROLLING_SUMMARY_CHARS - tail_size - 48
    return combined[:head_size] + "\n\n[older rolling summary truncated]\n\n" + combined[-tail_size:]


# ---------------------------------------------------------------------------
# Context tracking
# ---------------------------------------------------------------------------


@dataclass
class ContextStats:
    """Current context window statistics."""

    total_tokens: int = 0
    context_window: int = 0
    usage_percentage: float = 0.0
    messages_count: int = 0
    system_tokens: int = 0
    conversation_tokens: int = 0
    tool_calls_count: int = 0
    is_near_limit: bool = False
    should_compact: bool = False


@dataclass(slots=True)
class RecentFile:
    path: str
    operation: str
    content_hash: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "operation": self.operation,
            "content_hash": self.content_hash,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecentFile:
        return cls(
            path=str(data.get("path", "")),
            operation=str(data.get("operation", "read")),
            content_hash=data.get("content_hash"),
            reason=str(data.get("reason", "")),
        )


@dataclass(slots=True)
class RecoveryState:
    recent_files: list[RecentFile] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)
    active_plan_path: str | None = None
    permission_mode: str = "default"
    current_tasks: list[str] = field(default_factory=list)
    last_verification: str | None = None
    workspace: str = ""

    def remember_file(self, path: str, operation: str, reason: str = "") -> None:
        candidate = path.strip()[:500]
        if not candidate:
            return
        self.recent_files = [item for item in self.recent_files if item.path != candidate]
        self.recent_files.append(RecentFile(path=candidate, operation=operation, reason=reason))
        self.recent_files = self.recent_files[-12:]

    def render(self) -> str:
        lines = ["Recovered working state:"]
        if self.workspace:
            lines.append(f"- Workspace: {self.workspace}")
        lines.append(f"- Permission mode: {self.permission_mode}")
        if self.active_plan_path:
            lines.append(f"- Active plan: {self.active_plan_path}")
        if self.active_skills:
            lines.append("- Active skills: " + ", ".join(item[:100] for item in self.active_skills[-8:]))
        if self.recent_files:
            lines.append("- Recent files:")
            lines.extend(f"  - {item.path[:500]} ({item.operation[:40]})" for item in self.recent_files[-8:])
        if self.current_tasks:
            lines.append("- Current tasks:")
            lines.extend(f"  - {task[:500]}" for task in self.current_tasks[-8:])
        if self.last_verification:
            lines.append(f"- Last verification: {self.last_verification[:500]}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recent_files": [item.to_dict() for item in self.recent_files],
            "active_skills": list(self.active_skills),
            "active_plan_path": self.active_plan_path,
            "permission_mode": self.permission_mode,
            "current_tasks": list(self.current_tasks),
            "last_verification": self.last_verification,
            "workspace": self.workspace,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RecoveryState:
        data = data or {}
        return cls(
            recent_files=[
                RecentFile.from_dict(item) for item in data.get("recent_files", []) if isinstance(item, dict)
            ],
            active_skills=[str(item) for item in data.get("active_skills", [])],
            active_plan_path=data.get("active_plan_path"),
            permission_mode=str(data.get("permission_mode", "default")),
            current_tasks=[str(item) for item in data.get("current_tasks", [])],
            last_verification=data.get("last_verification"),
            workspace=str(data.get("workspace", "")),
        )


@dataclass(slots=True)
class CompactBoundary:
    version: int
    summary: str
    compacted_message_count: int
    before_tokens: int
    after_tokens: int
    protected_tail_count: int
    artifact_ids: list[str]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "summary": self.summary,
            "compacted_message_count": self.compacted_message_count,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "protected_tail_count": self.protected_tail_count,
            "artifact_ids": list(self.artifact_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompactBoundary:
        return cls(
            version=int(data.get("version", 1)),
            summary=str(data.get("summary", "")),
            compacted_message_count=int(data.get("compacted_message_count", 0)),
            before_tokens=int(data.get("before_tokens", 0)),
            after_tokens=int(data.get("after_tokens", 0)),
            protected_tail_count=int(data.get("protected_tail_count", 0)),
            artifact_ids=[str(item) for item in data.get("artifact_ids", [])],
            created_at=float(data.get("created_at", 0.0)),
        )


@dataclass(slots=True)
class CompactCircuitBreaker:
    attempts: int = 0
    last_fingerprint: str = ""
    last_before_tokens: int = 0
    last_after_tokens: int = 0
    max_attempts: int = MAX_COMPACTION_ATTEMPTS
    minimum_reduction_ratio: float = MINIMUM_COMPACTION_REDUCTION_RATIO
    blocked_reason: str = ""

    def can_attempt(self, fingerprint: str) -> bool:
        if self.attempts >= self.max_attempts:
            self.blocked_reason = "maximum consecutive compaction attempts reached"
            return False
        if self.last_fingerprint == fingerprint and self.attempts > 0:
            self.blocked_reason = "the context is unchanged since the previous compaction attempt"
            return False
        self.blocked_reason = ""
        return True

    def record(self, *, fingerprint: str, before_tokens: int, after_tokens: int, accepted: bool) -> None:
        self.last_fingerprint = fingerprint
        self.last_before_tokens = before_tokens
        self.last_after_tokens = after_tokens
        self.attempts = 0 if accepted else self.attempts + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "last_fingerprint": self.last_fingerprint,
            "last_before_tokens": self.last_before_tokens,
            "last_after_tokens": self.last_after_tokens,
            "max_attempts": self.max_attempts,
            "minimum_reduction_ratio": self.minimum_reduction_ratio,
            "blocked_reason": self.blocked_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CompactCircuitBreaker:
        data = data or {}
        return cls(
            attempts=int(data.get("attempts", 0)),
            last_fingerprint=str(data.get("last_fingerprint", "")),
            last_before_tokens=int(data.get("last_before_tokens", 0)),
            last_after_tokens=int(data.get("last_after_tokens", 0)),
            max_attempts=int(data.get("max_attempts", MAX_COMPACTION_ATTEMPTS)),
            minimum_reduction_ratio=float(data.get("minimum_reduction_ratio", MINIMUM_COMPACTION_REDUCTION_RATIO)),
            blocked_reason=str(data.get("blocked_reason", "")),
        )


def context_fingerprint(messages: list[dict[str, Any]]) -> str:
    payload = json.dumps(messages, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _group_atomic_messages(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group multi-tool batches and their thinking/progress prefix atomically."""
    groups: list[list[dict[str, Any]]] = []
    prefix: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role in {"assistant_thinking", "assistant_progress"}:
            prefix.append(message)
            index += 1
            continue
        if role == "assistant_tool_call":
            batch = list(prefix)
            prefix.clear()
            call_ids: set[str] = set()
            while index < len(messages) and messages[index].get("role") == "assistant_tool_call":
                call = messages[index]
                batch.append(call)
                if call.get("toolUseId"):
                    call_ids.add(str(call.get("toolUseId")))
                index += 1
            while index < len(messages) and messages[index].get("role") == "tool_result":
                result = messages[index]
                if call_ids and str(result.get("toolUseId")) not in call_ids:
                    break
                batch.append(result)
                index += 1
            groups.append(batch)
            continue
        group = list(prefix)
        prefix.clear()
        group.append(message)
        groups.append(group)
        index += 1
    if prefix:
        groups.append(prefix)
    return groups


def validate_tool_pairs(messages: list[dict[str, Any]]) -> list[str]:
    call_ids = {str(m.get("toolUseId")) for m in messages if m.get("role") == "assistant_tool_call"}
    result_ids = {str(m.get("toolUseId")) for m in messages if m.get("role") == "tool_result"}
    issues = [f"tool call without result: {item}" for item in sorted(call_ids - result_ids)]
    issues.extend(f"tool result without call: {item}" for item in sorted(result_ids - call_ids))
    return issues


@dataclass
class ContextManager:
    """Manages context window tracking and auto-compaction."""

    model: str = "default"
    context_window: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    compaction_history: list[dict[str, Any]] = field(default_factory=list)
    compact_boundaries: list[CompactBoundary] = field(default_factory=list)
    recovery_state: RecoveryState = field(default_factory=RecoveryState)
    circuit_breaker: CompactCircuitBreaker = field(default_factory=CompactCircuitBreaker)
    protected_recent_groups: int = PROTECTED_RECENT_GROUPS
    last_compaction_changed: bool = False
    last_compaction_error: str = ""
    # Real token counts reported by the provider's API (preferred over the
    # char-heuristic estimate when available).
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0
    # Optional callback used by compact_messages to LLM-summarize old turns.
    # Signature: (messages_to_summarize) -> summary_text.  None -> drop-only.
    summarizer: Any = None

    def __post_init__(self):
        if self.context_window == 0:
            self.context_window = DEFAULT_CONTEXT_WINDOWS.get(self.model, DEFAULT_CONTEXT_WINDOWS["default"])

    def update_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record real token usage reported by the API for the latest call."""
        if input_tokens > 0:
            self.actual_input_tokens = input_tokens
        if output_tokens > 0:
            self.actual_output_tokens = output_tokens

    def _summarize_dropped(self, dropped: list[dict[str, Any]]) -> str:
        """Summarize dropped messages, preserving high-value, long-lived info.

        Uses the optional ``summarizer`` callback (an LLM call) when available;
        otherwise falls back to a regex/heuristic extraction so compaction
        still keeps file paths, errors, and decisions even with no model.
        """
        # Cap the input so the summarizer prompt itself stays bounded.
        text = _render_messages_for_summary(dropped)[:15000]
        if self.summarizer is not None:
            try:
                summary = self.summarizer(text)
                if isinstance(summary, str) and summary.strip():
                    return summary.strip()[:MAX_ROLLING_SUMMARY_CHARS]
            except Exception:  # noqa: BLE001 - never let summarization break compaction
                pass
        return _heuristic_summary(dropped)

    def update_model(self, model: str) -> None:
        """Update model and adjust context window."""
        self.model = model
        self.context_window = DEFAULT_CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOWS["default"])

    def add_message(self, message: dict[str, Any]) -> None:
        """Add a message and update tracking."""
        self.messages.append(message)

    def update_runtime_state(self, workspace: str, permissions: Any | None = None) -> None:
        """Capture the small amount of runtime state needed after compaction."""
        self.recovery_state.workspace = workspace
        if permissions is None:
            return
        mode = getattr(permissions, "mode", None)
        self.recovery_state.permission_mode = str(getattr(mode, "value", mode or "default"))
        self.recovery_state.active_plan_path = (
            getattr(permissions, "plan_file_path", None) if bool(getattr(permissions, "is_plan_mode", False)) else None
        )

    def observe_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        output: str,
        ok: bool,
    ) -> None:
        """Update durable recovery hints from a completed tool call."""
        if tool_name in {
            "read_file",
            "write_file",
            "edit_file",
            "apply_patch",
            "grep",
            "glob",
        }:
            path = arguments.get("path") or arguments.get("file_path")
            if isinstance(path, str):
                operation = "error" if not ok else tool_name.removesuffix("_file")
                self.recovery_state.remember_file(path, operation)
        elif tool_name == "load_skill" and ok:
            name = arguments.get("name")
            if isinstance(name, str) and name.strip():
                skills = [item for item in self.recovery_state.active_skills if item != name.strip()]
                skills.append(name.strip()[:100])
                self.recovery_state.active_skills = skills[-8:]
        elif tool_name == "todo_write" and ok:
            todos = arguments.get("todos")
            if isinstance(todos, list):
                self.recovery_state.current_tasks = [
                    str(item.get("content"))[:500]
                    for item in todos
                    if isinstance(item, dict) and item.get("status", "pending") != "completed" and item.get("content")
                ][-8:]
        elif tool_name in {"test_runner", "run_with_debug"}:
            status = "passed" if ok else "failed"
            first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
            self.recovery_state.last_verification = f"{tool_name} {status}: {first_line}"[:500]

    def get_stats(self) -> ContextStats:
        """Calculate current context statistics."""
        if not self.messages:
            return ContextStats(
                context_window=self.context_window,
            )

        # Count tokens
        system_tokens = 0
        conversation_tokens = 0
        tool_calls = 0

        for msg in self.messages:
            msg_tokens = estimate_message_tokens(msg)
            if msg.get("role") == "system":
                system_tokens += msg_tokens
            else:
                conversation_tokens += msg_tokens

            if msg.get("role") == "assistant_tool_call":
                tool_calls += 1

        total_tokens = system_tokens + conversation_tokens
        # Prefer the provider's real input-token count when we have one - it
        # reflects exactly what the model saw last call, including tool schemas
        # the heuristic ignores.
        if self.actual_input_tokens > 0:
            total_tokens = max(total_tokens, self.actual_input_tokens)
        usage_pct = (total_tokens / self.context_window * 100) if self.context_window > 0 else 0

        is_near_limit = usage_pct >= 80  # Warning at 80%
        should_compact = usage_pct >= (AUTOCOMPACT_THRESHOLD * 100)

        return ContextStats(
            total_tokens=total_tokens,
            context_window=self.context_window,
            usage_percentage=usage_pct,
            messages_count=len(self.messages),
            system_tokens=system_tokens,
            conversation_tokens=conversation_tokens,
            tool_calls_count=tool_calls,
            is_near_limit=is_near_limit,
            should_compact=should_compact,
        )

    def should_auto_compact(self) -> bool:
        """Check if auto-compaction should trigger."""
        stats = self.get_stats()
        return stats.should_compact

    def compact_messages(self, force: bool = False) -> list[dict[str, Any]]:
        """Compact old atomic turns while protecting recent working state."""
        self.last_compaction_changed = False
        self.last_compaction_error = ""
        stats = self.get_stats()
        if not force and not stats.should_compact:
            return self.messages

        fingerprint = context_fingerprint(self.messages)
        before_estimated = estimate_messages_tokens(self.messages)
        if not self.circuit_breaker.can_attempt(fingerprint):
            self.last_compaction_error = self.circuit_breaker.blocked_reason
            return self.messages

        target_tokens = int(self.context_window * COMPACT_TARGET_RATIO)
        system_messages = [m for m in self.messages if m.get("role") == "system" and not m.get("_compaction_marker")]
        prior_markers = [m for m in self.messages if m.get("role") == "system" and m.get("_compaction_marker")]
        other_messages = [m for m in self.messages if m.get("role") != "system"]
        retained_markers: list[dict[str, Any]] = []
        groups = _group_atomic_messages(other_messages)
        protected_group_count = min(max(0, self.protected_recent_groups), len(groups))
        droppable_count = len(groups) - protected_group_count
        dropped: list[dict[str, Any]] = []
        while droppable_count > 0:
            retained = [message for group in groups for message in group]
            candidate = system_messages + retained_markers + retained
            if estimate_messages_tokens(candidate) <= target_tokens:
                break
            first_group = groups[0]
            if len(retained) - len(first_group) < MIN_MESSAGES_TO_KEEP:
                break
            dropped.extend(groups.pop(0))
            droppable_count -= 1

        filtered = [message for group in groups for message in group]
        call_ids = {str(m.get("toolUseId")) for m in filtered if m.get("role") == "assistant_tool_call"}
        result_ids = {str(m.get("toolUseId")) for m in filtered if m.get("role") == "tool_result"}
        repaired: list[dict[str, Any]] = []
        for message in filtered:
            role = message.get("role")
            tool_use_id = str(message.get("toolUseId"))
            if (
                role == "tool_result"
                and tool_use_id not in call_ids
                or role == "assistant_tool_call"
                and tool_use_id not in result_ids
            ):
                dropped.append(message)
            else:
                repaired.append(message)
        filtered = repaired

        if not dropped:
            self.last_compaction_error = "no eligible atomic message groups could be removed"
            self.circuit_breaker.record(
                fingerprint=fingerprint,
                before_tokens=before_estimated,
                after_tokens=before_estimated,
                accepted=False,
            )
            return self.messages

        current_summary = self._summarize_dropped(dropped)
        previous_summary = self.compact_boundaries[-1].summary if self.compact_boundaries else ""
        if not previous_summary and prior_markers:
            previous_summary = _message_text(prior_markers[-1])[: MAX_ROLLING_SUMMARY_CHARS // 2]
        summary_text = _merge_rolling_summary(previous_summary, current_summary)
        current_artifact_ids = {
            artifact_id
            for message in dropped
            for artifact_id in ARTIFACT_REFERENCE_PATTERN.findall(_message_text(message))
        }
        previous_artifact_ids = set(self.compact_boundaries[-1].artifact_ids) if self.compact_boundaries else set()
        artifact_ids = sorted(previous_artifact_ids | current_artifact_ids)[-MAX_ROLLING_ARTIFACT_IDS:]
        protected_tail_count = sum(len(group) for group in groups[-protected_group_count:])
        marker_body = (
            f"[Context compacted at {time.strftime('%H:%M:%S')}. "
            f"Previous {len(dropped)} messages summarized. "
            f"Token usage reduced from {stats.usage_percentage:.0f}% to "
            f"{estimate_messages_tokens(filtered) / self.context_window * 100:.0f}%]"
        )
        if summary_text:
            marker_body += "\n\nSummary of earlier work:\n" + summary_text
        marker_body += "\n\n" + self.recovery_state.render()
        if artifact_ids:
            marker_body += "\n- Recoverable context artifacts: " + ", ".join(artifact_ids)
        compaction_marker = {"role": "system", "content": marker_body, "_compaction_marker": True}
        compacted = system_messages + retained_markers + [compaction_marker] + filtered
        pairing_issues = validate_tool_pairs(compacted)
        after_estimated = estimate_messages_tokens(compacted)
        reduction_ratio = (before_estimated - after_estimated) / max(1, before_estimated)
        if pairing_issues or reduction_ratio < self.circuit_breaker.minimum_reduction_ratio:
            reason = (
                "; ".join(pairing_issues)
                if pairing_issues
                else f"estimated reduction {reduction_ratio:.1%} is below the safety threshold"
            )
            self.last_compaction_error = reason
            self.circuit_breaker.record(
                fingerprint=fingerprint,
                before_tokens=before_estimated,
                after_tokens=after_estimated,
                accepted=False,
            )
            return self.messages

        now = time.time()
        boundary = CompactBoundary(
            version=1,
            summary=summary_text,
            compacted_message_count=len(dropped),
            before_tokens=stats.total_tokens,
            after_tokens=after_estimated,
            protected_tail_count=protected_tail_count,
            artifact_ids=artifact_ids,
            created_at=now,
        )
        self.compact_boundaries.append(boundary)
        self.compact_boundaries = self.compact_boundaries[-10:]
        self.compaction_history.append(
            {
                "timestamp": now,
                "before_tokens": stats.total_tokens,
                "after_tokens": after_estimated,
                "messages_removed": max(0, stats.messages_count - len(compacted)),
            }
        )
        self.messages = compacted
        self.actual_input_tokens = 0
        self.last_compaction_changed = True
        self.circuit_breaker.record(
            fingerprint=fingerprint,
            before_tokens=before_estimated,
            after_tokens=after_estimated,
            accepted=True,
        )
        return compacted

    def get_context_summary(self) -> str:
        """Get a human-readable context usage summary."""
        stats = self.get_stats()

        if stats.messages_count == 0:
            return "Context: empty"

        status = "OK"
        if stats.is_near_limit:
            status = "WARN"
        if stats.should_compact:
            status = "FULL"

        return (
            f"Context: {status} {stats.usage_percentage:.0f}% "
            f"({stats.total_tokens:,}/{stats.context_window:,} tokens, "
            f"{stats.messages_count} msgs, {stats.tool_calls_count} tools)"
        )

    def format_context_details(self) -> str:
        """Get detailed context information for /context command."""
        stats = self.get_stats()

        lines = [
            "Context Window Usage",
            "=" * 50,
            f"Model: {self.model}",
            f"Context window: {stats.context_window:,} tokens",
            "",
            f"Total tokens: {stats.total_tokens:,}",
            f"Usage: {stats.usage_percentage:.1f}%",
            f"Messages: {stats.messages_count}",
            f"Tool calls: {stats.tool_calls_count}",
            "",
        ]

        if stats.should_compact:
            lines.append("WARNING: Context is near capacity!")
            lines.append("Auto-compaction will trigger soon.")
            lines.append("")

        if self.compaction_history:
            lines.append("Compaction History:")
            for comp in self.compaction_history[-3:]:  # Last 3
                ts = time.strftime("%H:%M:%S", time.localtime(comp["timestamp"]))
                lines.append(
                    f"  {ts}: {comp['messages_removed']} messages removed, "
                    f"{comp['before_tokens']:,} -> {comp['after_tokens']:,} tokens"
                )

        if self.compact_boundaries:
            latest = self.compact_boundaries[-1]
            lines.append("")
            lines.append(
                f"Latest boundary: {latest.compacted_message_count} messages summarized, "
                f"{latest.protected_tail_count} recent messages protected, "
                f"{len(latest.artifact_ids)} artifact reference(s)"
            )
        if self.last_compaction_error:
            lines.append(f"Last compaction skipped: {self.last_compaction_error}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_context_state(manager: ContextManager) -> None:
    """Save context manager state to disk."""
    state_path = PEPSI_CODE_DIR / "context_state.json"
    PEPSI_CODE_DIR.mkdir(parents=True, exist_ok=True)

    state = {
        "model": manager.model,
        "context_window": manager.context_window,
        "messages": manager.messages,
        "compaction_history": manager.compaction_history[-10:],  # Keep last 10
        "compact_boundaries": [item.to_dict() for item in manager.compact_boundaries[-10:]],
        "recovery_state": manager.recovery_state.to_dict(),
        "circuit_breaker": manager.circuit_breaker.to_dict(),
    }

    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_context_state() -> ContextManager | None:
    """Load context manager state from disk."""
    state_path = PEPSI_CODE_DIR / "context_state.json"
    if not state_path.exists():
        return None

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return ContextManager(
            model=state.get("model", "default"),
            context_window=state.get("context_window", 0),
            messages=state.get("messages", []),
            compaction_history=state.get("compaction_history", []),
            compact_boundaries=[
                CompactBoundary.from_dict(item)
                for item in state.get("compact_boundaries", [])
                if isinstance(item, dict)
            ],
            recovery_state=RecoveryState.from_dict(state.get("recovery_state")),
            circuit_breaker=CompactCircuitBreaker.from_dict(state.get("circuit_breaker")),
        )
    except (json.JSONDecodeError, KeyError):
        return None


def clear_context_state() -> None:
    """Clear saved context state."""
    state_path = PEPSI_CODE_DIR / "context_state.json"
    if state_path.exists():
        state_path.unlink()
