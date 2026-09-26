"""Pluggable storage backends for the layered memory system.

Decouples *where* memory is persisted from *how* it is read/written by the
``MemoryManager``.  Two backends ship today:

- ``FileMemoryStore``     - the original on-disk MEMORY.md/memory.json store
- ``PostgresMemoryStore`` - a PostgreSQL-backed store (single ``pepsi_memory``
                            table, one row per entry across all three scopes)

``create_memory_manager`` in ``pepsicode.context.memory`` uses the file store
unless PostgreSQL is explicitly requested, and falls back to the file store
when the database cannot be reached.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Protocol

from pepsicode.config import PEPSI_CODE_DIR
from pepsicode.context.memory import MemoryEntry, MemoryFile, MemoryScope

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PostgreSQL connection parameters
# ---------------------------------------------------------------------------
# Read at call time so tests and operators can set the environment after
# import.  There is no default password: a missing password is a failed
# connection, and the factory then keeps the file store.


def _pg_setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def postgres_requested() -> bool:
    """Whether this process asked to store memory in PostgreSQL."""
    if os.environ.get("PEPSI_MEMORY_BACKEND", "").strip().lower() == "postgres":
        return True
    return any(
        os.environ.get(name)
        for name in (
            "PEPSI_MEMORY_PG_DBNAME",
            "PEPSI_MEMORY_PG_USER",
            "PEPSI_MEMORY_PG_PASSWORD",
            "PEPSI_MEMORY_PG_HOST",
            "PEPSI_MEMORY_PG_PORT",
        )
    )


# ---------------------------------------------------------------------------
# MemoryStore protocol
# ---------------------------------------------------------------------------


class MemoryStore(Protocol):
    """Storage backend contract for the memory system.

    Each scope (user/project/local) is saved and loaded independently.  A
    backend receives the full entry list for a scope on save and returns the
    full list on load, so it does not need to understand incremental edits.
    """

    def save_scope(self, scope: MemoryScope, entries: list[MemoryEntry]) -> None:
        """Persist all entries for a scope (replaces prior content)."""
        ...

    def load_scope(self, scope: MemoryScope) -> list[MemoryEntry]:
        """Load all entries for a scope.  Returns [] when empty/unavailable."""
        ...


# ---------------------------------------------------------------------------
# File backend (the original implementation, extracted verbatim)
# ---------------------------------------------------------------------------


class FileMemoryStore:
    """On-disk store: MEMORY.md + memory.json per scope directory."""

    def __init__(self, workspace: str):
        self.workspace = workspace
        workspace_path = Path(workspace)
        self.paths = {
            MemoryScope.USER: PEPSI_CODE_DIR / "memory",
            MemoryScope.PROJECT: workspace_path / ".pepsi-code-memory",
            MemoryScope.LOCAL: workspace_path / ".pepsi-code-memory-local",
        }

    # -- path helpers ------------------------------------------------------

    def _get_scope_path(self, scope: MemoryScope) -> Path:
        return self.paths[scope]

    def _ensure_scope_path(self, scope: MemoryScope) -> None:
        self._get_scope_path(scope).mkdir(parents=True, exist_ok=True)

    # -- load --------------------------------------------------------------

    def load_scope(self, scope: MemoryScope) -> list[MemoryEntry]:
        path = self._get_scope_path(scope)
        memory_md = path / "MEMORY.md"
        memory_json = path / "memory.json"

        if not memory_md.exists() and not memory_json.exists():
            return []

        # Prefer structured JSON metadata when present.
        if memory_json.exists():
            try:
                data = json.loads(memory_json.read_text(encoding="utf-8"))
                return [MemoryEntry.from_dict(entry_data) for entry_data in data.get("entries", [])]
            except (json.JSONDecodeError, KeyError):
                pass  # fall through to Markdown parsing

        if memory_md.exists():
            content = memory_md.read_text(encoding="utf-8")
            return self._parse_memory_md(content, scope)
        return []

    @staticmethod
    def _parse_memory_md(content: str, scope: MemoryScope) -> list[MemoryEntry]:
        """Parse a MEMORY.md file into entries (mirrors MemoryManager logic)."""
        lines = content.split("\n")
        current_category = "general"
        entry_counter = 0
        entries: list[MemoryEntry] = []

        for line in lines:
            line = line.strip()

            if line.startswith("#") or line.startswith("*") or not line:
                if line.startswith("## "):
                    current_category = line[3:].strip().lower()
                continue

            if line.startswith("- "):
                entry_content = line[2:]

                # Extract ``tags`` delimited by backticks.
                tags: list[str] = []
                if "`" in entry_content:
                    tag_matches = re.findall(r"`([^`]+)`", entry_content)
                    for tag_match in tag_matches:
                        tags.extend(tag_match.split())
                    entry_content = re.sub(r"`[^`]+`", "", entry_content).strip()

                entry_counter += 1
                entries.append(
                    MemoryEntry(
                        id=f"{scope.value}-{entry_counter}",
                        scope=scope,
                        category=current_category,
                        content=entry_content,
                        tags=tags,
                    )
                )
        return entries

    # -- save --------------------------------------------------------------

    def save_scope(self, scope: MemoryScope, entries: list[MemoryEntry]) -> None:
        self._ensure_scope_path(scope)
        path = self._get_scope_path(scope)

        # Structured metadata.
        memory_json = path / "memory.json"
        data = {
            "scope": scope.value,
            "last_updated": time.time(),
            "entries": [e.to_dict() for e in entries],
        }
        memory_json.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Human-readable mirror.
        memory_md = path / "MEMORY.md"
        memory_file = MemoryFile(scope=scope, entries=entries)
        memory_md.write_text(memory_file.format_as_markdown(), encoding="utf-8")


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------


class PostgresMemoryStore:
    """PostgreSQL-backed store.  Single table, one row per memory entry.

    psycopg2 is imported lazily so that simply having this module on the path
    never fails when the driver (or the database) is unavailable - the
    ``create_memory_manager`` factory catches the failure and falls back to
    ``FileMemoryStore``.
    """

    def __init__(self, workspace: str):
        self.workspace = workspace
        self._conn: Any = None
        self._connect()
        self._ensure_table()

    def describe(self) -> str:
        """Return a log-safe description of the configured database."""
        return (
            f"{_pg_setting('PEPSI_MEMORY_PG_DBNAME', 'my_first_db')}@"
            f"{_pg_setting('PEPSI_MEMORY_PG_HOST', 'localhost')}:"
            f"{_pg_setting('PEPSI_MEMORY_PG_PORT', '5432')}"
        )

    def _scope_workspace(self, scope: MemoryScope) -> str:
        """User memory is global. Project and local memory stay in one workspace."""
        if scope is MemoryScope.USER:
            return ""
        return str(Path(self.workspace).resolve())

    # -- connection --------------------------------------------------------

    def _connect(self) -> None:
        try:
            import psycopg2  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError(f"psycopg2 not installed: {error}") from error

        password = _pg_setting("PEPSI_MEMORY_PG_PASSWORD")
        if not password:
            raise RuntimeError("PEPSI_MEMORY_PG_PASSWORD is not set")

        self._conn = psycopg2.connect(
            dbname=_pg_setting("PEPSI_MEMORY_PG_DBNAME", "my_first_db"),
            user=_pg_setting("PEPSI_MEMORY_PG_USER", "postgres"),
            password=password,
            host=_pg_setting("PEPSI_MEMORY_PG_HOST", "localhost"),
            port=_pg_setting("PEPSI_MEMORY_PG_PORT", "5432"),
        )
        self._conn.autocommit = False

    def _ensure_table(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pepsi_memory (
                    workspace   TEXT        NOT NULL DEFAULT '',
                    scope       TEXT        NOT NULL,
                    id          TEXT        NOT NULL,
                    category    TEXT        NOT NULL DEFAULT 'general',
                    content     TEXT        NOT NULL,
                    created_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
                    updated_at  DOUBLE PRECISION NOT NULL DEFAULT 0,
                    tags        JSONB       NOT NULL DEFAULT '[]'::jsonb,
                    usage_count INTEGER     NOT NULL DEFAULT 0,
                    PRIMARY KEY (workspace, scope, id)
                )
                """
            )
            cur.execute("ALTER TABLE pepsi_memory ADD COLUMN IF NOT EXISTS workspace TEXT NOT NULL DEFAULT ''")
            cur.execute(
                """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a
                  ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = 'pepsi_memory'::regclass AND i.indisprimary
                """
            )
            primary_columns = {row[0] for row in cur.fetchall()}
            if "workspace" not in primary_columns:
                cur.execute("ALTER TABLE pepsi_memory DROP CONSTRAINT IF EXISTS pepsi_memory_pkey")
                cur.execute("ALTER TABLE pepsi_memory ADD PRIMARY KEY (workspace, scope, id)")
        self._conn.commit()

    # -- load --------------------------------------------------------------

    def load_scope(self, scope: MemoryScope) -> list[MemoryEntry]:
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT id, category, content, created_at, updated_at, "
                    "tags, usage_count FROM pepsi_memory "
                    "WHERE scope = %s AND workspace = %s "
                    "ORDER BY created_at",
                    (scope.value, self._scope_workspace(scope)),
                )
                rows = cur.fetchall()
            self._conn.commit()
        except Exception as error:  # noqa: BLE001 - never break reads
            logger.warning("PostgresMemoryStore.load_scope failed: %s", error)
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            return []

        entries: list[MemoryEntry] = []
        for row in rows:
            tags = row[5]
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except (json.JSONDecodeError, TypeError):
                    tags = []
            entries.append(
                MemoryEntry(
                    id=row[0],
                    scope=scope,
                    category=row[1] or "general",
                    content=row[2],
                    created_at=row[3] or time.time(),
                    updated_at=row[4] or time.time(),
                    tags=tags if isinstance(tags, list) else [],
                    usage_count=row[6] or 0,
                )
            )
        return entries

    # -- save --------------------------------------------------------------

    def save_scope(self, scope: MemoryScope, entries: list[MemoryEntry]) -> None:
        workspace = self._scope_workspace(scope)
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM pepsi_memory WHERE scope = %s AND workspace = %s",
                    (scope.value, workspace),
                )
                for entry in entries:
                    cur.execute(
                        """
                        INSERT INTO pepsi_memory
                            (workspace, scope, id, category, content, created_at,
                             updated_at, tags, usage_count)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            workspace,
                            scope.value,
                            entry.id,
                            entry.category,
                            entry.content,
                            entry.created_at,
                            entry.updated_at,
                            json.dumps(entry.tags),
                            entry.usage_count,
                        ),
                    )
            self._conn.commit()
        except Exception as error:  # noqa: BLE001 - never break writes
            logger.warning("PostgresMemoryStore.save_scope failed: %s", error)
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
