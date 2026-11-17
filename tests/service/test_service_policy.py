"""Unattended policy: strict validation, expiry, revocation, request scope."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pepsicode.service import policy as policy_module
from pepsicode.service.policy import (
    PolicyError,
    check_policy_checkpoint,
    evaluate_request,
    policy_effective,
    revoke_policy,
    validate_policy,
)


def _policy(**overrides):
    base = {
        "version": 1,
        "user": "operator",
        "workspace": "E:/demo",
        "allowed_paths": ["E:/demo/src"],
        "allowed_tools": [],
        "allowed_commands": ["python -m pytest", "npm test"],
        "network_targets": [],
        "limits": {"max_cost_usd": 5.0, "max_requests": 100, "max_duration_seconds": 3600},
        "external_actions": [],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds"),
    }
    base.update(overrides)
    return base


def test_validate_rejects_unknown_and_missing_fields():
    with pytest.raises(PolicyError, match="unknown policy fields"):
        validate_policy(_policy(root_all=True))
    with pytest.raises(PolicyError, match="missing policy fields"):
        validate_policy({"version": 1, "user": "x"})
    with pytest.raises(PolicyError, match="limits"):
        validate_policy(_policy(limits={"whatever": 1}))
    with pytest.raises(PolicyError, match="ISO-8601"):
        validate_policy(_policy(expires_at="not-a-date"))


def test_expiry_takes_effect_at_the_checkpoint():
    policy = _policy(expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds"))
    effective, reason = policy_effective(policy)
    assert not effective and "expired" in reason


def test_revocation_takes_effect_at_the_checkpoint(tmp_path):
    workspace = tmp_path
    policy_module.save_policy(workspace, _policy(workspace=str(workspace)))
    effective, reason = policy_effective(policy_module.load_policy(workspace))
    assert effective, reason
    revoked = revoke_policy(workspace)
    assert revoked["revoked"] is True
    effective, reason = policy_effective(policy_module.load_policy(workspace))
    assert not effective and "revoked" in reason


def test_command_scope_is_prefix_based_and_fail_closed():
    policy = _policy()
    assert evaluate_request(policy, {"kind": "command", "summary": "python -m pytest -q tests"}) == "allow"
    assert evaluate_request(policy, {"kind": "command", "summary": "python -m pytest_extra"}) == "ask"
    assert evaluate_request(policy, {"kind": "command", "summary": "npm test -- silent"}) == "allow"
    # Outside scope → ask (never widen).
    assert evaluate_request(policy, {"kind": "command", "summary": "rm -rf /"}) == "ask"
    assert evaluate_request(policy, {"kind": "command", "summary": "curl evil.example"}) == "ask"
    assert evaluate_request(policy, {"kind": "mcp", "summary": "anything"}) == "ask"
    assert evaluate_request(None, {"kind": "command", "summary": "python -m pytest"}) == "ask"


def test_path_scope_matches_allowed_roots():
    policy = _policy()
    assert evaluate_request(policy, {"kind": "path", "summary": "E:/demo/src/app.py"}) == "allow"
    assert evaluate_request(policy, {"kind": "path", "summary": "E:/demo/other/secret.env"}) == "ask"
    assert evaluate_request(policy, {"kind": "path", "summary": "E:/demo/src-sibling/file.py"}) == "ask"


def test_unreadable_policy_is_an_error_not_silence(tmp_path):
    path = policy_module.policy_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(PolicyError):
        policy_module.load_policy(tmp_path)


def test_missing_policy_returns_none(tmp_path):
    assert policy_module.load_policy(tmp_path) is None


def test_tool_network_and_external_action_policy_fields():
    policy = _policy(
        allowed_tools=["write_file", "run_command"],
        network_targets=["api.github.com", "*.trusted.example", "https://hooks.example/v1"],
        external_actions=["HTTP POST", "git push"],
    )
    assert evaluate_request(policy, {"kind": "edit", "summary": "E:/demo/src/app.py"}) == "allow"
    assert evaluate_request(policy, {"kind": "edit", "summary": "E:/demo/other/app.py"}) == "ask"
    assert evaluate_request(policy, {"kind": "network", "summary": "https://api.github.com/repos/o/r"}) == "allow"
    assert evaluate_request(policy, {"kind": "network", "summary": "https://sub.trusted.example/data"}) == "allow"
    assert evaluate_request(policy, {"kind": "network", "summary": "https://hooks.example/v1/notify"}) == "allow"
    assert evaluate_request(policy, {"kind": "network", "summary": "https://evil.example/steal"}) == "ask"
    assert (
        evaluate_request(
            policy,
            {
                "kind": "mcp",
                "metadata": {"operation": "HTTP POST", "target": "https://api.github.com/repos/o/r/pulls"},
            },
        )
        == "allow"
    )
    assert (
        evaluate_request(
            policy,
            {
                "kind": "mcp",
                "metadata": {"operation": "HTTP DELETE", "target": "https://api.github.com/repos/o/r/pulls"},
            },
        )
        == "ask"
    )
    assert (
        evaluate_request(
            policy,
            {"kind": "command", "summary": "python -m pytest", "metadata": {"tool": "forbidden_tool"}},
        )
        == "ask"
    )

    check_policy_checkpoint(policy, kind="tool", tool="write_file", mutating=True)
    with pytest.raises(PermissionError, match="allowed_tools"):
        check_policy_checkpoint(policy, kind="tool", tool="delete_file", mutating=True)
    with pytest.raises(PermissionError, match="network_targets"):
        check_policy_checkpoint(policy, kind="network", url="https://evil.example/x", action="HTTP GET", mutating=False)
    with pytest.raises(PermissionError, match="external_actions"):
        check_policy_checkpoint(
            policy, kind="network", url="https://api.github.com/x", action="HTTP DELETE", mutating=True
        )


def test_policy_tightening_revocation_and_expiry_block_next_side_effect(tmp_path):
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from pepsicode.execution import external, process
    from pepsicode.execution.filesystem import FILES
    from pepsicode.execution.runtime import ExecutionJournal, ExecutionScope, execution_journal_dir
    from pepsicode.permissions import PermissionManager
    from pepsicode.tooling import ToolContext, ToolDefinition, ToolRegistry, ToolResult

    allowed_dir = tmp_path / "allowed"
    denied_dir = tmp_path / "denied"
    allowed_dir.mkdir()
    denied_dir.mkdir()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "https://evil.example/redirected")
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        initial = _policy(
            workspace=str(tmp_path),
            allowed_paths=[str(allowed_dir), str(denied_dir)],
            allowed_tools=["echo_tool"],
            allowed_commands=[f"{Path(sys.executable).stem} -c", "python -c"],
            network_targets=["127.0.0.1", "api.github.com"],
            external_actions=["HTTP POST"],
        )
        policy_module.save_policy(tmp_path, initial)

        permissions = PermissionManager(str(tmp_path), mode="full_access")
        try:
            scope = ExecutionScope(
                str(tmp_path),
                permissions=permissions,
                journal=ExecutionJournal(execution_journal_dir(tmp_path) / "policy.jsonl"),
                policy_checkpoint=lambda **kw: check_policy_checkpoint(policy_module.load_policy(tmp_path), **kw),
            )
            ctx = ToolContext(str(tmp_path), permissions=permissions, execution=scope)

            # Initial operations within scope succeed.
            FILES.write_text(ctx, denied_dir / "ok.txt", "before")
            tool = ToolDefinition(
                name="echo_tool",
                description="test tool",
                input_schema={},
                validator=lambda raw: raw,
                run=lambda args, context: ToolResult(True, "ok"),
            )
            registry = ToolRegistry([tool])
            assert registry.execute("echo_tool", {}, ctx).ok
            external.authorize(ctx, "HTTP POST", "https://api.github.com/repos/o/r", mutating=True)
            res = process.run([sys.executable, "-c", "print('ok')"], context=ctx, cwd=tmp_path, capture_output=True)
            assert res.returncode == 0

            # Tighten the policy on disk (drop denied_dir, echo_tool, api.github.com, and python -c).
            tightened = _policy(
                workspace=str(tmp_path),
                allowed_paths=[str(allowed_dir)],
                allowed_tools=["other_tool"],
                allowed_commands=["npm test"],
                network_targets=["127.0.0.1"],
                external_actions=[],
            )
            policy_module.save_policy(tmp_path, tightened)

            # Very next side-effect call in each boundary is blocked immediately.
            with pytest.raises(PermissionError, match="allowed_paths"):
                FILES.write_text(ctx, denied_dir / "blocked.txt", "after")
            assert not (denied_dir / "blocked.txt").exists()

            tool_res = registry.execute("echo_tool", {}, ctx)
            assert not tool_res.ok and tool_res.status == "blocked"

            with pytest.raises(PermissionError, match="allowed_commands"):
                process.run([sys.executable, "-c", "print('blocked')"], context=ctx, cwd=tmp_path, capture_output=True)

            with pytest.raises(PermissionError, match="network_targets"):
                external.authorize(ctx, "HTTP POST", "https://api.github.com/repos/o/r", mutating=True)

            # Redirect from an allowed host to an unauthorized host is blocked before following.
            with pytest.raises(PermissionError, match="network_targets"):
                external.urlopen(f"http://127.0.0.1:{port}/start", context=ctx, timeout=5)
        finally:
            permissions.close()
    finally:
        server.shutdown()
        server.server_close()
