"""Shared usage telemetry. Input buckets are mutually exclusive."""

from __future__ import annotations

from typing import Any


def prompt_tokens(usage: dict[str, Any]) -> int:
    return sum(int(usage.get(key, 0) or 0) for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))


def cache_hit_percentage(usage: dict[str, Any] | None) -> float | None:
    if not usage or "cache_read_tokens" not in usage:
        return None
    total = prompt_tokens(usage)
    return 100 * int(usage["cache_read_tokens"]) / total if total else None


def usage_snapshot(context_manager: Any, usage: dict[str, Any] | None) -> dict[str, Any]:
    context = None
    if context_manager is not None:
        stats = context_manager.get_stats()
        context = {
            "total_tokens": stats.total_tokens,
            "max_tokens": stats.context_window,
            "usage_percentage": stats.usage_percentage,
            "is_estimate": context_manager.actual_input_tokens <= 0,
            "window_is_estimate": context_manager.context_window_is_estimate,
        }
    return {"cache_hit_percentage": cache_hit_percentage(usage), "context": context}


def format_usage_status(snapshot: dict[str, Any]) -> str:
    rate = snapshot.get("cache_hit_percentage")
    cache = f"{rate:.1f}%" if rate is not None else "未提供"
    context = snapshot.get("context")
    if context and context.get("max_tokens"):
        prefix = "约" if context.get("is_estimate") or context.get("window_is_estimate") else ""
        window = f"{prefix}{context['usage_percentage']:.1f}%"
    else:
        window = "未提供"
    return f"缓存命中 {cache} · 上下文 {window}"
