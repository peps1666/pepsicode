"""Context window management for LLM conversations.

Tracks token usage, estimates context window consumption, and provides
auto-compaction to prevent context overflow in long conversations.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pepsicode.config import PEPSI_CODE_DIR


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


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

import re
_CJK_PATTERN = re.compile(r'[\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]')


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


@dataclass
class ContextManager:
    """Manages context window tracking and auto-compaction."""
    model: str = "default"
    context_window: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    compaction_history: list[dict[str, Any]] = field(default_factory=list)
    # Real token counts reported by the provider's API (preferred over the
    # char-heuristic estimate when available).
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0
    # Optional callback used by compact_messages to LLM-summarize old turns.
    # Signature: (messages_to_summarize) -> summary_text.  None -> drop-only.
    summarizer: Any = None

    def __post_init__(self):
        if self.context_window == 0:
            self.context_window = DEFAULT_CONTEXT_WINDOWS.get(
                self.model, DEFAULT_CONTEXT_WINDOWS["default"]
            )

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
                    return summary.strip()
            except Exception:  # noqa: BLE001 - never let summarization break compaction
                pass
        return _heuristic_summary(dropped)

    
    def update_model(self, model: str) -> None:
        """Update model and adjust context window."""
        self.model = model
        self.context_window = DEFAULT_CONTEXT_WINDOWS.get(
            model, DEFAULT_CONTEXT_WINDOWS["default"]
        )
    
    def add_message(self, message: dict[str, Any]) -> None:
        """Add a message and update tracking."""
        self.messages.append(message)
    
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
        """Compact messages to fit within context window.

        Strategy:
        1. Keep system prompt (always)
        2. Keep recent messages (last N)
        3. Summarize/condense older tool calls
        4. Remove old assistant progress messages

        When ``force`` is True the compaction runs even if the usage threshold
        has not been crossed (used to recover from an API context-overflow).
        """
        stats = self.get_stats()
        if not force and not stats.should_compact:
            return self.messages
        
        # Calculate target: reduce to ~70% of context window
        target_tokens = int(self.context_window * 0.70)
        
        # Always keep system prompt.  Exclude prior compaction markers from the
        # real-system set so empty/no-op markers cannot accumulate; genuine
        # markers (which carry summaries) are re-added below only when work is
        # done.
        system_messages = [
            m for m in self.messages
            if m.get("role") == "system" and not m.get("_compaction_marker")
        ]
        prior_markers = [
            m for m in self.messages
            if m.get("role") == "system" and m.get("_compaction_marker")
        ]
        other_messages = [m for m in self.messages if m.get("role") != "system"]

        # Remove old progress messages first
        filtered = [
            m for m in other_messages
            if m.get("role") != "assistant_progress"
        ]
        progress_removed = len(other_messages) - len(filtered)

        # If still too large, drop oldest messages one at a time, recording
        # what we drop so we can summarize it.  Prefer dropping tool-call/
        # tool-result pairs first, then plain assistant/user messages.  Always
        # keep the most recent messages.
        dropped: list[dict[str, Any]] = []
        while estimate_messages_tokens(filtered) > target_tokens and len(filtered) > MIN_MESSAGES_TO_KEEP:
            removed = False
            for i in range(len(filtered) - MIN_MESSAGES_TO_KEEP):
                role = filtered[i].get("role")
                # Drop tool-call + its result as a pair
                if role == "assistant_tool_call":
                    if (i + 1 < len(filtered) and
                            filtered[i + 1].get("role") == "tool_result"):
                        dropped.extend(filtered[i:i + 2])
                        del filtered[i:i + 2]
                    else:
                        dropped.append(filtered[i])
                        del filtered[i]
                    removed = True
                    break
                # Drop standalone tool_result (orphaned)
                if role == "tool_result":
                    dropped.append(filtered[i])
                    del filtered[i]
                    removed = True
                    break
                # Drop plain user/assistant messages
                if role in ("user", "assistant"):
                    dropped.append(filtered[i])
                    del filtered[i]
                    removed = True
                    break

            if not removed:
                break

        # Repair tool_use/tool_result pairing.  Positional dropping above can
        # leave a tool_result whose assistant_tool_call was dropped (or vice
        # versa); Anthropic rejects such orphans with a 400.  Pair by id.
        call_ids = {
            m.get("toolUseId") for m in filtered if m.get("role") == "assistant_tool_call"
        }
        result_ids = {
            m.get("toolUseId") for m in filtered if m.get("role") == "tool_result"
        }
        repaired = []
        for m in filtered:
            role = m.get("role")
            if role == "tool_result" and m.get("toolUseId") not in call_ids:
                dropped.append(m)
                continue
            if role == "assistant_tool_call" and m.get("toolUseId") not in result_ids:
                dropped.append(m)
                continue
            repaired.append(m)
        filtered = repaired

        # If nothing actually changed, return unchanged rather than prepending an
        # empty marker (which would grow the payload and accumulate over forced
        # retries).
        if not dropped and progress_removed == 0:
            return self.messages

        # Summarize what we dropped (LLM if a summarizer is set, else regex).
        # This preserves file paths, decisions, and unresolved errors that pure
        # truncation would lose (context-compression layers 2/3).
        summary_text = self._summarize_dropped(dropped) if dropped else ""
        marker_body = (
            f"[Context compacted at {time.strftime('%H:%M:%S')}. "
            f"Previous {len(dropped)} messages summarized. "
            f"Token usage reduced from {stats.usage_percentage:.0f}% to "
            f"{estimate_messages_tokens(filtered) / self.context_window * 100:.0f}%]"
        )
        if summary_text:
            marker_body += "\n\nSummary of earlier work:\n" + summary_text
        compaction_marker = {"role": "system", "content": marker_body, "_compaction_marker": True}

        # Build final message list: real system prompt(s), then prior summaries,
        # then this compaction's marker, then the surviving recent messages.
        compacted = system_messages + prior_markers + [compaction_marker] + filtered

        # Record compaction
        self.compaction_history.append({
            "timestamp": time.time(),
            "before_tokens": stats.total_tokens,
            "after_tokens": estimate_messages_tokens(compacted),
            "messages_removed": max(0, stats.messages_count - len(compacted)),
        })

        self.messages = compacted
        # The stale API token count described the pre-compaction payload; clear
        # it so stats fall back to a fresh estimate until the next real usage.
        self.actual_input_tokens = 0
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
        )
    except (json.JSONDecodeError, KeyError):
        return None


def clear_context_state() -> None:
    """Clear saved context state."""
    state_path = PEPSI_CODE_DIR / "context_state.json"
    if state_path.exists():
        state_path.unlink()
