"""Contracts for the supported VIOS backup, restore, and list commands."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import live_fixture
from fastmcp import Client

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.config import build_config
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.vios.core import (
    hmc_backup_vios,
    hmc_list_vios_backups,
    hmc_restore_vios,
)

SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"
SYSTEM_NAME = "Server-9080-M9S-SN12345"
OPERATING = {"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME, "State": "operating"}
VIOS_UUID = "00000000-0000-0000-0000-000000000003"
BACKUP_NAME = "vios1_backup_001"
NO_RESULTS = "No results were found.\n"

INVALID_BACKUP_NAMES = (
    "",
    "   ",
    "../other/x.tar",
    "/data/viosbackup/x.tar",
    "a\\b.tar",
    ".",
    "..",
    " .. ",
    " backup.tar ",
    "-operation",
)


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
    """Set env vars so HMCConfig() succeeds inside the tool."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def _client_factory(hmc, config=None):
    @asynccontextmanager
    async def factory(profile):
        hmc.config = config if config is not None else build_config(profile=profile)
        yield hmc

    return factory


@pytest.fixture(autouse=True)
def _fake_vios_client(monkeypatch):
    """Prevent command-contract tests from depending on a reachable HMC."""
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    hmc.find_vios_by_name.return_value = {"UUID": VIOS_UUID}
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))


# ---------------------------------------------------------------------- #
# hmc_list_vios_backups
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["hmc_list_vios_backups", "hmc_power_on_vios"])
async def test_vios_mcp_scope_selects_duplicate_names(monkeypatch, tool):
    _hmc_env(monkeypatch)
    other_system = "33333333-3333-4333-8333-333333333333"
    other_vios = "44444444-4444-4444-8444-444444444444"
    identities = {SYSTEM_UUID: VIOS_UUID, other_system: other_vios}
    hmc = AsyncMock()

    async def find(name, *, system_uuid=None):
        assert name == "shared-vios"
        if system_uuid is None:
            raise ValueError("ambiguous VIOS name")
        return {"UUID": identities[system_uuid]}

    hmc.find_vios_by_name.side_effect = find
    hmc.power_on_vios.return_value = None
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))
    run_cli = AsyncMock(return_value=NO_RESULTS)
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_cli)
    policy = compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": [tool],
                            "connections": ["<default>"],
                            "targets": "all-targets",
                        }
                    ]
                }
            }
        },
        "test",
        TOOL_SECURITY,
        "test-vios-scope.toml",
    )
    async with Client(create_mcp(policy, catalog="registered")) as client:
        for system, vios in identities.items():
            result = await client.call_tool(
                tool,
                {"vios_name_or_uuid": "shared-vios", "system_name_or_uuid": system},
            )
            assert not result.is_error
            hmc.find_vios_by_name.assert_awaited_with("shared-vios", system_uuid=system)
            if tool == "hmc_power_on_vios":
                hmc.power_on_vios.assert_awaited_with(vios)
            else:
                assert f"vios_uuids={vios}" in run_cli.await_args.args[0]
        denied = await client.call_tool(
            tool, {"vios_name_or_uuid": "shared-vios"}, raise_on_error=False
        )
        assert denied.is_error
        assert "ambiguous VIOS name" in str(denied.content)


@pytest.mark.parametrize("belongs", [False, True])
def test_list_vios_backups_scoped_uuid_checks_system_before_ssh(monkeypatch, belongs):
    _hmc_env(monkeypatch)
    hmc = AsyncMock()
    hmc.list_vios.return_value = [{"UUID": VIOS_UUID}] if belongs else []
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))
    run_cli = AsyncMock(return_value=NO_RESULTS)
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_cli)
    if belongs:
        assert hmc_list_vios_backups(VIOS_UUID, system_name_or_uuid=SYSTEM_UUID) == []
        run_cli.assert_awaited_once()
    else:
        with pytest.raises(ValueError, match="does not belong"):
            hmc_list_vios_backups(VIOS_UUID, system_name_or_uuid=SYSTEM_UUID)
        run_cli.assert_not_awaited()
    hmc.list_vios.assert_awaited_once_with(SYSTEM_UUID)


def test_list_vios_backups_runs_supported_command_and_parses_csv(monkeypatch):
    """Listing pins the supported projection and CSV parsing semantics."""
    _hmc_env(monkeypatch)
    output = 'name,type\r\n"nightly, ""quoted""",viosioconfig\r\nbase,ssp\r\n'
    conn_mock = _make_ssh_mock(output)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_list_vios_backups(VIOS_UUID)

    conn_mock.run.assert_called_once_with(
        f"lsviosbk --filter vios_uuids={VIOS_UUID} -F name,type --header",
        check=True,
        timeout=300.0,
    )
    assert result == [
        {"name": 'nightly, "quoted"', "type": "viosioconfig"},
        {"name": "base", "type": "ssp"},
    ]


def test_list_vios_backups_preserves_embedded_newline_in_quoted_name(monkeypatch):
    """CSV parsing preserves a catalog name's embedded record separator."""
    _hmc_env(monkeypatch)
    conn_mock = _make_ssh_mock('name,type\r\n"night\nly",ssp\r\n')

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_list_vios_backups(VIOS_UUID)

    assert result == [{"name": "night\nly", "type": "ssp"}]


@pytest.mark.parametrize("capture", ["cli-lsviosbk-empty", "cli-lsviosbk-empty-v11r2"])
def test_list_vios_backups_returns_empty_list(monkeypatch, capture):
    """The HMC's no-results sentinel is an empty backup catalog.

    V10R3 and V11R2 both answer `lsviosbk ... -F name,type --header` for a VIOS
    with no backups with exit 0 and this line, not an empty string (#1202).
    """
    _hmc_env(monkeypatch)
    recorded = live_fixture(capture)
    assert recorded["exit_status"] == 0
    conn_mock = _make_ssh_mock(recorded["stdout"])

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_list_vios_backups(VIOS_UUID)

    assert result == []


def test_list_vios_backups_parses_live_backup_row(monkeypatch):
    """A viosioconfig backup is listed with its `.tar.gz` catalog name (#1458).

    V10R3 answers an `lsviosbk --filter vios_uuids=... -F name,type --header`
    command, shaped like the one the tool issues, with the header `name,type`
    and one row per backup, and lists a backup made with `-f <name>` as
    `<name>.tar.gz`.
    """
    _hmc_env(monkeypatch)
    recorded = live_fixture("cli-lsviosbk-rows")
    assert recorded["exit_status"] == 0
    conn_mock = _make_ssh_mock(recorded["stdout"])

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_list_vios_backups(VIOS_UUID)

    assert result == [
        {"name": "hmcpctl-live-q1458-458edb12.tar.gz", "type": "viosioconfig"}
    ]


def test_list_vios_backups_rejects_the_hmc_wide_listing_header(monkeypatch):
    """`lsviosbk -F --header` answers more columns than the tool asks for.

    The parser pins the `name,type` header, so the HMC-wide shape is refused
    rather than read column by column (#1458).
    """
    _hmc_env(monkeypatch)
    conn_mock = _make_ssh_mock(live_fixture("cli-lsviosbk-all-rows")["stdout"])

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(ValueError, match="expected the exact header"),
    ):
        hmc_list_vios_backups(VIOS_UUID)


@pytest.mark.parametrize(
    "output",
    [
        "backup,type\nbase,vios\n",
        "name,name\nbase,vios\n",
        "name,type\n,vios\n",
        "name,type\nbase,\n",
        "name,type,extra\nbase,vios,unexpected\n",
    ],
    ids=[
        "wrong-header",
        "duplicate-header",
        "empty-name",
        "empty-type",
        "extra-column",
    ],
)
def test_list_vios_backups_refuses_malformed_csv(monkeypatch, output):
    """The explicit CSV projection refuses malformed rows instead of guessing."""
    _hmc_env(monkeypatch)
    conn_mock = _make_ssh_mock(output)

    with (
        patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock),
        pytest.raises(ValueError),
    ):
        hmc_list_vios_backups(VIOS_UUID)


def test_list_vios_backups_resolves_vios_name(monkeypatch):
    """A non-UUID VIOS selector resolves before the supported list command."""
    _hmc_env(monkeypatch)
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    hmc.find_vios_by_name.return_value = {"UUID": VIOS_UUID}
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))
    conn_mock = _make_ssh_mock(NO_RESULTS)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        hmc_list_vios_backups("vios-prod")

    hmc.find_vios_by_name.assert_awaited_once_with("vios-prod")
    conn_mock.run.assert_awaited_once_with(
        f"lsviosbk --filter vios_uuids={VIOS_UUID} -F name,type --header",
        check=True,
        timeout=300.0,
    )


def test_list_vios_backups_with_uuid_uses_one_config_without_rest(monkeypatch):
    """A CLI-ready VIOS UUID reaches SSH without opening a REST session."""
    config = object()
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    client_type = MagicMock(side_effect=_client_factory(hmc, config))
    run_hmc_cli = AsyncMock(return_value=NO_RESULTS)
    monkeypatch.setattr("hmcpctl._app.client_from_env", client_type)
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_hmc_cli)

    assert hmc_list_vios_backups(VIOS_UUID, profile="dev") == []

    client_type.assert_called_once_with("dev")
    hmc.find_vios_by_name.assert_not_awaited()
    assert run_hmc_cli.await_args.args[1] is config


def test_list_vios_backups_reuses_config_for_rest_and_ssh(monkeypatch):
    """Name resolution and SSH cannot observe different profile snapshots."""
    config = object()
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    hmc.find_vios_by_name.return_value = {"UUID": VIOS_UUID}
    client_type = MagicMock(side_effect=_client_factory(hmc, config))
    run_hmc_cli = AsyncMock(return_value=NO_RESULTS)
    monkeypatch.setattr("hmcpctl._app.client_from_env", client_type)
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_hmc_cli)

    assert hmc_list_vios_backups("vios-prod", profile="dev") == []

    client_type.assert_called_once_with("dev")
    assert run_hmc_cli.await_args.args[1] is config


# ---------------------------------------------------------------------- #
# hmc_backup_vios
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("backup_type", ["vios", "viosioconfig", "ssp"])
def test_backup_vios_runs_supported_command(monkeypatch, backup_type):
    """Each supported type uses all required mkviosbk selectors and output name."""
    _hmc_env(monkeypatch)
    conn_mock = _make_ssh_mock("Backup completed successfully.\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_backup_vios(
            SYSTEM_NAME,
            VIOS_UUID,
            backup_name=BACKUP_NAME,
            backup_type=backup_type,
        )

    conn_mock.run.assert_called_once_with(
        f"mkviosbk -t {backup_type} -m {SYSTEM_NAME} --uuid {VIOS_UUID} -f {BACKUP_NAME}",
        check=True,
        timeout=300.0,
    )
    assert "Backup completed" in result


def test_backup_vios_defaults_to_full_vios_type(monkeypatch):
    """The explicit output-name contract retains only the vios backup default."""
    _hmc_env(monkeypatch)
    conn_mock = _make_ssh_mock("Done.\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        hmc_backup_vios(SYSTEM_NAME, VIOS_UUID, backup_name=BACKUP_NAME)

    assert "mkviosbk -t vios" in conn_mock.run.call_args.args[0]


def test_backup_vios_with_cli_ready_selectors_uses_one_config_without_rest(
    monkeypatch,
):
    """A direct system name and VIOS UUID require SSH but no REST login."""
    config = object()
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    client_type = MagicMock(side_effect=_client_factory(hmc, config))
    run_hmc_cli = AsyncMock(return_value="completed\n")
    monkeypatch.setattr("hmcpctl._app.client_from_env", client_type)
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_hmc_cli)

    assert (
        hmc_backup_vios(SYSTEM_NAME, VIOS_UUID, backup_name=BACKUP_NAME, profile="dev")
        == "completed\n"
    )

    client_type.assert_called_once_with("dev")
    hmc.get_managed_system.assert_not_awaited()
    hmc.find_vios_by_name.assert_not_awaited()
    assert run_hmc_cli.await_args.args[1] is config


def test_backup_vios_invalid_type_raises_before_external_calls(monkeypatch):
    """Unknown types fail before selector resolution or SSH is touched."""
    run_hmc_cli = AsyncMock(side_effect=AssertionError("reached the SSH layer"))
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_hmc_cli)

    with pytest.raises(ValueError, match="Invalid backup_type"):
        hmc_backup_vios(
            SYSTEM_UUID,
            "vios-prod",
            backup_name=BACKUP_NAME,
            backup_type="bogus",
        )

    run_hmc_cli.assert_not_awaited()


@pytest.mark.parametrize(
    ("tool", "legacy_arguments"),
    [
        (hmc_backup_vios, ("old-vios", "ssp", "old-profile")),
        (
            hmc_restore_vios,
            ("old-vios", "old-backup", "old-profile", "old-system"),
        ),
    ],
    ids=["backup", "restore"],
)
def test_vios_backup_tools_reject_legacy_positional_calls_before_io(
    monkeypatch, tool, legacy_arguments
):
    """Legacy maximum-arity calls cannot bind as replacement arguments."""
    rest_client = MagicMock(side_effect=AssertionError("opened a REST client"))
    run_hmc_cli = AsyncMock(side_effect=AssertionError("reached the SSH layer"))
    monkeypatch.setattr("hmcpctl._app.client_from_env", rest_client)
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_hmc_cli)

    with pytest.raises(TypeError):
        tool(*legacy_arguments)

    rest_client.assert_not_called()
    run_hmc_cli.assert_not_awaited()


# ---------------------------------------------------------------------- #
# hmc_restore_vios
# ---------------------------------------------------------------------- #


@pytest.mark.parametrize("backup_type", ["viosioconfig", "ssp"])
@pytest.mark.parametrize("restart_if_required", [False, True])
def test_restore_vios_runs_supported_command(
    monkeypatch, backup_type, restart_if_required
):
    """Restore uses a required supported type and emits -r only when requested."""
    _hmc_env(monkeypatch)
    conn_mock = _make_ssh_mock("Restore completed successfully.\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_restore_vios(
            SYSTEM_NAME,
            VIOS_UUID,
            BACKUP_NAME,
            backup_type=backup_type,
            restart_if_required=restart_if_required,
        )

    restart_flag = " -r" if restart_if_required else ""
    conn_mock.run.assert_called_once_with(
        f"rstviosbk -t {backup_type} -m {SYSTEM_NAME} --uuid {VIOS_UUID} "
        f"-f {BACKUP_NAME}{restart_flag}",
        check=True,
        timeout=300.0,
    )
    assert "Restore completed" in result


def test_restore_vios_rejects_full_vios_type_before_external_calls(monkeypatch):
    """Unsupported restore types fail before selector resolution or SSH."""
    run_hmc_cli = AsyncMock(side_effect=AssertionError("reached the SSH layer"))
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_hmc_cli)

    with pytest.raises(ValueError, match="backup_type"):
        hmc_restore_vios(SYSTEM_UUID, "vios-prod", BACKUP_NAME, backup_type="vios")

    run_hmc_cli.assert_not_awaited()


@pytest.mark.parametrize("backup_name", INVALID_BACKUP_NAMES)
def test_backup_vios_refuses_a_name_that_could_leave_the_catalog(
    monkeypatch, backup_name
):
    """Invalid creation names fail before selector resolution or SSH."""
    run_hmc_cli = AsyncMock(side_effect=AssertionError("reached the SSH layer"))
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_hmc_cli)

    with pytest.raises(ValueError, match="backup_name"):
        hmc_backup_vios(SYSTEM_UUID, "vios-prod", backup_name=backup_name)

    run_hmc_cli.assert_not_awaited()


def test_backup_vios_catalog_name_error_describes_creation_safe_syntax(monkeypatch):
    """Creation guidance describes syntax without suggesting an existing entry."""
    _hmc_env(monkeypatch)

    with pytest.raises(ValueError) as error:
        hmc_backup_vios(SYSTEM_NAME, VIOS_UUID, backup_name="../existing")

    message = str(error.value)
    assert "nonempty, unpadded catalog name" in message
    assert "hmc_list_vios_backups" not in message


@pytest.mark.parametrize("backup_name", INVALID_BACKUP_NAMES)
def test_restore_vios_refuses_a_name_that_could_leave_the_catalog(
    monkeypatch, backup_name
):
    """Invalid restore names fail before selector resolution or SSH."""
    run_hmc_cli = AsyncMock(side_effect=AssertionError("reached the SSH layer"))
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_hmc_cli)

    with pytest.raises(ValueError, match="backup_name"):
        hmc_restore_vios(
            SYSTEM_UUID,
            "vios-prod",
            backup_name,
            backup_type="ssp",
            restart_if_required=False,
        )

    run_hmc_cli.assert_not_awaited()


@pytest.mark.parametrize(
    "backup_name",
    ["vios1_backup_001", "nim_resources.tar", "cfgbackup.tar.gz", "a-b_c.1"],
)
@pytest.mark.parametrize(
    ("tool", "arguments", "keywords"),
    [
        (hmc_backup_vios, (SYSTEM_NAME, VIOS_UUID), {}),
        (
            hmc_restore_vios,
            (SYSTEM_NAME, VIOS_UUID),
            {"backup_type": "ssp", "restart_if_required": False},
        ),
    ],
    ids=["backup", "restore"],
)
def test_vios_backup_tools_admit_ordinary_catalog_names(
    monkeypatch, backup_name, tool, arguments, keywords
):
    """Validation is narrow enough to retain ordinary catalog names for both tools."""
    _hmc_env(monkeypatch)
    conn_mock = _make_ssh_mock("completed\n")
    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        tool(*arguments, backup_name=backup_name, **keywords)

    assert f"-f {backup_name}" in conn_mock.run.call_args.args[0]


def test_restore_vios_returns_cli_output(monkeypatch):
    """hmc_restore_vios returns the raw SSH stdout verbatim."""
    _hmc_env(monkeypatch)
    raw_output = "Operation: restore\nStatus: OK\nFile: mybackup\n"
    conn_mock = _make_ssh_mock(raw_output)

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        result = hmc_restore_vios(
            SYSTEM_NAME,
            VIOS_UUID,
            "mybackup",
            backup_type="ssp",
            restart_if_required=False,
        )

    assert result == raw_output


# ---------------------------------------------------------------------- #
# Explicit system and VIOS selector resolution
# ---------------------------------------------------------------------- #


def test_backup_vios_preserves_a_direct_system_name_and_scopes_vios_name(monkeypatch):
    """A name stays the CLI identity while its VIOS lookup is system-scoped."""
    _hmc_env(monkeypatch)
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    hmc.find_vios_by_name.return_value = {"UUID": VIOS_UUID}
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))
    conn_mock = _make_ssh_mock("completed\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        hmc_backup_vios(SYSTEM_NAME, "vios-prod", backup_name=BACKUP_NAME)

    hmc.find_vios_by_name.assert_awaited_once_with("vios-prod", system_uuid=SYSTEM_UUID)
    assert f"-m {SYSTEM_NAME} --uuid {VIOS_UUID}" in conn_mock.run.call_args.args[0]


@pytest.mark.parametrize(
    ("mtms", "expected_shell_mtms"),
    [
        (
            {"MachineType": "9009", "Model": "42A", "SerialNumber": "1234567"},
            "'9009-42A*1234567'",
        ),
        (
            {
                "Metadata": {"Atom": ""},
                "MachineType": {"@attrs": {"kb": "CUR"}, "text": "9009"},
                "Model": "42A",
                "SerialNumber": "1234567",
            },
            "'9009-42A*1234567'",
        ),
    ],
    ids=["served", "attributed-children"],
)
def test_backup_vios_uses_mtms_for_a_system_uuid_even_when_names_collide(
    monkeypatch, mtms, expected_shell_mtms
):
    """A UUID resolves to its unique MTMS, never an ambiguous user-defined name."""
    _hmc_env(monkeypatch)
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    hmc.get_managed_system.return_value = {
        "Resource": {
            "SystemName": SYSTEM_NAME,
            "MachineTypeModelAndSerialNumber": mtms,
        }
    }
    hmc.find_vios_by_name.return_value = {"UUID": VIOS_UUID}
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))
    conn_mock = _make_ssh_mock("completed\n")

    with patch("hmcpctl.ssh.transport.asyncssh.connect", return_value=conn_mock):
        hmc_backup_vios(SYSTEM_UUID, "vios-prod", backup_name=BACKUP_NAME)

    hmc.find_vios_by_name.assert_awaited_once_with("vios-prod", system_uuid=SYSTEM_UUID)
    assert (
        f"-m {expected_shell_mtms} --uuid {VIOS_UUID}"
        in conn_mock.run.call_args.args[0]
    )
    assert SYSTEM_NAME not in conn_mock.run.call_args.args[0]


@pytest.mark.parametrize(
    "managed_system",
    [
        {"Resource": {}},
        {"Resource": {"MachineTypeModelSerialNumber": "9009-42A*1234567"}},
        {"Resource": {"MachineTypeModelAndSerialNumber": "9009-42A*1234567"}},
    ],
    ids=["missing-mtms", "unserved-element-name", "scalar-value"],
)
def test_backup_vios_refuses_uuid_without_complete_mtms_before_ssh(
    monkeypatch, managed_system
):
    """A UUID without a valid CLI identity fails closed before external SSH."""
    _hmc_env(monkeypatch)
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    hmc.get_managed_system.return_value = managed_system
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))

    with (
        patch(
            "hmcpctl.ssh.transport.asyncssh.connect",
            side_effect=AssertionError("reached the SSH layer"),
        ),
        pytest.raises(ValueError, match="MachineTypeModelAndSerialNumber"),
    ):
        hmc_backup_vios(SYSTEM_UUID, VIOS_UUID, backup_name=BACKUP_NAME)


@pytest.mark.parametrize(
    ("component", "value"),
    [
        ("MachineType", None),
        ("Model", None),
        ("SerialNumber", None),
        ("MachineType", " "),
        ("Model", " "),
        ("SerialNumber", " "),
    ],
    ids=[
        "missing-machine-type",
        "missing-model",
        "missing-serial-number",
        "blank-machine-type",
        "blank-model",
        "blank-serial-number",
    ],
)
def test_backup_vios_refuses_missing_or_blank_nested_mtms_component_before_ssh(
    monkeypatch, component, value
):
    """Each required nested MTMS component independently fails closed."""
    _hmc_env(monkeypatch)
    mtms = {
        "MachineType": "9009",
        "Model": "42A",
        "SerialNumber": "1234567",
    }
    if value is None:
        del mtms[component]
    else:
        mtms[component] = value
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    hmc.get_managed_system.return_value = {
        "Resource": {"MachineTypeModelAndSerialNumber": mtms}
    }
    monkeypatch.setattr("hmcpctl._app.client_from_env", _client_factory(hmc))

    with (
        patch(
            "hmcpctl.ssh.transport.asyncssh.connect",
            side_effect=AssertionError("reached the SSH layer"),
        ),
        pytest.raises(ValueError, match="MachineTypeModelAndSerialNumber"),
    ):
        hmc_backup_vios(SYSTEM_UUID, VIOS_UUID, backup_name=BACKUP_NAME)


def test_backup_vios_reuses_config_for_rest_and_ssh(monkeypatch):
    """REST-assisted mutation uses one immutable routing snapshot."""
    config = object()
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [OPERATING]
    hmc.get_managed_system.return_value = {
        "Resource": {
            "MachineTypeModelAndSerialNumber": {
                "MachineType": "9009",
                "Model": "42A",
                "SerialNumber": "1234567",
            }
        }
    }
    client_type = MagicMock(side_effect=_client_factory(hmc, config))
    run_hmc_cli = AsyncMock(return_value="completed\n")
    monkeypatch.setattr("hmcpctl._app.client_from_env", client_type)
    monkeypatch.setattr("hmcpctl.operations.vios.core.run_hmc_cli", run_hmc_cli)

    assert (
        hmc_backup_vios(SYSTEM_UUID, VIOS_UUID, backup_name=BACKUP_NAME, profile="dev")
        == "completed\n"
    )

    client_type.assert_called_once_with("dev")
    assert run_hmc_cli.await_args.args[1] is config
