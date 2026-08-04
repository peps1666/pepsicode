"""Persistent storage for oversized tool results kept outside model context."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ARTIFACT_ID_PATTERN = re.compile(r"^ctx_[0-9a-f]{24}$")
ARTIFACT_REFERENCE_PATTERN = re.compile(r"\bctx_[0-9a-f]{24}\b")
DEFAULT_ARTIFACT_READ_CHARS = 12_000
MAX_ARTIFACT_READ_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class ContextArtifact:
    artifact_id: str
    tool_name: str
    path: str
    size_chars: int
    sha256: str
    created_at: float


class ContextArtifactStore:
    """Content-addressed, workspace-local artifact storage."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    @classmethod
    def for_workspace(cls, workspace: str | Path) -> ContextArtifactStore:
        return cls(Path(workspace) / ".pepsi-code" / "tool-results")

    @staticmethod
    def _validate_id(artifact_id: str) -> str:
        candidate = artifact_id.strip().lower()
        if not ARTIFACT_ID_PATTERN.fullmatch(candidate):
            raise ValueError("Invalid context artifact ID")
        return candidate

    def _path_for(self, artifact_id: str) -> Path:
        safe_id = self._validate_id(artifact_id)
        path = (self.root / f"{safe_id}.txt").resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise ValueError("Artifact path escapes storage root") from error
        return path

    def save(self, tool_name: str, content: str) -> ContextArtifact:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        artifact_id = f"ctx_{digest[:24]}"
        target = self._path_for(artifact_id)
        self.root.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            fd, temp_name = tempfile.mkstemp(prefix=f".{artifact_id}-", suffix=".tmp", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    handle.write(content)
                os.replace(temp_name, target)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
        return ContextArtifact(
            artifact_id=artifact_id,
            tool_name=tool_name,
            path=str(target),
            size_chars=len(content),
            sha256=digest,
            created_at=target.stat().st_mtime,
        )

    def read_chunk(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        max_chars: int = DEFAULT_ARTIFACT_READ_CHARS,
    ) -> tuple[str, int, int]:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        limit = max(1, min(int(max_chars), MAX_ARTIFACT_READ_CHARS))
        target = self._path_for(artifact_id)
        if not target.is_file():
            raise FileNotFoundError(f"Context artifact not found: {artifact_id}")
        content = target.read_text(encoding="utf-8")
        end = min(len(content), offset + limit)
        return content[offset:end], len(content), end

    def exists(self, artifact_id: str) -> bool:
        try:
            return self._path_for(artifact_id).is_file()
        except ValueError:
            return False

    def cleanup(self, max_age_days: int = 30) -> int:
        if not self.root.is_dir():
            return 0
        cutoff = time.time() - max(0, max_age_days) * 86_400
        removed = 0
        for path in self.root.glob("ctx_*.txt"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        return removed


def prepare_tool_result(
    *,
    tool_name: str,
    output: str,
    budget_chars: int,
    artifact_store: ContextArtifactStore | None,
) -> str:
    """Bound a tool result while preserving the complete output as an artifact."""
    budget = max(1_000, int(budget_chars))
    if len(output) <= budget:
        return output

    artifact: ContextArtifact | None = None
    if artifact_store is not None:
        try:
            artifact = artifact_store.save(tool_name, output)
        except OSError:
            artifact = None

    # Keep a conservative fixed allowance for metadata/separators so the
    # returned value never exceeds the tool's advertised character budget.
    remaining = max(200, budget - 400)
    head_size = remaining // 2
    tail_size = remaining - head_size
    head = output[:head_size]
    tail = output[-tail_size:]
    removed = max(0, len(output) - len(head) - len(tail))
    artifact_lines = (
        [
            f"Artifact: {artifact.artifact_id}",
            "Use read_context_artifact with offset/max_chars to inspect the full result in chunks.",
        ]
        if artifact is not None
        else ["Artifact persistence failed; only this bounded preview is available."]
    )
    return "\n".join(
        [
            f"[Tool output bounded: {len(output):,} chars; {removed:,} chars omitted from context]",
            *artifact_lines,
            "",
            "--- head ---",
            head,
            "--- tail ---",
            tail,
        ]
    )
