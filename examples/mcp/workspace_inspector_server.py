"""Small, dependency-free MCP server for inspecting this repository.

The server uses MCP's newline-delimited JSON-RPC stdio transport and exposes
read-only tools.  It is intentionally self-contained so a fresh pepsicode
checkout can exercise a real MCP handshake without downloading a package.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
IGNORED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    ".pytest_codex_tmp",
    ".ruff_cache",
    "dist",
    "dist-electron",
    "node_modules",
    "release",
}


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(message_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": message_id, "result": result})


def _error(message_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_pyproject(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            parsed = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _walk_files(root: Path, suffix: str) -> list[Path]:
    results: list[Path] = []
    for path in root.rglob(f"*{suffix}"):
        if any(part in IGNORED_DIRECTORIES for part in path.parts):
            continue
        if path.is_file():
            results.append(path)
    return results


def _project_info(root: Path) -> dict[str, Any]:
    pyproject = _read_pyproject(root / "pyproject.toml")
    project = pyproject.get("project", {}) if isinstance(pyproject.get("project"), dict) else {}
    mcp_config = _read_json(root / ".mcp.json")
    mcp_servers = mcp_config.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}

    skill_root = root / ".pepsi-code" / "skills"
    skill_names = sorted(
        path.parent.name for path in skill_root.glob("*/SKILL.md") if path.is_file()
    ) if skill_root.is_dir() else []

    python_files = _walk_files(root / "pepsicode", ".py") if (root / "pepsicode").is_dir() else []
    test_files = _walk_files(root / "tests", ".py") if (root / "tests").is_dir() else []

    return {
        "workspace": str(root),
        "name": project.get("name") or root.name,
        "version": project.get("version"),
        "python_files": len(python_files),
        "test_files": len(test_files),
        "project_skills": skill_names,
        "mcp_servers": sorted(mcp_servers),
        "has_frontend": (root / "pepsicode-client" / "package.json").is_file(),
        "has_git_repository": (root / ".git").exists(),
    }


def _list_top_level(root: Path) -> dict[str, Any]:
    entries = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if path.name in IGNORED_DIRECTORIES:
            continue
        entries.append({"name": path.name, "type": "directory" if path.is_dir() else "file"})
    return {"workspace": str(root), "entries": entries}


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, ensure_ascii=False)}],
        "structuredContent": payload,
    }


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "project_info",
            "description": "Return read-only metadata and extension status for the current Pepsicode workspace.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_top_level",
            "description": "List non-generated files and directories at the workspace root.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def _handle(message: dict[str, Any], root: Path) -> None:
    method = message.get("method")
    message_id = message.get("id")

    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion")
        _result(
            message_id,
            {
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "pepsicode-workspace-inspector", "version": "1.0.0"},
                "instructions": "All tools are read-only and inspect only the configured workspace.",
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "ping":
        _result(message_id, {})
        return
    if method == "tools/list":
        _result(message_id, {"tools": _tools()})
        return
    if method == "resources/list":
        _result(message_id, {"resources": []})
        return
    if method == "prompts/list":
        _result(message_id, {"prompts": []})
        return
    if method == "tools/call":
        params = message.get("params", {})
        name = params.get("name") if isinstance(params, dict) else None
        if name == "project_info":
            _result(message_id, _tool_result(_project_info(root)))
            return
        if name == "list_top_level":
            _result(message_id, _tool_result(_list_top_level(root)))
            return
        _result(
            message_id,
            {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            },
        )
        return

    if message_id is not None:
        _error(message_id, -32601, f"Method not found: {method}")


def main() -> None:
    root = Path.cwd().resolve()
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object")
            _handle(message, root)
        except (json.JSONDecodeError, ValueError) as error:
            _error(None, -32700, str(error))
        except Exception as error:  # keep the stdio server alive after one failed request
            message_id = message.get("id") if isinstance(locals().get("message"), dict) else None
            _error(message_id, -32603, str(error))


if __name__ == "__main__":
    main()

