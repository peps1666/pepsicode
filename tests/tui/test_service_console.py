"""Attach console regression tests (M3 §19.1 TUI attach entry).

Unit tests drive :class:`~pepsicode.tui.service_console.ServiceConsole` with a
fake connection; the integration test runs the real ``PepsiCodeServer`` behind
a real loopback WebSocket and verifies the TUI-level B08 behaviour: the
console detaches, the running turn is not cancelled, and a reconnect reads the
result.
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from pepsicode.core.session import create_new_session
from pepsicode.permissions import PermissionManager
from pepsicode.server import app
from pepsicode.service.host import ServiceHost
from pepsicode.service.policy import save_policy
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.service_console import (
    ServiceConnection,
    ServiceConsole,
    ServiceRequestError,
    run_service_console,
)
from pepsicode.types import StreamToken

BANNER = {"service": True, "generation": "gen-1", "execution_mode": "service", "cwd": "/w"}


class FakeConnection:
    """Duck-typed stand-in for ServiceConnection (no network)."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.requests = []
        self.event_handler = None
        self.closed = False

    def emit(self, name, data):
        handler = self.event_handler
        if handler is not None:
            handler(name, data)

    def request(self, method, params=None, timeout=30.0):
        self.requests.append((method, params))
        outcome = self.responses.get(method)
        if callable(outcome):
            outcome = outcome(params)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or {}

    def close(self):
        self.closed = True


def make_console(lines, responses=None, banner=BANNER, session_id=None):
    connection = FakeConnection(responses)
    output = io.StringIO()
    console = ServiceConsole(
        connection,
        session_id=session_id,
        input_stream=io.StringIO("".join(f"{line}\n" for line in lines)),
        output=output,
    )
    if banner is not None:
        console.handle_event("connection/ready", banner)
    return console, connection, output


DETACH_OK = {"detached": True}
NOTIFY_EMPTY = {"notifications": [], "cursor": 0}


def test_refuses_non_service_target():
    console, connection, output = make_console(["/detach"], banner={"service": False, "cwd": "/w"})
    code = console.run()
    assert code == 1
    assert "不是常驻服务" in output.getvalue()
    assert connection.closed


def test_banner_attach_replay_and_detach():
    view = {
        "session_id": "s1",
        "history": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "done"},
        ],
        "event_cursor": 7,
    }
    responses = {
        "notifications/fetch": {"notifications": [{"id": "n1"}], "cursor": 1},
        "session/list": {
            "sessions": [
                {
                    "session_id": "s1",
                    "updated_at": "2026-11-14T00:00:00",
                    "message_count": 2,
                    "first_message": "你好",
                }
            ]
        },
        "session/resume": view,
        "session/events": {
            "events": [
                {"event": "service/notice", "data": {"kind": "client_detached"}},
                {"event": "message/end", "data": {"content": "x"}},
            ],
            "cursor": 9,
        },
        "session/detach": DETACH_OK,
    }
    console, connection, output = make_console(["/attach latest", "/detach"], responses)
    code = console.run()
    assert code == 0
    text = output.getvalue()
    assert "代次 gen-1" in text
    assert "待处理通知 1 条" in text
    assert "已 attach 会话 s1" in text
    assert "[历史·assistant] done" in text
    assert "离线期间事件：message/end×1, service/notice×1" in text
    assert "[补放·service/notice]" in text
    assert "已 detach；会话与服务继续运行" in text
    assert ("session/detach", {}) in connection.requests
    assert connection.closed


def test_text_without_session_prompts_attach_hint():
    console, connection, output = make_console(["hello"], {"session/detach": DETACH_OK})
    assert console.run() == 0
    assert "/attach" in output.getvalue()


def test_turn_run_policy_refusal_shows_guidance():
    responses = {
        "notifications/fetch": NOTIFY_EMPTY,
        "turn/run": ServiceRequestError("Unattended policy is not effective: missing policy"),
        "session/detach": DETACH_OK,
    }
    console, connection, output = make_console(["做点事"], responses, session_id="s1")
    assert console.run() == 0
    text = output.getvalue()
    assert "Unattended policy" in text
    assert "预授权策略" in text
    assert console.turn_running is False
    assert ("turn/run", {"input": "做点事"}) in connection.requests


def test_text_enqueued_while_turn_running():
    responses = {"turn/enqueue": {"id": "q1", "mode": "steer"}, "session/detach": DETACH_OK}
    console, connection, output = make_console(["排队一下"], responses, session_id="s1")
    console.turn_running = True
    assert console.run() == 0
    assert ("turn/enqueue", {"input": "排队一下", "mode": "steer"}) in connection.requests
    assert "已排队输入" in output.getvalue()


def test_permission_prompt_is_answered_once():
    responses = {"tool/approve": {"resolved": True}, "session/detach": DETACH_OK}
    console, connection, output = make_console(["y"], responses, session_id="s1")
    console.handle_event("permission/request", {"prompt_id": "p1", "kind": "command", "summary": "run pytest"})
    assert console.run() == 0
    assert ("tool/approve", {"prompt_id": "p1", "decision": "allow_once"}) in connection.requests
    assert console.pending_prompt is None


def test_non_answer_line_while_prompt_pending_is_rejected():
    responses = {"tool/approve": {"resolved": True}, "session/detach": DETACH_OK}
    console, connection, output = make_console(["继续", "n"], responses, session_id="s1")
    console.handle_event("permission/request", {"prompt_id": "p1", "kind": "path", "summary": "write file"})
    assert console.run() == 0
    decisions = [params for method, params in connection.requests if method == "tool/approve"]
    assert decisions == [{"prompt_id": "p1", "decision": "deny_once"}]


def test_withdrawn_approval_clears_pending_prompt():
    console, connection, output = make_console(
        ["做点事"], {"turn/run": {"status": "ok"}, "session/detach": DETACH_OK}, session_id="s1"
    )
    console.handle_event("permission/request", {"prompt_id": "p1", "kind": "command", "summary": "x"})
    console.handle_event("permission/withdrawn", {"prompt_id": "p1"})
    assert console.pending_prompt is None


class InterruptingStream:
    """Input stream raising KeyboardInterrupt twice, then EOF."""

    def __init__(self):
        self.calls = 0

    def readline(self):
        self.calls += 1
        if self.calls <= 2:
            raise KeyboardInterrupt
        return ""


def test_interrupt_cancels_running_turn_then_exits():
    console, connection, output = make_console(
        [], {"turn/cancel": {"cancelling": True}, "session/detach": DETACH_OK}, session_id="s1"
    )
    console.turn_running = True
    console.input_stream = InterruptingStream()
    assert console.run() == 0
    assert ("turn/cancel", {}) in connection.requests
    assert connection.closed


def test_unknown_command_reports_without_exit():
    console, connection, output = make_console(["/nope", "/detach"], {"session/detach": DETACH_OK})
    assert console.run() == 0
    assert "未知命令 /nope" in output.getvalue()


# ---------------------------------------------------------------------------
# Integration: real server behind a real loopback WebSocket (TUI-level B08).
# ---------------------------------------------------------------------------


class TurnModel:
    """Streaming model: blocks the first request until released."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.requests = 0

    def next_stream(self, messages):
        self.requests += 1
        if self.requests == 1:
            self.started.set()
            assert self.release.wait(10), "model was never released"
        yield StreamToken(type="text", content="done")
        yield StreamToken(type="done")


def policy_payload(tmp_path):
    return {
        "version": 1,
        "user": "operator",
        "workspace": str(tmp_path),
        "allowed_paths": [str(tmp_path)],
        "allowed_tools": [],
        "allowed_commands": ["python -m pytest"],
        "network_targets": [],
        "limits": {"max_cost_usd": 5, "max_requests": 100, "max_duration_seconds": 3600},
        "external_actions": [],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds"),
    }


def make_resident_session(tmp_path, host, model=None):
    session = app.ClientSession(str(tmp_path))
    session.model = model if model is not None else app.MockModelAdapter()
    session.tools = ToolRegistry([])
    session.runtime = {"verificationMode": "off", "model": "mock"}
    session.permissions = PermissionManager(str(tmp_path), mode="full_access")
    session.session_data = create_new_session(str(tmp_path))
    host.register_session(session.session_data.session_id, session)
    return session


class LoopbackService:
    """Serve the real ``PepsiCodeServer.handle_ws`` on a real loopback port."""

    def __init__(self, server):
        import websockets

        self._websockets = websockets
        self._server = server
        self.port = None
        self.loop = asyncio.new_event_loop()
        self._stop_event = asyncio.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while self.port is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.port is None:
            raise RuntimeError("loopback service did not start")

    def _run(self):
        asyncio.set_event_loop(self.loop)

        async def main():
            async with self._websockets.serve(self._server.handle_ws, "127.0.0.1", 0, origins=None) as ws:
                self.port = ws.sockets[0].getsockname()[1]
                await self._stop_event.wait()

        self.loop.run_until_complete(main())

    def stop(self):
        self.loop.call_soon_threadsafe(self._stop_event.set)
        self.thread.join(timeout=5)
        self.loop.close()


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.core.session.PEPSI_CODE_DIR", tmp_path / "state")
    monkeypatch.setattr("pepsicode.core.session.SESSIONS_DIR", tmp_path / "state" / "sessions")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    return tmp_path


def _wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_console_attach_steer_and_disconnect_does_not_cancel_turn(isolated, tmp_path):
    host = ServiceHost(
        str(tmp_path), {"model": "mock", "service": {"connectors": {}}}, token="test-token", port=0, generation="gen-1"
    )
    save_policy(tmp_path, policy_payload(tmp_path))
    server = app.PepsiCodeServer(port=0, host="127.0.0.1", token="test-token", service_host=host)
    model = TurnModel()
    session = make_resident_session(tmp_path, host, model=model)
    session_id = session.session_data.session_id
    service = LoopbackService(server)
    output = io.StringIO()
    result = {}

    def run_console():
        result["code"] = run_service_console(
            url=f"ws://127.0.0.1:{service.port}",
            token="test-token",
            session_id=session_id,
            input_stream=io.StringIO("开始任务\n补充上下文\n"),
            output=output,
        )

    try:
        thread = threading.Thread(target=run_console, name="console-under-test")
        thread.start()
        assert model.started.wait(15), "turn/run never reached the model"
        assert _wait_until(lambda: session._worker_active.is_set()), "turn is not active server-side"
        assert "已排队输入" in output.getvalue(), "the steer line was never accepted"
        assert not session._cancellation_event.is_set()

        thread.join(timeout=20)
        assert not thread.is_alive(), "console did not exit after its script was consumed"
        assert result["code"] == 0
        assert host.attached(session_id) == 0, "console exit must detach the control connection"
        assert not session._cancellation_event.is_set(), "detaching must not cancel the running turn"

        model.release.set()
        assert _wait_until(lambda: not session._worker_active.is_set()), "turn never finished"

        connection = ServiceConnection(f"ws://127.0.0.1:{service.port}", "test-token")
        try:
            connection.authenticate("test-token")
            view = connection.request("session/resume", {"session_id": session_id})
            contents = [str(entry.get("content", "")) for entry in view.get("history", [])]
            assert "done" in contents, "reconnect must read the finished turn's result"
        finally:
            connection.close()
    finally:
        model.release.set()
        _wait_until(lambda: not session._worker_active.is_set(), timeout=5)
        service.stop()

    text = output.getvalue()
    assert "常驻服务已连接 · 代次 gen-1" in text
    assert "已 detach；会话与服务继续运行" in text
