"""Resolve interrupted dispatch acknowledgements from committed session state."""


def recover_dispatch_receipts(session, scope, task_id):
    state = session.task_control.get("orchestration") or {}
    receipts = state.get("dispatch_receipts", {})
    work = {item["attempt_id"]: item for item in state.get("work", [])}
    effects = {effect["attempt_id"]: effect for effect in state.get("effects", [])}
    for call_id, call in list(scope.unresolved.items()):
        if (
            call.get("event") != "tool/started"
            or call.get("tool") != "task_dispatch"
            or call.get("trace_id")
            or not call.get("task_id")
        ):
            continue
        attempt_id = receipts.get(f"{call['task_id']}:{call_id}")
        item = work.get(attempt_id)
        if (
            item is None
            or item["task_id"] != task_id
            or attempt_id not in effects
            or call.get("arguments") != [item["work_key"], item["specification"]]
        ):
            continue
        scope.reconcile(
            call_id,
            scope.recovery_version[0],
            {
                "source": "durable_dispatch_receipt",
                "attempt_id": attempt_id,
                "effect_id": effects[attempt_id]["effect_id"],
            },
            "occurred",
        )
