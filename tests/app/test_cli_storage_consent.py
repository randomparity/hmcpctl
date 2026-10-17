"""Storage consent policy with synthetic dispatch boundaries and real stdin (#1703)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from unittest.mock import Mock

import pytest
import typer.testing
from click import unstyle
from test_cli_storage_results import CASES
from typer.testing import CliRunner

from hmcpctl import cli
from hmcpctl.cli_commands.storage import resources as storage

RUNNER = CliRunner()


def _args(command, args):
    return [
        "storage",
        command,
        *[a for a in args if a not in {"--yes", "--confirm"}],
        "--system",
        "sys-test",
    ]


@pytest.mark.parametrize("command,args,data,target", CASES[:-1])
@pytest.mark.parametrize("input_text", ["", "y\n", "n\n"])
@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_noninteractive_refusal(
    monkeypatch, command, args, data, target, input_text, flags
):
    call = Mock(return_value=data)
    monkeypatch.setattr(storage, "with_client", call)
    result = RUNNER.invoke(cli.app, [*_args(command, args), *flags], input=input_text)
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert "--yes" in result.stderr and "sys-test" in result.stderr
    assert "[y/N]" not in result.stderr
    call.assert_not_called()
    retry = shlex.split(result.stderr.splitlines()[-1])
    assert retry[:3] == ["hmcpctl", "storage", command]
    accepted = RUNNER.invoke(cli.app, retry[1:])
    assert accepted.exit_code == 0, accepted.output
    call.assert_called_once()


@pytest.mark.parametrize("command,args,data,target", CASES[:-1])
@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_explicit_yes_dispatches(monkeypatch, command, args, data, target, flag):
    call = Mock(return_value=data)
    monkeypatch.setattr(storage, "with_client", call)
    result = RUNNER.invoke(cli.app, [*_args(command, args), flag])
    assert result.exit_code == 0, result.output
    call.assert_called_once()
    assert "[y/N]" not in result.stderr


@pytest.mark.parametrize("command,args,data,target", CASES[:-1])
@pytest.mark.parametrize("answer", ["n\n", ""])
def test_interactive_decline_and_eof(monkeypatch, command, args, data, target, answer):
    monkeypatch.setattr(
        typer.testing._NamedTextIOWrapper, "isatty", lambda self: self.name == "<stdin>"
    )
    call = Mock(return_value=data)
    monkeypatch.setattr(storage, "with_client", call)
    result = RUNNER.invoke(cli.app, _args(command, args), input=answer)
    assert result.exit_code == 1, result.output
    assert "sys-test" in result.stderr and "vi-test" in result.stderr
    assert target in result.stderr
    call.assert_not_called()


@pytest.mark.parametrize("command,args,data,target", CASES[10:12])
@pytest.mark.parametrize("force_color", [False, True])
def test_confirm_removed(monkeypatch, command, args, data, target, force_color):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", force_color)
    call = Mock()
    monkeypatch.setattr(storage, "with_client", call)
    result = RUNNER.invoke(cli.app, [*_args(command, args), "--confirm"])
    assert result.exit_code == 2
    call.assert_not_called()
    help_result = RUNNER.invoke(cli.app, ["storage", command, "--help"])
    help_text = unstyle(help_result.stdout)
    assert "--yes" in help_text and "--confirm" not in help_text


def test_dry_run_needs_no_consent(monkeypatch):
    command, args, data, _ = CASES[3]
    call = Mock(return_value=data)
    monkeypatch.setattr(storage, "with_client", call)
    result = RUNNER.invoke(cli.app, [*_args(command, args), "--dry-run"])
    assert result.exit_code == 0, result.output
    call.assert_called_once()
    assert "[y/N]" not in result.stderr


def test_retry_does_not_echo_password(monkeypatch):
    call = Mock()
    monkeypatch.setattr(storage, "with_client", call)
    command, args, _, _ = CASES[2]
    result = RUNNER.invoke(
        cli.app, ["--password", "private-test-value", *_args(command, args)]
    )
    assert result.exit_code == 2
    assert "private-test-value" not in result.output
    assert "same connection options" in result.stderr
    call.assert_not_called()


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("shell", ["bash", "zsh"])
@pytest.mark.parametrize("locale", ["C", "C.UTF-8"])
def test_retry_preserves_hostile_scope_and_target(monkeypatch, terminal, shell, locale):
    import shutil

    if shutil.which(shell) is None:
        pytest.skip(f"{shell} unavailable")
    value = "雪[bold]'\\\n\x1b\u202e$(printf BAD)"
    monkeypatch.setattr(
        typer.testing._NamedTextIOWrapper,
        "isatty",
        lambda self: terminal and self.name == "<stderr>",
    )
    call = Mock(return_value={})
    monkeypatch.setattr(storage, "with_client", call)
    result = RUNNER.invoke(
        cli.app,
        [
            "storage",
            "delete-disk",
            "--vg",
            value,
            "--name",
            value,
            "--system",
            value,
            "--",
            value,
        ],
    )
    assert result.exit_code == 2
    invocation = result.stderr.splitlines()[-1]
    # The shell runs only this argv recorder; the real application is never invoked.
    recorded = subprocess.run(
        [shell, "-c", "hmcpctl() { printf '%s\\0' \"$@\"; }; " + invocation],
        capture_output=True,
        check=True,
        timeout=10,
        env={**os.environ, "LC_ALL": locale},
    )
    argv = recorded.stdout.decode().split("\0")[:-1]
    for option in ["--vg", "--name", "--system"]:
        assert argv[argv.index(option) + 1] == value
    assert argv[-1] == value
    assert "\x1b" not in invocation and "\u202e" not in invocation
    accepted = RUNNER.invoke(cli.app, argv)
    assert accepted.exit_code == 0, accepted.output
    call.assert_called_once()


SOURCE = """
import sys
from hmcpctl import cli
from hmcpctl.cli_commands.storage import resources

def dispatch(operation):
    print("DISPATCH", file=sys.stderr)
    return {}

resources.with_client = dispatch
cli.app(["storage", "delete-disk", "vi-test", "--vg", "vg-test", "--name", "disk-test",
         "--system", "sys-test", "--json", *sys.argv[1:]])
"""


@pytest.mark.parametrize("answer,expected", [(b"y\n", 0), (b"n\n", 1), (b"\x04", 1)])
def test_real_terminal_yes_decline_eof(answer, expected):
    import pty

    master, slave = pty.openpty()
    try:
        with os.fdopen(slave, "rb") as terminal:
            os.write(master, answer)
            result = subprocess.run(
                [sys.executable, "-c", SOURCE],
                stdin=terminal,
                capture_output=True,
                timeout=20,
                check=False,
            )
        assert result.returncode == expected, result.stderr
        assert b"sys-test" in result.stderr and b"[y/N]" in result.stderr
        assert (b"DISPATCH" in result.stderr) == (expected == 0)
        if expected == 0:
            assert json.loads(result.stdout)["operation"] == "delete-disk"
        else:
            assert result.stdout == b""
    finally:
        os.close(master)


def test_open_pipe_without_yes_returns_without_reading():
    with subprocess.Popen(
        [sys.executable, "-c", SOURCE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            # Leave the pipe open: waiting for a line would exceed this bound.
            process.wait(timeout=10)
            stdout, stderr = process.communicate()
        except BaseException:
            process.kill()
            process.communicate()
            raise
    assert process.returncode == 2
    assert stdout == b"" and b"DISPATCH" not in stderr
    assert b"--yes" in stderr and b"[y/N]" not in stderr


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_real_closed_stdin_with_explicit_yes(flag):
    result = subprocess.run(
        [sys.executable, "-c", SOURCE, flag],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["operation"] == "delete-disk"
    assert b"DISPATCH" in result.stderr and b"[y/N]" not in result.stderr


def test_real_absent_stdin_refuses_with_retry():
    result = subprocess.run(
        [sys.executable, "-c", SOURCE],
        preexec_fn=lambda: os.close(0),
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert result.stdout == b"" and b"DISPATCH" not in result.stderr
    assert b"--yes" in result.stderr and b"sys-test" in result.stderr
    assert b"[y/N]" not in result.stderr and b"Traceback" not in result.stderr
