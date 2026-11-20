"""Bounded host-side IPC for one managed Worker process."""

from __future__ import annotations

import json
import math
import os
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread

from pepsicode.execution import process
from pepsicode.execution.runtime import bind_scope
from pepsicode.tooling import ToolContext

_FRAME_BYTES = 64 * 1024
_MAX_MESSAGES = 256
_MAX_STDERR_BYTES = 4096
_DEFAULT_WORKER_TIMEOUT = 300
_DRIVER_ENV_KEYS = (
    "PATH",
    "SystemRoot",
    "WINDIR",
    "SystemDrive",
    "PATHEXT",
    "COMSPEC",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
)


class WorkerProtocolError(RuntimeError):
    """The child did not follow the attempt's IPC contract."""


class WorkerProcessError(RuntimeError):
    def __init__(self, exit_code, stderr=""):
        self.exit_code = exit_code
        self.stderr = stderr
        detail = f": {stderr.strip()}" if stderr and stderr.strip() else ""
        super().__init__(f"Worker process exited with code {exit_code} without a successful result{detail}")


@dataclass(frozen=True)
class WorkerProcessResult:
    pid: int
    payload: dict


def _encode(frame):
    data = json.dumps(frame, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
    if len(data) > _FRAME_BYTES:
        raise WorkerProtocolError("Worker IPC frame exceeds the byte limit")
    return data


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def _read(stream):
    data = stream.readline(_FRAME_BYTES + 1)
    if not data:
        return None
    if len(data) > _FRAME_BYTES or not data.endswith(b"\n"):
        raise WorkerProtocolError("Oversized or incomplete Worker IPC frame")
    try:
        return json.loads(data, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise WorkerProtocolError("Malformed Worker IPC frame") from error


def _write(stream, data):
    stream.write(data)
    stream.flush()


def run_worker_process(
    argv,
    *,
    scope,
    cwd,
    run_id,
    attempt_id,
    generation,
    payload,
    operations,
    timeout=_DEFAULT_WORKER_TIMEOUT,
):
    """Run a trusted Worker executable and broker its explicitly supplied host operations.

    The caller selects the executable, workspace and operation handlers. Only
    these handlers can perform host actions. They must honor the supplied scope.
    """
    if (
        scope.parent_task_id is None
        or scope.permissions is None
        or scope.journal is None
        or scope.budget is None
        or scope.cancellation_event is None
    ):
        raise ValueError("Worker IPC requires a child scope with permissions, journal, shared budget and cancellation")
    if not isinstance(payload, dict) or type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Worker IPC requires a start object and a positive timeout")
    if any(not isinstance(value, str) or not 0 < len(value) <= 128 for value in (run_id, attempt_id, generation)):
        raise ValueError("Worker IPC requires bounded run, attempt and generation identities")
    if Path(cwd).resolve() != Path(scope.cwd).resolve():
        raise ValueError("Worker process cwd does not match its execution scope")
    operations = dict(operations)
    identity = {
        "version": 1,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "generation": generation,
        "nonce": secrets.token_hex(16),
    }
    start = _encode({**identity, "sequence": 0, "kind": "start", "payload": payload})
    context = ToolContext(
        str(cwd), permissions=scope.permissions, cancellation_event=scope.cancellation_event, execution=scope
    )
    env = {name: os.environ[name] for name in _DRIVER_ENV_KEYS if name in os.environ}
    deadline = time.monotonic() + timeout
    scope.deadline = min(scope.deadline, deadline) if scope.deadline is not None else deadline
    stderr_chunks: list[bytes] = []

    def stderr_text():
        raw = b"".join(stderr_chunks)[-_MAX_STDERR_BYTES:]
        return raw.decode("utf-8", errors="replace").strip()

    def drain_stderr(stream):
        total = 0
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    break
                if total < _MAX_STDERR_BYTES * 2:
                    stderr_chunks.append(chunk)
                    total += len(chunk)
        except OSError:
            pass

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError("Worker process exceeded its deadline")
        scope.check_cancelled()
        if scope.has_unknown:
            raise PermissionError("Reconcile the unknown execution before handling more Worker requests")

    def exchange(pool, function, *args):
        check()
        future = pool.submit(function, *args)
        while True:
            check()
            try:
                return future.result(timeout=0.05)
            except TimeoutError:
                if future.done():
                    return future.result()

    def broker(pool, child, handler, arguments):
        completed = Event()

        def monitor():
            while not completed.is_set():
                if (exit_code := child.poll()) is not None:
                    scope.cancellation_event.set()
                    return exit_code
                if time.monotonic() >= scope.deadline:
                    scope.cancellation_event.set()
                    return None
                completed.wait(0.05)
            return None

        watcher = pool.submit(monitor)
        try:
            return handler(scope, arguments)
        finally:
            completed.set()
            exit_code = watcher.result()
            if exit_code is None:
                exit_code = child.poll()
            if exit_code is not None:
                raise WorkerProcessError(exit_code, stderr_text())

    with bind_scope(scope), ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker-ipc") as pool:
        check()
        child = process.Popen(
            argv,
            cwd=str(cwd),
            context=context,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            reason="Start the managed Worker process",
        )
        stderr_thread = Thread(target=drain_stderr, args=(child.stderr,), daemon=True, name="worker-stderr")
        stderr_thread.start()
        try:
            scope.record(
                "orchestration/worker_process_started",
                run_id=run_id,
                attempt_id=attempt_id,
                generation=generation,
                pid=child.pid,
            )
            exchange(pool, _write, child.stdin, start)
            for sequence in range(1, _MAX_MESSAGES + 1):
                frame = exchange(pool, _read, child.stdout)
                if frame is None:
                    code = exchange(pool, child.wait)
                    stderr_thread.join(timeout=0.2)
                    raise WorkerProcessError(code, stderr_text())
                if (
                    not isinstance(frame, dict)
                    or any(frame.get(key) != value for key, value in identity.items())
                    or type(frame.get("sequence")) is not int
                    or frame["sequence"] != sequence
                    or not isinstance(frame.get("payload"), dict)
                ):
                    raise WorkerProtocolError("Worker IPC identity, sequence or payload is invalid")
                data = frame["payload"]
                if frame.get("kind") == "result":
                    if exchange(pool, _read, child.stdout) is not None:
                        raise WorkerProtocolError("Worker sent data after its result")
                    exit_code = exchange(pool, child.wait)
                    if exit_code:
                        stderr_thread.join(timeout=0.2)
                        raise WorkerProcessError(exit_code, stderr_text())
                    return WorkerProcessResult(child.pid, data)
                operation = data.get("operation")
                arguments = data.get("arguments")
                if frame.get("kind") != "request" or not isinstance(operation, str) or operation not in operations:
                    raise WorkerProtocolError("Worker requested an unsupported host operation")
                if not isinstance(arguments, dict):
                    raise WorkerProtocolError("Worker operation arguments must be an object")
                check()
                response = broker(pool, child, operations[operation], arguments)
                exchange(
                    pool,
                    _write,
                    child.stdin,
                    _encode({**identity, "sequence": sequence, "kind": "response", "payload": response}),
                )
            raise WorkerProtocolError("Worker IPC message limit exceeded")
        finally:
            try:
                process.release(child, context, drain=False)
            finally:
                child.stdin.close()
                child.stdout.close()
                stderr_thread.join(timeout=0.2)
                if child.stderr is not None:
                    child.stderr.close()
                exit_code = child.poll()
                excerpt = stderr_text()[:800] if exit_code else ""
                scope.record(
                    "orchestration/worker_process_ended",
                    run_id=run_id,
                    attempt_id=attempt_id,
                    generation=generation,
                    pid=child.pid,
                    exit_code=exit_code,
                    **({"stderr_excerpt": excerpt} if excerpt else {}),
                )
