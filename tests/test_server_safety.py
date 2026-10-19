import asyncio
import json
import threading
from unittest.mock import Mock

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from pepsicode.core.session import create_new_session, save_session
from pepsicode.permissions import PermissionManager
from pepsicode.server import app
from pepsicode.server.security import authenticate
from pepsicode.tooling import ToolRegistry


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    return tmp_path


class AuthSocket:
    def __init__(self, frame, origin=None):
        self.frame = frame
        self.request_headers = {} if origin is None else {"Origin": origin}
        self.sent = []
        self.closed = None

    async def recv(self):
        return self.frame

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, **options):
        self.closed = options


def auth_frame(token="test-token"):
    return json.dumps({"kind": "request", "id": 0, "method": "connection/auth", "params": {"token": token}})


@pytest.mark.parametrize(
    "frame,origin",
    [
        (auth_frame("wrong-token"), None),
        (auth_frame(), "https://untrusted.example"),
        (json.dumps({"kind": "request", "method": "session/list"}), None),
        ("[]", None),
        ("invalid", None),
        (b"binary", None),
        ("x" * 8193, None),
    ],
)
def test_unauthenticated_clients_are_rejected_before_session_creation(frame, origin, monkeypatch):
    created = Mock(side_effect=AssertionError("session must not be created"))
    monkeypatch.setattr(app, "ClientSession", created)
    socket = AuthSocket(frame, origin)
    asyncio.run(app.PepsiCodeServer(token="test-token").handle_ws(socket))
    assert socket.closed["code"] == 1008
    assert not socket.sent and not created.called


@pytest.mark.parametrize("origin", [None, "null", "file://", "http://localhost:5173"])
def test_authentication_accepts_only_explicit_trusted_origins_and_token(origin):
    socket = AuthSocket(auth_frame(), origin)
    assert asyncio.run(authenticate(socket, "test-token", {"null", "file://", "http://localhost:5173"}))
    assert socket.sent == [{"kind": "response", "id": 0, "result": {"authenticated": True}}]


def test_real_socket_cannot_read_session_metadata_without_authentication(isolated_sessions):
    root = isolated_sessions
    session = create_new_session(str(root))
    session.messages = [{"role": "user", "content": "private fixture"}]
    save_session(session)

    async def scenario():
        server = app.PepsiCodeServer(token="test-token")
        async with websockets.serve(server.handle_ws, "127.0.0.1", 0) as listener:
            uri = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
            async with websockets.connect(uri, origin="https://untrusted.example") as client:
                with pytest.raises(ConnectionClosed):
                    await client.send(auth_frame())
                    await client.recv()
            async with websockets.connect(uri) as client:
                await client.send(json.dumps({"kind": "request", "id": 1, "method": "session/list"}))
                with pytest.raises(ConnectionClosed):
                    await client.recv()
            async with websockets.connect(uri, origin="null") as client:
                await client.send(auth_frame())
                assert json.loads(await client.recv())["result"]["authenticated"]
                assert json.loads(await client.recv())["event"] == "connection/ready"
                await client.send(
                    json.dumps(
                        {"kind": "request", "id": 2, "method": "session/list", "params": {"workspace": str(root)}}
                    )
                )
                result = json.loads(await client.recv())
                assert result["result"]["sessions"][0]["first_message"] == "private fixture"
        assert not server._sessions

    asyncio.run(scenario())


def test_cancel_immediately_releases_approval_and_late_response_is_ignored(isolated_sessions):
    async def scenario():
        emitted = asyncio.Event()

        async def emit(name, data):
            emitted.set()

        session = app.ClientSession(str(isolated_sessions))
        bridge = app.RemotePermissionBridge(asyncio.get_running_loop(), emit, session._cancellation_event)
        session._bridge = bridge
        worker = asyncio.create_task(asyncio.to_thread(bridge._raw_prompt, {"kind": "command"}))
        session._current_turn_task = worker
        await asyncio.wait_for(emitted.wait(), 2)
        prompt = next(iter(bridge._pending))
        result = await app.PepsiCodeServer()._dispatch(
            "turn/cancel", {}, session, bridge, emit, asyncio.get_running_loop()
        )
        assert result["cancelled"]
        assert (await asyncio.wait_for(worker, 0.5))["decision"] == "deny_once"
        assert not bridge.resolve(prompt, {"decision": "allow_once"})
        assert not bridge._pending
        bridge.cancel_pending(close=True)
        assert bridge._raw_prompt({"kind": "command"})["decision"] == "deny_once"

    asyncio.run(scenario())


def test_disconnect_stops_worker_before_disposing_tools_and_prevents_later_writes(isolated_sessions, monkeypatch):
    root = isolated_sessions
    started = threading.Event()
    ended = threading.Event()
    marker = root / "must-not-be-written"
    sessions = []
    original = app.ClientSession

    class Session(original):
        def __init__(self, cwd):
            super().__init__(str(root))
            self.tools = ToolRegistry([], disposer=lambda: ended.is_set() or pytest.fail("disposed before worker exit"))
            self.tools.dispose = Mock(wraps=self.tools.dispose)
            self.permissions = PermissionManager(str(root), mode="full_access")
            self.model = object()
            self.runtime = {"verificationMode": "off"}
            self.session_data = create_new_session(str(root))
            self._rebuild_system_prompt = lambda: None
            sessions.append(self)

    def runner(**options):
        started.set()
        try:
            if not options["cancellation_event"].wait(2):
                marker.touch()
            return [*options["messages"], {"role": "assistant", "content": "cancelled"}]
        finally:
            ended.set()

    monkeypatch.setattr(app, "ClientSession", Session)
    monkeypatch.setattr(app, "run_agent_turn_stream", runner)

    class Socket(AuthSocket):
        count = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.count += 1
            if self.count == 1:
                return json.dumps({"kind": "request", "id": 1, "method": "turn/run", "params": {"input": "edit"}})
            assert await asyncio.to_thread(started.wait, 2)
            raise StopAsyncIteration

    async def scenario():
        server = app.PepsiCodeServer(token="test-token")
        await server.handle_ws(Socket(auth_frame()))
        assert ended.is_set() and not marker.exists()
        assert sessions[0]._cancellation_event.is_set()
        sessions[0].tools.dispose.assert_called_once()
        assert not sessions[0]._worker_active.is_set() and not server._sessions

    asyncio.run(scenario())


def test_cancel_before_worker_start_is_not_erased(isolated_sessions, monkeypatch):
    async def scenario():
        server = app.PepsiCodeServer()
        session = app.ClientSession(str(isolated_sessions))
        session.model = object()
        session.permissions = PermissionManager(str(isolated_sessions), mode="full_access")
        runner = Mock(side_effect=AssertionError("worker must not start"))
        monkeypatch.setattr(app, "run_agent_turn_stream", runner)
        socket = AuthSocket(auth_frame())

        async def emit(name, data):
            pass

        loop = asyncio.get_running_loop()
        await server._handle_request(
            {"id": 1, "method": "turn/run", "params": {"input": "edit"}}, session, None, emit, socket, loop
        )
        assert (await server._dispatch("turn/cancel", {}, session, None, emit, loop))["cancelled"]
        await asyncio.gather(*list(session._background_tasks))
        assert not runner.called
        assert socket.sent[-1]["result"]["status"] == "cancelled"

    asyncio.run(scenario())


def test_cleanup_waits_for_executor_thread_even_if_async_wrapper_has_finished(isolated_sessions):
    async def scenario():
        session = app.ClientSession(str(isolated_sessions))
        session._worker_active.set()
        thread_exited = threading.Event()
        may_exit = threading.Event()

        def worker():
            session._cancellation_event.wait(2)
            may_exit.wait(2)
            thread_exited.set()
            session._worker_active.clear()

        thread = threading.Thread(target=worker)
        thread.start()
        session.tools = Mock()
        session.tools.dispose.side_effect = lambda: thread_exited.is_set() or pytest.fail(
            "tools disposed while worker still active"
        )

        async def emit(name, data):
            pass

        bridge = app.RemotePermissionBridge(asyncio.get_running_loop(), emit, session._cancellation_event)
        server = app.PepsiCodeServer()
        server._sessions.add(session)
        close = asyncio.create_task(server._close_session(session, bridge))
        await asyncio.sleep(0.04)
        assert not session.tools.dispose.called
        may_exit.set()
        await asyncio.wait_for(close, 1)
        thread.join(timeout=1)
        session.tools.dispose.assert_called_once()
        assert not server._sessions

    asyncio.run(scenario())
