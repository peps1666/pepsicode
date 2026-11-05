import asyncio
from pathlib import Path

import pytest

from pepsicode.context.instructions import load_agent_instructions
from pepsicode.context.prompt import PROMPT_FORMAT_VERSION, build_session_prompt
from pepsicode.core.agent_loop import _messages_for_model, run_agent_turn, run_agent_turn_stream
from pepsicode.core.async_context import AsyncContextCollector
from pepsicode.tooling import ToolRegistry
from pepsicode.types import AgentStep, StreamToken


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    # Keep fixtures independent of a checkout containing this test's temp dir.
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.mark.parametrize("worktree", [False, True])
def test_global_and_project_chain_stops_at_nearest_git_root(tmp_path, isolated_home, worktree):
    root = tmp_path / "project"
    cwd = root / "src" / "feature"
    cwd.mkdir(parents=True)
    if worktree:
        (root / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    else:
        (root / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("outside rules", encoding="utf-8")
    global_file = isolated_home / ".pepsi-code" / "AGENTS.md"
    global_file.parent.mkdir()
    global_file.write_text("global rules", encoding="utf-8")
    (root / "AGENTS.md").write_text("root rules", encoding="utf-8-sig")
    (root / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("feature rules 中文", encoding="utf-8")

    context = load_agent_instructions(str(cwd))

    assert context.index("global rules") < context.index("root rules") < context.index("src rules")
    assert context.index("src rules") < context.index("feature rules 中文")
    assert "outside rules" not in context
    assert "\ufeff" not in context


def test_non_git_project_does_not_read_ancestors_or_descendants(tmp_path, monkeypatch):
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if path.name == ".git" else original_exists(path))
    cwd = tmp_path / "plain"
    child = cwd / "child"
    child.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("parent rules", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("current rules", encoding="utf-8")
    (child / "AGENTS.md").write_text("child rules", encoding="utf-8")

    context = load_agent_instructions(str(cwd))

    assert "current rules" in context
    assert "parent rules" not in context and "child rules" not in context


def test_missing_empty_and_legacy_files_are_not_instructions(tmp_path, isolated_home):
    (tmp_path / "AGENTS.md").write_text(" \n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("legacy project rule", encoding="utf-8")
    legacy_global = isolated_home / ".claude" / "CLAUDE.md"
    legacy_global.parent.mkdir()
    legacy_global.write_text("legacy global rule", encoding="utf-8")

    assert load_agent_instructions(str(tmp_path)) == ""
    prompt = build_session_prompt(str(tmp_path))
    assert "legacy project rule" not in prompt and "legacy global rule" not in prompt
    (tmp_path / "AGENTS.md").unlink()
    assert load_agent_instructions(str(tmp_path)) == ""


def test_instruction_budget_bounds_reads_and_marks_utf8_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.context.instructions.MAX_INSTRUCTION_BYTES", 7)
    (tmp_path / "AGENTS.md").write_text("中文规则" * 100, encoding="utf-8")

    context = load_agent_instructions(str(tmp_path))

    assert "中文" in context and "规则" not in context
    assert "budget (7 bytes) reached" in context and "omitted" in context
    assert "\ufffd" not in context


@pytest.mark.parametrize("failure", ["encoding", "permission"])
def test_unreadable_instruction_is_visible(tmp_path, monkeypatch, failure):
    instructions = tmp_path / "AGENTS.md"
    instructions.write_bytes(b"\xff")
    if failure == "permission":
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == instructions:
                raise PermissionError("access denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)

    context = load_agent_instructions(str(tmp_path))

    assert "could not load AGENTS.md" in context
    assert str(instructions) in context


@pytest.mark.parametrize("managed", [False, True])
def test_requests_refresh_instructions_without_rewriting_history(tmp_path, managed):
    file = tmp_path / "AGENTS.md"
    file.write_text("original rule", encoding="utf-8")
    sections = [] if managed else None
    system = build_session_prompt(str(tmp_path), dynamic_sections=sections)
    # Managed sessions keep project text out of their stable system prefix.
    assert ("original rule" not in system) if managed else ("original rule" in system)
    messages = [{"role": "system", "content": "subagent" if not managed else system}]
    if managed:
        messages[0]["_prompt_format"] = PROMPT_FORMAT_VERSION
    tools = ToolRegistry([])
    _messages_for_model(messages, None, cwd=str(tmp_path), tools=tools)
    snapshot = [dict(message) for message in messages]
    assert "original rule" in "\n".join(message["content"] for message in messages)
    _messages_for_model(messages, None, cwd=str(tmp_path), tools=tools)
    assert messages == snapshot

    file.write_text("updated rule", encoding="utf-8")
    _messages_for_model(messages, None, cwd=str(tmp_path), tools=tools)
    assert messages[: len(snapshot)] == snapshot
    assert "updated rule" in messages[-1]["content"]
    assert messages[-1]["source"]["kind"] == "agent-instructions-projection"

    file.unlink()
    _messages_for_model(messages, None, cwd=str(tmp_path), tools=tools)
    latest = next(
        message
        for message in reversed(messages)
        if message.get("source", {}).get("kind") == "agent-instructions-projection"
    )
    assert "Previously loaded file instructions no longer apply" in latest["content"]
    other_project = tmp_path / "other"
    other_project.mkdir()
    (other_project / "AGENTS.md").write_text("other project rule", encoding="utf-8")
    _messages_for_model(messages, None, cwd=str(other_project), tools=tools)
    latest = next(
        message
        for message in reversed(messages)
        if message.get("source", {}).get("kind") == "agent-instructions-projection"
    )
    assert "other project rule" in latest["content"]
    assert "updated rule" not in latest["content"]


@pytest.mark.parametrize("streaming", [False, True])
def test_actual_model_request_receives_project_rules(tmp_path, streaming):
    (tmp_path / "AGENTS.md").write_text("project acceptance rule", encoding="utf-8")
    requests = []

    class Model:
        def next(self, messages):
            requests.append([dict(message) for message in messages])
            return AgentStep(type="assistant", content="<final>done")

        def next_stream(self, messages):
            requests.append([dict(message) for message in messages])
            yield StreamToken(type="text", content="<final>done")
            yield StreamToken(type="done")

    runner = run_agent_turn_stream if streaming else run_agent_turn
    runner(
        model=Model(),
        tools=ToolRegistry([]),
        messages=[{"role": "user", "content": "read project"}],
        cwd=str(tmp_path),
        max_steps=2,
    )
    assert requests
    assert any("project acceptance rule" in message.get("content", "") for message in requests[0])


def test_async_context_uses_same_loader_and_refreshes_without_cache_delay(tmp_path):
    file = tmp_path / "AGENTS.md"
    file.write_text("first rule", encoding="utf-8")
    collector = AsyncContextCollector(str(tmp_path))
    context = asyncio.run(collector.get_user_context())
    assert "first rule" in collector.format_context_for_prompt(context)
    assert "claude_md" not in context
    file.write_text("second rule", encoding="utf-8")
    assert "second rule" in asyncio.run(collector.get_user_context())["agents_md"]
    file.unlink()
    assert "agents_md" not in asyncio.run(collector.get_user_context())


def test_desktop_rebuilds_legacy_system_prompt_on_resume(tmp_path):
    from pepsicode.permissions import PermissionManager
    from pepsicode.server.app import ClientSession

    session = ClientSession(str(tmp_path))
    session.permissions = PermissionManager(str(tmp_path))
    session.tools = ToolRegistry([])
    session.messages = [{"role": "system", "content": "legacy embedded project rules", "_prompt_format": 1}]
    session._rebuild_system_prompt()

    assert session.messages[0]["_prompt_format"] == PROMPT_FORMAT_VERSION
    assert "legacy embedded project rules" not in session.messages[0]["content"]


def test_mock_commands_still_use_human_input_after_instruction_injection(tmp_path):
    from pepsicode.llm.mock_model import MockModelAdapter

    (tmp_path / "AGENTS.md").write_text("/write should not become a user command", encoding="utf-8")
    messages = [{"role": "user", "content": "/ls"}]
    _messages_for_model(messages, None, cwd=str(tmp_path))

    step = MockModelAdapter().next(messages)

    assert step.type == "tool_calls"
    assert step.calls[0]["toolName"] == "list_files"
