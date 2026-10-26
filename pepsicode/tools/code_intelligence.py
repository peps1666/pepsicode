"""Read-only navigation and explicitly approved multi-file semantic rename."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from pepsicode.core.file_review import build_unified_diff
from pepsicode.execution.filesystem import FILES
from pepsicode.execution.filesystem import version as file_version
from pepsicode.intelligence import fallback
from pepsicode.intelligence.lsp import LanguageServers, LspUnavailableError, document_key, offset
from pepsicode.tooling import ToolCapability, ToolDefinition, ToolResult
from pepsicode.workspace import resolve_tool_path


def _replace_file(path: Path, content: bytes, *, expected=None) -> None:
    FILES.write(FILES.context(path), path, content, expected=expected)


def _validate(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("file_path"), str) or not value["file_path"]:
        raise ValueError("file_path is required")
    action = value.get("action", "symbols")
    if action not in {"symbols", "definition", "references", "diagnostics", "rename"}:
        raise ValueError("Choose symbols, definition, references, diagnostics or rename")
    for key in ("line", "column"):
        number = value.get(key, 1)
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError(f"{key} must be a positive 1-based integer")
    if action == "rename":
        name = value.get("new_name")
        # Identifier syntax belongs to the selected language server, not Python.
        if not isinstance(name, str) or not name.strip() or any(char in name for char in "\0\r\n"):
            raise ValueError("new_name must be a non-empty, single-line name; the language server validates syntax")
    return {**value, "action": action, "line": value.get("line", 1), "column": value.get("column", 1)}


def _apply_rename(edit: dict, manager: LanguageServers, context, client=None) -> dict:
    if not isinstance(edit, dict):
        raise ValueError("Language server did not return a workspace edit")
    changes = dict(edit.get("changes", {}))
    clients = [client] if client is not None else list(manager.clients.values())
    for document in edit.get("documentChanges", []):
        if not isinstance(document, dict) or "textDocument" not in document:
            raise ValueError("File creation, deletion and moves are not supported by rename_symbol")
        doc = document["textDocument"]
        uri = doc["uri"]
        if uri in changes:
            raise ValueError("Duplicate edits for the same document")
        version = doc.get("version")
        client_versions = [server._documents.get(document_key(uri)) for server in clients]
        if version is not None and not any(item and item[1] == version for item in client_versions):
            raise ValueError("Rename edit targets an unknown or stale document version")
        changes[uri] = document["edits"]
    planned = []
    for uri, edits in changes.items():
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ValueError("Rename edits must target local file URIs")
        path = resolve_tool_path(context, url2pathname(parsed.path), "write")
        # Require explicit approval before changing any file, including all
        # dependent files, and check every edit before committing any writes.
        from pepsicode.execution.filesystem import FILES

        previous = FILES.snapshot(context, path, for_edit=True).content
        if previous is None:
            raise FileNotFoundError(str(path))
        text = previous.decode("utf-8")
        for server in clients:
            document = server._documents.get(document_key(uri))
            if document is not None and document[0] != text:
                raise ValueError(f"File changed since language-server analysis: {path}")
        if any(existing == path for existing, _, _ in planned):
            raise ValueError("Multiple URIs target the same file")
        spans = sorted(
            (
                (offset(text, item["range"]["start"]), offset(text, item["range"]["end"]), item["newText"])
                for item in edits
            ),
            key=lambda span: (span[0], span[1]),
        )
        last_end = -1
        for start, end, replacement in spans:
            if start < last_end or end < start or not isinstance(replacement, str):
                raise ValueError("Overlapping or invalid rename edits")
            last_end = end
        updated = text
        for start, end, replacement in reversed(spans):
            updated = updated[:start] + replacement + updated[end:]
        if context.permissions is not None:
            outcome = context.permissions.check_edit(str(path), build_unified_diff(str(path), text, updated))
            if outcome.is_denied or outcome.is_unavailable:
                raise PermissionError(outcome.denial_message(scope=str(path)))
        planned.append((path, previous, updated.encode("utf-8")))
    for path, previous, _ in planned:
        if FILES.read_bytes(path) != previous:
            raise ValueError(f"File changed during rename approval: {path}")
    if context.cancellation_event is not None and context.cancellation_event.is_set():
        raise RuntimeError("Rename cancelled before writing")
    written = []
    try:
        for path, previous, updated in planned:
            if FILES.read_bytes(path) != previous:
                raise ValueError(f"File changed while applying rename: {path}")
            _replace_file(path, updated, expected=file_version(previous))
            written.append((path, previous, updated))
    except (OSError, ValueError):
        for path, previous, updated in reversed(written):
            if FILES.read_bytes(path) == updated:
                _replace_file(path, previous, expected=file_version(updated))
        raise
    return {"changed_files": [str(path) for path, _, _ in planned]}


def create_code_intelligence_tools(manager: LanguageServers) -> list[ToolDefinition]:
    def navigate(value, context):
        path = resolve_tool_path(context, value["file_path"], "read")
        action = value["action"]
        try:
            client = manager.client(path, context)
            result = client.query(action, path, value["line"], value["column"], context, value.get("new_name", ""))
            return ToolResult(
                True,
                json.dumps(
                    {"backend": "lsp", "action": action, "result_positions": "zero_based_utf16", "result": result},
                    ensure_ascii=False,
                ),
            )
        except (LspUnavailableError, OSError) as error:
            if action == "rename":
                return ToolResult(False, f"Semantic rename requires a working language server: {error}")
            if action == "diagnostics":
                result = {"status": "not_available", "reason": str(error)}
            elif action == "symbols":
                result = fallback.symbols(path)
            else:
                text = FILES.read_text(path)
                lines = text.splitlines()
                if not 1 <= value["line"] <= len(lines):
                    raise ValueError("line is outside the document")
                line = lines[value["line"] - 1]
                match = next(
                    (
                        item
                        for item in re.finditer(r"[A-Za-z_$][\w$]*", line)
                        if item.start() <= value["column"] - 1 < item.end()
                    ),
                    None,
                )
                if match is None:
                    raise ValueError("Place the cursor on a symbol")
                name = match.group()
                result = []
                for file in fallback.source_files(Path(context.cwd)):
                    file = resolve_tool_path(context, str(file), "read")
                    try:
                        found = fallback.symbols(file) if action == "definition" else fallback.references(file, name)
                        result.extend(
                            {"file": str(file), **item}
                            for item in found
                            if action != "definition" or item["name"] == name
                        )
                    except (OSError, UnicodeError):
                        continue
                    if len(result) >= 200:
                        break
                result = result[:200]
            return ToolResult(
                True,
                json.dumps(
                    {
                        "backend": "fallback",
                        "warning": str(error) + "; lexical candidates are not resolved semantic references",
                        "action": action,
                        "result": result,
                    },
                    ensure_ascii=False,
                ),
            )

    def rename(value, context):
        path = resolve_tool_path(context, value["file_path"], "read")
        client = manager.client(path, context)
        edit = client.query("rename", path, value["line"], value["column"], context, value["new_name"])
        return ToolResult(True, json.dumps(_apply_rename(edit, manager, context, client), ensure_ascii=False))

    properties = {
        "file_path": {"type": "string"},
        "line": {"type": "integer", "minimum": 1},
        "column": {"type": "integer", "minimum": 1},
        "action": {"type": "string", "enum": ["symbols", "definition", "references", "diagnostics", "rename"]},
        "new_name": {"type": "string"},
    }

    def validate_rename(value):
        parsed = _validate({**value, "action": "rename"})
        return parsed

    return [
        ToolDefinition(
            "code_intelligence",
            "Use a language server for symbols, go-to-definition, references and diagnostics. Positions are 1-based Unicode columns. rename returns a read-only edit preview; use rename_symbol to apply it. Falls back to explicitly labelled syntax/lexical candidates when a server is not installed.",
            {"type": "object", "properties": properties, "required": ["file_path"]},
            _validate,
            navigate,
            capabilities={ToolCapability.READ_ONLY},
        ),
        ToolDefinition(
            "rename_symbol",
            "Semantically rename a symbol and its references across files using LSP, after path/edit approvals. Requires a working language server; never performs textual substitution as fallback.",
            {
                "type": "object",
                "properties": {key: value for key, value in properties.items() if key != "action"},
                "required": ["file_path", "line", "column", "new_name"],
            },
            validate_rename,
            rename,
        ),
    ]
