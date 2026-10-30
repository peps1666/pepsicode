import asyncio
import json
import threading

import pytest

from pepsicode.core.session import create_new_session
from pepsicode.permissions import PermissionManager
from pepsicode.server.app import ClientSession, PepsiCodeServer
from pepsicode.tooling import ToolRegistry
from pepsicode.types import StreamToken


@pytest.mark.parametrize("action", ["complete", "cancel", "retrieve"])
def test_busy_server_admits_input_without_starting_concurrent_workers(tmp_path, monkeypatch, action):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")
    started, release = threading.Event(), threading.Event()
    requests, events, responses = [], [], []

    class Model:
        def next_stream(self, messages):
            requests.append([dict(message) for message in messages])
            if len(requests) == 1:
                started.set()
                assert release.wait(3), "test did not release model"
            yield StreamToken(type="text", content="done")
            yield StreamToken(type="done")

    class Socket:
        async def send(self, payload):
            responses.append(json.loads(payload))

    async def scenario():
        server = PepsiCodeServer()
        session = ClientSession(str(tmp_path))
        session.model = Model()
        session.tools = ToolRegistry([])
        session.runtime = {"verificationMode": "off", "model": "test"}
        session.permissions = PermissionManager(str(tmp_path), mode="full_access")
        session.session_data = create_new_session(str(tmp_path))
        loop = asyncio.get_running_loop()

        async def emit(name, data):
            events.append((name, data))

        try:
            await server._handle_request(
                {"id": 1, "method": "turn/run", "params": {"input": "initial task"}},
                session,
                None,
                emit,
                Socket(),
                loop,
            )
            assert await asyncio.to_thread(started.wait, 2)
            steering = await server._dispatch(
                "turn/enqueue", {"input": "adjust current task"}, session, None, emit, loop
            )
            await server._dispatch(
                "turn/enqueue", {"input": "next task", "mode": "follow_up"}, session, None, emit, loop
            )
            assert len(requests) == 1
            if action == "cancel":
                assert (await server._dispatch("turn/cancel", {}, session, None, emit, loop))["cancelled"]
                with pytest.raises(ValueError, match="stopping"):
                    await server._dispatch("turn/enqueue", {"input": "too late"}, session, None, emit, loop)
            if action == "retrieve":
                assert (await server._dispatch("turn/dequeue", {"id": steering["id"]}, session, None, emit, loop))[
                    "removed"
                ]
            release.set()
            await asyncio.wait_for(asyncio.gather(*list(session._background_tasks)), 4)
            # Flush the queue events scheduled from the executor thread.
            await asyncio.sleep(0)
            delivered = [
                item["content"] for name, data in events if name == "input/queue" for item in data["delivered"]
            ]
            returned = [item["content"] for name, data in events if name == "input/queue" for item in data["returned"]]
            if action == "cancel":
                assert len(requests) == 1 and delivered == []
                assert returned == ["adjust current task", "next task"]
            else:
                assert delivered == (["adjust current task", "next task"] if action == "complete" else ["next task"])
                assert len(requests) == (3 if action == "complete" else 2)
                user_text = [
                    message["content"]
                    for message in session.messages
                    if message["role"] == "user" and not message.get("internal")
                ]
                assert user_text == ["initial task", *delivered]
            assert not session.input_queue.accepting
            assert len([name for name, _ in events if name == "turn/end"]) == 1
            assert responses[-1].get("error") is None
            assert responses[-1]["result"]["status"] == ("cancelled" if action == "cancel" else "completed")
            with pytest.raises(ValueError, match="finishing|stopping"):
                await server._dispatch("turn/enqueue", {"input": "after settlement"}, session, None, emit, loop)
        finally:
            release.set()
            session._cancellation_event.set()
            if session._background_tasks:
                await asyncio.wait_for(asyncio.gather(*list(session._background_tasks)), 4)

    asyncio.run(scenario())
