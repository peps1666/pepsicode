"""One isolated evaluation process. Connection credentials arrive over stdin."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from pepsicode.context.context_manager import ContextManager, load_context_state, save_context_state
from pepsicode.context.prompt import build_system_prompt
from pepsicode.core.agent_loop import run_agent_turn
from pepsicode.core.verification import run_verified_turn
from pepsicode.evals.cases import CASES, MONEY_REQUIREMENTS, SMOKE
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter, _normalize_usage
from pepsicode.llm.cost_tracker import MODEL_PRICING, BudgetExceededError, CostTracker
from pepsicode.permissions import PermissionManager
from pepsicode.sandbox import format_command_signature
from pepsicode.tooling import ToolCapability, ToolContext, ToolDefinition, ToolRegistry
from pepsicode.tools.ask_user import ask_user_tool
from pepsicode.tools.edit_file import edit_file_tool
from pepsicode.tools.grep_files import grep_files_tool
from pepsicode.tools.list_files import list_files_tool
from pepsicode.tools.patch_file import patch_file_tool
from pepsicode.tools.read_context_artifact import read_context_artifact_tool
from pepsicode.tools.read_file import read_file_tool
from pepsicode.tools.run_command import run_command_tool
from pepsicode.tools.task_state import task_state_tool
from pepsicode.tools.todo_write import todo_write_tool
from pepsicode.tools.write_file import write_file_tool
from pepsicode.types import AgentStep


class MeasuredAdapter(AnthropicModelAdapter):
    def __init__(self, runtime, tools, *, max_requests, budget, pricing=None):
        super().__init__(runtime, tools)
        self.requests = []
        self.costs = CostTracker()
        self.max_requests = max_requests
        self.budget = budget
        self.limit_reason = None
        self.pricing = pricing or {}

    def _send(self, request):
        if len(self.requests) >= self.max_requests:
            self.limit_reason = "request_limit"
            raise RuntimeError("Evaluation model request limit reached")
        if self.costs.total_cost_usd >= self.budget:
            self.limit_reason = "budget_limit"
            raise BudgetExceededError(self.budget, self.costs.total_cost_usd)
        entry = {
            "model": json.loads(request.data)["model"],
            "usage": None,
            "duration_ms": None,
            "estimated_cost_usd": None,
            "error_kind": None,
        }
        self.requests.append(entry)
        started = time.monotonic()
        try:
            data, status = super()._send(request)
            entry["http_status"] = status
            usage = data.get("usage") if isinstance(data, dict) else None
            if isinstance(usage, dict) and all(
                isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool) and usage[key] >= 0
                for key in ("input_tokens", "output_tokens")
            ):
                entry["usage"] = _normalize_usage(usage)
                default_cost = self.costs.add_usage(
                    entry["model"],
                    usage["input_tokens"],
                    usage["output_tokens"],
                    cache_read_tokens=entry["usage"].get("cache_read_tokens", 0),
                    cache_write_tokens=entry["usage"].get("cache_write_tokens", 0),
                )
                rates = self.pricing.get(entry["model"]) or MODEL_PRICING.get(entry["model"])
                if rates is not None:
                    cost = sum(
                        entry["usage"].get(key, 0) * rates[rate] / 1_000_000
                        for key, rate in (
                            ("input_tokens", "input"),
                            ("output_tokens", "output"),
                            ("cache_read_tokens", "cache_read"),
                            ("cache_write_tokens", "cache_write"),
                        )
                    )
                    entry["estimated_cost_usd"] = cost
                    self.costs.total_cost_usd += cost - default_cost
            return data, status
        except Exception as error:
            entry["error_kind"] = type(error).__name__
            raise
        finally:
            entry["duration_ms"] = round((time.monotonic() - started) * 1_000, 2)


class SmokeModel:
    """A scripted tool sequence solely for checking the evaluation plumbing."""

    last_usage = None

    def __init__(self):
        self.index = 0

    def next(self, _messages):
        self.index += 1
        if self.index == 1:
            return AgentStep(
                type="tool_calls",
                calls=[
                    {
                        "id": "smoke-write",
                        "toolName": "write_file",
                        "input": {"path": "calculation.py", "content": "def add(a, b):\n    return a + b\n"},
                    }
                ],
            )
        if self.index == 2:
            return AgentStep(type="tool_calls", calls=[{"id": "smoke-tests", "toolName": "test_runner", "input": {}}])
        return AgentStep(type="assistant", content="Implemented addition.", kind="final")


def test_command() -> list[str]:
    # -B avoids stale bytecode when an edit has the same size and timestamp.
    return [sys.executable, "-B", "test_public.py"]


def evaluation_tools(workspace: str, permissions: PermissionManager) -> ToolRegistry:
    def validate_test_input(value):
        if not isinstance(value, dict) or value:
            raise ValueError("Expected an empty argument object")
        return value

    def run_tests(_value, context):
        argv = test_command()
        return run_command_tool.run(
            {"command": argv[0], "args": argv[1:], "cwd": workspace, "timeout": 30},
            ToolContext(
                cwd=workspace,
                permissions=permissions,
                command_is_argv=True,
                cancellation_event=context.cancellation_event,
            ),
        )

    test_tool = ToolDefinition(
        name="test_runner",
        description="Run this project's public unittest suite. Takes no arguments.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        validator=validate_test_input,
        run=run_tests,
        affects_workspace=False,
    )
    reads = [read_file_tool, list_files_tool, grep_files_tool, read_context_artifact_tool]
    for tool in reads:
        tool.capabilities.add(ToolCapability.READ_ONLY)
    return ToolRegistry(
        [
            *reads,
            write_file_tool,
            edit_file_tool,
            patch_file_tool,
            todo_write_tool,
            task_state_tool,
            ask_user_tool,
            test_tool,
        ]
    )


def seed_context(workspace: str, *, constraints: str) -> ContextManager:
    manager = ContextManager(context_window=16_000)
    manager.recovery_state.workspace = workspace
    manager.messages = [
        {"role": "system", "content": build_system_prompt(workspace)},
        {
            "role": "user",
            "content": "Earlier investigation notes. " + "Diagnostic log, no requirements. " * 600 + "\n" + constraints,
        },
    ]
    manager.messages.extend({"role": "assistant", "content": "Investigation log. " * 100} for _ in range(40))
    manager.recovery_state.current_tasks = ["Implement pricing.py, then run public checks"]
    return manager


def run_worker(payload: dict) -> dict:
    workspace = str(Path(payload["workspace"]).resolve())
    os.chdir(workspace)
    case = SMOKE if payload["mode"] == "smoke" else CASES[payload["case"]]
    if payload.get("phase") == "prepare":
        # Fixture checkpoint: persisted by one process, reopened by another.
        manager = seed_context(workspace, constraints=MONEY_REQUIREMENTS)
        save_context_state(manager)
        return {"status": "prepared"}

    signature = format_command_signature(test_command()[0], test_command()[1:])
    decisions = []

    def approve(request):
        allow = request.get("kind") == "command" and request.get("scope") == signature
        decisions.append({"kind": request.get("kind"), "allowed": allow})
        return {"decision": "allow_once" if allow else "deny_once"}

    permissions = PermissionManager(workspace, prompt=approve, mode="auto")
    tools = evaluation_tools(workspace, permissions)
    runtime = dict(payload.get("runtime", {}))
    argv = test_command()
    runtime.update(
        {
            "verificationMode": "always",
            "maxIterations": 3,
            "verificationCommands": [{"name": "public unittest", "command": argv, "timeout": 30}],
            "taskAcceptance": [{"id": "public-behavior", "kind": "command", "argv": argv}],
        }
    )
    smoke = payload["mode"] == "smoke"
    model = (
        SmokeModel()
        if smoke
        else MeasuredAdapter(
            runtime,
            tools,
            max_requests=payload["max_requests"],
            budget=payload["budget"],
            pricing=payload.get("pricing"),
        )
    )
    restored = case.scenario == "resume"
    if restored:
        manager = load_context_state()
        if manager is None or manager.recovery_state.workspace != workspace:
            raise ValueError("Evaluation checkpoint is missing or belongs to another workspace")
    elif case.scenario == "compaction":
        manager = seed_context(workspace, constraints=MONEY_REQUIREMENTS)
    else:
        manager = ContextManager(context_window=16_000)
        manager.messages = [{"role": "system", "content": build_system_prompt(workspace)}]
    if not smoke:
        manager.summarizer = model.summarize
    if case.scenario in {"compaction", "resume"}:
        rounds = 2 if case.scenario == "compaction" else 1
        for index in range(rounds):
            if index:
                manager.messages.append(
                    {
                        "role": "user",
                        "content": "Continue with all earlier constraints. "
                        + "Additional diagnostic log. " * 600
                        + "\nRound only once at the end.",
                    }
                )
                manager.messages.extend(
                    {"role": "assistant", "content": "Investigation log. " * 100} for _ in range(40)
                )
            manager.compact_messages(force=True)
            if not manager.last_compaction_changed:
                last_request = model.requests[-1] if model.requests else {}
                infrastructure_error = last_request.get("error_kind") or last_request.get("http_status", 200) >= 400
                return {
                    "status": "error" if infrastructure_error else "compaction_failed",
                    "failure_reason": model.limit_reason or "compaction_failed",
                    "requests": model.requests,
                    "compaction_error": manager.last_compaction_error,
                    "compactions": [item.to_dict() for item in manager.compact_boundaries],
                }
    manager.messages.append({"role": "user", "content": case.prompt})
    reports = []
    started = time.monotonic()
    messages = run_verified_turn(
        runtime=runtime,
        runner=run_agent_turn,
        model=model,
        tools=tools,
        messages=manager.messages,
        cwd=workspace,
        permissions=permissions,
        context_manager=manager,
        max_steps=payload["max_steps"],
        on_verification=reports.append,
    )
    last = messages[-1] if messages else {}
    status = "completed"
    if last.get("awaitingUser"):
        status = "awaiting_user"
    elif getattr(model, "limit_reason", None):
        status = model.limit_reason
    elif last.get("isError"):
        status = (
            "verification_failed"
            if reports and reports[-1].get("status") == "failed"
            else "step_limit"
            if "maximum tool step limit" in last.get("content", "")
            else "error"
        )
    result = {
        "status": status,
        "failure_reason": getattr(model, "limit_reason", None) or (status if status != "completed" else None),
        "duration_seconds": round(time.monotonic() - started, 3),
        "requests": [] if smoke else model.requests,
        "estimated_cost_usd": sum(request["estimated_cost_usd"] for request in model.requests)
        if not smoke and model.requests and all(request["estimated_cost_usd"] is not None for request in model.requests)
        else None,
        "budget_spend_estimate_usd": None if smoke else model.costs.total_cost_usd,
        "verification": reports,
        "approvals": decisions,
        "human_interventions": int(status == "awaiting_user"),
        "compactions": [boundary.to_dict() for boundary in manager.compact_boundaries],
        "resume_process_reopened": restored,
        "tool_calls": sum(message.get("role") == "assistant_tool_call" for message in messages),
        "tool_errors": sum(
            message.get("role") == "tool_result" and message.get("isError", False) for message in messages
        ),
        "messages": messages,
    }
    return result


def main() -> None:
    payload = json.load(sys.stdin)
    try:
        result = run_worker(payload)
    except Exception as error:  # the parent keeps infrastructure errors out of pass rates
        result = {"status": "error", "failure_reason": f"worker_{type(error).__name__}"}
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    for key in ("apiKey", "authToken"):
        value = payload.get("runtime", {}).get(key)
        if value:
            serialized = serialized.replace(json.dumps(str(value), ensure_ascii=False)[1:-1], "[REDACTED]")
    Path(payload["result_path"]).write_text(serialized, encoding="utf-8")


if __name__ == "__main__":
    main()
