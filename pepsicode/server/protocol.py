"""Wire protocol types for the pepsicode desktop client server.

Defines the JSON-RPC message envelope and the request/response/event shapes
used between the TypeScript Electron client and the Python WebSocket server.

Design (inspired by dsh's sdk/protocol):
- Requests carry ``id`` + ``method`` + ``params``; responses carry ``id`` + ``result`` or ``error``.
- Events are server-to-client notifications with no ``id``; they stream agent activity.
- Every message is a JSON object with a ``kind`` discriminant: ``"request"``, ``"response"``, ``"event"``.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pepsicode.llm.assistant_text import parse_assistant_text

# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

MessageKind = Literal["request", "response", "event"]


class Request(TypedDict, total=False):
    kind: Literal["request"]
    id: int | str
    method: str
    params: dict[str, Any]


class Response(TypedDict, total=False):
    kind: Literal["response"]
    id: int | str
    result: Any
    error: dict[str, Any] | None


class Event(TypedDict, total=False):
    kind: Literal["event"]
    event: str
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# Request methods
# ---------------------------------------------------------------------------

RequestMethod = Literal[
    "connection/auth",
    "session/create",
    "session/list",
    "session/resume",
    "session/delete",
    "session/tree",
    "session/checkout",
    "session/fork",
    "session/clone",
    "turn/regenerate",
    "turn/run",
    "turn/cancel",
    "turn/enqueue",
    "turn/dequeue",
    "tool/approve",
    "hooks/list",
    "hooks/reload",
    "hooks/trust",
    "hooks/enable",
    "hooks/disable",
    "plan/enter",
    "plan/exit",
    "plan/approve",
    "cost/query",
    "context/query",
    "tools/list",
    "commands/list",
    "config/validate",
    "model/settings",
    "model/update",
    "provider/probe",
    "provider/save",
    "provider/cancel",
    "status",
]

# ---------------------------------------------------------------------------
# Event names
# ---------------------------------------------------------------------------

EventName = Literal[
    "model/settings",
    "connection/ready",
    "message/start",
    "message/delta",
    "message/end",
    "thinking/start",
    "thinking/delta",
    "thinking/end",
    "progress/message",
    "tool/call",
    "tool/result",
    "request/retry",
    "verification/update",
    "permission/request",
    "hook/notification",
    "hook/context",
    "session/saved",
    "session/created",
    "session/view",
    "cost/update",
    "usage/update",
    "context/compact",
    "error",
    "turn/end",
    "input/queue",
    "status",
]


def make_response(req_id: int | str, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC response envelope."""
    msg: dict[str, Any] = {"kind": "response", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def make_event(name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a server-to-client event envelope."""
    return {"kind": "event", "event": name, "data": data or {}}


def make_error(code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC error object."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err


def serialize_session_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert persisted agent messages into the desktop timeline contract."""
    history: list[dict[str, Any]] = []
    for message in messages:
        if message.get("internal"):
            continue
        role = message.get("role")
        if role == "system":
            continue

        if role == "assistant_thinking":
            for block in message.get("blocks", []):
                if not isinstance(block, dict) or block.get("type") != "thinking":
                    continue
                content = block.get("thinking")
                if isinstance(content, str) and content.strip():
                    history.append({"role": "thinking", "content": content})
            continue

        if role in {"user", "assistant"}:
            content = str(message.get("content", ""))
            if role == "user":
                content = str(message.get("displayContent", content))
            if role == "assistant" and not message.get("isError"):
                content, _ = parse_assistant_text(content)
            history.append(
                {
                    "role": role,
                    "content": content,
                    "is_error": bool(message.get("isError", False)),
                    "entry_id": message.get("entryId"),
                    **(
                        {"verification": message["verification"]}
                        if isinstance(message.get("verification"), dict)
                        else {}
                    ),
                }
            )
            continue

        if role == "assistant_progress":
            content, _ = parse_assistant_text(str(message.get("content", "")))
            history.append(
                {
                    "role": "progress",
                    "content": content,
                    **(
                        {"activity_kind": message["activityKind"], "retry": message.get("retry")}
                        if message.get("activityKind")
                        else {}
                    ),
                }
            )
            continue

        if role == "assistant_tool_call":
            history.append(
                {
                    "role": "tool_call",
                    "tool_name": str(message.get("toolName", "unknown")),
                    "tool_input": message.get("input"),
                    **({"call_id": message["toolUseId"]} if message.get("toolUseId") else {}),
                }
            )
            continue

        if role == "tool_result":
            history.append(
                {
                    "role": "tool_result",
                    "content": str(message.get("content", "")),
                    "tool_name": str(message.get("toolName", "unknown")),
                    "is_error": bool(message.get("isError", False)),
                    **{
                        wire: message[key]
                        for key, wire in (
                            ("toolUseId", "call_id"),
                            ("startedAt", "started_at"),
                            ("durationMs", "duration_ms"),
                            ("toolStatus", "status"),
                            ("exitCode", "exit_code"),
                        )
                        if message.get(key) is not None
                    },
                }
            )

    return history


# Standard error codes (aligned with JSON-RPC 2.0)
ERR_PARSE_ERROR = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_PERMISSION = -32000
ERR_NOT_READY = -32001
