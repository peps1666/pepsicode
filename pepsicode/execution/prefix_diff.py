"""Content-free comparison of provider request components recorded in journals.

The API does not expose its internal serialization or tokenization. These
structural differences are diagnostics, not a prediction of cached tokens.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

_PUBLIC_KEYS = frozenset(
    {
        "role",
        "content",
        "type",
        "text",
        "name",
        "input",
        "input_schema",
        "description",
        "tool_use_id",
        "id",
        "is_error",
        "properties",
        "required",
        "items",
    }
)


def first_difference(before: Any, after: Any, path: str = "") -> str | None:
    """Return the first differing JSON path, including appended elements."""
    if type(before) is not type(after):
        return path or "$"
    if isinstance(before, dict):
        for key in list(before) + [key for key in after if key not in before]:
            # Tool arguments and arbitrary JSON keys may themselves contain secrets.
            label = key if key in _PUBLIC_KEYS else "key#" + hashlib.sha256(str(key).encode()).hexdigest()[:12]
            child = f"{path}.{label}" if path else label
            if key not in before or key not in after:
                return child
            difference = first_difference(before[key], after[key], child)
            if difference is not None:
                return difference
        return None
    if isinstance(before, list):
        for index, (left, right) in enumerate(zip(before, after)):
            difference = first_difference(left, right, f"{path}[{index}]")
            if difference is not None:
                return difference
        return f"{path}[{min(len(before), len(after))}]" if len(before) != len(after) else None
    if before == after:
        return None
    if isinstance(before, str) and isinstance(after, str):
        offset = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)))
        return f"{path}@char:{offset}"
    return path or "$"


def compare_bodies(before: dict[str, Any], after: dict[str, Any]) -> dict[str, str | None]:
    return {key: first_difference(before.get(key), after.get(key), key) for key in ("system", "tools", "messages")}


def message_change(before: dict[str, Any], after: dict[str, Any]) -> str:
    old, new = before.get("messages"), after.get("messages")
    if old == new:
        return "unchanged"
    if isinstance(old, list) and isinstance(new, list) and len(new) > len(old) and new[: len(old)] == old:
        return "appended"
    if isinstance(old, list) and isinstance(new, list) and old and len(new) >= len(old):
        a, b = old[-1], new[len(old) - 1]
        if old[:-1] == new[: len(old) - 1] and isinstance(a, dict) and isinstance(b, dict):
            ac, bc = a.get("content"), b.get("content")
            if (
                {k: v for k, v in a.items() if k != "content"} == {k: v for k, v in b.items() if k != "content"}
                and isinstance(ac, list)
                and isinstance(bc, list)
                and len(bc) > len(ac)
                and bc[: len(ac)] == ac
            ):
                return "extended_tail"
    return "changed"


def load_journals(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    files: list[Path] = []
    for item in paths:
        path = Path(item)
        files.extend(sorted(path.glob("*.jsonl")) if path.is_dir() else [path])
    records = []
    for path in dict.fromkeys(p.resolve() for p in files):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    records.append({**json.loads(line), "_journal": str(path)})
    return sorted(records, key=lambda r: r.get("time", 0))


def _usage(usage: Any) -> dict[str, Any] | None:
    if not isinstance(usage, dict) or "input_tokens" not in usage:
        return None
    read = usage.get("cache_read_tokens")
    write = usage.get("cache_write_tokens")
    total = usage["input_tokens"] + (read or 0) + (write or 0)
    return {
        "input_total": total,
        "cache_read": read,
        "cache_write": write,
        "cache_read_ratio": read / total if read is not None and total else None,
        "cost_usd": None,
    }


def compare_prepared(records: Iterable[dict[str, Any]], *, include_cross_task: bool = False) -> list[dict[str, Any]]:
    records = list(records)
    if all(isinstance(r.get("time"), (int, float)) for r in records):
        records.sort(key=lambda r: r["time"])
    prepared = [r for r in records if r.get("event") == "model/prepared" and isinstance(r.get("body"), dict)]
    outcomes = {r.get("request_id"): r for r in records if r.get("event") in {"model/settled", "model/failed"}}

    def failed(record: dict[str, Any]) -> bool:
        outcome = outcomes.get(record.get("request_id"), {})
        return outcome.get("event") == "model/failed" or (outcome.get("status") or 0) >= 400

    prepared = [r for r in prepared if not failed(r)]
    positions = {id(record): index for index, record in enumerate(records)}
    result = []
    for a, b in zip(prepared, prepared[1:]):
        cross_task = a.get("task_id") != b.get("task_id")
        if cross_task and not include_cross_task:
            continue
        diffs = compare_bodies(a["body"], b["body"])
        change = message_change(a["body"], b["body"])
        model_changed = a["body"].get("model") != b["body"].get("model")
        endpoint_changed = a.get("endpoint") != b.get("endpoint")
        intact = (
            not model_changed
            and not endpoint_changed
            and diffs["system"] is None
            and diffs["tools"] is None
            and change != "changed"
        )
        invalidates = (
            (
                "endpoint"
                if endpoint_changed
                else "model"
                if model_changed
                else next((k for k in ("tools", "system", "messages") if diffs[k] is not None), None)
            )
            if not intact
            else None
        )
        outcome = outcomes.get(b.get("request_id"), {})
        result.append(
            {
                "before": a.get("request_id"),
                "after": b.get("request_id"),
                "task_id": b.get("task_id"),
                "cross_task": cross_task,
                "turn_boundary": a.get("_journal") != b.get("_journal"),
                "model_changed": model_changed,
                "endpoint_changed": endpoint_changed,
                "message_change": change,
                "differences": diffs,
                "prefix_intact": intact,
                "invalidates_from": invalidates,
                "prefix_breaks": [
                    {"reason": "tool-discovery", "added_count": event.get("added_count")}
                    for event in records[positions[id(a)] + 1 : positions[id(b)]]
                    if event.get("event") == "model/prefix-break"
                    and event.get("reason") == "tool-discovery"
                    and event.get("task_id") == b.get("task_id")
                ],
                "status": "settled" if outcome.get("event") == "model/settled" else "unknown",
                "usage": _usage(outcome.get("usage")),
            }
        )
    return result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Compare adjacent recorded provider requests without printing their contents"
    )
    parser.add_argument("journals", type=Path, nargs="+")
    parser.add_argument("--include-cross-task", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            compare_prepared(load_journals(args.journals), include_cross_task=args.include_cross_task),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
