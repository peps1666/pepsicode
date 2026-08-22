from __future__ import annotations

from pepsicode.context.context_artifacts import (
    DEFAULT_ARTIFACT_READ_CHARS,
    MAX_ARTIFACT_READ_CHARS,
    ContextArtifactStore,
)
from pepsicode.tooling import ToolCapability, ToolDefinition, ToolResult


def _validate(input_data: dict) -> dict:
    artifact_id = input_data.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id is required")
    offset = input_data.get("offset", 0)
    max_chars = input_data.get("max_chars", DEFAULT_ARTIFACT_READ_CHARS)
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    return {
        "artifact_id": artifact_id.strip(),
        "offset": offset,
        "max_chars": min(max_chars, MAX_ARTIFACT_READ_CHARS),
    }


def _run(input_data: dict, context) -> ToolResult:
    store = ContextArtifactStore.for_workspace(context.cwd)
    chunk, total, end = store.read_chunk(
        input_data["artifact_id"],
        offset=input_data["offset"],
        max_chars=input_data["max_chars"],
    )
    return ToolResult(
        ok=True,
        output=(f"[Artifact {input_data['artifact_id']} chars {input_data['offset']}:{end} of {total}]\n{chunk}"),
    )


read_context_artifact_tool = ToolDefinition(
    name="read_context_artifact",
    description="Read a bounded chunk of a persisted oversized tool result by artifact ID.",
    input_schema={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_ARTIFACT_READ_CHARS},
        },
        "required": ["artifact_id"],
    },
    validator=_validate,
    run=_run,
    capabilities={ToolCapability.READ_ONLY},
    max_result_size_chars=MAX_ARTIFACT_READ_CHARS + 200,
)
