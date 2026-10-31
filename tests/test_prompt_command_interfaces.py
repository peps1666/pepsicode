import asyncio
import io
import threading
from pathlib import Path

import pytest

from pepsicode.core.session import create_new_session, load_session, save_session
from pepsicode.llm.mock_model import MockModelAdapter
from pepsicode.permissions import PermissionManager
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.server.protocol import serialize_session_history
from pepsicode.tooling import ToolRegistry
from pepsicode.tools.write_file import write_file_tool
from pepsicode.tui import app
from pepsicode.tui.input_parser import PasteEvent, TerminalInputDecoder
from pepsicode.types import StreamToken


@pytest.fixture
def project(tmp_path, monkeypatch):
    root, home = tmp_path / "project", tmp_path / "home"
    directory = root / ".pepsi-code" / "commands"
    directory.mkdir(parents=True)
    home.mkdir()
    (directory / "review.md").write_text(
        "---\nargument-hint: '[focus]'\n---\nReview ${1:-correctness}.", encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(app, "save_history_entries", lambda entries: None)
    monkeypatch.setattr("pepsicode.config.PEPSI_CODE_DIR", home)
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", home)
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", home / "sessions")
    return root


def dispatch(args, state, value):
    for event in TerminalInputDecoder().feed(value):
        app._handle_event(args, state, event, lambda: None, threading.Event(), {})


class CapturingModel(MockModelAdapter):
    def __init__(self):
        self.requests = []

    def next_stream(self, messages):
        self.requests.append([dict(message) for message in messages])
        yield StreamToken(type="text", content="done")
        yield StreamToken(type="done")


def terminal(project, model):
    args = app.TtyAppArgs(
        runtime={"verificationMode": "off"},
        model=model,
        tools=ToolRegistry([]),
        messages=[{"role": "system", "content": "test"}],
        cwd=str(project),
        permissions=PermissionManager(str(project), mode="full_access"),
    )
    return args, app.ScreenState()


def test_real_terminal_input_completes_command_without_inserting_hints_or_swallowing_numeric_argument(project):
    model = CapturingModel()
    args, state = terminal(project, model)
    dispatch(args, state, "/rev")
    dispatch(args, state, "\t")
    assert state.input == "/review "
    dispatch(args, state, "2")
    assert state.input == "/review 2"
    dispatch(args, state, "\r")
    state.agent_thread.join(2)
    assert not state.agent_thread.is_alive()
    app._collect_agent_result(args, state)
    human = next(m for m in reversed(model.requests[0]) if m.get("role") == "user" and not m.get("internal"))
    assert human["content"] == "Review 2."
    assert human["displayContent"] == "/review 2"
    assert state.transcript[0].body == "/review 2"


def test_terminal_image_command_reaches_model_as_image(project):
    (project / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
    model = CapturingModel()
    args, state = terminal(project, model)
    app._handle_input(args, state, lambda: None, "/image screenshot.png 解释这张图")
    state.agent_thread.join(5)
    assert not state.agent_thread.is_alive()
    assert model.requests, [(entry.kind, entry.body) for entry in state.transcript]
    human = next(m for m in reversed(model.requests[0]) if m.get("role") == "user" and not m.get("internal"))
    assert human["content"] == "解释这张图"
    assert human["images"][0]["media_type"] == "image/png"
    assert human["displayContent"] == "/image screenshot.png 解释这张图"
    assert state.transcript[0].body.endswith("[图片 ×1]")
    app._snapshot_session(args, state)
    save_session(state.session)
    restored = load_session(state.session.session_id)
    assert any(message.get("images") == human["images"] for message in restored.messages)


def test_ctrl_v_attaches_clipboard_image_to_draft(project, monkeypatch, capsys):
    payload = {"media_type": "image/png", "data": "aGVsbG8="}
    monkeypatch.setattr(app, "read_clipboard_image", lambda: payload)
    model = CapturingModel()
    args, state = terminal(project, model)
    dispatch(args, state, "\x16")
    assert state.pending_images == [payload]
    assert "[图片 ×1]" in "\n".join(app._render_stream_input(state, 80))
    assert "[图片 ×1]" in app._render_prompt_panel(state, str(project))
    args.runtime["model"] = "demo"
    app._render_home_screen(args, state, 80, 24)
    assert "[图片 ×1]" in capsys.readouterr().out
    assert not model.requests
    dispatch(args, state, "看一下这张图\r")
    state.agent_thread.join(5)
    assert not state.agent_thread.is_alive()
    human = next(m for m in reversed(model.requests[0]) if m.get("role") == "user" and not m.get("internal"))
    assert human["content"] == "看一下这张图"
    assert human["images"] == [payload]
    assert state.pending_images == []


def test_ctrl_g_removes_latest_image_without_erasing_draft(project, monkeypatch):
    images = iter(
        [
            {"media_type": "image/png", "data": "Zmlyc3Q="},
            {"media_type": "image/png", "data": "c2Vjb25k"},
        ]
    )
    monkeypatch.setattr(app, "read_clipboard_image", lambda: next(images))
    args, state = terminal(project, CapturingModel())
    dispatch(args, state, "问题\x16\x16")
    assert len(state.pending_images) == 2
    dispatch(args, state, "\x07")
    assert state.input == "问题"
    assert [image["data"] for image in state.pending_images] == ["Zmlyc3Q="]
    assert "[图片 ×1]" in "\n".join(app._render_stream_input(state, 80))

    dispatch(args, state, "\x01\x7f")
    assert state.input == "问题"
    assert state.pending_images == []


def test_paste_image_command_attaches_without_sending(project, monkeypatch):
    payload = {"media_type": "image/png", "data": "aGVsbG8="}
    monkeypatch.setattr(app, "read_clipboard_image", lambda: payload)
    model = CapturingModel()
    args, state = terminal(project, model)
    dispatch(args, state, "/paste-image\r")
    assert state.pending_images == [payload]
    assert not model.requests


def test_pasted_local_image_path_attaches_bytes_instead_of_text(project):
    image = project / "copied.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"copied")
    model = CapturingModel()
    args, state = terminal(project, model)
    dispatch(args, state, "\x1b[200~copied.png\x1b[201~")
    assert state.input == ""
    assert len(state.pending_images) == 1
    dispatch(args, state, "这是什么？\r")
    state.agent_thread.join(2)
    human = next(m for m in reversed(model.requests[0]) if m.get("role") == "user" and not m.get("internal"))
    assert human["content"] == "这是什么？"
    assert human["images"][0]["data"]


def test_plain_image_path_is_not_sent_as_path_only(project):
    (project / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"photo")
    model = CapturingModel()
    args, state = terminal(project, model)
    app._handle_input(args, state, lambda: None, "photo.png")
    state.agent_thread.join(2)
    human = next(m for m in reversed(model.requests[0]) if m.get("role") == "user" and not m.get("internal"))
    assert human["content"] != "photo.png"
    assert human["images"][0]["media_type"] == "image/png"


def test_deepseek_pro_image_is_rejected_without_sending(project):
    model = CapturingModel()
    args, state = terminal(project, model)
    args.runtime = {"provider": "deepseek", "model": "deepseek-v4-pro"}
    state.pending_images.append({"media_type": "image/png", "data": "aGVsbG8="})
    app._handle_input(args, state, lambda: None, "看这张图")
    assert not model.requests
    assert state.pending_images
    assert any("deepseek-flash" in entry.body for entry in state.transcript)


@pytest.mark.parametrize("cancel", [False, True])
def test_queued_command_keeps_expansion_snapshot_and_returns_original_text_on_stop(project, cancel):
    started, release = threading.Event(), threading.Event()

    class BlockingModel(CapturingModel):
        def next_stream(self, messages):
            if not self.requests:
                started.set()
                assert release.wait(3)
            yield from super().next_stream(messages)

    model = BlockingModel()
    args, state = terminal(project, model)
    try:
        app._handle_input(args, state, lambda: None, "initial task")
        assert started.wait(2)
        app._handle_event(args, state, PasteEvent("/review security"), lambda: None, threading.Event(), {})
        dispatch(args, state, b"\x1b\r")
        assert state.pending_inputs[0]["content"] == "Review security."
        assert state.pending_inputs[0]["display_content"] == "/review security"
        (project / ".pepsi-code" / "commands" / "review.md").write_text("changed", encoding="utf-8")
        state.input = "draft"
        if cancel:
            state.cancellation_event.set()
        release.set()
        state.agent_thread.join(3)
        assert not state.agent_thread.is_alive()
        app._collect_agent_result(args, state)
        if cancel:
            assert state.input == "/review security\n\ndraft"
            assert len(model.requests) == 1
        else:
            human = next(m for m in reversed(model.requests[-1]) if m.get("role") == "user" and not m.get("internal"))
            assert human["content"] == "Review security."
            assert human["commandPath"].endswith("review.md")
            assert [entry.body for entry in state.transcript if entry.kind == "user"][-1] == "/review security"
    finally:
        state.cancellation_event.set()
        release.set()
        state.agent_thread.join(3)


def test_command_metadata_cannot_grant_write_access_in_plan_mode(project):
    (project / ".pepsi-code" / "commands" / "review.md").write_text(
        "---\nallowed-tools: write_file\n---\nWrite a file.", encoding="utf-8"
    )

    class Model(CapturingModel):
        def next_stream(self, messages):
            if not self.requests:
                self.requests.append(list(messages))
                yield StreamToken(
                    type="tool_use",
                    tool_name="write_file",
                    tool_id="write",
                    tool_input_partial='{"path":"protected.txt","content":"forbidden"}',
                )
                yield StreamToken(type="done")
            else:
                yield from super().next_stream(messages)

    args, state = terminal(project, Model())
    args.tools = ToolRegistry([write_file_tool])
    args.permissions.enter_plan_mode()
    app._handle_input(args, state, lambda: None, "/review")
    state.agent_thread.join(3)
    assert not state.agent_thread.is_alive()
    app._collect_agent_result(args, state)
    assert not (project / "protected.txt").exists()
    assert any(message.get("role") == "tool_result" and message.get("isError") for message in args.messages)


def test_server_lists_commands_expands_prompts_and_persists_the_exact_payload(project):
    events = []

    async def scenario():
        server = PepsiCodeServer()
        session = ClientSession(str(project))
        session.model = CapturingModel()
        session.tools = ToolRegistry([])
        session.permissions = PermissionManager(str(project), mode="full_access")
        session.runtime = {"verificationMode": "off"}
        session.session_data = create_new_session(str(project))
        loop = asyncio.get_running_loop()

        async def emit(name, data):
            events.append((name, data))

        listed = await server._dispatch("commands/list", {}, session, None, emit, loop)
        assert any(item["name"] == "/review" and item["source"] == "project" for item in listed["commands"])
        assert all("content" not in item and "body" not in item for item in listed["commands"])
        await server._run_turn({"input": "/review security"}, session, emit, loop)
        persisted = load_session(session.session_data.session_id)
        user = next(message for message in persisted.messages if message["role"] == "user")
        assert user["content"] == "Review security." and user["displayContent"] == "/review security"
        assert serialize_session_history(persisted.messages)[0]["content"] == "/review security"
        assert len([name for name, _ in events if name == "turn/end"]) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ["/commands", "/commands reload", "/help", "/unknown-command"])
def test_desktop_local_commands_always_settle_without_calling_the_model(project, raw):
    async def scenario():
        server = PepsiCodeServer()
        session = ClientSession(str(project))
        session.model = CapturingModel()
        session.permissions = PermissionManager(str(project))
        events = []

        async def emit(name, data):
            events.append((name, data))

        await server._run_turn({"input": raw}, session, emit, asyncio.get_running_loop())
        assert not session.model.requests
        assert len([name for name, _ in events if name == "turn/end"]) == 1
        assert any(name == "message/end" for name, _ in events)

    asyncio.run(scenario())


@pytest.mark.parametrize("branching", [False, True])
def test_pipe_cli_expands_custom_commands_before_the_agent_turn(project, monkeypatch, branching):
    from pepsicode import main
    from pepsicode.context import context_manager, memory
    from pepsicode.hooks import HookEngine

    calls = []
    monkeypatch.chdir(project)
    monkeypatch.setattr(main.sys, "argv", ["pepsicode"])
    commands = "/review security\n/retry\n/clone\n/exit\n" if branching else "/review security\n/exit\n"
    monkeypatch.setattr(main.sys, "stdin", io.StringIO(commands))
    monkeypatch.setenv("PEPSI_CODE_MODEL_MODE", "mock")
    monkeypatch.setattr(main, "load_runtime_config", lambda cwd: None)
    monkeypatch.setattr(main, "create_default_tool_registry", lambda *args, **kwargs: ToolRegistry([]))
    monkeypatch.setattr(main, "create_hook_engine", lambda *args: HookEngine())
    monkeypatch.setattr(main, "save_history_entries", lambda entries: None)
    monkeypatch.setattr(main, "load_history_entries", lambda: [])
    monkeypatch.setattr(context_manager, "save_context_state", lambda value: None)
    monkeypatch.setattr(memory, "create_memory_manager", lambda cwd: None)
    monkeypatch.setattr("pepsicode.logging_config.setup_logging", lambda **kwargs: None)

    def turn(**options):
        calls.append(options["messages"][-1])
        return [*options["messages"], {"role": "assistant", "content": "done"}]

    monkeypatch.setattr(main, "run_verified_turn", turn)
    main.main()
    assert calls[0]["content"] == "Review security."
    assert calls[0]["displayContent"] == "/review security"
    from pepsicode.core.session import list_sessions

    saved = list_sessions(str(project))
    assert len(saved) == (2 if branching else 1)
    if branching:
        assert calls[1]["content"] == calls[0]["content"]
        assert sorted(len(load_session(item.session_id).tree["nodes"]) for item in saved) == [2, 4]


def test_large_custom_commands_can_be_queued_in_terminal_and_desktop(project):
    from pepsicode.cli.cli_commands import expand_prompt_command
    from pepsicode.input_limits import MAX_INPUT_CHARACTERS

    body = "a" * (MAX_INPUT_CHARACTERS + 100)
    (project / ".pepsi-code" / "commands" / "team-audit.md").write_text(body, encoding="utf-8")
    assert expand_prompt_command("/team-audit", str(project))["content"] == body
    args, state = terminal(project, CapturingModel())
    state.input_queue.start()
    app._enqueue_tty_input(args, state, "/team-audit", "follow_up")
    item = state.input_queue.next_or_close()[0]
    assert item.content == body and item.display_content == "/team-audit"

    async def scenario():
        client = ClientSession(str(project))
        from pepsicode.core.session import create_new_session

        client.session_data = create_new_session(str(project))
        client.input_queue.bind_session(client.session_data)
        client.input_queue.start()
        result = await PepsiCodeServer()._dispatch(
            "turn/enqueue",
            {"input": "/team-audit", "mode": "follow_up"},
            client,
            None,
            None,
            asyncio.get_running_loop(),
        )
        item = client.input_queue.next_or_close()[0]
        assert item.id == result["id"] and item.content == body
        assert item.display_content == "/team-audit"

    asyncio.run(scenario())
