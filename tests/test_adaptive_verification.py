import json
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from pepsicode.core.agent_loop import run_agent_turn, run_agent_turn_stream
from pepsicode.core.verification import ProjectVerifier, discover_checks, execution_argv, run_verified_turn
from pepsicode.tooling import ToolCapability, ToolContext, ToolDefinition, ToolRegistry, ToolResult
from pepsicode.tools.ask_user import ask_user_tool
from pepsicode.tools.write_file import write_file_tool
from pepsicode.types import AgentStep, StreamToken


def manifest(path: Path, **data):
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(json.dumps(data), encoding="utf-8")


def test_javascript_tests_directory_does_not_imply_pytest(tmp_path):
    manifest(tmp_path, scripts={"test": "node --test"})
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "feature.test.js").write_text("// node test")
    assert [check.argv for check in discover_checks(str(tmp_path))] == [["npm", "run", "test"]]


@pytest.mark.parametrize("manager", ["pnpm", "yarn", "bun"])
def test_nested_packages_inherit_declared_package_manager(tmp_path, manager):
    manifest(tmp_path, packageManager=f"{manager}@1.2.3", workspaces=["packages/*"])
    package = tmp_path / "packages" / "apps" / "admin"
    manifest(package, scripts={"verify:types": "tsc", "test:ci": "vitest run", "dev": "vite"})
    checks = discover_checks(str(tmp_path), discovery={"scripts": ["verify:types", "test:ci"]})
    assert [check.argv for check in checks] == [[manager, "run", "verify:types"], [manager, "run", "test:ci"]]
    assert all(check.cwd == str(package) and check.env["CI"] == "1" for check in checks)


def test_package_manager_conflicts_require_an_explicit_choice(tmp_path):
    manifest(tmp_path, scripts={"test": "runner"})
    (tmp_path / "yarn.lock").touch()
    (tmp_path / "pnpm-lock.yaml").touch()
    with pytest.raises(ValueError, match="Conflicting"):
        discover_checks(str(tmp_path))
    assert discover_checks(str(tmp_path), discovery={"packageManager": "pnpm"})[0].argv[0] == "pnpm"


def test_discovery_depth_and_exclusions_are_configurable(tmp_path):
    package = tmp_path / "apps" / "mobile" / "ui"
    manifest(package, scripts={"test": "runner"})
    assert not discover_checks(str(tmp_path), discovery={"maxDepth": 2})
    assert discover_checks(str(tmp_path), discovery={"maxDepth": 3})
    assert not discover_checks(str(tmp_path), discovery={"exclude": ["apps/mobile"]})
    with pytest.raises(ValueError, match="directory limit"):
        discover_checks(str(tmp_path), discovery={"maxDirectories": 1})


def test_custom_commands_replace_discovery_and_support_empty_arguments_env_timeout(tmp_path):
    manifest(tmp_path, scripts={"test": "must not run"})
    checks = discover_checks(
        str(tmp_path), [{"command": [sys.executable, "-c", "", ""], "env": {"WORKFLOW": "custom"}, "timeout": 17}]
    )
    assert len(checks) == 1 and checks[0].argv[-1] == ""
    assert checks[0].env == {"WORKFLOW": "custom"} and checks[0].timeout == 17
    assert discover_checks(str(tmp_path), []) == []


@pytest.mark.parametrize("manager", ["npm", "pnpm", "yarn", "bun"])
def test_windows_package_manager_adapter_preserves_safe_arguments(manager):
    argv = [manager, "run", "test:ci", "--", "--config", "config folder/test.json"]
    converted = execution_argv(argv, windows=True)
    assert converted[:4] == ["cmd", "/d", "/s", "/c"]
    assert '"config folder/test.json"' in converted[-1]
    assert execution_argv(argv, windows=False) == argv
    with pytest.raises(ValueError, match="control"):
        execution_argv([manager, "run", "test&other-command"], windows=True)


def test_verifier_collects_all_failures_and_does_not_hide_them_behind_empty_tests(tmp_path):
    marker = tmp_path / "checked"
    commands = [
        {"name": "first", "command": [sys.executable, "-c", "raise ValueError('first')"]},
        {"name": "empty", "command": [sys.executable, "-c", "raise SystemExit(5)"], "noTestsExitCodes": [5]},
        {
            "name": "last",
            "command": [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch(); raise ValueError('last')",
            ],
        },
    ]
    tools = ToolRegistry([])
    result = ProjectVerifier({"verificationMode": "always", "verificationCommands": commands}, tools).verify(
        messages=[], tools=tools, cwd=str(tmp_path), permissions=None
    )
    assert marker.exists() and result.status == "failed"
    assert "first" in result.reason and "last" in result.reason
    assert "empty: not_run" in result.details


def test_arbitrary_command_output_does_not_define_test_collection_status(tmp_path):
    tools = ToolRegistry([])
    runtime = {
        "verificationMode": "always",
        "verificationCommands": [{"command": [sys.executable, "-c", "print('no tests ran: documentation example')"]}],
    }
    result = ProjectVerifier(runtime, tools).verify(messages=[], tools=tools, cwd=str(tmp_path), permissions=None)
    assert result.ok


@pytest.mark.parametrize("runner", [run_agent_turn, run_agent_turn_stream])
def test_agent_clarification_pauses_before_checks_and_retries(tmp_path, runner):
    class Model:
        calls = 0

        def step(self):
            self.calls += 1
            if self.calls == 1:
                return "write_file", {"path": "draft.txt", "content": "working draft"}
            return "ask_user", {"question": "Which workflow should the draft support?"}

        def next(self, messages):
            name, data = self.step()
            return AgentStep(type="tool_calls", calls=[{"id": str(self.calls), "toolName": name, "input": data}])

        def next_stream(self, messages):
            name, data = self.step()
            yield StreamToken(type="tool_use", tool_name=name, tool_id=str(self.calls))
            yield StreamToken(type="tool_use", tool_input_partial=json.dumps(data))
            yield StreamToken(type="done")

    marker = tmp_path / "must-not-run"
    runtime = {
        "verificationMode": "always",
        "verificationCommands": [
            {"command": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}
        ],
    }
    model = Model()
    messages = run_verified_turn(
        runtime=runtime,
        runner=runner,
        model=model,
        tools=ToolRegistry([write_file_tool, ask_user_tool]),
        messages=[{"role": "user", "content": "Explore a new workflow"}],
        cwd=str(tmp_path),
    )
    assert model.calls == 2 and messages[-1]["awaitingUser"]
    assert runtime["_lastVerification"]["reason_code"] == "awaiting_user"
    assert not marker.exists() and (tmp_path / "draft.txt").exists()


def test_new_integration_can_trigger_verification_even_when_it_raises(tmp_path):
    def generate(value, context):
        (tmp_path / "generated.txt").write_text("partial output")
        raise RuntimeError("failed after writing")

    tools = ToolRegistry([ToolDefinition("third_party_generator", "", {}, lambda value: value, generate)])
    runtime = {"verificationCommands": [{"command": [sys.executable, "-c", "assert True"]}]}

    def agent(**options):
        assert not tools.execute("third_party_generator", {}, ToolContext(str(tmp_path))).ok
        return [{"role": "assistant", "content": "Partial generation reported"}]

    run_verified_turn(runtime=runtime, runner=agent, model=object(), tools=tools, messages=[], cwd=str(tmp_path))
    assert tools.mutation_count == 1 and runtime["_lastVerification"]["status"] == "passed"


def test_tool_impact_can_be_dynamic_without_name_lists(tmp_path):
    tools = ToolRegistry(
        [
            ToolDefinition(
                "project_extension",
                "",
                {},
                lambda value: value,
                lambda value, context: ToolResult(True, "done"),
                affects_workspace=lambda value: value["operation"] == "write",
            )
        ]
    )
    for operation in ("read", "write"):
        tools.execute("project_extension", {"operation": operation}, ToolContext(str(tmp_path)))
    assert tools.mutation_count == 1


def test_read_only_tool_never_implies_a_workspace_change(tmp_path):
    tool = ToolDefinition(
        "new_reader",
        "",
        {},
        lambda value: value,
        lambda value, context: ToolResult(True, "done"),
        capabilities={ToolCapability.READ_ONLY},
    )
    tools = ToolRegistry([tool])
    tools.execute("new_reader", {}, ToolContext(str(tmp_path)))
    assert tools.mutation_count == 0


def test_retry_budget_is_not_silently_capped_at_ten_and_patience_is_configurable(tmp_path):
    count = []
    runtime = {
        "verificationMode": "always",
        "maxIterations": 12,
        "verificationNoProgressLimit": 15,
        "verificationCommands": [{"command": [sys.executable, "-c", "raise SystemExit(1)"]}],
    }

    def agent(**options):
        count.append(1)
        return [*options["messages"], {"role": "assistant", "content": "Attempt complete"}]

    messages = run_verified_turn(
        runtime=runtime, runner=agent, model=object(), tools=ToolRegistry([]), messages=[], cwd=str(tmp_path)
    )
    assert len(count) == 12 and messages[-1]["isError"]


def test_hook_denial_is_blocked_without_automatic_retries(tmp_path):
    hooks = Mock()
    hooks.evaluate_pre_tool.return_value = Mock(allowed=False, reason="requires workflow review", hook_id="project")
    agent = Mock(return_value=[{"role": "assistant", "content": "done"}])
    runtime = {
        "verificationMode": "always",
        "verificationCommands": [{"command": [sys.executable, "-c", "raise AssertionError('must not execute')"]}],
    }
    run_verified_turn(
        runtime=runtime,
        runner=agent,
        model=object(),
        tools=ToolRegistry([]),
        messages=[],
        cwd=str(tmp_path),
        hook_engine=hooks,
    )
    assert agent.call_count == 1 and runtime["_lastVerification"]["status"] == "blocked"


def test_pending_edits_are_verified_after_clarification_even_without_another_edit(tmp_path):
    tools = ToolRegistry([write_file_tool])
    marker = tmp_path / "verified"
    runtime = {
        "verificationCommands": [
            {"command": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}
        ]
    }

    def draft(**options):
        tools.execute("write_file", {"path": "draft.txt", "content": "draft"}, ToolContext(str(tmp_path)))
        return [{"role": "assistant", "content": "Choose a workflow", "awaitingUser": True}]

    options = dict(runtime=runtime, model=object(), tools=tools, messages=[], cwd=str(tmp_path))
    run_verified_turn(runner=draft, **options)
    assert not marker.exists() and runtime["_lastVerification"]["reason_code"] == "awaiting_user"
    answer = Mock(return_value=[{"role": "assistant", "content": "Applied the confirmed choice"}])
    run_verified_turn(runner=answer, **options)
    assert marker.exists() and runtime["_lastVerification"]["status"] == "passed"
    run_verified_turn(runner=answer, **options)
    assert runtime["_lastVerification"]["reason_code"] == "no_changes"


def test_literal_argv_preserves_executable_paths_and_does_not_infer_shells(tmp_path, monkeypatch):
    from pepsicode.tools import run_command

    executable = str(tmp_path / "a folder" / "tool&name.exe")
    monkeypatch.setattr(
        run_command,
        "_run_cancellable_command",
        lambda command, args, **options: ToolResult(True, json.dumps([command, *args])),
    )
    tools = ToolRegistry([run_command.run_command_tool])
    context = ToolContext(str(tmp_path), command_is_argv=True, cancellation_event=threading.Event())
    for args in ([], ["", "literal&argument"]):
        result = tools.execute("run_command", {"command": executable, "args": args}, context)
        assert result.ok and json.loads(result.output) == [executable, *args]
