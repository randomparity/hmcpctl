"""Caller-declared secret redaction at the SSH dispatch boundary (ADR 0246).

Every diagnostic a secret-bearing ``run_hmc_command`` produces — the translated
error, its traceback, and the ``asyncssh`` log records emitted during the call —
must omit the declared values while keeping the non-secret context actionable.
asyncssh is mocked as in ``test_ssh.py``.
"""

from __future__ import annotations

import logging
import shlex
import traceback
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest
from conftest import make_config

from hmcpctl.ssh.commands import (
    _parse_lshwres_output,
    build_attribute_record,
    parse_hmc_result_rows,
)
from hmcpctl.ssh.transport import REDACTED, HMCCLIError, run_hmc_command

# An apostrophe and a backslash: repr() escapes both, and shlex.quote rewrites the first.
SECRET = "s3cr'et\\655"  # pragma: allowlist secret - redaction fixture
# Fragments either side of the backslash; "655" alone also matches the worktree path.
FRAGMENTS = ("s3cr", "\\655")
CMD = f"chhmcusr -u op -t passwd -v {shlex.quote(SECRET)}"


def _conn(run_side_effect=None) -> AsyncMock:
    result = MagicMock()
    result.stdout = ""
    result.stderr = ""
    conn = AsyncMock()
    conn.run = AsyncMock(return_value=result, side_effect=run_side_effect)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _process_error(**overrides) -> asyncssh.ProcessError:
    fields = {
        "env": {},
        "command": CMD,
        "subsystem": None,
        "exit_status": 1,
        "exit_signal": None,
        "returncode": 1,
        "stdout": "",
        "stderr": "",
    }
    fields.update(overrides)
    return asyncssh.ProcessError(**fields)


def _assert_no_secret(text: str) -> None:
    for fragment in FRAGMENTS:
        assert fragment not in text


# (connect side effect, run side effect, text the message must keep)
_FAILURES = {
    "connect-timeout": (TimeoutError("timed out"), None, "timed out after 300s"),
    "run-timeout": (None, TimeoutError("timed out"), "timed out after 300s"),
    "connection-refused": (
        ConnectionRefusedError(f"refused {SECRET}"),
        None,
        "connection failed",
    ),
    "invalid-value": (ValueError(f"bad {SECRET}"), None, "connection failed"),
    "exit-status-stdout-echo": (
        None,
        _process_error(stdout=f"HSCL0001 The password {SECRET} is invalid"),
        "HSCL0001",
    ),
    "signal-stderr-echo": (
        None,
        _process_error(
            exit_status=None, exit_signal="TERM", returncode=-15, stderr=SECRET
        ),
        "signal TERM",
    ),
    "asyncssh-error": (
        asyncssh.Error(asyncssh.DISC_BY_APPLICATION, f"refused {SECRET}"),
        None,
        "SSH command failed",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_FAILURES))
async def test_secret_failure_is_redacted(case):
    connect_effect, run_effect, kept = _FAILURES[case]
    conn = _conn(run_effect)
    connect = (
        patch("hmcpctl.ssh.transport.asyncssh.connect", side_effect=connect_effect)
        if connect_effect is not None
        else patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn)
    )
    with connect, pytest.raises(HMCCLIError) as exc_info:
        await run_hmc_command(make_config(), CMD, secrets=[SECRET])

    error = exc_info.value
    _assert_no_secret(str(error))
    _assert_no_secret("".join(traceback.format_exception(error)))
    assert error.__cause__ is None
    assert error.__context__ is None
    assert kept in str(error)
    if run_effect is not None:
        conn.run.assert_called_once_with(CMD, check=True, timeout=300.0)
        assert "chhmcusr -u op -t passwd -v" in str(error)
        assert REDACTED in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("secret", ["hunter2pass ", "hunter2pass\n"])
async def test_trailing_whitespace_secret_echoed_at_detail_end_is_redacted(
    secret, stream
):
    # The builders strip() the detail, cutting the secret's trailing whitespace.
    conn = _conn(_process_error(**{stream: f"HSCL1234 {secret}"}))
    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError) as exc_info,
    ):
        await run_hmc_command(make_config(), "chhmcusr -u op", secrets=[secret])

    assert "hunter2pass" not in str(exc_info.value)
    assert "HSCL1234" in str(exc_info.value)


@pytest.mark.asyncio
async def test_overlapping_secrets_leave_no_fragment():
    conn = _conn(_process_error(stderr="echo XYZW and ZW"))
    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError) as exc_info,
    ):
        await run_hmc_command(make_config(), "x -v XYZW", secrets=["XYZ", "ZW"])

    message = str(exc_info.value)
    assert "W" not in message.replace("<redacted>", "")
    assert message.endswith(f"echo {REDACTED} and {REDACTED}")


@pytest.mark.asyncio
async def test_secret_with_single_quote_is_redacted_in_quoted_record():
    record = shlex.quote(f"name=op,passwd={SECRET}")
    conn = _conn(_process_error(command=f"chhmcusr -i {record}"))
    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError) as exc_info,
    ):
        await run_hmc_command(make_config(), f"chhmcusr -i {record}", secrets=[SECRET])

    _assert_no_secret(str(exc_info.value))
    assert "name=op,passwd=" in str(exc_info.value)


@pytest.mark.asyncio
async def test_asyncssh_log_record_is_redacted_and_filter_removed(caplog):
    library_log = logging.getLogger("asyncssh")
    # The adapter chain asyncssh's channel logs its exec command through (channel.py).
    channel_log = asyncssh.logging.logger.get_child(context="conn=0").get_child(
        context="chan=0"
    )

    def run(*_args, **_kwargs):
        channel_log.info("  Command: %s", CMD)
        channel_log.info("%d", CMD)  # arguments that do not format
        raise _process_error()

    conn = _conn(run)
    with (
        caplog.at_level(logging.INFO, logger="asyncssh"),
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError),
    ):
        await run_hmc_command(make_config(), CMD, secrets=[SECRET])

    messages = [record.getMessage() for record in caplog.records]
    assert any("Command:" in message for message in messages)
    for message in messages:
        _assert_no_secret(message)
    assert library_log.filters == []


@pytest.mark.asyncio
async def test_secret_success_returns_output_and_removes_filter():
    conn = _conn()
    conn.run.return_value.stdout = "done\n"
    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        assert await run_hmc_command(make_config(), CMD, secrets=[SECRET]) == "done\n"

    conn.run.assert_called_once_with(CMD, check=True, timeout=300.0)
    assert logging.getLogger("asyncssh").filters == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secrets", "error"),
    [([""], ValueError), ("abc", TypeError)],
    ids=["empty", "bare-str"],
)
async def test_invalid_secrets_rejected_before_connect(secrets, error):
    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect") as connect,
        pytest.raises(error, match="secrets must be"),
    ):
        await run_hmc_command(make_config(), CMD, secrets=secrets)
    connect.assert_not_called()


@pytest.mark.asyncio
async def test_no_secrets_keeps_cause():
    conn = _conn(_process_error())
    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError) as exc_info,
    ):
        await run_hmc_command(make_config(), CMD)

    assert isinstance(exc_info.value.__cause__, asyncssh.ProcessError)
    assert repr(CMD) in str(exc_info.value)


@pytest.mark.parametrize(
    "parse",
    [
        lambda: parse_hmc_result_rows(f"name,pw\n{SECRET}\n", ["name", "pw"], "op"),
        lambda: parse_hmc_result_rows(f"pw\n{SECRET}\n", ["name", "pw"], "op"),
        lambda: _parse_lshwres_output(f'name=a,"pw={SECRET}'),
    ],
    ids=["column-count", "header-mismatch", "unclosed-quote"],
)
def test_parse_failures_do_not_echo_secret_output(parse):
    with pytest.raises(HMCCLIError) as exc_info:
        parse()
    _assert_no_secret(str(exc_info.value))


@pytest.mark.parametrize("character", [",", "=", '"', "\n"])
def test_secret_attribute_refusal_omits_value(character):
    value = f"pw-{character}655"
    with pytest.raises(HMCCLIError) as exc_info:
        build_attribute_record([("name", "op"), ("passwd", value)], secret={"passwd"})
    message = str(exc_info.value)
    assert "'passwd'" in message
    assert REDACTED in message
    assert "pw-" not in message
    assert "655" not in message


def test_bare_string_secret_names_rejected():
    with pytest.raises(TypeError, match="secret must be"):
        names = "passwd"  # pragma: allowlist secret - an attribute name
        build_attribute_record([("passwd", "top,x")], secret=names)


def test_secret_name_absent_from_record_rejected():
    with pytest.raises(ValueError, match=r"\['password'\]"):
        build_attribute_record([("passwd", "top,x")], secret={"password"})


@pytest.mark.asyncio
async def test_loopback_exec_failure_redacts_error_and_asyncssh_log(
    monkeypatch, caplog
):
    """A real asyncssh peer: its own exec-path log record and echoed stderr."""

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return True

    def process_handler(process):
        process.stderr.write(f"HSCL1234 rejected: {process.command}\n")
        process.exit(1)

    async with asyncssh.create_server(
        Server,
        "127.0.0.1",
        0,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        process_factory=process_handler,
    ) as listener:
        connect = asyncssh.connect
        monkeypatch.setattr(
            asyncssh,
            "connect",
            lambda **kwargs: connect(port=listener.get_port(), config=None, **kwargs),
        )
        config = make_config(host="127.0.0.1", ssh_verify_host_key=False, ssh_timeout=5)
        with (
            caplog.at_level(logging.INFO, logger="asyncssh"),
            pytest.raises(HMCCLIError) as exc_info,
        ):
            await run_hmc_command(config, CMD, secrets=[SECRET])

    _assert_no_secret(str(exc_info.value))
    assert "HSCL1234 rejected" in str(exc_info.value)
    messages = [record.getMessage() for record in caplog.records]
    assert any("Command:" in message for message in messages)
    for message in messages:
        _assert_no_secret(message)


def test_non_secret_attribute_refusal_still_quotes_value():
    with pytest.raises(HMCCLIError, match="'op,x'"):
        build_attribute_record([("name", "op,x"), ("passwd", "p")], secret={"passwd"})


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["exit", "overflow", "timeout", "cancel"])
async def test_bounded_secret_failure_and_cancellation(monkeypatch, caplog, case):
    import asyncio
    from types import SimpleNamespace

    logger = logging.getLogger("asyncssh")
    before = list(logger.filters)
    process = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(side_effect=[b"", b""])),
        stderr=SimpleNamespace(read=AsyncMock(side_effect=[SECRET.encode(), b""])),
        env={},
        command=CMD,
        subsystem=None,
        exit_status=1,
        exit_signal=None,
        returncode=1,
        wait_closed=AsyncMock(),
        close=MagicMock(),
    )
    conn = _conn()
    conn.abort = MagicMock()

    async def create(*args, **kwargs):
        logger.warning("exec %s", CMD)
        if case == "timeout":
            raise TimeoutError(SECRET)
        if case == "cancel":
            raise asyncio.CancelledError
        return process

    conn.create_process.side_effect = create
    monkeypatch.setattr(asyncssh, "connect", MagicMock(return_value=conn))
    limit = 2 if case == "overflow" else 64
    with (
        caplog.at_level(logging.WARNING, logger="asyncssh"),
        pytest.raises(
            asyncio.CancelledError if case == "cancel" else HMCCLIError
        ) as caught,
    ):
        await run_hmc_command(
            make_config(), CMD, secrets=[SECRET], max_output_bytes=limit
        )
    _assert_no_secret(caplog.text)
    _assert_no_secret("".join(traceback.format_exception(caught.value)))
    assert logger.filters == before
    if case != "cancel":
        assert caught.value.__cause__ is None and caught.value.__context__ is None
