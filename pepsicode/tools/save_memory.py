from __future__ import annotations

from pepsicode.tooling import ToolDefinition, ToolResult

_SEARCH_LIMIT = 20


def _scope_enum(scope: str):
    from pepsicode.context.memory import MemoryScope

    return {
        "user": MemoryScope.USER,
        "project": MemoryScope.PROJECT,
        "local": MemoryScope.LOCAL,
    }[scope]


def _manager(context):
    """Use the session manager so writes show up in the next prompt."""
    memory_mgr = context.memory
    if memory_mgr is None:
        from pepsicode.context.memory import create_memory_manager

        memory_mgr = create_memory_manager(context.cwd)
    return memory_mgr


def _validate_scope(scope: object) -> str:
    if scope not in ("user", "project", "local"):
        raise ValueError("scope must be one of: user, project, local")
    return str(scope)


def _validate_save(input_data: dict) -> dict:
    scope = _validate_scope(input_data.get("scope"))

    category = input_data.get("category")
    if not isinstance(category, str) or not category.strip():
        raise ValueError("category is required")
    category = category.strip()

    content = input_data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content is required")
    content = content.strip()

    tags_provided = "tags" in input_data and input_data.get("tags") is not None
    tags = input_data.get("tags", [])
    if tags_provided and (not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags)):
        raise ValueError("tags must be a list of strings")

    entry_id = input_data.get("id")
    if entry_id is not None:
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("id must be a non-empty string")
        entry_id = entry_id.strip()

    return {
        "scope": scope,
        "category": category,
        "content": content,
        "tags": [str(tag) for tag in tags] if tags_provided else None,
        "id": entry_id,
    }


def _run_save(input_data: dict, context) -> ToolResult:
    memory_mgr = _manager(context)
    scope_enum = _scope_enum(input_data["scope"])
    entry_id = input_data.get("id")

    if entry_id:
        updated = memory_mgr.update_entry(
            scope_enum,
            entry_id,
            input_data["content"],
            category=input_data["category"],
            tags=input_data["tags"],
        )
        if not updated:
            return ToolResult(
                ok=False,
                output=f"No {input_data['scope']} memory entry with id {entry_id}. Search first; do not create a new one.",
            )
        entry = next(item for item in memory_mgr.memories[scope_enum].entries if item.id == entry_id)
        verb = "Updated"
    else:
        entry = memory_mgr.add_entry(
            scope=scope_enum,
            category=input_data["category"],
            content=input_data["content"],
            tags=input_data["tags"] or [],
        )
        verb = "Saved to"

    preview = entry.content[:80]
    tags_str = f" [{', '.join(entry.tags)}]" if entry.tags else ""
    return ToolResult(
        ok=True,
        output=(
            f"{verb} {entry.scope.value} memory\n"
            f"  Category: {entry.category}\n"
            f"  Content: {preview}{tags_str}\n"
            f"  ID: {entry.id}"
        ),
    )


def _validate_forget(input_data: dict) -> dict:
    scope = _validate_scope(input_data.get("scope"))
    entry_id = input_data.get("id")
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise ValueError("id is required")
    return {"scope": scope, "id": entry_id.strip()}


def _run_forget(input_data: dict, context) -> ToolResult:
    memory_mgr = _manager(context)
    scope_enum = _scope_enum(input_data["scope"])
    if not memory_mgr.delete_entry(scope_enum, input_data["id"]):
        return ToolResult(
            ok=False,
            output=f"No {input_data['scope']} memory entry with id {input_data['id']}.",
        )
    return ToolResult(ok=True, output=f"Forgot {input_data['scope']} memory {input_data['id']}")


def _validate_search(input_data: dict) -> dict:
    query = input_data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required")
    scope = input_data.get("scope")
    if scope is not None:
        scope = _validate_scope(scope)
    return {"query": query.strip(), "scope": scope}


def _run_search(input_data: dict, context) -> ToolResult:
    memory_mgr = _manager(context)
    scope_enum = _scope_enum(input_data["scope"]) if input_data["scope"] else None
    matches = memory_mgr.search(input_data["query"], scope_enum)
    matches.sort(key=lambda entry: entry.updated_at, reverse=True)
    matches = matches[:_SEARCH_LIMIT]
    if not matches:
        return ToolResult(ok=True, output="No matching memory entries.")
    lines = []
    for entry in matches:
        tags_str = f" [{', '.join(entry.tags)}]" if entry.tags else ""
        lines.append(f"{entry.id}  {entry.scope.value}  {entry.category}{tags_str}\n  {entry.content}")
    return ToolResult(ok=True, output="\n".join(lines))


save_memory_tool = ToolDefinition(
    name="save_memory",
    description=(
        "Persist a durable memory entry (decision, convention, pattern, fact) "
        "that should survive across sessions. Stored under the chosen scope: "
        "'user' (cross-project), 'project' (shared/versioned), or 'local' "
        "(project-specific, not checked in). Pass id to replace that existing "
        "entry instead of appending a second one. Use search_memory when you "
        "need an id that is not already in the prompt."
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
                "description": "The memory content to remember, or the replacement text when id is set.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags. When id is set, tags are replaced only if this field is present.",
            },
            "id": {
                "type": "string",
                "description": "Existing entry id to replace. Omit to add a new entry.",
            },
        },
        "required": ["scope", "category", "content"],
    },
    validator=_validate_save,
    run=_run_save,
)

forget_memory_tool = ToolDefinition(
    name="forget_memory",
    description=(
        "Delete one memory entry by scope and id. Use this when a remembered "
        "fact should not be kept. Do not leave a contradictory entry beside it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["user", "project", "local"],
                "description": "Which memory layer the entry is stored in.",
            },
            "id": {
                "type": "string",
                "description": "Entry id from the prompt or from search_memory.",
            },
        },
        "required": ["scope", "id"],
    },
    validator=_validate_forget,
    run=_run_forget,
)

search_memory_tool = ToolDefinition(
    name="search_memory",
    description=(
        "Find memory entries by a keyword in content, category, or tags. "
        "Returns id, scope, and category so you can update or forget a match "
        "that is not in the current prompt."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Substring to match against content, category, or tags.",
            },
            "scope": {
                "type": "string",
                "enum": ["user", "project", "local"],
                "description": "Optional layer to search. Omit to search all layers.",
            },
        },
        "required": ["query"],
    },
    validator=_validate_search,
    run=_run_search,
)
