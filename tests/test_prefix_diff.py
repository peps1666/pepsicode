import json

from pepsicode.context.prompt import PROMPT_FORMAT_VERSION
from pepsicode.execution.model_requests import _begin
from pepsicode.execution.prefix_diff import compare_prepared, first_difference, load_journals
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter, _to_anthropic_messages
from pepsicode.tooling import ToolRegistry


def test_first_difference_reports_nested_changes_and_append_without_content():
    assert first_difference([{"content": "secret abc"}], [{"content": "secret xyz"}], "messages") == (
        "messages[0].content@char:7"
    )
    assert first_difference([1], [1, 2], "tools") == "tools[1]"
    assert first_difference({"a": 1}, {"a": 1}) is None


def test_multijournal_cross_task_failures_and_extended_tail(tmp_path):
    body = {
        "model": "m",
        "system": "stable",
        "tools": [],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }
    later = json.loads(json.dumps(body))
    later["messages"][0]["content"].append({"type": "text", "text": "reminder"})
    later["messages"].append({"role": "assistant", "content": []})
    events = [
        {"time": 1, "event": "model/prepared", "task_id": "one", "request_id": "a", "body": body},
        {"time": 2, "event": "model/settled", "task_id": "one", "request_id": "a", "usage": {}},
        {"time": 3, "event": "model/prepared", "task_id": "two", "request_id": "bad", "body": body},
        {"time": 4, "event": "model/failed", "task_id": "two", "request_id": "bad"},
        {"time": 5, "event": "model/prepared", "task_id": "two", "request_id": "b", "body": later},
        {
            "time": 6,
            "event": "model/settled",
            "task_id": "two",
            "request_id": "b",
            "usage": {"input_tokens": 20, "cache_read_tokens": 80, "cache_write_tokens": 0},
        },
    ]
    for filename, selected in (("second.jsonl", events[2:]), ("first.jsonl", events[:2])):
        (tmp_path / filename).write_text("\n".join(json.dumps(item) for item in selected), encoding="utf-8")
    records = load_journals([tmp_path])
    assert compare_prepared(records) == []
    comparison = compare_prepared(records, include_cross_task=True)[0]
    assert comparison["turn_boundary"] and comparison["cross_task"]
    assert comparison["before"] == "a" and comparison["after"] == "b"
    assert comparison["message_change"] == "extended_tail" and comparison["prefix_intact"]
    assert comparison["usage"] == {
        "input_total": 100,
        "cache_read": 80,
        "cache_write": 0,
        "cache_read_ratio": 0.8,
        "cost_usd": None,
    }


def test_parent_child_parent_is_not_compared_by_default():
    body = {"model": "m", "system": "same", "tools": [], "messages": []}
    records = [
        {"event": "model/prepared", "time": i, "task_id": task, "request_id": str(i), "body": body}
        for i, task in enumerate(["parent", "child", "parent"])
    ]
    assert compare_prepared(records) == []
    assert len(compare_prepared(records, include_cross_task=True)) == 2


def test_recorded_request_comparison_covers_lifecycle_boundaries():
    class Journal:
        def __init__(self):
            self.records = []

        def record(self, event, **fields):
            self.records.append({"event": event, **fields})

    journal = Journal()
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "secret"}, ToolRegistry([])
    )
    history = [{"role": "system", "content": "stable"}, {"role": "user", "content": "start"}]

    def record(messages):
        system, converted = _to_anthropic_messages(messages)
        _begin(journal, adapter._build_request("test", system, converted))

    record(history)
    # Tool result is appended after the model's call, not edited into old input.
    history += [
        {"role": "assistant_tool_call", "toolUseId": "a", "toolName": "read", "input": {}},
        {"role": "tool_result", "toolUseId": "a", "content": "result", "isError": False},
    ]
    record(history)
    # Current implementation injects task state as a leading system message.
    record([{"role": "system", "content": "task revision 1"}, *history])
    # A resume from exactly the same durable messages reconstructs the request.
    resumed = json.loads(json.dumps(history))
    record([{"role": "system", "content": "task revision 1"}, *resumed])
    # Compaction and a plan switch are visible as separate prefix boundaries.
    record([{"role": "system", "content": "stable"}, {"role": "user", "content": "summary"}])
    record([{"role": "system", "content": "stable plan"}, {"role": "user", "content": "summary"}])

    diffs = compare_prepared(journal.records)
    assert diffs[0]["differences"] == {"system": None, "tools": None, "messages": "messages[1]"}
    assert diffs[0]["message_change"] == "appended"
    assert diffs[1]["differences"]["system"] == "system@char:0"
    assert diffs[2]["differences"] == {"system": None, "tools": None, "messages": None}
    assert diffs[2]["message_change"] == "unchanged"
    assert diffs[3]["differences"]["messages"] == "messages[0].content[0].text@char:1"
    assert diffs[4]["differences"]["system"] == "system@char:6"
    assert "secret" not in json.dumps(diffs)


def test_real_tool_loop_is_measured_from_execution_journal(tmp_path, monkeypatch):
    import io

    from pepsicode import config
    from pepsicode.core.agent_loop import run_agent_turn
    from pepsicode.execution.runtime import ExecutionJournal, execution_journal_dir
    from pepsicode.tooling import ToolDefinition, ToolResult

    monkeypatch.setattr(config, "PEPSI_CODE_DIR", tmp_path / "home")
    calls = 0

    def send(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        content = (
            [{"type": "tool_use", "id": f"call-{calls}", "name": "read", "input": {}}]
            if calls <= 5
            else [{"type": "text", "text": "<final>done</final>"}]
        )
        body = {
            "content": content,
            "stop_reason": "tool_use" if calls <= 5 else "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        result = io.BytesIO(json.dumps(body).encode())
        result.status = 200
        return result

    monkeypatch.setattr("urllib.request.urlopen", send)
    tool = ToolDefinition(
        "read", "Read", {}, lambda value: value, lambda _value, _context: ToolResult(ok=True, output="x" * 8000)
    )
    registry = ToolRegistry([tool])
    adapter = AnthropicModelAdapter({"model": "test", "baseUrl": "https://example.test", "apiKey": "secret"}, registry)
    result = run_agent_turn(
        model=adapter,
        tools=registry,
        messages=[{"role": "system", "content": "stable"}, {"role": "user", "content": "read files"}],
        cwd=str(tmp_path),
    )
    assert result[-1]["content"] == "done"
    journal = next(execution_journal_dir(str(tmp_path)).glob("*.jsonl"))
    comparisons = compare_prepared(ExecutionJournal.read(journal))
    assert len(comparisons) == 5
    assert all(item["differences"]["system"] is None for item in comparisons)
    assert all(item["differences"]["tools"] is None for item in comparisons)
    assert all(item["message_change"] == "appended" for item in comparisons)
    results = [message for message in result if message.get("role") == "tool_result"]
    assert all("Artifact: ctx_" in message["content"] and len(message["content"]) <= 4000 for message in results)


def test_real_plan_resume_and_compaction_boundaries(tmp_path, monkeypatch):
    from pepsicode.context.context_manager import ContextManager
    from pepsicode.context.prompt import build_session_prompt
    from pepsicode.core import session as sessions
    from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    journal = ExecutionJournal(tmp_path / "journal.jsonl")
    scope = ExecutionScope(str(tmp_path), journal=journal)
    adapter = AnthropicModelAdapter(
        {"model": "test", "baseUrl": "https://example.test", "apiKey": "secret"}, ToolRegistry([])
    )

    def record(messages):
        system, converted = _to_anthropic_messages(messages)
        _begin(scope, adapter._build_request("test", system, converted))

    manager = ContextManager(context_window=1600)
    manager.add_message({"role": "system", "content": build_session_prompt(str(tmp_path))})
    for index in range(30):
        manager.add_message({"role": "user", "content": f"{index} " + "x" * 300})
    record(manager.messages)
    saved = sessions.create_new_session(str(tmp_path))
    saved.messages = manager.messages
    sessions.save_session(saved)
    restored = sessions.load_session(saved.session_id)
    assert restored is not None
    record(restored.messages)
    compacted = manager.compact_messages(force=True)
    assert manager.last_compaction_changed
    record(compacted)
    planned = list(compacted)
    planned[0] = {"role": "system", "content": build_session_prompt(str(tmp_path), plan_mode=True)}
    record(planned)

    diffs = compare_prepared(ExecutionJournal.read(journal.path))
    assert diffs[0]["differences"] == {"system": None, "tools": None, "messages": None}
    assert diffs[1]["differences"]["system"] is not None
    assert diffs[1]["differences"]["messages"] is not None
    assert diffs[2]["differences"]["system"] is not None


def test_task_state_update_is_visible_in_recorded_request(tmp_path):
    from pepsicode.core.agent_loop import _messages_for_model
    from pepsicode.core.task_control import begin_task
    from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope, bind_scope
    from pepsicode.tooling import ToolContext
    from pepsicode.tools.task_state import task_state_tool

    journal = ExecutionJournal(tmp_path / "task.jsonl")
    scope = ExecutionScope(str(tmp_path), journal=journal)
    registry = ToolRegistry([task_state_tool])
    adapter = AnthropicModelAdapter({"model": "test", "baseUrl": "https://example.test", "apiKey": "secret"}, registry)
    messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "CSV export please"}]

    def record():
        system, converted = _to_anthropic_messages(_messages_for_model(messages, None))
        _begin(scope, adapter._build_request("test", system, converted))

    with bind_scope(scope):
        task = begin_task(messages, {})
        record()
        result = registry.execute(
            "task_state",
            {
                "action": "classify",
                "task_id": task["id"],
                "revision": task["revision"],
                "kind": "change",
                "reason": "user requested implementation",
            },
            ToolContext(str(tmp_path), execution=scope),
        )
        assert result.ok
        record()
    comparison = compare_prepared(ExecutionJournal.read(journal.path))[0]
    assert comparison["differences"]["system"] is None
    assert comparison["differences"]["tools"] is None
    assert comparison["message_change"] == "extended_tail"


def test_task_context_is_appended_once_per_change_and_survives_resume(tmp_path):
    from pepsicode.core.agent_loop import _messages_for_model
    from pepsicode.core.task_control import begin_task, edit_control
    from pepsicode.execution.runtime import ExecutionScope, bind_scope

    scope = ExecutionScope(str(tmp_path))
    messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "write code"}]
    requests = []
    with bind_scope(scope):
        begin_task(messages, {})
        for value in ("one", "two", "three"):
            with edit_control() as control:
                control["task"]["objective"] = value
            result = _messages_for_model(messages, None)
            system, converted = _to_anthropic_messages(result)
            requests.append(
                {
                    "event": "model/prepared",
                    "request_id": value,
                    "body": {"system": system, "messages": converted, "tools": []},
                }
            )
        resumed = json.loads(json.dumps(messages))
        _messages_for_model(resumed, None)
        assert resumed == messages
    for item in compare_prepared(requests):
        assert item["differences"]["system"] is None
        assert item["message_change"] == "extended_tail"


def test_internal_context_survives_save_load_and_checkout(tmp_path, monkeypatch):
    from pepsicode.core import session as sessions
    from pepsicode.core.session_tree import checkout_entry, record_messages

    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", tmp_path)
    session = sessions.create_new_session(str(tmp_path))
    messages = [{"role": "system", "content": "stable"}, {"role": "user", "content": "begin"}]
    for index in range(3):
        messages += [
            {"role": "user", "internal": True, "content": f"reminder {index}"},
            {"role": "assistant", "content": f"reply {index}"},
        ]
    record_messages(session, messages)
    last_id = messages[-1]["entryId"]
    sessions.save_session(session)
    restored = sessions.load_session(session.session_id)
    assert restored is not None
    checkout_entry(restored, last_id)
    assert [m["content"] for m in restored.messages if m.get("internal")] == [f"reminder {i}" for i in range(3)]
    system, before = _to_anthropic_messages(restored.messages)
    restored.messages.append({"role": "user", "content": "continue"})
    new_system, after = _to_anthropic_messages(restored.messages)
    assert system == new_system and after[: len(before)] == before


def test_plan_mode_switch_projects_latest_context_without_rewriting_system(tmp_path):
    from pepsicode.context.prompt import build_session_prompt
    from pepsicode.core.agent_loop import _messages_for_model
    from pepsicode.permissions import PermissionManager

    permissions = PermissionManager(str(tmp_path))
    registry = ToolRegistry([])
    system = build_session_prompt(str(tmp_path), dynamic_sections=[])
    messages = [
        {"role": "system", "content": system, "_prompt_format": PROMPT_FORMAT_VERSION},
        {"role": "user", "content": "start"},
    ]
    bodies = []
    for action in ("off", "enter", "exit"):
        if action == "enter":
            permissions.enter_plan_mode()
        elif action == "exit":
            permissions.mode = permissions.previous_mode
        _messages_for_model(messages, None, cwd=str(tmp_path), permissions=permissions, tools=registry)
        prompt, converted = _to_anthropic_messages(messages)
        bodies.append(
            {
                "event": "model/prepared",
                "request_id": action,
                "body": {"system": prompt, "messages": converted, "tools": []},
            }
        )
    comparisons = compare_prepared(bodies)
    assert all(c["differences"]["system"] is None and c["message_change"] == "extended_tail" for c in comparisons)
    assert "PLAN MODE IS ACTIVE" in messages[-2]["content"]
    assert "PLAN MODE IS INACTIVE" in messages[-1]["content"]


def test_session_projection_is_deduplicated_and_follows_tool_batch(tmp_path):
    from pepsicode.context.prompt import build_session_prompt
    from pepsicode.core.agent_loop import _messages_for_model
    from pepsicode.permissions import PermissionManager

    registry = ToolRegistry([])
    permissions = PermissionManager(str(tmp_path))
    messages = [
        {
            "role": "system",
            "content": build_session_prompt(str(tmp_path), dynamic_sections=[]),
            "_prompt_format": PROMPT_FORMAT_VERSION,
        },
        {"role": "user", "content": "start"},
    ]
    _messages_for_model(messages, None, cwd=str(tmp_path), permissions=permissions, tools=registry)
    count = len(messages)
    _messages_for_model(messages, None, cwd=str(tmp_path), permissions=permissions, tools=registry)
    assert len(messages) == count
    messages += [
        {"role": "assistant_tool_call", "toolName": "read", "toolUseId": "1", "input": {}},
        {"role": "tool_result", "toolName": "read", "toolUseId": "1", "content": "ok", "isError": False},
    ]
    (tmp_path / "new-file.txt").write_text("test", encoding="utf-8")
    _messages_for_model(messages, None, cwd=str(tmp_path), permissions=permissions, tools=registry)
    assert messages[-2]["role"] == "tool_result" and messages[-1]["internal"]
    _, converted = _to_anthropic_messages(messages)
    assert converted[-1]["content"][0]["type"] == "tool_result"
    assert "new-file.txt" in converted[-1]["content"][-1]["text"]
    prior_count = len(messages)
    messages.append({"role": "assistant", "content": "done"})
    messages.append({"role": "user", "content": "next turn"})
    _messages_for_model(messages, None, cwd=str(tmp_path), permissions=permissions, tools=registry)
    assert len(messages) == prior_count + 3
    assert messages[-1]["source"]["kind"] == "session-context-projection"


def test_desktop_session_keeps_stable_prompt_across_plan_switch(tmp_path):
    from pepsicode.core.session import create_new_session
    from pepsicode.permissions import PermissionManager
    from pepsicode.server.app import ClientSession

    session = ClientSession(str(tmp_path))
    session.permissions = PermissionManager(str(tmp_path))
    session.tools = ToolRegistry([])
    session.session_data = create_new_session(str(tmp_path))
    session._rebuild_system_prompt()
    original = session.messages[0]["content"]
    session.permissions.enter_plan_mode()
    session._rebuild_system_prompt()
    assert session.messages[0]["content"] == original
    assert session.messages[0]["_prompt_format"] == PROMPT_FORMAT_VERSION
