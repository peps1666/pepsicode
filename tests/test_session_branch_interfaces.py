import asyncio
import json

import pytest

from pepsicode.core.session import create_new_session, load_session
from pepsicode.core.session_tree import tree_view
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.permissions import PermissionManager
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolRegistry
from pepsicode.tui import app
from pepsicode.types import StreamToken


class Model(MockModelAdapter):
    def __init__(self):
        self.requests = []

    def next_stream(self, messages):
        self.requests.append([dict(message) for message in messages])
        yield StreamToken(type="text", content=f"answer {len(self.requests)}")
        yield StreamToken(type="done")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")
    monkeypatch.setattr(app, "save_history_entries", lambda entries: None)
    return tmp_path


def client(project):
    session = ClientSession(str(project))
    session.model = Model()
    session.tools = ToolRegistry([])
    session.permissions = PermissionManager(str(project), mode="full_access")
    session.runtime = {"verificationMode": "off"}
    session.session_data = create_new_session(str(project))
    return session


def test_desktop_fork_and_clone_keep_source_and_files_intact(project):
    marker = project / "code.py"
    marker.write_text("present disk state", encoding="utf-8")

    async def scenario():
        server, session, events = PepsiCodeServer(), client(project), []

        async def emit(name, data):
            events.append((name, data))

        loop = asyncio.get_running_loop()
        await server._run_turn({"input": "first task"}, session, emit, loop)
        await server._run_turn({"input": "second task"}, session, emit, loop)
        source_id = session.session_data.session_id
        target = [node for node in tree_view(session.session_data)["nodes"] if node["role"] == "user"][-1]
        result = await server._dispatch("session/fork", {"entry_id": target["id"]}, session, None, emit, loop)
        assert result["draft"] == "second task"
        assert result["session_id"] != source_id
        assert [item["content"] for item in result["history"]] == ["first task", "answer 1"]
        original = load_session(source_id)
        assert original.messages[-1]["content"] == "answer 2"
        assert len(original.tree["nodes"]) == 4
        fork_id = session.session_data.session_id
        clone = await server._dispatch("session/clone", {}, session, None, emit, loop)
        assert clone["tree"]["parent_session_id"] == fork_id
        assert clone["history"] == result["history"] and clone["draft"] == ""
        assert marker.read_text(encoding="utf-8") == "present disk state"
        assert session.permissions.access_mode.value == "full_access"

    asyncio.run(scenario())


def test_desktop_regeneration_streams_new_answer_and_preserves_old_branch(project):
    async def scenario():
        server, session, events, responses = PepsiCodeServer(), client(project), [], []

        async def emit(name, data):
            events.append((name, data))

        class Socket:
            async def send(self, raw):
                responses.append(json.loads(raw))

        loop = asyncio.get_running_loop()
        await server._run_turn({"input": "initial task"}, session, emit, loop)
        original = list(session.session_data.tree["nodes"])
        events.clear()
        await server._handle_request(
            {"id": 7, "method": "turn/regenerate", "params": {"entry_id": original[0]["id"]}},
            session,
            None,
            emit,
            Socket(),
            loop,
        )
        await asyncio.wait_for(asyncio.gather(*list(session._background_tasks)), 3)
        assert not responses[-1].get("error")
        assert len(session.session_data.tree["nodes"]) == 4
        assert session.session_data.tree["nodes"][:2] == original
        assert session.model.requests[-1][-1]["content"] == "initial task"
        assert session.messages[-1]["content"] == "answer 2"
        assert len([name for name, _ in events if name == "turn/end"]) == 1
        assert any(name == "session/view" and data["history"][-1]["content"] == "answer 2" for name, data in events)
        await server._dispatch("session/checkout", {"entry_id": original[1]["id"]}, session, None, emit, loop)
        assert session.messages[-1]["content"] == "answer 1"

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["session/checkout", "session/fork", "session/clone"])
def test_busy_desktop_rejects_branch_changes_without_changing_history(project, method):
    async def scenario():
        session, server = client(project), PepsiCodeServer()

        async def emit(*args):
            pass

        async with session._turn_lock:
            with pytest.raises(ValueError, match="finish"):
                await server._dispatch(method, {}, session, None, emit, asyncio.get_running_loop())
        assert not session.session_data.tree

    asyncio.run(scenario())


def test_terminal_tree_fork_clone_and_retry_are_actual_commands(project):
    model = Model()
    args = app.TtyAppArgs(
        runtime={"verificationMode": "off"},
        model=model,
        tools=ToolRegistry([]),
        messages=[{"role": "system", "content": "test"}],
        cwd=str(project),
        permissions=PermissionManager(str(project), mode="full_access"),
    )
    state = app.ScreenState()
    app._handle_input(args, state, lambda: None, "first task")
    state.agent_thread.join(3)
    app._collect_agent_result(args, state)
    source_id = state.session.session_id
    original_nodes = list(state.session.tree["nodes"])
    app._handle_input(args, state, lambda: None, "/retry")
    state.agent_thread.join(3)
    app._collect_agent_result(args, state)
    assert args.messages[-1]["content"] == "answer 2"
    assert state.session.tree["nodes"][:2] == original_nodes
    app._handle_input(args, state, lambda: None, f"/tree {original_nodes[1]['id'][:8]}")
    assert args.messages[-1]["content"] == "answer 1"
    app._handle_input(args, state, lambda: None, "/clone")
    assert state.session.session_id != source_id
    assert state.session.parent_session_id == source_id
    clone_id = state.session.session_id
    app._handle_input(args, state, lambda: None, f"/fork {original_nodes[0]['id'][:8]}")
    assert state.input == "first task"
    assert state.session.session_id != clone_id and state.session.parent_session_id == clone_id
    assert all(message["role"] == "system" for message in args.messages)
    assert load_session(source_id).tree["nodes"][:2] == original_nodes
