"""Completion evidence for tasks that request Worker reports as the result."""

import re

_REPORT_ONLY = re.compile(
    r"(?:只|仅)(?:想)?体验(?:异步)?派工|(?:只|仅)(?:需|要)?(?:汇报|报告)\s*(?:各|每个|两个|派工|worker)"
    r"|\b(?:just|only) (?:report|summarize) (?:the )?worker\b",
    re.I,
)
_DELIVER = re.compile(
    r"(?:请|改为|现在要|还要|(?<!不)需要|务必)\s*(?:正式交付|合并|集成|提交\s*PR)|\b(?:please|now) (?:deliver|integrate|merge|open a PR)\b",
    re.I,
)


def _requires_project(task):
    requirements = task.get("requirements") or {}
    return any(requirements.get(key) for key in ("delivery_target", "artifacts", "behavior")) or any(
        source.get("source") == "caller" for source in task.get("criterion_sources", {}).values()
    )


def bind_worker_output(task, kind):
    """The first accepted assignment fixes the parent deliverable for this revision."""
    output = task.get("orchestration_output") or {}
    if kind == "worker_reports" and _requires_project(task):
        raise ValueError("The parent task requires project delivery")
    if kind and output.get("revision") == task["revision"] and output["kind"] != kind:
        raise ValueError("The current assignments already have a different deliverable")
    if output.get("revision") != task["revision"]:
        task["orchestration_output"] = {
            "revision": task["revision"],
            "kind": kind or "project",
        }


def worker_report_only(task):
    output = task.get("orchestration_output") or {}
    return (
        output.get("kind") == "worker_reports"
        and output.get("revision") == task.get("revision")
        and not _requires_project(task)
    )


def restore_worker_output(control):
    """Keep the deliverable already used by Workers from pre-contract sessions."""
    task = control.get("task") or {}
    if "orchestration_output" in task or not worker_results(control, task):
        return
    for text in reversed([task.get("objective", ""), *task.get("instructions", [])]):
        if _REPORT_ONLY.search(text):
            task["orchestration_output"] = {"revision": task["revision"], "kind": "worker_reports"}
            break
        if _DELIVER.search(text):
            break


def worker_results(control, task):
    state = control.get("orchestration") or {}
    latest = {
        item["work_id"]: item
        for item in state.get("work", [])
        if (item["task_id"], item["revision"]) == (task["id"], task["revision"])
    }
    return [
        {
            **{key: item[key] for key in ("work_id", "attempt_id", "trace_id", "work_key", "state")},
            "worktree": (state.get("environments") or {}).get(work_id, {}).get("worktree"),
        }
        for work_id, item in latest.items()
    ]
