"""pepsicode Python TTY Application.

This module implements the full-screen terminal user interface for pepsicode,
including:
- Real-time transcript rendering with tool output collapsing
- Interactive permission approval prompts
- Background agent thread management
- Keyboard event handling and command routing
- Session persistence and autosave
"""

from __future__ import annotations

import logging
import os
import random
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from pepsicode.agent_loop import run_agent_turn, run_agent_turn_stream
from pepsicode.background_tasks import list_background_tasks
from pepsicode.cli_commands import (
    SLASH_COMMANDS,
    find_matching_slash_commands,
    try_handle_local_command,
)
from pepsicode.cost_tracker import CostTracker
from pepsicode.history import load_history_entries, save_history_entries
from pepsicode.local_tool_shortcuts import parse_local_tool_shortcut
from pepsicode.permissions import PermissionManager
from pepsicode.prompt import build_system_prompt
from pepsicode.session import (
    AutosaveManager,
    SessionData,
    SessionMetadata,
    create_new_session,
    delete_session,
    format_session_list,
    format_session_resume,
    get_latest_session,
    list_sessions,
    load_session,
    save_session,
)
from pepsicode.state import AppState, Store, create_app_store, format_app_state_summary
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tui.chrome import (
    _cached_terminal_size,
    get_permission_prompt_max_scroll_offset,
    render_banner,
    render_footer_bar,
    render_panel,
    render_permission_prompt,
    render_slash_menu,
    render_status_line,
    render_tool_panel,
    truncate_plain,
    wrap_text,
    ACCENT,
    BRIGHT_GREEN,
    BRIGHT_WHITE,
    BOLD,
    DIM,
    GREEN,
    HIGHLIGHT_BG,
    ICON_PROMPT,
    ICON_DOT,
    ICON_DIVIDER,
    ICON_ARROW,
    YELLOW,
    SUBTLE,
    RESET,
)
from pepsicode.tui.input import render_input_prompt
from pepsicode.tui.input_parser import (
    KeyEvent,
    ParsedInputEvent,
    TextEvent,
    WheelEvent,
    parse_input_chunk,
)
from pepsicode.tui.markdown import render_markdownish
from pepsicode.tui.screen import (
    clear_screen,
    enter_alternate_screen,
    exit_alternate_screen,
    hide_cursor,
    show_cursor,
)
from pepsicode.tui.transcript import (
    _render_transcript_lines,
    get_transcript_window_size,
    render_transcript,
)
from pepsicode.tui.types import TranscriptEntry
from pepsicode.types import ChatMessage, ModelAdapter, StreamToken
from pepsicode.workspace import resolve_tool_path

# ---------------------------------------------------------------------------
# Terminal size -- use unified cache from chrome module
# ---------------------------------------------------------------------------

# Alias to the single canonical implementation in chrome.py
_get_terminal_size = _cached_terminal_size


# ---------------------------------------------------------------------------
# Throttled renderer
# ---------------------------------------------------------------------------

class _ThrottledRenderer:
    """Coalesces rapid rerender() calls into at most one actual render per interval.

    THREAD SAFETY: The actual render function (_render_fn) is ONLY executed on
    the thread that calls ``flush()`` or ``force()``.  ``request()`` never
    invokes the render function directly -- it only marks a pending flag.  This
    ensures that background threads (agent, collapse timer) can safely call
    ``request()`` without writing to stdout concurrently with the main UI
    thread.
    """

    __slots__ = ("_render_fn", "_min_interval", "_pending", "_last_render_time", "_lock")

    def __init__(self, render_fn: Callable[[], None], min_interval: float = 0.033) -> None:
        self._render_fn = render_fn
        self._min_interval = min_interval  # ~30 fps cap (sufficient for terminal UI)
        self._pending = False
        self._last_render_time: float = 0.0
        self._lock = threading.Lock()

    def request(self) -> None:
        """Mark that a rerender is needed.

        This method is safe to call from any thread.  It never invokes the
        render function -- the actual render happens on the next ``flush()``
        call from the main event loop.
        """
        with self._lock:
            self._pending = True

    def flush(self) -> None:
        """Execute a pending render if the throttle interval has elapsed.

        Must be called from the main UI thread only.
        """
        now = time.monotonic()
        with self._lock:
            if not self._pending:
                return
            elapsed = now - self._last_render_time
            if elapsed < self._min_interval:
                return  # Still within throttle window -- defer
            self._pending = False
            self._last_render_time = now
        self._render_fn()

    def force(self) -> None:
        """Unconditionally render now, ignoring throttle.

        Must be called from the main UI thread only.
        """
        with self._lock:
            self._pending = False
            self._last_render_time = time.monotonic()
        self._render_fn()


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class TtyAppArgs:
    runtime: dict | None
    tools: ToolRegistry
    model: ModelAdapter
    messages: list[ChatMessage]
    cwd: str
    permissions: PermissionManager
    context_manager: Any | None = None


@dataclass
class PendingApproval:
    request: dict[str, Any]
    resolve: Callable[[dict[str, Any]], None]
    details_expanded: bool = False
    details_scroll_offset: int = 0
    selected_choice_index: int = 0
    feedback_mode: bool = False
    feedback_input: str = ""


@dataclass
class AggregatedEditProgress:
    entry_id: int
    tool_name: str
    path: str
    total: int = 1
    completed: int = 0
    errors: int = 0
    last_output: str = ""


@dataclass
class ScreenState:
    input: str = ""
    cursor_offset: int = 0
    transcript: list[TranscriptEntry] = field(default_factory=list)
    transcript_scroll_offset: int = 0
    transcript_total_lines: int = 0
    user_scrolled_away: bool = False
    _animation_frame: int = 0
    selected_slash_index: int = 0
    status: str | None = None
    active_tool: str | None = None
    recent_tools: list[dict[str, str]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    history_index: int = 0
    history_draft: str = ""
    next_entry_id: int = 1
    pending_approval: PendingApproval | None = None
    resume_picker_sessions: list[SessionMetadata] | None = None
    resume_picker_index: int = 0
    is_busy: bool = False
    # Session persistence
    session: SessionData | None = None
    autosave: AutosaveManager | None = None
    # State management (Zustand-style)
    app_state: Store[AppState] | None = None
    # Cost tracking
    cost_tracker: CostTracker | None = None
    # Background agent thread
    agent_thread: Any = None
    agent_result: dict | None = None
    agent_lock: Any = None
    # Tool execution timing tracker
    tool_start_time: float | None = None
    # Streaming state: the entry currently being streamed into (if any).
    streaming_entry_id: int | None = None
    streaming_text: str = ""


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _get_session_stats(args: TtyAppArgs, state: ScreenState) -> dict[str, int]:
    """Get current session statistics.
    
    Returns a dict with transcript, message, skill, and MCP server counts.
    """
    return {
        "transcriptCount": len(state.transcript),
        "messageCount": len(args.messages),
        "skillCount": len(args.tools.get_skills()),
        "mcpCount": len(args.tools.get_mcp_servers()),
    }


def _push_transcript_entry(state: ScreenState, **kwargs: Any) -> int:
    """Create and append a new transcript entry.
    
    Returns the unique entry ID for later updates.
    """
    entry_id = state.next_entry_id
    state.next_entry_id += 1
    state.transcript.append(TranscriptEntry(id=entry_id, **kwargs))
    return entry_id


def _session_rows_for_picker(cwd: str) -> list[SessionMetadata]:
    sessions = list_sessions()
    workspace = str(Path(cwd).resolve())
    current = [session for session in sessions if session.workspace == workspace]
    other = [session for session in sessions if session.workspace != workspace]
    return current + other


def _restore_session_into_state(
    args: TtyAppArgs,
    state: ScreenState,
    session: SessionData,
) -> None:
    args.messages = list(session.messages)
    state.session = session
    state.autosave = AutosaveManager(session)
    state.transcript = [TranscriptEntry(**entry) for entry in session.transcript_entries]
    state.next_entry_id = max((entry.id for entry in state.transcript), default=0) + 1
    state.transcript_scroll_offset = 0
    state.transcript_total_lines = 0
    state.user_scrolled_away = False
    state.resume_picker_sessions = None
    state.resume_picker_index = 0
    state.input = ""
    state.cursor_offset = 0
    state.status = f"Resumed session {session.session_id[:8]}"
    if state.app_state:
        def update_app_state(app_state: AppState) -> AppState:
            app_state.session_id = session.session_id
            app_state.workspace = session.workspace
            app_state.message_count = len(session.messages)
            app_state.update_timestamp()
            return app_state

        state.app_state.set_state(update_app_state)


def _open_resume_picker(args: TtyAppArgs, state: ScreenState) -> None:
    sessions = _session_rows_for_picker(args.cwd)
    if not sessions:
        state.status = "No saved sessions found."
        _push_transcript_entry(state, kind="assistant", body="No saved sessions found.")
        return
    state.resume_picker_sessions = sessions
    state.resume_picker_index = 0
    state.input = ""
    state.cursor_offset = 0
    state.status = "Select a session to resume"


def _resume_session_by_id(args: TtyAppArgs, state: ScreenState, session_id: str) -> bool:
    session = load_session(session_id)
    if not session:
        state.status = f"Session '{session_id}' not found."
        _push_transcript_entry(state, kind="assistant", body=f"Session '{session_id}' not found.")
        return False
    _restore_session_into_state(args, state, session)
    _push_transcript_entry(
        state,
        kind="assistant",
        body=f"Session {session.session_id[:8]} resumed ({len(session.messages)} messages loaded).",
    )
    return True


def _mark_running_tools_as_error(state: ScreenState, message: str) -> None:
    """Mark all currently running tools as failed with the given error message.
    
    This is used when a turn ends unexpectedly while tools are still running.
    """
    for entry in state.transcript:
        if entry.kind == "tool" and entry.status == "running":
            entry.status = "error"
            entry.body = message
            entry.collapsed = False
            entry.collapsedSummary = None
            entry.collapsePhase = None
            state.recent_tools.append({"name": entry.toolName or "unknown", "status": "error"})
    if any(e.kind == "tool" and e.status == "error" for e in state.transcript):
        state.active_tool = None


def _update_tool_entry(
    state: ScreenState,
    entry_id: int,
    status: str,
    body: str,
) -> None:
    """Update a tool entry's status and output body.
    
    Automatically un-collapses the entry so the new content is visible.
    """
    for entry in state.transcript:
        if entry.id == entry_id and entry.kind == "tool":
            entry.status = status
            entry.body = body
            entry.collapsed = False
            entry.collapsedSummary = None
            entry.collapsePhase = None
            return


def _set_tool_entry_collapse_phase(state: ScreenState, entry_id: int, phase: int) -> None:
    """Set the collapse animation phase for a tool entry."""
    for entry in state.transcript:
        if entry.id == entry_id and entry.kind == "tool" and entry.status != "running":
            entry.collapsePhase = phase
            return


def _collapse_tool_entry(state: ScreenState, entry_id: int, summary: str) -> None:
    """Collapse a tool entry to show only a summary line.
    
    Used for completed tools to reduce visual clutter in the transcript.
    """
    for entry in state.transcript:
        if entry.id == entry_id and entry.kind == "tool" and entry.status != "running":
            entry.collapsePhase = None
            entry.collapsed = True
            entry.collapsedSummary = summary
            return


def _get_running_tool_entries(state: ScreenState) -> list[TranscriptEntry]:
    """Get all transcript entries that are still in 'running' status."""
    return [e for e in state.transcript if e.kind == "tool" and e.status == "running"]


def _finalize_dangling_running_tools(state: ScreenState) -> None:
    """Mark all running tools as errors when a turn ends unexpectedly.
    
    This happens when the model stops responding but tools are still active,
    indicating a potential sync issue or background process.
    """
    running = _get_running_tool_entries(state)
    if running:
        error_message = (
            f"{running[0].body}\n\n"
            "ERROR: Tool did not report a final result before the turn ended. "
            "This usually means the command kept running in the background "
            "or the tool lifecycle got out of sync."
        )
        _mark_running_tools_as_error(state, error_message)
        state.status = f"Previous turn ended with {len(running)} unfinished tool call(s)."


def _summarize_collapsed_tool_body(output: str) -> str:
    line = next(
        (l.strip() for l in output.split("\n") if l.strip()),
        "output collapsed",
    )
    return line[:140] + "..." if len(line) > 140 else line


def _schedule_tool_auto_collapse(
    state: ScreenState,
    entry_id: int,
    output: str,
    rerender: Callable[[], None],
) -> None:
    """Collapse tool output with a brief animation. Optimized to use a single
    combined delay instead of 3 separate sleep+rerender cycles."""
    summary = _summarize_collapsed_tool_body(output)

    def _do_collapse() -> None:
        # Single delay then jump straight to collapsed state
        # (avoids 3 separate rerender() calls for an animation most users barely see)
        time.sleep(0.05)
        _collapse_tool_entry(state, entry_id, summary)
        rerender()

    t = threading.Thread(target=_do_collapse, daemon=True)
    t.start()


def _get_contextual_help(state: ScreenState, args: TtyAppArgs) -> str | None:
    """Return a contextual help hint relevant to the current screen state."""
    # Idle state - show a random quick tip
    if not state.is_busy and not state.pending_approval:
        tips = [
            "💡 Tip: Use /skills to see available workflows",
            "💡 Tip: Try 'analyze this project' to get started",
            "💡 Tip: Use Tab to autocomplete commands",
            "💡 Tip: Type /help for all commands",
            "💡 Tip: Use Ctrl+R to search history",
        ]
        return random.choice(tips)

    # Tool running - show a cancel hint
    if state.is_busy and state.active_tool:
        return f"⏳ Running {state.active_tool}... Press Ctrl+C to cancel"

    # Permission approval pending
    if state.pending_approval:
        return "🔒 Permission required. Use arrow keys and Enter to choose"
    
    return None


# ---------------------------------------------------------------------------
# Tool summarization
# ---------------------------------------------------------------------------


def _truncate_for_display(text: str, max_len: int = 180) -> str:
    return text[:max_len] + "..." if len(text) > max_len else text


def _summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, str):
        return _truncate_for_display(" ".join(tool_input.split()).strip())

    if isinstance(tool_input, dict):
        path = str(tool_input.get("path", "")).strip()
        path_part = f" path={path}" if path else ""

        if tool_name == "patch_file":
            replacements = tool_input.get("replacements")
            count = len(replacements) if isinstance(replacements, list) else 0
            return f"patch_file{path_part} replacements={count}"
        if tool_name == "edit_file":
            return f"edit_file{path_part}"
        if tool_name == "read_file":
            extras: list[str] = []
            if tool_input.get("offset") is not None:
                extras.append(f"offset={tool_input['offset']}")
            if tool_input.get("limit") is not None:
                extras.append(f"limit={tool_input['limit']}")
            return f"read_file{path_part}{' ' + ' '.join(extras) if extras else ''}"
        if tool_name == "run_command":
            cmd = str(tool_input.get("command", "")).strip()
            return f"run_command{' ' + _truncate_for_display(cmd, 120) if cmd else ''}"
        if path:
            return f"{tool_name}{path_part}"

    try:
        return _truncate_for_display(str(tool_input))
    except Exception:
        return _truncate_for_display(repr(tool_input))


def _is_file_edit_tool(tool_name: str) -> bool:
    return tool_name in ("edit_file", "patch_file", "modify_file", "write_file")


def _extract_path_from_tool_input(tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    value = tool_input.get("path")
    return value if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------------------
# Scroll / history / slash
# ---------------------------------------------------------------------------


_HEADER_LINES_ESTIMATE = 10  # banner panel: top/title/divider + 5 body lines + bottom
_PROMPT_LINES_ESTIMATE = 7   # prompt panel: top border + title + divider + 3 body + bottom border
_FOOTER_LINES = 2  # status line plus optional contextual help
_GAPS = 4
_TRANSCRIPT_FRAME_LINES = 4  # top/bottom border + title + empty

def _get_transcript_body_lines(args: TtyAppArgs, state: ScreenState) -> int:
    _, rows = _get_terminal_size()
    rows = max(12, rows)
    # Use cached estimates instead of re-rendering header/prompt just to count lines
    chrome_overhead = (
        _HEADER_LINES_ESTIMATE
        + _PROMPT_LINES_ESTIMATE
        + _FOOTER_LINES
        + _GAPS
        + _TRANSCRIPT_FRAME_LINES
    )
    return max(1, rows - chrome_overhead)


def _get_stream_max_scroll_offset(args: TtyAppArgs, state: ScreenState) -> int:
    """Compute max scroll offset using the stream renderer's actual line counts."""
    _, rows = _get_terminal_size()
    rows = max(10, rows)
    # Reserve space for input (2 lines), slash menu if visible, separator, footer
    input_height = 2
    visible_commands = _get_visible_commands(state.input)
    if visible_commands and state.input:
        input_height += len(visible_commands) + 3
    reserved = input_height + 3
    transcript_height = max(1, rows - reserved)
    return max(0, state.transcript_total_lines - transcript_height)


def _get_max_transcript_scroll_offset(args: TtyAppArgs, state: ScreenState) -> int:
    return get_transcript_max_scroll_offset(
        state.transcript, _get_transcript_body_lines(args, state)
    )


def _scroll_transcript_by(args: TtyAppArgs, state: ScreenState, delta: int) -> bool:
    if os.environ.get("pepsicode_LEGACY_TUI") == "1":
        max_offset = _get_max_transcript_scroll_offset(args, state)
    else:
        max_offset = _get_stream_max_scroll_offset(args, state)
    next_offset = max(0, min(max_offset, state.transcript_scroll_offset + delta))
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    state.user_scrolled_away = state.transcript_scroll_offset > 0
    return True


def _jump_transcript_to_edge(args: TtyAppArgs, state: ScreenState, target: str) -> bool:
    if os.environ.get("pepsicode_LEGACY_TUI") == "1":
        max_offset = _get_max_transcript_scroll_offset(args, state)
    else:
        max_offset = _get_stream_max_scroll_offset(args, state)
    next_offset = max_offset if target == "top" else 0
    if next_offset == state.transcript_scroll_offset:
        return False
    state.transcript_scroll_offset = next_offset
    state.user_scrolled_away = target == "top"
    return True


def _reset_scroll_if_needed(state: ScreenState) -> None:
    """Reset scroll to bottom only if user has not manually scrolled away."""
    if not state.user_scrolled_away:
        state.transcript_scroll_offset = 0


def _scroll_pending_approval_by(state: ScreenState, delta: int) -> bool:
    pending = state.pending_approval
    if not pending or not pending.details_expanded:
        return False
    max_offset = get_permission_prompt_max_scroll_offset(pending.request, expanded=True)
    next_offset = max(0, min(max_offset, pending.details_scroll_offset + delta))
    if next_offset == pending.details_scroll_offset:
        return False
    pending.details_scroll_offset = next_offset
    return True


def _toggle_pending_approval_expand(state: ScreenState) -> bool:
    pending = state.pending_approval
    if not pending or pending.request.get("kind") != "edit":
        return False
    pending.details_expanded = not pending.details_expanded
    pending.details_scroll_offset = 0
    return True


def _move_pending_approval_selection(state: ScreenState, delta: int) -> bool:
    pending = state.pending_approval
    if not pending or pending.feedback_mode:
        return False
    total = len(pending.request.get("choices", []))
    if total <= 0:
        return False
    pending.selected_choice_index = (pending.selected_choice_index + delta + total) % total
    return True


def _history_up(state: ScreenState) -> bool:
    if not state.history or state.history_index <= 0:
        return False
    if state.history_index == len(state.history):
        state.history_draft = state.input
    state.history_index -= 1
    state.input = state.history[state.history_index] if state.history_index < len(state.history) else ""
    state.cursor_offset = len(state.input)
    return True


def _history_down(state: ScreenState) -> bool:
    if state.history_index >= len(state.history):
        return False
    state.history_index += 1
    state.input = (
        state.history_draft
        if state.history_index == len(state.history)
        else (state.history[state.history_index] if state.history_index < len(state.history) else "")
    )
    state.cursor_offset = len(state.input)
    return True


def _get_visible_commands(input_text: str) -> list[Any]:
    if not input_text.startswith("/"):
        return []
    if input_text == "/":
        return SLASH_COMMANDS
    matches = find_matching_slash_commands(input_text)
    return [cmd for cmd in SLASH_COMMANDS if getattr(cmd, "usage", str(cmd)) in matches]


# ---------------------------------------------------------------------------
# Rendering -- cached header & footer
# ---------------------------------------------------------------------------

# Banner cache: the banner rarely changes (only when cwd, model, or stats change).
_banner_cache: dict[str, tuple[tuple, str]] = {"key": ((), "")}


def _render_header_panel(args: TtyAppArgs, state: ScreenState) -> str:
    """Render the top banner panel with model info, cwd, and session stats.
    
    The result is cached to avoid re-rendering when stats haven't changed.
    """
    stats = _get_session_stats(args, state)
    cache_key = (
        args.cwd,
        id(args.runtime),
        stats.get("transcriptCount"),
        stats.get("messageCount"),
        stats.get("skillCount"),
        stats.get("mcpCount"),
        _cached_terminal_size(),
    )
    cached = _banner_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]
    result = render_banner(
        args.runtime,
        args.cwd,
        args.permissions.get_summary(),
        stats,
    )
    _banner_cache["key"] = (cache_key, result)
    return result


# Footer cache: only changes with status, tool/skill state, background tasks
_footer_cache: dict[str, tuple[tuple, str]] = {"key": ((), "")}


def _render_footer_cached(
    status: str | None,
    tools_enabled: bool,
    skills_enabled: bool,
    background_tasks: list[dict[str, Any]],
) -> str:
    """Render the bottom status bar with caching to reduce flicker.
    
    Shows current operation status, tool/skill availability, and background tasks.
    """
    cache_key = (
        status,
        tools_enabled,
        skills_enabled,
        len(background_tasks),
        _cached_terminal_size(),
    )
    cached = _footer_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]
    result = render_footer_bar(status, tools_enabled, skills_enabled, background_tasks)
    _footer_cache["key"] = (cache_key, result)
    return result


def _render_prompt_panel(state: ScreenState) -> str:
    commands = _get_visible_commands(state.input)
    prompt_body = render_input_prompt(state.input, state.cursor_offset)
    if commands:
        prompt_body += "\n" + render_slash_menu(
            commands,
            min(state.selected_slash_index, len(commands) - 1),
        )
    return render_panel("prompt", prompt_body)


def _truncate_frame_lines(lines: list[str], cols: int, rows: int) -> list[str]:
    visible = lines[: max(1, rows)]
    return [truncate_plain(line, max(1, cols)) for line in visible]


def _visible_width(text: str) -> int:
    from pepsicode.tui.chrome import strip_ansi, string_display_width

    return string_display_width(strip_ansi(text))


def _pad_visible(text: str, cols: int) -> str:
    return text + " " * max(0, cols - _visible_width(text))


def _center_line(text: str, cols: int) -> str:
    pad = max(0, (cols - _visible_width(text)) // 2)
    return " " * pad + text


def _paint_line(text: str, cols: int, bg: str) -> str:
    painted = text.replace(RESET, RESET + bg)
    return bg + _pad_visible(truncate_plain(painted, cols), cols) + RESET


def _black_line(text: str, cols: int) -> str:
    return _paint_line(text, cols, "\u001b[48;5;16m")


def _left_rule_block(lines: list[str], cols: int, color: str, fill: str | None = None) -> list[str]:
    width = max(1, cols - 4)
    result: list[str] = []
    for raw in lines or [""]:
        text = truncate_plain(raw, width)
        if fill:
            result.append(f"{color}|{RESET}{fill} {truncate_plain(_pad_visible(text, width), width)} {RESET}")
        else:
            result.append(f"{color}|{RESET} {text}")
    return result


def _render_home_input_card(state: ScreenState, model: str, provider: str, cols: int) -> list[str]:
    """Claude-style: clean input without card background."""
    offset = max(0, min(state.cursor_offset, len(state.input)))
    before = state.input[:offset]
    current = state.input[offset] if offset < len(state.input) else " "
    after = state.input[offset + 1 :]
    placeholder = f"{DIM}Ask anything...{RESET}" if not state.input else ""
    prompt = f"{before}{HIGHLIGHT_BG}{BRIGHT_WHITE}{current}{RESET}{after}{placeholder}"
    return [
        f"  {ACCENT}>{RESET} {prompt}",
        f"  {SUBTLE}{model}{RESET}",
    ]


def _render_home_brand(cols: int, rows: int) -> list[str]:
    """Render the startup wordmark and a small terminal-safe mascot."""
    wordmark = [
        f"{ACCENT}{BOLD}pepsicode{RESET}",
        f"{SUBTLE}terminal coding agent{RESET}",
    ]
    if rows < 18 or cols < 58:
        return wordmark + [
            "",
            "  /\\_/\\",
            " ( o.o )  ready",
            "  > ^ <",
        ]

    mascot = [
        "                __",
        "           /\\_/\\\\`.__",
        "          (  o.o  )  )   sniffing the codebase",
        "          /  ___  \\_/",
        "         /__/   \\__\\  tests ready",
    ]
    return wordmark + [""] + mascot


def _render_home_screen(args: TtyAppArgs, state: ScreenState, cols: int, rows: int) -> None:
    """Claude-style: centered name, amber prompt, example prompts, minimal footer."""
    runtime = args.runtime or {}
    model = runtime.get("model", "unknown")
    visible_commands = _get_visible_commands(state.input)

    offset = max(0, min(state.cursor_offset, len(state.input)))
    before = state.input[:offset]
    current = state.input[offset] if offset < len(state.input) else " "
    after = state.input[offset + 1 :]
    placeholder = f"{DIM}Ask anything...{RESET}" if not state.input else ""
    prompt_line = f"  {ACCENT}>{RESET} {before}{HIGHLIGHT_BG}{BOLD}{current}{RESET}{after}{placeholder}"

    # Example prompts (like Claude Code)
    examples = [
        f"  {SUBTLE}{ICON_DOT}{RESET} Analyze this codebase",
        f"  {SUBTLE}{ICON_DOT}{RESET} Refactor the auth module",
        f"  {SUBTLE}{ICON_DOT}{RESET} Write tests for the API",
        f"  {SUBTLE}{ICON_DOT}{RESET} Explain the architecture",
    ]

    cwd_text = truncate_plain(str(Path(args.cwd)), max(10, cols - 30))
    footer = f"  {SUBTLE}{cwd_text}{RESET}  {DIM}{model}{RESET}"

    content = [""] + _render_home_brand(cols, rows) + [""] + examples + [""] + [prompt_line]
    if visible_commands and state.input:
        content.append("")
        content.extend(
            render_slash_menu(
                visible_commands,
                min(state.selected_slash_index, len(visible_commands) - 1),
            ).splitlines()
        )
    content += ["", footer]
    top_pad = max(1, min(8, (rows - len(content) - 1) // 2))
    frame = [""] * top_pad + content
    while len(frame) < rows - 1:
        frame.append("")
    frame.append(f"  {SUBTLE}/help for commands{RESET}  {SUBTLE}{ICON_DOT}{RESET}  {SUBTLE}ctrl+c to exit{RESET}")

    sys.stdout.write("[H" + "[K\n".join(frame[:rows]) + "[K[J")
    sys.stdout.flush()


def _render_resume_picker_lines(
    sessions: list[SessionMetadata],
    selected_index: int,
    cols: int,
    rows: int,
) -> list[str]:
    lines = [
        "",
        f"  {ACCENT}{BOLD}Resume Session{RESET}",
        f"  {SUBTLE}Up/Down select, Enter resume, d delete, Esc cancel{RESET}",
        "",
    ]
    visible_rows = max(1, rows - 8)
    selected_index = max(0, min(selected_index, len(sessions) - 1))
    start = max(0, min(selected_index - visible_rows // 2, max(0, len(sessions) - visible_rows)))
    end = min(len(sessions), start + visible_rows)

    for index in range(start, end):
        session = sessions[index]
        selected = index == selected_index
        marker = ">" if selected else " "
        color = ACCENT if selected else SUBTLE
        updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(session.updated_at))
        workspace = truncate_plain(session.workspace or "unknown", max(12, cols - 36))
        first = truncate_plain(session.first_message or "(empty)", max(12, cols - 10))
        lines.append(f"  {color}{marker} [{session.session_id[:8]}]{RESET} {updated}  {workspace}")
        lines.append(f"      {DIM}{session.message_count} messages | {first}{RESET}")

    if start > 0:
        lines.insert(4, f"  {SUBTLE}... {start} newer/hidden row(s) ...{RESET}")
    if end < len(sessions):
        lines.append(f"  {SUBTLE}... {len(sessions) - end} more row(s) ...{RESET}")
    return _truncate_frame_lines(lines, cols, rows)


def _render_stream_input(state: ScreenState, model: str, cols: int, visible_commands: list | None = None) -> list[str]:
    """Claude-style: clean amber > prompt line with slash hints."""
    offset = max(0, min(state.cursor_offset, len(state.input)))
    before = state.input[:offset]
    current = state.input[offset] if offset < len(state.input) else " "
    after = state.input[offset + 1 :]
    placeholder = f"{DIM}Ask anything...{RESET}" if not state.input else ""
    prompt = f"  {ACCENT}>{RESET} {before}{HIGHLIGHT_BG}{BOLD}{current}{RESET}{after}{placeholder}"

    lines = [prompt]

    # Slash command hints
    if visible_commands:
        if state.input == "/":
            lines.append(f"  {SUBTLE}Type to filter, arrows to navigate, Tab to autocomplete{RESET}")
        elif state.input.startswith("/"):
            count = len(visible_commands)
            lines.append(f"  {SUBTLE}{count} matching command{'s' if count != 1 else ''}{RESET}")

    # Status / thinking indicator
    if getattr(state, "is_busy", False):
        frame = getattr(state, "_animation_frame", 0)
        dots = "." * ((frame % 3) + 1)
        lines.append(f"  {ACCENT}{ICON_DOT}{RESET} {YELLOW}Thinking{dots}{RESET}")
    else:
        lines.append(f"  {SUBTLE}{model}{RESET}")

    return lines


def _render_stream_transcript(entries: list[TranscriptEntry], scroll_offset: int, height: int, cols: int) -> tuple[list[str], int]:
    """Claude-style: simple left rules, no backgrounds.

    Returns (visible_window_lines, total_rendered_lines).
    """
    if not entries:
        return ([f"{SUBTLE}Type a message to get started.{RESET}"][:height], 0)

    rendered: list[str] = []
    for entry in entries:
        body_lines = (entry.body or "").splitlines() or [""]
        if entry.kind == "user":
            rendered.append(f"  {ACCENT}{ICON_ARROW}{RESET} {BOLD}You{RESET}")
            for line in body_lines:
                rendered.append(f"     {wrap_text(line, cols - 7)}")
            rendered.append("")
            continue

        if entry.kind == "assistant":
            for line in render_markdownish(entry.body).splitlines() or [""]:
                rendered.append(f"  {wrap_text(line, cols - 4)}")
            rendered.append("")
            continue

        if entry.kind == "progress":
            for index, line in enumerate(body_lines):
                prefix = f"{ACCENT}{ICON_DOT}{RESET} " if index == 0 else "  "
                rendered.append(f"  {prefix}{SUBTLE}{wrap_text(line, cols - 5)}{RESET}")
            rendered.append("")
            continue

        if entry.kind == "tool":
            name = entry.toolName or "tool"
            status = entry.status or ""
            if status == "success":
                status_color = BRIGHT_GREEN
            elif status == "running":
                status_color = YELLOW
            else:
                status_color = "[91m"
            rendered.append(f"  {SUBTLE}{ICON_DIVIDER}{RESET} {BOLD}{name}{RESET} {status_color}{status}{RESET}")
            if entry.collapsed and entry.collapsedSummary:
                rendered.append(f"     {SUBTLE}{truncate_plain(entry.collapsedSummary, cols - 7)}{RESET}")
            elif not entry.collapsed:
                body = entry.body or ""
                body_output_lines = body.splitlines()
                max_visible = 30
                for i, line in enumerate(body_output_lines[:max_visible]):
                    rendered.append(f"     {SUBTLE}{wrap_text(line, cols - 7)}{RESET}")
                if len(body_output_lines) > max_visible:
                    rendered.append(f"     {SUBTLE}... {len(body_output_lines) - max_visible} more lines ...{RESET}")
            rendered.append("")

    total = len(rendered)
    max_offset = max(0, total - height)
    offset = max(0, min(scroll_offset, max_offset))
    end = total - offset
    start = max(0, end - height)
    window = rendered[start:end]
    if offset > 0:
        pct = int(offset / max_offset * 100) if max_offset > 0 else 0
        marker = f"{SUBTLE}-- {offset}/{max_offset} ({pct}%) --{RESET}"
        window = [marker] + window
        window = window[:height]
    return (window[-height:] if window else [], total)


def _render_stream_screen(args: TtyAppArgs, state: ScreenState) -> None:
    """Claude-style: clean stream layout with proper scroll tracking."""
    cols, rows = _get_terminal_size()
    cols = max(40, cols)
    rows = max(10, rows)
    if state.resume_picker_sessions is not None:
        frame = _render_resume_picker_lines(
            state.resume_picker_sessions,
            state.resume_picker_index,
            cols,
            rows - 1,
        )
        footer = f"  {SUBTLE}{Path(args.cwd).name or args.cwd}{RESET}  {SUBTLE}{len(state.resume_picker_sessions)} saved sessions{RESET}"
        sys.stdout.write("[H" + "[K\n".join(frame + [footer]) + "[K[J")
        sys.stdout.flush()
        return

    if not state.transcript:
        _render_home_screen(args, state, cols, rows)
        return

    model_name = args.runtime.get("model", "unknown") if args.runtime else "unknown"

    visible_commands = _get_visible_commands(state.input)
    input_lines = _render_stream_input(state, model_name, cols, visible_commands)

    # Render slash menu if visible
    if visible_commands and state.input:
        menu = render_slash_menu(
            visible_commands,
            min(state.selected_slash_index, len(visible_commands) - 1),
        )
        input_lines.append("")
        input_lines.extend(menu.splitlines())

    msg_count = len(state.transcript)
    sep = f"  {SUBTLE}{ICON_DIVIDER * min(50, cols - 4)}{RESET}"

    reserved = len(input_lines) + 3
    transcript_height = max(1, rows - reserved)
    transcript_snapshot = list(state.transcript)
    transcript_lines, total = _render_stream_transcript(
        transcript_snapshot,
        state.transcript_scroll_offset,
        transcript_height,
        cols,
    )
    state.transcript_total_lines = total

    cwd_name = Path(args.cwd).name or args.cwd
    footer = f"  {SUBTLE}{cwd_name}{RESET}  {SUBTLE}{msg_count} events{RESET}  {SUBTLE}v0.1{RESET}"

    frame = (
        _truncate_frame_lines(transcript_lines, cols, transcript_height)
        + [sep]
        + input_lines
        + [footer]
    )
    frame = _truncate_frame_lines(frame, cols, rows)

    sys.stdout.write("[H" + "[K\n".join(frame) + "[K[J")
    sys.stdout.flush()


def _render_screen(args: TtyAppArgs, state: ScreenState) -> None:
    if not state.pending_approval and os.environ.get("pepsicode_LEGACY_TUI") != "1":
        _render_stream_screen(args, state)
        return

    background_tasks = list_background_tasks()

    # Get contextual help text
    contextual_help = _get_contextual_help(state, args)

    # Build the entire frame into a buffer, then write once
    buf: list[str] = []
    # Full display clear -- 2J clears the entire display instead of just erasing
    # below the cursor (plain J), which is more reliable on Windows alt-screen.
    buf.append("\u001b[H")

    # Header
    buf.append(_render_header_panel(args, state))
    buf.append("\n\n")

    has_skills = len(args.tools.get_skills()) > 0

    if state.pending_approval:
        # Permission approval overlay
        buf.append(
            render_permission_prompt(
                state.pending_approval.request,
                expanded=state.pending_approval.details_expanded,
                scroll_offset=state.pending_approval.details_scroll_offset,
                selected_choice_index=state.pending_approval.selected_choice_index,
                feedback_mode=state.pending_approval.feedback_mode,
                feedback_input=state.pending_approval.feedback_input,
            )
        )
        buf.append("\n\n")
        buf.append(
            render_panel(
                "activity",
                render_tool_panel(state.active_tool, state.recent_tools, background_tasks),
            )
        )
        buf.append("\n\n")
        buf.append(_render_footer_cached(state.status, True, has_skills, background_tasks))
        sys.stdout.write("".join(buf) + "[J")
        sys.stdout.flush()
        return

    # Transcript -- snapshot the list to avoid IndexError from concurrent
    # agent-thread appends (CPython GIL makes list.append atomic but
    # iteration + append can still race on length vs slot access).
    transcript_snapshot = list(state.transcript)
    body_lines = _get_transcript_body_lines(args, state)
    if transcript_snapshot:
        transcript_body = render_transcript(
            transcript_snapshot, state.transcript_scroll_offset, body_lines
        )
    else:
        transcript_body = f"{render_status_line(None)}\n\nType /help for commands."
    buf.append(
        render_panel(
            "session feed",
            transcript_body,
            right_title=f"{len(transcript_snapshot)} events",
            min_body_lines=body_lines,
        )
    )
    buf.append("\n\n")

    # Prompt
    buf.append(_render_prompt_panel(state))
    buf.append("\n\n")

    # Footer (cached)
    buf.append(_render_footer_cached(state.status, True, has_skills, background_tasks))
    
    # Contextual help line
    if contextual_help:
        buf.append(f"\n{SUBTLE}{contextual_help}{RESET}")
    
    sys.stdout.write("".join(buf) + "[J")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Cross-platform raw mode stdin
# ---------------------------------------------------------------------------

# Windows msvcrt scan-code -> ANSI escape sequence mapping.
# msvcrt.getwch() returns a two-char sequence for special keys:
#   prefix ('\x00' or '\xe0') + scan-code byte.
# We translate these to the ANSI sequences that input_parser.py already
# understands.
_WIN_SCANCODE_TO_ANSI: dict[int, str] = {
    72: "\x1b[A",    # Up
    80: "\x1b[B",    # Down
    77: "\x1b[C",    # Right
    75: "\x1b[D",    # Left
    71: "\x1b[H",    # Home
    79: "\x1b[F",    # End
    73: "\x1b[5~",   # Page Up
    81: "\x1b[6~",   # Page Down
    83: "\x1b[3~",   # Delete
    82: "\x1b[2~",   # Insert
    # Alt+Arrow (returned with \x00 prefix on some terminals)
    152: "\x1b[1;3A",  # Alt+Up
    160: "\x1b[1;3B",  # Alt+Down
    157: "\x1b[1;3C",  # Alt+Right
    155: "\x1b[1;3D",  # Alt+Left
    # Ctrl+Arrow
    141: "\x1b[1;5A",  # Ctrl+Up
    145: "\x1b[1;5B",  # Ctrl+Down
    116: "\x1b[1;5C",  # Ctrl+Right
    115: "\x1b[1;5D",  # Ctrl+Left
}


def _win_read_one_key() -> str:
    """Read one logical key from Windows msvcrt, translating special keys
    into ANSI escape sequences.

    Returns an empty string if no key is available.
    """
    import msvcrt

    if not msvcrt.kbhit():
        return ""

    ch = msvcrt.getwch()

    # Special-key prefix: next char is a scan code
    if ch in ("\x00", "\xe0"):
        if msvcrt.kbhit():
            scan = ord(msvcrt.getwch())
        else:
            # Prefix arrived alone (rare) -- treat as Escape
            return "\x1b"
        return _WIN_SCANCODE_TO_ANSI.get(scan, "")

    # Ctrl+C -> keep as '\x03' so parse_input_chunk handles it
    return ch


def _read_raw_char() -> str:
    """Read a single character from stdin in raw mode, cross-platform."""
    if sys.platform == "win32":
        return _win_read_one_key()
    else:
        import select

        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            # Use os.read() to bypass Python's TextIOWrapper buffering.
            # In raw/cbreak mode the kernel returns whatever bytes are
            # available, so os.read() won't block.
            data = os.read(fd, 4096)
            return data.decode("utf-8", errors="replace") if data else ""
        return ""


def _read_raw_chunk() -> str:
    """Read all available raw chars as a single chunk."""
    if sys.platform == "win32":
        result = ""
        while True:
            ch = _win_read_one_key()
            if not ch:
                break
            result += ch
        return result
    else:
        import select

        fd = sys.stdin.fileno()
        # First wait with a timeout for initial data
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            return ""
        # Read all available bytes in one go.  In raw mode the kernel
        # delivers whatever has arrived so far; os.read() returns
        # immediately with 1..N bytes.
        data = os.read(fd, 4096)
        if not data:
            return ""
        # Drain any remaining bytes without blocking
        while True:
            ready2, _, _ = select.select([fd], [], [], 0)
            if not ready2:
                break
            more = os.read(fd, 4096)
            if not more:
                break
            data += more
        return data.decode("utf-8", errors="replace")


class _RawModeContext:
    """Context manager for raw terminal mode.

    On Unix: switches stdin to raw mode via termios/tty and restores on exit.
    On Windows: msvcrt provides character-at-a-time input natively, but we
    need to ensure the console code page is set for UTF-8 and VT processing
    is enabled.
    """

    def __init__(self) -> None:
        self._old_settings: Any = None
        self._old_cp: int | None = None

    def __enter__(self) -> _RawModeContext:
        if sys.platform == "win32":
            # Ensure VT processing is active (idempotent)
            from pepsicode.tui.screen import _enable_windows_vt_processing
            _enable_windows_vt_processing()
            # Switch console to UTF-8 code page for proper Unicode handling
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                self._old_cp = kernel32.GetConsoleOutputCP()
                kernel32.SetConsoleOutputCP(65001)  # UTF-8
            except Exception:
                pass
        else:
            import termios

            fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            # Input flags: disable CR->NL translation and XON/XOFF flow control,
            # strip high bit, and break signal generation.
            new[0] &= ~(
                termios.BRKINT | termios.ICRNL | termios.INPCK
                | termios.ISTRIP | termios.IXON
            )
            # Output flags: KEEP OPOST so that \n -> \r\n translation still
            # works.  tty.setraw() clears OPOST which causes "staircase"
            # output on Linux/macOS -- every newline only moves down without
            # returning the cursor to column 0.
            # new[1] is intentionally left untouched.
            # Control flags: set 8-bit chars
            new[2] &= ~(termios.CSIZE | termios.PARENB)
            new[2] |= termios.CS8
            # Local flags: disable echo, canonical mode, extended processing,
            # and signal generation from keys (Ctrl-C, Ctrl-Z).
            new[3] &= ~(
                termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG
            )
            # Special characters: read returns after 1 byte, no timeout.
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSAFLUSH, new)
        return self

    def __exit__(self, *_: Any) -> None:
        if sys.platform == "win32":
            if self._old_cp is not None:
                try:
                    import ctypes
                    ctypes.windll.kernel32.SetConsoleOutputCP(self._old_cp)  # type: ignore[attr-defined]
                except Exception:
                    pass
        elif self._old_settings is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)


# ---------------------------------------------------------------------------
# Tool shortcut execution
# ---------------------------------------------------------------------------


def _execute_tool_shortcut(
    args: TtyAppArgs,
    state: ScreenState,
    tool_name: str,
    tool_input: Any,
    rerender: Callable[[], None],
) -> None:
    state.is_busy = True
    state.status = f"Running {tool_name}..."
    state.active_tool = tool_name
    entry_id = _push_transcript_entry(
        state,
        kind="tool",
        toolName=tool_name,
        status="running",
        body=_summarize_tool_input(tool_name, tool_input),
    )
    rerender()

    try:
        result = args.tools.execute(
            tool_name,
            tool_input,
            context=ToolContext(cwd=args.cwd, permissions=args.permissions),
        )
        state.recent_tools.append({
            "name": tool_name,
            "status": "success" if result.ok else "error",
        })
        output = result.output if result.ok else f"ERROR: {result.output}"
        _update_tool_entry(state, entry_id, "success" if result.ok else "error", output)
        _collapse_tool_entry(state, entry_id, _summarize_collapsed_tool_body(output))
        state.transcript_scroll_offset = 0
        state.user_scrolled_away = False
    finally:
        state.is_busy = False
        state.active_tool = None
        _finalize_dangling_running_tools(state)
        if not _get_running_tool_entries(state):
            state.status = None


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def _handle_input(
    args: TtyAppArgs,
    state: ScreenState,
    rerender: Callable[[], None],
    submitted_raw_input: str | None = None,
) -> bool:
    """Returns True if /exit was typed."""
    if state.is_busy:
        state.status = (
            f"Running {state.active_tool}..."
            if state.active_tool
            else "Current turn is still running..."
        )
        return False

    input_text = (submitted_raw_input if submitted_raw_input is not None else state.input).strip()
    if not input_text:
        return False
    if input_text == "/exit":
        return True

    # History
    if not state.history or state.history[-1] != input_text:
        state.history.append(input_text)
        save_history_entries(state.history)
    state.history_index = len(state.history)
    state.history_draft = ""

    # Autosave trigger
    if state.autosave:
        state.autosave.mark_dirty()

    # /tools
    if input_text == "/tools":
        _push_transcript_entry(
            state,
            kind="assistant",
            body="\n".join(
                f"{t.name}: {t.description}" for t in args.tools.list()
            ),
        )
        return False

    if input_text == "/resume" or input_text.startswith("/resume "):
        session_id = input_text[len("/resume") :].strip()
        if session_id:
            _resume_session_by_id(args, state, session_id)
        else:
            _open_resume_picker(args, state)
        return False

    # Local commands
    local_result = try_handle_local_command(input_text, tools=args.tools)
    if local_result is not None:
        _push_transcript_entry(state, kind="assistant", body=local_result)
        return False

    # Tool shortcuts
    shortcut = parse_local_tool_shortcut(input_text)
    if shortcut:
        _execute_tool_shortcut(
            args, state, shortcut["toolName"], shortcut["input"], rerender
        )
        return False

    # Unknown slash commands
    if input_text.startswith("/"):
        matches = find_matching_slash_commands(input_text)
        _push_transcript_entry(
            state,
            kind="assistant",
            body=(
                f"Unknown command. Did you mean:\n{chr(10).join(matches)}"
                if matches
                else "Unknown command. Type /help to see available commands."
            ),
        )
        return False

    # Agent turn
    _push_transcript_entry(state, kind="user", body=input_text)
    state.transcript_scroll_offset = 0
    state.user_scrolled_away = False
    state.status = "Thinking..."
    state.is_busy = True
    
    # Update app state
    if state.app_state:
        from pepsicode.state import set_busy
        state.app_state.set_state(set_busy())
    
    rerender()

    pending_tool_entries: dict[str, list[int]] = defaultdict(list)
    aggregated_edit_by_key: dict[str, AggregatedEditProgress] = {}
    aggregated_edit_by_entry_id: dict[int, AggregatedEditProgress] = {}

    # Refresh system prompt
    args.messages[0] = {
        "role": "system",
        "content": build_system_prompt(
            args.cwd,
            args.permissions.get_summary(),
            {
                "skills": args.tools.get_skills(),
                "mcpServers": args.tools.get_mcp_servers(),
                "governance": bool(args.runtime.get("governance")) if args.runtime else False,
            },
        ),
    }
    args.messages.append({"role": "user", "content": input_text})

    def on_token(token: StreamToken) -> None:
        """Handle a single streaming token from the model.

        Creates or updates a transcript entry in real-time so the user sees
        the response being generated character-by-character.
        """
        if token.type == "text":
            state.streaming_text += token.content
            if state.streaming_entry_id is None:
                # Create a new transcript entry for the streaming output.
                state.streaming_entry_id = _push_transcript_entry(
                    state, kind="assistant", body=state.streaming_text,
                )
            else:
                # Update the existing entry in-place.
                for entry in state.transcript:
                    if entry.id == state.streaming_entry_id:
                        entry.body = state.streaming_text
                        break
            _reset_scroll_if_needed(state)
            rerender()

        elif token.type == "done":
            # Don't clear streaming state here -- let on_assistant_message
            # or on_progress_message finalize it to avoid duplicate entries.
            pass

    def on_assistant_message(content: str) -> None:
        if state.streaming_entry_id is not None:
            # Streaming already displayed this content in a transcript entry.
            # Update the entry body (idempotent for normal flow, overwrites
            # partial content on error paths) and finalize streaming state.
            for entry in state.transcript:
                if entry.id == state.streaming_entry_id:
                    entry.body = content
                    break
            state.streaming_entry_id = None
            state.streaming_text = ""
        else:
            # No streaming happened (error path, etc.) -- push a new entry.
            _push_transcript_entry(state, kind="assistant", body=content)
        _reset_scroll_if_needed(state)
        rerender()

    def on_progress_message(content: str) -> None:
        if state.streaming_entry_id is not None:
            # Streaming created an "assistant" entry for text before tool calls.
            # Convert it to "progress" kind so the display is consistent.
            for entry in state.transcript:
                if entry.id == state.streaming_entry_id:
                    entry.kind = "progress"
                    break
            state.streaming_entry_id = None
            state.streaming_text = ""
        else:
            _push_transcript_entry(state, kind="progress", body=content)
        _reset_scroll_if_needed(state)
        rerender()

    def on_tool_start(tool_name: str, tool_input: Any) -> None:
        state.status = f"Running {tool_name}..."
        state.active_tool = tool_name
        state.tool_start_time = time.monotonic()  # Record tool start time

        target_path = _extract_path_from_tool_input(tool_input)
        can_aggregate = _is_file_edit_tool(tool_name) and target_path is not None

        if can_aggregate:
            key = f"{tool_name}:{target_path}"
            existing = aggregated_edit_by_key.get(key)
            if existing:
                existing.total += 1
                existing.last_output = _summarize_tool_input(tool_name, tool_input)
                entry_id = existing.entry_id
                _update_tool_entry(
                    state,
                    entry_id,
                    "error" if existing.errors > 0 else "running",
                    f"Aggregated {tool_name} for {target_path}\nCompleted: {existing.completed}/{existing.total}",
                )
            else:
                entry_id = _push_transcript_entry(
                    state,
                    kind="tool",
                    toolName=tool_name,
                    status="running",
                    body=_summarize_tool_input(tool_name, tool_input),
                )
                progress = AggregatedEditProgress(
                    entry_id=entry_id,
                    tool_name=tool_name,
                    path=target_path,
                    total=1,
                    completed=0,
                    errors=0,
                    last_output=_summarize_tool_input(tool_name, tool_input),
                )
                aggregated_edit_by_key[key] = progress
                aggregated_edit_by_entry_id[entry_id] = progress
        else:
            entry_id = _push_transcript_entry(
                state,
                kind="tool",
                toolName=tool_name,
                status="running",
                body=_summarize_tool_input(tool_name, tool_input),
            )

        pending_tool_entries[tool_name].append(entry_id)
        _reset_scroll_if_needed(state)
        rerender()

    def on_tool_result(tool_name: str, output: str, is_error: bool) -> None:
        # Compute and display the tool execution time
        elapsed = ""
        if state.tool_start_time is not None:
            elapsed_secs = time.monotonic() - state.tool_start_time
            if elapsed_secs > 1:
                elapsed = f" ({elapsed_secs:.1f}s)"
        
        pending = pending_tool_entries.get(tool_name, [])
        entry_id = pending.pop(0) if pending else None
        if entry_id is not None:
            aggregated = aggregated_edit_by_entry_id.get(entry_id)
            if aggregated and aggregated.tool_name == tool_name:
                aggregated.completed += 1
                if is_error:
                    aggregated.errors += 1
                aggregated.last_output = output
                done = aggregated.completed >= aggregated.total
                if done:
                    state.recent_tools.append({
                        "name": f"{tool_name} x{aggregated.total}",
                        "status": "error" if aggregated.errors > 0 else "success",
                    })
                body = (
                    "\n".join([
                        f"Aggregated {tool_name} for {aggregated.path}",
                        f"Operations: {aggregated.total}, errors: {aggregated.errors}",
                        f"Last result: {aggregated.last_output}",
                    ])
                    if done
                    else f"Aggregated {tool_name} for {aggregated.path}\nCompleted: {aggregated.completed}/{aggregated.total}"
                )
                _update_tool_entry(
                    state,
                    entry_id,
                    "error" if aggregated.errors > 0 else ("success" if done else "running"),
                    body,
                )
                if done:
                    _collapse_tool_entry(state, entry_id, _summarize_collapsed_tool_body(body))
                    aggregated_edit_by_entry_id.pop(entry_id, None)
                    aggregated_edit_by_key.pop(f"{tool_name}:{aggregated.path}", None)
            else:
                state.recent_tools.append({
                    "name": tool_name,
                    "status": "error" if is_error else "success",
                })
                
                # Error recovery hints
                display_output = output
                if is_error:
                    suggestions = []
                    output_lower = output.lower()
                    if "not found" in output_lower or "no such file" in output_lower:
                        suggestions.append("💡 File not found. Try /ls to see available files")
                    elif "permission" in output_lower or "denied" in output_lower:
                        suggestions.append("💡 Permission denied. Check file access rights")
                    elif "syntax" in output_lower or "error" in output_lower:
                        suggestions.append("💡 Error occurred. Review the output and fix issues")
                    
                    if suggestions:
                        display_output = f"ERROR: {output}\n\n" + "\n".join(suggestions)
                    else:
                        display_output = f"ERROR: {output}"
                
                _update_tool_entry(
                    state,
                    entry_id,
                    "error" if is_error else "success",
                    display_output,
                )
                _schedule_tool_auto_collapse(
                    state,
                    entry_id,
                    display_output,
                    rerender,
                )

        state.active_tool = None
        remaining = sum(len(v) for v in pending_tool_entries.values())
        if remaining > 0:
            state.status = f"{remaining} tool(s) still running..."
        else:
            state.status = None
        _reset_scroll_if_needed(state)
        rerender()

    args.permissions.begin_turn()
    
    # Run agent turn in background thread to keep UI responsive
    agent_error = None
    agent_result: dict = {"messages": None}
    agent_thread_lock = threading.Lock()
    
    def _run_agent_background():
        nonlocal agent_error, agent_result
        try:
            # Use streaming agent turn for real-time token output
            next_messages = run_agent_turn_stream(
                model=args.model,
                tools=args.tools,
                messages=list(args.messages),  # Copy to avoid race condition
                cwd=args.cwd,
                permissions=args.permissions,
                on_token=on_token,
                on_tool_start=on_tool_start,
                on_tool_result=on_tool_result,
                on_assistant_message=on_assistant_message,
                on_progress_message=on_progress_message,
                context_manager=args.context_manager,
            )
            with agent_thread_lock:
                agent_result["messages"] = next_messages
        except Exception as e:
            agent_error = e
        finally:
            args.permissions.end_turn()
            with agent_thread_lock:
                agent_result["done"] = True
            state.is_busy = False
            state.active_tool = None
            state.status = None
            # Clear streaming state
            state.streaming_entry_id = None
            state.streaming_text = ""
            rerender()
    
    agent_thread = threading.Thread(target=_run_agent_background, daemon=True)
    agent_thread.start()
    state.agent_thread = agent_thread
    # Assign lock BEFORE result -- the main loop checks agent_result first,
    # so the lock must already be available to avoid AttributeError.
    state.agent_lock = agent_thread_lock
    state.agent_result = agent_result
    
    # Return immediately - agent runs in background
    return False


# ---------------------------------------------------------------------------
# Main event-driven TTY app
# ---------------------------------------------------------------------------


def run_tty_app(
    *,
    runtime: dict | None,
    tools: ToolRegistry,
    model: ModelAdapter,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager,
    resume_session: str | None = None,
    list_sessions_only: bool = False,
) -> list[ChatMessage]:
    """Event-driven full-screen TTY application, ported from the TypeScript version.
    
    Args:
        resume_session: Session ID to resume, or "latest" for most recent
        list_sessions_only: If True, print session list and exit
    """

    context_manager = None
    if runtime:
        from pepsicode.context_manager import ContextManager
        context_manager = ContextManager(model=runtime.get("model", "default"))
        if hasattr(model, "summarize"):
            context_manager.summarizer = model.summarize

    args = TtyAppArgs(
        runtime=runtime,
        tools=tools,
        model=model,
        messages=messages,
        cwd=cwd,
        permissions=permissions,
        context_manager=context_manager,
    )

    # Session initialization
    session: SessionData | None = None
    
    if list_sessions_only:
        sessions = list_sessions()
        print(format_session_list(sessions))
        return messages
    
    if resume_session:
        if resume_session == "latest":
            session = get_latest_session(workspace=str(Path(cwd).resolve()))
            if session:
                print(format_session_resume(session))
            else:
                print("No previous session found for this workspace.")
                session = create_new_session(workspace=str(Path(cwd).resolve()))
        else:
            session = load_session(resume_session)
            if not session:
                print(f"Session '{resume_session}' not found.")
                return messages
            print(format_session_resume(session))
    else:
        # Check for existing session in current workspace
        session = get_latest_session(workspace=str(Path(cwd).resolve()))
        if session:
            if os.environ.get("PEPSI_CODE_VERBOSE", "") == "1":
                print(f"Previous session found: {session.session_id[:8]}")
            print("Use --resume to continue, or starting fresh session.")
            session = None
    
    if not session:
        session = create_new_session(workspace=str(Path(cwd).resolve()))
    
    # Initialize AppState store (Zustand-style)
    app_state_store = create_app_store({
        "session_id": session.session_id,
        "workspace": cwd,
        "model": runtime.get("model", "unknown") if runtime else "unknown",
    })
    
    # Initialize CostTracker
    cost_tracker = CostTracker()

    state = ScreenState(
        history=load_history_entries(),
        session=session,
        autosave=AutosaveManager(session),
        app_state=app_state_store,
        cost_tracker=cost_tracker,
    )
    state.history_index = len(state.history)

    # Restore session state if resuming
    if session.messages:
        # Restore messages
        args.messages.clear()
        args.messages.extend(session.messages)
        
        # Restore transcript entries
        for entry_data in session.transcript_entries:
            entry = TranscriptEntry(**entry_data)
            state.transcript.append(entry)
        
        print(f"Restored {len(session.messages)} messages, {len(state.transcript)} transcript entries.")

    # Wire up permission prompt handler
    approval_event = threading.Event()
    approval_result: dict[str, Any] = {}

    def _permission_prompt_handler(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal approval_result
        state.pending_approval = PendingApproval(
            request=request,
            resolve=lambda r: None,
        )
        # Signal the main thread's throttled renderer to show the approval UI.
        # Do NOT call _render_screen() here -- we're on the agent thread and
        # writing to stdout concurrently with the main thread would corrupt
        # the terminal display.  request() only sets a pending flag; the main
        # event loop's next flush() will do the actual render safely.
        rerender()
        approval_event.clear()
        approval_event.wait()
        result = approval_result.copy()
        state.pending_approval = None
        return result

    permissions.prompt = _permission_prompt_handler

    # Throttled renderer: coalesces rapid rerender() calls to reduce flickering
    throttled = _ThrottledRenderer(lambda: _render_screen(args, state), min_interval=0.016)

    def rerender() -> None:
        throttled.request()

    input_remainder = ""
    should_exit = False
    # Autosave throttle: check at most every ~2 seconds, not every 20ms
    _autosave_counter = 0
    _AUTOSAVE_CHECK_INTERVAL = 100  # iterations (~2s at 20ms polling)

    # Set console to UTF-8 on Windows before any render -- avoids garbled
    # CJK on Chinese/Japanese systems where the default code page may be
    # 936/932 (the _RawModeContext also sets this, but enters AFTER the
    # first _render_screen call).
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # type: ignore[attr-defined]
        except Exception:
            pass

    enter_alternate_screen()
    hide_cursor()

    # On Unix, listen for SIGWINCH so terminal resizes are picked up
    # immediately rather than waiting for the 0.5s cache TTL.
    # signal.signal() can only be called from the main thread.
    _prev_sigwinch = None
    if (
        sys.platform != "win32"
        and threading.current_thread() is threading.main_thread()
    ):
        import signal as _signal

        from pepsicode.tui.chrome import invalidate_terminal_size_cache

        def _on_sigwinch(_signum: int, _frame: Any) -> None:
            invalidate_terminal_size_cache()
            throttled.request()

        try:
            _prev_sigwinch = _signal.signal(_signal.SIGWINCH, _on_sigwinch)
        except (OSError, ValueError):
            # Couldn't set signal handler (e.g. not main thread despite check)
            _prev_sigwinch = None

    try:
        _render_screen(args, state)

        with _RawModeContext():
            while not should_exit:
                # Autosave check (throttled)
                _autosave_counter += 1
                if state.autosave and _autosave_counter >= _AUTOSAVE_CHECK_INTERVAL:
                    _autosave_counter = 0
                    state.autosave.save_if_needed()

                # Animation frame for thinking indicator
                state._animation_frame += 1
                
                # Check if background agent thread completed
                agent_result_data = state.agent_result
                lock = getattr(state, "agent_lock", None)
                if agent_result_data is not None and lock is not None and agent_result_data.get("done"):
                    with lock:
                        if agent_result_data.get("messages"):
                            args.messages = agent_result_data["messages"]
                        agent_result_data["done"] = False  # Reset flag

                # Read raw input
                if sys.platform == "win32":
                    import msvcrt

                    if not msvcrt.kbhit():
                        # Flush any deferred renders during idle
                        throttled.flush()
                        time.sleep(0.05)  # raised from 0.02 to 0.05 to lower CPU usage
                        continue
                    # Use _win_read_one_key to translate special keys
                    chunk = ""
                    while True:
                        ch = _win_read_one_key()
                        if not ch:
                            break
                        chunk += ch
                else:
                    import select

                    _fd = sys.stdin.fileno()
                    ready, _, _ = select.select([_fd], [], [], 0.05)
                    if not ready:
                        # Flush any deferred renders during idle
                        throttled.flush()
                        continue
                    # Use os.read() to bypass Python's TextIOWrapper/
                    # BufferedReader which can block on partial UTF-8
                    # sequences in raw mode.
                    _raw = os.read(_fd, 4096)
                    if not _raw:
                        should_exit = True
                        continue
                    # Drain any remaining bytes without blocking
                    while True:
                        ready2, _, _ = select.select([_fd], [], [], 0)
                        if not ready2:
                            break
                        _more = os.read(_fd, 4096)
                        if not _more:
                            break
                        _raw += _more
                    chunk = _raw.decode("utf-8", errors="replace")

                if not chunk:
                    continue

                parsed = parse_input_chunk(input_remainder + chunk)
                input_remainder = parsed.rest

                for event in parsed.events:
                    try:
                        _handle_event(args, state, event, rerender, approval_event, approval_result)
                        if state.input == "/exit" or (
                            isinstance(event, KeyEvent)
                            and event.name == "c"
                            and event.ctrl
                        ):
                            raise SystemExit(0)
                    except SystemExit:
                        should_exit = True
                        break
                    except Exception as e:
                        # Log the event-handling error but keep the main loop running
                        logging.debug("Event handling error: %s", e, exc_info=True)

                # Ensure the final state after processing all events is visible
                throttled.flush()

    finally:
        # Restore previous SIGWINCH handler on Unix
        if _prev_sigwinch is not None and sys.platform != "win32":
            import signal as _signal

            _signal.signal(_signal.SIGWINCH, _prev_sigwinch)

        show_cursor()
        exit_alternate_screen()
        
        # Final session save
        if state.session:
            # Update session with current state
            state.session.messages = list(args.messages)
            state.session.transcript_entries = [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "toolName": e.toolName,
                    "status": e.status,
                    "body": e.body,
                    "collapsed": e.collapsed,
                    "collapsedSummary": e.collapsedSummary,
                    "collapsePhase": e.collapsePhase,
                }
                for e in state.transcript
            ]
            state.session.history = state.history
            state.session.permissions_summary = args.permissions.get_summary()
            state.session.skills = args.tools.get_skills()
            state.session.mcp_servers = args.tools.get_mcp_servers()
            
            # Force save
            if state.autosave:
                state.autosave.force_save()
            else:
                save_session(state.session)
            
            if os.environ.get("PEPSI_CODE_VERBOSE", "") == "1":
                print(
                    f"\nSession saved: {state.session.session_id[:8]} "
                    f"(resume with: python -m pepsicode.main --resume {state.session.session_id})"
                )

    return args.messages


def _handle_event(
    args: TtyAppArgs,
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """Process a single parsed input event.
    
    Routes the event to the appropriate handler based on current state:
    - Ctrl+C: Exit immediately
    - Pending approval: Handle permission dialog input
    - Normal mode: Handle input, navigation, and commands
    
    Args:
        args: Application arguments (tools, model, permissions)
        state: Current screen state
        event: Parsed input event from terminal
        rerender: Function to trigger screen redraw
        approval_event: Threading event for approval synchronization
        approval_result: Dict to store approval decision
    """
    # ---------- Ctrl+C -> exit ----------
    if isinstance(event, TextEvent) and event.ctrl and event.text == "c":
        raise SystemExit(0)

    # ---------- Pending approval mode ----------
    # Capture locally to avoid TOCTOU -- the agent thread may clear
    # state.pending_approval between our check and the handler's use.
    pending = state.pending_approval
    if pending is not None:
        _handle_pending_approval_event(state, pending, event, rerender, approval_event, approval_result)
        return

    if state.resume_picker_sessions is not None:
        _handle_resume_picker_event(args, state, event, rerender)
        return

    # ---------- Normal mode ----------
    _handle_normal_mode_event(args, state, event, rerender)


# ---------------------------------------------------------------------------
# Pending approval event handlers
# ---------------------------------------------------------------------------


def _handle_resume_picker_event(
    args: TtyAppArgs,
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
) -> None:
    sessions = state.resume_picker_sessions or []
    if not sessions:
        state.resume_picker_sessions = None
        state.resume_picker_index = 0
        state.status = None
        rerender()
        return

    if isinstance(event, KeyEvent):
        if event.name == "escape":
            state.resume_picker_sessions = None
            state.resume_picker_index = 0
            state.status = "Resume cancelled"
            rerender()
            return
        if event.name == "up":
            state.resume_picker_index = max(0, state.resume_picker_index - 1)
            rerender()
            return
        if event.name == "down":
            state.resume_picker_index = min(len(sessions) - 1, state.resume_picker_index + 1)
            rerender()
            return
        if event.name == "pageup":
            state.resume_picker_index = max(0, state.resume_picker_index - 8)
            rerender()
            return
        if event.name == "pagedown":
            state.resume_picker_index = min(len(sessions) - 1, state.resume_picker_index + 8)
            rerender()
            return
        if event.name == "return":
            selected = sessions[state.resume_picker_index]
            _resume_session_by_id(args, state, selected.session_id)
            rerender()
            return

    if isinstance(event, TextEvent) and not event.ctrl and event.text.lower() == "d":
        selected = sessions[state.resume_picker_index]
        if delete_session(selected.session_id):
            sessions = _session_rows_for_picker(args.cwd)
            if not sessions:
                state.resume_picker_sessions = None
                state.resume_picker_index = 0
                state.status = "Deleted session; no saved sessions remain."
            else:
                state.resume_picker_sessions = sessions
                state.resume_picker_index = min(state.resume_picker_index, len(sessions) - 1)
                state.status = f"Deleted session {selected.session_id[:8]}"
        else:
            state.status = f"Could not delete session {selected.session_id[:8]}"
        rerender()


def _handle_pending_approval_event(
    state: ScreenState,
    pending: Any,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """Handle input events while a permission approval is pending.
    
    ``pending`` is captured by the caller to avoid TOCTOU races with the
    agent thread (which may set ``state.pending_approval = None`` after an
    approval event is signalled).
    """
    if pending.feedback_mode:
        _handle_feedback_mode_event(state, event, rerender, approval_event, approval_result)
        return
    
    if isinstance(event, KeyEvent):
        if _handle_pending_approval_key(state, event, rerender, approval_event, approval_result):
            return
    
    if isinstance(event, TextEvent) and not event.ctrl:
        if _handle_pending_approval_text(state, event, rerender, approval_event, approval_result):
            return
    
    if isinstance(event, WheelEvent):
        if _handle_pending_approval_wheel(state, event, rerender):
            return


def _handle_pending_approval_key(
    state: ScreenState,
    event: KeyEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> bool:
    """Handle key events during pending approval. Returns True if handled."""
    pending = state.pending_approval
    
    if event.name == "escape":
        approval_result.clear()
        approval_result["decision"] = "deny_once"
        approval_event.set()
        rerender()
        return True
    
    if event.name == "return":
        _confirm_pending_choice(state, rerender, approval_event, approval_result)
        return True
    
    if event.name == "up" and _move_pending_approval_selection(state, -1):
        rerender()
        return True
    
    if event.name == "down" and _move_pending_approval_selection(state, 1):
        rerender()
        return True
    
    if event.name == "pageup" and _scroll_pending_approval_by(state, -5):
        rerender()
        return True
    
    if event.name == "pagedown" and _scroll_pending_approval_by(state, 5):
        rerender()
        return True
    
    # Digit keys for choices
    choices = pending.request.get("choices", [])
    for choice in choices:
        if event.text == choice.get("key"):
            _select_pending_choice(state, choice, rerender, approval_event, approval_result)
            return True
    
    return False


def _handle_pending_approval_text(
    state: ScreenState,
    event: TextEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> bool:
    """Handle text events during pending approval. Returns True if handled."""
    pending = state.pending_approval
    
    if event.text == "v" and _toggle_pending_approval_expand(state):
        rerender()
        return True
    
    # Check digit keys for choices
    choices = pending.request.get("choices", [])
    for choice in choices:
        if event.text == choice.get("key"):
            _select_pending_choice(state, choice, rerender, approval_event, approval_result)
            return True
    
    return False


def _handle_pending_approval_wheel(
    state: ScreenState,
    event: WheelEvent,
    rerender: Callable[[], None],
) -> bool:
    """Handle wheel events during pending approval for scrolling. Returns True if handled."""
    delta = 3 if event.direction == "up" else -3
    if _scroll_pending_approval_by(state, delta):
        rerender()
        return True
    return False



def _confirm_pending_choice(
    state: ScreenState,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """Confirm the selected permission choice."""
    pending = state.pending_approval
    choices = pending.request.get("choices", [])
    
    if choices and 0 <= pending.selected_choice_index < len(choices):
        choice = choices[pending.selected_choice_index]
        _select_pending_choice(state, choice, rerender, approval_event, approval_result)
    else:
        approval_result.clear()
        approval_result["decision"] = "allow_once"
        approval_event.set()
        rerender()


def _select_pending_choice(
    state: ScreenState,
    choice: dict,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """Select a permission choice and resolve."""
    pending = state.pending_approval
    decision = choice.get("decision", "allow_once")
    
    if decision == "deny_with_feedback":
        pending.feedback_mode = True
        pending.feedback_input = ""
        rerender()
        return
    
    approval_result.clear()
    approval_result["decision"] = decision
    approval_event.set()
    rerender()


# ---------------------------------------------------------------------------
# Normal mode event handlers
# ---------------------------------------------------------------------------


def _handle_normal_mode_event(
    args: TtyAppArgs,
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
) -> None:
    """Handle input events in normal mode (no pending approval)."""
    visible_commands = _get_visible_commands(state.input)
    
    if isinstance(event, KeyEvent):
        if _handle_normal_mode_key(args, state, event, visible_commands, rerender):
            return
    elif isinstance(event, TextEvent):
        if _handle_normal_mode_text(args, state, event, visible_commands, rerender):
            return
    elif isinstance(event, WheelEvent):
        if _handle_normal_mode_wheel(args, state, event, rerender):
            return


def _handle_normal_mode_key(
    args: TtyAppArgs,
    state: ScreenState,
    event: KeyEvent,
    visible_commands: list,
    rerender: Callable[[], None],
) -> bool:
    """Handle key events in normal mode. Returns True if handled."""
    # Return -> submit input or select slash command
    if event.name == "return":
        _handle_normal_mode_return(args, state, visible_commands, rerender)
        return True
    
    # Tab -> autocomplete slash command
    if event.name == "tab" and visible_commands:
        _handle_normal_mode_tab(state, visible_commands, rerender)
        return True
    
    # Navigation and editing keys
    if _handle_normal_mode_navigation(state, event, rerender):
        return True
    
    # Ctrl shortcuts (P, N handled in text handler)
    # PageUp/PageDown -> scroll transcript
    if event.name == "pageup" and _scroll_transcript_by(args, state, 8):
        rerender()
        return True
    
    if event.name == "pagedown" and _scroll_transcript_by(args, state, -8):
        rerender()
        return True
    
    # Up/Down arrows (history or command selection)
    if event.name == "up":
        _handle_up_arrow(args, state, visible_commands, rerender)
        return True
    
    if event.name == "down":
        _handle_down_arrow(args, state, visible_commands, rerender)
        return True
    
    return False


def _handle_normal_mode_return(
    args: TtyAppArgs,
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    """Handle Return key in normal mode."""
    if visible_commands and 0 <= state.selected_slash_index < len(visible_commands):
        selected = visible_commands[state.selected_slash_index]
        usage = getattr(selected, "usage", str(selected))
        # If input already matches the command exactly, submit instead of completing
        if state.input.strip() == usage:
            state.selected_slash_index = 0
            submitted = state.input
            state.input = ""
            state.cursor_offset = 0
            rerender()
            if _handle_input(args, state, rerender, submitted):
                raise SystemExit(0)
            rerender()
            return
        # Otherwise, complete the command in the input field
        state.input = usage
        state.cursor_offset = len(state.input)
        state.selected_slash_index = 0
        rerender()
        return
    
    submitted = state.input
    state.input = ""
    state.cursor_offset = 0
    state.selected_slash_index = 0
    rerender()
    if _handle_input(args, state, rerender, submitted):
        raise SystemExit(0)
    rerender()


def _handle_normal_mode_tab(
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    """Handle Tab key for slash command autocompletion."""
    selected = visible_commands[min(state.selected_slash_index, len(visible_commands) - 1)]
    usage = getattr(selected, "usage", str(selected))
    state.input = usage + " "
    state.cursor_offset = len(state.input)
    state.selected_slash_index = 0
    rerender()


def _handle_normal_mode_navigation(
    state: ScreenState,
    event: KeyEvent,
    rerender: Callable[[], None],
) -> bool:
    """Handle navigation and editing keys. Returns True if handled."""
    if event.name == "backspace" and state.cursor_offset > 0:
        state.input = state.input[:state.cursor_offset - 1] + state.input[state.cursor_offset:]
        state.cursor_offset -= 1
        state.selected_slash_index = 0
        rerender()
        return True
    
    if event.name == "delete" and state.cursor_offset < len(state.input):
        state.input = state.input[:state.cursor_offset] + state.input[state.cursor_offset + 1:]
        state.selected_slash_index = 0
        rerender()
        return True
    
    if event.name == "home":
        state.cursor_offset = 0
        rerender()
        return True
    
    if event.name == "end":
        state.cursor_offset = len(state.input)
        rerender()
        return True
    
    if event.name == "left":
        state.cursor_offset = max(0, state.cursor_offset - 1)
        rerender()
        return True
    
    if event.name == "right":
        state.cursor_offset = min(len(state.input), state.cursor_offset + 1)
        rerender()
        return True
    
    if event.name == "escape":
        state.input = ""
        state.cursor_offset = 0
        state.selected_slash_index = 0
        rerender()
        return True
    
    return False


def _handle_up_arrow(
    args: TtyAppArgs,
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    """Handle Up arrow key."""
    if visible_commands:
        state.selected_slash_index = (state.selected_slash_index - 1 + len(visible_commands)) % len(visible_commands)
        rerender()
    elif _history_up(state):
        rerender()


def _handle_down_arrow(
    args: TtyAppArgs,
    state: ScreenState,
    visible_commands: list,
    rerender: Callable[[], None],
) -> None:
    """Handle Down arrow key."""
    if visible_commands:
        state.selected_slash_index = (state.selected_slash_index + 1) % len(visible_commands)
        rerender()
    elif _history_down(state):
        rerender()


def _handle_normal_mode_text(
    args: TtyAppArgs,
    state: ScreenState,
    event: TextEvent,
    visible_commands: list,
    rerender: Callable[[], None],
) -> bool:
    """Handle text events in normal mode. Returns True if handled."""
    # Digit quick-select for slash commands (1-9)
    if visible_commands and event.text.isdigit() and not event.ctrl:
        idx = int(event.text) - 1
        if 0 <= idx < len(visible_commands):
            selected = visible_commands[idx]
            usage = getattr(selected, "usage", str(selected))
            state.input = usage
            state.cursor_offset = len(state.input)
            state.selected_slash_index = 0
            rerender()
            return True

    # Ctrl shortcuts
    if event.ctrl:
        if event.text == "u":  # Ctrl-U -> clear line
            state.input = ""
            state.cursor_offset = 0
            state.selected_slash_index = 0
            rerender()
            return True
        
        if event.text == "a":  # Ctrl-A -> home / jump to top
            if not state.input:
                if _jump_transcript_to_edge(args, state, "top"):
                    rerender()
                return True
            state.cursor_offset = 0
            rerender()
            return True
        
        if event.text == "e":  # Ctrl-E -> end / jump to bottom
            if not state.input:
                if _jump_transcript_to_edge(args, state, "bottom"):
                    rerender()
                return True
            state.cursor_offset = len(state.input)
            rerender()
            return True
        
        if event.text == "p":  # Ctrl-P -> history up
            if _history_up(state):
                rerender()
            return True
        
        if event.text == "n":  # Ctrl-N -> history down
            if _history_down(state):
                rerender()
            return True
        
        return False
    
    # Regular text input (accept any non-empty text, including multi-byte CJK/emoji)
    if not event.ctrl and event.text:
        state.input = state.input[:state.cursor_offset] + event.text + state.input[state.cursor_offset:]
        state.cursor_offset += len(event.text)
        state.selected_slash_index = 0
        state.history_index = len(state.history)
        rerender()
        return True
    
    return False


def _handle_normal_mode_wheel(
    args: TtyAppArgs,
    state: ScreenState,
    event: WheelEvent,
    rerender: Callable[[], None],
) -> bool:
    """Handle wheel events in normal mode for scrolling. Returns True if handled."""
    delta = 3 if event.direction == "up" else -3
    if _scroll_transcript_by(args, state, delta):
        rerender()
        return True
    return False


# ---------------------------------------------------------------------------
# Public API / backward-compatible exports for tests
# ---------------------------------------------------------------------------


def summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """Generate a human-readable summary of tool input.
    
    Public wrapper around _summarize_tool_input for external callers.
    
    Args:
        tool_name: Name of the tool being called
        tool_input: Input dictionary passed to the tool
        
    Returns:
        Human-readable summary string for display in transcript
    """
    return _summarize_tool_input(tool_name, tool_input)


def summarize_tool_output(tool_name: str, output: str) -> str:
    """Summarize tool output for collapsed display.
    
    Picks the first meaningful line and truncates to 140 characters.
    
    Args:
        tool_name: Name of the tool (unused but kept for API consistency)
        output: Full tool output string
        
    Returns:
        Truncated summary suitable for collapsed tool display
    """
    return _summarize_collapsed_tool_body(output)


def _format_history(entries: list[str], limit: int = 20) -> str:
    """Format recent history entries with 1-based numbers."""
    start = max(0, len(entries) - limit)
    return "\n".join(
        f"{start + i + 1}. {entry}" for i, entry in enumerate(entries[start:])
    )


def _save_transcript(state_obj: Any, cwd: str, permissions: PermissionManager, output_path: str) -> str:
    """Save transcript entries to file. Returns the resolved path string."""
    from pepsicode.tui.transcript import format_transcript_text

    target = resolve_tool_path(ToolContext(cwd=cwd, permissions=permissions), output_path, "write")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_transcript_text(state_obj.transcript), encoding="utf-8")
    return str(target)


def _apply_tool_result_visual_state(
    entry: TranscriptEntry,
    tool_name: str,
    output: str,
    is_error: bool,
) -> None:
    """Apply tool result visual state to a transcript entry."""
    entry.status = "error" if is_error else "success"
    entry.body = f"ERROR: {output}" if is_error else output
    if is_error:
        entry.collapsed = False
        entry.collapsedSummary = None
        entry.collapsePhase = None
    else:
        entry.collapsed = True
        entry.collapsedSummary = _summarize_collapsed_tool_body(output)
        entry.collapsePhase = 3


def _mark_unfinished_tools(state_obj: Any) -> int:
    """Mark running tool entries as errors and clean up state. Returns count of affected entries."""
    count = 0
    for entry in state_obj.transcript:
        if entry.kind == "tool" and entry.status == "running":
            entry.status = "error"
            entry.body = (
                f"{entry.body}\n\n"
                "ERROR: Tool did not report a final result before the turn ended. "
                "This usually means the command kept running in the background "
                "or the tool lifecycle got out of sync."
            )
            entry.collapsed = False
            entry.collapsedSummary = None
            entry.collapsePhase = None
            state_obj.recent_tools.append({"name": entry.toolName or "unknown", "status": "error"})
            count += 1
    if hasattr(state_obj, "pending_tool_runs"):
        state_obj.pending_tool_runs = {}
    state_obj.active_tool = None
    return count


def _handle_feedback_mode_event(
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """Handle events when in feedback mode (rejection guidance input)."""
    pending = state.pending_approval
    if not pending:
        return

    if isinstance(event, KeyEvent):
        if event.name == "escape":
            pending.feedback_mode = False
            pending.feedback_input = ""
            rerender()
            return
        if event.name == "return":
            approval_result.clear()
            approval_result["decision"] = "deny_with_feedback"
            approval_result["feedback"] = pending.feedback_input
            approval_event.set()
            rerender()
            return
        if event.name == "backspace":
            if pending.feedback_input:
                pending.feedback_input = pending.feedback_input[:-1]
                rerender()
            return

    if isinstance(event, TextEvent) and not event.ctrl:
        pending.feedback_input += event.text
        rerender()
