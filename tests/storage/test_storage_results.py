"""Operation-boundary projections for VIOS storage inventory."""

from dataclasses import asdict
from pathlib import Path
from typing import cast

import pytest

from hmcpctl.client.client_parse import _parse_feed
from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.storage.resources import (
    list_optical_media,
    list_storage_mappings,
    list_volume_groups,
)

VIOS_UUID = "00000000-0000-0000-0000-000000000001"
_OBSERVED_FEED = (
    '<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>'
    '<VirtualIOServer xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">'
    + Path(__file__).with_name("vscsi_mapping_v10r3.xml").read_text(encoding="utf-8")
    + "</VirtualIOServer></content></entry></feed>"
)


def _observed_mappings() -> list[dict]:
    """The observed V10R3 mapping (#940): no UUID, absolute system-scoped LPAR link."""
    resource = _parse_feed(_OBSERVED_FEED, "observed")[0]["Resource"]
    return [resource["VirtualSCSIMappings"]["VirtualSCSIMapping"]]


class _StorageClient:
    async def list_volume_groups(self, _vios_uuid: str):
        return [
            {
                "UUID": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "Resource": {"GroupName": "rootvg", "FreeSpace": "10"},
            }
        ]

    async def list_optical_media(self, _vios_uuid: str, _vg_uuid: str):
        return [{"MediaName": "install.iso", "Size": "1"}]

    async def list_storage_mappings(self, _vios_uuid: str, _lpar_uuid=None):
        return _observed_mappings()


@pytest.mark.asyncio
async def test_storage_inventory_translates_hmc_resources() -> None:
    client = cast(HMCClient, _StorageClient())

    volume_groups = await list_volume_groups(client, VIOS_UUID)
    optical_media = await list_optical_media(
        client, VIOS_UUID, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    mappings = await list_storage_mappings(client, VIOS_UUID)

    assert asdict(volume_groups[0]) == {
        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "name": "rootvg",
        "capacity_gib": None,
        "free_space_gib": 10,
        "free_space_diagnostic": None,
    }
    assert asdict(optical_media[0]) == {
        "name": "install.iso",
        "size_mib": 1024,
    }
    assert asdict(mappings[0]) == {
        "id": "vhost0/vtscsi0",
        "lpar_uuid": "00000000-0000-4000-8000-0000000000AA",
        "backing_kind": "VirtualDisk",
        "backing_name": "vd-R1",
    }


@pytest.mark.asyncio
async def test_volume_group_rejects_missing_required_name() -> None:
    class MissingName:
        async def list_volume_groups(self, _vios_uuid: str):
            return [{"UUID": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "Resource": {}}]

    with pytest.raises(HMCError, match="GroupName"):
        await list_volume_groups(cast(HMCClient, MissingName()), VIOS_UUID)


def _vg_client(resource: dict[str, object]) -> HMCClient:
    class _Client:
        async def list_volume_groups(self, _vios_uuid: str):
            return [
                {
                    "UUID": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "Resource": {"GroupName": "rootvg", **resource},
                }
            ]

    return cast(HMCClient, _Client())


@pytest.mark.asyncio
async def test_volume_group_accepts_fractional_capacity_strings() -> None:
    """The HMC reports these fields as decimal strings with a fractional part.

    Observed live on V10R3 / POWER10: rejecting them failed the whole
    volume-group projection for a well-formed HMC reply.
    """
    groups = await list_volume_groups(
        _vg_client({"GroupCapacity": "279.25", "FreeSpace": "558.9111"}), VIOS_UUID
    )

    assert groups[0].capacity_gib == pytest.approx(279.25)
    assert groups[0].free_space_gib is None
    assert groups[0].free_space_diagnostic == "free_space_exceeds_capacity"


@pytest.mark.asyncio
async def test_volume_group_keeps_integral_values_as_int() -> None:
    """An integral value stays an int so rendered output gains no '.0' suffix."""
    groups = await list_volume_groups(
        _vg_client({"GroupCapacity": "102400", "FreeSpace": "0"}), VIOS_UUID
    )

    assert groups[0].capacity_gib == 102400
    assert not isinstance(groups[0].capacity_gib, float)
    assert groups[0].free_space_gib == 0
    assert not isinstance(groups[0].free_space_gib, float)


@pytest.mark.parametrize("value", ["279.0", "279.000"])
@pytest.mark.asyncio
async def test_volume_group_keeps_integral_float_strings_as_int(value: str) -> None:
    groups = await list_volume_groups(_vg_client({"GroupCapacity": value}), VIOS_UUID)

    assert groups[0].capacity_gib == 279
    assert not isinstance(groups[0].capacity_gib, float)


@pytest.mark.parametrize("value", ["9007199254740993", "9007199254740993.0"])
@pytest.mark.asyncio
async def test_volume_group_parses_an_integral_value_exactly(value: str) -> None:
    """2**53 + 1 is the smallest integer `float` cannot hold.

    Both spellings must yield the same number, so the integral path cannot go
    through `float()` — it would round both to 9007199254740992.
    """
    groups = await list_volume_groups(_vg_client({"GroupCapacity": value}), VIOS_UUID)

    assert groups[0].capacity_gib == 9007199254740993


@pytest.mark.asyncio
async def test_volume_group_absent_capacity_stays_none() -> None:
    groups = await list_volume_groups(_vg_client({}), VIOS_UUID)

    assert groups[0].capacity_gib is None
    assert groups[0].free_space_gib is None
    assert groups[0].free_space_diagnostic is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc",
        "279.25.1",
        "-5",
        "1e5",
        "NaN",
        "Infinity",
        "279,25",
        " 279.25 ",
        "٢٧٩",  # Arabic-Indic digits: `\d` would admit these, `[0-9]` does not.
        "279.25\n",  # A trailing newline: `$` would admit this, `\Z` does not.
        "9" * 21,  # Wider than any storage quantity; `int()` past 4300 digits raises.
        "1" * 21 + ".5",  # Likewise fractional, where `float()` would reach `inf`.
    ],
)
@pytest.mark.asyncio
async def test_volume_group_rejects_non_decimal_capacity(value: str) -> None:
    """A value that is not a plain bounded decimal raises, naming operation and field.

    The empty string is a deliberate rejection rather than a synonym for absent:
    an HMC that sends `<FreeSpace/>` is not reporting "no value", it is sending a
    malformed one, and `HMCError` names the field so the reply can be diagnosed.
    """
    with pytest.raises(
        HMCError, match="list_volume_groups returned an invalid GroupCapacity"
    ):
        await list_volume_groups(_vg_client({"GroupCapacity": value}), VIOS_UUID)


@pytest.mark.asyncio
async def test_volume_group_rejects_bool_capacity() -> None:
    """`True` is an `int` subclass; admitting it would report a capacity of 1."""
    with pytest.raises(HMCError, match="GroupCapacity"):
        await list_volume_groups(_vg_client({"GroupCapacity": True}), VIOS_UUID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size_gib", "size_mib"),
    [("1.0801", 1106.0224), ("0.5", 512), ("20", 20480), (None, None)],
)
async def test_optical_media_reports_the_gib_size_field_in_mib(
    size_gib, size_mib
) -> None:
    """The live medium carries Size in GiB (#963); size_mib is that value times 1024."""

    class _Client(_StorageClient):
        async def list_optical_media(self, _vios_uuid: str, _vg_uuid: str):
            return [{"MediaName": "install.iso", "Size": size_gib}]

    media = await list_optical_media(
        cast(HMCClient, _Client()), VIOS_UUID, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )

    assert media[0].size_mib == size_mib
    assert type(media[0].size_mib) is type(size_mib)


@pytest.mark.asyncio
async def test_storage_mapping_without_adapter_name_is_listed_with_null_id() -> None:
    class Unidentified:
        async def list_storage_mappings(self, _vios_uuid: str, _lpar_uuid=None):
            mapping = _observed_mappings()[0]
            del mapping["ServerAdapter"]["AdapterName"]
            return [mapping]

    mappings = await list_storage_mappings(cast(HMCClient, Unidentified()), VIOS_UUID)

    assert [(m.id, m.backing_name) for m in mappings] == [(None, "vd-R1")]
