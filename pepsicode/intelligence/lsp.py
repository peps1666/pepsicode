"""Bounded stdio LSP client. Servers are started only when a tool needs them."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from pepsicode.cancellation import TurnCancelledError
from pepsicode.subprocess_utils import hide_window_kwargs

LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
}
DEFAULT_SERVERS = {
    "python": [("basedpyright-langserver", ["--stdio"]), ("pyright-langserver", ["--stdio"]), ("pylsp", [])],
    "typescript": [("typescript-language-server", ["--stdio"])],
    "go": [("gopls", [])],
    "rust": [("rust-analyzer", [])],
}


def language_for(path: Path) -> str:
    return LANGUAGES.get(path.suffix.lower(), "")


def document_key(uri: str) -> str:
    return uri.lower() if os.name == "nt" else uri


def document_lines(text: str) -> list[str]:
    # LSP line endings are CRLF, CR or LF. Unicode separators and form-feed
    # inside literals are ordinary document characters, not extra lines.
    parts = re.split(r"(\r\n|\r|\n)", text)
    return [parts[index] + (parts[index + 1] if index + 1 < len(parts) else "") for index in range(0, len(parts), 2)]


def position(text: str, line: int, column: int) -> dict:
    """Convert user-facing 1-based Unicode columns to LSP UTF-16 positions."""
    lines = document_lines(text)
    if not 1 <= line <= len(lines) or not 1 <= column <= len(lines[line - 1].rstrip("\r\n")) + 1:
        raise ValueError("line or column is outside the document")
    return {"line": line - 1, "character": len(lines[line - 1][: column - 1].encode("utf-16-le")) // 2}


def offset(text: str, location: dict) -> int:
    lines = document_lines(text)
    line, character = location.get("line"), location.get("character")
    if isinstance(line, bool) or not isinstance(line, int) or not 0 <= line < len(lines):
        raise ValueError("Invalid edit line")
    if isinstance(character, bool) or not isinstance(character, int) or character < 0:
        raise ValueError("Invalid edit character")
    units = 0
    for index, char in enumerate(lines[line].rstrip("\r\n")):
        if units == character:
            return sum(map(len, lines[:line])) + index
        units += len(char.encode("utf-16-le")) // 2
    if units != character:
        raise ValueError("Edit splits a UTF-16 character or exceeds the line")
    return sum(map(len, lines[:line])) + len(lines[line].rstrip("\r\n"))


class LspUnavailableError(RuntimeError):
    pass


class LspClient:
    def __init__(
        self,
        root: Path,
        command: list[str],
        context,
        settings: dict | None = None,
        timeout: float = 15,
        environment: dict[str, str] | None = None,
        initialization_options=None,
        language_resolver=None,
    ) -> None:
        self.root = root
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Language server timeout must be a positive finite number")
        self.timeout = float(timeout)
        if settings is not None and not isinstance(settings, dict):
            raise ValueError("Language server settings must be an object")
        self.settings = settings or {}
        self.language_for = language_resolver or language_for
        self.capabilities: dict = {}
        self._lock = threading.RLock()
        self._document_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._next_id = 0
        self._documents: dict[str, tuple[str, int]] = {}
        self.diagnostics: dict[str, tuple[int | None, list]] = {}
        self._diagnostics_changed = threading.Condition()
        self._closed = False
        if context.permissions is not None:
            outcome = context.permissions.check_command(
                command[0],
                command[1:],
                str(root),
                force_prompt_reason="Start a language server to analyze this workspace",
            )
            if outcome.is_denied or outcome.is_unavailable:
                raise PermissionError(outcome.denial_message(scope=command[0]))
        executable = shutil.which(command[0]) or command[0]
        argv = [executable, *command[1:]]
        if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
            argv = ["cmd", "/d", "/s", "/c", subprocess.list2cmdline(argv)]
        self.process = subprocess.Popen(
            argv,
            cwd=root,
            env={**os.environ, **environment} if environment else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **hide_window_kwargs({"start_new_session": True} if os.name != "nt" else {}),
        )
        self._job = None
        if os.name == "nt":
            from pepsicode.subprocess_utils import WindowsProcessJob

            try:
                self._job = WindowsProcessJob(self.process)
            except OSError:
                pass
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            result = self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": root.as_uri(),
                    "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
                    "initializationOptions": initialization_options,
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-16"]},
                        "workspace": {"configuration": True, "workspaceEdit": {"documentChanges": True}},
                        "textDocument": {"publishDiagnostics": {"versionSupport": True}, "diagnostic": {}},
                    },
                },
                context.cancellation_event,
            )
            if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
                raise LspUnavailableError("Language server returned an invalid initialize response")
            self.capabilities = result["capabilities"]
            if self.capabilities.get("positionEncoding", "utf-16") != "utf-16":
                raise LspUnavailableError("Language server did not negotiate UTF-16 positions")
            self.notify("initialized", {})
            self.notify("workspace/didChangeConfiguration", {"settings": self.settings})
        except BaseException:
            self.close()
            raise

    def _send(self, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        with self._write_lock:
            self.process.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
            self.process.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read(self) -> None:
        try:
            while not self._closed:
                headers = {}
                while True:
                    line = self.process.stdout.readline(8192)
                    if not line:
                        raise EOFError("Language server exited")
                    if line in {b"\r\n", b"\n"}:
                        break
                    key, separator, value = line.decode("ascii").partition(":")
                    if not separator:
                        raise ValueError("Invalid LSP header")
                    headers[key.lower()] = value.strip()
                length = int(headers.get("content-length", 0))
                if not 0 < length <= 8 * 1024 * 1024:
                    raise ValueError("Invalid LSP response size")
                payload = self.process.stdout.read(length)
                if len(payload) != length:
                    raise EOFError("Incomplete LSP response")
                message = json.loads(payload)
                method = message.get("method")
                if method == "textDocument/publishDiagnostics":
                    params = message.get("params", {})
                    with self._diagnostics_changed:
                        self.diagnostics[document_key(params["uri"])] = (
                            params.get("version"),
                            params.get("diagnostics", []),
                        )
                        self._diagnostics_changed.notify_all()
                elif method and "id" in message:
                    if method == "workspace/configuration":
                        values = []
                        for item in message.get("params", {}).get("items", []):
                            value = self.settings
                            for part in item.get("section", "").split("."):
                                value = value.get(part, {}) if isinstance(value, dict) else {}
                            values.append(value)
                        self._send({"jsonrpc": "2.0", "id": message["id"], "result": values})
                    elif method == "workspace/applyEdit":
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                                "result": {
                                    "applied": False,
                                    "failureReason": "Edits require an explicit rename_symbol tool call",
                                },
                            }
                        )
                    elif method in {"client/registerCapability", "window/workDoneProgress/create"}:
                        self._send({"jsonrpc": "2.0", "id": message["id"], "result": None})
                    else:
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                                "error": {"code": -32601, "message": "Unsupported client request"},
                            }
                        )
                elif "id" in message:
                    with self._lock:
                        future = self._pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        if message.get("error"):
                            future.set_exception(
                                LspUnavailableError(str(message["error"].get("message", "LSP request failed")))
                            )
                        else:
                            future.set_result(message.get("result"))
        except (OSError, ValueError, EOFError, KeyError, TypeError) as error:
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for future in pending:
                if not future.done():
                    future.set_exception(LspUnavailableError(str(error)))

    def request(self, method: str, params: dict, cancellation=None):
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            future = Future()
            self._pending[request_id] = future
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while True:
                if cancellation is not None and cancellation.is_set():
                    self.notify("$/cancelRequest", {"id": request_id})
                    raise TurnCancelledError("Code intelligence request cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.notify("$/cancelRequest", {"id": request_id})
                    raise LspUnavailableError(f"Language server timed out during {method}")
                try:
                    return future.result(timeout=min(0.05, remaining))
                except FutureTimeout:
                    continue
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def open_document(self, path: Path) -> tuple[str, str]:
        text = path.read_bytes().decode("utf-8")
        uri = path.as_uri()
        key = document_key(uri)
        previous = self._documents.get(key)
        if previous is None or previous[0] != text:
            version = previous[1] + 1 if previous else 1
            with self._diagnostics_changed:
                self.diagnostics.pop(key, None)
            if previous is None:
                self.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": self.language_for(path),
                            "version": version,
                            "text": text,
                        }
                    },
                )
            else:
                self.notify(
                    "textDocument/didChange",
                    {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]},
                )
            self._documents[key] = (text, version)
        return uri, text

    def query(self, action: str, path: Path, line: int, column: int, context, new_name: str = ""):
        # didOpen/didChange and a subsequent request must observe the same document.
        with self._document_lock:
            uri, text = self.open_document(path)
            key = document_key(uri)
            params = {"textDocument": {"uri": uri}}
            if action == "diagnostics":
                if self.capabilities.get("diagnosticProvider"):
                    return self.request("textDocument/diagnostic", params, context.cancellation_event)
                with self._diagnostics_changed:
                    deadline = time.monotonic() + min(self.timeout, 3)
                    while key not in self.diagnostics:
                        if context.cancellation_event is not None and context.cancellation_event.is_set():
                            raise TurnCancelledError("Code intelligence request cancelled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return {
                                "status": "not_available",
                                "reason": "Language server has not published diagnostics for this document",
                            }
                        self._diagnostics_changed.wait(min(remaining, 0.05))
                    version, items = self.diagnostics[key]
                    if version is not None and version != self._documents[key][1]:
                        return {"status": "not_available", "reason": "Diagnostics are for an older document version"}
                    return {"kind": "full", "items": items}
            method = {
                "symbols": "documentSymbol",
                "definition": "definition",
                "references": "references",
                "rename": "rename",
            }[action]
            if action != "symbols":
                params["position"] = position(text, line, column)
            if action == "references":
                params["context"] = {"includeDeclaration": True}
            if action == "rename":
                params["newName"] = new_name
            return self.request(f"textDocument/{method}", params, context.cancellation_event)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._job is not None:
            try:
                self._job.terminate()
            finally:
                self._job.close()
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
        self._reader.join(timeout=1)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


class LanguageServers:
    def __init__(self, root: str, settings: dict | None = None) -> None:
        self.root = Path(root).resolve()
        self.settings = {} if settings is None else settings
        if not isinstance(self.settings, dict):
            raise ValueError("languageServers must be an object")
        self.extensions = dict(LANGUAGES)
        for language, config in self.settings.items():
            if not isinstance(language, str) or not language or (config is not False and not isinstance(config, dict)):
                raise ValueError("languageServers must map language names to configuration objects or false")
            if config is False:
                continue
            extensions = config.get("extensions", [])
            if not isinstance(extensions, list) or not all(
                isinstance(ext, str) and ext.startswith(".") and len(ext) > 1 and not any(char in ext for char in "/\\")
                for ext in extensions
            ):
                raise ValueError("Language server extensions must be an array such as ['.cs', '.csx']")
            language_id = config.get("languageId", language)
            if not isinstance(language_id, str) or not language_id:
                raise ValueError("Language server languageId must be a non-empty string")
            self.extensions.update({ext.lower(): language for ext in extensions})
        self.clients: dict[str, LspClient] = {}
        self._lock = threading.Lock()

    def language_for(self, path: Path) -> str:
        language = self.extensions.get(path.suffix.lower(), "")
        config = self.settings.get(language)
        return config.get("languageId", language) if isinstance(config, dict) else language

    def client(self, path: Path, context) -> LspClient:
        language = self.extensions.get(path.suffix.lower(), "")
        family = "typescript" if language in {"javascript", "javascriptreact", "typescriptreact"} else language
        if not family:
            raise LspUnavailableError(f"No language server configured for {path.suffix}")
        server_key = language if language in self.settings else family
        config = self.settings.get(server_key)
        if config is False:
            raise LspUnavailableError(f"Language server disabled for {language}")
        with self._lock:
            existing = self.clients.get(server_key)
            if existing is not None and existing.process.poll() is None:
                return existing
            if existing is not None:
                existing.close()
            if isinstance(config, dict) and "command" in config:
                command = config.get("command")
                args = config.get("args", [])
                if (
                    not isinstance(command, str)
                    or not command
                    or not isinstance(args, list)
                    or not all(isinstance(arg, str) for arg in args)
                ):
                    raise ValueError("Language server requires a command string and an args array")
                argv = [command, *args]
            else:
                choices = DEFAULT_SERVERS.get(family, [])
                argv = next(([command, *args] for command, args in choices if shutil.which(command)), None)
                if argv is None:
                    raise LspUnavailableError(
                        f"No installed language server for {language}; configure languageServers or install one"
                    )
                config = config or {}
                if "args" in config:
                    args = config["args"]
                    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                        raise ValueError("Language server args must be an array of strings")
                    argv = [argv[0], *args]
            environment = config.get("env", {})
            if not isinstance(environment, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
            ):
                raise ValueError("Language server env must map strings to strings")
            client = LspClient(
                self.root,
                argv,
                context,
                config.get("settings"),
                config.get("timeout", 15),
                environment,
                config.get("initializationOptions"),
                self.language_for,
            )
            self.clients[server_key] = client
            return client

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()
