"""The default ``name=value`` record parser against captured HMC output (#1202)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from conftest import live_fixture, live_process_error, make_config

from hmcpctl.ssh.commands import _parse_lshwres_output, hmc_error_code
from hmcpctl.ssh.io_inventory import list_io_slots
from hmcpctl.ssh.transport import HMCCLIError, run_hmc_command


def test_quoted_list_pair_is_one_attribute():
    """A list value prints as one quoted pair; its commas stay inside the value."""
    (row,) = _parse_lshwres_output(live_fixture("cli-sys-attrs")["stdout"])

    assert row["lpar_proc_compat_modes"] == "default,POWER7,POWER8,POWER9,POWER9_base"
    assert row["curr_sys_keylock"] == "manual"
    assert row["description"] == ""
    assert not [key for key in row if '"' in key]


def test_empty_result_sentinel_is_no_rows():
    """An empty read exits 0 and prints the sentinel, which is not a row."""
    assert _parse_lshwres_output(live_fixture("cli-mempool-empty")["stdout"]) == []


def test_unbalanced_quote_is_refused():
    """A quoted pair that never closes is malformed, not a value to guess at."""
    with pytest.raises(HMCCLIError, match="malformed HMC name=value record"):
        _parse_lshwres_output('name=a,"curr_lpar_names=x,y\n')


@pytest.mark.asyncio
async def test_io_slot_listing_keeps_quoted_feature_codes_whole():
    """The captured default-format slot listing: list pairs stay one attribute."""
    capture = live_fixture("cli-io-slot-default")
    run = AsyncMock(return_value=capture["stdout"])

    with patch("hmcpctl.ssh.io_inventory.run_hmc_command", run):
        slots = await list_io_slots(make_config(), "sys-R1")

    assert run.await_args.args[1] == capture["command"]
    assert len(slots) == capture["stdout"].count("\n")
    assert not [key for slot in slots for key in slot if '"' in key or "=" in key]
    assert {"5260,5899", "2CF3,EC66,EC67"} <= {slot["feature_codes"] for slot in slots}


@pytest.mark.asyncio
async def test_hsc_code_is_read_from_stdout_when_stderr_is_empty():
    """The HMC prints a refusal on stdout with stderr empty; the code survives."""
    connection = AsyncMock()
    connection.run = AsyncMock(side_effect=live_process_error("cli-lpar-unknown"))
    connection.__aenter__ = AsyncMock(return_value=connection)
    connection.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=connection),
        pytest.raises(HMCCLIError) as raised,
    ):
        await run_hmc_command(
            make_config(), live_fixture("cli-lpar-unknown")["command"]
        )

    assert hmc_error_code(raised.value) == "HSCL8012"
    assert getattr(raised.value, "remote_refusal_code", None) == "HSCL8012"


def test_hsc_code_is_absent_from_an_uncoded_refusal():
    """The missing -m refusal carries no HSCL code, so none is invented."""
    message = live_fixture("cli-lpar-no-m")["stdout"]
    assert hmc_error_code(HMCCLIError(f"SSH command failed: {message}")) is None


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("HSCLA4A0: refusal", "HSCLA4A0"),
        ("banner\r\nHSCL1234 refusal", "HSCL1234"),
        (b"HSCL1234 refusal", "HSCL1234"),
        (b"\xffHSCL1234", None),
        ("echoed name: HSCLA4A0", None),
        ("HSCLA4A00 invalid code", None),
        ("HSCLA4A0_ invalid code", None),
    ],
)
def test_remote_refusal_code_is_anchored_in_process_response(response, expected):
    import asyncssh

    from hmcpctl.ssh.transport import _process_error

    error = _process_error(
        "mksyscfg -i 'name=part: HSCLA4A0'",
        asyncssh.ProcessError(
            env={},
            command="name=part: HSCLA4A0",
            subsystem=None,
            exit_status=1,
            exit_signal=None,
            returncode=1,
            stdout=response,
            stderr="",
        ),
    )
    assert getattr(error, "remote_refusal_code", None) == expected
