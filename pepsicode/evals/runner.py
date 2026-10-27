"""Plan a complete cohort, run fresh processes, and grade outside each workspace."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from pepsicode.build_info import source_identity
from pepsicode.evals.cases import CASES, SMOKE, Case, case_digest, materialize, protected_files_intact
from pepsicode.llm.providers import CONNECTION_KEYS
from pepsicode.subprocess_utils import Popen, WindowsProcessJob
from pepsicode.version import VERSION

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def harness_source_digest():
    hasher = hashlib.sha256()
    package = SOURCE_ROOT / "pepsicode"
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package)
        if path.suffix not in {".py", ".md", ".json"} or any(
            part in {"__pycache__", ".pepsi-code", "evals"} for part in relative.parts
        ):
            continue
        hasher.update(relative.as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return hasher.hexdigest()


def isolated_environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    for directory in ("temp", "appdata", "localappdata"):
        (home / directory).mkdir(exist_ok=True)
    # Do not pass provider keys, other account credentials, user Python paths,
    # global skills, hooks or MCP configuration into fixtures and test scripts.
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG", "LC_ALL"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "appdata"),
            "LOCALAPPDATA": str(home / "localappdata"),
            "TEMP": str(home / "temp"),
            "TMP": str(home / "temp"),
            "PYTHONPATH": str(SOURCE_ROOT),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PEPSI_CODE_MAX_RETRIES": "1",
            "PEPSI_CODE_GOVERNANCE": "0",
        }
    )
    return env


def run_process(argv, *, cwd, env, timeout, input_text=None) -> dict:
    process = Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=os.name != "nt",
    )
    job = None

    def terminate():
        if job is not None:
            job.terminate()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()

    try:
        if os.name == "nt":
            job = WindowsProcessJob(process)
        try:
            output, _ = process.communicate(input_text, timeout=timeout)
            return {"status": "exited", "exit_code": process.returncode, "output": output[-8_000:]}
        except subprocess.TimeoutExpired:
            terminate()
            output, _ = process.communicate(timeout=5)
            return {"status": "timeout", "exit_code": process.returncode, "output": output[-8_000:]}
    finally:
        terminate()
        if job is not None:
            job.close()
        process.wait(timeout=5)


def grade(case: Case, workspace: Path, run_dir: Path, env: dict, *, label: str) -> dict:
    nonce = "EVAL_COMPLETE_" + uuid.uuid4().hex
    script = run_dir / f"{label}_grader.py"
    script.write_text(
        "import os, sys\nsys.path.insert(0, sys.argv[1])\nos.chdir(sys.argv[1])\n"
        + case.grader
        + f"\nprint({nonce!r})\n",
        encoding="utf-8",
    )
    result = run_process([sys.executable, "-I", "-B", str(script), str(workspace)], cwd=run_dir, env=env, timeout=30)
    result["passed"] = (
        result["status"] == "exited" and result["exit_code"] == 0 and nonce in result["output"].splitlines()
    )
    result["output"] = result["output"].replace(nonce, "EVAL_COMPLETE")
    return result


def aggregate(records: list[dict], *, mode: str, expected: int, evaluation_kind: str = "provider") -> dict:
    complete = len(records) == expected and all(record.get("success") is not None for record in records)
    requests = [request for record in records for request in record.get("requests", [])]
    usage_complete = complete and bool(requests) and all(request.get("usage") is not None for request in requests)
    scored = [r for r in records if r.get("success") is not None]
    claims = [r for r in scored if r.get("internal_claimed_complete")]
    false_claims = sum(not r["success"] for r in claims)
    answers = [r for r in scored if r.get("scenario") == "analysis"]
    recoveries = [
        r
        for r in scored
        if r.get("recovery_attempts", 0) or r.get("scenario") in {"resume", "interrupted", "unknown_recovery"}
    ]
    return {
        "planned_runs": expected,
        "finished_runs": len(records),
        "cohort_complete": complete,
        "passed": sum(record.get("success") is True for record in records),
        "failed": sum(record.get("success") is False for record in records),
        "unscored": expected - sum(record.get("success") is not None for record in records),
        "success_rate": sum(record["success"] for record in records) / expected
        if mode == "real" and complete and evaluation_kind == "provider"
        else None,
        "behavior_success_rate": sum(record["success"] for record in records) / expected if complete else None,
        "smoke_passed": complete and all(record["success"] for record in records) if mode == "smoke" else None,
        "duration_seconds": round(sum(record.get("duration_seconds", 0) for record in records), 3),
        "model_requests": len(requests),
        "usage_complete": usage_complete,
        "input_tokens": sum(request["usage"].get("input_tokens", 0) for request in requests)
        if usage_complete
        else None,
        "output_tokens": sum(request["usage"].get("output_tokens", 0) for request in requests)
        if usage_complete
        else None,
        "estimated_cost_usd": sum(record["estimated_cost_usd"] for record in records)
        if complete and usage_complete and all(record.get("estimated_cost_usd") is not None for record in records)
        else None,
        "clarification_waits": sum(
            record.get("clarification_waits", record.get("human_interventions", 0)) for record in records
        ),
        "approval_requests": sum(record.get("approval_metrics", {}).get("requests", 0) for record in records),
        "approval_decisions": sum(record.get("approval_metrics", {}).get("decisions", 0) for record in records),
        "approval_wait_seconds": sum(record.get("approval_metrics", {}).get("wait_seconds", 0) for record in records),
        "claimed_complete_scored_runs": len(claims),
        "false_completions": false_claims,
        "false_completion_rate": false_claims / len(claims) if claims else None,
        "answer_blocked_rate": sum(not r.get("internal_claimed_complete") for r in answers) / len(answers)
        if answers
        else None,
        "answer_scored_runs": len(answers),
        "recovery_scored_runs": len(recoveries),
        "recovery_success_rate": sum(r["success"] for r in recoveries) / len(recoveries) if recoveries else None,
        "externally_passed_but_internal_unfinished": sum(
            r["success"] and not r.get("internal_claimed_complete") for r in scored
        ),
        "infrastructure_errors": sum(record.get("success") is None for record in records),
        "infrastructure_error_reasons": [r.get("failure_reason") for r in records if r.get("success") is None],
        "task_failure_rate": sum(not r["success"] for r in scored) / len(scored) if scored else None,
    }


def evaluate(
    output: Path,
    *,
    mode: str,
    cases: list[str],
    runs: int = 1,
    runtime: dict | None = None,
    timeout: float = 180,
    max_steps: int = 40,
    max_requests: int = 50,
    budget: float = 1.0,
    pricing: dict | None = None,
    acceptance_track: str = "preset",
) -> dict:
    if (
        mode not in {"real", "smoke"}
        or acceptance_track not in {"preset", "model"}
        or runs < 1
        or not math.isfinite(timeout)
        or timeout <= 0
        or max_steps < 1
        or max_requests < 1
        or not math.isfinite(budget)
        or budget <= 0
    ):
        raise ValueError("Invalid evaluation mode or limits")
    pricing = {} if pricing is None else pricing
    if not isinstance(pricing, dict):
        raise ValueError("Pricing must be a model-ID to rates object")
    for model, rates in pricing.items():
        if (
            not isinstance(model, str)
            or not isinstance(rates, dict)
            or set(rates) != {"input", "output", "cache_read", "cache_write"}
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
                for value in rates.values()
            )
        ):
            raise ValueError(
                "Each model needs finite non-negative input/output/cache_read/cache_write USD rates per million tokens"
            )
    if mode == "real" and any(name not in CASES for name in cases):
        raise ValueError("Unknown evaluation case")
    selected = [SMOKE] if mode == "smoke" else [CASES[name] for name in cases]
    if not selected or len({case.id for case in selected}) != len(selected):
        raise ValueError("Select at least one case without duplicates")
    if mode == "real" and (
        not runtime or not runtime.get("model") or not (runtime.get("apiKey") or runtime.get("authToken"))
    ):
        raise ValueError("Real evaluations require a configured model and credentials")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cohort = [
        {"case": case.id, "run": number, "case_sha256": case_digest(case)}
        for case in selected
        for number in range(1, runs + 1)
    ]
    evaluation_kind = (
        "offline_smoke"
        if mode == "smoke"
        else "scripted_service"
        if (runtime or {}).get("_evaluationSource") == "scripted_service"
        else "provider"
    )
    protocol = {
        "version": 1,
        "evaluation_kind": evaluation_kind,
        "acceptance_track": acceptance_track,
        "approval_policy": "automated allow-once for exact public test command; all others denied; wait times are simulated backend time",
        "pepsicode_version": VERSION,
        "source_identity": source_identity(),
        "evaluator_sha256": hashlib.sha256(
            b"".join(path.read_bytes() for path in sorted(Path(__file__).parent.glob("*.py")))
        ).hexdigest(),
        "harness_sha256": harness_source_digest(),
        "harness_digest_scope": "production Python and bundled agent data, excluding evaluator/runtime artifacts",
        "pricing": pricing,
        "mode": mode,
        "model": (runtime or {}).get("model"),
        "provider": (runtime or {}).get("provider"),
        "reasoning_effort": (runtime or {}).get("reasoningEffort", "default"),
        "limits": {
            "timeout_seconds": timeout,
            "max_steps_per_iteration": max_steps,
            "max_requests": max_requests,
            "estimated_budget_usd": budget,
        },
        "cohort": cohort,
        "cost_basis": "CostTracker approximate price table; unknown models use its default for budget enforcement only",
        "isolation": "Fresh process/home/workspace; restricted tool set; not an OS security sandbox",
    }
    protocol["sha256"] = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
    (output / "protocol.json").write_text(json.dumps(protocol, indent=2, ensure_ascii=False), encoding="utf-8")
    records = []
    connection = {key: value for key, value in (runtime or {}).items() if key in CONNECTION_KEYS}
    for planned in cohort:
        case = SMOKE if mode == "smoke" else CASES[planned["case"]]
        run_dir = output / f"{case.id}-{planned['run']:03d}"
        run_dir.mkdir()
        workspace = run_dir / "workspace"
        materialize(case, workspace)
        env = isolated_environment(run_dir / "home")
        started = time.monotonic()
        record = {
            **planned,
            "scenario": case.scenario,
            "success": None,
            "status": "error",
            "failure_reason": None,
            "estimated_cost_usd": None,
        }
        try:
            baseline = grade(case, workspace, run_dir, env, label="baseline")
            record["baseline"] = baseline
            if baseline["status"] == "timeout" or baseline["passed"]:
                raise ValueError("Invalid or ungradeable fixture")
            payload = {
                "case": case.id,
                "mode": mode,
                "workspace": str(workspace),
                "runtime": connection,
                "max_steps": max_steps,
                "max_requests": max_requests,
                "budget": budget,
                "pricing": pricing,
                "acceptance_track": acceptance_track,
                "checkpoint_ready": str(run_dir / "checkpoint-ready"),
                "result_path": str(run_dir / "worker.json"),
            }
            phases = ("prepare", "run") if case.scenario in {"resume", "interrupted"} else ("run",)
            for phase in phases:
                payload["phase"] = phase
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    record.update(status="timeout", failure_reason="timeout", success=False)
                    break
                process = run_process(
                    [sys.executable, "-m", "pepsicode.evals.worker"],
                    cwd=workspace,
                    env=env,
                    timeout=remaining,
                    input_text=json.dumps(payload),
                )
                if process["status"] == "timeout":
                    record.update(status="timeout", failure_reason="timeout", success=False)
                    break
                result_path = Path(payload["result_path"])
                if (
                    phase == "prepare"
                    and case.scenario == "interrupted"
                    and process["exit_code"] == 75
                    and Path(payload["checkpoint_ready"]).is_file()
                ):
                    record["checkpoint_process_interrupted"] = True
                    continue
                if process["exit_code"] != 0 or not result_path.is_file():
                    raise RuntimeError("Worker exited without a result")
                worker = json.loads(result_path.read_text(encoding="utf-8"))
                if phase == "prepare":
                    if worker.get("status") != "prepared":
                        raise RuntimeError("Checkpoint preparation failed")
                    result_path.unlink()
                    continue
                # Full transcript is retained in worker.json, not duplicated in reports.
                record.update({key: value for key, value in worker.items() if key != "messages"})
                if worker.get("status") == "error":
                    break
                hidden = grade(case, workspace, run_dir, env, label="final")
                record["hidden_checks"] = hidden
                protected = protected_files_intact(case, workspace)
                verified = bool(worker.get("verification")) and worker["verification"][-1].get("status") == "passed"
                scenario_ok = case.scenario != "compaction" or len(worker.get("compactions", [])) >= 2
                success = (
                    hidden["passed"]
                    and protected
                    and scenario_ok
                    and worker.get("status")
                    not in {"budget_limit", "request_limit", "awaiting_user", "step_limit", "cancelled"}
                )
                reason = (
                    None
                    if success
                    else worker.get("failure_reason")
                    if worker.get("failure_reason")
                    else "protected_files_changed"
                    if not protected
                    else "public_verification_failed"
                    if not verified
                    else "hidden_checks_failed"
                    if not hidden["passed"]
                    else worker.get("failure_reason") or worker.get("status")
                )
                record.update(
                    success=success,
                    status="passed" if success else "failed",
                    failure_reason=reason,
                    protected_files_intact=protected,
                    internal_claimed_complete=worker.get("internal_claimed_complete", False),
                )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            record.update(status="error", failure_reason=f"harness_{type(error).__name__}", success=None)
        record["duration_seconds"] = round(time.monotonic() - started, 3)
        records.append(record)
        with (output / "observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"{case.id} run {planned['run']}: {record['status']} ({record['duration_seconds']}s)", flush=True)
    report = {
        "protocol_sha256": protocol["sha256"],
        "mode": mode,
        "evaluation_kind": evaluation_kind,
        "summary": aggregate(records, mode=mode, expected=len(cohort), evaluation_kind=evaluation_kind),
        "runs": records,
        "cases": {
            case.id: aggregate(
                [record for record in records if record["case"] == case.id],
                mode=mode,
                expected=runs,
                evaluation_kind=evaluation_kind,
            )
            for case in selected
        },
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    rate = f"{summary['success_rate']:.1%}" if summary["success_rate"] is not None else "unavailable"
    (output / "report.md").write_text(
        f"# Pepsicode task evaluation\n\nMode: {mode}\n\nEvaluation source: {evaluation_kind}\n\nModel success rate: {rate}\n\n"
        f"Planned: {len(cohort)}; passed: {summary['passed']}; failed: {summary['failed']}; unscored: {summary['unscored']}.\n\n"
        "Smoke uses a scripted agent and does not measure model quality. Missing telemetry is null. "
        "Cost is an estimate; per-run budgets use the existing approximate price table. "
        "A single repetition does not establish stability.\n",
        encoding="utf-8",
    )
    return report
