from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Protocol, runtime_checkable

from pepsicode.tooling import ToolCapability, ToolDefinition, ToolResult
from pepsicode.version import VERSION

# =============================================================================
# Security: command validation constants
# =============================================================================

# Shell metacharacters forbidden in MCP server arguments to prevent injection
DANGEROUS_SHELL_CHARS = set("|&;`$(){}<>\n\r")

# Allowlist of permitted commands (common MCP server commands)
ALLOWED_COMMANDS = {
    "node",
    "npm",
    "npx",
    "python",
    "python3",
    "pip",
    "pip3",
    "uv",
    "deno",
    "bun",
    "cargo",
    "go",
    "java",
    "javac",
    "ruby",
    "gem",
    "dotnet",
    "curl",
    "wget",
}


JsonRpcProtocol = str  # "content-length" | "newline-json" | "streamable-http"

# MCP protocol revision advertised during the initialize handshake.
MCP_PROTOCOL_VERSION = "2024-11-05"

# Anthropic caps tool names at 64 characters (``^[a-zA-Z0-9_-]{1,64}$``).
MAX_TOOL_NAME_LENGTH = 64

# Timeout defaults, in seconds.  These are deliberately generous: ``npx``
# alone costs ~1.7s to start on Windows, and a cold package fetch costs
# several seconds more, so a tight handshake budget makes stdio servers
# fail to connect at all.
DEFAULT_INIT_TIMEOUT = 30.0
DEFAULT_LIST_TIMEOUT = 30.0
DEFAULT_CALL_TIMEOUT = 120.0


@dataclass(slots=True, frozen=True)
class McpTimeouts:
    """Per-server timeout budget, resolved from config with defaults."""

    init: float = DEFAULT_INIT_TIMEOUT
    list: float = DEFAULT_LIST_TIMEOUT
    call: float = DEFAULT_CALL_TIMEOUT


def _positive_float(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _timeouts(config: dict[str, Any]) -> McpTimeouts:
    """Read ``initTimeout`` / ``listTimeout`` / ``timeout`` from a server config."""
    return McpTimeouts(
        init=_positive_float(config.get("initTimeout"), DEFAULT_INIT_TIMEOUT),
        list=_positive_float(config.get("listTimeout"), DEFAULT_LIST_TIMEOUT),
        call=_positive_float(config.get("timeout"), DEFAULT_CALL_TIMEOUT),
    )


# =============================================================================
# McpClient Protocol: unified interface for all transport types
# =============================================================================


@runtime_checkable
class McpClient(Protocol):
    """MCP 客户端统一接口，stdio 和 HTTP 传输都实现此协议。"""

    def start(self) -> None: ...
    def list_tools(self) -> list[dict[str, Any]]: ...
    def list_resources(self) -> list[dict[str, Any]]: ...
    def read_resource(self, uri: str) -> ToolResult: ...
    def list_prompts(self) -> list[dict[str, Any]]: ...
    def get_prompt(self, name: str, args: dict[str, str] | None = None) -> ToolResult: ...
    def call_tool(self, name: str, input_data: Any) -> ToolResult: ...
    def close(self) -> None: ...


# =============================================================================
# Utility functions: env interpolation, client factory, name sanitization
# =============================================================================


def _interpolate_env(value: str) -> str:
    """Replace $VAR or ${VAR} with os.environ values."""
    import re

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1) or match.group(2)
        return os.environ.get(var_name, match.group(0))

    return re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)|\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _replace, value)


def _create_client(server_name: str, config: dict[str, Any], cwd: str) -> McpClient:
    """根据配置选择 stdio 或 HTTP 传输的 MCP 客户端。"""
    if config.get("url"):
        return HttpMcpClient(server_name, config, cwd)
    return StdioMcpClient(server_name, config, cwd)


@dataclass(slots=True)
class McpServerSummary:
    name: str
    command: str
    status: str
    toolCount: int
    error: str | None = None
    protocol: str | None = None
    resourceCount: int | None = None
    promptCount: int | None = None
    # Registered (wrapped) tool names, so callers can reference the real
    # names instead of guessing them from the server name.
    toolNames: list[str] | None = None


def _sanitize_tool_segment(value: str) -> str:
    """Make a string safe for use as a tool name component (lowercase, alphanum only)."""
    normalized = "".join(char.lower() if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    normalized = normalized.strip("_")
    return normalized or "tool"


def _wrapped_tool_name(server_name: str, tool_name: str, taken: set[str]) -> str:
    """Build the ``mcp__<server>__<tool>`` alias, respecting the API name limit.

    Names longer than :data:`MAX_TOOL_NAME_LENGTH` are truncated and given a
    short stable hash suffix derived from the full name, so a long server or
    tool name degrades into a unique alias instead of a 400 from the API.
    ``taken`` is mutated with the returned name to guarantee uniqueness within
    a single registry.
    """
    full = f"mcp__{_sanitize_tool_segment(server_name)}__{_sanitize_tool_segment(tool_name)}"

    if len(full) > MAX_TOOL_NAME_LENGTH:
        digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:6]
        full = f"{full[: MAX_TOOL_NAME_LENGTH - len(digest) - 1]}_{digest}"

    if full not in taken:
        taken.add(full)
        return full

    # Collision (two distinct MCP tools sanitized to the same alias): append a
    # counter, trimming from the left to stay within the limit.
    for counter in range(2, 1000):
        suffix = f"_{counter}"
        candidate = full[: MAX_TOOL_NAME_LENGTH - len(suffix)] + suffix
        if candidate not in taken:
            taken.add(candidate)
            return candidate

    taken.add(full)
    return full


# =============================================================================
# Security: command and argument validation
# =============================================================================


def _validate_mcp_command(command: str) -> None:
    """Validate that an MCP command is safe to execute."""
    from pathlib import Path

    normalized = Path(command).resolve().as_posix()

    if ".." in normalized or "~" in normalized:
        raise RuntimeError("Invalid MCP command: contains path traversal characters")

    base_command = Path(command).name.lower()
    # Strip the .exe suffix
    if base_command.endswith(".exe"):
        base_command = base_command[:-4]

    if Path(command).is_absolute():
        # Check whether the command lives in a common system directory
        home_posix = str(Path.home().as_posix())
        allowed_system_dirs = [
            "/usr/bin",
            "/usr/local/bin",
            "/usr/local/sbin",
            "/usr/sbin",
            "/opt",
            # macOS Homebrew
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",  # Apple Silicon
            "/usr/local/Cellar",  # Intel
            # Linux extras
            "/snap/bin",  # Ubuntu Snap
            "/home/linuxbrew/.linuxbrew/bin",  # Homebrew on Linux
            # User-level tool directories (pip --user, pipx, cargo, nvm, etc.)
            f"{home_posix}/.local/bin",
            f"{home_posix}/.cargo/bin",
            f"{home_posix}/.nvm",
        ]
        if os.name == "nt":
            allowed_system_dirs.extend(
                [
                    "C:\\Program Files",
                    "C:\\Program Files (x86)",
                    "C:\\Windows\\System32",
                ]
            )

        is_in_allowed_dir = any(normalized.lower().startswith(d.lower()) for d in allowed_system_dirs)

        # Not in an allowed system directory and not in the allowlist
        if not is_in_allowed_dir and base_command not in ALLOWED_COMMANDS:
            raise RuntimeError(
                f'MCP command "{command}" is not in the allowed list. '
                f"Use a whitelisted command or place the executable in a standard system directory."
            )

        # use shell
        dangerous_shells = ["cmd.exe", "command.com", "powershell.exe", "pwsh.exe"]
        if any(normalized.lower().endswith(d) for d in dangerous_shells):
            raise RuntimeError(
                f'MCP command "{command}" is a dangerous system shell. '
                f"Direct execution of shells is not allowed for security reasons."
            )
        return

    if base_command not in ALLOWED_COMMANDS:
        raise RuntimeError(
            f'MCP command "{command}" is not in the allowed list. '
            f"Allowed commands: {', '.join(sorted(ALLOWED_COMMANDS))}. "
            f"Use absolute paths for custom commands."
        )


def _validate_mcp_args(args: list[str]) -> None:
    """Validate that MCP arguments contain no dangerous shell metacharacters."""
    for arg in args:
        for char in arg:
            if char in DANGEROUS_SHELL_CHARS:
                raise RuntimeError(
                    f"Invalid MCP argument: contains dangerous shell character '{char}'. "
                    f"MCP server arguments cannot contain shell metacharacters for security reasons."
                )


# =============================================================================
# Response formatting: convert JSON-RPC results to ToolResult
# =============================================================================


def _is_declared_read_only(declaration: Any, tool_name: str) -> bool:
    """Whether a server config declares ``tool_name`` as side-effect free.

    ``readOnlyTools`` accepts ``"*"`` (the whole server is read-only) or a list
    of tool names.  Anything else — including the field being absent — means
    the tool is treated as potentially side-effecting and goes through the
    approval gate.
    """
    if declaration == "*":
        return True
    if isinstance(declaration, (list, tuple, set)):
        return tool_name in {str(item) for item in declaration}
    return False


def _normalize_input_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    return schema if isinstance(schema, dict) else {"type": "object", "additionalProperties": True}


def _format_content_block(block: Any) -> str:
    if not isinstance(block, dict):
        return json.dumps(block, indent=2, ensure_ascii=False)
    if block.get("type") == "text" and "text" in block:
        return str(block["text"])
    return json.dumps(block, indent=2, ensure_ascii=False)


def _format_tool_call_result(result: Any) -> ToolResult:
    if not isinstance(result, dict):
        return ToolResult(ok=True, output=json.dumps(result, indent=2, ensure_ascii=False))
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list) and content:
        parts.append("\n\n".join(_format_content_block(block) for block in content))
    if "structuredContent" in result:
        parts.append("STRUCTURED_CONTENT:\n" + json.dumps(result["structuredContent"], indent=2, ensure_ascii=False))
    if not parts:
        parts.append(json.dumps(result, indent=2, ensure_ascii=False))
    return ToolResult(ok=not bool(result.get("isError")), output="\n\n".join(parts).strip())


def _format_read_resource_result(result: Any) -> ToolResult:
    if not isinstance(result, dict):
        return ToolResult(ok=False, output=json.dumps(result, indent=2, ensure_ascii=False))
    contents = result.get("contents", [])
    if not contents:
        return ToolResult(ok=True, output="No resource contents returned.")
    rendered = []
    for item in contents:
        header_lines = [f"URI: {item.get('uri', '(unknown)')}"]
        if item.get("mimeType"):
            header_lines.append(f"MIME: {item['mimeType']}")
        header = "\n".join(header_lines) + "\n\n"
        if isinstance(item.get("text"), str):
            rendered.append(header + item["text"])
        elif isinstance(item.get("blob"), str):
            rendered.append(header + "BLOB:\n" + item["blob"])
        else:
            rendered.append(header + json.dumps(item, indent=2, ensure_ascii=False))
    return ToolResult(ok=True, output="\n\n".join(rendered))


def _format_prompt_result(result: Any) -> ToolResult:
    if not isinstance(result, dict):
        return ToolResult(ok=False, output=json.dumps(result, indent=2, ensure_ascii=False))
    header = f"DESCRIPTION: {result['description']}\n\n" if result.get("description") else ""
    body_parts = []
    for message in result.get("messages", []):
        role = message.get("role", "unknown")
        content = message.get("content")
        if isinstance(content, str):
            rendered = content
        elif isinstance(content, list):
            rendered = "\n".join(
                str(part["text"])
                if isinstance(part, dict) and "text" in part
                else json.dumps(part, indent=2, ensure_ascii=False)
                for part in content
            )
        else:
            rendered = json.dumps(content, indent=2, ensure_ascii=False)
        body_parts.append(f"[{role}]\n{rendered}")
    output = (header + "\n\n".join(body_parts)).strip()
    return ToolResult(ok=True, output=output or json.dumps(result, indent=2, ensure_ascii=False))


# =============================================================================
# StdioMcpClient: communicates via subprocess stdin/stdout
# =============================================================================


class StdioMcpClient:
    def __init__(self, server_name: str, config: dict[str, Any], cwd: str) -> None:
        self.server_name = server_name
        self.config = config
        self.cwd = cwd
        self.timeouts = _timeouts(config)
        self.process: subprocess.Popen[bytes] | None = None
        self.protocol: JsonRpcProtocol | None = None
        self.next_id = 1
        self._pending: dict[int, Queue[Any]] = {}
        self._lock = threading.Lock()
        self.stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None

    def _outbound_protocol(self) -> JsonRpcProtocol:
        """Pick the framing used for messages we send.

        The MCP stdio transport is newline-delimited JSON; ``content-length``
        framing is an LSP convention that a few servers borrow.  We therefore
        default to newline-json and only use content-length when the server
        config asks for it explicitly.  Unrecognised values (e.g. the legacy
        ``"auto"``) fall back to the default rather than triggering a probe:
        re-spawning to test a second framing costs a full ``npx`` cold start.

        Inbound framing is still auto-detected in :meth:`_consume_stdout`, so
        a content-length server remains readable either way.
        """
        return "content-length" if self.config.get("protocol") == "content-length" else "newline-json"

    def start(self) -> None:
        if self.process is not None:
            return
        try:
            self._spawn_process()
            self.protocol = self._outbound_protocol()
            self.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pepsicode", "version": VERSION},
                },
                timeout_seconds=self.timeouts.init,
            )
            self.notify("notifications/initialized", {})
        except Exception as error:  # noqa: BLE001
            self.close()
            raise RuntimeError(str(error)) from error

    def _spawn_process(self) -> None:
        command = str(self.config.get("command", "")).strip()
        if not command:
            raise RuntimeError(f'MCP server "{self.server_name}" has no command configured.')

        _validate_mcp_command(command)
        _validate_mcp_args(list(self.config.get("args", []) or []))

        process_cwd = Path(self.cwd)
        if self.config.get("cwd"):
            process_cwd = (process_cwd / str(self.config["cwd"])).resolve()
        env = os.environ.copy()
        for key, value in dict(self.config.get("env", {}) or {}).items():
            env[str(key)] = str(value)

        args = list(self.config.get("args", []) or [])
        popen_kwargs: dict = {}
        argv: list[str]
        if os.name == "nt":
            # On Windows the Electron host spawns us with no real console
            # (windowsHide). Launching grandchild commands like ``npx`` (a
            # ``.cmd`` batch wrapper that itself spawns node) under the
            # inherited no-console state has been observed to crash the
            # parent Python process with 0xC0000005 (access violation) via
            # cmd.exe handle/console inheritance.
            #
            # Mitigation: CREATE_NO_WINDOW gives the child its own console
            # that is never displayed, so it neither inherits nor pops up the
            # parent's console state.
            #
            # Do NOT use DETACHED_PROCESS here.  Combined with the cmd.exe
            # wrapper below it silently severs the child's stdin/stdout
            # redirection, so every request times out and no stdio MCP server
            # can connect at all.  CREATE_NO_WINDOW provides the same console
            # isolation while keeping the pipes intact.
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            popen_kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            popen_kwargs["startupinfo"] = startupinfo

            # Route commands through cmd.exe /c so batch wrappers (npx.cmd,
            # etc.) resolve and run in the child's own context instead of
            # relying on Python's implicit PATHEXT extension resolution.
            argv = ["cmd.exe", "/c", command, *args]
        else:
            argv = [command, *args]

        try:
            self.process = subprocess.Popen(  # noqa: S603
                argv,
                cwd=str(process_cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                **popen_kwargs,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Command not found: {command}. Install it first and ensure it is available in PATH."
            ) from None

        self.stderr_lines = []
        with self._lock:
            self._pending = {}
        self._stderr_thread = threading.Thread(target=self._consume_stderr, daemon=True)
        self._stderr_thread.start()

    def _consume_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            try:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self.stderr_lines.append(text)
                    self.stderr_lines = self.stderr_lines[-8:]
            except Exception:
                continue

    def _ensure_stdout_thread(self) -> None:
        if self._stdout_thread is not None:
            return
        self._stdout_thread = threading.Thread(target=self._consume_stdout, daemon=True)
        self._stdout_thread.start()

    def _consume_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line_bytes = self.process.stdout.readline()
                if not line_bytes:
                    break

                try:
                    line = line_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    continue

                stripped = line.strip()
                if not stripped:
                    continue

                # Auto-detect protocol if not determined yet
                if self.protocol is None:
                    if line.lower().startswith("content-length:"):
                        self.protocol = "content-length"
                    else:
                        self.protocol = "newline-json"

                if self.protocol == "newline-json":
                    try:
                        self._handle_message(json.loads(stripped))
                    except json.JSONDecodeError:
                        continue
                else:
                    # Content-length protocol
                    # The current 'line' is the first header line
                    header_lines = [line.rstrip("\r\n")]
                    while True:
                        next_line_bytes = self.process.stdout.readline()
                        if not next_line_bytes:
                            return
                        try:
                            next_line = next_line_bytes.decode("utf-8")
                        except UnicodeDecodeError:
                            return
                        h_stripped = next_line.rstrip("\r\n")
                        if h_stripped == "":
                            break
                        header_lines.append(h_stripped)

                    content_length = 0
                    for header in header_lines:
                        if header.lower().startswith("content-length:"):
                            try:
                                content_length = int(header.split(":", 1)[1].strip())
                            except ValueError:
                                pass
                            break

                    if content_length > 0:
                        body_bytes = self.process.stdout.read(content_length)
                        if len(body_bytes) < content_length:
                            return
                        try:
                            self._handle_message(json.loads(body_bytes.decode("utf-8")))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
        finally:
            # Bug 2: Notify pending requests when process exits
            if self.process:
                exit_code = self.process.poll()
                error_msg = {"error": {"code": -1, "message": f"MCP server process exited (code={exit_code})"}}
                with self._lock:
                    for req_id, q in list(self._pending.items()):
                        q.put(error_msg)
                    self._pending.clear()

    def _handle_message(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if not isinstance(message_id, int):
            return
        with self._lock:
            queue = self._pending.pop(message_id, None)
            if queue is not None:
                queue.put(message)

    def send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError(f'MCP server "{self.server_name}" is not running.')

        payload_bytes = json.dumps(message, ensure_ascii=False).encode("utf-8")

        if self.protocol == "newline-json":
            self.process.stdin.write(payload_bytes + b"\n")
            self.process.stdin.flush()
            self._ensure_stdout_thread()
            return

        header = f"Content-Length: {len(payload_bytes)}\r\n\r\n".encode()
        self.process.stdin.write(header + payload_bytes)
        self.process.stdin.flush()
        self._ensure_stdout_thread()

    def notify(self, method: str, params: Any) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Any, timeout_seconds: float | None = None) -> Any:
        if timeout_seconds is None:
            timeout_seconds = self.timeouts.call
        message_id = self.next_id
        self.next_id += 1
        response_queue: Queue[Any] = Queue(maxsize=1)
        with self._lock:
            self._pending[message_id] = response_queue
        self.send({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params})
        try:
            message = response_queue.get(timeout=timeout_seconds)
        except Empty as error:
            with self._lock:
                self._pending.pop(message_id, None)
            stderr = "\n".join(self.stderr_lines)
            raise RuntimeError(
                f"MCP {self.server_name}: request timed out for {method}" + (f"\n{stderr}" if stderr else "")
            ) from error
        if message.get("error"):
            details = message["error"].get("data")
            suffix = f"\n{json.dumps(details, indent=2, ensure_ascii=False)}" if details else ""
            raise RuntimeError(f"MCP {self.server_name}: {message['error']['message']}{suffix}")
        return message.get("result")

    def list_tools(self) -> list[dict[str, Any]]:
        result = self.request("tools/list", {}, timeout_seconds=self.timeouts.list)
        return list(result.get("tools", []) if isinstance(result, dict) else [])

    def list_resources(self) -> list[dict[str, Any]]:
        result = self.request("resources/list", {}, timeout_seconds=self.timeouts.list)
        return list(result.get("resources", []) if isinstance(result, dict) else [])

    def read_resource(self, uri: str) -> ToolResult:
        return _format_read_resource_result(
            self.request("resources/read", {"uri": uri}, timeout_seconds=self.timeouts.call)
        )

    def list_prompts(self) -> list[dict[str, Any]]:
        result = self.request("prompts/list", {}, timeout_seconds=self.timeouts.list)
        return list(result.get("prompts", []) if isinstance(result, dict) else [])

    def get_prompt(self, name: str, args: dict[str, str] | None = None) -> ToolResult:
        return _format_prompt_result(
            self.request("prompts/get", {"name": name, "arguments": args or {}}, timeout_seconds=self.timeouts.call)
        )

    def call_tool(self, name: str, input_data: Any) -> ToolResult:
        return _format_tool_call_result(
            self.request(
                "tools/call",
                {"name": name, "arguments": input_data or {}},
                timeout_seconds=self.timeouts.call,
            )
        )

    def close(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            for queue in pending:
                queue.put(
                    {"error": {"message": f'MCP server "{self.server_name}" closed before completing the request.'}}
                )

        if self.process is not None:
            try:
                if os.name == "nt":
                    try:
                        from pepsicode.subprocess_utils import hide_window_kwargs

                        subprocess.run(
                            ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                            capture_output=True,
                            timeout=5,
                            **hide_window_kwargs(),
                        )
                    except subprocess.TimeoutExpired:
                        # taskkill failed, use kill
                        try:
                            self.process.kill()
                        except OSError:
                            pass
                    except Exception:
                        try:
                            self.process.kill()
                        except OSError:
                            pass
                else:
                    # Unix: send SIGTERM then SIGKILL
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            self.process.kill()
                        except OSError:
                            pass

                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass
            finally:
                self.process = None

        self.protocol = None
        self._stdout_thread = None
        self._stderr_thread = None


# =============================================================================
# HttpMcpClient: communicates via HTTP POST (Streamable HTTP transport)
# =============================================================================


class HttpMcpClient:
    """通过 Streamable HTTP（HTTP POST）与 MCP 服务器通信的客户端。"""

    def __init__(self, server_name: str, config: dict[str, Any], cwd: str) -> None:
        self.server_name = server_name
        self.config = config
        self.cwd = cwd
        self.url: str = str(config.get("url", ""))
        self.protocol: JsonRpcProtocol = "streamable-http"
        self.timeouts = _timeouts(config)
        self.next_id = 1
        self._headers: dict[str, str] = {}
        # Assigned from the initialize response; the Streamable HTTP transport
        # requires it to be echoed on every subsequent request.
        self._session_id: str | None = None
        self._build_headers()

    def _build_headers(self) -> None:
        """构建请求头：先注入 Bearer Token，再合并用户自定义 headers。"""
        # 从 token 存储注入 Bearer Token
        try:
            from pepsicode.config import read_mcp_tokens

            tokens = read_mcp_tokens()
            token = tokens.get(self.server_name)
            if token:
                self._headers["Authorization"] = f"Bearer {token}"
        except Exception:
            pass

        # 合并用户自定义 headers（支持 $ENV_VAR 插值）
        for key, value in self.config.get("headers", {}).items():
            self._headers[str(key)] = _interpolate_env(str(value))

    def start(self) -> None:
        """发送 initialize 握手请求，并记录服务器返回的会话 ID。"""
        if not self.url:
            raise RuntimeError(f'MCP server "{self.server_name}" has no url configured.')
        self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pepsicode", "version": VERSION},
            },
            timeout_seconds=self.timeouts.init,
            capture_session=True,
        )
        self._notify("notifications/initialized", {})

    def _request_headers(self) -> dict[str, str]:
        """构建每次请求的头部：基础头 + 协议版本 + 会话 ID。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            **self._headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _request(
        self,
        method: str,
        params: Any,
        timeout_seconds: float | None = None,
        *,
        capture_session: bool = False,
    ) -> Any:
        """发送 JSON-RPC 请求并等待响应。"""
        if timeout_seconds is None:
            timeout_seconds = self.timeouts.call
        message_id = self.next_id
        self.next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": method,
            "params": params,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=self._request_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                if capture_session:
                    session_id = resp.headers.get("Mcp-Session-Id")
                    if session_id:
                        self._session_id = str(session_id)
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(
                f"MCP {self.server_name}: HTTP {e.code} {e.reason}" + (f"\n{error_body}" if error_body else "")
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"MCP {self.server_name}: connection failed: {e.reason}") from e

        # 解析响应（可能是 SSE 格式或纯 JSON）
        result = self._parse_response(raw)
        if result.get("error"):
            details = result["error"].get("data")
            suffix = f"\n{json.dumps(details, indent=2, ensure_ascii=False)}" if details else ""
            raise RuntimeError(f"MCP {self.server_name}: {result['error']['message']}{suffix}")
        return result.get("result")

    def _parse_response(self, raw: str) -> dict[str, Any]:
        """解析 HTTP 响应体，支持纯 JSON 和 SSE 格式。"""
        # 尝试直接解析为 JSON
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # 尝试从 SSE 格式提取最后一条 JSON 消息
        last_json = None
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[len("data:") :].strip()
                if data_str:
                    try:
                        last_json = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
        if last_json is not None:
            return last_json
        raise RuntimeError(f"MCP {self.server_name}: failed to parse response:\n{raw[:500]}")

    def _notify(self, method: str, params: Any) -> None:
        """发送 JSON-RPC 通知（无 id，不等待响应）。"""
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers=self._request_headers(), method="POST")
        try:
            urllib.request.urlopen(req, timeout=self.timeouts.list).read()
        except Exception:
            pass  # 通知不需要处理响应

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {}, timeout_seconds=self.timeouts.list)
        return list(result.get("tools", []) if isinstance(result, dict) else [])

    def list_resources(self) -> list[dict[str, Any]]:
        result = self._request("resources/list", {}, timeout_seconds=self.timeouts.list)
        return list(result.get("resources", []) if isinstance(result, dict) else [])

    def read_resource(self, uri: str) -> ToolResult:
        return _format_read_resource_result(
            self._request("resources/read", {"uri": uri}, timeout_seconds=self.timeouts.call)
        )

    def list_prompts(self) -> list[dict[str, Any]]:
        result = self._request("prompts/list", {}, timeout_seconds=self.timeouts.list)
        return list(result.get("prompts", []) if isinstance(result, dict) else [])

    def get_prompt(self, name: str, args: dict[str, str] | None = None) -> ToolResult:
        return _format_prompt_result(
            self._request("prompts/get", {"name": name, "arguments": args or {}}, timeout_seconds=self.timeouts.call)
        )

    def call_tool(self, name: str, input_data: Any) -> ToolResult:
        return _format_tool_call_result(
            self._request(
                "tools/call",
                {"name": name, "arguments": input_data or {}},
                timeout_seconds=self.timeouts.call,
            )
        )

    def close(self) -> None:
        """结束 Streamable HTTP 会话（若服务器分配了 session id）。"""
        if not self._session_id or not self.url:
            return
        req = urllib.request.Request(self.url, headers=self._request_headers(), method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=self.timeouts.list).read()
        except Exception:
            pass  # 服务器可能不支持 DELETE；断开本身不应报错
        finally:
            self._session_id = None


# =============================================================================
# create_mcp_backed_tools: main entry point — wire up all MCP servers as tools
# =============================================================================


def create_mcp_backed_tools(*, cwd: str, mcp_servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Connect to all configured MCP servers and wrap their capabilities as local tools.

    Returns a dict with:
      - tools: list of ToolDefinition (the actual tools the LLM can call)
      - servers: list of server summary dicts (for status display)
      - dispose: callable to shut down all MCP connections
    """
    clients: list[McpClient] = []
    tools: list[ToolDefinition] = []
    servers: list[dict[str, Any]] = []
    resource_index: dict[str, dict[str, Any]] = {}
    prompt_index: dict[str, dict[str, Any]] = {}
    taken_names: set[str] = set()

    try:
        # --- Phase 1: connect to each server, discover tools/resources/prompts ---
        for server_name, config in mcp_servers.items():
            if config.get("enabled") is False:
                servers.append(
                    asdict(
                        McpServerSummary(
                            name=server_name,
                            command=config.get("command", ""),
                            status="disabled",
                            toolCount=0,
                            protocol=config.get("protocol"),
                        )
                    )
                )
                continue

            client = _create_client(server_name, config, cwd)
            try:
                client.start()
                descriptors = client.list_tools()
                try:
                    resources = client.list_resources()
                except Exception:  # noqa: BLE001
                    resources = []
                try:
                    prompts = client.list_prompts()
                except Exception:  # noqa: BLE001
                    prompts = []
                clients.append(client)

                # Index resources and prompts for later meta-tool creation
                for resource in resources:
                    resource_index[f"{server_name}:{resource.get('uri')}"] = {
                        "serverName": server_name,
                        "resource": resource,
                    }
                for prompt in prompts:
                    prompt_index[f"{server_name}:{prompt.get('name')}"] = {"serverName": server_name, "prompt": prompt}

                # Tools the server config declares side-effect free.  These get
                # the READ_ONLY capability, which lets them run in Plan mode and
                # skips the approval prompt.  Everything else is gated.
                read_only_declaration = config.get("readOnlyTools")

                # Wrap each MCP tool as a local ToolDefinition
                # Naming convention: mcp__<server>__<tool>
                server_tool_names: list[str] = []
                for descriptor in descriptors:
                    descriptor_name = str(descriptor.get("name", "tool"))
                    wrapped_name = _wrapped_tool_name(server_name, descriptor_name, taken_names)
                    input_schema = _normalize_input_schema(descriptor.get("inputSchema"))

                    def _validator(value: Any) -> Any:
                        return value

                    def _run(input_data: Any, _context, *, _client=client, _descriptor_name=descriptor_name):
                        return _client.call_tool(_descriptor_name, input_data)

                    # Always state the true server/tool identity: the wrapped
                    # name may have been truncated to fit the API name limit.
                    description = f"[MCP {server_name}/{descriptor_name}] " + str(
                        descriptor.get("description") or f"Call MCP tool {descriptor_name} from server {server_name}."
                    )

                    capabilities: set[ToolCapability] = set()
                    if _is_declared_read_only(read_only_declaration, descriptor_name):
                        capabilities.add(ToolCapability.READ_ONLY)

                    tools.append(
                        ToolDefinition(
                            name=wrapped_name,
                            description=description,
                            input_schema=input_schema,
                            validator=_validator,
                            run=_run,
                            capabilities=capabilities,
                        )
                    )
                    server_tool_names.append(wrapped_name)

                servers.append(
                    asdict(
                        McpServerSummary(
                            name=server_name,
                            command=config.get("command", ""),
                            status="connected",
                            toolCount=len(descriptors),
                            protocol=client.protocol,
                            resourceCount=len(resources),
                            promptCount=len(prompts),
                            toolNames=server_tool_names,
                        )
                    )
                )
            except Exception as error:  # noqa: BLE001
                client.close()
                servers.append(
                    asdict(
                        McpServerSummary(
                            name=server_name,
                            command=config.get("command", ""),
                            status="error",
                            toolCount=0,
                            error=str(error),
                            protocol=config.get("protocol"),
                        )
                    )
                )
    except Exception:
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
        raise

    # --- Phase 2: create meta-tools for resources and prompts ---
    # These are built-in tools that let the LLM browse MCP resources/prompts
    if resource_index:
        tools.append(
            ToolDefinition(
                name="list_mcp_resources",
                description="List available MCP resources exposed by connected MCP servers.",
                input_schema={"type": "object", "properties": {"server": {"type": "string"}}},
                validator=lambda value: (
                    {"server": value.get("server")} if isinstance(value, dict) else {"server": None}
                ),
                run=lambda input_data, _context: ToolResult(
                    ok=True,
                    output="\n".join(
                        f"{entry['serverName']}: {entry['resource'].get('uri')}"
                        + (f" ({entry['resource'].get('name')})" if entry["resource"].get("name") else "")
                        + (f" - {entry['resource'].get('description')}" if entry["resource"].get("description") else "")
                        for entry in resource_index.values()
                        if not input_data.get("server") or entry["serverName"] == input_data["server"]
                    )
                    or "No MCP resources available.",
                ),
                capabilities={ToolCapability.READ_ONLY},
            )
        )

        def _read_resource(input_data: dict, _context) -> ToolResult:
            client = next((item for item in clients if item.server_name == input_data["server"]), None)
            if client is None:
                return ToolResult(ok=False, output=f"Unknown MCP server: {input_data['server']}")
            return client.read_resource(input_data["uri"])

        tools.append(
            ToolDefinition(
                name="read_mcp_resource",
                description="Read a specific MCP resource by server and URI.",
                input_schema={
                    "type": "object",
                    "properties": {"server": {"type": "string"}, "uri": {"type": "string"}},
                    "required": ["server", "uri"],
                },
                validator=lambda value: value,
                run=_read_resource,
                capabilities={ToolCapability.READ_ONLY},
            )
        )

    if prompt_index:
        tools.append(
            ToolDefinition(
                name="list_mcp_prompts",
                description="List available MCP prompts exposed by connected MCP servers.",
                input_schema={"type": "object", "properties": {"server": {"type": "string"}}},
                validator=lambda value: (
                    {"server": value.get("server")} if isinstance(value, dict) else {"server": None}
                ),
                run=lambda input_data, _context: ToolResult(
                    ok=True,
                    output="\n".join(
                        f"{entry['serverName']}: {entry['prompt'].get('name')}"
                        + (
                            " args=["
                            + ", ".join(
                                f"{arg.get('name')}{'*' if arg.get('required') else ''}"
                                for arg in entry["prompt"].get("arguments", [])
                            )
                            + "]"
                            if entry["prompt"].get("arguments")
                            else ""
                        )
                        + (f" - {entry['prompt'].get('description')}" if entry["prompt"].get("description") else "")
                        for entry in prompt_index.values()
                        if not input_data.get("server") or entry["serverName"] == input_data["server"]
                    )
                    or "No MCP prompts available.",
                ),
                capabilities={ToolCapability.READ_ONLY},
            )
        )

        def _get_prompt(input_data: dict, _context) -> ToolResult:
            client = next((item for item in clients if item.server_name == input_data["server"]), None)
            if client is None:
                return ToolResult(ok=False, output=f"Unknown MCP server: {input_data['server']}")
            return client.get_prompt(input_data["name"], input_data.get("arguments"))

        tools.append(
            ToolDefinition(
                name="get_mcp_prompt",
                description="Fetch a rendered MCP prompt by server, prompt name, and optional arguments.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "name": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["server", "name"],
                },
                validator=lambda value: value,
                run=_get_prompt,
                capabilities={ToolCapability.READ_ONLY},
            )
        )

    return {
        "tools": tools,
        "servers": servers,
        "dispose": lambda: [client.close() for client in clients],
    }
