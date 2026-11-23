"""Durable service lifecycle state.

All files live under ``<workspace>/.pepsi-code/service/``:

- ``service.json`` — the running service identity: pid, generation, port,
  token fingerprint.  Written by the service at startup; a second ``start``
  refuses to launch while the recorded pid is alive.
- ``stopped.json`` — a manual-stop marker.  Written by an explicit
  ``service/shutdown``; while it exists an automatic restart (a watchdog
  calling plain ``start``) must refuse.  Only a human ``start --clear-stop``
  removes it.
- ``health.json`` — periodically refreshed component health snapshot, read
  by the CLI so ``status``/``health`` never depend on the WebSocket.

Writes are atomic (temp file + ``os.replace``) so a crash never leaves a
half-written identity behind.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

SERVICE_STATE_VERSION = 1

_STATE_FILE = "service.json"
_STOP_FILE = "stopped.json"
_HEALTH_FILE = "health.json"


class ServiceStateError(RuntimeError):
    """Raised when the requested lifecycle transition is not allowed."""


def service_dir(workspace: str | Path) -> Path:
    """Directory holding this workspace's service state files."""
    return Path(workspace) / ".pepsi-code" / "service"


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe that never signals the target process.

    ``os.kill(pid, 0)`` is unsafe on Windows (non-signal values terminate the
    process), so Windows uses ``OpenProcess`` + ``GetExitCodeProcess``.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def token_fingerprint(token: str) -> str:
    """Short fingerprint so the CLI can confirm it talks to the right service
    without storing the token itself."""
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_state(workspace: str | Path) -> dict | None:
    """Read ``service.json``; ``None`` when absent or unreadable."""
    return _read_json(service_dir(workspace) / _STATE_FILE)


def running_service(workspace: str | Path) -> dict | None:
    """Return the state of a *live* service process, else ``None``."""
    state = read_state(workspace)
    if state and state.get("pid") and _pid_alive(int(state["pid"])):
        return state
    return None


def write_running_state(workspace: str | Path, *, port: int, token: str, execution_mode: str, generation=None) -> dict:
    """Atomically claim the service identity for this workspace."""
    previous = read_state(workspace)
    generation = generation or uuid.uuid4().hex
    state = {
        "version": SERVICE_STATE_VERSION,
        "pid": os.getpid(),
        "generation": generation,
        "previous_generation": previous.get("generation") if previous else None,
        "port": int(port),
        "host": "127.0.0.1",
        "token_fingerprint": token_fingerprint(token),
        "execution_mode": execution_mode,
        "started_at": _now(),
    }
    atomic_write_json(service_dir(workspace) / _STATE_FILE, state)
    return state


def replace_state_on_exit(workspace: str | Path) -> None:
    """Neutralize the identity file so a dead pid is never reported running."""
    state = read_state(workspace)
    if not state:
        return
    state["pid"] = 0
    state["stopped_at"] = _now()
    atomic_write_json(service_dir(workspace) / _STATE_FILE, state)


def read_manual_stop(workspace: str | Path) -> dict | None:
    return _read_json(service_dir(workspace) / _STOP_FILE)


def mark_manual_stop(workspace: str | Path, *, reason: str = "shutdown") -> dict:
    """Persist the human-stop marker that blocks watchdog restarts."""
    state = read_state(workspace) or {}
    marker = {
        "version": SERVICE_STATE_VERSION,
        "stopped_at": _now(),
        "generation": state.get("generation"),
        "reason": str(reason)[:200],
    }
    atomic_write_json(service_dir(workspace) / _STOP_FILE, marker)
    return marker


def clear_manual_stop(workspace: str | Path) -> bool:
    (path := service_dir(workspace) / _STOP_FILE).unlink(missing_ok=True)
    return not path.exists()


def ensure_start_allowed(workspace: str | Path, *, clear_stop: bool = False) -> None:
    """Refuse a plain start while the manual-stop marker exists.

    A watchdog restarting the service calls the same entry point, so the
    marker blocks it too; only an explicit human ``--clear-stop`` proceeds.
    """
    marker = read_manual_stop(workspace)
    if marker is None:
        return
    if clear_stop:
        clear_manual_stop(workspace)
        return
    raise ServiceStateError(
        "This workspace's service was stopped manually at "
        f"{marker.get('stopped_at', 'unknown time')}; a watchdog must not restart it. "
        "Run `python -m pepsicode.service start --clear-stop` to authorize a new start."
    )


def read_health(workspace: str | Path) -> dict | None:
    return _read_json(service_dir(workspace) / _HEALTH_FILE)


def write_health(workspace: str | Path, health: dict) -> None:
    atomic_write_json(service_dir(workspace) / _HEALTH_FILE, health)
