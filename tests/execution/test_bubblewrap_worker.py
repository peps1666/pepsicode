from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from pepsicode.sandbox.bubblewrap import BubblewrapBackend
from pepsicode.sandbox.types import SandboxMode, SandboxPolicy


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("bwrap"), reason="Requires Linux with Bubblewrap")
def test_worker_namespace_permits_checkout_but_blocks_host_resources(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("fixture private data", encoding="utf-8")
    (checkout / "alias").symlink_to(sibling)
    script = """
import os, pathlib, socket, sys
assert not pathlib.Path(sys.argv[1]).exists()
assert not pathlib.Path('../sibling.txt').exists()
assert not pathlib.Path('alias').exists()
assert not pathlib.Path('/etc/shadow').exists()
assert 'PEPSI_FIXTURE_SECRET' not in os.environ
pathlib.Path('result.txt').write_text('checkout writable')
assert socket.if_nameindex() == [(1, 'lo')]
"""
    command = BubblewrapBackend().confine(
        ["/usr/bin/python3", "-I", "-c", script, str(sibling)],
        SandboxPolicy(SandboxMode.WORKSPACE_WRITE, str(checkout)),
    )
    result = subprocess.run(
        command.argv,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PEPSI_FIXTURE_SECRET": "fixture"},
    )
    assert result.returncode == 0, result.stderr
    assert (checkout / "result.txt").read_text() == "checkout writable"
