from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Sequence

from pepsicode.cancellation import TurnCancelledError
from pepsicode.core.background_tasks import register_background_shell_task
from pepsicode.execution import process as process_service
from pepsicode.tooling import ToolDefinition, ToolResult
from pepsicode.workspace import resolve_tool_path

# Command execution timeout (seconds): 5 minutes
COMMAND_TIMEOUT = 300

# Read-only commands that never need permission prompts.
# Includes both Unix and Windows equivalents.
READONLY_COMMANDS = {
    # Unix
    "pwd",
    "ls",
    "find",
    "rg",
    "grep",
    "cat",
    "head",
    "tail",
    "wc",
    "sed",
    "echo",
    "df",
    "du",
    "whoami",
    # Windows equivalents
    "dir",
    "type",
    "where",
    "findstr",
    "more",
    "hostname",
}

# Development commands (write access but commonly allowed).
DEVELOPMENT_COMMANDS = {
    "git",
    "npm",
    "node",
    "python",
    "python3",
    "pytest",
    "bash",
    "sh",
    # Windows-common development tools
    "pip",
    "pip3",
    "cargo",
    "go",
    "make",
    "cmake",
    "dotnet",
    "powershell",
    "pwsh",
    "cmd",
}


def split_command_line(command_line: str) -> list[str]:
    """Split a command string into tokens.

    On Windows, ``shlex.split(posix=True)`` can choke on backslash paths
    (e.g. ``C:\\Users\\foo``).  We fall back to ``posix=False`` which
    preserves backslashes, then try the native ``shlex.split`` as a
    last resort.
    """
    if os.name == "nt":
        try:
            return shlex.split(command_line, posix=False)
        except ValueError:
            # If even non-posix fails, fall back to simple whitespace split
            return command_line.split()
    return shlex.split(command_line, posix=True)


def _is_allowed_command(command: str) -> bool:
    cmd = command.lower() if os.name == "nt" else command
    return cmd in READONLY_COMMANDS or cmd in DEVELOPMENT_COMMANDS


def _is_read_only_command(command: str) -> bool:
    cmd = command.lower() if os.name == "nt" else command
    return cmd in READONLY_COMMANDS


def _looks_like_shell_snippet(command: str, args: list[str]) -> bool:
    return not args and any(char in command for char in "|&;<>()$`")


def _is_background_shell_snippet(command: str, args: list[str]) -> bool:
    trimmed = command.strip()
    return not args and trimmed.endswith("&") and not trimmed.endswith("&&")


def _strip_trailing_background_operator(command: str) -> str:
    return command.strip().removesuffix("&").strip()


def _normalize_command_input(input_data: dict, *, preserve_argv: bool = False) -> tuple[str, list[str]]:
    command = str(input_data.get("command", ""))
    if not preserve_argv:
        command = command.strip()
    raw_args = input_data.get("args") or []
    if raw_args or preserve_argv:
        return command, [str(arg) for arg in raw_args]
    parsed = split_command_line(command) if command else []
    return (parsed[0], parsed[1:]) if parsed else ("", [])


def _is_windows_shell_builtin(command: str) -> bool:
    return os.name == "nt" and command.lower() in {
        "cd",
        "chdir",
        "cls",
        "copy",
        "date",
        "del",
        "dir",
        "echo",
        "erase",
        "md",
        "mkdir",
        "mklink",
        "move",
        "rd",
        "ren",
        "rename",
        "rmdir",
        "time",
        "type",
        "ver",
        "vol",
    }


def _build_execution_command(
    raw_command: str,
    normalized_command: str,
    normalized_args: Sequence[str],
    *,
    use_shell: bool,
    background_shell: bool,
) -> tuple[str, list[str]]:
    if use_shell:
        shell_command = _strip_trailing_background_operator(raw_command) if background_shell else raw_command
        if os.name == "nt":
            return "cmd", ["/d", "/s", "/c", shell_command]
        # Use the user's preferred shell (macOS defaults to zsh since
        # Catalina).  Fall back to /bin/sh for maximum POSIX compatibility.
        shell = os.environ.get("SHELL", "/bin/sh")
        return shell, ["-lc", shell_command]
    if _is_windows_shell_builtin(normalized_command):
        quoted_args = subprocess.list2cmdline(list(normalized_args))
        shell_command = normalized_command if not quoted_args else f"{normalized_command} {quoted_args}"
        return "cmd", ["/d", "/s", "/c", shell_command]
    return normalized_command, list(normalized_args)


def _validate(input_data: dict) -> dict:
    command = input_data.get("command")
    if not isinstance(command, str):
        raise ValueError("command is required")
    args = input_data.get("args") or []
    if not isinstance(args, list):
        raise ValueError("args must be a list")
    cwd = input_data.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("cwd must be a string")
    timeout = input_data.get("timeout", COMMAND_TIMEOUT)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= COMMAND_TIMEOUT:
        raise ValueError(f"timeout must be an integer between 1 and {COMMAND_TIMEOUT}")
    return {"command": command, "args": [str(arg) for arg in args], "cwd": cwd, "timeout": timeout}


def _run(input_data: dict, context) -> ToolResult:
    environment = {**os.environ, **(getattr(context, "environment", None) or {})}
    effective_cwd = str(resolve_tool_path(context, input_data["cwd"], "list")) if input_data.get("cwd") else context.cwd
    timeout = int(input_data.get("timeout", COMMAND_TIMEOUT))
    preserve_argv = getattr(context, "command_is_argv", False)
    normalized_command, normalized_args = _normalize_command_input(input_data, preserve_argv=preserve_argv)
    if not normalized_command:
        return ToolResult(ok=False, output="Command not allowed: empty command")

    raw_args = input_data.get("args") or []
    use_shell = not preserve_argv and _looks_like_shell_snippet(input_data["command"], raw_args)
    background_shell = not preserve_argv and _is_background_shell_snippet(input_data["command"], raw_args)
    if (
        preserve_argv
        and _is_windows_shell_builtin(normalized_command)
        and any(re.search(r'["&|<>^%!\r\n]', arg) for arg in normalized_args)
    ):
        return ToolResult(
            False,
            "Shell control characters cannot be represented as literal argv for Windows builtins; use a direct executable or wrapper",
            status="blocked",
        )
    known_command = _is_allowed_command(normalized_command)

    command, args = _build_execution_command(
        input_data["command"],
        normalized_command,
        normalized_args,
        use_shell=use_shell,
        background_shell=background_shell,
    )
    force_prompt_reason = (
        None
        if use_shell or known_command
        else f"Unknown command '{normalized_command}' is not in the built-in read-only/development set"
    )

    if use_shell and background_shell:
        from pepsicode.subprocess_utils import hide_window_kwargs

        # Platform-specific process isolation flags
        popen_kwargs: dict = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            # On Unix, start the background process in its own session so
            # it is not killed when the parent terminal closes.
            popen_kwargs["start_new_session"] = True
        # Merge in CREATE_NO_WINDOW + STARTUPINFO on Windows.
        popen_kwargs.update(hide_window_kwargs(popen_kwargs))

        child = process_service.Popen(  # noqa: S603
            [command, *args],
            context=context,
            reason=force_prompt_reason,
            persistent=True,
            cwd=effective_cwd,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **popen_kwargs,
        )

        if child.pid is None:
            return ToolResult(
                ok=False,
                output="Failed to get PID for background command. Process may have exited immediately.",
            )

        background_task = register_background_shell_task(
            command=_strip_trailing_background_operator(input_data["command"]),
            pid=child.pid,
            cwd=effective_cwd,
        )
        return ToolResult(
            ok=True,
            output=f"Background command started.\nTASK: {background_task.taskId}\nPID: {background_task.pid}",
            backgroundTask=background_task,
            status="running",
        )

    try:
        completed = process_service.run(
            [command, *args],
            context=context,
            reason=force_prompt_reason,
            cwd=effective_cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
        return ToolResult(ok=completed.returncode == 0, output=output, exit_code=completed.returncode)
    except PermissionError as error:
        return ToolResult(False, str(error), status="blocked")
    except TurnCancelledError as error:
        return ToolResult(False, str(error), status="cancelled")
    except subprocess.TimeoutExpired:
        return ToolResult(
            False,
            f"Command timed out after {timeout} seconds; partial side effects may have occurred.",
            status="timed_out",
        )


run_command_tool = ToolDefinition(
    name="run_command",
    description="Run a common development command from an allowlist.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "args": {"type": "array"},
            "cwd": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": COMMAND_TIMEOUT},
        },
        "required": ["command"],
    },
    validator=_validate,
    run=_run,
)
