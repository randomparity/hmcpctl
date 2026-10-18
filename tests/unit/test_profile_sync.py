"""Where adapter changes live, and which current adapters a profile lacks (#981)."""

from unittest.mock import AsyncMock

import pytest

from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.profile_sync import (
    ChangeLocation,
    adapters_missing_from_profile,
    profile_adapter_warnings,
    read_change_location,
)
from hmcpctl.xmlutil import parse_feed

LPAR_UUID = "00000000-0000-0000-0000-000000000002"

# The shape a V10R3 HMC returned for a partition profile on 2026-09-24, cut to
# the I/O block; kb/kxe attributes kept so the parser sees what it will see live.
PROFILE_FEED = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>00000000-0000-0000-0000-0000000000aa</id>
<content type="application/vnd.ibm.powervm.uom+xml; type=LogicalPartitionProfile">
<LogicalPartitionProfile:LogicalPartitionProfile
 xmlns:LogicalPartitionProfile="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
 xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/" schemaVersion="V1_0">
<Metadata><Atom/></Metadata>
<IOConfigurationInstance kb="CUD" kxe="false" schemaVersion="V1_0">
<Metadata><Atom/></Metadata>
<MaximumVirtualIOSlots kxe="false" kb="CUD">6</MaximumVirtualIOSlots>
<ProfileVirtualIOAdapters kb="CUD" kxe="false" schemaVersion="V1_0">
<Metadata><Atom/></Metadata>
<ProfileVirtualIOAdapterSubclass>
<ProfileVirtualSCSIClientAdapter schemaVersion="V1_0">
<Metadata><Atom/></Metadata>
<VirtualSlotNumber kxe="false" kb="CUR">2</VirtualSlotNumber>
<IsRequired kxe="false" kb="CUD">false</IsRequired>
<AdapterType kb="CUR" kxe="false">Client</AdapterType>
<LocalPartitionID kxe="false" kb="CUR">1</LocalPartitionID>
<RemoteSlotNumber kb="CUA" kxe="false">3</RemoteSlotNumber>
<RemotePartitionID kxe="false" kb="CUR">100</RemotePartitionID>
</ProfileVirtualSCSIClientAdapter>
</ProfileVirtualIOAdapterSubclass>
</ProfileVirtualIOAdapters>
<VirtualOpticonnectPool kb="CUD" kxe="false">false</VirtualOpticonnectPool>
</IOConfigurationInstance>
<ProfileName kxe="false" kb="CUR">default_profile</ProfileName>
</LogicalPartitionProfile:LogicalPartitionProfile>
</content></entry></feed>"""


def _partition(resource: dict) -> AsyncMock:
    hmc = AsyncMock()
    hmc.get_logical_partition.return_value = {"Resource": resource}
    return hmc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "expected"),
    [
        (
            {
                "CurrentProfileSync": "On",
                "LastActivatedProfile": {"@attrs": {"ksv": "V1_7_0"}, "text": "p1"},
            },
            ChangeLocation("On", "current-configuration-and-profile", "p1"),
        ),
        (
            {"CurrentProfileSync": "Disabled", "LastActivatedProfile": "p1"},
            ChangeLocation("Disabled", "current-configuration"),
        ),
        (
            {"CurrentProfileSync": "Suspended"},
            ChangeLocation("Suspended", "current-configuration"),
        ),
        (
            {"CurrentProfileSync": "Sideways"},  # live-vocabulary: allow unknown mode
            ChangeLocation("Sideways", "unknown"),
        ),
        ({}, ChangeLocation(None, "unknown")),
    ],
)
async def test_read_change_location_maps_current_profile_sync(resource, expected):
    hmc = _partition(resource)

    assert await read_change_location(hmc, LPAR_UUID) == expected
    hmc.get_logical_partition.assert_awaited_once_with(LPAR_UUID)


@pytest.mark.asyncio
async def test_read_change_location_reports_unknown_for_a_missing_partition():
    hmc = AsyncMock()
    hmc.get_logical_partition.return_value = None

    assert await read_change_location(hmc, LPAR_UUID) == ChangeLocation(None, "unknown")


def test_summary_names_where_the_change_lives():
    both = ChangeLocation("On", "current-configuration-and-profile", "p1").summary()
    assert "current configuration" in both and "'p1'" in both
    unnamed = ChangeLocation("On", "current-configuration-and-profile").summary()
    assert "profile." in unnamed
    only = ChangeLocation("Disabled", "current-configuration").summary()
    assert "only in the current configuration" in only
    assert "not reported" in ChangeLocation(None, "unknown").summary()


def _adapters(by_type: dict[str, list[str | None]]) -> AsyncMock:
    hmc = AsyncMock()

    async def list_adapters(lpar_uuid, adapter_type):
        assert lpar_uuid == LPAR_UUID
        return [
            {"Resource": {} if slot is None else {"VirtualSlotNumber": slot}}
            for slot in by_type.get(adapter_type, [])
        ]

    hmc.list_adapters.side_effect = list_adapters
    return hmc


def _profile(*subclasses: dict) -> dict:
    block = list(subclasses) if len(subclasses) != 1 else subclasses[0]
    adapters = {"ProfileVirtualIOAdapters": {"ProfileVirtualIOAdapterSubclass": block}}
    return {"Resource": {"IOConfigurationInstance": adapters}}


@pytest.mark.asyncio
async def test_live_profile_shape_covers_its_own_vscsi_slot():
    profile = parse_feed(PROFILE_FEED)[0]
    hmc = _adapters({"VirtualSCSIClientAdapter": ["2"], "ClientNetworkAdapter": ["5"]})

    missing = await adapters_missing_from_profile(hmc, LPAR_UUID, profile)

    assert missing == ["ClientNetworkAdapter in virtual slot 5"]


@pytest.mark.asyncio
async def test_profile_slots_are_read_from_each_subclass_direct_child():
    profile = _profile(
        {
            "ProfileVirtualSCSIClientAdapter": [
                {"VirtualSlotNumber": "2"},
                {"VirtualSlotNumber": "3"},
            ]
        },
        {
            "ProfileClientNetworkAdapter": {
                "VirtualSlotNumber": "4",
                "Nested": {"VirtualSlotNumber": "6"},
            }
        },
        {"@attrs": {"x": "y"}, "Other": "text"},
    )
    hmc = _adapters(
        {
            "VirtualSCSIClientAdapter": ["2", "3"],
            "VirtualFibreChannelClientAdapter": ["6"],
            "ClientNetworkAdapter": ["4", None],
        }
    )

    missing = await adapters_missing_from_profile(hmc, LPAR_UUID, profile)

    assert missing == [
        "VirtualFibreChannelClientAdapter in virtual slot 6",
        "ClientNetworkAdapter in virtual slot not reported",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile",
    [
        {"Resource": {}},
        {"Resource": {"IOConfigurationInstance": ""}},
        {"Resource": {"IOConfigurationInstance": {"ProfileVirtualIOAdapters": ""}}},
        # Directly under the profile is not where the HMC puts them.
        {
            "Resource": {
                "ProfileVirtualIOAdapters": {
                    "ProfileVirtualIOAdapterSubclass": {
                        "ProfileVirtualSCSIClientAdapter": {"VirtualSlotNumber": "2"}
                    }
                }
            }
        },
        {},
    ],
)
async def test_a_profile_without_adapters_lacks_every_current_adapter(profile):
    hmc = _adapters({"VirtualSCSIClientAdapter": ["2"]})

    assert await adapters_missing_from_profile(hmc, LPAR_UUID, profile) == [
        "VirtualSCSIClientAdapter in virtual slot 2"
    ]


@pytest.mark.asyncio
async def test_profile_adapter_warnings_name_each_missing_adapter():
    hmc = _adapters({"VirtualSCSIClientAdapter": ["2", "7"]})

    warnings = await profile_adapter_warnings(hmc, LPAR_UUID, _profile())

    assert len(warnings) == 2
    assert "VirtualSCSIClientAdapter in virtual slot 7" in warnings[1]
    assert "without a partition profile" in warnings[1]


@pytest.mark.asyncio
async def test_profile_adapter_warnings_are_empty_when_the_profile_has_every_adapter():
    hmc = _adapters({"VirtualSCSIClientAdapter": ["2"]})
    profile = _profile({"ProfileVirtualSCSIClientAdapter": {"VirtualSlotNumber": "2"}})

    assert await profile_adapter_warnings(hmc, LPAR_UUID, profile) == ()


@pytest.mark.asyncio
async def test_profile_adapter_warnings_report_a_failed_feed_read_instead_of_raising():
    hmc = AsyncMock()
    hmc.list_adapters.side_effect = HMCError("GET feed failed", 500, "")

    warnings = await profile_adapter_warnings(hmc, LPAR_UUID, _profile())

    assert warnings == (
        "Partition profile adapter check not run: GET feed failed (HTTP 500)",
    )


VIOS_UUID = "00000000-0000-0000-0000-000000000003"
UNSYNCED = ChangeLocation("Disabled", "current-configuration")


def _operations():
    from hmcpctl.operations.storage import resources as storage
    from hmcpctl.operations.virtualization import adapters

    return [
        (
            "add_network_adapter",
            lambda h: adapters.add_network_adapter(
                h, LPAR_UUID, 1, system_name_or_uuid=None
            ),
        ),
        (
            "add_vscsi_adapter",
            lambda h: adapters.add_vscsi_adapter(
                h, LPAR_UUID, 1, 2, system_name_or_uuid=None
            ),
        ),
        (
            "add_vfc_adapter",
            lambda h: adapters.add_vfc_adapter(
                h, LPAR_UUID, 1, 2, system_name_or_uuid=None
            ),
        ),
        (
            "delete_adapter",
            lambda h: adapters.delete_adapter(
                h, LPAR_UUID, "ClientNetworkAdapter", "a", system_name_or_uuid=None
            ),
        ),
        (
            "map_storage_to_lpar",
            lambda h: storage.map_storage(
                h, VIOS_UUID, LPAR_UUID, kind="VirtualDisk", storage_name="d"
            ),
        ),
        (
            "create_optical_mapping",
            lambda h: storage.mount_optical_media(
                h, VIOS_UUID, LPAR_UUID, media_name="m"
            ),
        ),
        (
            "delete_storage_mapping",
            lambda h: storage.detach_storage_mapping(h, VIOS_UUID, "vhost0/vtscsi0"),
        ),
        (
            "delete_storage_mapping",
            lambda h: storage.unmount_optical_media(
                h, VIOS_UUID, LPAR_UUID, media_name="m"
            ),
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write", "operation"),
    _operations(),
    ids=[
        "add-network",
        "add-vscsi",
        "add-vfc",
        "delete-adapter",
        "map",
        "mount",
        "detach",
        "unmount",
    ],
)
async def test_each_operation_reads_the_location_before_its_write(
    monkeypatch, write, operation
):
    authorize = AsyncMock(return_value=LPAR_UUID)
    for module in ("virtualization.adapters", "storage.resources"):
        monkeypatch.setattr(
            f"hmcpctl.operations.{module}.resolve_and_authorize_lpar_mutation",
            authorize,
        )
    monkeypatch.setattr(
        "hmcpctl.operations.storage.resources.resolve_vios_uuid",
        AsyncMock(return_value=VIOS_UUID),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.storage.resources.storage_mapping_id",
        lambda _mapping: "vhost0/vtscsi0",
    )
    monkeypatch.setattr(
        "hmcpctl.operations.storage.resources.mapping_lpar_uuid",
        lambda _mapping: LPAR_UUID,
    )
    hmc = AsyncMock()
    hmc.get_logical_partition.return_value = {
        "Resource": {"CurrentProfileSync": "Disabled"}
    }
    hmc.list_storage_mappings.return_value = [{}]
    hmc.list_optical_mappings.return_value = [
        {"Storage": {"VirtualOpticalMedia": {"MediaName": "m"}}}
    ]

    result = await operation(hmc)

    called = [name for name, _args, _kwargs in hmc.mock_calls]
    assert called.index("get_logical_partition") < called.index(write)
    location = result if isinstance(result, ChangeLocation) else result.change_location
    assert location == UNSYNCED


@pytest.mark.asyncio
async def test_a_failed_partition_read_stops_the_write(monkeypatch):
    from hmcpctl.operations.virtualization.adapters import add_vscsi_adapter

    monkeypatch.setattr(
        "hmcpctl.operations.virtualization.adapters.resolve_and_authorize_lpar_mutation",
        AsyncMock(return_value=LPAR_UUID),
    )
    hmc = AsyncMock()
    hmc.get_logical_partition.side_effect = HMCError("GET partition failed", 500)

    with pytest.raises(HMCError):
        await add_vscsi_adapter(hmc, LPAR_UUID, 1, 2, system_name_or_uuid=None)

    hmc.add_vscsi_adapter.assert_not_awaited()
