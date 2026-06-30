from __future__ import annotations

import time
from pathlib import Path

from pepsicode.tooling import ToolDefinition, ToolResult
from pepsicode.workspace import resolve_tool_path

DEFAULT_READ_LIMIT = 8000
MAX_READ_LIMIT = 20000

# File content cache to avoid re-reading the same file repeatedly.
# Cache key: (file path, modification time) -> (content, cache timestamp)
_file_cache: dict[tuple[str, float], tuple[str, float]] = {}
_FILE_CACHE_TTL = 2.0  # cache entries are valid for 2 seconds
# read_file is concurrency-safe and may run in parallel worker threads, so the
# shared cache must be guarded against concurrent mutation.
import threading

_file_cache_lock = threading.Lock()


def _get_cached_file_content(target: Path) -> str:
    """Get file content, using the cache to avoid repeated reads (thread-safe)."""
    try:
        stat = target.stat()
        mtime = stat.st_mtime
        cache_key = (str(target), mtime)
        now = time.monotonic()

        with _file_cache_lock:
            cached = _file_cache.get(cache_key)
            if cached is not None:
                content, cache_time = cached
                if now - cache_time <= _FILE_CACHE_TTL:
                    return content
            # Purge expired cache entries (deleted within the lock to avoid mutation during iteration)
            expired_keys = [k for k, (c, t) in list(_file_cache.items()) if now - t > _FILE_CACHE_TTL]
            for k in expired_keys:
                _file_cache.pop(k, None)

        # Read outside the lock (avoid I/O blocking other threads) then cache
        content = target.read_text(encoding="utf-8")
        with _file_cache_lock:
            _file_cache[cache_key] = (content, time.monotonic())
        return content
    except OSError:
        # If file doesn't exist or can't be accessed, read directly
        return target.read_text(encoding="utf-8")


def _validate(input_data: dict) -> dict:
    path = input_data.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    offset = int(input_data.get("offset", 0))
    limit = int(input_data.get("limit", DEFAULT_READ_LIMIT))
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit < 1 or limit > MAX_READ_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_READ_LIMIT}")
    return {"path": path, "offset": offset, "limit": limit}


def _run(input_data: dict, context) -> ToolResult:
    target = resolve_tool_path(context, input_data["path"], "read")

    try:
        # Read using the cache
        content = _get_cached_file_content(target)
    except UnicodeDecodeError:
        return ToolResult(
            ok=False,
            output=f"File {input_data['path']} appears to be binary. Cannot read as text.",
        )

    offset = input_data["offset"]
    limit = input_data["limit"]
    end = min(len(content), offset + limit)
    chunk = content[offset:end]
    truncated = end < len(content)
    header = "\n".join(
        [
            f"FILE: {input_data['path']}",
            f"OFFSET: {offset}",
            f"END: {end}",
            f"TOTAL_CHARS: {len(content)}",
            f"TRUNCATED: {'yes - call read_file again with offset ' + str(end) if truncated else 'no'}",
            "",
        ]
    )
    return ToolResult(ok=True, output=header + chunk)


read_file_tool = ToolDefinition(
    name="read_file",
    description="Read a UTF-8 text file relative to the workspace root.",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "number"}, "limit": {"type": "number"}}, "required": ["path"]},
    validator=_validate,
    run=_run,
    concurrency_safe=True,
)

