"""Managed-system scoping for VIOS storage operations."""

from __future__ import annotations

from typing import cast

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.storage.resources import list_volume_groups

SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"
VIOS_UUID = "00000000-0000-0000-0000-000000000002"


class _StorageClient:
    def __init__(self) -> None:
        self.system_queries: list[str] = []
        self.vios_queries: list[tuple[str, str | None]] = []
        self.volume_group_queries: list[str] = []

    async def list_system_summaries(self) -> list[dict[str, str]]:
        self.system_queries.append("quick/All")
        return [{"UUID": SYSTEM_UUID, "SystemName": "system-a"}]

    async def find_vios_by_name(
        self, name: str, *, system_uuid: str | None = None
    ) -> dict[str, str]:
        self.vios_queries.append((name, system_uuid))
        return {"UUID": VIOS_UUID}

    async def list_volume_groups(self, vios_uuid: str) -> list[dict[str, object]]:
        self.volume_group_queries.append(vios_uuid)
        return []


@pytest.mark.asyncio
async def test_vios_name_is_resolved_with_managed_system_scope() -> None:
    client = _StorageClient()

    await list_volume_groups(
        cast(HMCClient, client), "shared-vios", system_name_or_uuid="system-a"
    )

    assert client.system_queries == ["quick/All"]
    assert client.vios_queries == [("shared-vios", SYSTEM_UUID)]
    assert client.volume_group_queries == [VIOS_UUID]


@pytest.mark.asyncio
async def test_vios_uuid_needs_no_managed_system_scope() -> None:
    client = _StorageClient()

    await list_volume_groups(cast(HMCClient, client), VIOS_UUID)

    assert client.system_queries == []
    assert client.vios_queries == []
    assert client.volume_group_queries == [VIOS_UUID]


VG_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_VG = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["rootvg", VG_UUID.upper()])
async def test_volume_group_selector_is_scoped_and_returns_canonical_uuid(selector):
    from unittest.mock import AsyncMock

    from hmcpctl.operations.storage.resources import create_virtual_disk

    client = AsyncMock()
    client.list_volume_groups.return_value = [
        {"UUID": VG_UUID, "Resource": {"GroupName": "rootvg", "FreeSpace": "bad"}},
        {"UUID": OTHER_VG, "Resource": {"GroupName": "other"}},
    ]
    await create_virtual_disk(client, VIOS_UUID, selector, "disk01", 1024)
    client.list_volume_groups.assert_awaited_once_with(VIOS_UUID)
    client.create_virtual_disk.assert_awaited_once_with(
        VIOS_UUID, VG_UUID, "disk01", 1024
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "rows"),
    [
        ("Rootvg", [{"UUID": VG_UUID, "Resource": {"GroupName": "rootvg"}}]),
        (VG_UUID, [{"UUID": OTHER_VG, "Resource": {"GroupName": "rootvg"}}]),
        ("rootvg", []),
        ("rootvg", [{"UUID": VG_UUID}, {"UUID": OTHER_VG}]),
        ("rootvg", [{"Resource": {"GroupName": "rootvg"}}]),
        ("rootvg", [{"UUID": "../escape", "Resource": {"GroupName": "rootvg"}}]),
        (
            "rootvg",
            [
                {"UUID": VG_UUID, "Resource": {"GroupName": "rootvg"}},
                {"UUID": OTHER_VG, "Resource": {"GroupName": "rootvg"}},
            ],
        ),
        (VG_UUID, [{"UUID": VG_UUID}, {"UUID": VG_UUID}]),
        (
            "rootvg",
            [
                {"UUID": VG_UUID, "Resource": {"GroupName": "rootvg"}},
                {"UUID": OTHER_VG, "Resource": None},
            ],
        ),
        (VG_UUID, [{"UUID": VG_UUID}, {"Resource": {"GroupName": "other"}}]),
    ],
)
async def test_volume_group_resolution_refuses_unproven_identity_before_write(
    selector, rows
):
    from unittest.mock import AsyncMock

    from hmcpctl.operations.storage.resources import create_virtual_disk

    client = AsyncMock()
    client.list_volume_groups.return_value = rows
    with pytest.raises(ValueError, match="Candidates:.*list-vgs"):
        await create_virtual_disk(client, VIOS_UUID, selector, "disk01", 1024)
    client.create_virtual_disk.assert_not_awaited()


@pytest.mark.asyncio
async def test_volume_group_lookup_read_failure_propagates_before_write():
    from unittest.mock import AsyncMock

    from hmcpctl.errors import HMCError
    from hmcpctl.operations.storage.resources import delete_virtual_disk

    client = AsyncMock()
    client.list_volume_groups.side_effect = HMCError("inventory unavailable")
    with pytest.raises(HMCError, match="inventory unavailable"):
        await delete_virtual_disk(client, VIOS_UUID, VG_UUID, "disk01")
    client.delete_virtual_disk.assert_not_awaited()
    client.list_storage_mappings.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments", "client_method", "tail"),
    [
        ("create_media_repository", (1024,), "create_media_repository", (1024,)),
        (
            "create_optical_media",
            ("install.iso", 1024),
            "create_optical_media",
            ("install.iso", 1024),
        ),
        ("delete_media_repository", (), "delete_media_repository", ()),
        (
            "delete_optical_media",
            ("install.iso",),
            "delete_optical_media",
            ("install.iso",),
        ),
        ("get_media_repository", (), "get_media_repository", ()),
        ("list_optical_media", (), "list_optical_media", ()),
        ("delete_virtual_disk", ("disk01",), "delete_virtual_disk", ("disk01",)),
    ],
)
async def test_volume_group_operation_resolves_name_before_client_call(
    operation, arguments, client_method, tail
):
    from unittest.mock import AsyncMock

    from hmcpctl.operations.storage import resources

    client = AsyncMock()
    client.list_volume_groups.return_value = [
        {"UUID": VG_UUID, "Resource": {"GroupName": "rootvg"}}
    ]
    client.list_optical_media.return_value = []
    client.list_optical_mappings.return_value = []
    client.list_storage_mappings.return_value = []
    await getattr(resources, operation)(client, VIOS_UUID, "rootvg", *arguments)
    getattr(client, client_method).assert_awaited_with(VIOS_UUID, VG_UUID, *tail)


@pytest.mark.asyncio
async def test_volume_group_diagnostics_show_safe_candidates():
    from unittest.mock import AsyncMock

    from hmcpctl.operations.storage.resources import resolve_volume_group_uuid

    client = AsyncMock()
    client.list_volume_groups.return_value = [
        {"UUID": VG_UUID, "Resource": {"GroupName": "other\nvg"}}
    ]
    with pytest.raises(ValueError) as raised:
        await resolve_volume_group_uuid(client, VIOS_UUID, "missing")
    message = str(raised.value)
    assert "'other\\nvg'" in message
    assert VG_UUID in message and VIOS_UUID in message
    assert "\n" not in message
