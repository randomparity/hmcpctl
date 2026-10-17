"""Shared ownership boundary for ordinary LPAR reconfiguration operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from hmcpctl import cli
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar import LparResources
from hmcpctl.operations.lpar.configuration import (
    configure_lpar_msp,
    configure_lpar_processor_compatibility,
    synchronize_lpar_profile,
)
from hmcpctl.operations.lpar.migration import (
    LpmMigrationRequest,
    RemoteRestartRequest,
    abort_lpar_migration,
    migrate_lpar,
    recover_lpar_migration,
    remote_restart_lpar,
)
from hmcpctl.operations.lpar.profile_sync import ChangeLocation
from hmcpctl.operations.lpar.provision import ProvisionStorage, attach_disk_to_lpar
from hmcpctl.operations.storage.resources import (
    map_storage,
    mount_optical_media,
    unmount_optical_media,
)
from hmcpctl.operations.virtualization.adapters import (
    add_network_adapter,
    add_vfc_adapter,
    add_vscsi_adapter,
    delete_adapter,
)
from hmcpctl.resource_identity import ResourceNotFoundError
from hmcpctl.server_tools.lpar import configuration as server_configuration
from hmcpctl.server_tools.lpar import lifecycle as server_lifecycle
from hmcpctl.server_tools.lpar import profiles as server_profiles

LPAR = "11111111-1111-4111-8111-111111111111"
VIOS = "22222222-2222-4222-8222-222222222222"
VG = "33333333-3333-4333-8333-333333333333"
SYSTEM_NAME = "frame1"
SYSTEM_UUID = "44444444-4444-4444-8444-444444444444"
LPAR_NAME = "client1"
VIOS_NAME = "vios-1"
FOREIGN_OWNER = "[hmcpctl owner:another-agent created:2026-08-14]"

Operation = Callable[[AsyncMock], Awaitable[object]]


CASES: tuple[tuple[str, Operation], ...] = (
    (
        "hmcpctl.operations.virtualization.adapters.resolve_and_authorize_lpar_mutation",
        lambda hmc: add_network_adapter(hmc, None, LPAR, 100),
    ),
    (
        "hmcpctl.operations.virtualization.adapters.resolve_and_authorize_lpar_mutation",
        lambda hmc: add_vscsi_adapter(hmc, None, LPAR, 2, 10),
    ),
    (
        "hmcpctl.operations.virtualization.adapters.resolve_and_authorize_lpar_mutation",
        lambda hmc: add_vfc_adapter(hmc, None, LPAR, 2, 10),
    ),
    (
        "hmcpctl.operations.virtualization.adapters.resolve_and_authorize_lpar_mutation",
        lambda hmc: delete_adapter(hmc, None, LPAR, "ClientNetworkAdapter", "adapter"),
    ),
    (
        "hmcpctl.operations.storage.resources.resolve_and_authorize_lpar_mutation",
        lambda hmc: map_storage(
            hmc,
            VIOS,
            LPAR,
            system_name_or_uuid=None,
            kind="VirtualDisk",
            storage_name="disk1",
        ),
    ),
    (
        "hmcpctl.operations.storage.resources.resolve_and_authorize_lpar_mutation",
        lambda hmc: mount_optical_media(hmc, VIOS, LPAR, media_name="aix.iso"),
    ),
    (
        "hmcpctl.operations.storage.resources.resolve_and_authorize_lpar_mutation",
        lambda hmc: unmount_optical_media(hmc, VIOS, LPAR, media_name="aix.iso"),
    ),
    (
        "hmcpctl.operations.lpar.provision.resolve_and_authorize_lpar_mutation",
        lambda hmc: attach_disk_to_lpar(
            hmc,
            None,
            LPAR,
            ProvisionStorage(VIOS, "disk1", vg_uuid=VG),
            capacity_mib=1024,
        ),
    ),
    (
        "hmcpctl.operations.lpar.migration.resolve_and_authorize_lpar_mutation",
        lambda hmc: migrate_lpar(
            hmc, None, LPAR, LpmMigrationRequest("target"), validate_first=False
        ),
    ),
    (
        "hmcpctl.operations.lpar.migration.resolve_and_authorize_lpar_mutation",
        lambda hmc: abort_lpar_migration(hmc, None, LPAR),
    ),
    (
        "hmcpctl.operations.lpar.migration.resolve_and_authorize_lpar_mutation",
        lambda hmc: recover_lpar_migration(hmc, None, LPAR),
    ),
    (
        "hmcpctl.operations.lpar.migration.resolve_and_authorize_lpar_mutation",
        lambda hmc: remote_restart_lpar(
            hmc, "source", LPAR, RemoteRestartRequest("cleanup")
        ),
    ),
)


@pytest.mark.parametrize(("guard_path", "operation"), CASES)
@pytest.mark.asyncio
async def test_foreign_owner_stops_reconfiguration_before_first_write(
    monkeypatch: pytest.MonkeyPatch, guard_path: str, operation: Operation
) -> None:
    guard = AsyncMock(side_effect=PermissionError("foreign owner"))
    monkeypatch.setattr(guard_path, guard)
    hmc = AsyncMock()
    hmc.list_volume_groups.return_value = [{"UUID": VG}]

    with pytest.raises(PermissionError, match="foreign owner"):
        await operation(hmc)

    guard.assert_awaited_once()
    for method in (
        "add_network_adapter",
        "add_vscsi_adapter",
        "add_vfc_adapter",
        "delete_adapter",
        "map_storage_to_lpar",
        "create_optical_mapping",
        "delete_storage_mapping",
        "create_virtual_disk",
        "lpar_migrate",
        "lpar_migrate_abort",
        "lpar_migrate_recover",
        "lpar_remote_restart",
    ):
        getattr(hmc, method).assert_not_awaited()


def _real_guard_hmc() -> AsyncMock:
    """Configure the client double for the real ownership guard path."""
    hmc = AsyncMock()
    hmc.config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "u",
            "password": "p",
            "agent_id": "my-agent",
        }
    )
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM_UUID, "SystemName": SYSTEM_NAME}
    ]
    hmc.get_logical_partition.return_value = {"Resource": {"PartitionName": LPAR_NAME}}
    hmc.list_logical_partitions.return_value = [{"UUID": LPAR}]
    hmc.list_vios.return_value = [{"UUID": VIOS}]
    hmc.list_optical_mappings.return_value = [
        {
            "ServerAdapter": {"AdapterName": "vhost0"},
            "TargetDevice": {"VirtualOpticalTargetDevice": {"TargetName": "vtopt0"}},
            "Storage": {"VirtualOpticalMedia": {"MediaName": "aix.iso"}},
        }
    ]
    return hmc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "write_method"),
    [
        (mount_optical_media, "create_optical_mapping"),
        (unmount_optical_media, "delete_storage_mapping"),
    ],
)
async def test_real_guard_rejects_foreign_optical_owner_before_write(
    operation: Operation, write_method: str
) -> None:
    """A foreign token blocks both optical mutations before their HMC write."""
    hmc = _real_guard_hmc()

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=FOREIGN_OWNER),
        ),
        pytest.raises(PermissionError, match="ownership_override=true"),
    ):
        await operation(
            hmc, VIOS, LPAR, media_name="aix.iso", system_name_or_uuid=SYSTEM_UUID
        )

    getattr(hmc, write_method).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "write_method"),
    [
        (mount_optical_media, "create_optical_mapping"),
        (unmount_optical_media, "delete_storage_mapping"),
    ],
)
async def test_optical_ownership_override_bypasses_read_and_writes(
    operation: Operation, write_method: str
) -> None:
    """An approved override skips the ownership read and reaches the write."""
    hmc = _real_guard_hmc()

    with patch(
        "hmcpctl.operations.lpar.ownership.get_lpar_description",
        new=AsyncMock(return_value=FOREIGN_OWNER),
    ) as read:
        await operation(
            hmc,
            VIOS,
            LPAR,
            media_name="aix.iso",
            system_name_or_uuid=SYSTEM_UUID,
            ownership_override=True,
        )

    read.assert_not_awaited()
    getattr(hmc, write_method).assert_awaited_once()


def test_mcp_resource_modify_rejects_foreign_owner_before_hmc_write() -> None:
    """The MCP resource-only leg uses the real guard before its POST."""
    hmc = _real_guard_hmc()
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=FOREIGN_OWNER),
        ),
        patch(
            "hmcpctl.server_tools.lpar.lifecycle.with_client",
            side_effect=lambda fn, **_: asyncio.run(fn(hmc)),
        ),
        pytest.raises(PermissionError, match="ownership_override=true"),
    ):
        server_lifecycle.hmc_modify_lpar(
            LPAR,
            resources=LparResources(desired_memory=8192),
            system_name_or_uuid=SYSTEM_UUID,
        )

    hmc.update_logical_partition.assert_not_awaited()


def test_cli_resource_modify_rejects_foreign_owner_before_hmc_write() -> None:
    """The CLI resource-only leg uses the real guard before its POST."""
    hmc = _real_guard_hmc()
    context = AsyncMock()
    context.__aenter__.return_value = hmc
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=FOREIGN_OWNER),
        ),
        patch("hmcpctl.cli_commands.runtime.client", return_value=context),
        patch(
            "hmcpctl.cli_commands.runtime.run_cli_coroutine",
            side_effect=lambda fn: asyncio.run(fn()),
        ),
    ):
        result = CliRunner().invoke(
            cli.app,
            [
                "lpars",
                "modify",
                LPAR,
                "--system",
                SYSTEM_UUID,
                "--mem",
                "8192",
                "--yes",
            ],
        )
        assert result.exit_code != 0
        assert result.exception is not None
        assert "ownership_override=true" in str(result.exception)

    hmc.update_logical_partition.assert_not_awaited()


def test_resource_modify_override_skips_ownership_read_and_writes() -> None:
    """An approved resource override reaches the shared operation's POST."""
    hmc = _real_guard_hmc()
    hmc.update_logical_partition.return_value = {
        "Resource": {"PartitionName": LPAR_NAME}
    }
    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=FOREIGN_OWNER),
        ) as read,
        patch(
            "hmcpctl.server_tools.lpar.lifecycle.with_client",
            side_effect=lambda fn, **_: asyncio.run(fn(hmc)),
        ),
    ):
        server_lifecycle.hmc_modify_lpar(
            LPAR,
            resources=LparResources(desired_memory=8192),
            ownership_override=True,
        )

    read.assert_not_awaited()
    hmc.update_logical_partition.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "write_name", "args"),
    [
        (configure_lpar_processor_compatibility, "set_lpar_proc_compat", ("POWER9",)),
        (synchronize_lpar_profile, "sync_lpar_profile", ()),
    ],
)
async def test_config_operations_reject_foreign_owner_before_ssh_write(
    operation: Operation, write_name: str, args: tuple[object, ...]
) -> None:
    """Each extracted configuration mutation checks ownership before its write."""
    hmc = _real_guard_hmc()
    write = AsyncMock(return_value="ok")

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=FOREIGN_OWNER),
        ),
        patch(f"hmcpctl.operations.lpar.configuration.{write_name}", new=write),
        pytest.raises(PermissionError, match="ownership_override=true"),
    ):
        await operation(hmc, SYSTEM_UUID, LPAR, *args)

    write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "write_name", "args"),
    [
        (configure_lpar_processor_compatibility, "set_lpar_proc_compat", ("POWER9",)),
        (synchronize_lpar_profile, "sync_lpar_profile", ()),
    ],
)
async def test_config_operations_override_skips_ownership_read(
    operation: Operation, write_name: str, args: tuple[object, ...]
) -> None:
    """An approved override bypasses the ownership read and reaches each write."""
    hmc = _real_guard_hmc()
    write = AsyncMock(return_value="ok")

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value=FOREIGN_OWNER),
        ) as read,
        patch(f"hmcpctl.operations.lpar.configuration.{write_name}", new=write),
    ):
        await operation(hmc, SYSTEM_UUID, LPAR, *args, ownership_override=True)

    read.assert_not_awaited()
    write.assert_awaited_once()


def _vios_guard_hmc() -> AsyncMock:
    """The real-guard double whose selected system hosts one VIOS (#1318)."""
    hmc = _real_guard_hmc()
    hmc.list_vios.return_value = [
        {"UUID": VIOS, "Resource": {"PartitionName": VIOS_NAME}}
    ]
    return hmc


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [VIOS, VIOS_NAME])
async def test_configure_lpar_msp_rejects_foreign_vios_owner_before_write(
    selector: str,
) -> None:
    """A VIOS carrying another agent's stamp is refused before the MSP write."""
    hmc = _vios_guard_hmc()
    read = AsyncMock(return_value=FOREIGN_OWNER)
    write = AsyncMock(return_value="ok")

    with (
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        patch("hmcpctl.operations.lpar.configuration.set_lpar_msp", new=write),
        pytest.raises(PermissionError, match="ownership_override=true"),
    ):
        await configure_lpar_msp(hmc, SYSTEM_UUID, selector, True)

    read.assert_awaited_once_with(hmc.config, SYSTEM_NAME, VIOS_NAME)
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_configure_lpar_msp_writes_unstamped_vios() -> None:
    """An unstamped VIOS is unowned under ADR 0011 and reaches the write."""
    hmc = _vios_guard_hmc()
    write = AsyncMock(return_value="ok")

    with (
        patch(
            "hmcpctl.operations.lpar.ownership.get_lpar_description",
            new=AsyncMock(return_value="\n"),
        ),
        patch("hmcpctl.operations.lpar.configuration.set_lpar_msp", new=write),
    ):
        await configure_lpar_msp(hmc, SYSTEM_UUID, VIOS_NAME, False)

    write.assert_awaited_once_with(hmc.config, SYSTEM_NAME, VIOS_NAME, False)


@pytest.mark.asyncio
async def test_configure_lpar_msp_override_skips_read_and_audits() -> None:
    """An approved override reads no stamp, audits the VIOS, and writes."""
    hmc = _vios_guard_hmc()
    read = AsyncMock(return_value=FOREIGN_OWNER)
    write = AsyncMock(return_value="ok")

    with (
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        patch("hmcpctl.operations.lpar.configuration.set_lpar_msp", new=write),
        patch(
            "hmcpctl.operations.lpar.ownership.audit.record_ownership_override"
        ) as audited,
    ):
        await configure_lpar_msp(hmc, SYSTEM_UUID, VIOS, True, ownership_override=True)

    read.assert_not_awaited()
    write.assert_awaited_once()
    assert audited.call_args.kwargs["lpar"] == VIOS_NAME


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [False, True])
async def test_configure_lpar_msp_refuses_non_vios_before_any_ssh(
    override: bool,
) -> None:
    """A selector outside the system's VIOS feed is refused, override or not."""
    hmc = _vios_guard_hmc()
    read = AsyncMock(return_value="")
    write = AsyncMock(return_value="ok")

    with (
        patch("hmcpctl.operations.lpar.ownership.get_lpar_description", new=read),
        patch("hmcpctl.operations.lpar.configuration.set_lpar_msp", new=write),
        pytest.raises(ResourceNotFoundError, match="lpar_env=vioserver"),
    ):
        await configure_lpar_msp(
            hmc, SYSTEM_UUID, LPAR, True, ownership_override=override
        )

    hmc.list_vios.assert_awaited_once_with(SYSTEM_UUID)
    read.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.parametrize(
    ("tool", "operation", "args"),
    [
        (
            server_configuration.hmc_set_lpar_msp,
            "configure_lpar_msp",
            (SYSTEM_UUID, LPAR, True),
        ),
        (
            server_configuration.hmc_set_lpar_proc_compat,
            "configure_lpar_processor_compatibility",
            (SYSTEM_UUID, LPAR, "POWER9"),
        ),
        (
            server_profiles.hmc_sync_lpar_profile,
            "synchronize_lpar_profile",
            (SYSTEM_UUID, LPAR),
        ),
    ],
)
def test_mcp_config_tools_forward_ownership_override(
    monkeypatch: pytest.MonkeyPatch, tool, operation: str, args: tuple[object, ...]
) -> None:
    """MCP wrappers expose and forward the explicit ownership override."""
    target_module = (
        server_profiles
        if tool is server_profiles.hmc_sync_lpar_profile
        else server_configuration
    )
    delegated = AsyncMock(return_value="ok")
    monkeypatch.setattr(target_module, operation, delegated)

    async def invoke(fn, *, profile=None):
        return await fn(AsyncMock())

    monkeypatch.setattr(
        target_module, "with_client", lambda fn, **kwargs: asyncio.run(invoke(fn))
    )
    result = tool(*args, ownership_override=True)

    assert result == "ok"
    delegated.assert_awaited_once()
    assert delegated.await_args.kwargs["ownership_override"] is True


@pytest.mark.asyncio
async def test_delete_adapter_returns_where_the_deletion_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = AsyncMock(return_value=LPAR)
    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.adapters.resolve_and_authorize_lpar_mutation",
        guard,
    )
    hmc = AsyncMock()
    hmc.get_logical_partition.return_value = {
        "Resource": {"CurrentProfileSync": "Disabled"}
    }

    result = await delete_adapter(
        hmc, None, LPAR, "ClientNetworkAdapter", "adapter-uuid"
    )

    assert result == ChangeLocation("Disabled", "current-configuration")
    hmc.get_logical_partition.assert_awaited_once_with(LPAR)
    hmc.delete_adapter.assert_awaited_once_with(
        LPAR, "ClientNetworkAdapter", "adapter-uuid"
    )
