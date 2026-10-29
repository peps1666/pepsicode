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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pepsicode.cli.cli_commands import (
    expand_prompt_command,
    find_matching_slash_commands,
    get_slash_commands,
    try_handle_local_command,
)
from pepsicode.cli.local_tool_shortcuts import parse_local_tool_shortcut
from pepsicode.cli.model_commands import update_session_model_settings
from pepsicode.context.context_manager import ContextManager
from pepsicode.context.history import load_history_entries, save_history_entries
from pepsicode.context.prompt import build_session_prompt
from pepsicode.core.agent_loop import run_agent_turn_stream
from pepsicode.core.background_tasks import list_background_tasks
from pepsicode.core.input_queue import InputMode, TurnInputQueue
from pepsicode.core.session import (
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
    same_workspace,
    save_session,
)
from pepsicode.core.session_tree import (
    checkout_entry,
    copy_session,
    format_tree,
    prepare_regeneration,
    rebuild_transcript,
    record_messages,
    recording_session,
)
from pepsicode.core.state import AppState, Store, create_app_store
from pepsicode.core.verification import restore_verification_state, run_verified_turn, snapshot_verification_state
from pepsicode.hooks import HookContext, HookEngine, HookEvent
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.llm.assistant_text import parse_assistant_text
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.llm.model_discovery import refresh_model_catalog
from pepsicode.llm.model_settings import (
    apply_model_settings,
    model_capabilities,
    model_display_name,
    model_selection_error,
)
from pepsicode.llm.provider_setup import probe_provider_setup, save_provider_setup
from pepsicode.llm.providers import PROVIDERS, model_settings_snapshot, provider_runtime
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools import create_default_tool_registry
from pepsicode.tui.chrome import (
    ACCENT,
    BOLD,
    BRIGHT_RED,
    BRIGHT_WHITE,
    DIM,
    HIGHLIGHT_BG,
    ICON_ARROW,
    ICON_DOT,
    ITALIC,
    RESET,
    SUBTLE,
    YELLOW,
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
)
from pepsicode.tui.clipboard import copy_to_clipboard
from pepsicode.tui.input import render_input_prompt
from pepsicode.tui.input_parser import (
    KeyEvent,
    MouseEvent,
    ParsedInputEvent,
    PasteEvent,
    TerminalInputDecoder,
    TextEvent,
    WheelEvent,
)
from pepsicode.tui.markdown import render_markdownish
from pepsicode.tui.provider_wizard import ProviderWizard, create_provider_wizard, render_provider_wizard, wizard_event
from pepsicode.tui.screen import (
    FrameRenderer,
    Selection,
    enter_alternate_screen,
    exit_alternate_screen,
    hide_cursor,
    show_cursor,
)
from pepsicode.tui.settings_picker import (
    SettingsPicker,
    create_permissions_picker,
    create_settings_picker,
    render_settings_picker,
)
from pepsicode.tui.transcript import (
    get_transcript_max_scroll_offset,
    render_tool_activity,
    render_transcript,
)
from pepsicode.tui.types import TranscriptEntry
from pepsicode.types import ChatMessage, ModelAdapter, StreamToken
from pepsicode.usage import format_usage_status, usage_snapshot
from pepsicode.version import DISPLAY_VERSION
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
    # Shared cost tracker -- when provided by the caller the TTY app reuses it
    # so sub-agent token usage (recorded via the Task tool) is visible to the
    # session-level budget guard.  When ``None`` a fresh tracker is created.
    cost_tracker: CostTracker | None = None
    # Trace manager for sub-agent observability (token/tool-call tree).
    trace_manager: Any | None = None
    hook_engine: HookEngine | None = None
    memory: Any | None = None


def _emit_tty_hook(
    args: TtyAppArgs,
    event: HookEvent,
    *,
    data: dict[str, Any] | None = None,
) -> None:
    if args.hook_engine is None:
        return
    mode = getattr(args.permissions.mode, "value", str(args.permissions.mode))
    try:
        args.hook_engine.emit(
            event,
            HookContext(
                event=event,
                cwd=args.cwd,
                data=data or {},
                metadata={"permission_mode": mode, "agent_scope": "main"},
            ),
        )
    except Exception as error:  # noqa: BLE001 - UI hooks are isolated
        logging.debug("Hook event %s failed: %s", event.value, error)


def _drain_tty_hook_notifications(args: TtyAppArgs, state: ScreenState) -> None:
    if args.hook_engine is None:
        return
    for notification in args.hook_engine.drain_notifications():
        marker = "ok" if notification.success else "error"
        _push_transcript_entry(
            state,
            kind="assistant",
            body=f"[hook:{notification.hook_id} {marker}] {notification.output}",
        )


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
    latest_usage: dict | None = None
    usage_stats_key: tuple | None = None
    usage_stats: dict | None = None
    input: str = ""
    cursor_offset: int = 0
    transcript: list[TranscriptEntry] = field(default_factory=list)
    transcript_scroll_offset: int = 0
    transcript_total_lines: int = 0
    # Rows and width of the last rendered stream transcript viewport.
    transcript_view_height: int = 0
    transcript_layout_cols: int = 0
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
    settings_picker: SettingsPicker | None = None
    provider_wizard: ProviderWizard | None = None
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
    cancellation_event: threading.Event = field(default_factory=threading.Event, repr=False)
    input_queue: TurnInputQueue = field(default_factory=TurnInputQueue, repr=False)
    pending_inputs: list[dict] = field(default_factory=list)
    returned_inputs: list[str] = field(default_factory=list)
    # Tool execution timing tracker
    tool_start_time: float | None = None
    # Streaming state: the entry currently being streamed into (if any).
    streaming_entry_id: int | None = None
    streaming_text: str = ""
    # The model's reasoning streams into its own entry, separate from answer text.
    thinking_entry_id: int | None = None
    frame_renderer: FrameRenderer = field(default_factory=FrameRenderer, repr=False)
    # Mouse text selection (drawn by frame_renderer.selection).
    selection_dragging: bool = False
    selection_notice: str | None = None
    exit_requested_at: float | None = None
    # Finished turns fold their tool/progress activity behind one header row.
    # Keys are the first entry id of each activity run.
    expanded_activity_groups: set[int] = field(default_factory=set)
    # Screen row -> activity group id for headers visible in the last frame.
    activity_header_rows: dict[int, int] = field(default_factory=dict)
    # Keep a toggled header on the row it was on: (group id, screen row).
    activity_anchor: tuple[int, int] | None = None


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _snapshot_session(args: TtyAppArgs, state: ScreenState) -> None:
    """Copy live state before every save, including an in-progress transcript."""
    if state.session is None:
        return
    if not state.is_busy:
        record_messages(state.session, args.messages)
    with state.session._tree_lock:
        live = state.session.messages if state.is_busy and state.session.tree else args.messages
        messages = [dict(message) for message in live]
    transcript = [
        {
            "id": entry.id,
            "kind": entry.kind,
            "toolName": entry.toolName,
            "status": entry.status,
            "body": entry.body,
            "collapsed": entry.collapsed,
            "collapsedSummary": entry.collapsedSummary,
            "collapsePhase": entry.collapsePhase,
            "toolSummary": entry.toolSummary,
        }
        for entry in list(state.transcript)
    ]
    verification = snapshot_verification_state(args.tools, args.runtime)
    if state.autosave and (
        state.session.messages != messages
        or state.session.transcript_entries != transcript
        or state.session.history != state.history
        or state.session.verification_state != verification
    ):
        state.autosave.mark_dirty()
    state.session.messages = messages
    state.session.verification_state = verification
    state.session.transcript_entries = transcript
    state.session.history = list(state.history)
    state.session.permissions_summary = args.permissions.get_summary()
    state.session.skills = args.tools.get_skills()
    state.session.mcp_servers = args.tools.get_mcp_servers()
    state.session.access_mode = args.permissions.access_mode.value
    state.session.permission_mode = args.permissions.mode.value
    state.session.plan_file_path = args.permissions.plan_file_path
    if args.runtime and not isinstance(args.model, MockModelAdapter):
        state.session.model_settings = model_settings_snapshot(args.runtime)


def _collect_agent_result(args: TtyAppArgs, state: ScreenState) -> bool:
    result, lock = state.agent_result, state.agent_lock
    if result is None or lock is None:
        return False
    with lock:
        if not result.get("done"):
            return False
        if result.get("messages") is not None:
            args.messages = result["messages"]
        result["done"] = False
    if state.returned_inputs:
        state.input = "\n\n".join([*state.returned_inputs, *([state.input] if state.input else [])])
        state.returned_inputs = []
        state.cursor_offset = len(state.input)
    _snapshot_session(args, state)
    if state.autosave:
        state.autosave.mark_dirty()
    return True


def _sync_model_settings_state(args: TtyAppArgs, state: ScreenState) -> None:
    if not args.runtime or isinstance(args.model, MockModelAdapter):
        return
    if state.session:
        state.session.model_settings = model_settings_snapshot(args.runtime)
        state.session.messages = list(args.messages)
        if state.autosave:
            state.autosave.mark_dirty()
    if state.app_state:
        state.app_state.set_state(lambda current: replace(current, model=args.runtime["model"]))


def _open_settings_picker(
    args: TtyAppArgs,
    state: ScreenState,
    kind: str,
    *,
    notice: str = "",
    return_input: str = "",
    provider: str | None = None,
) -> None:
    if kind == "permissions":
        state.settings_picker = create_permissions_picker(args.permissions.access_mode.value)
        state.input = ""
        state.cursor_offset = 0
        return
    if kind != "provider" and (not args.runtime or isinstance(args.model, MockModelAdapter)):
        _push_transcript_entry(
            state, kind="assistant", status="error", body="Mock 模式下无法选择模型或思考强度，请先配置 API。"
        )
        return
    try:
        candidate = provider_runtime(args.runtime or {"model": "mock"}, provider)
        if kind != "provider":
            refresh_model_catalog(candidate)
        state.settings_picker = create_settings_picker(candidate, kind, notice=notice, return_input=return_input)
    except ValueError as error:
        _push_transcript_entry(state, kind="assistant", status="error", body=str(error))
        return
    state.input = ""
    state.cursor_offset = 0


def _handle_settings_picker_event(
    args: TtyAppArgs, state: ScreenState, event: ParsedInputEvent, rerender: Callable[[], None]
) -> None:
    picker = state.settings_picker
    if picker is None:
        return
    if picker.kind == "permissions":
        if isinstance(event, KeyEvent):
            if event.name == "escape":
                state.settings_picker = None
            elif event.name in {"up", "down"}:
                picker.selected = (picker.selected + (-1 if event.name == "up" else 1)) % len(picker.options)
            elif event.name == "return":
                option = picker.options[picker.selected]
                try:
                    args.permissions.set_access_mode(option.value)
                    if state.session:
                        state.session.access_mode = option.value
                        state.session.permission_mode = args.permissions.mode.value
                        state.session.plan_file_path = args.permissions.plan_file_path
                        if state.autosave:
                            state.autosave.mark_dirty()
                            state.autosave.force_save()
                except (ValueError, OSError) as error:
                    picker.error = str(error)
                else:
                    _push_transcript_entry(state, kind="assistant", body=f"权限模式：{option.label} √")
                    state.settings_picker = None
        rerender()
        return
    if isinstance(event, TextEvent) and not event.ctrl and event.text.lower() == "r":
        candidate = provider_runtime(args.runtime or {"model": "mock"}, picker.provider)
        if picker.kind != "provider":
            refresh_model_catalog(candidate, force=True)
        updated = create_settings_picker(candidate, picker.kind, return_input=picker.return_input)
        updated.persist = picker.persist
        state.settings_picker = updated
        rerender()
        return
    if isinstance(event, TextEvent) and not event.ctrl and event.text.lower() == "s":
        picker.persist = not picker.persist
    elif isinstance(event, KeyEvent):
        if event.name == "escape":
            state.input = picker.return_input
            state.cursor_offset = len(state.input)
            state.settings_picker = None
        elif event.name in {"up", "down"} and picker.options:
            picker.selected = (picker.selected + (-1 if event.name == "up" else 1)) % len(picker.options)
        elif event.name == "return" and picker.options:
            option = picker.options[picker.selected]
            if picker.kind == "provider":
                if option.value == "__add__":
                    state.settings_picker = None
                    state.provider_wizard = create_provider_wizard("custom")
                    rerender()
                    return
                try:
                    candidate = provider_runtime(args.runtime or {"model": "mock"}, option.value)
                    refresh_model_catalog(candidate)
                    updated = create_settings_picker(candidate, "model", return_input=picker.return_input)
                    updated.persist = picker.persist
                    state.settings_picker = updated
                except ValueError as error:
                    state.settings_picker = None
                    state.provider_wizard = create_provider_wizard(option.value)
                rerender()
                return
            model = option.value if picker.kind == "model" else args.runtime["model"]
            effort = (
                option.value
                if picker.kind == "effort"
                else (
                    args.runtime.get("reasoningEffort", "default")
                    if model == args.runtime["model"] and picker.provider == args.runtime.get("provider")
                    else "default"
                )
            )
            try:
                args.permissions.ensure_local_command_allowed(f"/{picker.kind} {option.value}")
                update_session_model_settings(
                    runtime=args.runtime,
                    model_adapter=args.model,
                    model=model,
                    effort=effort,
                    persist=picker.persist,
                    messages=args.messages,
                    context_manager=args.context_manager,
                    provider=picker.provider,
                )
            except (ValueError, OSError, RuntimeError) as error:
                picker.error = str(error)
            else:
                state.latest_usage = None
                _sync_model_settings_state(args, state)
                label = f"模型：{model}" if picker.kind == "model" else f"思考强度：{option.label}（{option.value}）"
                scope = "已保存为新会话默认设置。" if picker.persist else "仅用于当前会话。"
                _push_transcript_entry(state, kind="assistant", body=f"{label}。下一轮生效，{scope}")
                state.input = picker.return_input
                state.cursor_offset = len(state.input)
                state.settings_picker = None
    rerender()


def _handle_provider_wizard_event(
    args: TtyAppArgs, state: ScreenState, event: ParsedInputEvent, rerender: Callable[[], None]
) -> None:
    wizard = state.provider_wizard
    action = wizard_event(wizard, event)
    if action == "cancel":
        state.provider_wizard = None
    elif action in {"probe", "save"}:
        try:
            args.permissions.ensure_local_command_allowed("/provider add")
            if action == "probe":
                wizard.candidate = probe_provider_setup(wizard.draft)
                wizard.move(5)
            else:
                if state.is_busy:
                    raise ValueError("请等待当前对话结束后再保存")
                runtime = args.runtime if args.runtime is not None else {"model": ""}
                save_provider_setup(runtime, wizard.draft, wizard.candidate)
                args.runtime = runtime
                # Bootstrap first-run mock fallback into a real adapter without dropping history.
                if isinstance(args.model, MockModelAdapter):
                    old_tools = args.tools
                    verification = snapshot_verification_state(old_tools, args.runtime)
                    args.tools = create_default_tool_registry(
                        args.cwd, runtime=runtime, cost_tracker=args.cost_tracker, trace_manager=args.trace_manager
                    )
                    args.model = AnthropicModelAdapter(runtime, args.tools)
                    restore_verification_state(args.tools, runtime, verification)
                    old_tools.dispose()
                args.messages[:] = [message for message in args.messages if message.get("role") != "assistant_thinking"]
                if args.context_manager is None:
                    args.context_manager = ContextManager(model=runtime["model"])
                apply_model_settings(runtime, runtime["model"], "default", context_manager=args.context_manager)
                args.context_manager.summarizer = args.model.summarize
                _sync_model_settings_state(args, state)
                _push_transcript_entry(
                    state, kind="assistant", body=f"已保存并启用 {runtime['providerName']}，模型：{runtime['model']}。"
                )
                wizard.draft.clear()
                wizard.candidate = None
                wizard.text, wizard.cursor = "", 0
                state.provider_wizard = None
        except (ValueError, OSError, RuntimeError) as error:
            wizard.error = str(error)
    rerender()


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
    if kwargs.get("kind") == "tool":
        kwargs.setdefault("toolSummary", kwargs.get("body"))
    state.transcript.append(TranscriptEntry(id=entry_id, **kwargs))
    return entry_id


def _session_rows_for_picker(cwd: str) -> list[SessionMetadata]:
    return list_sessions(workspace=cwd)


def _restore_model_settings(args: TtyAppArgs, session: SessionData) -> None:
    if not session.model_settings or not args.runtime or isinstance(args.model, MockModelAdapter):
        return
    try:
        apply_model_settings(
            args.runtime,
            session.model_settings["model"],
            session.model_settings.get("reasoningEffort", "default"),
            context_manager=args.context_manager,
            provider=session.model_settings.get("provider"),
        )
    except (KeyError, ValueError) as error:
        # A removed provider profile must not make an old conversation impossible to open.
        logging.getLogger(__name__).warning("Could not restore session model settings: %s", error)


def _restore_session_into_state(
    args: TtyAppArgs,
    state: ScreenState,
    session: SessionData,
) -> None:
    if not same_workspace(args.cwd, session.workspace):
        raise ValueError(f"Session belongs to {session.workspace}. Change to that directory before resuming it.")
    args.messages = list(session.messages)
    restore_verification_state(args.tools, args.runtime, session.verification_state, session.messages)
    state.latest_usage = None
    state.usage_stats_key = None
    if args.context_manager is not None:
        args.context_manager.select_branch(args.messages, args.cwd, args.permissions)
    args.permissions.mode = args.permissions.access_mode
    args.permissions.plan_file_path = None
    args.permissions.set_access_mode(session.access_mode)
    args.permissions.restore_plan_state(session.permission_mode, session.plan_file_path)
    _restore_model_settings(args, session)
    state.session = session
    state.autosave = AutosaveManager(session)
    if not session.transcript_entries and session.messages:
        rebuild_transcript(session)
    state.transcript = [TranscriptEntry(**entry) for entry in session.transcript_entries]
    state.next_entry_id = max((entry.id for entry in state.transcript), default=0) + 1
    state.expanded_activity_groups.clear()
    state.activity_anchor = None
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
            app_state.model = args.runtime.get("model", "unknown") if args.runtime else "unknown"
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
    try:
        _restore_session_into_state(args, state, session)
    except ValueError as error:
        state.status = str(error)
        _push_transcript_entry(state, kind="assistant", body=str(error))
        return False
    _push_transcript_entry(
        state,
        kind="assistant",
        body=f"Session {session.session_id[:8]} resumed ({len(session.messages)} messages loaded).",
    )
    _emit_tty_hook(
        args,
        HookEvent.SESSION_RESUME,
        data={"session_id": session.session_id, "message_count": len(session.messages)},
    )
    _drain_tty_hook_notifications(args, state)
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
        (line.strip() for line in output.split("\n") if line.strip()),
        "output collapsed",
    )
    return line[:140] + "..." if len(line) > 140 else line


def _finish_tool_entry(state: ScreenState, entry_id: int, status: str, body: str) -> None:
    """Store successful output collapsed, keeping failures visible.

    Called from the agent thread while the UI thread renders. Collapsing
    before the status/body change means no frame ever shows the full output
    for an instant and then shrinks it, which made the screen jump.
    """
    for entry in state.transcript:
        if entry.id == entry_id and entry.kind == "tool":
            entry.collapsedSummary = _summarize_collapsed_tool_body(body) if status == "success" else None
            entry.collapsePhase = None
            entry.collapsed = status == "success"
            entry.body = body
            entry.status = status
            return


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
_PROMPT_LINES_ESTIMATE = 7  # prompt panel: top border + title + divider + 3 body + bottom border
_FOOTER_LINES = 2  # status line plus optional contextual help
_GAPS = 4
_TRANSCRIPT_FRAME_LINES = 4  # top/bottom border + title + empty


def _get_transcript_body_lines(args: TtyAppArgs, state: ScreenState) -> int:
    _, rows = _get_terminal_size()
    rows = max(12, rows)
    # Use cached estimates instead of re-rendering header/prompt just to count lines
    chrome_overhead = _HEADER_LINES_ESTIMATE + _PROMPT_LINES_ESTIMATE + _FOOTER_LINES + _GAPS + _TRANSCRIPT_FRAME_LINES
    return max(1, rows - chrome_overhead)


def _get_stream_max_scroll_offset(args: TtyAppArgs, state: ScreenState) -> int:
    """Compute max scroll offset using the stream renderer's actual line counts."""
    if state.transcript_view_height > 0:
        return max(0, state.transcript_total_lines - state.transcript_view_height)
    _, rows = _get_terminal_size()
    rows = max(10, rows)
    # Reserve space for input (2 lines), slash menu if visible, separator, footer
    input_height = 2
    visible_commands = _get_visible_commands(state.input, args.cwd)
    if visible_commands and state.input:
        input_height += len(visible_commands) + 3
    reserved = input_height + 3
    transcript_height = max(1, rows - reserved)
    return max(0, state.transcript_total_lines - transcript_height)


def _get_max_transcript_scroll_offset(args: TtyAppArgs, state: ScreenState) -> int:
    return get_transcript_max_scroll_offset(state.transcript, _get_transcript_body_lines(args, state))


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
    if not pending or (not pending.details_expanded and pending.request.get("kind") != "command"):
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


def _get_visible_commands(input_text: str, cwd: str | None = None) -> list[Any]:
    if not input_text.startswith("/"):
        return []
    if input_text == "/":
        return get_slash_commands(cwd)
    matches = find_matching_slash_commands(input_text, cwd)
    return [cmd for cmd in get_slash_commands(cwd) if getattr(cmd, "usage", str(cmd)) in matches]


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
    mode: str = "auto",
    plan: bool = False,
) -> str:
    """Render the bottom status bar with caching to reduce flicker.

    Shows current operation status, tool/skill availability, and background tasks.
    """
    cache_key = (
        status,
        tools_enabled,
        skills_enabled,
        len(background_tasks),
        mode,
        plan,
        _cached_terminal_size(),
    )
    cached = _footer_cache.get("key")
    if cached and cached[0] == cache_key:
        return cached[1]
    result = render_footer_bar(status, tools_enabled, skills_enabled, background_tasks, mode, plan)
    _footer_cache["key"] = (cache_key, result)
    return result


def _render_prompt_panel(state: ScreenState, cwd: str | None = None) -> str:
    commands = _get_visible_commands(state.input, cwd)
    cols, _ = _get_terminal_size()
    prompt_body = render_input_prompt(state.input, state.cursor_offset, width=max(5, cols - 4))
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
    from pepsicode.tui.chrome import string_display_width, strip_ansi

    return string_display_width(strip_ansi(text))


def _pad_visible(text: str, cols: int) -> str:
    return text + " " * max(0, cols - _visible_width(text))


def _center_line(text: str, cols: int) -> str:
    pad = max(0, (cols - _visible_width(text)) // 2)
    return " " * pad + text


def _paint_line(text: str, cols: int, bg: str) -> str:
    painted = text.replace(RESET, RESET + bg) if RESET else text
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
    """Render a compact wordmark and terminal-safe mascot."""
    lines = [
        f"{ACCENT}{BOLD}pepsicode{RESET}  {SUBTLE}{DISPLAY_VERSION}{RESET}",
        f"{SUBTLE}terminal coding agent{RESET}",
    ]
    if rows >= 22 and cols >= 58:
        lines += [
            "",
            f"{ACCENT} /\\_/\\{RESET}",
            f"{ACCENT}( o.o ){RESET}  {SUBTLE}Ready to explore your code.{RESET}",
            f"{ACCENT} > ^ <{RESET}",
        ]
    return lines


def _permission_mode_label(args: TtyAppArgs) -> str:
    permissions = getattr(args, "permissions", None)
    mode = getattr(getattr(permissions, "access_mode", None), "value", "auto")
    risk = " 高风险" if mode == "trust_commands" else ""
    plan = " PLAN" if getattr(permissions, "is_plan_mode", False) else ""
    return f"{YELLOW}mode: {mode}{risk}{plan}{RESET}"


def _render_home_screen(args: TtyAppArgs, state: ScreenState, cols: int, rows: int) -> None:
    """Fill the viewport with welcome content above a bottom-anchored composer."""
    runtime = args.runtime or {}
    model = f"{model_display_name(runtime) if runtime else 'unknown'} · {runtime.get('reasoningEffort', 'default')}"
    visible_commands = _get_visible_commands(state.input, args.cwd)
    block_width = max(4, cols - 4)
    margin = "  "
    content = _render_home_brand(cols, rows)
    if rows < 10:
        content = content[:1]
    if rows >= 18 and not state.input.startswith("/"):
        content += [
            "",
            f"{BOLD}Start with an idea{RESET}",
            f"{SUBTLE}{ICON_DOT} Explore the codebase   {ICON_DOT} Fix a bug   {ICON_DOT} Add tests{RESET}",
        ]
    composer = [
        f"{SUBTLE}{'─' * block_width}{RESET}",
        render_input_prompt(state.input, state.cursor_offset, width=block_width),
        f"{SUBTLE}{'─' * block_width}{RESET}",
        f"{SUBTLE}{model}{RESET}",
        _render_usage_status(args, state, block_width),
    ]
    menu: list[str] = []
    if visible_commands and state.input:
        # Expand upward so opening/filtering the menu does not move the input.
        menu = render_slash_menu(
            visible_commands,
            min(state.selected_slash_index, len(visible_commands) - 1),
            max_rows=max(1, min(10, rows - len(composer) - 2)),
            width=block_width,
        ).splitlines()
    welcome_height = max(0, rows - len(composer) - len(menu) - 2)
    frame = [margin + line for line in content[:welcome_height]]
    frame += [""] * (welcome_height - len(frame))
    frame += [margin + line for line in menu + composer]
    frame.append(f"  {SUBTLE}{Path(args.cwd)}{RESET}")
    exit_hint = (
        "Press Ctrl+C again to exit" if state.exit_requested_at is not None else "/help for commands · /exit to quit"
    )
    frame.append(f"  {_permission_mode_label(args)}  {SUBTLE}{exit_hint}{RESET}")
    frame = _truncate_frame_lines(frame, cols, rows)
    state.frame_renderer.draw(frame, cols, rows)


def _render_usage_status(args: TtyAppArgs, state: ScreenState, width: int) -> str:
    manager = getattr(args, "context_manager", None)
    if manager is not None and not state.is_busy:
        manager.messages = list(args.messages)
    key = (id(state.latest_usage),)
    if manager is not None:
        key += (
            len(manager.messages),
            manager.actual_input_tokens,
            manager.actual_output_tokens,
            manager.context_window,
            manager.context_window_is_estimate,
        )
    # Streaming text repaints at 30fps; only rescan the context when its
    # messages, provider counts, or model capacity actually change.
    if state.usage_stats is None or state.usage_stats_key != key:
        state.usage_stats = usage_snapshot(manager, state.latest_usage)
        state.usage_stats_key = key
    snapshot = state.usage_stats
    label = format_usage_status(snapshot)
    if _visible_width(label) > width:
        rate = snapshot["cache_hit_percentage"]
        context = snapshot["context"]
        cache = f"{rate:.0f}%" if rate is not None else "--"
        percent = f"{context['usage_percentage']:.0f}%" if context else "--"
        estimated = "~" if context and (context["is_estimate"] or context["window_is_estimate"]) else ""
        label = f"Cache {cache} · Ctx {estimated}{percent}"
    return f"{SUBTLE}{truncate_plain(label, width)}{RESET}"


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
    prompt = " " + render_input_prompt(state.input, state.cursor_offset, width=max(5, cols - 2))

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
        queued = len(getattr(state, "pending_inputs", []))
        lines.append(f"  {SUBTLE}Enter: steer · Alt+Enter or /queue: follow-up · {queued} queued · Ctrl+C: stop{RESET}")
    else:
        lines.append(f"  {SUBTLE}{model}{RESET}")

    return lines


def _render_stream_transcript(
    entries: list[TranscriptEntry], scroll_offset: int, height: int, cols: int
) -> tuple[list[str], int]:
    """Claude-style: simple left rules, no backgrounds.

    Returns (visible_window_lines, total_rendered_lines).
    """
    if not entries:
        return ([f"{SUBTLE}Type a message to get started.{RESET}"][:height], 0)
    rendered = _build_stream_transcript_lines(entries, cols)
    return (_window_stream_transcript(rendered, scroll_offset, height), len(rendered))


def _finished_activity_groups(
    entries: list[TranscriptEntry], streaming_entry_id: int | None = None
) -> dict[int, list[TranscriptEntry]]:
    """Runs of progress/thinking/tool entries whose turn has already produced its answer.

    Keyed by the first entry id of each run, in transcript order. A run that is
    still streaming or has a tool running stays open so live work is visible.
    """
    groups: dict[int, list[TranscriptEntry]] = {}
    run: list[TranscriptEntry] = []
    for entry in entries:
        if entry.kind in ("progress", "thinking", "tool"):
            run.append(entry)
            continue
        answered = entry.kind == "assistant" and entry.id != streaming_entry_id
        if run and answered and all(item.status != "running" for item in run):
            groups[run[0].id] = run
        run = []
    return groups


def _render_activity_header(group: list[TranscriptEntry], expanded: bool, cols: int) -> str:
    tools = sum(entry.kind == "tool" for entry in group)
    thoughts = len(group) - tools
    failed = sum(entry.status == "error" for entry in group)
    parts = []
    if tools:
        parts.append(f"{tools} tool{'s' if tools != 1 else ''}")
    if thoughts:
        parts.append(f"{thoughts} thought{'s' if thoughts != 1 else ''}")
    summary = f" {SUBTLE}·{RESET} ".join(f"{SUBTLE}{part}{RESET}" for part in parts)
    if failed:
        summary += f" {SUBTLE}·{RESET} {BRIGHT_RED}{failed} failed{RESET}"
    arrow = "▾" if expanded else "▸"
    hint = f"click/ctrl+o to {'collapse' if expanded else 'expand'}"
    line = f"  {ACCENT}{arrow}{RESET} {BOLD}Activity{RESET}  {summary}  {DIM}{hint}{RESET}"
    return truncate_plain(line, cols)


def _build_stream_transcript_lines(
    entries: list[TranscriptEntry],
    cols: int,
    *,
    expanded_groups: set[int] | frozenset[int] = frozenset(),
    streaming_entry_id: int | None = None,
    header_lines: dict[int, int] | None = None,
) -> list[str]:
    """Render the stream transcript; ``header_lines`` collects line index -> group id."""
    groups = _finished_activity_groups(entries, streaming_entry_id)
    hidden: set[int] = set()
    rendered: list[str] = []
    for entry in entries:
        group = groups.get(entry.id)
        if group is not None:
            expanded = entry.id in expanded_groups
            if header_lines is not None:
                header_lines[len(rendered)] = entry.id
            rendered.append(_render_activity_header(group, expanded, cols))
            rendered.append("")
            if not expanded:
                hidden.update(item.id for item in group)
        if entry.id in hidden:
            continue

        body_lines = (entry.body or "").splitlines() or [""]
        if entry.kind == "user":
            rendered.append(f"  {ACCENT}{ICON_ARROW}{RESET} {BOLD}You{RESET}")
            for line in body_lines:
                rendered.extend(f"     {part}" for part in wrap_text(line, max(2, cols - 7)).split("\n"))
            rendered.append("")
            continue

        if entry.kind == "assistant":
            label = f"{BRIGHT_RED}{BOLD}Error{RESET}" if entry.status == "error" else f"{BOLD}pepsicode{RESET}"
            rendered.append(f"  {ACCENT}{ICON_DOT}{RESET} {label}")
            content, _ = parse_assistant_text(entry.body)
            for line in render_markdownish(content).splitlines() or [""]:
                rendered.extend(f"  {part}" for part in wrap_text(line, max(2, cols - 4)).split("\n"))
            rendered.append("")
            continue

        if entry.kind == "progress":
            content, _ = parse_assistant_text(entry.body)
            body_lines = content.splitlines() or [""]
            for index, line in enumerate(body_lines):
                prefix = f"{ACCENT}{ICON_DOT}{RESET} " if index == 0 else "  "
                for part_index, part in enumerate(wrap_text(line, max(2, cols - 5)).split("\n")):
                    marker = prefix if part_index == 0 else "  "
                    rendered.append(f"  {marker}{SUBTLE}{part}{RESET}")
            rendered.append("")
            continue

        if entry.kind == "thinking":
            running = entry.status == "running"
            label = "Thinking…" if running else "Thought"
            rendered.append(f"  {ACCENT}✻{RESET} {SUBTLE}{ITALIC}{label}{RESET}")
            for line in body_lines:
                rendered.extend(
                    f"    {SUBTLE}{ITALIC}{part}{RESET}" for part in wrap_text(line, max(2, cols - 6)).split("\n")
                )
            rendered.append("")
            continue

        if entry.kind == "tool":
            rendered.extend(render_tool_activity(entry, cols))
            rendered.append("")
    return rendered


def _stream_window_rows(total: int, scroll_offset: int, height: int) -> list[int | None]:
    """Rendered-line index on each transcript row; None marks the scroll marker."""
    max_offset = max(0, total - height)
    offset = max(0, min(scroll_offset, max_offset))
    end = total - offset
    start = max(0, end - height)
    rows: list[int | None] = list(range(start, end))
    if offset > 0:
        rows.insert(0, None)
    return rows[: max(0, height)]


def _window_stream_transcript(rendered: list[str], scroll_offset: int, height: int) -> list[str]:
    """The ``height`` rows ending ``scroll_offset`` rows above the bottom."""
    total = len(rendered)
    max_offset = max(0, total - height)
    offset = max(0, min(scroll_offset, max_offset))
    pct = int(offset / max_offset * 100) if max_offset > 0 else 0
    marker = f"{SUBTLE}-- {offset}/{max_offset} ({pct}%) --{RESET}"
    return [marker if index is None else rendered[index] for index in _stream_window_rows(total, offset, height)]


def _anchored_scroll_offset(total: int, height: int, line_index: int, row: int) -> int:
    """Scroll offset that shows ``line_index`` on screen ``row`` (as close as possible)."""
    # A scrolled-back view spends row 0 on the marker, shifting content down one.
    offset = total - height - (line_index - max(row, 1) + 1)
    return max(0, min(offset, max(0, total - height)))


def _toggle_activity_group(state: ScreenState, group_id: int, row: int | None = None) -> None:
    if group_id in state.expanded_activity_groups:
        state.expanded_activity_groups.discard(group_id)
    else:
        state.expanded_activity_groups.add(group_id)
    state.activity_anchor = (group_id, row) if row is not None else None


def _toggle_latest_activity_group(state: ScreenState) -> bool:
    groups = _finished_activity_groups(list(state.transcript), state.streaming_entry_id)
    if not groups:
        return False
    group_id = list(groups)[-1]
    rows = [row for row, header in state.activity_header_rows.items() if header == group_id]
    _toggle_activity_group(state, group_id, rows[0] if rows else None)
    return True


def _render_stream_screen(args: TtyAppArgs, state: ScreenState) -> None:
    """Claude-style: clean stream layout with proper scroll tracking."""
    cols, rows = _get_terminal_size()
    cols = max(8, cols)
    rows = max(8, rows)
    # Repopulated below only when the transcript is actually on screen.
    state.activity_header_rows = {}
    if state.provider_wizard is not None:
        state.frame_renderer.draw(render_provider_wizard(state.provider_wizard, cols, rows), cols, rows)
        return
    if state.settings_picker is not None:
        state.frame_renderer.draw(render_settings_picker(state.settings_picker, cols, rows), cols, rows)
        return
    if state.resume_picker_sessions is not None:
        frame = _render_resume_picker_lines(
            state.resume_picker_sessions,
            state.resume_picker_index,
            cols,
            rows - 1,
        )
        footer = f"  {_permission_mode_label(args)}  {SUBTLE}{Path(args.cwd).name or args.cwd}{RESET}  {SUBTLE}{len(state.resume_picker_sessions)} saved sessions{RESET}"
        state.frame_renderer.draw(frame + [footer], cols, rows)
        return

    if not state.transcript:
        _render_home_screen(args, state, cols, rows)
        return

    model_name = (
        f"{model_display_name(args.runtime)} · {args.runtime.get('reasoningEffort', 'default')}"
        if args.runtime
        else "unknown"
    )

    pending = state.pending_approval
    if pending is not None:
        # Swap only the composer for the approval prompt; switching to a
        # different full-screen layout for every tool approval flashed.
        input_lines = render_permission_prompt(
            pending.request,
            expanded=pending.details_expanded,
            scroll_offset=pending.details_scroll_offset,
            selected_choice_index=pending.selected_choice_index,
            feedback_mode=pending.feedback_mode,
            feedback_input=pending.feedback_input,
        ).splitlines()
    else:
        visible_commands = _get_visible_commands(state.input, args.cwd)
        input_lines = _render_stream_input(state, model_name, cols, visible_commands)

        # Render slash menu if visible
        if visible_commands and state.input:
            menu = render_slash_menu(
                visible_commands,
                min(state.selected_slash_index, len(visible_commands) - 1),
                max_rows=max(1, min(10, rows - len(input_lines) - 6)),
                width=max(2, cols - 1),
            )
            input_lines.append("")
            input_lines.extend(menu.splitlines())

    input_lines.append("  " + _render_usage_status(args, state, max(4, cols - 4)))

    msg_count = len(state.transcript)
    sep = f"  {SUBTLE}{'─' * max(1, cols - 4)}{RESET}"

    cwd_name = Path(args.cwd).name or args.cwd
    mode_label = _permission_mode_label(args)
    notice = f"  {ACCENT}{state.selection_notice}{RESET}" if state.selection_notice else ""
    footer = f"  {mode_label}  {SUBTLE}{cwd_name}{RESET}  {SUBTLE}{msg_count} events{RESET}  {SUBTLE}{DISPLAY_VERSION}{RESET}{notice}"
    if state.exit_requested_at is not None:
        footer = f"  {mode_label}  {YELLOW}Press Ctrl+C again within 2s to exit · /exit to quit{RESET}"

    reserved = len(input_lines) + 2
    transcript_height = rows - reserved
    if transcript_height < 1:
        # A prompt taller than the screen keeps its bottom (the choices) visible.
        state.frame_renderer.draw(_truncate_frame_lines((input_lines + [footer])[-rows:], cols, rows), cols, rows)
        return

    header_lines: dict[int, int] = {}
    rendered = _build_stream_transcript_lines(
        list(state.transcript),
        cols,
        expanded_groups=state.expanded_activity_groups,
        streaming_entry_id=state.streaming_entry_id,
        header_lines=header_lines,
    )
    total = len(rendered)
    anchor, state.activity_anchor = state.activity_anchor, None
    anchor_line = next((line for line, group in header_lines.items() if group == anchor[0]), None) if anchor else None
    if anchor and anchor_line is not None:
        # A toggled header stays under the pointer; its details open below it.
        state.transcript_scroll_offset = _anchored_scroll_offset(total, transcript_height, anchor_line, anchor[1])
    elif state.transcript_scroll_offset > 0 and cols == state.transcript_layout_cols:
        # Offsets count from the bottom, so keep a scrolled-back view on the
        # same text while new output is appended below it.
        state.transcript_scroll_offset += total - state.transcript_total_lines
    state.transcript_scroll_offset = max(0, min(state.transcript_scroll_offset, total - transcript_height))
    state.user_scrolled_away = state.transcript_scroll_offset > 0
    state.transcript_total_lines = total
    state.transcript_view_height = transcript_height
    state.transcript_layout_cols = cols
    window_rows = _stream_window_rows(total, state.transcript_scroll_offset, transcript_height)
    state.activity_header_rows = {
        row: header_lines[index] for row, index in enumerate(window_rows) if index in header_lines
    }
    transcript_lines = _window_stream_transcript(rendered, state.transcript_scroll_offset, transcript_height)
    # Anchor the composer while new output streams in.
    transcript_lines += [""] * max(0, transcript_height - len(transcript_lines))

    frame = _truncate_frame_lines(transcript_lines, cols, transcript_height) + [sep] + input_lines + [footer]
    frame = _truncate_frame_lines(frame, cols, rows)

    state.frame_renderer.draw(frame, cols, rows)


def _render_screen(args: TtyAppArgs, state: ScreenState) -> None:
    if os.environ.get("pepsicode_LEGACY_TUI") != "1" or state.settings_picker or state.provider_wizard:
        _render_stream_screen(args, state)
        return

    background_tasks = list_background_tasks()

    # Get contextual help text
    contextual_help = _get_contextual_help(state, args)

    # Build the entire frame into a buffer, then write once
    buf: list[str] = []
    cols, rows = _get_terminal_size()

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
        buf.append(_render_footer_cached(state.status, True, has_skills, background_tasks, args.permissions.access_mode.value, args.permissions.is_plan_mode))
        state.frame_renderer.draw("".join(buf).splitlines(), cols, rows)
        return

    # Transcript -- snapshot the list to avoid IndexError from concurrent
    # agent-thread appends (CPython GIL makes list.append atomic but
    # iteration + append can still race on length vs slot access).
    transcript_snapshot = list(state.transcript)
    body_lines = _get_transcript_body_lines(args, state)
    if transcript_snapshot:
        transcript_body = render_transcript(transcript_snapshot, state.transcript_scroll_offset, body_lines)
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
    buf.append(_render_prompt_panel(state, args.cwd))
    buf.append("\n\n")

    # Footer (cached)
    buf.append(_render_footer_cached(state.status, True, has_skills, background_tasks, args.permissions.access_mode.value, args.permissions.is_plan_mode))

    # Contextual help line
    if contextual_help:
        buf.append(f"\n{SUBTLE}{contextual_help}{RESET}")

    state.frame_renderer.draw("".join(buf).splitlines(), cols, rows)


# ---------------------------------------------------------------------------
# Cross-platform raw mode stdin
# ---------------------------------------------------------------------------

# Windows msvcrt scan-code -> ANSI escape sequence mapping.
# msvcrt.getwch() returns a two-char sequence for special keys:
#   prefix ('\x00' or '\xe0') + scan-code byte.
# We translate these to the ANSI sequences that input_parser.py already
# understands.
_WIN_SCANCODE_TO_ANSI: dict[int, str] = {
    72: "\x1b[A",  # Up
    80: "\x1b[B",  # Down
    77: "\x1b[C",  # Right
    75: "\x1b[D",  # Left
    71: "\x1b[H",  # Home
    79: "\x1b[F",  # End
    73: "\x1b[5~",  # Page Up
    81: "\x1b[6~",  # Page Down
    83: "\x1b[3~",  # Delete
    82: "\x1b[2~",  # Insert
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
        self._old_input_mode: int | None = None
        self._input_handle: Any = None

    def __enter__(self) -> _RawModeContext:
        if sys.platform == "win32":
            # Ensure VT processing is active (idempotent)
            from pepsicode.tui.screen import _enable_windows_vt_processing

            _enable_windows_vt_processing()
            # Switch console to UTF-8 code page for proper Unicode handling
            try:
                import ctypes
                import ctypes.wintypes as wintypes

                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                kernel32.GetStdHandle.restype = wintypes.HANDLE
                kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                self._input_handle = kernel32.GetStdHandle(-10)
                mode = wintypes.DWORD()
                if kernel32.GetConsoleMode(self._input_handle, ctypes.byref(mode)):
                    self._old_input_mode = mode.value
                    # Receive Ctrl+C as input, so selection handling runs before
                    # exit handling. QuickEdit must not intercept mouse drags.
                    raw_mode = (mode.value & ~(0x0001 | 0x0002 | 0x0004 | 0x0040)) | 0x0080
                    kernel32.SetConsoleMode(self._input_handle, raw_mode)
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
            new[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
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
            new[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
            # Special characters: read returns after 1 byte, no timeout.
            new[6][termios.VMIN] = 1
            new[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSAFLUSH, new)
        return self

    def __exit__(self, *_: Any) -> None:
        if sys.platform == "win32":
            if self._old_cp is not None or self._old_input_mode is not None:
                try:
                    import ctypes

                    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                    if self._old_input_mode is not None:
                        kernel32.SetConsoleMode(self._input_handle, self._old_input_mode)
                    if self._old_cp is not None:
                        kernel32.SetConsoleOutputCP(self._old_cp)
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
    state.cancellation_event.clear()
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

    state.agent_lock = threading.Lock()
    state.agent_result = {"messages": None, "done": False}

    def run_shortcut():
        try:
            result = args.tools.execute(
                tool_name,
                tool_input,
                context=ToolContext(
                    cwd=args.cwd,
                    permissions=args.permissions,
                    hooks=args.hook_engine,
                    memory=args.memory,
                    cancellation_event=state.cancellation_event,
                ),
            )
            state.recent_tools.append({"name": tool_name, "status": "success" if result.ok else "error"})
            output = result.output if result.ok else f"ERROR: {result.output}"
            _finish_tool_entry(state, entry_id, "success" if result.ok else "error", output)
            state.transcript_scroll_offset = 0
            state.user_scrolled_away = False
            _drain_tty_hook_notifications(args, state)
        finally:
            state.active_tool = None
            _finalize_dangling_running_tools(state)
            state.status = None
            with state.agent_lock:
                state.agent_result["done"] = True
            state.is_busy = False
            rerender()

    state.agent_thread = threading.Thread(target=run_shortcut, daemon=True)
    state.agent_thread.start()


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def _enqueue_tty_input(args: TtyAppArgs, state: ScreenState, input_text: str, input_mode: InputMode) -> None:
    input_text = input_text.strip()
    if not input_text:
        return
    if input_text.startswith("/queue "):
        input_text, input_mode = input_text[len("/queue ") :].strip(), "follow_up"
    try:
        if state.cancellation_event.is_set():
            raise ValueError("The task is stopping; draft kept for the next task")
        expanded = expand_prompt_command(input_text, args.cwd)
        state.input_queue.enqueue(
            expanded["content"] if expanded else input_text,
            input_mode,
            display_content=input_text if expanded else None,
            command_path=expanded["commandPath"] if expanded else None,
        )
    except ValueError as error:
        state.status = str(error)
        return
    state.input, state.cursor_offset = "", 0
    if not state.history or state.history[-1] != input_text:
        state.history.append(input_text)
        save_history_entries(state.history)
    state.history_index = len(state.history)
    state.history_draft = ""
    state.status = "Follow-up queued" if input_mode == "follow_up" else "Instruction queued for the next response"


def _handle_input(
    args: TtyAppArgs,
    state: ScreenState,
    rerender: Callable[[], None],
    submitted_raw_input: str | None = None,
    input_mode: InputMode = "steer",
) -> bool:
    """Returns True if /exit was typed."""
    _collect_agent_result(args, state)
    input_text = (submitted_raw_input if submitted_raw_input is not None else state.input).strip()
    if state.is_busy:
        _enqueue_tty_input(args, state, input_text, input_mode)
        return False

    if not input_text:
        return False
    if input_text == "/exit":
        return True

    regenerated_prompt = None
    if input_text.split()[0] in {"/tree", "/fork", "/clone", "/retry"}:
        if state.session is None:
            state.session = create_new_session(args.cwd)
        _snapshot_session(args, state)
        save_session(state.session)
        parts = input_text.split()
        try:
            if len(parts) > (1 if parts[0] in {"/clone", "/retry"} else 2):
                raise ValueError("Usage: /tree [entry-id], /fork [user-entry-id], /clone, /retry")
            if parts[0] in {"/tree", "/fork"} and len(parts) == 1:
                _push_transcript_entry(
                    state, kind="assistant", body=format_tree(state.session, users_only=parts[0] == "/fork")
                )
                return False
            if parts[0] == "/retry":
                regenerated_prompt = prepare_regeneration(state.session)
                args.messages = list(state.session.messages)
                if args.context_manager:
                    args.context_manager.select_branch(args.messages, args.cwd, args.permissions)
                rebuild_transcript(state.session)
                state.transcript = [TranscriptEntry(**entry) for entry in state.session.transcript_entries]
                state.next_entry_id = len(state.transcript) + 1
                input_text = regenerated_prompt.get("displayContent", regenerated_prompt["content"])
                if args.runtime is not None:
                    args.runtime.pop("_lastVerification", None)
            else:
                if parts[0] == "/tree":
                    draft = checkout_entry(state.session, parts[1])
                    selected = state.session
                else:
                    selected, draft = copy_session(
                        state.session, entry_id=parts[1] if len(parts) > 1 else None, clone=parts[0] == "/clone"
                    )
                save_session(selected)
                _restore_session_into_state(args, state, selected)
                state.input, state.cursor_offset = draft, len(draft)
                state.status = "Conversation branch selected; working-directory files are unchanged."
                rerender()
                return False
        except ValueError as error:
            _push_transcript_entry(state, kind="assistant", body=str(error), status="error")
            return False

    _emit_tty_hook(args, HookEvent.USER_INPUT, data={"user_input": input_text})
    _drain_tty_hook_notifications(args, state)

    # History
    if not state.history or state.history[-1] != input_text:
        state.history.append(input_text)
        save_history_entries(state.history)
    state.history_index = len(state.history)
    state.history_draft = ""

    # Autosave trigger
    if state.autosave:
        state.autosave.mark_dirty()

    if input_text == "/plan" or input_text.startswith("/plan "):
        plan_path = args.permissions.enter_plan_mode()
        if state.session:
            state.session.access_mode = args.permissions.access_mode.value
            state.session.permission_mode = args.permissions.mode.value
            state.session.plan_file_path = args.permissions.plan_file_path
        task = input_text[len("/plan") :].strip()
        _push_transcript_entry(
            state,
            kind="assistant",
            body=f"Plan mode active — read-only except for:\n{plan_path}",
        )
        if not task:
            state.status = "PLAN mode"
            rerender()
            return False
        input_text = task

    try:
        prompt_message = regenerated_prompt or expand_prompt_command(input_text, args.cwd)
    except ValueError as error:
        _push_transcript_entry(state, kind="assistant", body=str(error), status="error")
        return False

    if input_text.startswith("/"):
        try:
            args.permissions.ensure_local_command_allowed(input_text)
        except RuntimeError as error:
            _push_transcript_entry(state, kind="assistant", body=str(error))
            return False

    if input_text == "/provider add":
        state.settings_picker = None
        state.provider_wizard = create_provider_wizard("custom")
        state.input, state.cursor_offset = "", 0
        rerender()
        return False

    if input_text.startswith("/provider ") and len(input_text.split()) == 2 and input_text.split()[1] != "list":
        identifier = input_text.split()[1]
        try:
            provider_runtime(args.runtime or {"model": "mock"}, identifier)
        except ValueError:
            if identifier in PROVIDERS:
                # A preset without a Key goes straight to its setup instead of an error.
                state.provider_wizard = create_provider_wizard(identifier)
                state.input, state.cursor_offset = "", 0
                rerender()
                return False
        _open_settings_picker(args, state, "model", provider=identifier)
        rerender()
        return False

    if input_text in {"/model", "/effort", "/provider", "/permissions"}:
        _open_settings_picker(args, state, input_text[1:])
        rerender()
        return False

    if (
        (not input_text.startswith("/") or prompt_message is not None)
        and args.runtime
        and not isinstance(args.model, MockModelAdapter)
    ):
        problem = model_selection_error(args.runtime)
        if problem:
            _open_settings_picker(args, state, "model", notice=problem, return_input=input_text)
            rerender()
            return False

    # /tools
    if input_text == "/tools":
        _push_transcript_entry(
            state,
            kind="assistant",
            body="\n".join(f"{t.name}: {t.description}" for t in args.tools.list()),
        )
        return False

    if input_text == "/hooks" or input_text.startswith("/hooks "):
        output = (
            args.hook_engine.handle_command(input_text) if args.hook_engine is not None else "Hooks are unavailable."
        )
        _push_transcript_entry(state, kind="assistant", body=output)
        _drain_tty_hook_notifications(args, state)
        return False

    if input_text == "/resume" or input_text.startswith("/resume "):
        session_id = input_text[len("/resume") :].strip()
        if session_id:
            _resume_session_by_id(args, state, session_id)
        else:
            _open_resume_picker(args, state)
        return False

    # Local commands
    local_result = try_handle_local_command(
        input_text,
        tools=args.tools,
        permissions=args.permissions,
        runtime=args.runtime,
        model_adapter=args.model,
        messages=args.messages,
        context_manager=args.context_manager,
        cwd=args.cwd,
    )
    if local_result is not None:
        if input_text.split()[0] in {"/model", "/effort", "/provider"}:
            state.latest_usage = None
        _sync_model_settings_state(args, state)
        _push_transcript_entry(state, kind="assistant", body=local_result)
        return False

    # Tool shortcuts
    shortcut = parse_local_tool_shortcut(input_text)
    if shortcut:
        _execute_tool_shortcut(args, state, shortcut["toolName"], shortcut["input"], rerender)
        return False

    # Unknown slash commands
    if input_text.startswith("/") and prompt_message is None:
        matches = find_matching_slash_commands(input_text, args.cwd)
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
    state.cancellation_event.clear()

    # Update app state
    if state.app_state:
        from pepsicode.core.state import set_busy

        state.app_state.set_state(set_busy())

    rerender()

    pending_tool_entries: dict[str, list[int]] = defaultdict(list)
    aggregated_edit_by_key: dict[str, AggregatedEditProgress] = {}
    aggregated_edit_by_entry_id: dict[int, AggregatedEditProgress] = {}

    # Refresh system prompt
    args.messages[0] = {
        "role": "system",
        "content": build_session_prompt(
            args.cwd,
            args.permissions.get_summary(),
            skills=args.tools.get_skills(),
            mcp_servers=args.tools.get_mcp_servers(),
            memory_manager=args.memory,
            governance=bool(args.runtime.get("governance")) if args.runtime else False,
            plan_mode=args.permissions.is_plan_mode,
            plan_file_path=args.permissions.plan_file_path,
        ),
    }
    args.messages.append(prompt_message or {"role": "user", "content": input_text})
    if state.session is None:
        state.session = create_new_session(args.cwd)
    record_messages(state.session, args.messages)
    state.latest_usage = None

    def finish_thinking() -> None:
        """Mark the reasoning entry being streamed as complete."""
        if state.thinking_entry_id is None:
            return
        for entry in state.transcript:
            if entry.id == state.thinking_entry_id:
                entry.status = None
                break
        state.thinking_entry_id = None
        rerender()

    def on_token(token: StreamToken) -> None:
        """Handle a single streaming token from the model.

        Creates or updates a transcript entry in real-time so the user sees
        the response being generated character-by-character.
        """
        if token.type == "usage":
            state.latest_usage = token.usage
            rerender()
        elif token.type == "thinking":
            if token.content:
                if state.thinking_entry_id is None:
                    state.thinking_entry_id = _push_transcript_entry(
                        state, kind="thinking", body=token.content, status="running"
                    )
                else:
                    for entry in state.transcript:
                        if entry.id == state.thinking_entry_id:
                            entry.body += token.content
                            break
                _reset_scroll_if_needed(state)
                rerender()
            if token.thinking_block is not None:
                finish_thinking()
        elif token.type == "text":
            finish_thinking()
            state.streaming_text += token.content
            if state.streaming_entry_id is None:
                # Create a new transcript entry for the streaming output.
                state.streaming_entry_id = _push_transcript_entry(
                    state,
                    kind="assistant",
                    body=state.streaming_text,
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
        finish_assistant_message(content, is_error=False)

    def on_error_message(content: str) -> None:
        finish_assistant_message(content, is_error=True)

    def finish_assistant_message(content: str, *, is_error: bool) -> None:
        if state.streaming_entry_id is not None:
            # Streaming already displayed this content in a transcript entry.
            # Update the entry body (idempotent for normal flow, overwrites
            # partial content on error paths) and finalize streaming state.
            for entry in state.transcript:
                if entry.id == state.streaming_entry_id:
                    entry.body = content
                    entry.status = "error" if is_error else None
                    break
            state.streaming_entry_id = None
            state.streaming_text = ""
        else:
            # No streaming happened (error path, etc.) -- push a new entry.
            _push_transcript_entry(state, kind="assistant", body=content, status="error" if is_error else None)
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
                    path=target_path or "",
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
                    state.recent_tools.append(
                        {
                            "name": f"{tool_name} x{aggregated.total}",
                            "status": "error" if aggregated.errors > 0 else "success",
                        }
                    )
                body = (
                    "\n".join(
                        [
                            f"Aggregated {tool_name} for {aggregated.path}",
                            f"Operations: {aggregated.total}, errors: {aggregated.errors}",
                            f"Last result: {aggregated.last_output}",
                        ]
                    )
                    if done
                    else f"Aggregated {tool_name} for {aggregated.path}\nCompleted: {aggregated.completed}/{aggregated.total}"
                )
                status = "error" if aggregated.errors > 0 else ("success" if done else "running")
                if done:
                    _finish_tool_entry(state, entry_id, status, body)
                    aggregated_edit_by_entry_id.pop(entry_id, None)
                    aggregated_edit_by_key.pop(f"{tool_name}:{aggregated.path}", None)
                else:
                    _update_tool_entry(state, entry_id, status, body)
            else:
                state.recent_tools.append(
                    {
                        "name": tool_name,
                        "status": "error" if is_error else "success",
                    }
                )

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

                _finish_tool_entry(state, entry_id, "error" if is_error else "success", display_output)

        state.active_tool = None
        remaining = sum(len(v) for v in pending_tool_entries.values())
        if remaining > 0:
            state.status = f"{remaining} tool(s) still running..."
        else:
            state.status = None
        _reset_scroll_if_needed(state)
        rerender()

    args.permissions.begin_turn()

    def on_input_queue(update: dict) -> None:
        state.pending_inputs = update["pending"]
        for item in update["delivered"]:
            _push_transcript_entry(state, kind="user", body=item.get("display_content") or item["content"])
        state.returned_inputs.extend(item.get("display_content") or item["content"] for item in update["returned"])
        rerender()

    if state.session is not None:
        state.input_queue.bind_session(state.session)
    state.input_queue.start(on_input_queue)

    def on_usage(usage: dict) -> None:
        state.latest_usage = usage
        rerender()

    # Run agent turn in background thread to keep UI responsive
    agent_result: dict = {"messages": None}
    agent_thread_lock = threading.Lock()

    def _run_agent_background():
        nonlocal agent_result
        try:
            # Use streaming agent turn for real-time token output
            with recording_session(state.session):
                next_messages = run_verified_turn(
                    runtime=args.runtime,
                    runner=run_agent_turn_stream,
                    input_queue=state.input_queue,
                    model=args.model,
                    tools=args.tools,
                    messages=list(args.messages),  # Copy to avoid race condition
                    cwd=args.cwd,
                    permissions=args.permissions,
                    on_token=on_token,
                    on_tool_start=on_tool_start,
                    on_tool_result=on_tool_result,
                    on_assistant_message=on_assistant_message,
                    on_error_message=on_error_message,
                    on_progress_message=on_progress_message,
                    context_manager=args.context_manager,
                    cost_tracker=state.cost_tracker,
                    on_usage=on_usage,
                    hook_engine=args.hook_engine,
                    memory=args.memory,
                    cancellation_event=state.cancellation_event,
                )
            if args.context_manager:
                args.context_manager.messages = [dict(message) for message in next_messages]
            with agent_thread_lock:
                agent_result["messages"] = next_messages
        except Exception as e:
            message = f"Task failed: {e}"
            _push_transcript_entry(state, kind="assistant", body=message, status="error")
            agent_result["messages"] = [*args.messages, {"role": "assistant", "content": message, "isError": True}]
        finally:
            state.input_queue.close()
            args.permissions.end_turn()
            # Persist context state to disk so /context can read it
            if args.context_manager:
                try:
                    from pepsicode.context.context_manager import save_context_state

                    save_context_state(args.context_manager)
                except Exception:
                    pass
            with agent_thread_lock:
                agent_result["done"] = True
            state.is_busy = False
            state.active_tool = None
            state.status = None
            # Clear streaming state
            state.streaming_entry_id = None
            finish_thinking()
            _drain_tty_hook_notifications(args, state)
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


def _session_memory(cwd: str) -> Any:
    """Return the process memory manager, creating the file-backed default."""
    from pepsicode.context.memory import create_memory_manager

    return create_memory_manager(cwd)


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
    cost_tracker: CostTracker | None = None,
    trace_manager: Any | None = None,
    hook_engine: HookEngine | None = None,
    memory: Any | None = None,
) -> list[ChatMessage]:
    """Event-driven full-screen TTY application, ported from the TypeScript version.

    Args:
        resume_session: Session ID to resume, or "latest" for most recent
        list_sessions_only: If True, print session list and exit
        cost_tracker: Shared cost tracker (reused for sub-agent token tracking)
        trace_manager: Shared trace manager for sub-agent observability
    """

    context_manager = None
    if runtime:
        from pepsicode.context.context_manager import ContextManager

        context_manager = ContextManager(model=runtime.get("model", "default"))
        window = model_capabilities(runtime, runtime["model"]).get("contextWindow")
        if window:
            context_manager.context_window = window
            context_manager.context_window_is_estimate = False
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
        cost_tracker=cost_tracker,
        trace_manager=trace_manager,
        hook_engine=hook_engine,
        memory=memory if memory is not None else _session_memory(cwd),
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
            if not same_workspace(cwd, session.workspace):
                print(f"Session belongs to {session.workspace}. Change to that directory before resuming it.")
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

    permissions.set_access_mode(session.access_mode)
    permissions.restore_plan_state(session.permission_mode, session.plan_file_path)
    restore_verification_state(args.tools, args.runtime, session.verification_state, session.messages)
    _restore_model_settings(args, session)

    # Initialize AppState store (Zustand-style)
    app_state_store = create_app_store(
        {
            "session_id": session.session_id,
            "workspace": cwd,
            "model": runtime.get("model", "unknown") if runtime else "unknown",
        }
    )

    # Initialize CostTracker -- reuse the one provided by the caller (so
    # sub-agent token usage recorded via the Task tool is visible) or create a
    # fresh one for this session.
    cost_tracker = args.cost_tracker or CostTracker()

    state = ScreenState(
        history=load_history_entries(),
        session=session,
        autosave=AutosaveManager(session),
        app_state=app_state_store,
        cost_tracker=cost_tracker,
    )
    state.history_index = len(state.history)

    if args.runtime and not isinstance(args.model, MockModelAdapter):
        problem = model_selection_error(args.runtime)
        if problem:
            _open_settings_picker(args, state, "model", notice=problem)

    # Restore session state if resuming
    if session.messages:
        if not session.transcript_entries:
            rebuild_transcript(session)
        # Restore messages
        args.messages.clear()
        args.messages.extend(session.messages)

        # Restore transcript entries
        for entry_data in session.transcript_entries:
            entry = TranscriptEntry(**entry_data)
            state.transcript.append(entry)

        print(f"Restored {len(session.messages)} messages, {len(state.transcript)} transcript entries.")
        _emit_tty_hook(
            args,
            HookEvent.SESSION_RESUME,
            data={"session_id": session.session_id, "message_count": len(session.messages)},
        )
        _drain_tty_hook_notifications(args, state)

    _drain_tty_hook_notifications(args, state)

    # Wire up permission prompt handler
    approval_event = threading.Event()
    approval_result: dict[str, Any] = {}

    def _permission_prompt_handler(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal approval_result
        approval_event.clear()
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
        while not approval_event.wait(0.05):
            if state.cancellation_event.is_set():
                state.pending_approval = None
                return {"decision": "deny_once"}
        if state.cancellation_event.is_set():
            state.pending_approval = None
            return {"decision": "deny_once"}
        result = approval_result.copy()
        state.pending_approval = None
        return result

    original_permission_prompt = permissions.prompt
    permissions.prompt = _permission_prompt_handler

    # Coalesce streaming updates to at most 30 fps.
    throttled = _ThrottledRenderer(lambda: _render_screen(args, state))

    def rerender() -> None:
        throttled.request()

    input_decoder = TerminalInputDecoder()
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
    if sys.platform != "win32" and threading.current_thread() is threading.main_thread():
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
        last_terminal_size = _get_terminal_size()

        with _RawModeContext():
            while not should_exit:
                terminal_size = _get_terminal_size()
                if terminal_size != last_terminal_size:
                    last_terminal_size = terminal_size
                    rerender()
                # Autosave check (throttled)
                _autosave_counter += 1
                if state.autosave and _autosave_counter >= _AUTOSAVE_CHECK_INTERVAL:
                    _autosave_counter = 0
                    _snapshot_session(args, state)
                    state.autosave.save_if_needed()

                # Animation frame for thinking indicator
                state._animation_frame += 1

                # Check if background agent thread completed
                if _collect_agent_result(args, state):
                    plan_followup = args.permissions.consume_plan_followup()
                    if state.session:
                        state.session.access_mode = args.permissions.access_mode.value
                        state.session.permission_mode = args.permissions.mode.value
                        state.session.plan_file_path = args.permissions.plan_file_path
                        if state.autosave:
                            state.autosave.mark_dirty()
                    if plan_followup and not state.is_busy:
                        _handle_input(args, state, rerender, submitted_raw_input=plan_followup)

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
                    chunk = _raw

                if not chunk:
                    continue

                for event in input_decoder.feed(chunk):
                    try:
                        _handle_event(args, state, event, rerender, approval_event, approval_result)
                    except SystemExit:
                        should_exit = True
                        break
                    except Exception as e:
                        # Log the event-handling error but keep the main loop running
                        logging.debug("Event handling error: %s", e, exc_info=True)

                # Ensure the final state after processing all events is visible
                throttled.flush()

    finally:
        if state.is_busy:
            state.cancellation_event.set()
            approval_event.set()
            if state.agent_thread is not None:
                state.agent_thread.join(timeout=2)
        _collect_agent_result(args, state)
        # Restore previous SIGWINCH handler on Unix
        if _prev_sigwinch is not None and sys.platform != "win32":
            import signal as _signal

            _signal.signal(_signal.SIGWINCH, _prev_sigwinch)

        show_cursor()
        exit_alternate_screen()
        permissions.prompt = original_permission_prompt

        # Final session save
        if state.session:
            _snapshot_session(args, state)

            # Force save
            if state.autosave:
                state.autosave.force_save()
            else:
                save_session(state.session)

            _emit_tty_hook(
                args,
                HookEvent.SESSION_SAVE,
                data={"session_id": state.session.session_id, "message_count": len(args.messages)},
            )

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
    - Ctrl+C: Copy a selection, stop the current task, or confirm idle exit
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
    is_interrupt = _is_ctrl_c(event)
    if not is_interrupt:
        state.exit_requested_at = None
    if _handle_selection_event(state, event, rerender):
        state.exit_requested_at = None
        return
    if is_interrupt:
        if state.is_busy:
            state.cancellation_event.set()
            state.exit_requested_at = None
            state.status = "Stopping current task..."
            rerender()
            return
        now = time.monotonic()
        if state.exit_requested_at is not None and now - state.exit_requested_at <= 2:
            raise SystemExit(0)
        state.exit_requested_at = now
        rerender()
        return

    if isinstance(event, PasteEvent):
        # Pasted controls must never submit a message, approve a tool, or select
        # a slash command. Keep newlines and tabs as part of the draft.
        text = "".join(char for char in event.text if char >= " " or char in "\n\t")
        if state.pending_approval is not None:
            if state.pending_approval.feedback_mode:
                state.pending_approval.feedback_input += text.replace("\n", " ")
        elif state.provider_wizard is not None:
            _handle_provider_wizard_event(args, state, TextEvent(text.strip(), False, False), rerender)
        elif state.settings_picker is None and state.resume_picker_sessions is None:
            state.input = state.input[: state.cursor_offset] + text + state.input[state.cursor_offset :]
            state.cursor_offset += len(text)
        rerender()
        return

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

    if pending is None:
        return

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
    if state.provider_wizard is not None:
        _handle_provider_wizard_event(args, state, event, rerender)
        return
    if state.settings_picker is not None:
        _handle_settings_picker_event(args, state, event, rerender)
        return
    visible_commands = _get_visible_commands(state.input, getattr(args, "cwd", None))

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
    if event.ctrl and event.name in {"a", "e", "u", "p", "n"}:
        return _handle_normal_mode_text(
            args, state, TextEvent(text=event.name, ctrl=True, meta=event.meta), visible_commands, rerender
        )
    # Return -> submit input or select slash command
    if event.name == "return":
        _handle_normal_mode_return(
            args, state, visible_commands, rerender, input_mode="follow_up" if event.meta else "steer"
        )
        return True

    if event.name == "up" and event.meta and state.pending_inputs:
        state.input_queue.remove(state.pending_inputs[-1]["id"])
        if state.returned_inputs:
            state.input = "\n\n".join([*state.returned_inputs, *([state.input] if state.input else [])])
            state.returned_inputs = []
            state.cursor_offset = len(state.input)
        rerender()
        return True

    # Tab -> autocomplete slash command
    if event.name == "tab" and visible_commands:
        _handle_normal_mode_tab(state, visible_commands, rerender)
        return True

    # Navigation and editing keys
    if _handle_normal_mode_navigation(state, event, rerender):
        return True

    # Ctrl-O -> fold/unfold the latest finished turn's activity
    if event.ctrl and event.name == "o":
        if _toggle_latest_activity_group(state):
            rerender()
        return True

    # Ctrl shortcuts (P, N handled in text handler)
    # PageUp/PageDown -> scroll transcript by most of a screen
    page = max(1, state.transcript_view_height - 2) if state.transcript_view_height else 8
    if event.name == "pageup" and _scroll_transcript_by(args, state, page):
        rerender()
        return True

    if event.name == "pagedown" and _scroll_transcript_by(args, state, -page):
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
    input_mode: InputMode = "steer",
) -> None:
    """Handle Return key in normal mode."""
    if state.is_busy:
        # Admission can race with worker settlement. A closed queue keeps the
        # draft; it must never accidentally start a second run here.
        _enqueue_tty_input(args, state, state.input, input_mode)
        rerender()
        return
    if visible_commands and 0 <= state.selected_slash_index < len(visible_commands):
        selected = visible_commands[state.selected_slash_index]
        usage = getattr(selected, "completion_text", getattr(selected, "usage", str(selected)))
        # If input already matches the command exactly, submit instead of completing
        if state.input.strip() == usage or usage in {"/model", "/effort", "/permissions"}:
            state.selected_slash_index = 0
            submitted = usage if usage in {"/model", "/effort", "/permissions"} else state.input
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
    usage = getattr(selected, "completion_text", getattr(selected, "usage", str(selected)))
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
        state.input = state.input[: state.cursor_offset - 1] + state.input[state.cursor_offset :]
        state.cursor_offset -= 1
        state.selected_slash_index = 0
        rerender()
        return True

    if event.name == "delete" and state.cursor_offset < len(state.input):
        state.input = state.input[: state.cursor_offset] + state.input[state.cursor_offset + 1 :]
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
            usage = getattr(selected, "completion_text", getattr(selected, "usage", str(selected)))
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

        if event.text == "o":  # Ctrl-O -> fold/unfold latest activity
            if _toggle_latest_activity_group(state):
                rerender()
            return True

        return False

    # Regular text input (accept any non-empty text, including multi-byte CJK/emoji)
    if not event.ctrl and event.text:
        state.input = state.input[: state.cursor_offset] + event.text + state.input[state.cursor_offset :]
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


def _is_ctrl_c(event: ParsedInputEvent) -> bool:
    if not isinstance(event, (KeyEvent, TextEvent)):
        return False
    return event.ctrl and (
        (isinstance(event, KeyEvent) and event.name == "c")
        or (isinstance(event, TextEvent) and event.text.lower() == "c")
    )


def _copy_selection(state: ScreenState) -> None:
    text = state.frame_renderer.selected_text()
    if text:
        copy_to_clipboard(text)
        state.selection_notice = f"Copied {len(text)} chars"


def _handle_selection_event(state: ScreenState, event: ParsedInputEvent, rerender: Callable[[], None]) -> bool:
    """Drag to select screen text, release to copy it. Returns True if consumed.

    Mouse capture (needed for wheel scrolling) stops the terminal from
    selecting text itself, so the app draws and copies the selection.
    """
    renderer = state.frame_renderer
    if isinstance(event, MouseEvent):
        cell = (event.y, event.x)
        if event.action == "press":
            renderer.selection = Selection(anchor=cell, focus=cell)
            state.selection_dragging = True
            state.selection_notice = None
        elif state.selection_dragging and renderer.selection is not None:
            renderer.selection.focus = cell
            if event.action == "release":
                state.selection_dragging = False
                if renderer.selection.is_empty:
                    renderer.selection = None
                    # A plain click on an activity header folds/unfolds it.
                    group_id = state.activity_header_rows.get(event.y)
                    if group_id is not None:
                        _toggle_activity_group(state, group_id, event.y)
                else:
                    _copy_selection(state)
        rerender()
        return True

    if renderer.selection is None and state.selection_notice is None:
        return False
    # Ctrl+C right after selecting is a copy, not a request to quit.
    is_copy = _is_ctrl_c(event) and renderer.selection is not None and not renderer.selection.is_empty
    if is_copy:
        _copy_selection(state)
    else:
        renderer.selection = None
    state.selection_dragging = False
    if not is_copy:
        state.selection_notice = None
    rerender()
    return is_copy


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
    return "\n".join(f"{start + i + 1}. {entry}" for i, entry in enumerate(entries[start:]))


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
