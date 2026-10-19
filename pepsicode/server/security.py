"""Authenticate before creating a session or accepting any workspace RPC."""

from __future__ import annotations

import asyncio
import hmac
import json

from websockets.exceptions import ConnectionClosed


def request_origin(websocket) -> str | None:
    request = getattr(websocket, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        headers = getattr(websocket, "request_headers", {})
    return headers.get("Origin")


async def authenticate(websocket, token: str, allowed_origins: set[str]) -> bool:
    try:
        origin = request_origin(websocket)
        if origin is not None and origin not in allowed_origins:
            raise ValueError("Origin not allowed")
        raw = await asyncio.wait_for(websocket.recv(), timeout=10)
        if not isinstance(raw, str) or len(raw) > 8192:
            raise ValueError("Invalid authentication frame")
        message = json.loads(raw)
        if (
            not isinstance(message, dict)
            or message.get("kind") != "request"
            or message.get("method") != "connection/auth"
        ):
            raise ValueError("Authentication required")
        params = message.get("params")
        provided = params.get("token") if isinstance(params, dict) else None
        if not isinstance(provided, str) or not hmac.compare_digest(provided.encode(), token.encode()):
            raise ValueError("Invalid authentication token")
        await websocket.send(
            json.dumps({"kind": "response", "id": message.get("id", 0), "result": {"authenticated": True}})
        )
        return True
    except (ValueError, TypeError, TimeoutError, OSError, ConnectionClosed):
        await websocket.close(code=1008, reason="Connection authentication failed")
        return False
