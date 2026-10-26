"""The policy-checked process launch point used by tools and capability providers."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from pepsicode.cancellation import TurnCancelledError
from pepsicode.execution.runtime import current_scope, current_tool_context
from pepsicode.sandbox import SandboxPolicy
from pepsicode.sandbox.backend import SandboxUnavailableError
from pepsicode.sandbox.types import SandboxMode
from pepsicode.subprocess_utils import WindowsProcessJob, hide_window_kwargs

logger = logging.getLogger(__name__)


def _context(context, cwd):
    if context is not None:
        scope = getattr(context, "execution", None) or current_scope()
        if scope and context.permissions is None:
            context.permissions = scope.permissions
        if scope and context.cancellation_event is None:
            context.cancellation_event = scope.cancellation_event
        return context
    context = current_tool_context()
    if context is not None:
        return _context(context, cwd)
    from pepsicode.tooling import ToolContext

    scope = current_scope()
    return ToolContext(
        str(scope.cwd if scope else cwd or Path.cwd()),
        permissions=scope.permissions if scope else None,
        cancellation_event=scope.cancellation_event if scope else None,
        execution=scope,
    )


def checked_argv(argv, *, context=None, cwd=None, reason=None):
    from pepsicode.workspace import resolve_tool_path

    if (
        not isinstance(argv, (list, tuple))
        or not argv
        or not all(isinstance(item, str) and "\0" not in item for item in argv)
    ):
        raise ValueError("Process execution requires a non-empty literal argv")
    context = _context(context, cwd)
    scope = getattr(context, "execution", None) or current_scope()
    if scope:
        scope.check_cancelled()
        if scope.has_unknown:
            raise PermissionError("Resolve the previous unknown operation before starting more processes")
    if context.cancellation_event is not None and context.cancellation_event.is_set():
        raise TurnCancelledError("Process cancelled before dispatch")
    target = resolve_tool_path(context, str(cwd or context.cwd), "command_cwd")
    permissions = context.permissions
    actual = list(argv)
    enforcement = "none"
    if permissions is not None:
        mode = permissions.mode
        command = Path(actual[0]).name.lower()
        force = reason or (
            None
            if command in {"echo", "pwd", "hostname", "whoami"}
            else "Run a local program with the current user's process permissions"
        )
        if command in {"docker", "docker.exe", "sed", "sed.exe", "rg", "rg.exe"}:
            force = force or "This command can execute code or affect external resources"
        if command in {"git", "git.exe"} and any(
            arg in {"commit", "config", "worktree", "merge", "rebase", "push", "reset", "clean", "add"}
            for arg in actual[1:]
        ):
            force = force or "This Git operation changes repository or external state"
        outcome = permissions.check_command(actual[0], actual[1:], str(target), force_prompt_reason=force)
        if outcome.is_denied or outcome.is_unavailable:
            raise PermissionError(outcome.denial_message(scope="process"))
        policy = SandboxPolicy(
            getattr(permissions, "process_sandbox_mode", SandboxMode.DANGER_FULL_ACCESS), permissions.workspace_root
        )
        confined = permissions.sandbox.confine(actual, policy)
        if policy.mode != SandboxMode.DANGER_FULL_ACCESS and confined.enforcement != "full":
            raise SandboxUnavailableError("Requested process confinement is unavailable; command not started")
        actual = confined.argv
        enforcement = confined.enforcement
        if permissions.mode != mode:
            raise PermissionError("Permissions changed during process approval")
        if resolve_tool_path(context, str(cwd or context.cwd), "command_cwd") != target:
            raise PermissionError("Process working directory changed during approval")
    if scope:
        scope.check_cancelled()
    if context.cancellation_event is not None and context.cancellation_event.is_set():
        raise TurnCancelledError("Process cancelled before dispatch")
    return actual, str(target), context, enforcement


def Popen(argv, *, context=None, reason=None, persistent=False, **kwargs):  # noqa: N802
    if kwargs.pop("shell", False):
        raise ValueError("Shell strings must be represented by an explicit shell argv")
    if kwargs.get("executable") is not None or kwargs.get("preexec_fn") is not None:
        raise ValueError("Process executable overrides and preexec hooks bypass reviewed argv")
    if kwargs.get("start_new_session") is False:
        raise ValueError("Processes must belong to an owned process group")
    actual, cwd, context, enforcement = checked_argv(argv, context=context, cwd=kwargs.pop("cwd", None), reason=reason)
    scope = getattr(context, "execution", None) or current_scope()
    if scope:
        scope.record("process/prepared", argv=actual, cwd=cwd, enforcement=enforcement)
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
    else:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(actual, cwd=cwd, **hide_window_kwargs(kwargs))
    process._pepsi_stop_lock = threading.RLock()
    process._pepsi_job = None
    try:
        if os.name == "nt":
            process._pepsi_job = WindowsProcessJob(process)
        if scope:
            scope.record(
                "process/started", pid=process.pid, argv=actual, persistent=persistent, enforcement=enforcement
            )
        if context.permissions is not None and hasattr(context.permissions, "_processes"):
            with context.permissions._process_lock:
                context.permissions._processes.add(process)
        if persistent:
            threading.Thread(target=_watch_background, args=(process, context, scope), daemon=True).start()
    except BaseException:
        stop(process, process._pepsi_job)
        if process._pepsi_job is not None:
            process._pepsi_job.close()
        raise
    return process


def stop(process, job=None):
    """Stop only this owned tree, and wait for its pipes and process to settle."""
    lock = getattr(process, "_pepsi_stop_lock", None)
    if lock is None:
        return _stop(process, job)
    with lock:
        return _stop(process, job)


def _stop(process, job):
    if os.name == "nt" and job is not None:
        job.terminate()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        # Trusted teardown of an owned PID; this is not an agent command.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=5, **hide_window_kwargs()
        )
        if process.poll() is None:
            process.kill()
    if process.poll() is None:
        process.communicate(timeout=5)


def _watch_background(process, context, scope):
    stopped = False
    job = process._pepsi_job
    try:
        while process.poll() is None:
            cancelled = context.cancellation_event is not None and context.cancellation_event.is_set()
            expired = scope is not None and scope.deadline is not None and time.monotonic() >= scope.deadline
            if cancelled or expired:
                stopped = True
                stop(process, job)
                break
            time.sleep(0.05)
        if scope:
            state = "unknown" if stopped or process.returncode else "completed"
            scope.record(
                "background/settled",
                call_id=context.tool_use_id,
                pid=process.pid,
                state=state,
                exit_code=process.returncode,
            )
            if state == "unknown":
                scope.mark_unknown(context.tool_use_id, tool="background process", pid=process.pid)
    except Exception as error:
        if scope:
            scope.mark_unknown(context.tool_use_id, tool="background process", pid=process.pid)
        logger.warning("Background process outcome could not be committed: %s", type(error).__name__)
    finally:
        with process._pepsi_stop_lock:
            if job is not None:
                job.terminate()
                job.close()
        if context.permissions is not None and hasattr(context.permissions, "_processes"):
            with context.permissions._process_lock:
                context.permissions._processes.discard(process)


def run(argv, *, context=None, reason=None, timeout=None, capture_output=False, input=None, check=False, **kwargs):
    context = _context(context, kwargs.get("cwd"))
    scope = getattr(context, "execution", None) or current_scope()
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if kwargs.get("stdin") is None:
        kwargs["stdin"] = subprocess.DEVNULL
    process = Popen(argv, context=context, reason=reason, **kwargs)
    context.effect_state = "started"
    job = getattr(process, "_pepsi_job", None)
    try:
        deadline = time.monotonic() + timeout if timeout is not None else None
        if scope and scope.deadline is not None:
            deadline = min(deadline, scope.deadline) if deadline is not None else scope.deadline
        first = True
        while True:
            if context.cancellation_event is not None and context.cancellation_event.is_set():
                stop(process, job)
                context.effect_state = "unknown"
                raise TurnCancelledError("Command cancelled after dispatch; side effects may have occurred")
            if deadline is not None and time.monotonic() >= deadline:
                stop(process, job)
                context.effect_state = "unknown"
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                stdout, stderr = process.communicate(input if first else None, timeout=0.05)
                result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                context.effect_state = "completed"
                if scope:
                    scope.record("process/settled", pid=process.pid, exit_code=process.returncode)
                if check:
                    result.check_returncode()
                return result
            except subprocess.TimeoutExpired:
                first = False
    finally:
        if process.poll() is None:
            stop(process, job)
        if job is not None:
            # Also drain descendants that detached their stdio before exit.
            job.terminate()
            job.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if context.permissions is not None and hasattr(context.permissions, "_processes"):
            with context.permissions._process_lock:
                context.permissions._processes.discard(process)
