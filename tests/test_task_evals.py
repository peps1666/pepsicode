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
                used = any(block.get("type") == "tool_use" for message in history for block in message["content"])
                if used:
                    content = [{"type": "text", "text": "<final>Implemented and tested.</final>"}]
                    stop = "end_turn"
                else:
                    case = next(name for name in CASES if f"{name}-001" in body["system"])
                    files = SOLUTIONS.get(case, {"pricing.py": MONEY_SOLUTION})
                    content = [
                        {
                            "type": "tool_use",
                            "id": f"write-{index}",
                            "name": "write_file",
                            "input": {"path": path, "content": text},
                        }
                        for index, (path, text) in enumerate(files.items())
                    ]
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
    assert summary["success_rate"] == 1.0
    assert summary["usage_complete"]
    assert summary["input_tokens"] == len(bodies) * 100
    assert summary["output_tokens"] == len(bodies) * 20
    assert summary["estimated_cost_usd"] is None  # unknown faux-model prices must not become real cost data
    assert all(not run["baseline"]["passed"] and run["hidden_checks"]["passed"] for run in report["runs"])
    compaction = next(run for run in report["runs"] if run["case"] == "compaction")
    assert len(compaction["compactions"]) >= 2
    assert any(len(body["messages"][0]["content"][0]["text"]) > 1_000 for body in bodies if not body.get("tools"))
    assert next(run for run in report["runs"] if run["case"] == "resume")["resume_process_reopened"]
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
