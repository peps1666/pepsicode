"""M4 external-delivery regression tests (checklist section 20.3, B16).

Covers the local-by-default delivery, push idempotency and reconciliation
against a local bare remote, the unknown-outcome (uncertain) semantics, and
PR idempotency against a stubbed GitHub HTTP endpoint — no real credentials
or network involved.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pepsicode.orchestration import external_delivery
from pepsicode.orchestration.candidate import record_candidate
from pepsicode.orchestration.external_delivery import DeliveryError, deliver_integration
from pepsicode.orchestration.integration import integrate_candidates, run_integration_acceptance
from tests.core.test_m4_delivery import make_project, settle  # noqa: F401 — fixture helper reuse


@pytest.fixture
def accepted_integration(tmp_path, monkeypatch):
    yield from make_project(
        tmp_path,
        monkeypatch,
        {"first": {"target.txt": "one"}, "second": {"feature.txt": "feature-two"}},
    )


def _accept(monkeypatch, data, scope, manifests):
    work_ids = [manifest["work_id"] for manifest in manifests]
    for work_id in work_ids:
        record_candidate(data, scope, work_id)
    monkeypatch.setattr(
        data,
        "runtime",
        {"verificationCommands": [{"name": "smoke", "command": [sys.executable, "-c", "print('ok')"], "timeout": 30}]},
        raising=False,
    )
    record = integrate_candidates(data, scope, work_ids)
    return run_integration_acceptance(data, scope, record["integration_id"])


def test_local_delivery_is_the_default(accepted_integration, monkeypatch):
    data, scope, sha, manifests = accepted_integration
    record = _accept(monkeypatch, data, scope, manifests)
    delivery = deliver_integration(data, scope, record["integration_id"])
    assert delivery["kind"] == "local"
    assert delivery["status"] == "local"
    assert delivery["head_sha"] == record["head_sha"]
    # No remote interaction happened.
    assert not any(attempt.get("outcome") == "pushing" for attempt in delivery["attempts"])


def _bare_remote(tmp_path):
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, timeout=15)
    return str(remote)


def test_push_is_idempotent_and_reconciles(accepted_integration, monkeypatch):
    data, scope, sha, manifests = accepted_integration
    remote = _bare_remote(Path(data.workspace).parent)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=data.workspace, check=True, capture_output=True)
    record = _accept(monkeypatch, data, scope, manifests)
    integration_id = record["integration_id"]
    head = record["head_sha"]
    branch = record["branch"]

    first = deliver_integration(data, scope, integration_id, push=True)
    assert first["status"] == "delivered"
    refs = subprocess.run(
        ["git", "ls-remote", remote, f"refs/heads/{branch}"], check=True, capture_output=True, text=True, timeout=15
    ).stdout
    assert head in refs

    # Delivering again must not push twice: reconcile sees the remote head.
    again = deliver_integration(data, scope, integration_id, push=True)
    assert again["status"] == "delivered"
    outcomes = [attempt["outcome"] for attempt in again["attempts"]]
    assert outcomes.count("pushing") == 1, "the second delivery must reconcile instead of pushing again"


def test_push_rejection_is_reported_not_retried(accepted_integration, monkeypatch):
    data, scope, sha, manifests = accepted_integration
    remote = _bare_remote(Path(data.workspace).parent)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=data.workspace, check=True, capture_output=True)
    record = _accept(monkeypatch, data, scope, manifests)

    # Make the remote branch diverge: a plain push cannot fast-forward it.
    seed = Path(data.workspace).parent / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", str(seed)], check=True, capture_output=True)
    (seed / "seed.txt").write_text("seed", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=seed, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.invalid", "commit", "-m", "seed"],
        cwd=seed,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", remote, f"HEAD:refs/heads/{record['branch']}"], cwd=seed, check=True, capture_output=True
    )

    with pytest.raises(ValueError, match="rejected"):
        deliver_integration(data, scope, record["integration_id"], push=True)
    state = data.task_control["orchestration"]["deliveries"]
    push_record = next(row for row in state.values() if row["kind"] == "push")
    assert push_record["status"] == "failed"
    assert push_record["attempts"][-1]["outcome"] == "push_rejected"


def test_push_unknown_outcome_stays_uncertain_until_reconciled(accepted_integration, monkeypatch):
    data, scope, sha, manifests = accepted_integration
    remote = _bare_remote(Path(data.workspace).parent)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=data.workspace, check=True, capture_output=True)
    record = _accept(monkeypatch, data, scope, manifests)
    integration_id = record["integration_id"]

    real_run_git = external_delivery.run_git
    pushes = {"count": 0}

    def losing_push(context, cwd, *arguments, **kwargs):
        if arguments and arguments[0] == "push":
            pushes["count"] += 1
            context.effect_state = "started"
            raise RuntimeError("Git push failed (255): connection lost mid-negotiation")
        return real_run_git(context, cwd, *arguments, **kwargs)

    monkeypatch.setattr(external_delivery, "run_git", losing_push)
    uncertain = deliver_integration(data, scope, integration_id, push=True)
    assert uncertain["status"] == "uncertain"
    assert pushes["count"] == 1

    # A later delivery runs on a fresh scope (the host reloads at scheduling
    # boundaries): reconciliation reads the remote instead of pushing again.
    monkeypatch.setattr(external_delivery, "run_git", real_run_git)

    def fresh_scope():
        from pepsicode.execution.runtime import (
            ExecutionJournal,
            ExecutionScope,
            execution_journal_dir,
        )

        return ExecutionScope(
            data.workspace,
            permissions=scope.permissions,
            journal=ExecutionJournal(execution_journal_dir(data.workspace) / "m4-reconcile.jsonl"),
        )

    reconciled = deliver_integration(data, fresh_scope(), integration_id, push=True)
    assert pushes["count"] == 1, "an uncertain delivery must never be blindly resent"
    assert reconciled["attempts"][-1]["outcome"] == "reconcile_inconclusive"
    assert reconciled["status"] == "uncertain"

    # The push actually landed elsewhere: reconciliation sees the remote head
    # and converges to delivered without a second push.
    subprocess.run(
        ["git", "push", remote, f"{record['branch']}:refs/heads/{record['branch']}"],
        cwd=data.workspace,
        check=True,
        capture_output=True,
    )
    final = deliver_integration(data, fresh_scope(), integration_id, push=True)
    assert final["status"] == "delivered"
    assert final["attempts"][-1]["outcome"] == "already_on_remote"
    assert pushes["count"] == 1


def test_pull_request_is_idempotent_by_head(accepted_integration, monkeypatch):
    from pepsicode.approval import ApprovalDecision, ApprovalOutcome

    data, scope, sha, manifests = accepted_integration
    record = _accept(monkeypatch, data, scope, manifests)
    integration_id = record["integration_id"]
    head = record["head_sha"]
    calls = {"get": 0, "post": 0}

    def fake_http(url, *, token=None, method="GET", payload=None, timeout=None):
        if method == "GET":
            calls["get"] += 1
            if calls["post"] == 0:
                return 200, []
            return 200, [{"number": 7, "html_url": "https://pr/7", "head": {"sha": head}}]
        calls["post"] += 1
        return 201, {"number": 7, "html_url": "https://pr/7"}

    monkeypatch.setattr(external_delivery, "http_request_json", fake_http)
    monkeypatch.setenv("TEST_TOKEN", "stub-token")
    with monkeypatch.context() as patcher:
        patcher.setattr(
            scope.permissions, "check_external_action", lambda *_: ApprovalOutcome(ApprovalDecision.DENY_ONCE)
        )
        with pytest.raises(PermissionError):
            deliver_integration(
                data,
                scope,
                integration_id,
                pull_request=True,
                repository="acme/widget",
                base_branch="main",
                token_env="TEST_TOKEN",
                api_base="https://api.stub",
            )
        assert calls["post"] == 0
    first = deliver_integration(
        data,
        scope,
        integration_id,
        pull_request=True,
        repository="acme/widget",
        base_branch="main",
        token_env="TEST_TOKEN",
        api_base="https://api.stub",
    )
    assert first["status"] == "delivered" and first["external_refs"]["pr_number"] == 7
    assert calls["post"] == 1

    again = deliver_integration(
        data,
        scope,
        integration_id,
        pull_request=True,
        repository="acme/widget",
        base_branch="main",
        token_env="TEST_TOKEN",
        api_base="https://api.stub",
    )
    assert again["status"] == "delivered"
    assert again["attempts"][-1]["outcome"] == "pr_already_exists"
    assert calls["post"] == 1, "an existing PR for the same head must never be recreated"


def test_pull_request_unknown_response_stays_uncertain(accepted_integration, monkeypatch):
    data, scope, sha, manifests = accepted_integration
    record = _accept(monkeypatch, data, scope, manifests)

    def lost_http(url, *, token=None, method="GET", payload=None, timeout=None):
        raise DeliveryError("connection reset during exchange")

    monkeypatch.setattr(external_delivery, "http_request_json", lost_http)
    monkeypatch.setenv("TEST_TOKEN", "stub-token")
    result = deliver_integration(
        data,
        scope,
        record["integration_id"],
        pull_request=True,
        repository="acme/widget",
        base_branch="main",
        token_env="TEST_TOKEN",
    )
    assert result["status"] == "uncertain"
    assert result["attempts"][-1]["outcome"] == "pr_check_failed"


def test_pull_request_requires_token_and_repository(accepted_integration, monkeypatch):
    data, scope, sha, manifests = accepted_integration
    record = _accept(monkeypatch, data, scope, manifests)
    monkeypatch.delenv("TEST_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        deliver_integration(
            data,
            scope,
            record["integration_id"],
            pull_request=True,
            repository="acme/widget",
            base_branch="main",
            token_env="TEST_TOKEN",
        )
    with pytest.raises(ValueError, match="repository"):
        deliver_integration(
            data,
            scope,
            record["integration_id"],
            pull_request=True,
            base_branch="main",
            token_env="TEST_TOKEN",
        )


def test_delivery_records_survive_schema_validation(accepted_integration, monkeypatch):
    from pepsicode.orchestration.schema import validate_orchestration

    data, scope, sha, manifests = accepted_integration
    record = _accept(monkeypatch, data, scope, manifests)
    deliver_integration(data, scope, record["integration_id"])
    state = data.task_control["orchestration"]
    validate_orchestration(state)
    assert state["deliveries"] and any(row["kind"] == "local" for row in state["deliveries"].values())


def test_lost_pr_response_does_not_repeat_post(accepted_integration, monkeypatch):
    data, scope, _, manifests = accepted_integration
    record = _accept(monkeypatch, data, scope, manifests)
    assert record["state"] == "accepted"
    methods = []

    def stub(*args, method="GET", **kwargs):
        methods.append(method)
        if method == "GET":
            return 200, []
        raise external_delivery.DeliveryError("simulated lost POST response")

    monkeypatch.setattr(external_delivery, "http_request_json", stub)
    monkeypatch.setenv("PEPSI_AUDIT_FAKE_TOKEN", "fixture-only")
    for _ in range(2):
        result = external_delivery.deliver_integration(
            data,
            scope,
            record["integration_id"],
            pull_request=True,
            repository="fixture/project",
            base_branch="main",
            token_env="PEPSI_AUDIT_FAKE_TOKEN",
        )
        assert result["status"] == "uncertain"
    assert methods.count("POST") == 1, methods


def test_delivery_target_gates_parent_task_completion_and_routes_ci_review_and_token_rotation(
    accepted_integration, monkeypatch, tmp_path
):
    import json
    import threading
    import time

    import websockets
    from websockets.sync.client import connect as ws_connect

    from pepsicode.core import session as sessions
    from pepsicode.core.task_control import edit_control
    from pepsicode.core.task_verification import delivered_acceptance_root
    from pepsicode.server import app
    from pepsicode.service import state as service_state
    from pepsicode.service.connectors import github as github_module
    from pepsicode.service.connectors.github import GitHubConnector
    from pepsicode.service.host import ServiceHost

    data, scope, _sha, manifests = accepted_integration
    record = _accept(monkeypatch, data, scope, manifests)
    integration_id = record["integration_id"]
    head = record["head_sha"]

    # 1. Task requesting PR cannot complete on local delivery alone.
    with edit_control(session=data) as control:
        control["task"]["objective"] = "Implement feature and open a pull request"
    deliver_integration(data, scope, integration_id)
    _, _, problem = delivered_acceptance_root(data, data.task_control["task"], scope)
    assert problem and "pull request" in problem.lower()

    def fake_pr_http(url, *, token=None, method="GET", payload=None, timeout=None):
        if method == "GET":
            return 200, []
        return 201, {"number": 19, "html_url": "https://github.com/acme/widget/pull/19"}

    monkeypatch.setattr(external_delivery, "http_request_json", fake_pr_http)
    monkeypatch.setenv("TEST_TOKEN", "stub-token")
    pr_delivery = deliver_integration(
        data,
        scope,
        integration_id,
        pull_request=True,
        repository="acme/widget",
        base_branch="main",
        token_env="TEST_TOKEN",
        api_base="https://api.stub",
    )
    assert pr_delivery["status"] == "delivered"
    checkout, delivered_head, problem = delivered_acceptance_root(data, data.task_control["task"], scope)
    assert problem is None and Path(checkout).is_dir() and delivered_head == head

    # 2. Task requiring merge/deploy must stay incomplete after merely creating the PR.
    with edit_control(session=data) as control:
        control["task"]["objective"] = "Implement feature, open a pull request and merge the PR"
    _, _, merge_problem = delivered_acceptance_root(data, data.task_control["task"], scope)
    assert merge_problem and "merge" in merge_problem.lower()

    # 3. Uncertain external delivery blocks acceptance until reconciled.
    with edit_control(session=data) as control:
        control["orchestration"]["deliveries"]["unc-push"] = {
            "delivery_id": "unc-push",
            "integration_id": integration_id,
            "task_id": data.task_control["task"]["id"],
            "revision": 1,
            "kind": "push",
            "target": {"remote": "origin", "branch": record["branch"]},
            "head_sha": head,
            "branch": record["branch"],
            "status": "uncertain",
            "attempts": [],
            "external_refs": {},
        }
    _, _, unc_problem = delivered_acceptance_root(data, data.task_control["task"], scope)
    assert unc_problem and "uncertain" in unc_problem.lower()

    with edit_control(session=data) as control:
        control["orchestration"]["deliveries"].pop("unc-push")
        for row in control["orchestration"]["deliveries"].values():
            if row["kind"] == "pull_request":
                row["external_refs"]["merged"] = True
    checkout, delivered_head, resolved_problem = delivered_acceptance_root(data, data.task_control["task"], scope)
    assert resolved_problem is None and delivered_head == head

    # 4. External CI & Review event routing binds to delivered head_sha and notifies outbox.
    sessions.save_session(data)
    host = ServiceHost(data.workspace, {"service": {}}, token="initial-token-123456", port=0, generation="gen-m4")
    client = app.ClientSession(data.workspace)
    client.session_data = data
    host.register_session(data.session_id, client)

    api_responses = {
        "/repos/acme/widget/pulls": [
            {"number": 19, "title": "Feature", "updated_at": "2026-11-17T10:00:00Z", "head": {"sha": head}}
        ],
        "/repos/acme/widget/pulls/19/reviews": [
            {"id": 501, "state": "CHANGES_REQUESTED", "body": "fix edge case", "commit_id": head}
        ],
        f"/repos/acme/widget/commits/{head}/check-runs": {
            "check_runs": [
                {"id": 601, "name": "ci", "conclusion": "failure", "run_id": 88, "html_url": "https://ci/88"}
            ]
        },
    }
    monkeypatch.setattr(
        github_module,
        "http_get_json",
        lambda url, *, token: (200, api_responses[url.split("api.github.com", 1)[1].split("?")[0]]),
    )
    events = GitHubConnector(repository="acme/widget", token="gh-tok").poll()
    # Held before input_queue starts, surfaced once in outbox, then flushed into input_queue.
    res = host.deliver_external_events(events, ["acme/widget"])
    assert res["held"] == 2 and res["dropped"] == 0
    outbox_items, _ = host.outbox.fetch_after(0)
    assert len(outbox_items) == 2

    client.input_queue.start()
    assert host.flush_pending_external(data.session_id) == 2

    # Stale head_sha event for the same PR must not be routed to the resident session.
    stale_api = {
        "/repos/acme/widget/pulls": [
            {"number": 19, "title": "Feature", "updated_at": "2026-11-17T09:00:00Z", "head": {"sha": "stale" * 8}}
        ],
        "/repos/acme/widget/pulls/19/reviews": [],
        f"/repos/acme/widget/commits/{'stale' * 8}/check-runs": {
            "check_runs": [
                {"id": 602, "name": "ci", "conclusion": "success", "run_id": 87, "html_url": "https://ci/87"}
            ]
        },
    }
    monkeypatch.setattr(
        github_module,
        "http_get_json",
        lambda url, *, token: (200, stale_api[url.split("api.github.com", 1)[1].split("?")[0]]),
    )
    stale_events = GitHubConnector(repository="acme/widget", token="gh-tok").poll()
    assert stale_events and not host._owns_external_event(data, stale_events[0])
    stale_res = host.deliver_external_events(stale_events, ["acme/widget"])
    assert stale_res["delivered"] == 0 and stale_res["held"] == 0

    # 5. Live token rotation rejects the old token and accepts the new token over WebSocket.
    server = app.PepsiCodeServer(port=0, host="127.0.0.1", token=host.token, service_host=host)
    import asyncio

    loop = asyncio.new_event_loop()
    stop_event = asyncio.Event()
    port_box = {}

    def run_ws():
        asyncio.set_event_loop(loop)

        async def serve():
            async with websockets.serve(server.handle_ws, "127.0.0.1", 0, origins=None) as ws_srv:
                port = ws_srv.sockets[0].getsockname()[1]
                host.port = port
                port_box["port"] = port
                await stop_event.wait()

        loop.run_until_complete(serve())

    thread = threading.Thread(target=run_ws, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while "port" not in port_box and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "port" in port_box
    url = f"ws://127.0.0.1:{port_box['port']}"

    try:
        with ws_connect(url, open_timeout=5, close_timeout=5) as ws:
            ws.send(
                json.dumps(
                    {
                        "kind": "request",
                        "id": 1,
                        "method": "connection/auth",
                        "params": {"token": "initial-token-123456"},
                    }
                )
            )
            assert json.loads(ws.recv(timeout=5))["result"]["authenticated"] is True
            ws.recv(timeout=5)  # connection/ready
            ws.send(
                json.dumps(
                    {
                        "kind": "request",
                        "id": 2,
                        "method": "service/rotate_token",
                        "params": {"new_token": "rotated-token-654321"},
                    }
                )
            )
            rot = json.loads(ws.recv(timeout=5))["result"]
            assert rot["rotated"] is True
            assert (service_state.service_dir(data.workspace) / "token").read_text(
                encoding="utf-8"
            ) == "rotated-token-654321"

        with ws_connect(url, open_timeout=5, close_timeout=5) as ws_old:
            ws_old.send(
                json.dumps(
                    {
                        "kind": "request",
                        "id": 1,
                        "method": "connection/auth",
                        "params": {"token": "initial-token-123456"},
                    }
                )
            )
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                ws_old.recv(timeout=5)

        with ws_connect(url, open_timeout=5, close_timeout=5) as ws_new:
            ws_new.send(
                json.dumps(
                    {
                        "kind": "request",
                        "id": 1,
                        "method": "connection/auth",
                        "params": {"token": "rotated-token-654321"},
                    }
                )
            )
            assert json.loads(ws_new.recv(timeout=5))["result"]["authenticated"] is True
    finally:
        client.input_queue.close()
        loop.call_soon_threadsafe(stop_event.set)
        thread.join(timeout=5)
        loop.close()
