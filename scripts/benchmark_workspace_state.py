"""Compare content scans on identical local inputs; never run a model or shell."""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pepsicode.build_info import source_identity
from pepsicode.core.task_control import begin_task
from pepsicode.core.workspace_state import workspace_state
from pepsicode.evals.runner import harness_source_digest
from pepsicode.execution.runtime import ExecutionScope, RequestBudget, bind_scope
from pepsicode.llm.cost_tracker import CostTracker
from pepsicode.permissions import PermissionManager


def measure(function, path, repeats):
    samples = [function(path) for _ in range(repeats)]
    return {
        "durations_ms": [sample["duration_ms"] for sample in samples],
        "median_ms": statistics.median(sample["duration_ms"] for sample in samples),
        "files": sum(value != "directory" for value in samples[-1]["entries"].values()),
        "bytes": samples[-1]["bytes"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", default="af533e9")
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10 or args.output.exists():
        parser.error("Use 1..10 samples and a fresh output file")
    code = subprocess.check_output(
        ["git", "show", f"{args.baseline}:pepsicode/core/workspace_state.py"], cwd=ROOT, text=True
    )
    baseline = types.ModuleType("baseline_workspace_state")
    exec(compile(code, "baseline_workspace_state.py", "exec"), baseline.__dict__)
    fixture = ROOT / ".eval-work/fingerprint-fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    for number in range(10_000):
        file = fixture / f"source_{number:05d}.py"
        if not file.exists():
            file.write_text("VALUE = 42\n" * 24, encoding="utf-8")
    report = {
        "kind": "local_content_scan_not_model_quality",
        "baseline": args.baseline,
        "source_identity": source_identity(ROOT),
        "harness_sha256": harness_source_digest(),
        "workloads": {},
    }
    # The repository root contains >512 MiB of packaged artifacts; the core
    # package is an actual source workspace within the documented size bound.
    for name, path in (("source_package", ROOT / "pepsicode"), ("synthetic_10000_files", fixture)):
        report["workloads"][name] = {
            "before": measure(baseline.workspace_state, path, args.samples),
            "after": measure(workspace_state, path, args.samples),
        }
    prompts = []
    workspace = ROOT / "pepsicode"
    permissions = PermissionManager(
        str(workspace), prompt=lambda request: prompts.append(request) or {"decision": "allow_task"}
    )
    scope = ExecutionScope(str(workspace), permissions=permissions, budget=RequestBudget(CostTracker()))
    try:
        with bind_scope(scope):
            begin_task([{"role": "user", "content": "Implement calculation behavior"}], {})
            for _ in range(2):
                assert permissions.check_command(
                    sys.executable, ["-c", "pass"], str(workspace), "snapshot profiling only"
                ).is_allowed
            workspace_state(workspace, reason="acceptance_start")
            workspace_state(workspace, reason="acceptance_final")
        report["single_task_profile"] = {
            **scope.snapshot()["workspace_scans"],
            "approval_requests": len(prompts),
            "note": "Source workspace; simulated allow_task decisions and no command/model execution",
        }
    finally:
        permissions.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
