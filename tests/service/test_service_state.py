"""Durable service state: single instance, manual-stop marker, health file."""

from __future__ import annotations

import json
import os

import pytest

from pepsicode.service import state as service_state


def _write_pid_file(workspace, pid):
    path = service_state.service_dir(workspace) / "service.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "pid": pid, "generation": "g" * 32, "port": 1234}), encoding="utf-8")


def test_state_roundtrip_and_atomicity(tmp_path):
    state = service_state.write_running_state(tmp_path, port=8765, token="secret", execution_mode="service")
    assert state["pid"] == os.getpid()
    assert state["execution_mode"] == "service"
    assert service_state.token_fingerprint("secret") == state["token_fingerprint"]
    reread = service_state.read_state(tmp_path)
    assert reread["generation"] == state["generation"]
    # No stray temp files left behind by the atomic write.
    assert [p.name for p in (service_state.service_dir(tmp_path)).glob("*.tmp")] == []


def test_running_service_detects_liveness(tmp_path):
    _write_pid_file(tmp_path, os.getpid())
    running = service_state.running_service(tmp_path)
    assert running is not None and running["generation"] == "g" * 32

    _write_pid_file(tmp_path, 999999999)
    assert service_state.running_service(tmp_path) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX liveness probe semantics")
def test_pid_alive_does_not_signal_the_target(tmp_path):
    # A pid we may not signal (permission error) must count as alive, and a
    # negative pid must not — os.kill(pid, 0) semantics, never signal 9.
    assert service_state._pid_alive(-1) is False


def test_new_identity_replaces_the_old_generation(tmp_path):
    """A second start supersedes the old identity; the old generation is
    recorded so a stale operator cannot mistake it for the current one."""
    first = service_state.write_running_state(tmp_path, port=1000, token="a", execution_mode="service")
    second = service_state.write_running_state(tmp_path, port=1001, token="b", execution_mode="service")
    assert second["generation"] != first["generation"]
    assert second["previous_generation"] == first["generation"]
    assert service_state.read_state(tmp_path)["port"] == 1001


def test_manual_stop_marker_blocks_restart_until_cleared(tmp_path):
    service_state.mark_manual_stop(tmp_path, reason="operator stop")
    with pytest.raises(service_state.ServiceStateError, match="--clear-stop"):
        service_state.ensure_start_allowed(tmp_path)
    service_state.ensure_start_allowed(tmp_path, clear_stop=True)
    assert service_state.read_manual_stop(tmp_path) is None
    # After clearing, a plain start is allowed again.
    service_state.ensure_start_allowed(tmp_path)


def test_health_snapshot_roundtrip(tmp_path):
    service_state.write_health(tmp_path, {"healthy": False, "components": {"storage": {"ok": False}}})
    health = service_state.read_health(tmp_path)
    assert health["components"]["storage"]["ok"] is False


def test_replace_state_on_exit_neutralizes_pid(tmp_path):
    service_state.write_running_state(tmp_path, port=1, token="t", execution_mode="service")
    service_state.replace_state_on_exit(tmp_path)
    assert service_state.running_service(tmp_path) is None
    state = service_state.read_state(tmp_path)
    assert state["pid"] == 0 and "stopped_at" in state
