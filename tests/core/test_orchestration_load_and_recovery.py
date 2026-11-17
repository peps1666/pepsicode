"""Load, approval-hold, compaction-traceability, and crash-recovery tests."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from threading import Barrier, Event

from pepsicode import config
from pepsicode.context.context_manager import ContextManager
from pepsicode.core import session as sessions
from pepsicode.core.session_tree import recording_session
from pepsicode.core.task_control import begin_task, edit_control
from pepsicode.execution.runtime import (
    ExecutionJournal,
    ExecutionScope,
    RequestBudget,
    bind_scope,
    execution_journal_dir,
)
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.orchestration.budget import bind_budget
from pepsicode.orchestration.runtime import run_owned
from pepsicode.orchestration.store import admit_work, claim_launch, retry_work, settle_launch
from pepsicode.permissions import PermissionManager
from pepsicode.server import app
from pepsicode.tooling import ToolRegistry
from pepsicode.tui.service_console import ServiceConnection
from pepsicode.types import AgentStep


def _isolate_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setattr(config, "PEPSI_CODE_DIR", home)
    monkeypatch.setattr(sessions, "PEPSI_CODE_DIR", home)
    monkeypatch.setattr(sessions, "SESSIONS_DIR", home / "sessions")
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", home / "permissions.json")
    return home


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "baseline"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_full_capacity_approval_hold_and_sequential_slot_release(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    session = sessions.create_new_session(str(ws))
    with recording_session(session):
        task = begin_task([{"role": "user", "content": "Run three read-only checks"}], {})
    items = [
        admit_work(
            session,
            task_id=task["id"],
            revision=1,
            work_key=key,
            specification={"agent_type": "explore", "goal": key},
        )
        for key in ("w1", "w2", "w3")
    ]
    entered_exec = {key: Event() for key in ("w1", "w2", "w3")}
    prompt_order = []
    prompt_cond = threading.Condition()
    approved = set()
    active_lock = threading.Lock()
    stats = {"active": 0, "peak": 0, "order": []}

    def approve(request):
        scope_text = str(request.get("scope", ""))
        key = next(k for k in ("w1", "w2", "w3") if k in scope_text)
        with prompt_cond:
            prompt_order.append(key)
            prompt_cond.notify_all()
            assert prompt_cond.wait_for(lambda: key in approved, timeout=10), f"{key} approval never released"
        return {"decision": "allow_once"}

    permissions = PermissionManager(str(ws), prompt=approve, mode="default")
    scope = ExecutionScope(
        str(ws),
        permissions=permissions,
        cancellation_event=Event(),
        budget=RequestBudget(CostTracker()),
        journal=ExecutionJournal(execution_journal_dir(ws) / "slots.jsonl"),
    )
    try:

        def execute(item):
            key = item["work_key"]
            with active_lock:
                stats["active"] += 1
                stats["peak"] = max(stats["peak"], stats["active"])
                stats["order"].append(key)
            entered_exec[key].set()
            try:
                outcome = permissions.check_command("echo", [key], str(ws), force_prompt_reason="test")
                assert outcome.is_allowed
                return "result_ready", f"done {key}"
            finally:
                with active_lock:
                    stats["active"] -= 1

        def driver():
            assert entered_exec["w1"].wait(10) and entered_exec["w2"].wait(10)
            with prompt_cond:
                assert prompt_cond.wait_for(lambda: len(prompt_order) == 1, timeout=10)
                first_key = prompt_order[0]
            time.sleep(0.05)
            # Slots 2/2 are full while w1 & w2 hold execution slots awaiting approval: w3 cannot start yet.
            assert not entered_exec["w3"].is_set()
            assert "w3" not in stats["order"]
            assert stats["peak"] == 2

            # Approve the first blocked worker -> slot opens for w3 while the second worker still awaits approval.
            with prompt_cond:
                approved.add(first_key)
                prompt_cond.notify_all()
            assert entered_exec["w3"].wait(10)
            with prompt_cond:
                assert prompt_cond.wait_for(lambda: len(prompt_order) == 2, timeout=10)
                second_key = prompt_order[1]
                approved.add(second_key)
                prompt_cond.notify_all()
                assert prompt_cond.wait_for(lambda: len(prompt_order) == 3, timeout=10)
                approved.add(prompt_order[2])
                prompt_cond.notify_all()

        thread = threading.Thread(target=driver, daemon=True)
        thread.start()
        with recording_session(session), bind_scope(scope):
            ran = run_owned(
                session,
                execute=execute,
                on_result=lambda *_: None,
                max_workers=2,
                is_read_only=lambda _: True,
            )
        thread.join(timeout=10)
        assert ran is True
        assert stats["peak"] == 2
        assert len(stats["order"]) == 3
        saved = sessions.load_session(session.session_id)
        assert [row["state"] for row in saved.task_control["orchestration"]["work"]] == [
            "result_ready",
            "result_ready",
            "result_ready",
        ]
        assert [row["work_id"] for row in saved.task_control["orchestration"]["work"]] == [
            item["work_id"] for item in items
        ]
    finally:
        with prompt_cond:
            approved.update(("w1", "w2", "w3"))
            prompt_cond.notify_all()
        permissions.close()


def test_cross_session_total_capacity_limits_concurrent_workers_across_sessions(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    s1 = sessions.create_new_session(str(ws1))
    s2 = sessions.create_new_session(str(ws2))
    with recording_session(s1):
        t1 = begin_task([{"role": "user", "content": "Session 1"}], {})
    with recording_session(s2):
        t2 = begin_task([{"role": "user", "content": "Session 2"}], {})
    for key in ("s1-a", "s1-b"):
        admit_work(s1, task_id=t1["id"], revision=1, work_key=key, specification={"agent_type": "explore", "goal": key})
    for key in ("s2-a", "s2-b"):
        admit_work(s2, task_id=t2["id"], revision=1, work_key=key, specification={"agent_type": "explore", "goal": key})

    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "entered": []}
    release_first = Event()
    both_entered = Event()

    def execute(item):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["entered"].append(item["work_key"])
            if len(state["entered"]) == 2:
                both_entered.set()
        try:
            assert release_first.wait(10)
            return "result_ready", f"ok {item['work_key']}"
        finally:
            with lock:
                state["active"] -= 1

    runtime = {"orchestration": {"maxTotalActiveWorkers": 2}}
    errors = []

    def run_host(sess):
        try:
            with recording_session(sess):
                run_owned(
                    sess,
                    execute=execute,
                    on_result=lambda *_: None,
                    max_workers=2,
                    is_read_only=lambda _: True,
                    runtime=runtime,
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_one = threading.Thread(target=run_host, args=(s1,))
    t_two = threading.Thread(target=run_host, args=(s2,))
    t_one.start()
    t_two.start()
    assert both_entered.wait(10)
    time.sleep(0.1)
    assert state["peak"] == 2
    assert len(state["entered"]) == 2
    release_first.set()
    t_one.join(timeout=10)
    t_two.join(timeout=10)
    assert not errors, errors
    assert state["peak"] == 2
    assert len(state["entered"]) == 4
    assert all(
        row["state"] == "result_ready"
        for sid in (s1.session_id, s2.session_id)
        for row in sessions.load_session(sid).task_control["orchestration"]["work"]
    )


def test_event_storm_fairness_coalescing_and_failure_question_isolation(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    session = sessions.create_new_session(str(ws))
    with recording_session(session):
        task = begin_task([{"role": "user", "content": "Burst and isolate"}], {})
    items = [
        admit_work(
            session,
            task_id=task["id"],
            revision=1,
            work_key=f"burst-{idx}",
            specification={"agent_type": "explore", "goal": f"goal-{idx}"},
        )
        for idx in range(10)
    ]
    barrier = Barrier(10)

    def execute_burst(item):
        barrier.wait(timeout=10)
        return "result_ready", f"done {item['work_key']}"

    wake_batches = []
    delivered_attempts = []

    def coordinate(sess, tools, attempt_id):
        if attempt_id is None:
            return
        from pepsicode.orchestration.delivery import deliver_pending

        delivered = deliver_pending(sess, batch_limit=4, context_limit=12_000)
        if delivered:
            wake_batches.append(len(delivered))
            for msg in sess.messages[-len(delivered) :]:
                text = msg.get("content", "")
                if "attempt " in text:
                    delivered_attempts.append(text.split("attempt ", 1)[1].split(",", 1)[0])

    with recording_session(session):
        run_owned(
            session,
            execute=execute_burst,
            coordinate=coordinate,
            max_workers=10,
            is_read_only=lambda _: True,
            batch_results=True,
            event_debounce_ms=80,
            event_max_delay_ms=150,
            runtime={"orchestration": {"eventBatchLimit": 4, "eventContextChars": 12_000}},
        )

    # All 10 burst events were delivered fairly (bounded batches, none starved).
    assert set(delivered_attempts) == {item["attempt_id"] for item in items}
    assert len(wake_batches) < 10

    # Isolation check: one ordinary failure and one required question must not disturb sibling work.
    s2 = sessions.create_new_session(str(ws))
    with recording_session(s2):
        t2 = begin_task([{"role": "user", "content": "Isolate failure and question"}], {})
    fail_item = admit_work(
        s2, task_id=t2["id"], revision=1, work_key="failing", specification={"agent_type": "explore", "goal": "fail"}
    )
    ask_item = admit_work(
        s2, task_id=t2["id"], revision=1, work_key="asking", specification={"agent_type": "explore", "goal": "ask"}
    )
    ok_item = admit_work(
        s2, task_id=t2["id"], revision=1, work_key="healthy", specification={"agent_type": "explore", "goal": "ok"}
    )
    permissions = PermissionManager(str(ws), mode="full_access")
    scope = ExecutionScope(
        str(ws),
        permissions=permissions,
        cancellation_event=Event(),
        budget=RequestBudget(CostTracker()),
        journal=ExecutionJournal(execution_journal_dir(ws) / "isolate.jsonl"),
    )
    try:

        def execute_mixed(item):
            key = item["work_key"]
            if key == "failing":
                return "failed", "local error in failing worker"
            if key == "asking":
                with edit_control(session=s2) as control:
                    control.setdefault("questions", []).append(
                        {
                            "id": "q-ask-1",
                            "trace_id": item["trace_id"],
                            "question": "Which config file?",
                            "required": True,
                            "status": "pending",
                        }
                    )
                return "waiting_user", "Which config file?"
            return "result_ready", "healthy worker finished"

        with recording_session(s2), bind_scope(scope):
            run_owned(
                s2,
                execute=execute_mixed,
                on_result=lambda *_: None,
                max_workers=3,
                is_read_only=lambda _: True,
            )
        saved2 = sessions.load_session(s2.session_id)
        by_key = {row["work_key"]: row for row in saved2.task_control["orchestration"]["work"]}
        assert by_key["failing"]["state"] == "failed"
        assert by_key["asking"]["state"] == "waiting_user"
        assert by_key["healthy"]["state"] == "result_ready"

        # Answering the required question unblocks ONLY the asking work item.
        with edit_control(session=saved2) as control:
            control["questions"][0].update(status="answered", answer="settings.json")
        retried = retry_work(saved2, task_id=t2["id"], revision=1, attempt_id=ask_item["attempt_id"])
        assert retried["work_id"] == ask_item["work_id"]
        assert retried["continuation"] == [{"question": "Which config file?", "answer": "settings.json"}]
        assert len(sessions.load_session(s2.session_id).task_control["orchestration"]["work"]) == 4
        assert fail_item["attempt_id"] != retried["attempt_id"]
        assert ok_item["attempt_id"] != retried["attempt_id"]
    finally:
        permissions.close()


def test_browsing_old_session_across_tui_server_and_qq_makes_zero_model_requests(tmp_path, monkeypatch):
    import websockets

    _isolate_home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()

    class CountingModel:
        def __init__(self):
            self.calls = 0

        def next(self, messages):
            self.calls += 1
            return AgentStep(type="assistant", content="unexpected model call")

        def next_stream(self, messages):
            self.calls += 1
            raise AssertionError("Browsing a saved session must never invoke the model")

    saved_sessions = {}
    for client in ("tui", "desktop", "qq"):
        s = sessions.create_new_session(str(ws))
        s.profile = "qq_private" if client == "qq" else "default"
        s.messages = [
            {"role": "user", "content": f"old prompt ({client})"},
            {"role": "assistant", "content": f"old answer ({client})"},
        ]
        with recording_session(s):
            t = begin_task([{"role": "user", "content": f"old prompt ({client})"}], {})
        item = admit_work(
            s,
            task_id=t["id"],
            revision=1,
            work_key=f"old-{client}",
            specification={"agent_type": "explore", "goal": "inspect"},
        )
        assert claim_launch(s, generation="old-gen")
        assert settle_launch(
            s, attempt_id=item["attempt_id"], generation="old-gen", outcome="result_ready", summary="ok"
        )
        sessions.save_session(s)
        saved_sessions[client] = s.session_id

    model = CountingModel()
    monkeypatch.setattr("pepsicode.llm.anthropic_adapter.AnthropicModelAdapter", lambda *a, **kw: model)
    server = app.PepsiCodeServer(port=0, host="127.0.0.1", token="browse-token")

    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()
    port_box = {}

    def run_ws():
        asyncio.set_event_loop(loop)

        async def serve():
            async with websockets.serve(server.handle_ws, "127.0.0.1", 0, origins=None) as ws_srv:
                port_box["port"] = ws_srv.sockets[0].getsockname()[1]
                await stop_event.wait()

        loop.run_until_complete(serve())

    thread = threading.Thread(target=run_ws, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while "port" not in port_box and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "port" in port_box

    try:
        # 1. TUI resume & browse via ServiceConnection + load_session
        loaded = sessions.load_session(saved_sessions["tui"])
        assert loaded is not None
        assert "Resuming session" in sessions.format_session_resume(loaded)
        tui_conn = ServiceConnection(f"ws://127.0.0.1:{port_box['port']}", "browse-token")
        try:
            tui_conn.authenticate("browse-token")
            tui_view = tui_conn.request("session/resume", {"session_id": saved_sessions["tui"]})
            assert tui_view["session_id"] == saved_sessions["tui"]
            assert len(tui_conn.request("agent/snapshot", {})["records"]) >= 0
        finally:
            tui_conn.close()

        # 2. Desktop/Server direct dispatch browse (list + resume + status)
        client_session = app.ClientSession(str(ws))
        client_session.model = model
        client_session.tools = ToolRegistry([])
        client_session.permissions = PermissionManager(str(ws), mode="full_access")
        try:
            listed = asyncio.run(
                server._dispatch("session/list", {"workspace": str(ws)}, client_session, None, None, None)
            )
            assert len(listed["sessions"]) == 3
            resumed_desktop = asyncio.run(
                server._dispatch(
                    "session/resume", {"session_id": saved_sessions["desktop"]}, client_session, None, None, None
                )
            )
            assert resumed_desktop["session_id"] == saved_sessions["desktop"]
            resumed_qq = asyncio.run(
                server._dispatch(
                    "session/resume", {"session_id": saved_sessions["qq"]}, client_session, None, None, None
                )
            )
            assert resumed_qq["profile"] == "qq_private"
        finally:
            client_session.permissions.close()
    finally:
        loop.call_soon_threadsafe(stop_event.set)
        thread.join(timeout=5)
        loop.close()

    assert model.calls == 0


def test_long_orchestration_task_compaction_retains_dispatch_failure_and_acceptance_evidence(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    session = sessions.create_new_session(str(ws))
    with recording_session(session):
        task = begin_task([{"role": "user", "content": "Fix auth across workers"}], {})
    item = admit_work(
        session,
        task_id=task["id"],
        revision=1,
        work_key="fix-auth",
        specification={"agent_type": "general", "goal": "Fix 401"},
    )
    assert claim_launch(session, generation="g1")
    failure_json = json.dumps(
        {"stage": "failed", "class": "code", "check": "pytest", "output": "AssertionError: 401 != 200"}
    )
    assert settle_launch(
        session, attempt_id=item["attempt_id"], generation="g1", outcome="failed", summary=failure_json
    )
    retry_item = retry_work(session, task_id=task["id"], revision=1, attempt_id=item["attempt_id"])
    assert claim_launch(session, generation="g1")
    assert settle_launch(
        session, attempt_id=retry_item["attempt_id"], generation="g1", outcome="result_ready", summary="Fixed 401"
    )
    with edit_control(session=session) as control:
        orch = control["orchestration"]
        orch["integrations"] = {
            "int-123": {
                "integration_id": "int-123",
                "task_id": task["id"],
                "revision": 1,
                "base_sha": "0" * 40,
                "head_sha": "abc123def456" + "0" * 28,
                "branch": "codex/integrate-int-123",
                "worktree": str(ws / "int"),
                "candidates": [item["work_id"]],
                "candidate_heads": {item["work_id"]: "1" * 40},
                "conflicts": [],
                "state": "accepted",
                "acceptance": {"valid": True},
            }
        }
        orch["deliveries"] = {
            "del-456": {
                "delivery_id": "del-456",
                "integration_id": "int-123",
                "kind": "local",
                "status": "local",
                "head_sha": "abc123def456" + "0" * 28,
                "attempts": [],
                "external_refs": {},
            }
        }

    manager = ContextManager(context_window=2000)
    manager.observe_tool_result("task_dispatch", {"work_key": "fix-auth"}, '{"work_id":"w"}', True)
    manager.observe_tool_result("task_integrate", {"work_ids": [item["work_id"]]}, '{"state":"accepted"}', True)
    manager.messages = [{"role": "system", "content": "Orchestrate task."}]
    for idx in range(20):
        manager.messages.append({"role": "user", "content": f"Step {idx}: " + ("context " * 40)})
        manager.messages.append({"role": "assistant", "content": f"Reply {idx}: " + ("analysis " * 40)})

    with recording_session(session):
        compacted = manager.compact_messages(force=True)

    marker = next(m for m in compacted if m.get("_compaction_marker"))
    body = marker["content"]
    assert "Orchestration evidence:" in body
    assert f"work_id={item['work_id']}" in body
    assert f"attempt_id={item['attempt_id']}" in body
    assert "state=failed" in body
    assert "AssertionError: 401 != 200" in body
    assert "Integration int-123: state=accepted, head_sha=abc123def456, acceptance_valid=True" in body
    assert "Delivery del-456: integration_id=int-123, status=local, head_sha=abc123def456" in body


def test_real_service_process_kill_mid_attempt_preserves_budget_and_fences_unknown_effects(tmp_path, monkeypatch):
    home = _isolate_home(tmp_path, monkeypatch)
    ws = tmp_path / "repo"
    _git_init(ws)
    marker = tmp_path / "in_flight.marker"
    session_file_out = tmp_path / "sid.txt"

    child_script = tmp_path / "service_child.py"
    child_script.write_text(
        f"""from __future__ import annotations
import time
from pathlib import Path
from threading import Event
from pepsicode import config
from pepsicode.core import session as sessions
from pepsicode.core.session_tree import checkpoint_execution, recording_session
from pepsicode.core.task_control import begin_task
from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope, RequestBudget, bind_scope, execution_journal_dir
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.orchestration.budget import bind_budget
from pepsicode.orchestration.runtime import run_owned
from pepsicode.orchestration.store import admit_work
from pepsicode.permissions import PermissionManager

home = Path({str(home)!r})
ws = Path({str(ws)!r})
config.PEPSI_CODE_DIR = home
sessions.PEPSI_CODE_DIR = home
sessions.SESSIONS_DIR = home / "sessions"
sess = sessions.create_new_session(str(ws))
with recording_session(sess):
    task = begin_task([{{"role": "user", "content": "Run in-flight work"}}], {{}})
admit_work(sess, task_id=task["id"], revision=1, work_key="w1", specification={{"agent_type": "explore", "goal": "g1"}})
admit_work(sess, task_id=task["id"], revision=1, work_key="w2", specification={{"agent_type": "explore", "goal": "g2"}})
permissions = PermissionManager(str(ws), mode="full_access")
journal = ExecutionJournal(execution_journal_dir(ws) / "svc-kill.jsonl")
scope = ExecutionScope(
    str(ws),
    permissions=permissions,
    cancellation_event=Event(),
    budget=RequestBudget(CostTracker(), max_requests=5),
    journal=journal,
)
with recording_session(sess), bind_scope(scope):
    bind_budget(sess, scope)
    journal.append("model/prepared", task_id=scope.task_id, request_id="req-1", body={{"model": "test-model", "messages": []}})
    journal.append("model/prepared", task_id=scope.task_id, request_id="req-2", body={{"model": "test-model", "messages": []}})
    checkpoint_execution(scope.snapshot())
    Path({str(session_file_out)!r}).write_text(sess.session_id, encoding="utf-8")

    def execute(item):
        journal.append("tool/started", task_id=scope.task_id, call_id="inflight-write-1", tool="write_file", mutating=True)
        Path({str(marker)!r}).write_text(item["attempt_id"], encoding="utf-8")
        time.sleep(30)
        return "result_ready", "should never finish"

    run_owned(sess, execute=execute, on_result=lambda *_: None, max_workers=1)
""",
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.Popen(
        [sys.executable, "-u", str(child_script)],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not marker.is_file() and time.monotonic() < deadline:
            assert proc.poll() is None, f"Child exited prematurely: {proc.stderr.read()}"
            time.sleep(0.05)
        assert marker.is_file(), "Child never entered in-flight execution"
        in_flight_attempt = marker.read_text(encoding="utf-8").strip()
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    sid = session_file_out.read_text(encoding="utf-8").strip()
    restarted = sessions.load_session(sid)
    assert restarted is not None

    # Restarted host must fence the interrupted in-flight attempt and never replay it or w2.
    replayed = []
    assert (
        run_owned(
            restarted,
            execute=lambda item: replayed.append(item["attempt_id"]) or ("result_ready", "bad"),
            on_result=lambda *_: None,
        )
        is False
    )
    assert replayed == []
    reloaded = sessions.load_session(sid)
    work_by_key = {row["work_key"]: row for row in reloaded.task_control["orchestration"]["work"]}
    assert work_by_key["w1"]["attempt_id"] == in_flight_attempt
    assert work_by_key["w1"]["state"] == "recovery_pending"
    assert work_by_key["w2"]["state"] == "queued"

    # Budget is restored from the execution journal (2 requests already spent, not reset to 0).
    permissions = PermissionManager(str(ws), mode="full_access")
    try:
        new_scope = ExecutionScope(
            str(ws),
            permissions=permissions,
            cancellation_event=Event(),
            budget=RequestBudget(CostTracker(), max_requests=5),
            journal=ExecutionJournal(
                Path(reloaded.task_control["orchestration"]["budgets"][reloaded.task_control["task"]["id"]]["journal"])
            ),
        )
        bind_budget(reloaded, new_scope, recovery=True)
        assert new_scope.budget.requests == 2
        interrupted = ExecutionJournal.interrupted_calls(new_scope.journal.path)
        assert any(call["call_id"] == "inflight-write-1" for call in interrupted)
    finally:
        permissions.close()
