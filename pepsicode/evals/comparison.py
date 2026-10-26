"""Compare paired runs without changing either harness or invoking a model."""

from __future__ import annotations

import json
import random
from pathlib import Path


def compare_reports(before_path: Path, after_path: Path):
    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in (before_path, after_path)]
    protocols = [
        json.loads(Path(path).with_name("protocol.json").read_text(encoding="utf-8"))
        for path in (before_path, after_path)
    ]
    for key in ("mode", "model", "provider", "reasoning_effort", "limits", "pricing", "cohort"):
        if protocols[0].get(key) != protocols[1].get(key):
            raise ValueError(f"Unpaired evaluation protocols: {key}")
    if protocols[0].get("acceptance_track", "preset") != protocols[1].get("acceptance_track", "preset"):
        raise ValueError("Cannot compare different acceptance tracks")
    kinds = [p.get("evaluation_kind", "offline_smoke" if p.get("mode") == "smoke" else "provider") for p in protocols]
    if kinds[0] != kinds[1]:
        raise ValueError("Cannot compare different evaluation sources")
    if not all(r["summary"]["cohort_complete"] for r in reports):
        raise ValueError("Incomplete cohorts cannot produce a paired success estimate")
    indexes = [{(run["case"], run["run"]): run for run in report["runs"]} for report in reports]
    if set(indexes[0]) != set(indexes[1]) or not indexes[0]:
        raise ValueError("Paired run identities must match")
    pairs = []
    for key in sorted(indexes[0]):
        left, right = indexes[0][key], indexes[1][key]
        if left.get("case_sha256") != right.get("case_sha256"):
            raise ValueError("Paired fixture digests differ")
        pairs.append(
            {
                "case": key[0],
                "run": key[1],
                "before": left["success"],
                "after": right["success"],
                "before_reason": left.get("failure_reason"),
                "after_reason": right.get("failure_reason"),
            }
        )
    differences = [int(p["after"]) - int(p["before"]) for p in pairs]
    randomizer = random.Random(0)
    samples = sorted(sum(randomizer.choice(differences) for _ in differences) / len(differences) for _ in range(1000))
    return {
        "version": 1,
        "mode": protocols[0]["mode"],
        "pairs": pairs,
        "evaluation_kind": kinds[0],
        "success_rate_delta": sum(differences) / len(differences)
        if protocols[0]["mode"] == "real" and kinds[0] == "provider"
        else None,
        "exploratory_paired_bootstrap_95": [samples[24], samples[974]]
        if protocols[0]["mode"] == "real" and kinds[0] == "provider"
        else None,
        "limitations": [
            "Fixed small fixtures; repetitions are not proof of independence or generalization.",
            "Approval policy and worker/tool capabilities must also be reviewed; matching metadata is not proof of equivalent authority.",
            "Smoke or scripted services do not establish real-model quality.",
        ],
    }
