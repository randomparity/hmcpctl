"""Terminal field integrity and intentional machine-stream exceptions (#1275)."""

from __future__ import annotations

import errno
import json
import os
import select
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import click
import pytest
from rich.console import Console

from hmcpctl.cli_commands import output

PAYLOAD = "雪[bold]:smile:\\\x00\x07\x08\t\n\r\x1b]0;title\x7f\x85\x9b\u202e\u2066"
VISIBLE = "雪[bold]:smile:\\\\x00\\x07\\x08\\t\\n\\r\\x1b]0;title\\x7f\\x85\\x9b\\u202e\\u2066"


def _child(
    source,
    *,
    stdout_tty=False,
    stderr_tty=False,
    environment=None,
    input="",
    stdin_tty=False,
):
    """Run bounded offline output through actual PTYs/pipes, draining and closing them."""
    import pty

    pairs = [pty.openpty() if tty else None for tty in (stdout_tty, stderr_tty)]
    input_pair = pty.openpty() if stdin_tty else None
    env = dict(os.environ)
    for key in ("FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE"):
        env.pop(key, None)
    env.update(environment or {})
    env["COLUMNS"] = "240"
    env["TERM"] = "xterm-256color"
    try:
        if input_pair:
            os.write(input_pair[0], input.encode())
        finished = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            readers = [
                pool.submit(_drain_pty, pair[0], finished) if pair else None
                for pair in pairs
            ]
            try:
                result = subprocess.run(
                    [sys.executable, "-c", source],
                    input=None if input_pair else input.encode(),
                    stdin=input_pair[1] if input_pair else None,
                    stdout=pairs[0][1] if pairs[0] else subprocess.PIPE,
                    stderr=pairs[1][1] if pairs[1] else subprocess.PIPE,
                    env=env,
                    timeout=20,
                    check=False,
                )
            finally:
                finished.set()
            streams = [result.stdout, result.stderr]
            for index, reader in enumerate(readers):
                if reader:
                    streams[index] = reader.result().replace(b"\r\n", b"\n")
        return result.returncode, *(stream.decode() for stream in streams)
    finally:
        for pair in [*pairs, input_pair]:
            if pair:
                for descriptor in pair:
                    if descriptor >= 0:
                        os.close(descriptor)


def _drain_pty(master, finished):
    # Drain while the child writes; macOS drops unread data on the last slave close.
    chunks = []
    while True:
        child_finished = finished.is_set()
        if not select.select([master], [], [], 0.1)[0]:
            if child_finished:
                break
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def test_pty_drains_final_write_when_completion_follows_empty_select(monkeypatch):
    import pty

    master, slave = pty.openpty()
    finished = threading.Event()
    original_select = select.select
    first = True

    def completing_select(*args):
        nonlocal first
        if first:
            first = False
            os.write(slave, b"final output")
            finished.set()
            return [], [], []
        return original_select(*args)

    monkeypatch.setattr(select, "select", completing_select)
    try:
        assert _drain_pty(master, finished) == b"final output"
    finally:
        os.close(slave)
        os.close(master)


def test_child_drains_both_terminals_before_exit():
    code, stdout, stderr = _child(
        "import sys\nsys.stdout.write('o' * 131072)\nsys.stderr.write('e' * 131072)",
        stdout_tty=True,
        stderr_tty=True,
    )
    assert code == 0
    assert stdout == "o" * 131072
    assert stderr == "e" * 131072


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize(
    "environment", [{}, {"FORCE_COLOR": "1"}, {"TTY_COMPATIBLE": "0"}]
)
def test_raw_actual_terminal_policy(method, terminal, environment):
    returned = (PAYLOAD, {}) if method == "get" else PAYLOAD
    arguments = ["raw", method, "/test"]
    if method == "post":
        arguments += ["<x/>", "--yes"]
    source = f"""
from hmcpctl import cli
from hmcpctl.cli_commands import raw
raw.with_client = lambda fn: {returned!r}
cli.app({arguments!r})
"""
    code, stdout, stderr = _child(source, stdout_tty=terminal, environment=environment)
    assert code == 0, stderr
    assert stdout == (VISIBLE if terminal else PAYLOAD) + "\n"


@pytest.mark.parametrize("stdout_tty,stderr_tty", [(True, False), (False, True)])
def test_independent_terminal_destinations(stdout_tty, stderr_tty):
    source = f"""
import sys
from hmcpctl.cli_commands.output import terminal_field
print(terminal_field({PAYLOAD!r}))
print(terminal_field({PAYLOAD!r}, err=True), file=sys.stderr)
"""
    code, stdout, stderr = _child(source, stdout_tty=stdout_tty, stderr_tty=stderr_tty)
    assert code == 0, stderr
    assert stdout == (VISIBLE if stdout_tty else PAYLOAD) + "\n"
    assert stderr == (VISIBLE if stderr_tty else PAYLOAD) + "\n"


@pytest.mark.parametrize("inventory", [False, True])
def test_terminal_json_escapes_without_changing_values(inventory):
    call = (
        "print_inventory(data, InventoryFormat.json)"
        if inventory
        else "print_json(data)"
    )
    code, stdout, stderr = _child(
        f"from hmcpctl.cli_commands.output import *\ndata = {{'name': {PAYLOAD!r}}}\n{call}",
        stdout_tty=True,
        environment={"TTY_COMPATIBLE": "0"},
    )
    assert code == 0, stderr
    assert json.loads(stdout) == {"name": PAYLOAD}
    assert "\u202e" not in stdout and "\x85" not in stdout and "\x9b" not in stdout


def test_table_fields_are_safe_without_losing_literal_text():
    table = output.VerbatimTable(title=PAYLOAD, caption=PAYLOAD)
    table.add_column(PAYLOAD)
    table.add_row(PAYLOAD)
    console = Console(width=240, record=True)
    console.print(table)
    rendered = console.export_text()
    assert rendered.count(VISIBLE) == 4


def test_error_field_and_trusted_guidance_are_distinct():
    code, stdout, stderr = _child(
        f"""
import typer
from hmcpctl.cli_commands.output import fail
try:
    fail(ValueError({PAYLOAD!r}), guidance='\\n\\nRun this:\\n    command')
except typer.Exit as exc:
    raise SystemExit(exc.exit_code)
""",
        stderr_tty=True,
        environment={"FORCE_COLOR": "1"},
    )
    assert code == 1 and stdout == ""
    assert f"Error: {VISIBLE}\n\nRun this:\n    command\n" == click.unstyle(stderr)
    assert "\x1b[" in stderr  # the application's own Error style remains


def test_raw_confirmation_encodes_argument_before_prompt():
    code, stdout, stderr = _child(
        f"from hmcpctl import cli\ncli.app(['raw', 'post', {PAYLOAD!r}, '<x/>'])",
        stdout_tty=True,
        input="n\n",
    )
    assert code == 1, stderr
    assert f"POST {VISIBLE} to the HMC?" in stdout


def test_machine_tsv_retains_existing_escape_grammar_on_terminal():
    code, stdout, stderr = _child(
        "from hmcpctl.cli_commands.output import *\n"
        "print_inventory({}, InventoryFormat.tsv, ('Name',), [('a\\t\\u202eb',)])",
        stdout_tty=True,
    )
    assert code == 0, stderr
    assert stdout == "Name\na\\t\u202eb\n"


@pytest.mark.parametrize(
    "source,destination",
    [
        (
            (
                "from hmcpctl.cli_commands.lpar import inventory as m\n"
                "m.with_client = lambda fn: value\n"
                "cli.app(['lpars', 'state', 'lp-test'])"
            ),
            "stdout",
        ),
        (
            (
                "from hmcpctl.cli_commands import console as m\n"
                "m.with_client = lambda fn: {'link': value, 'Resource': {}}\n"
                "cli.app(['console', 'info'])"
            ),
            "stdout",
        ),
        (
            (
                "from hmcpctl.cli_commands.systems import core as m\n"
                "m.with_client = lambda fn: [{'UUID': 'id-test', 'Resource': {'SystemName': value}}]\n"
                "cli.app(['systems', 'list'])"
            ),
            "stdout",
        ),
        (
            (
                "from hmcpctl.cli_commands.virtualization.vnic import _confirm_on_stderr\n"
                "_confirm_on_stderr(value)"
            ),
            "stderr",
        ),
        (
            (
                "from types import SimpleNamespace\n"
                "from hmcpctl.cli_commands.lpar.console import _report\n"
                "_report(SimpleNamespace(data=b'', error=value, stop_reason='error', "
                "released=False, system='sys-test', lpar='lp-test'))"
            ),
            "stderr",
        ),
    ],
)
def test_actual_cli_human_sinks_encode_fields(source, destination):
    code, stdout, stderr = _child(
        f"from hmcpctl import cli\nvalue = {PAYLOAD!r}\n{source}",
        stdout_tty=destination == "stdout",
        stderr_tty=destination == "stderr",
        input="n\n",
    )
    assert code == 0, stderr
    rendered = click.unstyle(stdout if destination == "stdout" else stderr)
    assert VISIBLE in rendered
    assert "\u202e" not in rendered and "\x9b" not in rendered


def test_serve_guidance_preserves_layout_but_encodes_path():
    code, stdout, stderr = _child(
        "from hmcpctl import cli\n"
        "from hmcpctl.cli_commands import serve\n"
        "serve._policy_file = lambda: ('dir\\n\\u202efile', False)\n"
        "cli.app(['serve'])",
        stderr_tty=True,
    )
    assert code == 2 and not stdout
    rendered = click.unstyle(stderr)
    assert "Error: serve requires --access-policy NAME\n\n" in rendered
    assert "\n    hmcpctl config init-access-policy\n" in rendered
    assert r"dir\n\u202efile" in rendered


def test_terminal_field_keeps_printable_and_empty_values(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert output.terminal_field("") == ""
    assert output.terminal_field("雪[é]:smile: \\") == "雪[é]:smile: \\"
    assert output.terminal_field(0) == "0"


@pytest.mark.parametrize("environment", [{}, {"FORCE_COLOR": "1"}, {"NO_COLOR": "1"}])
def test_storage_confirmation_uses_stderr_terminal_policy(environment):
    source = f"""
from hmcpctl import cli
cli.app(["storage", "delete-disk", "vio-test", "--vg", "vg-test", "--name", {PAYLOAD!r}])
"""
    code, stdout, stderr = _child(
        source, stderr_tty=True, environment=environment, input="n\n", stdin_tty=True
    )
    assert code != 0
    assert stdout == ""
    assert VISIBLE in click.unstyle(stderr)
    assert "\x1b]0;title" not in stderr and "\u202e" not in stderr
