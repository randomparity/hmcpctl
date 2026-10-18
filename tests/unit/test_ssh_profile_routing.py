"""Tests for per-call profile routing in SSH-backed MCP tools (issue #127).

These tests verify that:
1. `ssh_with_client` threads a caller-supplied profile to the SSH config and
   both name resolvers.
2. SSH selector resolution builds its REST client from the same profile-selected
   config used for SSH.
3. `run_hmc_cli` passes a pre-built HMCConfig to `run_hmc_command` when supplied.
4. MCP tool `profile` parameter reaches the SSH connection.
5. `profile=None` preserves existing env-default behavior.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import mock_uuid_resolution

from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCTransportError
from hmcpctl.ssh.transport import run_hmc_cli

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

SYSTEM_UUID = "22222222-2222-4222-8222-222222222222"
SYSTEM_NAME = "Server-9080-M9S-SN12345"
LPAR_UUID = "11111111-1111-4111-8111-111111111111"
LPAR_NAME = "my-lpar"

DEV_HOST = "dev-hmc.example.com"
DEV_USER = "devuser"
DEV_PASSWORD = "devpass"  # pragma: allowlist secret

PROD_HOST = "prod-hmc.example.com"
PROD_USER = "produser"
PROD_PASSWORD = "prodpass"  # pragma: allowlist secret

DEV_CONFIG = HMCConfig.from_mapping(
    {"host": DEV_HOST, "user": DEV_USER, "password": DEV_PASSWORD}
)
PROD_CONFIG = HMCConfig.from_mapping(
    {"host": PROD_HOST, "user": PROD_USER, "password": PROD_PASSWORD}
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


def _set_env(monkeypatch, host: str, user: str, password: str) -> None:
    """Set HMC_* env vars to make HMCConfig() resolve without TOML."""
    monkeypatch.setenv("HMC_HOST", host)
    monkeypatch.setenv("HMC_USER", user)
    monkeypatch.setenv("HMC_PASSWORD", password)


def _vios_client_factory():
    hmc = AsyncMock()
    hmc.config = DEV_CONFIG
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME}
    ]
    hmc.find_vios_by_name.return_value = {"UUID": SYSTEM_UUID}

    @asynccontextmanager
    async def factory(_profile):
        yield hmc

    return factory


# ---------------------------------------------------------------------------
# Task 1.3 — run_hmc_cli config passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_hmc_cli_uses_supplied_config():
    """run_hmc_cli(cmd, config=...) passes the supplied config to run_hmc_command."""
    conn = _make_ssh_mock("output")
    with patch(
        "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn
    ) as mock_connect:
        await run_hmc_cli("lshmc -v", DEV_CONFIG)

    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs["host"] == DEV_HOST
    assert call_kwargs["username"] == DEV_USER
    assert call_kwargs["password"] == DEV_PASSWORD


@pytest.mark.asyncio
async def test_run_hmc_cli_no_config_uses_hmcconfig(monkeypatch):
    """run_hmc_cli(cmd) with no config falls back to HMCConfig() from env."""
    _set_env(monkeypatch, PROD_HOST, PROD_USER, PROD_PASSWORD)
    conn = _make_ssh_mock("output")
    with patch(
        "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn
    ) as mock_connect:
        await run_hmc_cli("lshmc -v")

    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs["host"] == PROD_HOST


# ---------------------------------------------------------------------------
# Task 1.1 — ssh_with_client profile threading
# ---------------------------------------------------------------------------


def test_ssh_with_client_profile_reaches_ssh(monkeypatch, mock_hmc):
    """ssh_with_client with profile routes SSH to the profile's HMC host."""
    # mock_hmc router handles REST resolution for system UUID
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)

    # Stub config-only resolution for the selected profile.
    with patch("hmcpctl._app.build_config", return_value=DEV_CONFIG) as mock_config:
        conn = _make_ssh_mock("")
        with patch(
            "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn
        ) as mock_connect:
            from hmcpctl._app import ssh_with_client
            from hmcpctl.ssh.memory import list_memory_pools

            ssh_with_client(
                lambda config, system_name, _: list_memory_pools(config, system_name),
                system_name_or_uuid=SYSTEM_NAME,  # plain name → no UUID resolution
                profile="dev",
            )

    mock_config.assert_called_once_with(profile="dev")
    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs["host"] == DEV_HOST


def test_ssh_with_client_profile_none_uses_env(monkeypatch, mock_hmc):
    """ssh_with_client with profile=None uses env-default credentials."""
    _set_env(monkeypatch, PROD_HOST, PROD_USER, PROD_PASSWORD)
    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)

    conn = _make_ssh_mock("")
    with patch(
        "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn
    ) as mock_connect:
        from hmcpctl._app import ssh_with_client
        from hmcpctl.ssh.memory import list_memory_pools

        ssh_with_client(
            lambda config, system_name, _: list_memory_pools(config, system_name),
            system_name_or_uuid=SYSTEM_NAME,
            # no profile argument — should default to None → env fallback
        )

    call_kwargs = mock_connect.call_args.kwargs
    assert call_kwargs["host"] == PROD_HOST


# ---------------------------------------------------------------------------
# Task 1.2 — REST and SSH selector resolution share one config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_system_name_uses_supplied_config():
    """System resolution builds its REST client from the supplied config."""
    with patch("hmcpctl.ssh.selectors.HMCClient") as mock_client_type:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.list_system_summaries = AsyncMock(
            return_value=[{"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME}]
        )
        mock_client_type.return_value = mock_client

        from hmcpctl.ssh.selectors import resolve_system_name

        result = await resolve_system_name(DEV_CONFIG, SYSTEM_UUID)

    mock_client_type.assert_called_once_with(DEV_CONFIG)
    assert result == SYSTEM_NAME


@pytest.mark.asyncio
async def test_resolve_system_name_uses_one_rest_client():
    """System resolution opens exactly one REST client."""
    with patch("hmcpctl.ssh.selectors.HMCClient") as mock_client_type:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.list_system_summaries = AsyncMock(
            return_value=[{"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME}]
        )
        mock_client_type.return_value = mock_client

        from hmcpctl.ssh.selectors import resolve_system_name

        await resolve_system_name(DEV_CONFIG, SYSTEM_UUID)

    mock_client_type.assert_called_once_with(DEV_CONFIG)


@pytest.mark.asyncio
async def test_resolve_lpar_name_uses_supplied_config():
    """LPAR resolution builds its REST client from the supplied config."""
    with patch("hmcpctl.ssh.selectors.HMCClient") as mock_client_type:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get_logical_partition = AsyncMock(
            return_value={"Resource": {"PartitionName": LPAR_NAME}}
        )
        mock_client_type.return_value = mock_client

        from hmcpctl.ssh.selectors import resolve_lpar_name

        result = await resolve_lpar_name(DEV_CONFIG, LPAR_UUID)

    mock_client_type.assert_called_once_with(DEV_CONFIG)
    assert result == LPAR_NAME


# ---------------------------------------------------------------------------
# SSH fallback uses same config when REST transport fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_system_name_ssh_fallback_uses_supplied_config():
    """When REST transport fails, the SSH fallback uses the supplied config.

    The config passed to the fallback is the same one given to the resolver, so
    both transports use the same profile-selected credentials.
    """
    from hmcpctl.ssh.selectors import resolve_system_name

    fallback_output = f"{SYSTEM_UUID},{SYSTEM_NAME}\n"

    with patch("hmcpctl.ssh.selectors.HMCClient") as mock_client_type:
        # REST leg raises a transport error → SSH fallback runs
        mock_client = AsyncMock()
        mock_client.config = DEV_CONFIG
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.list_system_summaries = AsyncMock(
            side_effect=HMCTransportError("GET managed system failed: unreachable")
        )
        mock_client_type.return_value = mock_client

        conn = _make_ssh_mock(fallback_output)
        with patch(
            "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn
        ) as mock_connect:
            result = await resolve_system_name(DEV_CONFIG, SYSTEM_UUID)

    # SSH fallback used the config we supplied (DEV_CONFIG)
    assert result == SYSTEM_NAME
    assert mock_connect.call_args.kwargs["host"] == DEV_HOST


# ---------------------------------------------------------------------------
# Task 1.4 — MCP tool profile parameter threads to SSH
# ---------------------------------------------------------------------------


def test_hmc_run_command_profile_reaches_ssh(monkeypatch):
    """hmc_run_command(cmd, profile=...) routes SSH to the profile's HMC host."""
    from hmcpctl.server_tools.command import hmc_run_command

    with patch(
        "hmcpctl.server_tools.command.build_config", return_value=DEV_CONFIG
    ) as mock_config:
        conn = _make_ssh_mock("output")
        with patch(
            "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn
        ) as mock_connect:
            hmc_run_command("lshmc -v", profile="dev")

    mock_config.assert_called_once_with(profile="dev")
    assert mock_connect.call_args.kwargs["host"] == DEV_HOST


def test_hmc_restore_vios_profile_reaches_ssh(monkeypatch):
    """hmc_restore_vios with profile routes SSH to the profile's HMC host."""
    from hmcpctl.server_tools.vios.core import hmc_restore_vios

    client_factory = MagicMock(side_effect=_vios_client_factory())
    monkeypatch.setattr("hmcpctl._app.client_from_env", client_factory)
    conn = _make_ssh_mock("")
    with patch(
        "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn
    ) as mock_connect:
        hmc_restore_vios(
            SYSTEM_NAME,
            SYSTEM_UUID,
            "backup.tar.gz",
            backup_type="ssp",
            profile="dev",
        )

    client_factory.assert_called_once_with("dev")
    assert mock_connect.call_args.kwargs["host"] == DEV_HOST


def test_hmc_list_memory_pools_profile_reaches_ssh(monkeypatch, mock_hmc):
    """hmc_list_memory_pools with profile threads profile through ssh_with_client."""
    from hmcpctl.server_tools.systems.resources import (
        hmc_list_memory_pools,
    )

    mock_uuid_resolution(mock_hmc, SYSTEM_UUID, SYSTEM_NAME)

    with patch("hmcpctl._app.build_config", return_value=DEV_CONFIG) as mock_config:
        conn = _make_ssh_mock("")
        with patch(
            "hmcpctl.ssh.transport.asyncssh.connect", return_value=conn
        ) as mock_connect:
            hmc_list_memory_pools(SYSTEM_NAME, profile="dev")

    mock_config.assert_called_once_with(profile="dev")
    assert mock_connect.call_args.kwargs["host"] == DEV_HOST


def test_different_profiles_produce_independent_configs():
    """Two calls with different profiles use independent HMCConfig values.

    Verifies that profile selection is call-local with no shared state.
    """
    configs_seen = []

    def capture_connect(**kwargs):
        configs_seen.append(kwargs["host"])
        raise RuntimeError("abort after capture")  # abort the SSH connection attempt

    with patch("hmcpctl._app.build_config") as mock_config:
        # First call: profile="dev" → DEV_CONFIG
        mock_config.return_value = DEV_CONFIG
        with patch(
            "hmcpctl.ssh.transport.asyncssh.connect", side_effect=capture_connect
        ):
            try:
                from hmcpctl._app import ssh_with_client
                from hmcpctl.ssh.memory import list_memory_pools

                ssh_with_client(
                    lambda config, system_name, _: list_memory_pools(
                        config, system_name
                    ),
                    system_name_or_uuid=SYSTEM_NAME,
                    profile="dev",
                )
            except Exception:  # noqa: BLE001, S110 - the connection is aborted on purpose once the config is captured
                pass

        # Second call: profile="prod" → PROD_CONFIG
        mock_config.return_value = PROD_CONFIG
        with patch(
            "hmcpctl.ssh.transport.asyncssh.connect", side_effect=capture_connect
        ):
            try:
                ssh_with_client(
                    lambda config, system_name, _: list_memory_pools(
                        config, system_name
                    ),
                    system_name_or_uuid=SYSTEM_NAME,
                    profile="prod",
                )
            except Exception:  # noqa: BLE001, S110 - the connection is aborted on purpose once the config is captured
                pass

    assert configs_seen[0] == DEV_HOST
    assert configs_seen[1] == PROD_HOST
