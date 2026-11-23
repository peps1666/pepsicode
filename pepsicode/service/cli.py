"""CLI for the resident service (M3 §19.1).

Commands:

- ``start``   — spawn a detached service process for the workspace (refuses
                while the manual-stop marker exists unless ``--clear-stop``).
- ``stop``    — explicit stop via authenticated RPC; persists the manual-stop
                marker so a watchdog cannot restart the service.
- ``status``  — read the persisted service identity and pid liveness.
- ``health``  — read the latest component-health snapshot.
- ``attach``  — attach a terminal console to a resident session (M3 §19.1);
                exiting the console detaches and leaves running turns alone.
- ``foreground`` — run the service in this process (the entry point a real
                service manager such as NSSM or systemd calls directly).

The service binds to loopback only and requires a token; ``start`` writes the
token to ``.pepsi-code/service/token`` (best-effort 0600) so configured
clients can read it without it crossing a terminal scrollback.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from pepsicode.logging_config import get_logger, setup_logging
from pepsicode.service import state as service_state

logger = get_logger("service")

_START_TIMEOUT = 20.0
_STOP_TIMEOUT = 15.0


def _token_path(workspace: str | Path) -> Path:
    return service_state.service_dir(workspace) / "token"


def _write_token_file(workspace: str | Path, token: str) -> None:
    path = _token_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(token, encoding="utf-8")
        if os.name == "posix":
            os.chmod(path, 0o600)
    except OSError as error:
        logger.warning("Could not persist the service token file: %s", error)


def _read_token(workspace: str | Path) -> str | None:
    try:
        return _token_path(workspace).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _load_settings(workspace: Path) -> dict:
    from pepsicode.config import load_runtime_config

    try:
        return load_runtime_config(str(workspace))
    except Exception as error:  # noqa: BLE001
        raise SystemExit(f"Invalid workspace settings: {error}") from error


def _service_foreground_command(workspace: Path, port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            "service",
            "foreground",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
        ]
    return [
        sys.executable,
        "-I",
        "-m",
        "pepsicode.service",
        "foreground",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    ]


def cmd_start(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    settings = _load_settings(workspace)
    try:
        service_state.ensure_start_allowed(workspace, clear_stop=args.clear_stop)
    except service_state.ServiceStateError as error:
        print(error)
        return 1
    running = service_state.running_service(workspace)
    if running:
        print(f"Service already running (pid {running['pid']}, generation {running.get('generation', '')[:8]}).")
        return 1
    port = args.port if args.port is not None else int((settings.get("service") or {}).get("port", 0))
    token = secrets.token_urlsafe(32)
    log_path = service_state.service_dir(workspace) / "service.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PEPSI_CODE_SERVER_TOKEN": token}
    command = _service_foreground_command(workspace, port)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            | subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "ab") as log_stream:
        process = subprocess.Popen(
            command,
            env=env,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(workspace),
            **kwargs,
        )
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        state = service_state.read_state(workspace)
        if state and state.get("pid") == process.pid and state.get("port") is not None:
            _write_token_file(workspace, token)
            print(
                json.dumps(
                    {"started": True, "url": f"ws://127.0.0.1:{state['port']}", "pid": process.pid}, ensure_ascii=False
                )
            )
            return 0
        if process.poll() is not None:
            print(f"Service process exited during startup (code {process.returncode}); see {log_path}")
            return 1
        time.sleep(0.2)
    print(f"Service did not report readiness within {_START_TIMEOUT:.0f}s; see {log_path}")
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    state = service_state.running_service(workspace)
    if state is None:
        print("Service is not running.")
        return 0
    token = _read_token(workspace)
    if token is None:
        print("Service token file is missing; cannot issue an authenticated shutdown.")
        return 1
    if not _rpc_shutdown(int(state["port"]), token):
        print("Shutdown request failed; the service may already be stopping.")
        return 1
    deadline = time.monotonic() + _STOP_TIMEOUT
    while time.monotonic() < deadline:
        if service_state.running_service(workspace) is None:
            break
        time.sleep(0.2)
    marker = service_state.read_manual_stop(workspace)
    if marker:
        print("Service stopped; the manual-stop marker will block automatic restarts.")
    else:
        print("Service stopped.")
    return 0


def _rpc_shutdown(port: int, token: str) -> bool:
    """Send an authenticated ``service/shutdown`` request; returns success."""
    try:
        from websockets.sync.client import connect
    except ImportError:
        print("The 'websockets' package is required for the stop command")
        return False
    try:
        with connect(f"ws://127.0.0.1:{port}", open_timeout=5, close_timeout=5) as ws:
            ws.send(json.dumps({"kind": "request", "id": 1, "method": "connection/auth", "params": {"token": token}}))
            auth = json.loads(ws.recv(timeout=5))
            if not auth.get("result", {}).get("authenticated"):
                print("Authentication failed")
                return False
            ws.send(json.dumps({"kind": "request", "id": 2, "method": "service/shutdown", "params": {}}))
            json.loads(ws.recv(timeout=5))
        return True
    except Exception as error:  # noqa: BLE001
        print(f"Shutdown RPC failed: {error}")
        return False


def cmd_status(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    state = service_state.read_state(workspace)
    running = service_state.running_service(workspace)
    marker = service_state.read_manual_stop(workspace)
    payload = {
        "running": running is not None,
        "state": state,
        "manual_stop": marker,
        "health": service_state.read_health(workspace),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if running is not None else 1


def cmd_health(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    health = service_state.read_health(workspace)
    if health is None:
        print("No health snapshot available; is the service running?")
        return 1
    print(json.dumps(health, ensure_ascii=False, indent=2))
    return 0 if health.get("healthy") else 1


def cmd_foreground(args: argparse.Namespace) -> int:
    """Run the service in this process — the service-manager entry point."""
    from contextlib import ExitStack

    from pepsicode.orchestration.runtime import own_workspace

    workspace = Path(args.workspace).resolve()
    with ExitStack() as ownership:
        try:
            ownership.enter_context(own_workspace(service_state.service_dir(workspace)))
            service_state.ensure_start_allowed(workspace)
            if service_state.running_service(workspace):
                raise service_state.ServiceStateError("A service already owns this workspace")
        except RuntimeError as error:
            print(error)
            return 1
        return _run_foreground(args, workspace)
    return 1


def _run_foreground(args: argparse.Namespace, workspace: Path) -> int:
    from pepsicode.server.app import PepsiCodeServer
    from pepsicode.service.host import ServiceHost

    token = os.environ.get("PEPSI_CODE_SERVER_TOKEN")
    if not token:
        raise SystemExit("Set PEPSI_CODE_SERVER_TOKEN before running the service in the foreground")
    setup_logging(level=args.log_level)
    settings = _load_settings(workspace)
    runtime = {**settings, "orchestration": {**settings.get("orchestration", {}), "executionMode": "service"}}
    port = args.port if args.port is not None else int((settings.get("service") or {}).get("port", 0))
    generation = secrets.token_urlsafe(8)
    host = ServiceHost(str(workspace), runtime, token=token, port=port, generation=generation)
    server = PepsiCodeServer(port=port, host="127.0.0.1", token=token, service_host=host)

    import asyncio

    async def announce() -> None:
        # Persist the identity (pid/generation/bound port) once the listener
        # is up so `start` can report readiness and clients can connect.
        while server._bound_port is None:
            await asyncio.sleep(0.05)
        service_state.write_running_state(
            workspace, port=server._bound_port, token=token, execution_mode="service", generation=generation
        )

    async def run() -> None:
        import websockets  # noqa: F401 — fail fast when the dependency is missing

        announce_task = asyncio.create_task(announce())
        try:
            await server.start()
        finally:
            announce_task.cancel()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Service shutdown requested (keyboard interrupt)")
    finally:
        service_state.replace_state_on_exit(workspace)
        logger.info("Service stopped")
    return 0


def _resolve_attach_token(workspace: Path, token_file: str | None) -> str | None:
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    env_token_file = os.environ.get("PEPSICODE_SERVICE_TOKEN_FILE")
    if env_token_file:
        try:
            return Path(env_token_file).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return _read_token(workspace)


def cmd_attach(args: argparse.Namespace) -> int:
    """Attach a terminal console to the resident service (M3 §19.1)."""
    workspace = Path(args.workspace).resolve()
    running = service_state.running_service(workspace)
    url = args.url or os.environ.get("PEPSICODE_SERVICE_URL")
    if not url:
        if running is None:
            print("Service is not running; start it first: python -m pepsicode.service start")
            return 1
        url = f"ws://127.0.0.1:{int(running['port'])}"
    token = _resolve_attach_token(workspace, args.token_file)
    if token is None:
        print("Service token unavailable; pass --token-file or set PEPSICODE_SERVICE_TOKEN_FILE")
        return 1
    from pepsicode.tui.service_console import run_service_console

    return run_service_console(url=url, token=token, session_id=args.session, list_only=args.list)


def _add_workspace(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--workspace", default=str(Path.cwd()), help="Workspace the service owns")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pepsicode resident service (M3)")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Start the service as a detached process")
    _add_workspace(start)
    start.add_argument("--port", type=int, default=None)
    start.add_argument("--clear-stop", action="store_true", help="Authorize starting over a manual stop")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="Stop the service explicitly (blocks watchdog restarts)")
    _add_workspace(stop)
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="Show the persisted service identity and pid state")
    _add_workspace(status)
    status.set_defaults(func=cmd_status)

    health = sub.add_parser("health", help="Show the latest component-health snapshot")
    _add_workspace(health)
    health.set_defaults(func=cmd_health)

    attach = sub.add_parser("attach", help="Attach a terminal console to a resident session")
    _add_workspace(attach)
    attach.add_argument("--session", default=None, metavar="SESSION_ID|latest", help="Session to attach (default: ask)")
    attach.add_argument("--list", action="store_true", help="List resident sessions and exit")
    attach.add_argument("--url", default=None, help="Override the service URL (ws://host:port)")
    attach.add_argument("--token-file", default=None, help="Override the service token file path")
    attach.set_defaults(func=cmd_attach)

    fg = sub.add_parser("foreground", help="Run the service in this process (for service managers)")
    _add_workspace(fg)
    fg.add_argument("--port", type=int, default=None)
    fg.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    fg.set_defaults(func=cmd_foreground)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
