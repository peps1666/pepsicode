from __future__ import annotations

from pepsicode.execution.filesystem import FILES
from pepsicode.tooling import ToolDefinition, ToolResult
from pepsicode.workspace import resolve_tool_path


def _validate(input_data: dict) -> dict:
    changes = input_data.get("changes")
    if not isinstance(changes, list):
        raise ValueError("changes must be a list")
    if not changes:
        raise ValueError("changes cannot be empty")

    for i, change in enumerate(changes):
        if not isinstance(change, dict):
            raise ValueError(f"changes[{i}] must be an object")
        if "file" not in change:
            raise ValueError(f"changes[{i}] must have a 'file' field")
        if "old" not in change or "new" not in change:
            raise ValueError(f"changes[{i}] must have 'old' and 'new' fields")

    dry_run = input_data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")

    return {"changes": changes, "dry_run": dry_run}


def _run(input_data: dict, context) -> ToolResult:
    """Execute multi-file edit."""
    changes = input_data["changes"]
    dry_run = input_data["dry_run"]

    results = []
    success_count = 0
    error_count = 0
    total_changes = 0

    for i, change in enumerate(changes):
        file_path = resolve_tool_path(context, change["file"], "write" if not dry_run else "read")
        old_text = change["old"]
        new_text = change["new"]

        # Validate file exists
        if not file_path.exists():
            results.append(
                {
                    "file": change["file"],
                    "status": "error",
                    "message": f"File not found: {file_path}",
                }
            )
            error_count += 1
            continue

        try:
            snapshot = FILES.snapshot(context, file_path, for_edit=not dry_run)
            content = snapshot.text
        except Exception as e:
            results.append(
                {
                    "file": change["file"],
                    "status": "error",
                    "message": f"Read error: {e}",
                }
            )
            error_count += 1
            continue

        # Count occurrences
        occurrences = content.count(old_text)
        if occurrences == 0:
            results.append(
                {
                    "file": change["file"],
                    "status": "warning",
                    "message": "Pattern not found",
                    "old_preview": old_text[:50],
                }
            )
            continue

        total_changes += occurrences

        if dry_run:
            results.append(
                {
                    "file": change["file"],
                    "status": "dry_run",
                    "message": f"Would replace {occurrences} occurrence(s)",
                    "old_preview": old_text[:50],
                    "new_preview": new_text[:50],
                }
            )
            continue

        # Perform replacement
        new_content = content.replace(old_text, new_text)

        try:
            FILES.write_text(context, file_path, new_content, expected=snapshot.version)
            results.append(
                {
                    "file": change["file"],
                    "status": "success",
                    "message": f"Replaced {occurrences} occurrence(s)",
                    "old_preview": old_text[:50],
                    "new_preview": new_text[:50],
                }
            )
            success_count += 1
        except Exception as e:
            results.append(
                {
                    "file": change["file"],
                    "status": "error",
                    "message": f"Write error: {e}",
                }
            )
            error_count += 1

    # Format output
    lines = ["Multi-File Edit Result", "=" * 50, ""]

    if dry_run:
        lines.append("🔍 DRY RUN - No files were modified")
        lines.append("")

    lines.append("Summary:")
    lines.append(f"  Files processed: {len(changes)}")
    lines.append(f"  Successful: {success_count}")
    lines.append(f"  Errors: {error_count}")
    lines.append(f"  Total replacements: {total_changes}")
    lines.append("")

    for result in results:
        status_icon = {
            "success": "[OK]",
            "error": "[X]",
            "warning": "[!]",
            "dry_run": "🔍",
        }.get(result["status"], "?")

        lines.append(f"{status_icon} {result['file']}")
        lines.append(f"   {result['message']}")

        if "old_preview" in result:
            lines.append(f"   Old: {result['old_preview']}...")
        if "new_preview" in result:
            lines.append(f"   New: {result['new_preview']}...")

        lines.append("")

    if not dry_run and success_count > 0:
        lines.append("💡 Tip: Run your tests to verify the changes work correctly.")

    return ToolResult(
        ok=error_count == 0,
        output="\n".join(lines),
    )


multi_edit_tool = ToolDefinition(
    name="multi_edit",
    description="Edit multiple files at once by finding and replacing text patterns. Supports dry-run mode to preview changes before applying. Use this for cross-file refactoring like renaming, moving code, or updating imports.",
    input_schema={
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "description": "List of changes to apply. Each change: {file, old, new}",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File path relative to workspace"},
                        "old": {"type": "string", "description": "Text to find"},
                        "new": {"type": "string", "description": "Text to replace with"},
                    },
                    "required": ["file", "old", "new"],
                },
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, only preview changes without modifying files (default: false)",
            },
        },
        "required": ["changes"],
    },
    validator=_validate,
    run=_run,
)
