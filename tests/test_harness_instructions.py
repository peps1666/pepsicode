import pytest

from pepsicode.context.prompt import build_session_prompt
from pepsicode.prompts import load_harness_instructions
from pepsicode.types import AgentStep


def test_builtin_policy_is_independent_of_workspace_and_appears_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("workspace-only rule", encoding="utf-8")
    fake = tmp_path / "pepsicode" / "prompts"
    fake.mkdir(parents=True)
    (fake / "AGENTS.md").write_text("fake builtin", encoding="utf-8")
    policy = load_harness_instructions()
    prompt = build_session_prompt(str(tmp_path))
    assert prompt.count(policy) == 1
    assert "workspace-only rule" in prompt
    assert "fake builtin" not in prompt

    dynamic = []
    managed = build_session_prompt(str(tmp_path), dynamic_sections=dynamic)
    assert managed.count(policy) == 1
    assert policy not in "\n".join(dynamic)
    assert "workspace-only rule" not in managed
    assert str(tmp_path) in "\n".join(dynamic)


@pytest.mark.parametrize(
    "content", [None, b" \n", b"\xff", b"x" * (32 * 1024 + 1)], ids=["missing", "empty", "invalid-utf8", "oversized"]
)
def test_invalid_required_policy_fails_visibly_without_workspace_fallback(tmp_path, monkeypatch, content):
    if content is not None:
        (tmp_path / "AGENTS.md").write_bytes(content)
    monkeypatch.setattr("pepsicode.prompts.files", lambda package: tmp_path)
    with pytest.raises(RuntimeError, match="Cannot load bundled PepsiCode Harness instructions"):
        build_session_prompt(str(tmp_path), dynamic_sections=[])


@pytest.mark.parametrize("streaming", [False, True])
def test_main_model_receives_policy_and_runtime_context(tmp_path, streaming):
    from pepsicode.core.agent_loop import run_agent_turn, run_agent_turn_stream
    from pepsicode.permissions import PermissionManager
    from pepsicode.server.app import ClientSession
    from pepsicode.tooling import ToolRegistry
    from pepsicode.types import StreamToken

    session = ClientSession(str(tmp_path))
    session.permissions = PermissionManager(str(tmp_path))
    session.tools = ToolRegistry([])
    session._rebuild_system_prompt()
    policy = load_harness_instructions()
    requests = []

    class Model:
        def next(self, messages):
            requests.append([dict(message) for message in messages])
            return AgentStep(type="assistant", content="<final>done")

        def next_stream(self, messages):
            self.next(messages)
            yield StreamToken(type="text", content="<final>done")
            yield StreamToken(type="done")

    runner = run_agent_turn_stream if streaming else run_agent_turn
    runner(model=Model(), tools=session.tools, messages=session.messages, cwd=str(tmp_path), max_steps=2)
    assert requests[0][0]["content"].count(policy) == 1
    assert any("## Environment" in message.get("content", "") for message in requests[0][1:])


def test_subagent_receives_shared_policy_and_keeps_role_and_tool_restrictions(tmp_path):
    from pepsicode.tooling import ToolContext
    from pepsicode.tools.task import create_task_tool

    requests, available_tools = [], []

    class Model:
        def next(self, messages):
            requests.append([dict(message) for message in messages])
            return AgentStep(type="assistant", content="<final>Found the requested information")

    def factory(registry):
        available_tools.extend(tool.name for tool in registry.list())
        return Model()

    tool = create_task_tool(str(tmp_path), None, model_factory=factory)
    parsed = tool.validator({"agent_type": "explore", "task": "Inspect the project"})
    result = tool.run(parsed, ToolContext(cwd=str(tmp_path)))
    assert result.ok
    system = requests[0][0]["content"]
    assert system.count(load_harness_instructions()) == 1
    assert "## Sub-agent role" in system
    assert "read_file" in available_tools
    assert "write_file" not in available_tools and "ask_user" not in available_tools
