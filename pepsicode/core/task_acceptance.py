"""Validated task acceptance conditions shared by tools and persistence."""

from __future__ import annotations

from pathlib import Path


def validate_criteria(criteria):
    if not isinstance(criteria, list) or len(criteria) > 32:
        raise ValueError("criteria must be an array of at most 32 conditions")
    result = []
    ids = set()
    for item in criteria:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise ValueError("each criterion needs a non-empty id")
        key = item["id"].strip()
        if key in ids:
            raise ValueError("criterion IDs must be unique")
        ids.add(key)
        kind = item.get("kind")
        if kind not in {"file_exists", "file_contains", "command"}:
            raise ValueError("criterion kind must be file_exists, file_contains, or command")
        normalized = {"id": key, "kind": kind}
        if kind.startswith("file_"):
            path = item.get("path")
            if (
                not isinstance(path, str)
                or not path.strip()
                or Path(path).is_absolute()
                or Path(path).drive
                or ".." in Path(path).parts
            ):
                raise ValueError("criterion paths must be relative to the workspace")
            normalized["path"] = path
            if kind == "file_contains":
                if not isinstance(item.get("text"), str) or not item["text"]:
                    raise ValueError("file_contains needs non-empty text")
                normalized["text"] = item["text"]
        else:
            argv = item.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
            ):
                raise ValueError("command criterion needs a literal argv array")
            normalized["argv"] = argv
        if set(item) - {"id", "kind", "path", "text", "argv"}:
            raise ValueError("unknown criterion field")
        result.append(normalized)
    return result
