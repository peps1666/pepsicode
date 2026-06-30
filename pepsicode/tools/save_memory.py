from __future__ import annotations

from pepsicode.tooling import ToolDefinition, ToolResult


def _validate(input_data: dict) -> dict:
    scope = input_data.get("scope")
    if scope not in ("user", "project", "local"):
        raise ValueError("scope must be one of: user, project, local")

    category = input_data.get("category")
    if not isinstance(category, str) or not category.strip():
        raise ValueError("category is required")
    category = category.strip()

    content = input_data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content is required")
    content = content.strip()

    tags = input_data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError("tags must be a list of strings")

    return {"scope": scope, "category": category, "content": content, "tags": tags}


def _run(input_data: dict, context) -> ToolResult:
    from pepsicode.memory import MemoryScope

    scope_map = {
        "user": MemoryScope.USER,
        "project": MemoryScope.PROJECT,
        "local": MemoryScope.LOCAL,
    }
    scope_enum = scope_map[input_data["scope"]]

    # Reuse create_memory_manager so the same backend selection (PostgreSQL
    # preferred, file fallback) applies as in the main session.
    from pepsicode.memory import create_memory_manager

    memory_mgr = create_memory_manager(context.cwd)

    entry = memory_mgr.add_entry(
        scope=scope_enum,
        category=input_data["category"],
        content=input_data["content"],
        tags=input_data["tags"],
    )

    preview = entry.content[:80]
    tags_str = f" [{', '.join(entry.tags)}]" if entry.tags else ""
    return ToolResult(
        ok=True,
        output=(
            f"Saved to {entry.scope.value} memory\n"
            f"  Category: {entry.category}\n"
            f"  Content: {preview}{tags_str}\n"
            f"  ID: {entry.id}"
        ),
    )


save_memory_tool = ToolDefinition(
    name="save_memory",
    description=(
        "Persist a durable memory entry (decision, convention, pattern, fact) "
        "that should survive across sessions. Stored under the chosen scope: "
        "'user' (cross-project), 'project' (shared/versioned), or 'local' "
        "(project-specific, not checked in). Use this when you learn "
        "something worth remembering for future sessions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["user", "project", "local"],
                "description": "Which memory layer to store under.",
            },
            "category": {
                "type": "string",
                "description": "Grouping label, e.g. 'architecture', 'convention', 'decision', 'pattern'.",
            },
            "content": {
                "type": "string",
                "description": "The memory content to remember.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for later retrieval.",
            },
        },
        "required": ["scope", "category", "content"],
    },
    validator=_validate,
    run=_run,
)
