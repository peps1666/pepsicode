import json
import sys
import threading
from unittest.mock import Mock

import pytest

from pepsicode.core.verification import ProjectVerifier, discover_checks, run_verified_turn
from pepsicode.permissions import PermissionManager
from pepsicode.tooling import ToolContext, ToolRegistry
from pepsicode.tools.write_file import write_file_tool


def test_discovers_python_and_nested_frontend_checks(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_behavior.py").write_text("def test_ok(): assert True\n")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps(
            {"scripts": {"dev": "vite", "typecheck": "tsc --noEmit", "test": "node --test", "build": "vite build"}}
        )
    )
    checks = discover_checks(str(tmp_path))
    assert [check.name for check in checks] == [
        "Python tests",
        "frontend: typecheck",
        "frontend: test",
        "frontend: build",
    ]
    assert all("dev" not in check.argv for check in checks)


def test_no_checks_is_not_a_success(tmp_path):
    tools = ToolRegistry([])
    verifier = ProjectVerifier({"verificationMode": "always"}, tools)
    result = verifier.verify(messages=[], tools=tools, cwd=str(tmp_path), permissions=None)
    assert not result.ok and result.status == "not_run"


def test_auto_skips_read_only_turn_and_off_mode_never_executes(tmp_path):
    for mode in ("auto", "off"):
        tools = ToolRegistry([])
        verifier = ProjectVerifier(
            {"verificationMode": mode, "verificationCommands": [{"command": ["must-not-execute"]}]}, tools
        )
        assert verifier.verify(messages=[], tools=tools, cwd=str(tmp_path), permissions=None).status == "not_run"


def test_actual_failure_is_fed_back_and_fixed_then_reverified(tmp_path):
    tools = ToolRegistry([write_file_tool])
    reports = []
    calls = []
    runtime = {
        "verificationMode": "auto",
        "verificationCommands": [
            {
                "name": "addition behavior",
                "command": [sys.executable, "-c", "from calculation import add; assert add(2, 3) == 5"],
            }
        ],
    }

    def agent(**options):
        calls.append(options["messages"])
        content = "def add(a, b): return a - b\n" if len(calls) == 1 else "def add(a, b): return a + b\n"
        result = tools.execute("write_file", {"path": "calculation.py", "content": content}, ToolContext(str(tmp_path)))
        assert result.ok, result.output
        return [*options["messages"], {"role": "assistant", "content": "implemented"}]

    messages = run_verified_turn(
        runtime=runtime,
        runner=agent,
        model=object(),
        tools=tools,
        messages=[{"role": "user", "content": "implement addition"}],
        cwd=str(tmp_path),
        on_verification=reports.append,
    )
    assert len(calls) == 2
    assert "AssertionError" in calls[1][-1]["content"]
    assert [report["status"] for report in reports] == ["failed", "passed"]
    assert runtime["_lastVerification"]["ok"]
    assert not messages[-1].get("isError")


def test_different_failure_details_do_not_trigger_false_no_progress(tmp_path):
    tools = ToolRegistry([])
    count = []
    runtime = {
        "verificationMode": "always",
        "verificationCommands": [
            {
                "command": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; value=Path('error.txt').read_text(); assert value == 'ok', value",
                ]
            }
        ],
    }

    def agent(**options):
        count.append(1)
        (tmp_path / "error.txt").write_text(["first failure", "second failure", "ok"][len(count) - 1])
        return [*options["messages"], {"role": "assistant", "content": "done"}]

    run_verified_turn(runtime=runtime, runner=agent, model=object(), tools=tools, messages=[], cwd=str(tmp_path))
    assert len(count) == 3 and runtime["_lastVerification"]["status"] == "passed"


def test_same_failure_stops_and_reports_unverified_result(tmp_path):
    calls = []
    runtime = {
        "verificationMode": "always",
        "verificationCommands": [{"command": [sys.executable, "-c", "raise RuntimeError('same failure')"]}],
    }

    def agent(**options):
        calls.append(1)
        return [*options["messages"], {"role": "assistant", "content": "done"}]

    result = run_verified_turn(
        runtime=runtime, runner=agent, model=object(), tools=ToolRegistry([]), messages=[], cwd=str(tmp_path)
    )
    assert len(calls) == 2
    assert result[-1]["isError"] and "no_progress" in result[-1]["content"]


def test_verification_permission_denial_is_blocked_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("pepsicode.approval.store.PEPSI_CODE_PERMISSIONS_PATH", tmp_path / "permissions.json")
    marker = tmp_path / "must-not-exist"
    runtime = {
        "verificationMode": "always",
        "verificationCommands": [
            {"command": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}
        ],
    }
    agent = Mock(return_value=[{"role": "assistant", "content": "done"}])
    permissions = PermissionManager(str(tmp_path), mode="auto", prompt=lambda request: {"decision": "deny_once"})
    run_verified_turn(
        runtime=runtime,
        runner=agent,
        model=object(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=str(tmp_path),
        permissions=permissions,
    )
    assert runtime["_lastVerification"]["status"] == "blocked"
    assert agent.call_count == 1 and not marker.exists()


def test_cancellation_does_not_launch_verification_or_retry(tmp_path):
    event = threading.Event()
    marker = tmp_path / "must-not-exist"
    runtime = {
        "verificationMode": "always",
        "verificationCommands": [
            {"command": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}
        ],
    }

    def agent(**options):
        event.set()
        return [{"role": "assistant", "content": "Task stopped by user."}]

    run_verified_turn(
        runtime=runtime,
        runner=agent,
        model=object(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=str(tmp_path),
        cancellation_event=event,
    )
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["read_only", "plan"])
def test_read_only_and_plan_never_execute_verification(tmp_path, mode):
    permissions = PermissionManager(str(tmp_path), mode="read_only" if mode == "read_only" else "auto")
    if mode == "plan":
        permissions.enter_plan_mode()
    tools = ToolRegistry([])
    verifier = ProjectVerifier({"verificationMode": "always"}, tools)
    assert verifier.verify(messages=[], tools=tools, cwd=str(tmp_path), permissions=permissions).status == "not_run"


def test_configuration_rejects_shell_strings_and_outside_paths(tmp_path):
    with pytest.raises(ValueError, match="argv"):
        discover_checks(str(tmp_path), [{"command": "echo unsafe && do-other-things"}])
    with pytest.raises(ValueError):
        discover_checks(str(tmp_path), [{"command": ["echo", "hello"], "cwd": ".."}])


def test_verification_respects_pre_tool_hook_denial(tmp_path):
    marker = tmp_path / "must-not-exist"
    runtime = {
        "verificationMode": "always",
        "maxIterations": 1,
        "verificationCommands": [
            {"command": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]},
        ],
    }
    hooks = Mock()
    hooks.evaluate_pre_tool.return_value = Mock(allowed=False, reason="blocked by project", hook_id="policy")
    run_verified_turn(
        runtime=runtime,
        runner=Mock(return_value=[{"role": "assistant", "content": "done"}]),
        model=object(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=str(tmp_path),
        hook_engine=hooks,
    )
    assert not marker.exists()
    assert runtime["_lastVerification"]["status"] == "blocked"
    assert "blocked by project" in runtime["_lastVerification"]["details"]
