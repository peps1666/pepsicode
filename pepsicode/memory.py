"""Layered memory system for cross-session knowledge retention.

Provides three-tier memory hierarchy:
- User memory (~/.pepsi-code/memory/) - cross-project, persistent
- Project memory (.pepsi-code-memory/) - shared across sessions, can be versioned
- Local memory (.pepsi-code-memory-local/) - project-specific, not checked in

Memory is automatically injected into system prompts to give the agent
context about past decisions, codebase patterns, and project conventions.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pepsicode.config import PEPSI_CODE_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class MemoryScope(str, Enum):
    """Memory scope levels."""
    USER = "user"       # Cross-project, ~/.pepsi-code/memory/
    PROJECT = "project" # Project-shared, .pepsi-code-memory/
    LOCAL = "local"     # Project-local, .pepsi-code-memory-local/


@dataclass
class MemoryEntry:
    """A single memory entry (fact, pattern, decision, etc.)."""
    id: str
    scope: MemoryScope
    category: str  # e.g., "architecture", "convention", "decision", "pattern"
    content: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    usage_count: int = 0  # How often this was referenced
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "scope": self.scope.value,
            "category": self.category,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": self.tags,
            "usage_count": self.usage_count,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEntry":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            scope=MemoryScope(data.get("scope", "user")),
            category=data.get("category", "general"),
            content=data["content"],
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            tags=data.get("tags", []),
            usage_count=data.get("usage_count", 0),
        )


@dataclass
class MemoryFile:
    """Represents a MEMORY.md file content."""
    scope: MemoryScope
    entries: list[MemoryEntry] = field(default_factory=list)
    max_entries: int = 200  # Claude Code limit
    max_size_bytes: int = 25 * 1024  # 25KB limit
    
    @property
    def size_bytes(self) -> int:
        """Estimate size in bytes."""
        return sum(len(e.content) for e in self.entries)
    
    def add_entry(self, entry: MemoryEntry) -> None:
        """Add entry, respecting limits."""
        self.entries.append(entry)
        self._enforce_limits()
    
    def update_entry(self, entry_id: str, content: str) -> bool:
        """Update existing entry."""
        for entry in self.entries:
            if entry.id == entry_id:
                entry.content = content
                entry.updated_at = time.time()
                return True
        return False
    
    def delete_entry(self, entry_id: str) -> bool:
        """Delete entry."""
        for i, entry in enumerate(self.entries):
            if entry.id == entry_id:
                self.entries.pop(i)
                return True
        return False
    
    def get_entries_by_category(self, category: str) -> list[MemoryEntry]:
        """Get entries filtered by category."""
        return [e for e in self.entries if e.category == category]
    
    def search(self, query: str) -> list[MemoryEntry]:
        """Search entries by keyword."""
        query_lower = query.lower()
        results = []
        for entry in self.entries:
            if (query_lower in entry.content.lower() or
                query_lower in entry.category.lower() or
                any(query_lower in tag.lower() for tag in entry.tags)):
                results.append(entry)
        return results
    
    def _enforce_limits(self) -> None:
        """Remove oldest entries if exceeding limits."""
        # Check entry count
        while len(self.entries) > self.max_entries:
            self.entries.pop(0)  # Remove oldest
        
        # Check size
        while self.size_bytes > self.max_size_bytes and self.entries:
            self.entries.pop(0)
    
    def format_as_markdown(self, include_header: bool = True) -> str:
        """Format as MEMORY.md content."""
        lines = []
        
        if include_header:
            scope_names = {
                MemoryScope.USER: "User Memory",
                MemoryScope.PROJECT: "Project Memory",
                MemoryScope.LOCAL: "Local Memory",
            }
            lines.append(f"# {scope_names[self.scope]}")
            lines.append("")
            lines.append(f"*Last updated: {time.strftime('%Y-%m-%d %H:%M')}*")
            lines.append("")
        
        # Group by category
        categories: dict[str, list[MemoryEntry]] = {}
        for entry in self.entries:
            if entry.category not in categories:
                categories[entry.category] = []
            categories[entry.category].append(entry)
        
        for category, entries in categories.items():
            lines.append(f"## {category.title()}")
            lines.append("")
            for entry in entries:
                tags_str = f" `{' '.join(entry.tags)}`" if entry.tags else ""
                lines.append(f"- {entry.content}{tags_str}")
            lines.append("")
        
        return "\n".join(lines)


# Import MemoryStore at runtime *after* MemoryScope/MemoryEntry/MemoryFile are
# defined.  memory_store imports those three names from this module at its own
# load time, so importing it earlier would cause a circular-import failure.
# Placed here (not at module top) so the names it needs are already bound, and
# so the ``MemoryStore`` annotation on MemoryManager resolves for both static
# checkers and runtime get_type_hints().
from pepsicode.memory_store import MemoryStore  # noqa: E402


# ---------------------------------------------------------------------------
# Memory Manager
# ---------------------------------------------------------------------------

@dataclass
class MemoryPaths:
    """Paths for memory files at different scopes."""
    user_memory: Path
    project_memory: Path
    local_memory: Path
    
    @classmethod
    def for_workspace(cls, workspace: str) -> "MemoryPaths":
        """Create memory paths for a workspace."""
        workspace_path = Path(workspace)
        
        return cls(
            user_memory=PEPSI_CODE_DIR / "memory",
            project_memory=workspace_path / ".pepsi-code-memory",
            local_memory=workspace_path / ".pepsi-code-memory-local",
        )


class MemoryManager:
    """Manages layered memory system.

    Storage is delegated to a ``MemoryStore`` backend (file or PostgreSQL).
    When a PostgreSQL backend is active, writes are mirrored to the file store
    too, so the human-readable MEMORY.md stays current and there is always a
    on-disk fallback if the database becomes unavailable.
    """

    def __init__(self, workspace: str, store: "MemoryStore | None" = None):
        self.workspace = workspace
        self.paths = MemoryPaths.for_workspace(workspace)
        self.memories: dict[MemoryScope, MemoryFile] = {
            MemoryScope.USER: MemoryFile(scope=MemoryScope.USER),
            MemoryScope.PROJECT: MemoryFile(scope=MemoryScope.PROJECT),
            MemoryScope.LOCAL: MemoryFile(scope=MemoryScope.LOCAL),
        }
        # Lazily build the default file store to avoid an import cycle:
        # memory_store imports names from this module at module load time.
        from pepsicode.memory_store import FileMemoryStore  # noqa: F401
        self.store: MemoryStore = store if store is not None else FileMemoryStore(workspace)
        # File store is always kept around for mirroring / fallback, even when
        # the primary backend is PostgreSQL.
        self._file_store = FileMemoryStore(workspace)
        self._load_all()

    def _load_all(self) -> None:
        """Load all memory files from the active backend."""
        for scope in MemoryScope:
            self._load_scope(scope)

    def _load_scope(self, scope: MemoryScope) -> None:
        """Load memory entries for a scope from the backend."""
        entries = self.store.load_scope(scope)
        self.memories[scope].entries.extend(entries)

    def _save_scope(self, scope: MemoryScope) -> None:
        """Persist a scope to the primary backend, mirrored to the file store.

        PG is the primary; the file copy is always written so MEMORY.md stays
        human-readable and survives a database outage.  File-store failures are
        swallowed (the primary write is what matters); primary failures are
        logged inside the store itself and do not abort the call.
        """
        entries = self.memories[scope].entries
        # Primary backend first.
        self.store.save_scope(scope, entries)
        # Mirror to file store for readability + fallback.  Only attempt when
        # the primary is not already the file store.
        if self.store is not self._file_store:
            try:
                self._file_store.save_scope(scope, entries)
            except Exception:  # noqa: BLE001 - mirror is best-effort
                pass
    
    def add_entry(
        self,
        scope: MemoryScope,
        category: str,
        content: str,
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        """Add a new memory entry."""
        entry_id = f"{scope.value}-{int(time.time())}-{len(self.memories[scope].entries)}"
        entry = MemoryEntry(
            id=entry_id,
            scope=scope,
            category=category,
            content=content,
            tags=tags or [],
        )
        
        self.memories[scope].add_entry(entry)
        self._save_scope(scope)
        return entry
    
    def update_entry(self, scope: MemoryScope, entry_id: str, content: str) -> bool:
        """Update an existing entry."""
        if self.memories[scope].update_entry(entry_id, content):
            self._save_scope(scope)
            return True
        return False
    
    def delete_entry(self, scope: MemoryScope, entry_id: str) -> bool:
        """Delete an entry."""
        if self.memories[scope].delete_entry(entry_id):
            self._save_scope(scope)
            return True
        return False
    
    def search(self, query: str, scope: MemoryScope | None = None) -> list[MemoryEntry]:
        """Search across memory scopes."""
        results = []
        
        scopes_to_search = [scope] if scope else list(MemoryScope)
        
        for s in scopes_to_search:
            results.extend(self.memories[s].search(query))
        
        # Sort by usage count (most used first)
        results.sort(key=lambda e: e.usage_count, reverse=True)
        return results
    
    def get_relevant_context(
        self,
        max_entries: int = 20,
        max_tokens: int = 8000,
    ) -> str:
        """Get relevant memory context for system prompt injection.
        
        Returns formatted MEMORY.md content from all scopes,
        respecting token limits.
        """
        from pepsicode.context_manager import estimate_tokens
        
        parts = []
        total_tokens = 0
        
        # Priority order: LOCAL > PROJECT > USER
        for scope in [MemoryScope.LOCAL, MemoryScope.PROJECT, MemoryScope.USER]:
            memory = self.memories[scope]
            if not memory.entries:
                continue
            
            formatted = memory.format_as_markdown(include_header=True)
            tokens = estimate_tokens(formatted)
            
            if total_tokens + tokens <= max_tokens:
                parts.append(formatted)
                total_tokens += tokens
            else:
                # Partial: include only recent entries
                remaining_tokens = max_tokens - total_tokens
                partial_entries = memory.entries[-max_entries:]
                partial_memory = MemoryFile(scope=scope, entries=partial_entries)
                formatted = partial_memory.format_as_markdown(include_header=True)
                
                if estimate_tokens(formatted) <= remaining_tokens:
                    parts.append(formatted)
                break
        
        if not parts:
            return ""
        
        return "\n\n".join(parts)

    def get_stats(self) -> dict[str, Any]:
        """Get memory statistics."""
        return {
            scope.value: {
                "entries": len(memory.entries),
                "size_bytes": memory.size_bytes,
                "categories": list(set(e.category for e in memory.entries)),
            }
            for scope, memory in self.memories.items()
        }
    
    def format_stats(self) -> str:
        """Format memory stats for display."""
        stats = self.get_stats()
        lines = ["Memory System Status", "=" * 40, ""]
        
        for scope_name, scope_stats in stats.items():
            lines.append(f"{scope_name.title()} Memory:")
            lines.append(f"  Entries: {scope_stats['entries']}")
            lines.append(f"  Size: {scope_stats['size_bytes'] / 1024:.1f} KB")
            if scope_stats['categories']:
                lines.append(f"  Categories: {', '.join(scope_stats['categories'][:5])}")
            lines.append("")
        
        return "\n".join(lines)
    
    def clear_scope(self, scope: MemoryScope) -> None:
        """Clear all entries in a scope."""
        self.memories[scope] = MemoryFile(scope=scope)
        self._save_scope(scope)


# ---------------------------------------------------------------------------
# Factory: pick the best available backend (PostgreSQL preferred, file fallback)
# ---------------------------------------------------------------------------

def create_memory_manager(workspace: str) -> MemoryManager:
    """Create a MemoryManager backed by PostgreSQL when available.

    Tries ``PostgresMemoryStore`` first.  If psycopg2 is missing or the
    database is unreachable, silently falls back to ``FileMemoryStore`` so the
    agent always has working memory - just stored locally instead of in the
    database.
    """
    from pepsicode.memory_store import (
        FileMemoryStore,
        PostgresMemoryStore,
        PG_DBNAME,
        PG_HOST,
        PG_PORT,
    )

    try:
        store = PostgresMemoryStore(workspace)
        logger.info("Memory backend: PostgreSQL (%s@%s:%s)",
                    PG_DBNAME, PG_HOST, PG_PORT)
        return MemoryManager(workspace=workspace, store=store)
    except Exception as error:  # noqa: BLE001 - fallback is the whole point
        logger.info("Memory backend: file (PostgreSQL unavailable: %s)", error)
        return MemoryManager(workspace=workspace, store=FileMemoryStore(workspace))


# ---------------------------------------------------------------------------
# System prompt integration
# ---------------------------------------------------------------------------

def inject_memory_into_prompt(
    system_prompt: str,
    memory_manager: MemoryManager,
    max_tokens: int = 8000,
) -> str:
    """Inject memory context into system prompt."""
    memory_context = memory_manager.get_relevant_context(max_tokens=max_tokens)
    
    if not memory_context:
        return system_prompt
    
    return f"""{system_prompt}

## Project Memory & Context

The following information has been accumulated from previous sessions:

{memory_context}

Use this context to inform your decisions and follow established patterns."""


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def format_memory_list(scope: MemoryScope | None = None, category: str | None = None) -> str:
    """Format memory entries for CLI display."""
    # This would be called with a MemoryManager instance
    # Placeholder for CLI command formatting
    return "Memory listing not available without MemoryManager instance."
