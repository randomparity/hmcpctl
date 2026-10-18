"""ADR 0092 §4: the opt-in ADR 0011 ownership guard on the power operations (#371).

The guard is a configuration switch, so both settings are contract. The
off-by-default arm asserts the *absence* of work — no SSH connection, no extra
REST read — because that absence is the reason ADR 0092 chose the default.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from hmcpctl.audit import sink as audit_sink
from hmcpctl.cli import app as cli_app
from hmcpctl.cli_commands.lpar import lifecycle as cli_lpars
from hmcpctl.config import HMCConfig
from hmcpctl.operations.lpar import core as lpar_core
from hmcpctl.operations.lpar import provision as operations_provision
from hmcpctl.operations.lpar.core import power_off_lpar, submit_power_on_lpar
from hmcpctl.server_tools.lpar import lifecycle as server_lpars
from hmcpctl.ssh.transport import HMCCLIError

LPAR_UUID = "11111111-1111-1111-1111-111111111111"
SYSTEM_UUID = "22222222-2222-2222-2222-222222222222"
OTHER_LPAR_UUID = "33333333-3333-3333-3333-333333333333"
OWNED_BY_ALICE = "[hmcpctl owner:alice created:2026-08-14]"
OWNED_BY_BOB = "[hmcpctl owner:bob created:2026-08-14]"


def _hmc(*, authorize: bool, agent_id: str = "alice") -> AsyncMock:
    """An HMC client double carrying a real :class:`HMCConfig`.

    The config must be real: ``AsyncMock().config.authorize_power_operations``
    is a truthy child mock, which would silently enable the guard everywhere.
    ``from_mapping`` rather than the ordinary constructor (ADR 0096), so an
    exported ``HMC_AGENT_ID`` on a developer's workstation cannot decide which
    agent these tests believe they are.
    """
    hmc = AsyncMock()
    hmc.config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "u",
            "password": "p",
            "agent_id": agent_id,
            "authorize_power_operations": authorize,
        }
    )
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": "sys1", "State": "operating"}
    ]
    hmc.get_logical_partition.return_value = {"Resource": {"PartitionName": "aix1"}}
    hmc.submit_job.return_value = {"UUID": "job-uuid"}
    # ADR 0094's resolve chain confirms the partition lives on the selected
    # system, and walks the fleet when no selector is given.
    hmc.list_logical_partitions.return_value = [{"UUID": LPAR_UUID}]
    return hmc


@asynccontextmanager
async def _factory_for(hmc):
    yield hmc


def _client_factory(hmc):
    def factory(_profile=None):
        return _factory_for(hmc)

    return factory


# ---------------------------------------------------------------------------
# The setting itself
# ---------------------------------------------------------------------------


def test_authorize_power_operations_defaults_off() -> None:
    # from_mapping, so this asserts the declared default rather than whatever
    # HMC_AUTHORIZE_POWER_OPERATIONS happens to be on the running host.
    assert HMCConfig.from_mapping({}).authorize_power_operations is False


def test_authorize_power_operations_reads_its_environment_variable(monkeypatch) -> None:
    # The ordinary constructor here on purpose: reading the environment is the
    # behaviour under test, which is exactly where from_mapping is wrong.
    monkeypatch.setenv("HMC_AUTHORIZE_POWER_OPERATIONS", "true")
    assert HMCConfig().authorize_power_operations is True


# ---------------------------------------------------------------------------
# Setting off: the call path is what it was, and it opens no SSH connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_guard_opens_no_ssh_connection_and_reads_no_ownership() -> None:
    hmc = _hmc(authorize=False)
    connect = AsyncMock(side_effect=AssertionError("opened an SSH connection"))

    with patch("hmcpctl.ssh.transport.asyncssh.connect", new=connect):
        result = await power_off_lpar(hmc, LPAR_UUID, system_name_or_uuid=SYSTEM_UUID)

    assert result.job == {"UUID": "job-uuid"}
    connect.assert_not_called()
    hmc.list_system_summaries.assert_not_awaited()
    hmc.get_logical_partition.assert_not_awaited()
    hmc.submit_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_guard_powers_a_partition_owned_by_another_agent() -> None:
    """Ownership stays advisory by default — that is the ADR 0092 §4 decision."""
    hmc = _hmc(authorize=False, agent_id="alice")

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=AsyncMock(return_value=OWNED_BY_BOB),
    ) as read:
        await power_off_lpar(hmc, LPAR_UUID)

    read.assert_not_awaited()
    hmc.submit_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_guard_needs_no_managed_system_selector() -> None:
    hmc = _hmc(authorize=False)

    await power_off_lpar(hmc, LPAR_UUID)

    hmc.submit_job.assert_awaited_once()


# ---------------------------------------------------------------------------
# Setting on: the guard runs before the job is submitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enabled_guard_refuses_a_partition_another_agent_owns(caplog) -> None:
    hmc = _hmc(authorize=True, agent_id="alice")

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=OWNED_BY_BOB),
        ),
        caplog.at_level(logging.WARNING),
        pytest.raises(PermissionError, match="ownership_override=true"),
    ):
        await power_off_lpar(
            hmc,
            LPAR_UUID,
            system_name_or_uuid=SYSTEM_UUID,
            operation="dumprestart",
            allow_dump_restart=True,
        )

    hmc.submit_job.assert_not_awaited()
    # ADR 0180: a guard refusal records the denial and no power-off, because
    # nothing was sent. The denial record keeps the capture from being empty.
    events = [
        json.loads(record.getMessage())["event"]
        for record in caplog.records
        if record.name == audit_sink.AUDIT_LOGGER_NAME
    ]
    assert events == ["ownership-denied"]


@pytest.mark.asyncio
async def test_enabled_guard_powers_a_partition_this_agent_owns() -> None:
    hmc = _hmc(authorize=True, agent_id="alice")

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=AsyncMock(return_value=OWNED_BY_ALICE),
    ) as read:
        result = await power_off_lpar(hmc, LPAR_UUID, system_name_or_uuid=SYSTEM_UUID)

    assert result.job == {"UUID": "job-uuid"}
    read.assert_awaited_once_with(hmc.config, "sys1", "aix1")
    hmc.submit_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_enabled_guard_runs_before_the_already_running_short_circuit() -> None:
    """A foreign-owned partition is refused, not reported as already running."""
    hmc = _hmc(authorize=True, agent_id="alice")
    hmc.get_quick_property.return_value = "running"

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=OWNED_BY_BOB),
        ),
        pytest.raises(PermissionError),
    ):
        await submit_power_on_lpar(hmc, LPAR_UUID, system_name_or_uuid=SYSTEM_UUID)

    hmc.get_quick_property.assert_not_awaited()


@pytest.mark.asyncio
async def test_ownership_override_submits_the_job_and_is_audited(caplog) -> None:
    hmc = _hmc(authorize=True, agent_id="alice")

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=OWNED_BY_BOB),
        ) as read,
        caplog.at_level(logging.WARNING),
    ):
        result = await power_off_lpar(
            hmc,
            LPAR_UUID,
            system_name_or_uuid=SYSTEM_UUID,
            ownership_override=True,
        )

    assert result.job == {"UUID": "job-uuid"}
    read.assert_not_awaited()
    records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == audit_sink.AUDIT_LOGGER_NAME
    ]
    # The override is recorded before the ADR 0180 power-off record, which
    # names the job that override let through.
    assert [record["event"] for record in records] == [
        "ownership-override",
        "lpar-power-off",
    ]
    # The system is the caller's selector verbatim, not its resolved CLI name:
    # ADR 0094's override path deliberately skips the managed-system read so a
    # degraded fleet cannot block an approved exception. The partition name is
    # resolved, so the record still identifies what was bypassed.
    assert (records[0]["system"], records[0]["lpar"]) == (SYSTEM_UUID, "aix1")


@pytest.mark.asyncio
async def test_enabled_guard_fails_closed_when_the_ownership_read_fails() -> None:
    """SSH is a hard dependency once the guard is on — and it fails closed."""
    hmc = _hmc(authorize=True)

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(side_effect=HMCCLIError("SSH command timed out after 300s")),
        ),
        pytest.raises(HMCCLIError, match="timed out"),
    ):
        await power_off_lpar(hmc, LPAR_UUID, system_name_or_uuid=SYSTEM_UUID)

    hmc.submit_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_guard_resolves_the_selector_once_and_scopes_the_lpar() -> None:
    """The named selector is resolved once and scopes the partition lookup.

    Resolving it again inside the guard would buy a second REST GET, and it
    would let a partition named by name resolve outside the system whose
    ownership token is then read.
    """
    hmc = _hmc(authorize=True, agent_id="alice")
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": "sys1"}
    ]
    hmc.find_partition_by_name.return_value = {"UUID": LPAR_UUID}

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=AsyncMock(return_value=OWNED_BY_ALICE),
    ):
        await power_off_lpar(hmc, "aix1", system_name_or_uuid="sys1")

    # One summary read resolves the name to its UUID, one the UUID to the CLI name.
    assert hmc.list_system_summaries.await_count == 2
    hmc.find_partition_by_name.assert_awaited_once_with("aix1", system_uuid=SYSTEM_UUID)


@pytest.mark.asyncio
async def test_enabled_guard_discovers_the_owning_system_without_a_selector() -> None:
    """ADR 0063 keeps the selector optional; ADR 0094 derives the system."""
    hmc = _hmc(authorize=True, agent_id="alice")

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=AsyncMock(return_value=OWNED_BY_ALICE),
    ) as read:
        result = await power_off_lpar(hmc, LPAR_UUID)

    assert result.job == {"UUID": "job-uuid"}
    hmc.list_system_summaries.assert_awaited_once()
    read.assert_awaited_once_with(hmc.config, "sys1", "aix1")


@pytest.mark.asyncio
async def test_enabled_guard_refuses_a_partition_owned_by_a_discovered_system() -> None:
    hmc = _hmc(authorize=True, agent_id="alice")

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=OWNED_BY_BOB),
        ),
        pytest.raises(PermissionError, match="ownership_override=true"),
    ):
        await power_off_lpar(hmc, LPAR_UUID)

    hmc.submit_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_override_without_a_selector_skips_the_fleet_walk() -> None:
    """A degraded fleet must not block the operator's approved exception."""
    hmc = _hmc(authorize=True)

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=AsyncMock(return_value=OWNED_BY_BOB),
    ) as read:
        result = await power_off_lpar(hmc, LPAR_UUID, ownership_override=True)

    assert result.job == {"UUID": "job-uuid"}
    read.assert_not_awaited()
    hmc.list_system_summaries.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabled_guard_refuses_a_uuid_paired_with_a_foreign_system() -> None:
    """The token must never be read off a system the partition does not live on."""
    hmc = _hmc(authorize=True, agent_id="alice")
    hmc.list_logical_partitions.return_value = [{"UUID": OTHER_LPAR_UUID}]

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=OWNED_BY_ALICE),
        ) as read,
        pytest.raises(ValueError, match="does not belong to managed system"),
    ):
        await power_off_lpar(hmc, LPAR_UUID, system_name_or_uuid=SYSTEM_UUID)

    read.assert_not_awaited()
    hmc.submit_job.assert_not_awaited()


# ---------------------------------------------------------------------------
# Callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_activation_leg_overrides_its_own_stamp() -> None:
    """ADR 0092 Consequences: the leg authorizes a partition it just stamped."""
    hmc = _hmc(authorize=True)
    operation = AsyncMock(return_value=AsyncMock(job={"UUID": "job-uuid"}))

    with patch.object(operations_provision, "submit_power_on_lpar", operation):
        await operations_provision._power_on(hmc, "sys1", LPAR_UUID, None)

    assert operation.await_args.kwargs["ownership_override"] is True
    assert operation.await_args.kwargs["system_name_or_uuid"] == "sys1"


@pytest.mark.parametrize(
    ("tool", "operation_module"),
    [
        (server_lpars.hmc_power_on_lpar, lpar_core),
        (server_lpars.hmc_power_off_lpar, server_lpars),
    ],
)
def test_power_tools_forward_the_ownership_override(
    monkeypatch, tool, operation_module
) -> None:
    hmc = _hmc(authorize=True)
    operation = AsyncMock(return_value=AsyncMock(job={"UUID": "job-uuid"}))
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))
    monkeypatch.setattr(
        operation_module,
        "submit_power_on_lpar" if operation_module is lpar_core else "power_off_lpar",
        operation,
    )

    tool("aix1", system_name_or_uuid="sys1", ownership_override=True)

    assert operation.await_args.kwargs["ownership_override"] is True
    assert operation.await_args.kwargs["system_name_or_uuid"] == "sys1"


@pytest.mark.parametrize(
    ("command", "power_on"),
    [("power-on", True), ("power-off", False)],
)
def test_power_cli_forwards_the_system_selector_and_override(
    monkeypatch, command, power_on
) -> None:
    hmc = _hmc(authorize=True)
    operation = AsyncMock(return_value=AsyncMock(lpar_uuid=LPAR_UUID, job=None))
    monkeypatch.setattr(
        "hmcpctl.cli_commands.runtime.client", lambda: _factory_for(hmc)
    )
    monkeypatch.setattr(
        cli_lpars, "submit_power_on_lpar" if power_on else "power_off_lpar", operation
    )

    result = CliRunner().invoke(
        cli_app,
        [
            "lpars",
            command,
            "aix1",
            "--system",
            "sys1",
            "--ownership-override",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert operation.await_args.kwargs["ownership_override"] is True
    assert operation.await_args.kwargs["system_name_or_uuid"] == "sys1"
    operation.assert_awaited_once()
