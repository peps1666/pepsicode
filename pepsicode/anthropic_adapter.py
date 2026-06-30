from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from typing import Any

from pepsicode.types import AgentStep, ChatMessage, ProviderThinkingBlock, StepDiagnostics, StreamToken

DEFAULT_MAX_RETRIES = 4
BASE_RETRY_DELAY_MS = 500
MAX_RETRY_DELAY_MS = 8000

# Status codes that indicate the request body / context exceeded the model
# limit.  These are NOT retried verbatim - the caller compacts and retries.
OVERFLOW_STATUSES = {400, 413}
# Service-overloaded: retry on the configured fallback model if one exists.
OVERLOAD_STATUS = 529


class ContextOverflowError(Exception):
    """Raised when the API rejects a request because it is too large (400/413).

    The agent loop catches this, compacts the conversation, and retries.
    """


class ServiceOverloadError(Exception):
    """Raised on a 529 overload when no fallback model handled the request."""


def _sleep(milliseconds: int) -> None:
    time.sleep(max(0, milliseconds) / 1000)


def _get_retry_limit() -> int:
    try:
        value = int(float(__import__("os").environ.get("PEPSI_CODE_MAX_RETRIES", DEFAULT_MAX_RETRIES)))
    except ValueError:
        value = DEFAULT_MAX_RETRIES
    return max(0, value)


def _should_retry_status(status: int) -> bool:
    # 529 (overload) is NOT retried in-place - next() owns that decision and
    # switches to the configured fallback model instead of hammering the
    # already-overloaded primary.
    if status == OVERLOAD_STATUS:
        return False
    return status == 429 or 500 <= status < 600


def _parse_retry_after_ms(retry_after: str | None) -> int | None:
    if not retry_after:
        return None
    # Try numeric seconds first
    try:
        seconds = float(retry_after)
        if seconds >= 0:
            return int(seconds * 1000)
    except ValueError:
        pass
    # Try HTTP-date format: "Thu, 01 Dec 2025 16:00:00 GMT"
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(retry_after)
        delta_ms = int((target.timestamp() - time.time()) * 1000)
        return max(0, delta_ms)
    except (ValueError, TypeError):
        pass
    return None


def _is_thinking_block(block: dict) -> bool:
    return block.get("type") in ("thinking", "redacted_thinking")


def _get_retry_delay_ms(attempt: int, retry_after_ms: int | None) -> int:
    if retry_after_ms is not None:
        return retry_after_ms
    base = min(BASE_RETRY_DELAY_MS * (2 ** max(0, attempt - 1)), MAX_RETRY_DELAY_MS)
    jitter = random.random() * 0.25 * base
    return int(base + jitter)


def _read_json_body(response) -> Any:
    text = response.read().decode("utf-8")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": {"message": text.strip()}}


def _extract_error_message(data: Any, status: int) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return f"Model request failed: {status}"


# ---------------------------------------------------------------------------
# SSE (Server-Sent Events) parsing for streaming responses
# ---------------------------------------------------------------------------


def _parse_sse_event(event_str: str) -> dict[str, Any] | None:
    """Parse a single SSE event block.

    Each event is separated by a blank line (``\\n\\n``).  Inside a block the
    relevant lines are::

        event: <type>
        data: <json>

    Returns ``{"event": <type>, "data": <dict>}`` or ``None`` when the block
    contains nothing useful.
    """
    event_type: str | None = None
    data: Any = None

    for line in event_str.splitlines():
        if not line or line.startswith(":"):
            # Heartbeat / comment lines - ignore.
            continue
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_str = line[len("data:") :].strip()
            if not data_str:
                continue
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                # Non-JSON payloads (e.g. literal "[DONE]") are not actionable
                # here - the upstream caller can yield a "done" token from
                # the message_stop event instead.
                data = data_str

    if event_type and data is not None:
        return {"event": event_type, "data": data}
    return None


def _process_sse_event(event: dict[str, Any]) -> Generator[StreamToken, None, None]:
    """Translate a parsed SSE event into one or more ``StreamToken`` objects.

    Only the events we care about produce tokens; the rest are ignored so
    callers can safely iterate the whole stream without bookkeeping.
    """
    event_type = event.get("event")
    data = event.get("data")
    if not isinstance(data, dict):
        return

    if event_type == "content_block_start":
        block = data.get("content_block")
        if not isinstance(block, dict):
            return
        block_kind = block.get("type")
        if block_kind == "tool_use":
            yield StreamToken(
                type="tool_use",
                tool_name=block.get("name") if isinstance(block.get("name"), str) else None,
                tool_id=block.get("id") if isinstance(block.get("id"), str) else None,
            )
        elif block_kind == "thinking":
            yield StreamToken(type="thinking")

    elif event_type == "content_block_delta":
        delta = data.get("delta")
        if not isinstance(delta, dict):
            return
        delta_type = delta.get("type")
        if delta_type == "text_delta" and isinstance(delta.get("text"), str):
            yield StreamToken(type="text", content=delta["text"])
        elif delta_type == "input_json_delta" and isinstance(delta.get("partial_json"), str):
            yield StreamToken(
                type="tool_use",
                tool_input_partial=delta["partial_json"],
            )

    elif event_type == "message_stop":
        yield StreamToken(type="done")

    elif event_type == "message_delta":
        # Final usage / stop_reason - capture token usage when the provider
        # reports it.  Mirror the non-streaming path so context-manager stats
        # stay accurate.
        usage = data.get("usage")
        if isinstance(usage, dict):
            # Stash the usage on a sentinel attribute so next_stream() can
            # assign it back to self.last_usage at the end of the stream.
            _process_sse_event._pending_usage = {
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            }


def _consume_pending_usage() -> dict[str, int] | None:
    """Read and clear the pending usage stashed by ``_process_sse_event``."""
    usage = getattr(_process_sse_event, "_pending_usage", None)
    if usage is not None:
        try:
            delattr(_process_sse_event, "_pending_usage")
        except AttributeError:
            pass
    return usage


def _parse_assistant_text(content: str) -> tuple[str, str | None]:
    trimmed = content.strip()
    if not trimmed:
        return "", None
    markers = [
        ("<final>", "final", "</final>"),
        ("[FINAL]", "final", None),
        ("<progress>", "progress", "</progress>"),
        ("[PROGRESS]", "progress", None),
    ]
    for prefix, kind, closing_tag in markers:
        if trimmed.startswith(prefix):
            raw = trimmed[len(prefix) :].strip()
            if closing_tag:
                raw = raw.replace(closing_tag, "").strip()
            return raw, kind
    return trimmed, None


def _to_text_block(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _to_assistant_text(message: dict[str, Any]) -> str:
    if message["role"] == "assistant_progress":
        return f"<progress>\n{message['content']}\n</progress>"
    return message["content"]


def _push_anthropic_message(messages: list[dict[str, Any]], role: str, block: dict[str, Any]) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].append(block)
    else:
        messages.append({"role": role, "content": [block]})


def _to_anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "system":
            continue
        if role == "user":
            _push_anthropic_message(converted, "user", _to_text_block(message["content"]))
            continue
        if role in {"assistant", "assistant_progress"}:
            _push_anthropic_message(converted, "assistant", _to_text_block(_to_assistant_text(message)))
            continue
        if role == "assistant_thinking":
            for block in message.get("blocks", []):
                _push_anthropic_message(converted, "assistant", block)
            continue
        if role == "assistant_tool_call":
            _push_anthropic_message(
                converted,
                "assistant",
                {
                    "type": "tool_use",
                    "id": message["toolUseId"],
                    "name": message["toolName"],
                    "input": message["input"],
                },
            )
            continue
        _push_anthropic_message(
            converted,
            "user",
            {
                "type": "tool_result",
                "tool_use_id": message["toolUseId"],
                "content": message["content"],
                "is_error": message["isError"],
            },
        )
    return system, converted


class AnthropicModelAdapter:
    def __init__(self, runtime: dict[str, Any], tools) -> None:
        self.runtime = runtime
        self.tools = tools
        # Last token usage reported by the API ({"input_tokens", "output_tokens"}).
        self.last_usage: dict[str, int] | None = None

    def _build_request(
        self, model: str, system_message: str, converted_messages: list[dict[str, Any]]
    ) -> urllib.request.Request:
        request_body = {
            "model": model,
            "system": system_message,
            "messages": converted_messages,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in self.tools.list()
            ],
        }
        if self.runtime.get("maxOutputTokens") is not None:
            request_body["max_tokens"] = self.runtime["maxOutputTokens"]

        return urllib.request.Request(
            url=self.runtime["baseUrl"].rstrip("/") + "/v1/messages",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                **(
                    {"x-api-key": self.runtime["apiKey"]}
                    if self.runtime.get("apiKey")
                    else {"Authorization": f"Bearer {self.runtime['authToken']}"}
                ),
            },
            method="POST",
        )

    def _build_stream_request(
        self, model: str, system_message: str, converted_messages: list[dict[str, Any]]
    ) -> urllib.request.Request:
        """Build the streaming variant of the messages request.

        The only difference from ``_build_request`` is the ``"stream": True``
        flag in the body - everything else (auth, tools, max_tokens) is
        identical so a stream and a non-stream request remain comparable.
        """
        request_body = {
            "model": model,
            "system": system_message,
            "messages": converted_messages,
            "stream": True,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in self.tools.list()
            ],
        }
        if self.runtime.get("maxOutputTokens") is not None:
            request_body["max_tokens"] = self.runtime["maxOutputTokens"]

        return urllib.request.Request(
            url=self.runtime["baseUrl"].rstrip("/") + "/v1/messages",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "accept": "text/event-stream",
                **(
                    {"x-api-key": self.runtime["apiKey"]}
                    if self.runtime.get("apiKey")
                    else {"Authorization": f"Bearer {self.runtime['authToken']}"}
                ),
            },
            method="POST",
        )

    def _send(self, request: urllib.request.Request) -> tuple[Any, int]:
        """Send a request with backoff retries.  Returns (json_data, status)."""
        max_retries = _get_retry_limit()
        response = None
        for attempt in range(max_retries + 1):
            try:
                response = urllib.request.urlopen(request, timeout=60)  # noqa: S310
                break
            except urllib.error.HTTPError as error:
                response = error
                if not _should_retry_status(error.code) or attempt >= max_retries:
                    break
                _sleep(_get_retry_delay_ms(attempt + 1, _parse_retry_after_ms(error.headers.get("retry-after"))))
            except urllib.error.URLError:
                if attempt >= max_retries:
                    raise
                _sleep(_get_retry_delay_ms(attempt + 1, None))

        if response is None:
            raise RuntimeError("Model request failed before receiving a response")

        data = _read_json_body(response)
        status = getattr(response, "status", getattr(response, "code", 200))
        return data, status

    def summarize(self, transcript: str) -> str:
        """Summarize an old transcript into key durable facts (compression L2).

        Returns "" on any failure so the caller falls back to a heuristic.
        """
        system = (
            "Compress this development transcript into concise key information. "
            "Retain: file paths touched, decisions made, errors encountered, and "
            "the current task status / any explicit user constraints. Discard: "
            "lengthy tool output, repetitive discussion, and formatted code. "
            "Respond with a short bulleted summary only."
        )
        body = {
            "model": self.runtime.get("fallbackModel") or self.runtime["model"],
            "system": system,
            "messages": [{"role": "user", "content": [{"type": "text", "text": transcript[:15000]}]}],
            "max_tokens": 1024,
        }
        request = urllib.request.Request(
            url=self.runtime["baseUrl"].rstrip("/") + "/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                **(
                    {"x-api-key": self.runtime["apiKey"]}
                    if self.runtime.get("apiKey")
                    else {"Authorization": f"Bearer {self.runtime['authToken']}"}
                ),
            },
            method="POST",
        )
        try:
            data, status = self._send(request)
        except Exception:  # noqa: BLE001
            return ""
        if status >= 400 or not isinstance(data, dict):
            return ""
        texts = [b.get("text", "") for b in data.get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(t for t in texts if t).strip()

    def next_stream(self, messages: list[ChatMessage]) -> Generator[StreamToken, None, None]:
        """Stream tokens from the model using Anthropic's SSE endpoint.

        Yields ``StreamToken`` objects as they arrive.  A ``done`` token marks
        the end of the stream.  The adapter's ``last_usage`` attribute is
        updated when the final usage delta arrives, mirroring ``next()`` so
        callers don't have to special-case the streaming path.
        """
        system_message, converted_messages = _to_anthropic_messages(messages)
        request = self._build_stream_request(self.runtime["model"], system_message, converted_messages)

        # Reset any pending usage stashed by a previous run.
        try:
            delattr(_process_sse_event, "_pending_usage")
        except AttributeError:
            pass

        try:
            response = urllib.request.urlopen(request, timeout=120)  # noqa: S310
        except urllib.error.HTTPError as error:
            # Try to surface the provider's error message just like ``_send`` does.
            try:
                data = _read_json_body(error)
            except Exception:  # noqa: BLE001
                data = {}
            status = getattr(error, "code", 0)
            if status in OVERFLOW_STATUSES:
                raise ContextOverflowError(_extract_error_message(data, status))
            if status == OVERLOAD_STATUS:
                raise ServiceOverloadError(_extract_error_message(data, status))
            raise RuntimeError(_extract_error_message(data, status))
        except urllib.error.URLError as error:
            raise RuntimeError(f"Model stream request failed: {error}") from error

        # Read the SSE stream chunk by chunk, buffering partial event blocks.
        buffer = ""
        try:
            # ``iter_chunks`` returns (chunk, consumed_from_buffer); we use
            # ``read`` here for simplicity and to keep memory bounded.
            for raw_chunk in response:
                if not raw_chunk:
                    continue
                buffer += raw_chunk.decode("utf-8", errors="replace")
                # Drain complete ``\\n\\n``-delimited events.
                while "\n\n" in buffer:
                    event_str, buffer = buffer.split("\n\n", 1)
                    event = _parse_sse_event(event_str)
                    if event is None:
                        continue
                    for token in _process_sse_event(event):
                        if token.type == "done":
                            usage = _consume_pending_usage()
                            if usage is not None:
                                self.last_usage = usage
                            yield token
                            return
                        yield token
        finally:
            # Always close the underlying response so the HTTP connection
            # is released back to the pool.
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

        # Drain any trailing bytes that didn't end with a blank line.
        if buffer.strip():
            event = _parse_sse_event(buffer)
            if event is not None:
                for token in _process_sse_event(event):
                    yield token

        # Surface any pending usage that arrived without a ``done`` token.
        usage = _consume_pending_usage()
        if usage is not None:
            self.last_usage = usage
        yield StreamToken(type="done")

    def next(self, messages: list[dict[str, Any]]) -> AgentStep:
        system_message, converted_messages = _to_anthropic_messages(messages)
        request = self._build_request(self.runtime["model"], system_message, converted_messages)
        data, status = self._send(request)

        # Service overloaded: try the configured fallback model once.
        if status == OVERLOAD_STATUS:
            fallback = self.runtime.get("fallbackModel")
            if fallback and fallback != self.runtime["model"]:
                fallback_request = self._build_request(fallback, system_message, converted_messages)
                data, status = self._send(fallback_request)
            if status == OVERLOAD_STATUS:
                raise ServiceOverloadError(_extract_error_message(data, status))

        # Context/request too large: signal the loop to compact and retry.
        if status in OVERFLOW_STATUSES:
            raise ContextOverflowError(_extract_error_message(data, status))

        if status >= 400:
            raise RuntimeError(_extract_error_message(data, status))

        # Capture real token usage when the provider reports it.
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            usage = data["usage"]
            self.last_usage = {
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
            }

        tool_calls: list[dict[str, Any]] = []
        text_parts: list[str] = []
        thinking_blocks: list[ProviderThinkingBlock] = []
        block_types: list[str] = []
        ignored_block_types: list[str] = []

        for block in data.get("content", []) if isinstance(data, dict) else []:
            block_type = block.get("type")
            block_types.append(block_type)
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block_type == "tool_use" and isinstance(block.get("id"), str) and isinstance(block.get("name"), str):
                tool_calls.append({"id": block["id"], "toolName": block["name"], "input": block.get("input")})
            elif _is_thinking_block(block):
                thinking_blocks.append(block)  # type: ignore[arg-type]
            else:
                ignored_block_types.append(str(block_type))

        parsed_text, kind = _parse_assistant_text("\n".join(text_parts).strip())
        diagnostics = StepDiagnostics(
            stopReason=data.get("stop_reason") if isinstance(data, dict) else None,
            blockTypes=block_types,
            ignoredBlockTypes=ignored_block_types,
        )

        if tool_calls:
            return AgentStep(
                type="tool_calls",
                calls=tool_calls,
                content=parsed_text,
                contentKind="progress" if kind == "progress" else None,
                thinkingBlocks=thinking_blocks,
                diagnostics=diagnostics,
            )
        return AgentStep(
            type="assistant",
            content=parsed_text,
            kind=kind,
            thinkingBlocks=thinking_blocks,
            diagnostics=diagnostics,
        )
