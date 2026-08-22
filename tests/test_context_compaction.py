from __future__ import annotations

from pepsicode.context.context_manager import (
    CompactBoundary,
    ContextManager,
    RecoveryState,
    load_context_state,
    save_context_state,
    validate_tool_pairs,
)


def _add_tool_batch(manager: ContextManager, batch: int, size: int = 400) -> None:
    for suffix in ("a", "b"):
        manager.add_message(
            {
                "role": "assistant_tool_call",
                "toolUseId": f"{batch}-{suffix}",
                "toolName": "grep",
                "input": {"path": f"src/{batch}.py"},
            }
        )
    for suffix in ("a", "b"):
        manager.add_message(
            {
                "role": "tool_result",
                "toolUseId": f"{batch}-{suffix}",
                "toolName": "grep",
                "content": "x" * size,
                "isError": False,
            }
        )


def test_compaction_keeps_or_drops_multi_tool_batches_atomically():
    manager = ContextManager(context_window=2_000)
    manager.add_message({"role": "system", "content": "sys"})
    for batch in range(12):
        _add_tool_batch(manager, batch)

    compacted = manager.compact_messages(force=True)

    assert manager.last_compaction_changed
    assert validate_tool_pairs(compacted) == []
    survivor_ids = {message.get("toolUseId") for message in compacted if message.get("role") == "assistant_tool_call"}
    for batch in range(12):
        members = {f"{batch}-a", f"{batch}-b"}
        assert not (members & survivor_ids) or members <= survivor_ids


def test_boundary_carries_recovery_state_and_artifact_references():
    manager = ContextManager(context_window=1_600)
    manager.recovery_state = RecoveryState(
        workspace="D:/work",
        active_plan_path="D:/work/.pepsi-code/plans/plan.md",
        permission_mode="plan",
        current_tasks=["implement compaction"],
    )
    manager.add_message({"role": "system", "content": "sys"})
    for index in range(30):
        content = "x" * 300
        if index == 0:
            content += " Artifact: ctx_0123456789abcdef01234567"
        manager.add_message({"role": "user", "content": content})

    compacted = manager.compact_messages(force=True)
    boundary = manager.compact_boundaries[-1]
    marker = next(message for message in compacted if message.get("_compaction_marker"))

    assert isinstance(boundary, CompactBoundary)
    assert boundary.artifact_ids == ["ctx_0123456789abcdef01234567"]
    assert "Permission mode: plan" in marker["content"]
    assert "implement compaction" in marker["content"]


def test_repeated_compaction_rolls_forward_summary_and_artifacts():
    manager = ContextManager(context_window=1_400)
    manager.messages = [{"role": "system", "content": "sys"}]
    for index in range(30):
        suffix = " Artifact: ctx_111111111111111111111111" if index == 0 else ""
        manager.add_message({"role": "user", "content": "first.py " + "x" * 240 + suffix})
    manager.compact_messages(force=True)

    for index in range(30):
        suffix = " Artifact: ctx_222222222222222222222222" if index == 0 else ""
        manager.add_message({"role": "user", "content": "second.py " + "y" * 240 + suffix})
    manager.compact_messages(force=True)

    latest = manager.compact_boundaries[-1]
    markers = [message for message in manager.messages if message.get("_compaction_marker")]
    assert "first.py" in latest.summary
    assert "second.py" in latest.summary
    assert latest.artifact_ids == [
        "ctx_111111111111111111111111",
        "ctx_222222222222222222222222",
    ]
    assert len(markers) == 1


def test_recovery_state_observes_tools_without_storing_full_outputs():
    manager = ContextManager()
    manager.observe_tool_result("read_file", {"path": "src/app.py"}, "large" * 10_000, True)
    manager.observe_tool_result(
        "todo_write",
        {
            "todos": [
                {"content": "active", "status": "in_progress"},
                {"content": "done", "status": "completed"},
            ]
        },
        "updated",
        True,
    )
    manager.observe_tool_result("test_runner", {}, "42 passed\nrest", True)

    assert manager.recovery_state.recent_files[-1].path == "src/app.py"
    assert manager.recovery_state.current_tasks == ["active"]
    assert manager.recovery_state.last_verification == "test_runner passed: 42 passed"
    assert "large" not in manager.recovery_state.render()


def test_compaction_circuit_breaks_on_unchanged_noop_context():
    manager = ContextManager(context_window=100_000)
    manager.messages = [{"role": "user", "content": "small"}]

    assert manager.compact_messages(force=True) == manager.messages
    first_reason = manager.last_compaction_error
    assert manager.compact_messages(force=True) == manager.messages
    assert first_reason == "no eligible atomic message groups could be removed"
    assert "unchanged" in manager.last_compaction_error
    assert manager.compaction_history == []


def test_context_recovery_metadata_persists(monkeypatch, tmp_path):
    import pepsicode.context.context_manager as context_module

    monkeypatch.setattr(context_module, "PEPSI_CODE_DIR", tmp_path)
    manager = ContextManager()
    manager.recovery_state.remember_file("src/app.py", "read")
    manager.compact_boundaries.append(
        CompactBoundary(
            version=1,
            summary="summary",
            compacted_message_count=4,
            before_tokens=1_000,
            after_tokens=500,
            protected_tail_count=3,
            artifact_ids=["ctx_0123456789abcdef01234567"],
            created_at=1.0,
        )
    )

    save_context_state(manager)
    loaded = load_context_state()

    assert loaded is not None
    assert loaded.recovery_state.recent_files[-1].path == "src/app.py"
    assert loaded.compact_boundaries[-1].summary == "summary"
    assert loaded.compact_boundaries[-1].artifact_ids == ["ctx_0123456789abcdef01234567"]
