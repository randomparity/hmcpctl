"""Regression tests: SSH-backed tools shell-quote every interpolated value.

``run_hmc_command`` executes the built command string via the remote shell, so
any user-controlled value interpolated without quoting is an injection vector
(``; id``, ``$(...)``, backticks, etc.).  These tests pin the ``shlex.quote``
discipline: hostile values must land shell-quoted in the built command so the
remote shell passes them as a single argument instead of interpreting
metacharacters.

``hmc_run_command`` is deliberately exempt — it is the documented arbitrary-
command escape hatch.
"""

from __future__ import annotations

import shlex
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import mock_uuid_resolution

from hmcpctl.config import HMCConfig
from hmcpctl.operations.virtualization.vnic import (
    VnicBackingSelector,
    _validate_vnic_backing_selector,
)
from hmcpctl.server_tools.lpar.configuration import (
    hmc_set_lpar_description,
)
from hmcpctl.server_tools.lpar.profiles import (
    hmc_backup_lpar_profiles,
)
from hmcpctl.server_tools.systems.resources import (
    hmc_list_memory_pools,
)
from hmcpctl.server_tools.vios.core import (
    hmc_backup_vios,
    hmc_restore_vios,
)
from hmcpctl.ssh.commands import require_command_safe_text
from hmcpctl.ssh.io_inventory import list_io_slots

SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"
SYSTEM_NAME = "Server-9080-M9S-SN12345"
LPAR_UUID = "11111111-1111-4111-8111-111111111111"
LPAR_NAME = "my-lpar"

# Semicolon + space: without quoting the remote shell would run `id` as a
# second command; with quoting it must stay inside a single argument.
HOSTILE = "x; id"


def _make_ssh_mock(stdout: str = "") -> MagicMock:
    """Return a minimal asyncssh connection mock."""
    result = MagicMock()
    result.stdout = stdout

    conn = AsyncMock()
    conn.run = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


def _hmc_env(monkeypatch) -> None:
    """Set env vars so HMCConfig() succeeds inside the tools."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")
    monkeypatch.setenv("HMC_VERIFY_SSL", "true")


def _captured_cmd(conn_mock) -> str:
    """Return the command string of the last conn.run() call."""
    return conn_mock.run.call_args[0][0]


def _arg_after(args: list[str], option: str) -> str:
    """Return the argument following *option* in a shlex-split argv list."""
    return args[args.index(option) + 1]


def _vios_client_factory():
    hmc = AsyncMock()
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "hscroot", "password": "p"}
    )
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME},
        {"UUID": SYSTEM_UUID, "SystemName": HOSTILE},
    ]
    hmc.find_vios_by_name.return_value = {"UUID": SYSTEM_UUID}

    @asynccontextmanager
    async def factory(_profile):
        yield hmc

    return factory


# ---------------------------------------------------------------------- #
# ssh.py: list_io_slots (standalone -m value)
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_io_slots_quotes_hostile_system_name():
    """A hostile system name is shell-quoted in the lshwres command."""
    conn = _make_ssh_mock("")
    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        await list_io_slots(HMCConfig(host="h", user="u", password="p"), HOSTILE)

    cmd = _captured_cmd(conn)
    assert f"-m {HOSTILE}" not in cmd  # raw value never appears unquoted
    assert f"-m {shlex.quote(HOSTILE)}" in cmd


# ---------------------------------------------------------------------- #
# server.py tools
# ---------------------------------------------------------------------- #


def test_add_vnic_rejects_structural_selector_characters():
    """Typed vNIC selectors reject characters that alter HMC payload structure."""
    with pytest.raises(ValueError, match="alter HMC command structure"):
        _validate_vnic_backing_selector(
            VnicBackingSelector(f"vios,{HOSTILE}", "2", "1", "0", Decimal(2))
        )


def test_remove_vnic_rejects_structural_slot_characters():
    """Slot removal rejects characters that alter the HMC attribute payload."""
    with pytest.raises(ValueError, match="alter HMC command structure"):
        require_command_safe_text(f"4,{HOSTILE}", "slot_num")


def test_set_lpar_description_quotes_hostile_description(monkeypatch, mock_hmc):
    """hmc_set_lpar_description keeps a hostile description inside the -i payload."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME, LPAR_UUID, LPAR_NAME)
    conn = _make_ssh_mock("")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        hmc_set_lpar_description(
            system_name_or_uuid=SYSTEM_UUID,
            lpar_name_or_uuid=LPAR_UUID,
            description=HOSTILE,
            ownership_override=True,
        )

    payload = f"name={LPAR_NAME},description={HOSTILE}"
    args = shlex.split(_captured_cmd(conn))
    assert _arg_after(args, "-i") == payload


def test_backup_lpar_profiles_quotes_hostile_file_path(monkeypatch, mock_hmc):
    """hmc_backup_lpar_profiles shell-quotes a hostile file_path."""
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)
    conn = _make_ssh_mock("")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        hmc_backup_lpar_profiles(SYSTEM_UUID, "/tmp/bak;id")

    cmd = _captured_cmd(conn)
    assert shlex.quote("/tmp/bak;id") in cmd


@pytest.mark.parametrize(
    ("tool", "arguments", "keywords"),
    [
        (
            hmc_backup_vios,
            (SYSTEM_NAME, SYSTEM_UUID),
            {"backup_name": "vios;id"},
        ),
        (
            hmc_restore_vios,
            (SYSTEM_NAME, SYSTEM_UUID, "vios;id"),
            {"backup_type": "ssp", "restart_if_required": False},
        ),
    ],
    ids=["backup", "restore"],
)
def test_vios_backup_tools_quote_hostile_backup_name(
    monkeypatch, tool, arguments, keywords
):
    """VIOS backup and restore shell-quote a hostile catalog name.

    The hostile value carries a shell metacharacter but no path separator: ADR
    0044's containment guard refuses a separator-bearing name before the command
    is built, so a value with one would never reach the quoting this asserts.
    Quoting and containment are separate controls and this proves the first.
    """
    _hmc_env(monkeypatch)
    monkeypatch.setattr("hmcpctl._app.client_from_env", _vios_client_factory())
    conn = _make_ssh_mock("")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        tool(*arguments, **keywords)

    cmd = _captured_cmd(conn)
    assert shlex.quote("vios;id") in cmd


@pytest.mark.parametrize(
    ("tool", "arguments", "keywords"),
    [
        (
            hmc_backup_vios,
            (HOSTILE, SYSTEM_UUID),
            {"backup_name": "safe-backup"},
        ),
        (
            hmc_restore_vios,
            (HOSTILE, SYSTEM_UUID, "safe-backup"),
            {"backup_type": "ssp", "restart_if_required": False},
        ),
    ],
    ids=["backup", "restore"],
)
def test_vios_backup_tools_keep_hostile_direct_system_name_in_one_argument(
    monkeypatch, tool, arguments, keywords
):
    """A caller-controlled direct system name remains one exact ``-m`` word."""
    _hmc_env(monkeypatch)
    monkeypatch.setattr("hmcpctl._app.client_from_env", _vios_client_factory())
    conn = _make_ssh_mock("")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        tool(*arguments, **keywords)

    assert _arg_after(shlex.split(_captured_cmd(conn)), "-m") == HOSTILE


def test_resolved_system_name_is_quoted_too(monkeypatch, mock_hmc):
    """REST-resolved names are quoted too — the HMC could return metacharacters.

    Even though system/lpar names come from the HMC rather than the caller,
    they are interpolated into the shell command, so they must be quoted as
    uniformly as direct user input.
    """
    _hmc_env(monkeypatch)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, HOSTILE)  # hostile system name
    conn = _make_ssh_mock("")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        hmc_list_memory_pools(SYSTEM_UUID)

    cmd = _captured_cmd(conn)
    assert f"-m {HOSTILE}" not in cmd
    assert f"-m {shlex.quote(HOSTILE)}" in cmd
