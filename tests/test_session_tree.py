import copy
import json

import pytest

from pepsicode.core.agent_loop import run_agent_turn
from pepsicode.core.session import create_new_session, load_session, save_session
from pepsicode.core.session_tree import (
    branch_path,
    checkout_entry,
    copy_session,
    prepare_regeneration,
    record_messages,
    recording_session,
    tree_view,
)
from pepsicode.tooling import ToolRegistry
from pepsicode.types import AgentStep


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")
    value = create_new_session(str(tmp_path))
    record_messages(
        value,
        [
            {"role": "system", "content": "current instructions"},
            {"role": "user", "content": "implement login"},
            {"role": "assistant", "content": "JWT implementation"},
            {"role": "user", "content": "add tests"},
            {"role": "assistant", "content": "JWT tests"},
        ],
    )
    return value


def test_returning_to_user_preserves_original_branch_and_returns_draft(session):
    original = copy.deepcopy(session.tree["nodes"])
    user = original[0]
    draft = checkout_entry(session, user["id"][:8])
    assert draft == "implement login"
    assert session.tree["leaf_id"] is None
    record_messages(
        session,
        [
            *session.messages,
            {"role": "user", "content": "use Session cookies"},
            {"role": "assistant", "content": "Session implementation"},
        ],
    )
    assert session.tree["nodes"][:4] == original
    assert [node["message"]["content"] for node in branch_path(session)] == [
        "use Session cookies",
        "Session implementation",
    ]
    checkout_entry(session, original[-1]["id"])
    assert [message["content"] for message in session.messages[1:]] == [
        "implement login",
        "JWT implementation",
        "add tests",
        "JWT tests",
    ]
    assert len(tree_view(session)["nodes"]) == 6


def test_fork_is_independent_and_clone_contains_only_active_route(session):
    source_leaf = session.tree["leaf_id"]
    target = session.tree["nodes"][2]
    child, draft = copy_session(session, entry_id=target["id"])
    assert draft == "add tests"
    assert child.session_id != session.session_id and child.workspace == session.workspace
    assert child.parent_session_id == session.session_id
    assert len(child.tree["nodes"]) == 2
    assert session.tree["leaf_id"] == source_leaf
    record_messages(child, [*child.messages, {"role": "user", "content": "alternate tests"}])
    assert len(session.tree["nodes"]) == 4
    checkout_entry(session, target["id"])
    record_messages(
        session, [*session.messages, {"role": "user", "content": "alternate"}, {"role": "assistant", "content": "done"}]
    )
    clone, draft = copy_session(session, clone=True)
    assert draft == "" and len(clone.tree["nodes"]) == 4
    assert len(session.tree["nodes"]) == 6
    clone.messages[-1]["content"] = "mutated copy"
    assert session.messages[-1]["content"] == "done"


def test_regeneration_keeps_expanded_command_snapshot_and_grows_new_branch(session):
    prompt = {
        "role": "user",
        "content": "old expanded template",
        "displayContent": "/review security",
        "commandPath": "old.md",
    }
    record_messages(session, [*session.messages, prompt, {"role": "assistant", "content": "old answer"}])
    old_leaf = session.tree["leaf_id"]
    replacement = prepare_regeneration(session)
    assert replacement["content"] == "old expanded template" and "entryId" not in replacement
    record_messages(session, [*session.messages, replacement, {"role": "assistant", "content": "new answer"}])
    assert old_leaf in {node["id"] for node in session.tree["nodes"]}
    assert session.messages[-1]["content"] == "new answer"


def test_save_reload_preserves_all_routes_and_old_session_import(session):
    original = session.tree["leaf_id"]
    checkout_entry(session, session.tree["nodes"][0]["id"])
    record_messages(session, [*session.messages, {"role": "user", "content": "alternate"}])
    save_session(session)
    restored = load_session(session.session_id)
    assert len(restored.tree["nodes"]) == 5
    checkout_entry(restored, original)
    assert restored.messages[-1]["content"] == "JWT tests"
    old = create_new_session(session.workspace)
    old.messages = [{"role": "user", "content": "legacy"}, {"role": "assistant", "content": "answer"}]
    save_session(old)
    assert len(load_session(old.session_id).tree["nodes"]) == 2


def test_context_snipping_or_compaction_does_not_destroy_original_entries(session):
    original_nodes = copy.deepcopy(session.tree["nodes"])
    current = [
        session.messages[0],
        {"role": "system", "content": "summary", "_compaction_marker": True},
        *session.messages[-2:],
    ]
    current[-1] = {**current[-1], "content": "shortened"}
    record_messages(session, current)
    assert session.tree["nodes"] == original_nodes
    checkout_entry(session, session.tree["nodes"][-1]["id"])
    assert len(session.messages) == 5
    assert not any(message.get("_compaction_marker") for message in session.messages)


def test_model_loop_records_messages_before_they_can_be_compacted(session):
    class Model:
        def next(self, messages):
            return AgentStep(type="assistant", content="recorded final")

    with recording_session(session):
        result = run_agent_turn(model=Model(), tools=ToolRegistry([]), messages=session.messages, cwd=session.workspace)
    assert session.tree["nodes"][-1]["message"]["content"] == "recorded final"
    assert result[-1]["entryId"] == session.tree["leaf_id"]


def test_files_permissions_and_pending_verification_are_not_rewound(session, tmp_path):
    project_file = tmp_path / "code.py"
    project_file.write_text("current disk state", encoding="utf-8")
    session.access_mode = "read_only"
    session.verification_state = {"pending": True, "last_report": {"status": "passed"}}
    checkout_entry(session, session.tree["nodes"][0]["id"])
    child, _ = copy_session(session, clone=True)
    assert project_file.read_text(encoding="utf-8") == "current disk state"
    assert child.access_mode == "read_only"
    assert child.verification_state == {"pending": True, "last_report": None}


def test_bad_targets_and_partial_tool_batches_do_not_change_the_branch(session):
    before = session.tree["leaf_id"]
    with pytest.raises(ValueError):
        checkout_entry(session, "nonexistent")
    assert session.tree["leaf_id"] == before
    record_messages(
        session,
        [
            *session.messages,
            {"role": "assistant_tool_call", "toolUseId": "pending", "toolName": "read_file", "input": {}},
            {"role": "assistant", "content": "partial batch"},
        ],
    )
    before = session.tree["leaf_id"]
    with pytest.raises(ValueError, match="unfinished"):
        checkout_entry(session, before)
    assert session.tree["leaf_id"] == before


def test_failed_atomic_save_keeps_previous_session_file(session, monkeypatch):
    save_session(session)
    previous = load_session(session.session_id)
    record_messages(session, [*session.messages, {"role": "user", "content": "not persisted"}])
    monkeypatch.setattr("pepsicode.core.session.os.replace", lambda *args: (_ for _ in ()).throw(OSError("disk error")))
    with pytest.raises(OSError, match="disk error"):
        save_session(session)
    assert load_session(session.session_id).messages == previous.messages


@pytest.mark.parametrize("damage", ["parent", "duplicate", "leaf", "version"])
def test_damaged_tree_is_rejected_before_resume(session, damage):
    from pepsicode.core.session import _session_file

    save_session(session)
    path = _session_file(session.session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    if damage == "parent":
        data["tree"]["nodes"][0]["parent_id"] = "missing"
    elif damage == "duplicate":
        data["tree"]["nodes"].append(data["tree"]["nodes"][0])
    elif damage == "leaf":
        data["tree"]["leaf_id"] = "missing"
    else:
        data["tree"]["version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_session(session.session_id) is None


def test_branch_selection_clears_old_context_summary_and_provider_reasoning(session):
    from pepsicode.context.context_manager import ContextManager
    from pepsicode.permissions import PermissionManager

    manager = ContextManager(model="custom", context_window=4096)
    manager.compaction_history.append({"summary": "abandoned plan"})
    manager.compact_boundaries.append(object())
    manager.recovery_state.current_tasks = ["abandoned task"]
    manager.actual_input_tokens = 3500
    manager.circuit_breaker.attempts = 3
    manager.circuit_breaker.max_attempts = 7
    manager.circuit_breaker.minimum_reduction_ratio = 0.25
    record_messages(
        session,
        [
            *session.messages,
            {"role": "assistant_thinking", "content": "old signed block", "signature": "provider A"},
            {"role": "assistant", "content": "final"},
        ],
    )
    checkout_entry(session, session.tree["leaf_id"])
    manager.select_branch(session.messages, session.workspace, PermissionManager(session.workspace))
    assert not manager.compaction_history and not manager.compact_boundaries
    assert not manager.recovery_state.current_tasks
    assert not manager.circuit_breaker.attempts and manager.actual_input_tokens == 0
    assert manager.circuit_breaker.max_attempts == 7
    assert manager.circuit_breaker.minimum_reduction_ratio == 0.25
    assert manager.context_window == 4096 and manager.recovery_state.workspace == session.workspace
    assert not any(message["role"] == "assistant_thinking" for message in manager.messages)
    assert any(node["message"]["role"] == "assistant_thinking" for node in session.tree["nodes"])
