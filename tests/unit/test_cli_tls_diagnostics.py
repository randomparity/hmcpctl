"""CLI warning presentation preserves audit events and library contracts (#1700)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import subprocess
import sys
import warnings
from datetime import datetime

import pytest
from typer.testing import CliRunner

from hmcpctl import cli
from hmcpctl.audit import records, sink
from hmcpctl.cli_commands import runtime
from hmcpctl.client import core
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError


@pytest.fixture
def cli_boundary(monkeypatch):
    class AuditClock:
        @staticmethod
        def now(tz):
            return datetime(2026, 1, 1, 0, 0, 1, 10000, tzinfo=tz)

    # Keep "1.0" in the timestamp so warning assertions cannot match it.
    monkeypatch.setattr(records, "datetime", AuditClock)
    monkeypatch.delenv("HMC_VERIFY_SSL")
    monkeypatch.setattr(core, "_reported_tls_warning_keys", set())
    warnings.simplefilter("always")

    async def reject(self, body):
        raise HMCError("controlled authentication failure")

    monkeypatch.setattr(core.HMCClient, "_logon_once", reject)

    def activate():
        monkeypatch.setattr(logging.root, "handlers", [])

        def showwarning(message, category, filename, lineno, file=None, line=None):
            (file or sys.stderr).write(
                warnings.formatwarning(message, category, filename, lineno, line)
            )

        monkeypatch.setattr(warnings, "showwarning", showwarning)

    return activate


def _config(**values):
    return HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "u", "password": "p", **values}
    )


@pytest.mark.parametrize("verify", [None, False, True])
def test_cli_tls_warnings_before_real_error(cli_boundary, monkeypatch, verify):
    cli_boundary()
    config = _config(**({} if verify is None else {"verify_ssl": verify}))
    monkeypatch.setattr(runtime, "build_config", lambda **kw: config)
    original = warnings.formatwarning
    result = CliRunner().invoke(cli.app, ["systems", "list"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error: controlled authentication failure" in result.stderr
    assert ".py:" not in result.stderr
    assert "await self.logon()" not in result.stderr
    assert ("credentials can be intercepted" in result.stderr) is (verify is not True)
    assert ("TLS verification defaults to off until 1.0." in result.stderr) is (
        verify is None
    )
    audit = [
        json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")
    ]
    assert len(audit) == (0 if verify is True else 1)
    if audit:
        assert audit[0]["source"] == (
            "field-default" if verify is None else "explicit-argument"
        )
    assert warnings.formatwarning is original
    assert not logging.getLogger(sink.AUDIT_LOGGER_NAME).handlers


def test_actual_warning_suggestions_parse(cli_boundary):
    cli_boundary()
    with warnings.catch_warnings(record=True) as caught:
        hmc = core.HMCClient(_config())
        asyncio.run(hmc._http.aclose())
    flags = re.findall(r"--[a-z-]+(?:=[a-z]+)?", str(caught[0].message))
    assert flags
    for flag in flags:
        result = CliRunner().invoke(cli.app, [flag, "capabilities"])
        assert result.exit_code == 0, (flag, result.output)


def test_cli_warning_suggestions_parse(cli_boundary, monkeypatch):
    cli_boundary()
    monkeypatch.setattr(runtime, "build_config", lambda **kw: _config())
    result = CliRunner().invoke(cli.app, ["systems", "list"])
    flags = set(re.findall(r"--[a-z-]+(?:=[a-z]+)?", result.stderr))
    assert flags == {"--verify-ssl", "--no-verify-ssl"}
    for flag in flags:
        assert CliRunner().invoke(cli.app, [flag, "capabilities"]).exit_code == 0


def test_warning_throttle_and_complete_audit(cli_boundary, capsys):
    cli_boundary()

    async def clients():
        for values in [
            {"verify_ssl": False},
            {"verify_ssl": False},
            {"host": "other.test", "verify_ssl": False},
            {},
        ]:
            hmc = core.HMCClient(_config(**values))
            try:
                with pytest.raises(HMCError):
                    await hmc.logon()
            finally:
                await hmc._http.aclose()

    runtime.run_cli_coroutine(clients)
    stderr = capsys.readouterr().err
    assert stderr.count("Warning: TLS verification is disabled") == 3
    audit = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert len(audit) == 4
    assert [(r["host"], r["source"]) for r in audit] == [
        ("hmc.test", "explicit-argument"),
        ("hmc.test", "explicit-argument"),
        ("other.test", "explicit-argument"),
        ("hmc.test", "field-default"),
    ]


@pytest.mark.parametrize("tty", [False, True])
def test_audit_fallback_is_lossless_and_synchronous(cli_boundary, monkeypatch, tty):
    cli_boundary()
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: tty)
    monkeypatch.setattr(sys, "stderr", stream)
    event = {
        "time": "2026-10-16T00:00:00Z",
        "event": "tls-verification-disabled",
        "host": "hmc.test\x1b[31m",
        "source": "explicit-argument",
    }
    message = json.dumps(event)

    async def emit():
        for _ in range(1030):
            sink.emit(logging.WARNING, lambda: event)
        # Delivery completes before the operation returns, without a queue drain.
        assert len(stream.getvalue().splitlines()) == 1030

    runtime.run_cli_coroutine(emit)
    lines = stream.getvalue().splitlines()
    if tty:
        assert lines[0].startswith("Audit tls-verification-disabled:")
        assert 'host="hmc.test\\u001b[31m"' in lines[0]
        assert 'source="explicit-argument"' in lines[0]
        assert 'time="2026-10-16T00:00:00Z"' in lines[0]
        assert "\x1b" not in stream.getvalue()
    else:
        assert lines == [message] * 1030


@pytest.mark.parametrize("ancestor", [False, True])
def test_configured_audit_handlers_keep_original_record(
    cli_boundary, monkeypatch, ancestor
):
    cli_boundary()
    destination = io.StringIO()
    handler = logging.StreamHandler(destination)
    logger = logging.root if ancestor else logging.getLogger(sink.AUDIT_LOGGER_NAME)
    logger.addHandler(handler)
    before = list(logger.handlers)

    async def emit():
        records.record_tls_verification_disabled(
            host="hmc.test", source="field-default"
        )

    runtime.run_cli_coroutine(emit)
    assert json.loads(destination.getvalue())["event"] == "tls-verification-disabled"
    assert logger.handlers == before


def test_unrelated_warnings_keep_formatter(cli_boundary, capsys):
    cli_boundary()

    async def emit():
        warnings.warn("unrelated warning", RuntimeWarning, stacklevel=1)

    runtime.run_cli_coroutine(emit)
    stderr = capsys.readouterr().err
    assert "RuntimeWarning: unrelated warning" in stderr and ".py:" in stderr


@pytest.mark.parametrize(
    "message",
    [
        "not json",
        "[]",
        '{"event":"other"}',
        '{"event":"tls-verification-disabled","host":1}',
    ],
)
def test_unknown_audit_shapes_pass_through(cli_boundary, monkeypatch, message):
    cli_boundary()
    stream = io.StringIO()
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stderr", stream)

    async def emit():
        logging.getLogger(sink.AUDIT_LOGGER_NAME).warning(message)

    runtime.run_cli_coroutine(emit)
    assert stream.getvalue() == message + "\n"


@pytest.mark.skipif(os.name != "posix", reason="PTY proof requires POSIX")
@pytest.mark.parametrize("tty", [False, True])
def test_real_cli_process_tls_then_error(tty):
    import pty

    source = """
from hmcpctl.cli import app
from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
async def reject(self, body):
    raise HMCError("controlled authentication failure")
HMCClient._logon_once = reject
app(["--host", "hmc.test", "--user", "u", "--password", "p", "systems", "list"])
"""
    environment = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("HMC_")
    }
    pair = pty.openpty() if tty else None
    try:
        result = subprocess.run(
            [sys.executable, "-c", source],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=pair[1] if pair else subprocess.PIPE,
            timeout=20,
            check=False,
        )
        stderr = (os.read(pair[0], 65536) if pair else result.stderr).decode()
    finally:
        if pair:
            for descriptor in pair:
                os.close(descriptor)
    assert result.returncode == 1 and result.stdout == b""
    assert "controlled authentication failure" in stderr
    assert ".py:" not in stderr
    assert (
        "credentials can be intercepted" in stderr
        and "TLS verification defaults to off until 1.0." in stderr
    )
    assert ("Audit tls-verification-disabled:" in stderr) is tty
    assert ('"event": "tls-verification-disabled"' in stderr) is not tty


@pytest.mark.parametrize("custom", [False, True])
def test_custom_or_disabled_last_resort_is_preserved(cli_boundary, monkeypatch, custom):
    cli_boundary()
    stream = io.StringIO()
    fallback = logging.StreamHandler(stream) if custom else None
    monkeypatch.setattr(logging, "lastResort", fallback)

    async def emit():
        records.record_tls_verification_disabled(
            host="hmc.test", source="field-default"
        )

    runtime.run_cli_coroutine(emit)
    assert logging.lastResort is fallback
    if custom:
        assert json.loads(stream.getvalue())["host"] == "hmc.test"
    else:
        assert stream.getvalue() == ""


def test_warning_error_preserves_key_and_restores_state(cli_boundary, monkeypatch):
    cli_boundary()
    monkeypatch.setattr(runtime, "build_config", lambda **kw: _config(verify_ssl=False))
    warnings.simplefilter("error", core.TLSVerificationDisabledWarning)
    original = warnings.formatwarning
    result = CliRunner().invoke(cli.app, ["systems", "list"])
    assert result.exit_code == 1
    assert "TLS certificate verification is disabled" in result.stderr
    assert not core._reported_tls_warning_keys
    assert warnings.formatwarning is original
    assert not logging.getLogger(sink.AUDIT_LOGGER_NAME).handlers


def test_fallback_follows_actual_stderr_destination(cli_boundary, monkeypatch):
    cli_boundary()
    first, second = io.StringIO(), io.StringIO()
    monkeypatch.setattr(first, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stderr", first)

    async def emit():
        monkeypatch.setattr(sys, "stderr", second)
        records.record_tls_verification_disabled(
            host="hmc.test", source="field-default"
        )

    runtime.run_cli_coroutine(emit)
    assert first.getvalue() == ""
    assert json.loads(second.getvalue())["host"] == "hmc.test"


def test_migration_default_filter_survives_sequential_cli_calls(
    cli_boundary, monkeypatch
):
    cli_boundary()
    monkeypatch.setattr(runtime, "build_config", lambda **kw: _config())
    warnings.simplefilter("default", core.HMCVerifySSLDefaultWarning)
    first = CliRunner().invoke(cli.app, ["systems", "list"])
    second = CliRunner().invoke(cli.app, ["systems", "list"])
    assert "TLS verification defaults to off until 1.0." in first.stderr
    assert "TLS verification defaults to off until 1.0." not in second.stderr
    assert second.exit_code == 1
