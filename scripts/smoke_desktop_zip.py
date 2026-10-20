"""Exercise the real extracted backend with no Python/Node on its PATH."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from websockets.sync.client import connect


def smoke(archive: Path) -> None:
    expected = archive.with_suffix(archive.suffix + ".sha256").read_text(encoding="utf-8").split()[0]
    with archive.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == expected, "ZIP checksum mismatch"
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="ZIP 验收 space ", dir=root / "build") as temporary:
        directory = Path(temporary)
        application = directory / "解压后 application"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(application)
        executable = application / "resources/backend/pepsicode-server.exe"
        assert (application / "Pepsicode.exe").is_file(), "Desktop executable missing"
        assert (application / "resources/app.asar").is_file(), "Renderer archive missing"
        home = directory / "isolated-user"
        workspace = directory / "项目 workspace"
        home.mkdir()
        workspace.mkdir()
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("PEPSI_", "PYTHON", "CONDA", "ANTHROPIC_", "ELECTRON_"))
        }
        environment.update(
            {
                "PATH": str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"),
                "USERPROFILE": str(home),
                "HOME": str(home),
                "PEPSI_CODE_MODEL_MODE": "mock",
                "PEPSI_CODE_SERVER_TOKEN": secrets.token_hex(32),
                "PEPSI_CODE_SERVER_ORIGINS": '["null","file://"]',
            }
        )
        diagnostics = subprocess.run(
            [str(executable), "--self-test"],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert json.loads(diagnostics.stdout)["frozen"] is True
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [str(executable), "--port", str(port)],
            cwd=workspace,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        lines: queue.Queue[str] = queue.Queue()

        def collect() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)

        threading.Thread(target=collect, daemon=True).start()
        try:
            deadline = time.monotonic() + 30
            output = ""
            while f"pepsicode server listening on ws://127.0.0.1:{port}" not in output:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError(f"Backend did not become ready: {output}")
                try:
                    output += lines.get(timeout=0.2)
                except queue.Empty:
                    pass
            with connect(f"ws://127.0.0.1:{port}", origin="null", open_timeout=10) as websocket:
                request_id = 0
                events = []

                def rpc(method: str, params: dict) -> dict:
                    nonlocal request_id
                    request_id += 1
                    websocket.send(
                        json.dumps({"kind": "request", "id": request_id, "method": method, "params": params})
                    )
                    while True:
                        message = json.loads(websocket.recv(timeout=60))
                        if message.get("kind") == "event":
                            events.append(message)
                        if message.get("kind") == "response" and message.get("id") == request_id:
                            assert not message.get("error"), message.get("error")
                            return message.get("result", {})

                assert rpc("connection/auth", {"token": environment["PEPSI_CODE_SERVER_TOKEN"]})["authenticated"]
                session = rpc("session/create", {"cwd": str(workspace)})
                assert session["session_id"] and session["isMock"]
                rpc("turn/run", {"input": "Hello, verify the ZIP desktop backend."})
                assert any(
                    event.get("event") == "message/delta" and event.get("data", {}).get("text") for event in events
                )
                print(
                    "PASS: SHA-256, extracted Unicode/space path, frozen resources, authentication, session/create, mock turn/run; PATH contains no Python/Node."
                )
        finally:
            process.terminate()
            process.wait(timeout=10)
            if process.stdout:
                process.stdout.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    smoke(parser.parse_args().archive.resolve())
