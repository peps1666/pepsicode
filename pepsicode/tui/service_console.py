"""Attach console for the resident service (M3 §19.1).

A line-based terminal console that attaches a control connection to a
resident service session over the existing WebSocket protocol. It is an
interaction entry point only — it does not own sessions and never touches
``service/shutdown``:

- exiting the console detaches the control connection; running turns keep
  going (B08) and approvals fall back to the service policy backend;
- starting a new turn (``turn/run``) still requires an effective unattended
  policy server-side; steering (``turn/enqueue``) and ``turn/cancel`` do not.

The console shares no state with the in-process TUI (:mod:`pepsicode.tui.app`);
it deliberately avoids raw-mode screen ownership so it can run alongside the
service from any terminal.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from typing import Callable, TextIO

from pepsicode.logging_config import get_logger

logger = get_logger("service-console")

REQUEST_TIMEOUT = 30.0
AUTH_TIMEOUT = 10.0
READY_TIMEOUT = 10.0
_MAX_PRINT_CHARS = 4000
_HISTORY_TAIL = 5
_REPLAY_SUMMARY_CHARS = 160

# Events replayed from the journal after attach (bounded summary only).
_REPLAY_EVENT_NAMES = ("message/end", "turn/end", "service/notice", "notification", "error")


class ServiceConsoleError(RuntimeError):
    """Console-level failure (connection, authentication, protocol)."""


class ConsoleClosedError(ServiceConsoleError):
    """The connection dropped (or was closed by this console)."""


class ServiceRequestError(ServiceConsoleError):
    """The service answered the request with an error frame."""


class ServiceConnection:
    """Synchronous protocol connection with a demuxing reader thread.

    Responses are matched to ``request`` callers by frame id; events are
    forwarded to ``event_handler``. The reader thread is registered and joined
    on close — no daemon threads carry delivery.
    """

    def __init__(self, url: str, token: str, open_timeout: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._pending: dict[int, queue.SimpleQueue] = {}
        self._closed = threading.Event()
        self._early_events: list[tuple[str, dict]] = []
        self._event_handler: Callable[[str, dict], None] | None = None
        try:
            from websockets.sync.client import connect
        except ImportError as error:
            raise ServiceConsoleError("The 'websockets' package is required for the attach console") from error
        try:
            self._ws = connect(url, open_timeout=open_timeout, close_timeout=5)
        except Exception as error:  # noqa: BLE001 — surface any transport failure as console error
            raise ServiceConsoleError(f"Cannot connect to {url}: {error}") from error
        self._reader = threading.Thread(target=self._read_loop, name="service-console-reader")
        self._reader.start()

    # -- event handler -----------------------------------------------------

    @property
    def event_handler(self) -> Callable[[str, dict], None] | None:
        return self._event_handler

    @event_handler.setter
    def event_handler(self, handler: Callable[[str, dict], None] | None) -> None:
        with self._lock:
            self._event_handler = handler
            buffered, self._early_events = self._early_events, []
        if handler is not None:
            for name, data in buffered:
                handler(name, data)

    # -- frames ------------------------------------------------------------

    def authenticate(self, token: str) -> None:
        result = self.request("connection/auth", {"token": token, "orchestration": 1}, timeout=AUTH_TIMEOUT)
        if not result.get("authenticated"):
            raise ServiceConsoleError("Authentication failed")

    def request(self, method: str, params: dict | None = None, timeout: float | None = REQUEST_TIMEOUT) -> dict:
        with self._lock:
            if self._closed.is_set():
                raise ConsoleClosedError("connection closed")
            frame_id = self._next_id()
            waiter: queue.SimpleQueue = queue.SimpleQueue()
            self._pending[frame_id] = waiter
        frame = {"kind": "request", "id": frame_id, "method": method, "params": params or {}}
        try:
            self._ws.send(json.dumps(frame))
        except Exception as error:  # noqa: BLE001
            self._drop_waiter(frame_id)
            raise ConsoleClosedError(f"send failed: {error}") from error
        try:
            response = waiter.get(timeout=timeout) if timeout is not None else waiter.get()
        except queue.Empty as error:
            self._drop_waiter(frame_id)
            raise ServiceConsoleError(f"{method} timed out") from error
        if isinstance(response, Exception):
            raise response
        if "error" in response:
            error = response.get("error")
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ServiceRequestError(message or f"{method} failed")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 — closing a broken socket is best effort
            pass
        self._reader.join(timeout=5)

    # -- internals ---------------------------------------------------------

    def _next_id(self) -> int:
        frame_id = getattr(self, "_frame_id", 0) + 1
        self._frame_id = frame_id
        return frame_id

    def _drop_waiter(self, frame_id: int) -> None:
        with self._lock:
            self._pending.pop(frame_id, None)

    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                try:
                    raw = self._ws.recv(timeout=0.5)
                except TimeoutError:
                    continue
                if raw is None:
                    continue
                self._dispatch_frame(json.loads(raw))
        except Exception as error:  # noqa: BLE001 — reader exits on any transport failure
            logger.debug("Service console reader exited: %s", error)
        finally:
            with self._lock:
                self._closed.set()
                waiters = list(self._pending.values())
                self._pending.clear()
            for waiter in waiters:
                waiter.put(ConsoleClosedError("connection closed"))

    def _dispatch_frame(self, frame: dict) -> None:
        kind = frame.get("kind")
        if kind == "response":
            with self._lock:
                waiter = self._pending.pop(frame.get("id"), None)
            if waiter is not None:
                waiter.put(frame)
            return
        if kind == "event":
            name = str(frame.get("event", ""))
            data = frame.get("data")
            with self._lock:
                handler = self._event_handler
                if handler is None:
                    self._early_events.append((name, data if isinstance(data, dict) else {}))
                    return
            try:
                handler(name, data if isinstance(data, dict) else {})
            except Exception:  # noqa: BLE001 — a display bug must not kill the reader
                logger.exception("Service console event handler failed for %s", name)


def _clip(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（截断，共 {len(text)} 字符）"


class ServiceConsole:
    """Line-based interaction loop against an attached resident session."""

    def __init__(
        self,
        connection: ServiceConnection,
        *,
        session_id: str | None = None,
        input_stream: TextIO | None = None,
        output: TextIO | None = None,
    ) -> None:
        self.connection = connection
        self.connection.event_handler = self.handle_event
        self.input_stream = input_stream
        self.output = output
        self.session_id: str | None = session_id
        self.turn_running = False
        self.pending_prompt: dict | None = None
        self.banner: dict = {}
        self._ready = threading.Event()
        self._output_lock = threading.Lock()
        self._turn_threads: list[threading.Thread] = []

    # -- output helpers ----------------------------------------------------

    def _out(self) -> TextIO:
        return self.output if self.output is not None else sys.stdout

    def _in(self) -> TextIO:
        return self.input_stream if self.input_stream is not None else sys.stdin

    def _print(self, text: str = "") -> None:
        with self._output_lock:
            print(text, file=self._out(), flush=True)

    def _write_prompt(self, text: str) -> None:
        with self._output_lock:
            self._out().write(text)
            self._out().flush()

    # -- event handling ----------------------------------------------------

    def handle_event(self, name: str, data: dict) -> None:
        if name == "connection/ready":
            self.banner = data
            self._ready.set()
            return
        if name == "message/end":
            role = data.get("role")
            content = _clip(str(data.get("content", "")), _MAX_PRINT_CHARS)
            if data.get("is_error"):
                self._print(f"[错误回复] {content}")
            elif role == "progress":
                self._print(f"[进度] {content}")
            else:
                self._print(f"[助手] {content}")
            return
        if name == "turn/end":
            self.turn_running = False
            status = data.get("status", "ok")
            if status == "error":
                self._print(f"[轮次结束] status=error: {data.get('error', '')}")
            else:
                self._print(f"[轮次结束] status={status}")
            return
        if name == "service/notice":
            self._print(f"[服务] {data.get('kind', '')} {data.get('session_id', '')}".rstrip())
            return
        if name == "notification":
            self._print(f"[通知] {data.get('title', '')} — {data.get('body', '')}")
            return
        if name == "permission/request":
            self.pending_prompt = data
            summary = _clip(str(data.get("summary", "")), 200)
            self._print(f"[待审批] ({data.get('kind', 'unknown')}) {summary}")
            self._print("[待审批] 输入 y 允许一次 / n 拒绝")
            return
        if name == "permission/withdrawn":
            if self.pending_prompt is not None and self.pending_prompt.get("prompt_id") == data.get("prompt_id"):
                self.pending_prompt = None
                self._print("[审批已撤回]")
            return
        if name == "error":
            self._print(f"[服务错误] {data.get('message', data)}")
            return
        # deltas, agent/update, usage/queue updates are intentionally not printed

    # -- main loop ---------------------------------------------------------

    def run(self) -> int:
        if not self._ready.wait(READY_TIMEOUT):
            self._print("[错误] 服务未在时限内报告就绪（connection/ready 超时）")
            return 1
        if not self.banner.get("service"):
            self._print("目标服务不是常驻服务（interactive 模式断开会取消任务）；attach 控制台仅用于 service 模式。")
            self.connection.close()
            return 1
        self._print_banner()
        if self.session_id and not self._attach(self.session_id):
            self.connection.close()
            return 1
        while True:
            try:
                line = self._read_line()
            except EOFError:
                break
            except KeyboardInterrupt:
                if not self._on_interrupt():
                    break
                continue
            line = line.strip()
            if not line:
                continue
            if self.pending_prompt is not None:
                self._handle_prompt_answer(line)
                continue
            if line.startswith("/"):
                if not self._dispatch_command(line):
                    break
            else:
                self._submit_text(line)
        self._shutdown()
        return 0

    def _read_line(self) -> str:
        label = self.session_id or "no-session"
        self._write_prompt(f"{label}> ")
        line = self._in().readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")

    def _on_interrupt(self) -> bool:
        """Ctrl+C: cancel the running turn / deny the pending approval / exit."""
        if self.pending_prompt is not None:
            self._handle_prompt_answer("n")
            return True
        if self.turn_running:
            self._cancel_turn()
            return True
        return False

    def _shutdown(self) -> None:
        if self.session_id is not None:
            try:
                self.connection.request("session/detach", {}, timeout=5.0)
                self._print("已 detach；会话与服务继续运行")
            except ServiceConsoleError:
                logger.debug("Detach request failed during shutdown", exc_info=True)
        self.connection.close()
        for thread in list(self._turn_threads):
            thread.join(timeout=2.0)

    # -- commands ----------------------------------------------------------

    def _dispatch_command(self, line: str) -> bool:
        """Handle one ``/`` command; returns False when the console should exit."""
        parts = line.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command in ("/detach", "/quit", "/exit"):
            return False
        if command == "/help":
            self._print(
                "命令：/sessions 列出会话 · /attach <id|latest> 接管会话 · /status 服务状态 · "
                "/notifications 待处理通知 · /cancel 取消当前轮次 · /detach 断开（不停止任务） · /help"
            )
        elif command == "/sessions":
            self._list_sessions()
        elif command == "/attach":
            self._attach(argument or "latest")
        elif command == "/status":
            self._print_status()
        elif command == "/notifications":
            self._print_notifications()
        elif command == "/cancel":
            self._cancel_turn()
        else:
            self._print(f"未知命令 {command}；/help 查看可用命令")
        return True

    def _list_sessions(self) -> None:
        try:
            sessions = self.connection.request("session/list", {}).get("sessions", [])
        except ServiceConsoleError as error:
            self._print(f"会话列表获取失败：{error}")
            return
        if not sessions:
            self._print("服务没有已保存的会话")
            return
        for item in sessions:
            marker = "*" if item.get("session_id") == self.session_id else " "
            self._print(
                f"{marker} {item.get('session_id')} · 更新 {item.get('updated_at', '')} · "
                f"消息 {item.get('message_count', 0)} · {item.get('first_message', '')[:60]}"
            )

    def _attach(self, target: str) -> bool:
        session_id = target
        if target in ("", "latest"):
            try:
                sessions = self.connection.request("session/list", {}).get("sessions", [])
            except ServiceConsoleError as error:
                self._print(f"会话列表获取失败：{error}")
                return False
            if not sessions:
                self._print("服务没有已保存的会话")
                return False
            session_id = max(sessions, key=lambda item: str(item.get("updated_at", "")))["session_id"]
        try:
            view = self.connection.request("session/resume", {"session_id": session_id})
        except ServiceConsoleError as error:
            self._print(f"attach 失败：{error}")
            return False
        self.session_id = str(view.get("session_id", session_id))
        history = view.get("history", [])
        cursor = view.get("event_cursor", 0)
        self._print(f"已 attach 会话 {self.session_id}（消息 {len(history)} 条，事件游标 {cursor}）")
        self._print_history_tail(history)
        self._replay_events(int(cursor) if isinstance(cursor, (int, float)) else 0)
        return True

    def _print_history_tail(self, history: list) -> None:
        for item in history[-_HISTORY_TAIL:]:
            role = item.get("role", "")
            content = _clip(str(item.get("content", "")), 200)
            self._print(f"[历史·{role}] {content}")

    def _replay_events(self, cursor: int) -> None:
        """Summarize events journaled while no client was attached (bounded)."""
        try:
            page = self.connection.request("session/events", {"cursor": cursor, "limit": 200})
        except ServiceConsoleError as error:
            self._print(f"离线事件补放失败：{error}")
            return
        events = page.get("events", [])
        counts: dict[str, int] = {}
        for item in events:
            name = str(item.get("event", ""))
            counts[name] = counts.get(name, 0) + 1
            if name in ("service/notice", "notification", "error"):
                data = item.get("data", {}) or {}
                self._print(f"[补放·{name}] {_clip(json.dumps(data, ensure_ascii=False), _REPLAY_SUMMARY_CHARS)}")
        summary = ", ".join(f"{name}×{count}" for name, count in sorted(counts.items()))
        if summary:
            self._print(f"离线期间事件：{summary}")

    def _print_status(self) -> None:
        for method in ("service/status", "service/health"):
            try:
                payload = self.connection.request(method, {})
            except ServiceConsoleError as error:
                self._print(f"{method} 失败：{error}")
                continue
            self._print(json.dumps(payload, ensure_ascii=False, indent=2))

    def _print_notifications(self) -> None:
        try:
            page = self.connection.request("notifications/fetch", {"cursor": 0})
        except ServiceConsoleError as error:
            self._print(f"通知获取失败：{error}")
            return
        items = page.get("notifications", [])
        self._print(f"待处理通知 {len(items)} 条")
        for item in items:
            self._print(f"  [{item.get('status', '')}] {item.get('title', '')} — {item.get('body', '')}")

    def _cancel_turn(self) -> None:
        if self.session_id is None:
            self._print("尚未 attach 会话")
            return
        try:
            self.connection.request("turn/cancel", {})
            self._print("已请求取消当前轮次")
        except ServiceConsoleError as error:
            self._print(f"取消失败：{error}")

    # -- text input --------------------------------------------------------

    def _submit_text(self, text: str) -> None:
        if self.session_id is None:
            self._print("尚未 attach 会话：先使用 /attach <session_id|latest>")
            return
        if self.turn_running:
            self._enqueue(text)
        else:
            self._run_turn(text)

    def _run_turn(self, text: str) -> None:
        self.turn_running = True
        thread = threading.Thread(target=self._run_turn_request, args=(text,), name="service-console-turn")
        self._turn_threads.append(thread)
        thread.start()

    def _run_turn_request(self, text: str) -> None:
        try:
            # The response for turn/run arrives when the turn finishes; the
            # turn/end event reports status first, so the result is not printed.
            self.connection.request("turn/run", {"input": text}, timeout=None)
        except ServiceRequestError as error:
            self.turn_running = False
            message = str(error)
            if "previous turn is still finishing" in message:
                self._print("服务端报告上一轮次仍在收尾；已转为排队输入。")
                self._enqueue(text)
                return
            if "policy" in message.lower():
                self._print(f"无法开始新轮次：{message}")
                self._print("service 模式下 turn/run 需要有效的预授权策略；排队输入与取消不受影响。")
                return
            self._print(f"轮次失败：{message}")
        except ServiceConsoleError:
            self.turn_running = False

    def _enqueue(self, text: str) -> None:
        try:
            item = self.connection.request("turn/enqueue", {"input": text, "mode": "steer"})
            self._print(f"已排队输入（id={item.get('id', '')}，mode={item.get('mode', '')}）")
        except ServiceConsoleError as error:
            self._print(f"排队失败：{error}")

    def _handle_prompt_answer(self, line: str) -> None:
        prompt = self.pending_prompt
        if prompt is None:
            return
        answer = line.strip().lower()
        if answer in ("y", "yes"):
            decision = "allow_once"
        elif answer in ("n", "no"):
            decision = "deny_once"
        else:
            self._print("请输入 y（允许一次）或 n（拒绝）")
            return
        try:
            result = self.connection.request(
                "tool/approve", {"prompt_id": prompt.get("prompt_id", ""), "decision": decision}
            )
            self._print(f"已提交审批（{decision}，resolved={result.get('resolved')}）")
        except ServiceConsoleError as error:
            self._print(f"审批提交失败：{error}")
        self.pending_prompt = None

    # -- banner ------------------------------------------------------------

    def _print_banner(self) -> None:
        generation = self.banner.get("generation", "")
        mode = self.banner.get("execution_mode", "service")
        self._print(f"常驻服务已连接 · 代次 {generation} · 执行模式 {mode}")
        try:
            page = self.connection.request("notifications/fetch", {"cursor": 0})
        except ServiceConsoleError:
            page = {}
        self._print(f"待处理通知 {len(page.get('notifications', []))} 条（/notifications 查看）")
        self._print("提示：退出控制台不会停止任务；停止服务请使用 python -m pepsicode.service stop")


def run_service_console(
    *,
    url: str,
    token: str,
    session_id: str | None = None,
    list_only: bool = False,
    input_stream: TextIO | None = None,
    output: TextIO | None = None,
) -> int:
    """Connect, authenticate and run the attach console; returns exit code."""
    try:
        connection = ServiceConnection(url, token)
    except ServiceConsoleError as error:
        print(f"无法连接常驻服务：{error}")
        return 1
    try:
        connection.authenticate(token)
    except ServiceConsoleError as error:
        print(f"鉴权失败：{error}")
        connection.close()
        return 1
    console = ServiceConsole(connection, session_id=session_id, input_stream=input_stream, output=output)
    if list_only:
        console._list_sessions()
        connection.close()
        return 0
    try:
        return console.run()
    except KeyboardInterrupt:
        console._shutdown()
        return 130
