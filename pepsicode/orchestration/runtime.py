"""Host-side scheduling of committed launch intents."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Condition, Event, RLock

from pepsicode.core import session as sessions
from pepsicode.orchestration.coordinator import coordinator_turn
from pepsicode.orchestration.store import (
    cancel_queued,
    claim_activation,
    claim_launch,
    recover_launches,
    request_stop,
    settle_launch,
)

_HOST_SLOT_LOCK = RLock()
_HOST_SLOT_COND = Condition(_HOST_SLOT_LOCK)
_HOST_ACTIVE_WORKERS = 0


def _try_acquire_host_slot(max_total_workers: int | None) -> bool:
    global _HOST_ACTIVE_WORKERS
    with _HOST_SLOT_COND:
        if max_total_workers is not None and max_total_workers <= _HOST_ACTIVE_WORKERS:
            return False
        _HOST_ACTIVE_WORKERS += 1
        return True


def _wait_for_host_slot(timeout: float = 0.05) -> None:
    with _HOST_SLOT_COND:
        _HOST_SLOT_COND.wait(timeout=timeout)


def _release_host_slot() -> None:
    global _HOST_ACTIVE_WORKERS
    with _HOST_SLOT_COND:
        _HOST_ACTIVE_WORKERS = max(0, _HOST_ACTIVE_WORKERS - 1)
        _HOST_SLOT_COND.notify_all()


@contextmanager
def _own_lock(path, resource):
    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError(f"Could not acquire orchestration ownership for {resource}") from error
        yield


def own_session(session_id):
    """Hold an OS lock for an orchestration host, including across processes."""
    path = sessions._session_file(session_id).with_suffix(".orchestration.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    return _own_lock(path, "session")


def own_workspace(workspace):
    """Reserve the real workspace for a single scheduling run."""
    root = Path(tempfile.gettempdir()) / "pepsicode-orchestration-locks"
    root.mkdir(exist_ok=True)
    identity = os.path.normcase(str(Path(workspace).resolve()))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return _own_lock(root / f"{digest}.lock", "workspace")


def _deliver_results(session, on_result):
    """Offer current pending results until the callback stops acknowledging them."""
    while True:
        task = session.task_control.get("task") or {}
        if task.get("status") not in {"active", "waiting_user"}:
            return
        pending = next(
            (
                event
                for event in session.task_control.get("orchestration", {}).get("events", [])
                if event["state"] == "pending"
                and (event["task_id"], event["revision"]) == (task.get("id"), task.get("revision"))
            ),
            None,
        )
        if pending is None:
            return
        on_result(session, pending["attempt_id"])
        # A callback can elect to leave results pending, for example when
        # the coordinator cannot accept any more input.
        if pending["state"] == "pending":
            return


def run_pending(
    session_id,
    *,
    execute=None,
    worker=None,
    on_result=None,
    coordinate=None,
    start_turn=False,
    max_workers=None,
    is_read_only=lambda item: False,
    input_queue=None,
):
    """Run pending intents with a trusted executor and serial result callback.

    The host loads state after acquiring ownership. execute(item) runs on a
    worker thread and returns (outcome, summary). on_result(session, attempt_id)
    runs on the host thread after commit and may admit more work. The caller
    supplies execution policy, budgets and cancellation. This function only
    coordinates launch and settlement. Both OS ownership locks remain held
    during the callbacks.

    Concurrent runs cannot share a workspace. Within a run, work is exclusive
    unless the host classifies it as read-only. is_read_only must be a pure
    host policy, not a model assertion.

    An opt-in host can supply coordinate(session, tools, attempt_id) in place
    of on_result. Set start_turn to request an initial callback with no
    attempt ID. The host owns model invocation and delivery acknowledgement.
    Supply worker in place of execute to use the managed child Agent.
    """
    if max_workers is not None and (type(max_workers) is not int or max_workers < 1):
        raise ValueError("max_workers must be a positive integer")
    if (execute is None) == (worker is None):
        raise ValueError("Supply one Worker executor")
    if (on_result is None) == (coordinate is None) or (start_turn and coordinate is None):
        raise ValueError("Supply one coordinator callback")
    with own_session(session_id):
        session = sessions.load_session(session_id)
        if session is None:
            raise ValueError("Orchestration session could not be loaded")
        with own_workspace(session.workspace):
            return run_owned(
                session,
                execute=execute,
                worker=worker,
                on_result=on_result,
                coordinate=coordinate,
                start_turn=start_turn,
                max_workers=max_workers,
                is_read_only=is_read_only,
                input_queue=input_queue,
                runtime=worker.runtime if worker is not None else None,
            )


def run_owned(
    session,
    *,
    execute=None,
    worker=None,
    on_result=None,
    coordinate=None,
    start_turn=False,
    max_workers=None,
    is_read_only=lambda item: False,
    input_queue=None,
    batch_results=False,
    prepare_coordinate=None,
    activation_limit=None,
    event_debounce_ms=None,
    event_max_delay_ms=None,
    runtime=None,
):
    """Run under the host's existing session and workspace ownership locks."""
    orchestration = (runtime or {}).get("orchestration") or {}
    if max_workers is None:
        max_workers = orchestration.get("maxConcurrentWorkers", 3)
    if type(max_workers) is not int or max_workers < 1:
        raise ValueError("max_workers must be a positive integer")
    max_total_workers = orchestration.get("maxTotalActiveWorkers")
    if max_total_workers is not None and (type(max_total_workers) is not int or max_total_workers < 1):
        raise ValueError("max_total_workers must be a positive integer")
    if activation_limit is None:
        activation_limit = orchestration.get("maxCoordinatorActivations", 20)
    if type(activation_limit) is not int or activation_limit < 1:
        raise ValueError("activation_limit must be a positive integer")
    if event_debounce_ms is None:
        event_debounce_ms = orchestration.get("eventDebounceMs", 250)
    if event_max_delay_ms is None:
        event_max_delay_ms = orchestration.get("eventMaxDelayMs", 1000)
    for name, value in (("event_debounce_ms", event_debounce_ms), ("event_max_delay_ms", event_max_delay_ms)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a non-negative finite number")
    if event_max_delay_ms < event_debounce_ms:
        raise ValueError("event_max_delay_ms must be >= event_debounce_ms")
    generation = uuid.uuid4().hex
    if recover_launches(session, generation=generation) or any(
        effect["state"] == "recovery_pending"
        for effect in session.task_control.get("orchestration", {}).get("effects", [])
    ):
        return False
    control = session.task_control
    task = control.get("task") or {}
    state = control.get("orchestration") or {}
    prepared_process = worker is not None and worker.prepared_process
    if not prepared_process and any(
        item["state"] == "queued"
        and (item["task_id"], item["revision"]) == (task.get("id"), task.get("revision"))
        and item["work_id"] in state.get("environments", {})
        for item in state.get("work", [])
    ):
        raise RuntimeError("Managed worktree assignments require the M2 Worker process executor")
    if worker is None:
        _schedule(
            session,
            execute,
            on_result,
            max_workers,
            is_read_only,
            generation,
            start_turn=start_turn,
            coordinate=coordinate,
            batch_results=batch_results,
            prepare_coordinate=prepare_coordinate,
            runtime=runtime,
            activation_limit=activation_limit,
            event_debounce_ms=event_debounce_ms,
            event_max_delay_ms=event_max_delay_ms,
            max_total_workers=max_total_workers,
        )
    else:

        def parallel_safe(item):
            if (
                prepared_process
                and (
                    item["work_id"] in state.get("environments", {})
                    or (worker.isolation != "shared" and item["specification"].get("agent_type") == "general")
                )
                and not (worker.hooks and worker.hooks.has_command_actions())
            ):
                return True
            return worker.is_read_only(item)

        with worker.bind(session, generation=generation) as execute_worker:
            try:
                _schedule(
                    session,
                    execute_worker,
                    on_result,
                    max_workers,
                    parallel_safe,
                    generation,
                    start_turn=start_turn,
                    coordinate=coordinate,
                    scope=worker.scope,
                    input_queue=input_queue,
                    steering_safe=lambda: not (worker.hooks and worker.hooks.has_command_actions()),
                    batch_results=batch_results,
                    prepare_coordinate=prepare_coordinate,
                    runtime=runtime,
                    launch_isolation=worker.isolation,
                    activation_limit=activation_limit,
                    event_debounce_ms=event_debounce_ms,
                    event_max_delay_ms=event_max_delay_ms,
                    max_total_workers=max_total_workers,
                )
            finally:
                if worker.scope.cancellation_event.is_set():
                    cancel_queued(session)
    return True


def _collect_finished(ready, active, finished, debounce_ms, max_delay_ms):
    """Drain completed futures and coalesce arrivals inside the debounce window."""
    while len(finished) < len(active):
        try:
            extra = ready.get_nowait()
        except Empty:
            break
        if extra is None:
            break
        finished.append(extra)
    debounce = debounce_ms / 1000.0
    deadline = time.monotonic() + max_delay_ms / 1000.0
    while debounce > 0 and len(finished) < len(active):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            extra = ready.get(timeout=min(debounce, remaining))
        except Empty:
            break
        if extra is None:
            break
        finished.append(extra)
    return finished


def _schedule(
    session,
    execute,
    on_result,
    max_workers,
    is_read_only,
    generation,
    *,
    start_turn,
    coordinate,
    scope=None,
    input_queue=None,
    steering_safe=lambda: True,
    batch_results=False,
    prepare_coordinate=None,
    runtime=None,
    launch_isolation="shared",
    activation_limit=20,
    event_debounce_ms=250,
    event_max_delay_ms=1000,
    max_total_workers=None,
):
    stops = {}
    ready = SimpleQueue()
    stop_lock = RLock()
    controls_open = True

    def cancel(task_id, revision, attempt_id):
        with stop_lock:
            if not controls_open:
                raise ValueError("No active orchestration run")
            state = request_stop(session, task_id=task_id, revision=revision, attempt_id=attempt_id)
            if state == "stop_requested":
                stops[attempt_id].set()
            return state

    def user_stop(trace_id):
        with stop_lock, session._tree_lock:
            control = session.task_control
            task = control.get("task") or {}
            work = (control.get("orchestration") or {}).get("work", [])
            item = next((item for item in work if item["trace_id"] == trace_id), None)
            if item is None:
                raise ValueError("No Worker matches this trace ID")
            return cancel(task.get("id"), task.get("revision"), item["attempt_id"])

    @contextmanager
    def bind_controls():
        nonlocal controls_open
        with input_queue.wake_on_steering(lambda: ready.put(None), stop_worker=user_stop):
            try:
                yield
            finally:
                with stop_lock:
                    controls_open = False

    def can_continue():
        return scope is None or not (scope.cancellation_event.is_set() or scope.has_unknown)

    def notify(session, attempt_id):
        if not can_continue():
            return
        if scope is not None and attempt_id is not None and not claim_activation(session, limit=activation_limit):
            return
        if coordinate is None:
            on_result(session, attempt_id)
        else:
            if prepare_coordinate is not None:
                prepare_coordinate(session)
            with coordinator_turn(
                session,
                cancel=cancel if scope is not None else None,
                admitted_only=prepare_coordinate is not None,
                runtime=runtime,
                workspace_owned=True,
            ) as tools:
                coordinate(session, tools, attempt_id)

    steering = bind_controls() if input_queue is not None else nullcontext()
    with steering, ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="orchestration") as pool:
        if start_turn and (session.task_control.get("task") or {}).get("status") == "active":
            notify(session, None)
        _deliver_results(session, notify)
        active = {}
        failure = None
        while True:
            if (
                failure is None
                and can_continue()
                and input_queue is not None
                and input_queue.has_steering
                and (not active or steering_safe())
            ):
                try:
                    notify(session, None)
                except Exception as error:
                    failure = error
            prepare_wake = []
            while failure is None and can_continue() and len(active) < max_workers:

                def eligible(candidate):
                    return not active or (all(parallel for _, parallel in active.values()) and is_read_only(candidate))

                candidate = None
                if launch_isolation != "shared":
                    task = session.task_control.get("task") or {}
                    candidate = next(
                        (
                            row
                            for row in session.task_control["orchestration"]["work"]
                            if row["state"] == "queued"
                            and (row["task_id"], row["revision"]) == (task.get("id"), task.get("revision"))
                        ),
                        None,
                    )
                    if candidate and eligible(candidate) and candidate["specification"]["agent_type"] == "general":
                        from pepsicode.orchestration.environment import prepare_owned

                        try:
                            prepare_owned(session, candidate["work_id"], scope=scope, isolation=launch_isolation)
                        except (ValueError, RuntimeError, OSError) as error:
                            if scope is not None and scope.has_unknown:
                                failure = error
                                break
                            attempt_id = candidate["attempt_id"]
                            claimed = None
                            with stop_lock:
                                if can_continue():
                                    claimed = claim_launch(
                                        session,
                                        generation=generation,
                                        eligible=lambda row: row["attempt_id"] == attempt_id,
                                    )
                            if claimed is None:
                                failure = error
                                break
                            evidence = json.dumps(
                                {
                                    "stage": "prepare",
                                    "class": "environment",
                                    "check": "prepare_owned",
                                    "output": str(error)[:1000],
                                },
                                ensure_ascii=False,
                            )
                            settled = settle_launch(
                                session,
                                attempt_id=attempt_id,
                                generation=generation,
                                outcome="failed",
                                summary=evidence,
                            )
                            if settled:
                                task = session.task_control.get("task") or {}
                                if any(
                                    event["attempt_id"] == attempt_id
                                    and (event["task_id"], event["revision"]) == (task.get("id"), task.get("revision"))
                                    for event in session.task_control.get("orchestration", {}).get("events", [])
                                ):
                                    prepare_wake.append(attempt_id)
                            continue
                if not _try_acquire_host_slot(max_total_workers):
                    break
                item = None
                try:
                    with stop_lock:
                        if can_continue():
                            item = claim_launch(
                                session,
                                generation=generation,
                                eligible=lambda row: eligible(row)
                                and (candidate is None or candidate["attempt_id"] == row["attempt_id"]),
                            )
                            if item is not None and scope is not None:
                                stop = stops[item["attempt_id"]] = Event()
                except Exception:
                    _release_host_slot()
                    raise
                if item is None:
                    _release_host_slot()
                    if candidate and candidate["state"] == "cancelled":
                        continue
                    break
                try:
                    if scope is None:
                        future = pool.submit(execute, item)
                    else:
                        future = pool.submit(execute, item, stop)
                except Exception:
                    _release_host_slot()
                    raise
                active[future] = (item["attempt_id"], is_read_only(item))
                future.add_done_callback(ready.put)
            if failure is None and prepare_wake:
                try:
                    if batch_results:
                        _deliver_results(session, notify)
                    else:
                        for attempt_id in prepare_wake:
                            notify(session, attempt_id)
                except Exception as error:
                    failure = error
                else:
                    if not active and can_continue():
                        continue
            if not active:
                if failure is not None:
                    raise failure
                if can_continue() and max_total_workers is not None:
                    task = session.task_control.get("task") or {}
                    pending_ids = {
                        row["attempt_id"]
                        for row in session.task_control.get("orchestration", {}).get("work", [])
                        if row["state"] == "queued"
                        and (row["task_id"], row["revision"]) == (task.get("id"), task.get("revision"))
                    }
                    if pending_ids and any(
                        effect["state"] == "pending" and effect["attempt_id"] in pending_ids
                        for effect in session.task_control.get("orchestration", {}).get("effects", [])
                    ):
                        _wait_for_host_slot(0.05)
                        continue
                return
            future = ready.get()
            if future is None:
                continue
            finished = [future]
            if batch_results:
                _collect_finished(ready, active, finished, event_debounce_ms, event_max_delay_ms)
            wake = []
            for future in finished:
                attempt_id, _ = active.pop(future)
                # Unexpected executor exceptions leave the durable claim
                # unsettled. It may already have produced side effects.
                try:
                    outcome, summary = future.result()
                    settled = settle_launch(
                        session, attempt_id=attempt_id, generation=generation, outcome=outcome, summary=summary
                    )
                    if settled and failure is None:
                        task = session.task_control.get("task") or {}
                        if any(
                            event["attempt_id"] == attempt_id
                            and (event["task_id"], event["revision"]) == (task.get("id"), task.get("revision"))
                            for event in session.task_control.get("orchestration", {}).get("events", [])
                        ):
                            wake.append(attempt_id)
                except Exception as error:
                    # Stop admitting work but still preserve confirmed
                    # results from siblings that were already started.
                    if failure is None:
                        failure = error
                finally:
                    with stop_lock:
                        stops.pop(attempt_id, None)
                    _release_host_slot()
            if failure is None and wake:
                try:
                    if batch_results:
                        _deliver_results(session, notify)
                    else:
                        for attempt_id in wake:
                            notify(session, attempt_id)
                except Exception as error:
                    failure = error
