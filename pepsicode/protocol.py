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
    "session/create",
    "session/list",
    "session/resume",
    "session/delete",
    "turn/run",
    "turn/cancel",
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
    "config/validate",
    "status",
]

# ---------------------------------------------------------------------------
# Event names
# ---------------------------------------------------------------------------

EventName = Literal[
    "connection/ready",
    "message/start",
    "message/delta",
    "message/end",
    "progress/message",
    "tool/call",
    "tool/result",
    "permission/request",
    "hook/notification",
    "hook/context",
    "session/saved",
    "session/created",
    "cost/update",
    "context/compact",
    "error",
    "turn/end",
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


# Standard error codes (aligned with JSON-RPC 2.0)
ERR_PARSE_ERROR = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_PERMISSION = -32000
ERR_NOT_READY = -32001
