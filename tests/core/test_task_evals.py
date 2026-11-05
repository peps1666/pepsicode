from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pepsicode.evals.cases import CASES, MONEY_REQUIREMENTS, SMOKE, materialize, protected_files_intact
from pepsicode.evals.runner import aggregate, evaluate, grade, isolated_environment, run_process
from pepsicode.evals.worker import MeasuredAdapter
from pepsicode.llm.anthropic_adapter import AnthropicModelAdapter
from pepsicode.tooling import ToolRegistry

SOLUTIONS = {
    "pagination": {
        "pagination.py": "def paginate(items, page, size):\n    if page <= 0 or size <= 0:\n        raise ValueError('positive pages and sizes required')\n    return items[(page - 1) * size:page * size]\n",
    },
    "cross_file": {
        "orders.py": "def create(identifier, amount):\n    return {'id': identifier, 'amount': amount, 'status': 'pending'}\ndef cancel(order):\n    if order['status'] == 'paid':\n        raise ValueError('already paid')\n    return {**order, 'status': 'cancelled'}\n",
        "reporting.py": "def summary(orders):\n    return {'count': sum(o['status'] != 'cancelled' for o in orders), 'revenue': sum(o['amount'] for o in orders if o['status'] == 'paid')}\n",
    },
}
MONEY_SOLUTION = "from decimal import Decimal, ROUND_HALF_UP\ndef total(prices, discount):\n    values = [Decimal(p) for p in prices]\n    rate = Decimal(discount)\n    if not 0 <= rate <= 1 or any(p < 0 for p in values):\n        raise ValueError('invalid money input')\n    result = (sum(values, Decimal(0)) * (1 - rate)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)\n    return format(result, '.2f')\n"


@pytest.fixture
def faux_endpoint():
    """Exercise real HTTP + worker integration with an explicitly scripted provider."""
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(body)
            if not body.get("tools"):
                content = [{"type": "text", "text": MONEY_REQUIREMENTS}]
                stop = "end_turn"
            else:
                history = body["messages"]
                case = next(name for name in CASES if f"{name}-001" in body["system"])
                used = any(block.get("type") == "tool_use" for message in history for block in message["content"])
                if case == "multiple_unknown":
                    calls = [b for m in history for b in m["content"] if b.get("type") == "tool_use"]
                    reconciled = sum(c["input"].get("action") == "reconcile" for c in calls)
                    if reconciled == 2:
                        content, stop = [{"type": "text", "text": "Both calls reconciled without replay"}], "end_turn"
                    else:
                        value = {"action": "observe", "path": "receipt.txt"}
                        if calls and calls[-1]["input"].get("action") == "observe":
                            outputs = [b for m in history for b in m["content"] if b.get("type") == "tool_result"]
                            raw = outputs[-1]["content"]
                            text = raw if isinstance(raw, str) else "".join(b.get("text", "") for b in raw)
                            observed = None
                            for index, char in enumerate(text):
                                if char != "{":
                                    continue
                                try:
                                    value_json, _ = json.JSONDecoder().raw_decode(text[index:])
                                    if "recovery_revision" in value_json and "id" in value_json:
                                        observed = value_json
                                        break
                                except (ValueError, TypeError):
                                    continue
                            assert observed, text
                            value = {
                                "action": "reconcile",
                                "call_id": ("seeded-first", "seeded-second")[reconciled],
                                "revision": observed["recovery_revision"],
                                "observation_ids": [observed["id"]],
                                "outcome": "occurred",
                                "evidence": "receipt shows committed result",
                            }
                        content = [
                            {
                                "type": "tool_use",
                                "id": f"recover-{len(calls)}",
                                "name": "execution_status",
                                "input": value,
                            }
                        ]
                        stop = "tool_use"
                elif case == "steering" and used:
                    calls = [b for m in history for b in m["content"] if b.get("type") == "tool_use"]
                    if any(c.get("name") == "task_state" for c in calls):
                        content, stop = [{"type": "text", "text": "Updated and rechecked 43"}], "end_turn"
                    else:
                        texts = [
                            block.get("text", "")
                            for message in history
                            for block in message["content"]
                            if block.get("type") == "text" and "Current task contract: " in block.get("text", "")
                        ]
                        contract, _ = json.JSONDecoder().raw_decode(texts[-1].split("Current task contract: ", 1)[1])
                        content = [
                            {
                                "type": "tool_use",
                                "id": "changed-goal",
                                "name": "write_file",
                                "input": {"path": "goal.py", "content": "def value():\n    return 43\n"},
                            },
                            {
                                "type": "tool_use",
                                "id": "revise-goal",
                                "name": "task_state",
                                "input": {
                                    "action": "revise",
                                    "task_id": contract["id"],
                                    "revision": contract["revision"],
                                    "instruction_id": contract["inputs"][-1],
                                    "reason": "human changed the target to 43",
                                    "criteria": [
                                        {"id": "value", "kind": "file_contains", "path": "goal.py", "text": "return 43"}
                                    ],
                                },
                            },
                        ]
                        stop = "tool_use"
                elif case == "analysis":
                    content = [{"type": "text", "text": "Pages start at 1. (page - 1) * size is the offset."}]
                    stop = "end_turn"
                elif used:
                    content = [{"type": "text", "text": "<final>Implemented and tested.</final>"}]
                    stop = "end_turn"
                else:
                    files = SOLUTIONS.get(case, {"pricing.py": MONEY_SOLUTION})
                    if case in {"pagination_zh", "mixed"}:
                        files = SOLUTIONS["pagination"]
                    elif case == "document":
                        files = {"guide.md": "# CSV 导出\n正确处理引号、逗号和换行。\n"}
                    elif case == "no_tests":
                        files = {"slug.py": "def slug(value):\n    return value.strip().lower().replace(' ', '-')\n"}
                    elif case == "steering":
                        files = {"goal.py": "def value():\n    return 42\n"}
                    content = [
                        {
                            "type": "tool_use",
                            "id": f"write-{index}",
                            "name": "write_file",
                            "input": {"path": path, "content": text},
                        }
                        for index, (path, text) in enumerate(files.items())
                    ]
                    if case != "steering":
                        content.append({"type": "tool_use", "id": "test", "name": "test_runner", "input": {}})
                    stop = "tool_use"
            data = json.dumps(
                {"content": content, "stop_reason": stop, "usage": {"input_tokens": 100, "output_tokens": 20}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            {
                "model": "faux-eval",
                "_evaluationSource": "scripted_service",
                "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                "apiKey": "eval-test-secret",
            },
            bodies,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_complete_cohort_uses_actual_agent_http_checks_compaction_and_process_resume(tmp_path, faux_endpoint):
    runtime, bodies = faux_endpoint
    report = evaluate(tmp_path / "cohort", mode="real", cases=list(CASES), runtime=runtime, timeout=40)

    summary = report["summary"]
    assert summary["cohort_complete"], [(run["case"], run["failure_reason"]) for run in report["runs"]]
    assert summary["success_rate"] is None
    assert summary["behavior_success_rate"] == 1.0
    assert report["evaluation_kind"] == "scripted_service"
    assert summary["usage_complete"]
    assert summary["input_tokens"] == len(bodies) * 100
    assert summary["output_tokens"] == len(bodies) * 20
    assert summary["estimated_cost_usd"] is None  # unknown faux-model prices must not become real cost data
    assert all(not run["baseline"]["passed"] and run["hidden_checks"]["passed"] for run in report["runs"])
    compaction = next(run for run in report["runs"] if run["case"] == "compaction")
    assert len(compaction["compactions"]) >= 2
    assert any(len(body["messages"][0]["content"][0]["text"]) > 1_000 for body in bodies if not body.get("tools"))
    assert next(run for run in report["runs"] if run["case"] == "resume")["resume_process_reopened"]
    assert next(run for run in report["runs"] if run["case"] == "interrupted_resume")["checkpoint_process_interrupted"]
    assert next(run for run in report["runs"] if run["case"] == "multiple_unknown")["internal_claimed_complete"]
    assert "eval-test-secret" not in (tmp_path / "cohort" / "protocol.json").read_text(encoding="utf-8")
    for path in (tmp_path / "cohort").rglob("*.json"):
        assert "eval-test-secret" not in path.read_text(encoding="utf-8")


def test_smoke_runs_offline_but_never_claims_a_model_success_rate(tmp_path):
    report = evaluate(tmp_path / "smoke", mode="smoke", cases=[], runs=2)

    assert report["summary"]["smoke_passed"]
    assert report["summary"]["success_rate"] is None
    assert report["summary"]["input_tokens"] is None
    assert report["summary"]["estimated_cost_usd"] is None


def test_missing_worker_result_withholds_headline_instead_of_counting_as_pass(tmp_path, monkeypatch):
    import pepsicode.evals.runner as runner

    real_process = runner.run_process

    def broken_process(argv, **kwargs):
        if "pepsicode.evals.worker" in argv:
            return {"status": "exited", "exit_code": 1, "output": "failed to start"}
        return real_process(argv, **kwargs)

    monkeypatch.setattr(runner, "run_process", broken_process)
    report = evaluate(tmp_path / "missing", mode="smoke", cases=[])
    assert not report["summary"]["cohort_complete"]
    assert report["summary"]["unscored"] == 1
    assert report["summary"]["success_rate"] is None


def test_incomplete_cohort_and_missing_telemetry_do_not_become_zero_cost():
    record = {"success": True, "requests": [{"usage": None}], "estimated_cost_usd": 0.0}
    summary = aggregate([record], mode="real", expected=2)
    assert summary["success_rate"] is None
    assert summary["input_tokens"] is None
    assert summary["estimated_cost_usd"] is None
    assert summary["unscored"] == 1


def test_false_completion_and_answer_blocking_have_separate_denominators():
    records = [
        {"success": False, "internal_claimed_complete": True},
        {"success": True, "internal_claimed_complete": True},
        {"success": True, "internal_claimed_complete": False, "scenario": "analysis"},
    ]
    summary = aggregate(records, mode="real", expected=3)
    assert summary["false_completion_rate"] == 0.5
    assert summary["claimed_complete_scored_runs"] == 2
    assert summary["answer_blocked_rate"] == 1
    assert summary["task_failure_rate"] == pytest.approx(1 / 3)


def test_model_acceptance_track_is_not_given_preset_public_test_criteria(tmp_path, faux_endpoint):
    runtime, _ = faux_endpoint
    report = evaluate(
        tmp_path / "model-track",
        mode="real",
        cases=["pagination"],
        runtime=runtime,
        acceptance_track="model",
        timeout=40,
    )
    # The scripted provider never defines criteria. Independent behavior can
    # still pass; internal completion must remain false rather than injecting criteria.
    assert report["summary"]["behavior_success_rate"] == 1
    assert report["summary"]["success_rate"] is None
    assert report["runs"][0]["internal_claimed_complete"] is False
    assert report["summary"]["externally_passed_but_internal_unfinished"] == 1


def test_comparison_rejects_changed_protocol_and_keeps_each_pair(tmp_path):
    from pepsicode.evals.comparison import compare_reports

    for name, success in (("before", False), ("after", True)):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "protocol.json").write_text(
            json.dumps({"mode": "real", "cohort": ["same"], "limits": {"requests": 4}}), encoding="utf-8"
        )
        (folder / "report.json").write_text(
            json.dumps(
                {
                    "summary": {"cohort_complete": True},
                    "runs": [{"case": "case", "run": 1, "case_sha256": "fixed", "success": success}],
                }
            ),
            encoding="utf-8",
        )
    result = compare_reports(tmp_path / "before/report.json", tmp_path / "after/report.json")
    assert result["success_rate_delta"] == 1 and len(result["pairs"]) == 1
    (tmp_path / "after/protocol.json").write_text(
        '{"mode":"real","limits":{"requests":5},"cohort":["same"]}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="protocol"):
        compare_reports(tmp_path / "before/report.json", tmp_path / "after/report.json")


def test_fixtures_cannot_pass_by_changing_public_tests_or_exiting_grader_early(tmp_path):
    workspace = tmp_path / "workspace"
    materialize(SMOKE, workspace)
    env = isolated_environment(tmp_path / "home")
    (workspace / "test_public.py").write_text("print('all passed')", encoding="utf-8")
    assert not protected_files_intact(SMOKE, workspace)
    (workspace / "calculation.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
    assert not grade(SMOKE, workspace, tmp_path, env, label="tampered")["passed"]


def test_environment_does_not_leak_user_credentials_or_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("PYTHONPATH", "untrusted-user-code")
    env = isolated_environment(tmp_path / "home")
    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PYTHONPATH"] != "untrusted-user-code"
    assert env["USERPROFILE"] == str(tmp_path / "home")


def test_subprocess_deadline_is_recorded(tmp_path):
    env = isolated_environment(tmp_path / "home")
    result = run_process([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, env=env, timeout=0.2)
    assert result["status"] == "timeout"


def test_evaluation_never_overwrites_existing_output_and_requires_real_credentials(tmp_path):
    with pytest.raises(ValueError, match="credentials"):
        evaluate(tmp_path / "real", mode="real", cases=["pagination"], runtime={})
    with pytest.raises(FileExistsError):
        evaluate(tmp_path, mode="smoke", cases=[])


def test_summary_requests_count_toward_request_limit(monkeypatch):
    monkeypatch.setattr(
        AnthropicModelAdapter,
        "_send",
        lambda self, request: (
            {
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "checkpoint"}],
            },
            200,
        ),
    )
    model = MeasuredAdapter(
        {"model": "faux", "baseUrl": "https://example.test", "apiKey": "x"}, ToolRegistry([]), max_requests=1, budget=1
    )
    assert model.summarize("transcript") == "checkpoint"
    with pytest.raises(RuntimeError, match="request limit"):
        model.summarize("next transcript")
    assert len(model.requests) == 1
    assert model.limit_reason == "request_limit"


def test_declared_model_prices_cover_summary_usage_and_enforce_budget(monkeypatch):
    monkeypatch.setattr(
        AnthropicModelAdapter,
        "_send",
        lambda self, request: (
            {
                "usage": {"input_tokens": 100, "output_tokens": 20},
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "checkpoint"}],
            },
            200,
        ),
    )
    model = MeasuredAdapter(
        {"model": "faux", "baseUrl": "https://example.test", "apiKey": "x"},
        ToolRegistry([]),
        max_requests=10,
        budget=0.0001,
        pricing={"faux": {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0}},
    )
    model.summarize("transcript")
    assert model.requests[0]["estimated_cost_usd"] == pytest.approx(0.00014)
    from pepsicode.llm.cost_tracker import BudgetExceededError

    with pytest.raises(BudgetExceededError):
        model.summarize("next")
    assert model.limit_reason == "budget_limit"
