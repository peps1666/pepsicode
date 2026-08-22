from __future__ import annotations

import os
import time

import pytest

from pepsicode.context.context_artifacts import ContextArtifactStore, prepare_tool_result
from pepsicode.context.context_manager import ContextManager
from pepsicode.core.agent_loop import run_agent_turn
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools import create_default_tool_registry
from pepsicode.types import AgentStep, ModelAdapter


def test_artifact_store_round_trip_and_chunk_bounds(tmp_path):
    store = ContextArtifactStore.for_workspace(tmp_path)
    artifact = store.save("grep", "abcdefghij" * 3_000)

    assert store.exists(artifact.artifact_id)
    chunk, total, end = store.read_chunk(artifact.artifact_id, offset=7, max_chars=12)
    assert chunk == ("abcdefghij" * 3_000)[7:19]
    assert total == 30_000
    assert end == 19

    with pytest.raises(ValueError, match="Invalid context artifact ID"):
        store.read_chunk("../secret")


def test_prepare_tool_result_persists_full_output_and_returns_bounded_preview(tmp_path):
    store = ContextArtifactStore.for_workspace(tmp_path)
    output = "full-output\n" * 4_000
    bounded = prepare_tool_result(
        tool_name="run_command",
        output=output,
        budget_chars=4_000,
        artifact_store=store,
    )

    artifact_id = next(word for word in bounded.split() if word.startswith("ctx_"))
    artifact_id = artifact_id.rstrip(".,")
    assert len(bounded) <= 4_000
    assert store.read_chunk(artifact_id, max_chars=20_000)[0] == output[:20_000]

    minimum_budget_preview = prepare_tool_result(
        tool_name="run_command",
        output=output,
        budget_chars=1_000,
        artifact_store=store,
    )
    assert len(minimum_budget_preview) <= 1_000


def test_artifact_cleanup_removes_only_expired_files(tmp_path):
    store = ContextArtifactStore.for_workspace(tmp_path)
    old = store.save("grep", "old")
    fresh = store.save("grep", "fresh")
    old_path = store.root / f"{old.artifact_id}.txt"
    os.utime(old_path, (time.time() - 40 * 86_400,) * 2)

    assert store.cleanup(max_age_days=30) == 1
    assert not store.exists(old.artifact_id)
    assert store.exists(fresh.artifact_id)


def test_read_context_artifact_is_allowed_in_plan_mode(tmp_path):
    store = ContextArtifactStore.for_workspace(tmp_path)
    artifact = store.save("grep", "0123456789")
    permissions = PermissionManager(str(tmp_path))
    permissions.enter_plan_mode()
    registry = create_default_tool_registry(str(tmp_path))

    result = registry.execute(
        "read_context_artifact",
        {"artifact_id": artifact.artifact_id, "offset": 2, "max_chars": 4},
        ToolContext(cwd=str(tmp_path), permissions=permissions),
    )

    assert result.ok
    assert result.output.endswith("2345")


class _LargeResultModel(ModelAdapter):
    def __init__(self) -> None:
        self.calls = 0
        self.last_usage = None

    def next(self, _messages):
        self.calls += 1
        if self.calls == 1:
            return AgentStep(
                type="tool",
                calls=[{"id": "call-1", "toolName": "large", "input": {}}],
            )
        return AgentStep(type="assistant", content="done")


def test_agent_loop_bounds_new_tool_results_and_tracks_artifact(tmp_path):
    large_tool = ToolDefinition(
        name="large",
        description="Return a large result",
        input_schema={"type": "object"},
        validator=lambda value: value,
        run=lambda _value, _context: ToolResult(ok=True, output="z" * 30_000),
        max_result_size_chars=3_000,
    )
    manager = ContextManager(context_window=100_000)
    messages = run_agent_turn(
        model=_LargeResultModel(),
        tools=ToolRegistry([large_tool]),
        messages=[{"role": "user", "content": "go"}],
        cwd=str(tmp_path),
        context_manager=manager,
    )

    result = next(message for message in messages if message.get("role") == "tool_result")
    artifact_id = next(word.rstrip(".,") for word in result["content"].split() if word.startswith("ctx_"))
    assert len(result["content"]) <= 3_000
    assert ContextArtifactStore.for_workspace(tmp_path).exists(artifact_id)
