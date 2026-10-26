from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from pepsicode.evals.cases import CASES
from pepsicode.evals.runner import evaluate


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated Pepsicode coding-task evaluations")
    parser.add_argument("--mode", choices=("smoke", "real"), default="smoke")
    parser.add_argument("--list", action="store_true", help="List real task cases without invoking a model")
    parser.add_argument("--case", action="append", choices=tuple(CASES), dest="cases")
    parser.add_argument("--runs", type=int, default=1, help="Fresh repetitions per case")
    parser.add_argument("--timeout", type=float, default=180, help="Wall-clock deadline per repetition")
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--max-requests", type=int, default=50, help="Includes checkpoint requests")
    parser.add_argument("--budget", type=float, default=1.0, help="Estimated USD cap per run; a request may overshoot")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--pricing", type=Path, help="JSON model-ID to input/output/cache_read/cache_write USD-per-million rates"
    )
    args = parser.parse_args()
    if args.list:
        for case in CASES.values():
            print(f"{case.id}: {case.description}")
        return 0
    output = args.output or Path(".evals") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        runtime = None
        if args.mode == "real":
            from pepsicode.config import load_runtime_config

            runtime = load_runtime_config()
        pricing = json.loads(args.pricing.read_text(encoding="utf-8")) if args.pricing else None
        report = evaluate(
            output,
            mode=args.mode,
            cases=args.cases or list(CASES),
            runs=args.runs,
            runtime=runtime,
            timeout=args.timeout,
            max_steps=args.max_steps,
            max_requests=args.max_requests,
            budget=args.budget,
            pricing=pricing,
        )
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Report: {output.resolve() / 'report.json'}")
    summary = report["summary"]
    return 0 if summary["cohort_complete"] and summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
