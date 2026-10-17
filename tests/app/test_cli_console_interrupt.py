"""Real SIGINT through the CLI and real release logic, with offline SSH transport."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

DATA = b"boot\n\x00\xff\x1b[31m"
SOURCE = r"""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
sys.path[:0] = ["tests", "tests/unit"]
from test_console_capture import BANNER, CONTENTION, EofProcess, FakeConnection, FakeProcess
from hmcpctl import cli
from hmcpctl.config import HMCConfig
from hmcpctl.cli_commands import runtime
from hmcpctl.operations.lpar import console as operation
from hmcpctl.ssh import console
output, proven, late = Path(sys.argv[1]), sys.argv[2] == "True", sys.argv[3] == "True"
data = b"boot\n\x00\xff\x1b[31m" + (console.HELD_SENTINEL if late else b"")
stream = FakeConnection([EofProcess(BANNER, data)])
probe = FakeConnection([EofProcess(BANNER) if proven else FakeProcess(CONTENTION)])
commands = []
async def run_command(config, command):
    commands.append(command)
    return "Close command sent"
class Client:
    config = HMCConfig.from_mapping({"host": "hmc.test", "user": "tester"})
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
with (
    patch.object(runtime, "client", Client),
    patch.object(operation, "resolve_system_uuid", AsyncMock(return_value="system-uuid")),
    patch.object(operation, "resolve_lpar_uuid", AsyncMock(return_value="lpar-uuid")),
    patch.object(console, "open_hmc_connection", AsyncMock(side_effect=[stream, probe])),
    patch.object(console, "run_hmc_command", run_command),
    patch.object(console, "_RELEASE_PROBE_SECONDS", 0.05),
):
    try:
        cli.app(args=["lpars", "capture-console", "partition-a", "--system", "system-a",
                      "--duration", "60", "--idle-timeout", "60", "--output", str(output)])
    finally:
        output.with_suffix(".proof").write_text(json.dumps({
            "stream_closed": stream.closed, "probe_closed": probe.closed,
            "stream_calls": len(stream.create_process_calls),
            "probe_calls": len(probe.create_process_calls), "commands": commands,
        }))
"""


@pytest.mark.skipif(os.name != "posix", reason="SIGINT subprocess contract is POSIX")
@pytest.mark.parametrize("proven", [True, False])
@pytest.mark.parametrize("late", [False, True])
def test_sigint_retains_flushed_file_after_real_release(tmp_path, proven, late):
    from hmcpctl.ssh.console import HELD_SENTINEL

    target = tmp_path / "console.bin"
    expected = b"\r\n Open in progress  \r\n " + DATA + (HELD_SENTINEL if late else b"")
    # communicate drains stdout and stderr while the parent waits for flushed bytes.
    with (
        subprocess.Popen(
            [sys.executable, "-c", SOURCE, str(target), str(proven), str(late)],
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as child,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        captured = pool.submit(child.communicate)
        try:
            deadline = time.monotonic() + 15
            while not target.exists() or target.read_bytes() != expected:
                assert child.poll() is None, captured.result(timeout=1)
                assert time.monotonic() < deadline, "capture did not flush ready bytes"
                time.sleep(0.01)
            child.send_signal(signal.SIGINT)
            stdout, stderr = captured.result(timeout=15)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
    proof = json.loads(target.with_suffix(".proof").read_text())
    assert proof == {
        "stream_closed": True,
        "probe_closed": True,
        "stream_calls": 1,
        "probe_calls": 1,
        "commands": [],
    }
    assert child.returncode == (130 if proven else 3), stderr
    assert stdout == b""
    assert target.read_bytes() == expected
    assert target.stat().st_mode & 0o777 == 0o600
    assert b"stop reason: interrupted" in stderr
    assert f"released: {str(proven).lower()}".encode() in stderr
    if not proven:
        assert b"rmvterm" not in stderr
        assert b"check console ownership" in stderr
