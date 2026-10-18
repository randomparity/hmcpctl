"""Tests for the SSH-tool name resolvers (REST-first, SSH fallback).

The SSH-passthrough tools take a system / LPAR that may be given by CLI
name or by UUID.  Names pass through untouched; UUIDs are resolved to their
CLI names via REST, falling back to an ``lssyscfg`` name lookup over SSH
when the REST transport is unreachable.  These tests pin that contract:

- the SSH lookup primitives send ``lssyscfg -F uuid,name`` and parse its output;
- a name argument never touches REST or SSH;
- a transport failure (``httpx.HTTPError``) triggers the SSH fallback;
- a REST status error (``HMCError``) does *not* — REST answered, so the
  unknown-UUID error surfaces instead of silently guessing via SSH.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import httpx
import pytest
from conftest import live_fixture, live_process_error, make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError, HMCTransportError
from hmcpctl.resource_identity import ResourceNotFoundError
from hmcpctl.ssh.lpar import resolve_lpar_cli_name, resolve_system_cli_name
from hmcpctl.ssh.selectors import (
    _lpar_name_from_rest,
    _system_name_from_rest,
    resolve_lpar_name,
    resolve_ssh_names,
    resolve_system_name,
)
from hmcpctl.ssh.transport import HMCCLIError

SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"
SYSTEM_NAME = "Server-9080-M9S-SN12345"
LPAR_UUID = "11111111-1111-4111-8111-111111111111"
LPAR_NAME = "my-lpar"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary",
    [None, [], {}, {"UUID": SYSTEM_UUID}, {"UUID": SYSTEM_UUID, "SystemName": 1}],
)
async def test_system_rest_selector_rejects_malformed_resource(summary):
    hmc = MagicMock()
    hmc.list_system_summaries = AsyncMock(return_value=[summary])

    with pytest.raises(ValueError, match="Could not resolve system UUID") as raised:
        await _system_name_from_rest(hmc, SYSTEM_UUID)

    assert not isinstance(raised.value, ResourceNotFoundError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource", [None, [], {}, {"PartitionName": " "}, {"PartitionName": 1}]
)
async def test_lpar_rest_selector_rejects_malformed_resource(resource):
    hmc = MagicMock()
    hmc.get_logical_partition = AsyncMock(return_value={"Resource": resource})

    with pytest.raises(ValueError, match="Could not resolve LPAR UUID") as raised:
        await _lpar_name_from_rest(hmc, LPAR_UUID)

    assert not isinstance(raised.value, ResourceNotFoundError)


# ``lssyscfg -r sys|lpar -F uuid,name`` output rows.
_SYS_ROWS = f"00000000-0000-0000-0000-000000000000,other\n{SYSTEM_UUID},{SYSTEM_NAME}\n"
_LPAR_ROWS = f"{LPAR_UUID},{LPAR_NAME}\n"


def _make_ssh_mock(stdout: str = "") -> MagicMock:
    """Return a mock asyncssh connection whose run() returns stdout."""
    result = MagicMock()
    result.stdout = stdout

    conn = AsyncMock()
    conn.run = AsyncMock(return_value=result)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    return conn


# ---------------------------------------------------------------------- #
# ssh.py lookup primitives
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ssh_system_name_parses_matching_row():
    """resolve_system_cli_name returns the name on the matching uuid,name row."""
    conn = _make_ssh_mock(_SYS_ROWS)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        name = await resolve_system_cli_name(make_config(), SYSTEM_UUID)

    assert name == SYSTEM_NAME
    cmd = conn.run.call_args[0][0]
    assert cmd == "lssyscfg -r sys -F uuid,name"


@pytest.mark.asyncio
async def test_ssh_system_name_raises_when_uuid_missing():
    """A UUID with no matching row raises HMCCLIError, not a silent guess."""
    conn = _make_ssh_mock("00000000-0000-0000-0000-000000000000,other\n")

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="Could not resolve system UUID"),
    ):
        await resolve_system_cli_name(make_config(), SYSTEM_UUID)


@pytest.mark.asyncio
async def test_resolve_lpar_cli_name_scopes_to_system():
    """resolve_lpar_cli_name scopes lssyscfg with -m when a system is given."""
    conn = _make_ssh_mock(_LPAR_ROWS)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        name = await resolve_lpar_cli_name(
            make_config(), LPAR_UUID, system_name=SYSTEM_NAME
        )

    assert name == LPAR_NAME
    cmd = conn.run.call_args[0][0]
    assert cmd == f"lssyscfg -r lpar -m {SYSTEM_NAME} -F uuid,name"


@pytest.mark.asyncio
async def test_resolve_lpar_cli_name_without_system_scopes_each_system():
    """Without a system the lookup lists every system and scopes each with -m.

    ``lssyscfg -r lpar`` without ``-m`` exits 1 (lssyscfg.md: -m is required
    when listing partitions; captured in cli-lpar-no-m).
    """

    def run(cmd, **_kwargs):
        if " -m " not in cmd and cmd.startswith("lssyscfg -r lpar"):
            raise live_process_error("cli-lpar-no-m")
        result = MagicMock()
        result.stdout = {
            "lssyscfg -r sys -F name": f"other\n{SYSTEM_NAME}\n",
            "lssyscfg -r lpar -m other -F uuid,name": "No results were found.\n",
            f"lssyscfg -r lpar -m {SYSTEM_NAME} -F uuid,name": _LPAR_ROWS,
        }[cmd]
        return result

    conn = _make_ssh_mock()
    conn.run = AsyncMock(side_effect=run)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        name = await resolve_lpar_cli_name(make_config(), LPAR_UUID)

    assert name == LPAR_NAME
    assert [call.args[0] for call in conn.run.await_args_list] == [
        "lssyscfg -r sys -F name",
        "lssyscfg -r lpar -m other -F uuid,name",
        f"lssyscfg -r lpar -m {SYSTEM_NAME} -F uuid,name",
    ]


def _per_system_ssh_mock(answers: dict[str, str | BaseException]) -> MagicMock:
    """Answer each command from *answers*; an exception answer is raised."""

    def run(cmd, **_kwargs):
        answer = answers[cmd]
        if isinstance(answer, BaseException):
            raise answer
        return MagicMock(stdout=answer)

    conn = _make_ssh_mock()
    conn.run = AsyncMock(side_effect=run)
    return conn


def _unreachable_system_error() -> asyncssh.ProcessError:
    # Synthetic: no capture holds a lssyscfg answer for an unreachable system;
    # the lookup's contract covers any nonzero exit.
    return asyncssh.ProcessError(
        env={},
        command="lssyscfg",
        subsystem=None,
        exit_status=1,
        exit_signal=None,
        returncode=1,
        stdout="system unreachable\n",
        stderr="",
    )


@pytest.mark.asyncio
async def test_resolve_lpar_cli_name_skips_a_failing_system():
    """One listed system failing its partition listing does not end the search."""
    conn = _per_system_ssh_mock(
        {
            "lssyscfg -r sys -F name": f"down\n{SYSTEM_NAME}\n",
            "lssyscfg -r lpar -m down -F uuid,name": _unreachable_system_error(),
            f"lssyscfg -r lpar -m {SYSTEM_NAME} -F uuid,name": _LPAR_ROWS,
        }
    )

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        name = await resolve_lpar_cli_name(make_config(), LPAR_UUID)

    assert name == LPAR_NAME


@pytest.mark.asyncio
async def test_resolve_lpar_cli_name_reports_skipped_systems_when_unmatched():
    """With no match, the error names each system whose listing failed."""
    conn = _per_system_ssh_mock(
        {
            "lssyscfg -r sys -F name": f"down\n{SYSTEM_NAME}\n",
            "lssyscfg -r lpar -m down -F uuid,name": _unreachable_system_error(),
            f"lssyscfg -r lpar -m {SYSTEM_NAME} -F uuid,name": "No results were found.\n",
        }
    )

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="Could not resolve LPAR UUID") as raised,
    ):
        await resolve_lpar_cli_name(make_config(), LPAR_UUID)

    assert "'down'" in str(raised.value)
    assert "system unreachable" in str(raised.value)


@pytest.mark.asyncio
async def test_resolve_lpar_cli_name_named_system_failure_propagates():
    """A named system is the only place to look, so its failure is the answer."""
    conn = _per_system_ssh_mock(
        {f"lssyscfg -r lpar -m {SYSTEM_NAME} -F uuid,name": _unreachable_system_error()}
    )

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn),
        pytest.raises(HMCCLIError, match="system unreachable"),
    ):
        await resolve_lpar_cli_name(make_config(), LPAR_UUID, SYSTEM_NAME)


@pytest.mark.asyncio
async def test_resolve_lpar_cli_name_matches_the_captured_upper_case_uuid():
    """V10R3 prints LPAR UUIDs in upper case; a lower-case selector still matches."""
    capture = live_fixture("cli-lpar-uuid-name")
    conn = _make_ssh_mock(capture["stdout"])

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        name = await resolve_lpar_cli_name(
            make_config(), "00000001-abcd-4ef0-8abc-000000000001", system_name="sys-R1"
        )

    assert name == "sys-R1-lp3"
    assert conn.run.call_args[0][0] == capture["command"]


# REST element names the HMC CLI rejects as "An invalid attribute was entered"
# (live 2026-09-23, V10R3 M1060, for UUID). docs/hmc-cli-cheatsheet.md records
# the CLI attribute names: lower-case ``uuid`` and ``name``.
_REST_ELEMENT_NAMES = {"UUID", "SystemName", "PartitionName"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup",
    [
        lambda: resolve_system_cli_name(make_config(), SYSTEM_UUID),
        lambda: resolve_lpar_cli_name(make_config(), LPAR_UUID, SYSTEM_NAME),
    ],
)
async def test_ssh_lookups_send_only_hmc_cli_attributes(lookup):
    """No REST element name reaches an ``lssyscfg -F`` attribute list."""
    conn = _make_ssh_mock(_SYS_ROWS + _LPAR_ROWS)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        await lookup()

    attributes = conn.run.call_args[0][0].split(" -F ", 1)[1].split(",")
    assert attributes == ["uuid", "name"]
    assert not _REST_ELEMENT_NAMES & set(attributes)


# ---------------------------------------------------------------------- #
# resolve_system_name / resolve_lpar_name (REST-first, SSH fallback)
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_system_name_passes_names_through():
    """A plain name is returned untouched — no REST session, no SSH command."""
    with (
        patch("hmcpctl.ssh.selectors.HMCClient") as mock_client,
        patch("hmcpctl.ssh.transport.asyncssh.connect") as mock_connect,
    ):
        name = await resolve_system_name(make_config(), SYSTEM_NAME)

    assert name == SYSTEM_NAME
    mock_client.assert_not_called()
    mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_lpar_name_passes_names_through():
    """A plain LPAR name is returned untouched."""
    with (
        patch("hmcpctl.ssh.selectors.HMCClient") as mock_client,
        patch("hmcpctl.ssh.transport.asyncssh.connect") as mock_connect,
    ):
        name = await resolve_lpar_name(make_config(), LPAR_NAME, SYSTEM_NAME)

    assert name == LPAR_NAME
    mock_client.assert_not_called()
    mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_system_name_falls_back_to_ssh_when_rest_down(
    mock_hmc,
):
    """A REST transport failure triggers the SSH name lookup."""
    # REST is unreachable: logon raises a transport error (not an HMCError).
    mock_hmc.put("/rest/api/web/Logon").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    conn = _make_ssh_mock(_SYS_ROWS)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        name = await resolve_system_name(make_config(), SYSTEM_UUID)

    assert name == SYSTEM_NAME


@pytest.mark.asyncio
async def test_resolve_lpar_name_falls_back_scoped_by_system(mock_hmc):
    """The LPAR SSH fallback is scoped to the resolved system name."""
    mock_hmc.put("/rest/api/web/Logon").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    conn = _make_ssh_mock(_LPAR_ROWS)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        name = await resolve_lpar_name(
            make_config(), LPAR_UUID, system_name=SYSTEM_NAME
        )

    assert name == LPAR_NAME
    cmd = conn.run.call_args[0][0]
    assert f"-m {SYSTEM_NAME}" in cmd


@pytest.mark.asyncio
async def test_resolve_system_name_does_not_fall_back_on_rest_status_error(
    mock_hmc,
):
    """A REST error status (HMCError) is a real answer — no SSH fallback.

    REST responded, so its failure should surface as an error rather than
    silently resolving via SSH.
    """
    # logon succeeds (fixture default); the summary GET returns 500.
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(500, text="server error")
    )

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect") as mock_connect,
        pytest.raises(HMCError),
    ):
        await resolve_system_name(make_config(), SYSTEM_UUID)

    mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_system_name_reports_a_uuid_no_summary_lists(mock_hmc):
    """A UUID the summary does not list is a REST answer too — no SSH fallback."""
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(200, json=[])
    )

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect") as mock_connect,
        pytest.raises(ValueError, match="Could not resolve system UUID"),
    ):
        await resolve_system_name(make_config(), SYSTEM_UUID)

    mock_connect.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_lpar_cli_name_matches_uuid_case_insensitively():
    """`lssyscfg -F uuid` prints LPAR UUIDs in upper case on V10R3; a lower-case
    selector names the same partition (live capture, #879)."""
    lpar_uuid = "6d2b02ee-9c63-4e20-9f88-36ca5ea4e9ab"
    conn = _make_ssh_mock(f"{lpar_uuid.upper()},{LPAR_NAME}\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn):
        name = await resolve_lpar_cli_name(make_config(), lpar_uuid, SYSTEM_NAME)

    assert name == LPAR_NAME


@pytest.mark.asyncio
@pytest.mark.parametrize("borrowed", [False, True])
async def test_pair_uses_one_owned_or_existing_session(borrowed):
    config = make_config()
    hmc = MagicMock(spec=HMCClient)
    hmc.config = config
    hmc.list_system_summaries = AsyncMock(
        return_value=[{"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME}]
    )
    hmc.get_logical_partition = AsyncMock(
        return_value={"Resource": {"PartitionName": LPAR_NAME}}
    )
    with patch("hmcpctl.ssh.selectors.HMCClient", return_value=hmc) as factory:
        hmc.__aenter__.return_value = hmc
        assert await resolve_ssh_names(
            hmc if borrowed else config, SYSTEM_UUID, LPAR_UUID
        ) == (SYSTEM_NAME, LPAR_NAME)
    assert factory.call_count == (0 if borrowed else 1)
    assert hmc.__aenter__.await_count == (0 if borrowed else 1)
    assert hmc.__aexit__.await_count == (0 if borrowed else 1)
    hmc.list_system_summaries.assert_awaited_once()
    hmc.get_logical_partition.assert_awaited_once_with(LPAR_UUID)


@pytest.mark.asyncio
@pytest.mark.parametrize("system,lpar", [(None, None), (SYSTEM_NAME, LPAR_NAME)])
async def test_pair_names_need_no_session(system, lpar):
    with patch("hmcpctl.ssh.selectors.HMCClient") as factory:
        assert await resolve_ssh_names(make_config(), system, lpar) == (system, lpar)
    factory.assert_not_called()


def _resolver_client():
    hmc = MagicMock(spec=HMCClient)
    hmc.config = make_config()
    hmc.__aenter__.return_value = hmc
    hmc.list_system_summaries = AsyncMock(
        return_value=[{"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME}]
    )
    hmc.get_logical_partition = AsyncMock(
        return_value={"Resource": {"PartitionName": LPAR_NAME}}
    )
    return hmc


@pytest.mark.asyncio
@pytest.mark.parametrize("system_uuid", [False, True])
async def test_pair_one_uuid_uses_only_required_lookup(system_uuid):
    hmc = _resolver_client()
    with patch("hmcpctl.ssh.selectors.HMCClient", return_value=hmc) as factory:
        assert await resolve_ssh_names(
            hmc.config,
            SYSTEM_UUID if system_uuid else SYSTEM_NAME,
            LPAR_NAME if system_uuid else LPAR_UUID,
        ) == (SYSTEM_NAME, LPAR_NAME)
    factory.assert_called_once_with(hmc.config)
    assert hmc.list_system_summaries.await_count == int(system_uuid)
    assert hmc.get_logical_partition.await_count == int(not system_uuid)
    hmc.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["acquisition", "system", "lpar"])
async def test_pair_transport_fallback_does_not_repeat_lookups(stage):
    hmc = _resolver_client()
    lookup = {
        "acquisition": hmc.__aenter__,
        "system": hmc.list_system_summaries,
        "lpar": hmc.get_logical_partition,
    }[stage]
    lookup.side_effect = HMCTransportError("unreachable")
    with (
        patch("hmcpctl.ssh.selectors.HMCClient", return_value=hmc) as factory,
        patch(
            "hmcpctl.ssh.selectors.resolve_system_cli_name",
            new_callable=AsyncMock,
            return_value=SYSTEM_NAME,
        ) as system_cli,
        patch(
            "hmcpctl.ssh.selectors.resolve_lpar_cli_name",
            new_callable=AsyncMock,
            return_value=LPAR_NAME,
        ) as lpar_cli,
    ):
        assert await resolve_ssh_names(hmc.config, SYSTEM_UUID, LPAR_UUID) == (
            SYSTEM_NAME,
            LPAR_NAME,
        )
    factory.assert_called_once_with(hmc.config)
    assert hmc.list_system_summaries.await_count == int(stage != "acquisition")
    assert hmc.get_logical_partition.await_count == int(stage != "acquisition")
    if stage in ("acquisition", "system"):
        system_cli.assert_awaited_once_with(hmc.config, SYSTEM_UUID)
    else:
        system_cli.assert_not_awaited()
    if stage in ("acquisition", "lpar"):
        lpar_cli.assert_awaited_once_with(hmc.config, LPAR_UUID, SYSTEM_NAME)
    else:
        lpar_cli.assert_not_awaited()
    assert hmc.__aexit__.await_count == int(stage != "acquisition")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["acquisition", "system", "lpar"])
@pytest.mark.parametrize(
    "error", [HMCError("status", 500), ValueError("invalid"), asyncio.CancelledError()]
)
async def test_pair_nontransport_errors_propagate_and_close(stage, error):
    hmc = _resolver_client()
    lookup = {
        "acquisition": hmc.__aenter__,
        "system": hmc.list_system_summaries,
        "lpar": hmc.get_logical_partition,
    }[stage]
    lookup.side_effect = error
    with (
        patch("hmcpctl.ssh.selectors.HMCClient", return_value=hmc),
        patch(
            "hmcpctl.ssh.transport.asyncssh.connect",
            side_effect=AssertionError("unexpected SSH fallback"),
        ) as ssh,
        pytest.raises(type(error)) as raised,
    ):
        await resolve_ssh_names(hmc.config, SYSTEM_UUID, LPAR_UUID)
    assert raised.value is error
    ssh.assert_not_called()
    assert hmc.__aexit__.await_count == int(stage != "acquisition")


@pytest.mark.asyncio
async def test_borrowed_fallback_uses_client_destination_without_closing():
    hmc = _resolver_client()
    hmc.config = hmc.config.model_copy(update={"host": "other.example.test"})
    hmc.get_logical_partition.side_effect = HMCTransportError("unreachable")
    with (
        patch("hmcpctl.ssh.selectors.HMCClient") as factory,
        patch(
            "hmcpctl.ssh.selectors.resolve_lpar_cli_name",
            new_callable=AsyncMock,
            return_value=LPAR_NAME,
        ) as cli,
    ):
        assert await resolve_ssh_names(hmc, SYSTEM_UUID, LPAR_UUID) == (
            SYSTEM_NAME,
            LPAR_NAME,
        )
    cli.assert_awaited_once_with(hmc.config, LPAR_UUID, SYSTEM_NAME)
    factory.assert_not_called()
    hmc.__aenter__.assert_not_awaited()
    hmc.__aexit__.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [HMCError("status", 500), asyncio.CancelledError()])
async def test_borrowed_error_does_not_close_client(error):
    hmc = _resolver_client()
    hmc.get_logical_partition.side_effect = error
    with (
        patch("hmcpctl.ssh.selectors.HMCClient") as factory,
        patch("hmcpctl.ssh.transport.asyncssh.connect") as ssh,
        pytest.raises(type(error)) as raised,
    ):
        await resolve_ssh_names(hmc, SYSTEM_UUID, LPAR_UUID)
    assert raised.value is error
    factory.assert_not_called()
    ssh.assert_not_called()
    hmc.__aenter__.assert_not_awaited()
    hmc.__aexit__.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_fallback_failure_is_not_retried_as_acquisition():
    hmc = _resolver_client()
    hmc.get_logical_partition.side_effect = HMCTransportError("REST unavailable")
    error = HMCTransportError("fallback failed")
    with (
        patch("hmcpctl.ssh.selectors.HMCClient", return_value=hmc) as factory,
        patch(
            "hmcpctl.ssh.selectors.resolve_system_cli_name", new_callable=AsyncMock
        ) as system_cli,
        patch(
            "hmcpctl.ssh.selectors.resolve_lpar_cli_name",
            new_callable=AsyncMock,
            side_effect=error,
        ) as lpar_cli,
        pytest.raises(HMCTransportError) as raised,
    ):
        await resolve_ssh_names(hmc.config, SYSTEM_UUID, LPAR_UUID)
    assert raised.value is error
    factory.assert_called_once_with(hmc.config)
    system_cli.assert_not_awaited()
    lpar_cli.assert_awaited_once_with(hmc.config, LPAR_UUID, SYSTEM_NAME)
    hmc.list_system_summaries.assert_awaited_once()
    hmc.get_logical_partition.assert_awaited_once()
    hmc.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("lpar", [None, LPAR_NAME, LPAR_UUID])
async def test_single_lpar_preserves_literal_system_scope(lpar):
    hmc = _resolver_client()
    hmc.get_logical_partition.side_effect = HMCTransportError("unreachable")
    with (
        patch("hmcpctl.ssh.selectors.HMCClient", return_value=hmc) as factory,
        patch(
            "hmcpctl.ssh.selectors.resolve_lpar_cli_name",
            new_callable=AsyncMock,
            return_value=LPAR_NAME,
        ) as cli,
    ):
        assert await resolve_lpar_name(hmc.config, lpar, SYSTEM_UUID) == (
            LPAR_NAME if lpar == LPAR_UUID else lpar
        )
    hmc.list_system_summaries.assert_not_awaited()
    if lpar == LPAR_UUID:
        factory.assert_called_once_with(hmc.config)
        cli.assert_awaited_once_with(hmc.config, LPAR_UUID, SYSTEM_UUID)
    else:
        factory.assert_not_called()
        cli.assert_not_awaited()
