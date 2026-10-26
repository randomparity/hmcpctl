"""Opt-in aggregate SSH output bounds, including real local flow control."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncssh
import pytest
from conftest import make_config

from hmcpctl.ssh.transport import HMCCLIError, run_hmc_command


class Reader:
    def __init__(self, data=b""):
        self.data = data
        self.sizes = []
        self.tasks = []

    async def read(self, size):
        assert 0 < size <= 4096
        self.sizes.append(size)
        self.tasks.append(asyncio.current_task())
        await asyncio.sleep(0)
        result, self.data = self.data[:size], self.data[size:]
        return result


def connection(monkeypatch, stdout=b"", stderr=b"", **metadata):
    process = SimpleNamespace(
        stdout=Reader(stdout),
        stderr=Reader(stderr),
        env={},
        command="lshmc -V",
        subsystem=None,
        exit_status=0,
        exit_signal=None,
        returncode=0,
        close=MagicMock(),
        wait_closed=AsyncMock(),
    )
    for key, value in metadata.items():
        setattr(process, key, value)
    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = False
    conn.create_process.return_value = process
    conn.abort = MagicMock()
    conn.run.side_effect = AssertionError("unbounded collector selected")
    monkeypatch.setattr(asyncssh, "connect", MagicMock(return_value=conn))
    return conn, process


@pytest.mark.asyncio
@pytest.mark.parametrize("include_stderr", [False, True])
@pytest.mark.parametrize(
    ("stdout", "stderr", "limit", "overflow"),
    [
        (b"abc", b"", 3, False),
        (b"", b"abc", 3, False),
        (b"abc", b"def", 6, False),
        (b"abc", b"def", 5, True),
        (b"abcd", b"", 3, True),
        (b"", b"abcd", 3, True),
        (b"abcd", b"efgh", 3, True),
        ("éé".encode(), b"", 4, False),
        ("éé".encode(), b"", 3, True),
        (b"a" * 65536, b"x", 65536, True),
    ],
    ids=[
        "stdout-exact",
        "stderr-exact",
        "combined-exact",
        "combined-over",
        "stdout-over",
        "stderr-over",
        "both-over",
        "utf8-exact",
        "utf8-over",
        "chunked-over",
    ],
)
async def test_limit_stdout_stderr_combined(
    monkeypatch, stdout, stderr, limit, overflow, include_stderr
):
    conn, process = connection(monkeypatch, stdout, stderr)
    if overflow:
        with pytest.raises(HMCCLIError, match="completion.*unknown") as caught:
            await run_hmc_command(
                make_config(),
                "lshmc -V",
                max_output_bytes=limit,
                include_stderr=include_stderr,
            )
        assert not isinstance(caught.value.__cause__, asyncssh.ProcessError)
        conn.abort.assert_called_once()
        process.close.assert_called_once()
    else:
        result = await run_hmc_command(
            make_config(),
            "lshmc -V",
            max_output_bytes=limit,
            include_stderr=include_stderr,
        )
        expected = stdout.decode()
        if include_stderr and stderr:
            expected += ("\n" if expected else "") + "[stderr]\n" + stderr.decode()
        assert result == expected
        conn.abort.assert_not_called()
    conn.create_process.assert_awaited_once_with(
        "lshmc -V", encoding=None, window=4096, max_pktsize=4096
    )
    assert process.stdout.sizes and process.stderr.sizes
    assert all(task.done() for task in process.stdout.tasks + process.stderr.tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["success", "exit", "signal", "missing", "utf8", "overflow"]
)
async def test_bounded_exit_metadata(monkeypatch, case):
    metadata = {}
    if case in {"exit", "overflow"}:
        metadata = {"exit_status": 3, "returncode": 3}
    elif case == "signal":
        metadata = {
            "exit_status": -1,
            "exit_signal": ("TERM", False, "", ""),
            "returncode": -15,
        }
    elif case == "missing":
        metadata = {"exit_status": None, "returncode": None}
    stdout = b"\xff" if case == "utf8" else b"out"
    stderr = b"x" * 33 if case == "overflow" else b"err"
    _, process = connection(monkeypatch, stdout, stderr, **metadata)
    if case == "success":
        assert (
            await run_hmc_command(make_config(), "lshmc -V", max_output_bytes=32)
            == "out"
        )
        process.wait_closed.assert_awaited_once()
        return
    with pytest.raises(HMCCLIError) as caught:
        await run_hmc_command(make_config(), "lshmc -V", max_output_bytes=32)
    cause = caught.value.__cause__
    if case in {"exit", "signal"}:
        assert isinstance(cause, asyncssh.ProcessError)
        assert cause.exit_status == process.exit_status
        assert cause.exit_signal == process.exit_signal
        assert cause.returncode == process.returncode
        assert cause.stdout == "out" and cause.stderr == "err"
    else:
        assert "completion" in str(caught.value) and "unknown" in str(caught.value)
        assert not isinstance(cause, asyncssh.ProcessError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "stop"),
    [
        (phase, stop)
        for phase in ["create", "stdout", "stderr", "exit"]
        for stop in ["cancel", "timeout", "reader-error"]
        if stop != "reader-error" or phase in {"stdout", "stderr"}
    ],
)
async def test_bounded_cleanup(monkeypatch, phase, stop):
    conn, process = connection(monkeypatch)
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        try:
            if stop == "reader-error":
                raise OSError("read failed")
            await asyncio.Future()
        finally:
            finished.set()

    if phase == "create":
        conn.create_process.side_effect = blocked
    elif phase == "exit":
        process.wait_closed.side_effect = blocked
    else:
        getattr(process, phase).read = blocked
    config = make_config(ssh_timeout=0.05 if stop == "timeout" else 5)
    task = asyncio.create_task(run_hmc_command(config, "lshmc -V", max_output_bytes=32))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if stop == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(HMCCLIError):
                await asyncio.wait_for(task, 2)
        assert finished.is_set()
        conn.abort.assert_called_once()
        if phase != "create":
            process.close.assert_called_once()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "10"])
async def test_invalid_limit_precedes_io(monkeypatch, limit):
    connect = MagicMock()
    monkeypatch.setattr(asyncssh, "connect", connect)
    with pytest.raises(ValueError, match="max_output_bytes"):
        await run_hmc_command(make_config(), "lshmc -V", max_output_bytes=limit)
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_omitted_limit_compatibility(monkeypatch):
    conn, _ = connection(monkeypatch)
    conn.run.side_effect = None
    conn.run.return_value = SimpleNamespace(stdout="old\n", stderr="detail")
    for kwargs in [{}, {"max_output_bytes": None}]:
        assert (
            await run_hmc_command(
                make_config(), "lshmc -V", include_stderr=True, **kwargs
            )
            == "old\n[stderr]\ndetail"
        )
    conn.create_process.assert_not_awaited()
    assert conn.run.await_count == 2
    conn.run.assert_awaited_with("lshmc -V", check=True, timeout=300.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["success", "overflow", "delayed-exit"])
async def test_loopback_bounded_streams(monkeypatch, case):
    peers = []
    started = asyncio.Event()
    release = asyncio.Event()
    overflow = case == "overflow"

    class Server(asyncssh.SSHServer):
        def connection_made(self, conn):
            peers.append(conn)

        def begin_auth(self, username):
            return False

    async def serve(process):
        process.stdout.write(b"a" * 17000)
        process.stderr.write(b"b" * (70000 if overflow else 9000))
        started.set()
        if case == "delayed-exit":
            process.stdout.write_eof()
            await release.wait()
        process.exit(0)

    key = asyncssh.generate_private_key("ssh-ed25519")
    async with asyncssh.create_server(
        Server,
        "127.0.0.1",
        0,
        server_host_keys=[key],
        process_factory=serve,
        encoding=None,
    ) as listener:
        connect = asyncssh.connect
        monkeypatch.setattr(
            asyncssh,
            "connect",
            lambda **kw: connect(port=listener.get_port(), config=None, **kw),
        )
        config = make_config(host="127.0.0.1", ssh_verify_host_key=False, ssh_timeout=5)
        task = asyncio.create_task(
            run_hmc_command(
                config, "fixed-read", max_output_bytes=26000, include_stderr=True
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            if case == "delayed-exit":
                assert not task.done()
                release.set()
            if overflow:
                with pytest.raises(HMCCLIError, match="completion.*unknown"):
                    await task
            else:
                assert await task == ("a" * 17000 + "\n[stderr]\n" + "b" * 9000)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for peer in peers:
                peer.abort()
            await asyncio.gather(*(peer.wait_closed() for peer in peers))
