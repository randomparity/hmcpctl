"""Contracts for the fleet utilization survey (ADR 0184).

Element nesting follows the read-only captures of 2026-10-01 (V10R3 and V11R2); every
name, serial and UUID here is synthetic.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from conftest import assert_no_mutating_requests, make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.operations.inventory.utilization import (
    AdapterFigures,
    DiskFigures,
    ProfileFailure,
    SystemReading,
    capacity_pairs,
    fleet_systems,
    read_system,
    rollup,
    survey_fleet,
    survey_hmc,
    utilization_pct,
)
from hmcpctl.xmlutil import parse_feed

_NS = 'xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"'
SYSTEM_UUID = "11111111-2222-3333-4444-555555555555"
NEVER_UUID = "aaaaaaaa-0000-0000-0000-000000000003"
PROFILE_UUID = "bbbbbbbb-0000-0000-0000-000000000001"


def _entry(uuid: str, element: str, body: str) -> str:
    return (
        f"<entry><id>urn:uuid:{uuid}</id>"
        '<content type="application/vnd.ibm.powervm.uom+xml">'
        f"<{element} {_NS}>{body}</{element}></content></entry>"
    )


def _feed(*entries: str) -> str:
    return '<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"


def _slot(drc: int, description: str, partition: int | None = None) -> str:
    owner = "" if partition is None else f"<PartitionID>{partition}</PartitionID>"
    return (
        f"<IOSlot><Description>{description}</Description>{owner}"
        "<SlotDynamicReconfigurationConnectorIndex>"
        f"{drc}</SlotDynamicReconfigurationConnectorIndex></IOSlot>"
    )


def _sriov_adapter(drc: int, mode: str, ports: int, unconfigured: str) -> str:
    return (
        f"<IOAdapterChoice><SRIOVAdapter><AdapterID>{drc}</AdapterID>"
        f"<AdapterMode>{mode}</AdapterMode>"
        f"<MaximumLogicalPortsSupported>{ports}</MaximumLogicalPortsSupported>"
        f"{unconfigured}</SRIOVAdapter></IOAdapterChoice>"
    )


_FREE_PORT = (
    "<SRIOVUnconfiguredLogicalPort><DynamicReconfigurationConnectorName>PHB 4098"
    "</DynamicReconfigurationConnectorName></SRIOVUnconfiguredLogicalPort>"
)
# Slot DRC indexes are decimal in REST answers; 553713696 is lshwres's 21010020.
IO_SLOTS = (
    "<IOSlots><Metadata><Atom/></Metadata>"
    + _slot(553713680, "Empty slot")
    + _slot(553713681, "PCIe3 4-port 10GbE SR Adapter", partition=1)
    + _slot(553713696, "PCIe4 2-port 100GbE RoCE Adapter x16")
    + _slot(553713697, "Universal Serial Bus UHC Spec")
    + "</IOSlots>"
)
SRIOV_ADAPTERS = (
    "<SRIOVAdapters><Metadata><Atom/></Metadata>"
    + _sriov_adapter(
        553713696,
        "Sriov",
        48,
        f"<UnconfiguredLogicalPorts>{_FREE_PORT * 2}</UnconfiguredLogicalPorts>",
    )
    + _sriov_adapter(553713681, "Dedicated", 0, "")
    + "</SRIOVAdapters>"
)
IO_CONFIGURATION = IO_SLOTS + SRIOV_ADAPTERS


def _system(
    name: str = "system-a",
    serial: str = "SER0001",
    omit: tuple[str, ...] = (),
    io: str | None = IO_CONFIGURATION,
) -> str:
    memory = {
        "ConfigurableSystemMemory": "1000000",
        "CurrentAvailableSystemMemory": "600000",
        "InstalledSystemMemory": "1048576",
        "MemoryUsedByHypervisor": "40000",
        "CurrentAssignedMemoryToPartitions": "350000",
    }
    processors = {
        "ConfigurableSystemProcessorUnits": "48",
        "CurrentAvailableSystemProcessorUnits": "30",
        "InstalledSystemProcessorUnits": "48",
    }
    mem = "".join(f"<{k}>{v}</{k}>" for k, v in memory.items() if k not in omit)
    proc = "".join(f"<{k}>{v}</{k}>" for k, v in processors.items() if k not in omit)
    return _entry(
        SYSTEM_UUID,
        "ManagedSystem",
        f"<SystemName>{name}</SystemName><State>operating</State>"
        "<SystemFirmware>FW1120.00 (1)</SystemFirmware>"
        "<MachineTypeModelAndSerialNumber><MachineType>9080</MachineType>"
        f"<Model>HEX</Model><SerialNumber>{serial}</SerialNumber>"
        "</MachineTypeModelAndSerialNumber>"
        f"<AssociatedSystemMemoryConfiguration>{mem}"
        "</AssociatedSystemMemoryConfiguration>"
        f"<AssociatedSystemProcessorConfiguration>{proc}"
        "</AssociatedSystemProcessorConfiguration>"
        + (
            ""
            if io is None
            else f"<AssociatedSystemIOConfiguration>{io}"
            "</AssociatedSystemIOConfiguration>"
        ),
    )


def _partition(
    uuid: str,
    state: str,
    memory: int,
    units: float,
    *,
    dedicated: bool = True,
    pool: int = 0,
    element: str = "LogicalPartition",
    profile_uuid: str | None = None,
    extra: str = "",
) -> str:
    if dedicated:
        processors = (
            "<CurrentHasDedicatedProcessors>true</CurrentHasDedicatedProcessors>"
            "<CurrentDedicatedProcessorConfiguration>"
            f"<CurrentProcessors>{units:g}</CurrentProcessors>"
            "</CurrentDedicatedProcessorConfiguration>"
        )
    else:
        processors = (
            "<CurrentHasDedicatedProcessors>false</CurrentHasDedicatedProcessors>"
            "<CurrentSharedProcessorConfiguration>"
            f"<CurrentProcessingUnits>{units:g}</CurrentProcessingUnits>"
            f"<CurrentSharedProcessorPoolID>{pool}</CurrentSharedProcessorPoolID>"
            "</CurrentSharedProcessorConfiguration>"
        )
    link = (
        '<AssociatedPartitionProfile href="https://hmc.test/rest/api/uom/'
        f"LogicalPartition/{uuid}/LogicalPartitionProfile/{profile_uuid}"
        '" rel="related"/>'
        if profile_uuid
        else ""
    )
    return _entry(
        uuid,
        element,
        f"<PartitionState>{state}</PartitionState>{link}"
        "<PartitionMemoryConfiguration>"
        f"<CurrentMemory>{memory}</CurrentMemory><RuntimeMemory>256</RuntimeMemory>"
        f"</PartitionMemoryConfiguration><PartitionProcessorConfiguration>"
        f"{processors}</PartitionProcessorConfiguration>{extra}",
    )


def _profile(uuid: str, memory: int, units: float, *, dedicated: bool) -> str:
    if dedicated:
        processors = (
            "<HasDedicatedProcessors>true</HasDedicatedProcessors>"
            "<DedicatedProcessorConfiguration>"
            f"<DesiredProcessors>{units:g}</DesiredProcessors>"
            "</DedicatedProcessorConfiguration>"
        )
    else:
        processors = (
            "<HasDedicatedProcessors>false</HasDedicatedProcessors>"
            "<SharedProcessorConfiguration>"
            f"<DesiredProcessingUnits>{units:g}</DesiredProcessingUnits>"
            "</SharedProcessorConfiguration>"
        )
    return _entry(
        uuid,
        "LogicalPartitionProfile",
        f"<ProfileMemory><DesiredMemory>{memory}</DesiredMemory></ProfileMemory>"
        f"<ProcessorAttributes>{processors}</ProcessorAttributes>",
    )


LPARS = _feed(
    _partition("aaaaaaaa-0000-0000-0000-000000000001", "running", 200000, 8),
    _partition(
        "aaaaaaaa-0000-0000-0000-000000000002",
        "open firmware",
        50000,
        1.5,
        dedicated=False,
        pool=3,
    ),
    _partition("aaaaaaaa-0000-0000-0000-000000000004", "not activated", 80000, 4),
    _partition(
        NEVER_UUID, "not activated", 0, 0, dedicated=False, profile_uuid=PROFILE_UUID
    ),
)


def _volume(
    udid: str | None, capacity: int, *, fc: str = "false", available: str = "false"
) -> str:
    device = "" if udid is None else f"<UniqueDeviceID>{udid}</UniqueDeviceID>"
    return (
        f"<PhysicalVolume>{device}"
        f"<AvailableForUsage>{available}</AvailableForUsage>"
        f"<VolumeCapacity>{capacity}</VolumeCapacity><VolumeName>hdisk0</VolumeName>"
        f"<IsFibreChannelBacked>{fc}</IsFibreChannelBacked>"
        "<IsISCSIBacked>false</IsISCSIBacked></PhysicalVolume>"
    )


def _volumes(*volumes: str) -> str:
    return (
        '<PhysicalVolumes group="ViosStorage"><Metadata><Atom/></Metadata>'
        + "".join(volumes)
        + "</PhysicalVolumes>"
    )


def _vios(number: int, storage: str, state: str = "running") -> str:
    return _partition(
        f"cccccccc-0000-0000-0000-00000000000{number}",
        state,
        20000,
        2,
        element="VirtualIOServer",
        extra=f"<PartitionName>vios-{number}</PartitionName>{storage}",
    )


INTERNAL = _volume("UDID-1", 286102)
SAN = _volume("UDID-9", 102400, fc="true", available="true")
VIOS = _feed(_vios(1, _volumes(INTERNAL, SAN)))
PROFILES = _feed(
    _profile("bbbbbbbb-0000-0000-0000-000000000009", 1, 1, dedicated=True),
    _profile(PROFILE_UUID, 16384, 0.5, dedicated=False),
)


class FakeClient:
    """An HMC answering parsed feeds; a value that is an exception is raised."""

    def __init__(self, **answers: Any) -> None:
        self.answers = {
            "systems": parse_feed(_feed(_system())),
            "lpars": parse_feed(LPARS),
            "vios": parse_feed(VIOS),
            "profiles": parse_feed(PROFILES),
        }
        self.answers.update(answers)

    def _answer(self, key: str) -> list[dict[str, Any]]:
        value = self.answers[key]
        if isinstance(value, Exception):
            raise value
        return value

    async def list_system_summaries(self) -> list[dict[str, Any]]:
        return [
            {
                "UUID": system["UUID"],
                "SystemName": system["Resource"].get("SystemName"),
                "State": system["Resource"].get("State"),
            }
            for system in self._answer("systems")
        ]

    async def get_managed_system(self, uuid: str) -> dict[str, Any] | None:
        return next(s for s in self._answer("systems") if s["UUID"] == uuid)

    async def list_logical_partitions(self, system_uuid=None):
        return self._answer("lpars")

    async def list_vios(self, system_uuid=None):
        return self._answer("vios")

    async def list_child(self, parent_type, parent_uuid, child_type):
        return self._answer("profiles")


async def _read(client: FakeClient) -> SystemReading:
    system = client.answers["systems"][0]
    return await read_system(client, "hmc-1", system)


@pytest.mark.asyncio
async def test_read_system_splits_allocation_per_adr_0184() -> None:
    reading = await _read(FakeClient())

    assert (reading.name, reading.machine_type, reading.model, reading.serial) == (
        "system-a",
        "9080",
        "HEX",
        "SER0001",
    )
    assert (reading.firmware, reading.state) == ("FW1120.00 (1)", "operating")
    memory = reading.memory
    assert (memory.installed, memory.configurable, memory.free) == (
        1048576,
        1000000,
        600000,
    )
    assert (memory.hypervisor, memory.vios) == (40000, 20000)
    assert (memory.client_active, memory.idle_reserved) == (250000, 80000)
    assert memory.other_reserved == 1000000 - 600000 - 40000 - 350000
    cpu = reading.cpu
    assert (cpu.vios, cpu.client_active, cpu.idle_reserved) == (2, 9.5, 4)
    assert cpu.other_reserved == 48 - 30 - 2 - 9.5 - 4
    assert (cpu.dedicated, cpu.shared) == (14, 1.5)
    assert reading.shared_pools == (3,)
    partitions = reading.partitions
    assert (partitions.running, partitions.not_activated, partitions.other) == (
        1,
        2,
        1,
    )
    assert reading.gaps == ()


@pytest.mark.asyncio
async def test_never_applied_partition_reports_profile_claim() -> None:
    reading = await _read(FakeClient())

    assert reading.partitions.profile_claims == 1
    assert reading.partitions.profile_claim_memory == 16384
    assert reading.partitions.profile_claim_units == 0.5
    assert reading.memory.idle_reserved == 80000


@pytest.mark.asyncio
async def test_failed_vios_feed_leaves_only_vios_dependent_figures_unknown() -> None:
    failure = HMCError("GET VirtualIOServer failed", 500)
    reading = await _read(FakeClient(vios=failure))

    assert reading.memory.vios is None
    assert reading.cpu.vios is None
    assert reading.cpu.other_reserved is None
    assert (reading.cpu.dedicated, reading.cpu.shared) == (None, None)
    assert reading.memory.other_reserved == 10000
    assert reading.memory.client_active == 250000
    assert reading.gaps == (
        "VirtualIOServer feed: GET VirtualIOServer failed (HTTP 500)",
    )


@pytest.mark.asyncio
async def test_failed_profile_read_is_a_gap() -> None:
    reading = await _read(FakeClient(profiles=HMCError("GET profile failed", 404)))

    assert reading.partitions.profile_claims == 1
    assert reading.partitions.profile_claim_memory is None
    assert reading.gaps == (
        "LogicalPartitionProfile feed: GET profile failed (HTTP 404)",
    )


@pytest.mark.asyncio
async def test_unreadable_profile_claim_names_the_reason() -> None:
    unlinked = parse_feed(_feed(_partition(NEVER_UUID, "not activated", 0, 0)))
    reading = await _read(FakeClient(lpars=unlinked))

    assert reading.partitions.profile_claim_memory is None
    assert reading.gaps == (
        (
            f"LogicalPartitionProfile: partition {NEVER_UUID} has no "
            "AssociatedPartitionProfile link"
        ),
    )
    other = _profile("bbbbbbbb-0000-0000-0000-000000000009", 1, 1, dedicated=True)
    reading = await _read(FakeClient(profiles=parse_feed(_feed(other))))

    assert reading.gaps == (
        (
            f"LogicalPartitionProfile: partition {NEVER_UUID}'s linked profile is not "
            "in its feed"
        ),
    )


@pytest.mark.asyncio
async def test_missing_system_figure_is_unknown_not_zero() -> None:
    systems = parse_feed(_feed(_system(omit=("MemoryUsedByHypervisor",))))
    reading = await _read(FakeClient(systems=systems))

    assert reading.memory.hypervisor is None
    assert reading.memory.other_reserved is None
    assert reading.unknown_figures == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["NaN", "inf", "-Infinity"])
async def test_non_finite_system_figure_is_unknown(text: str) -> None:
    xml = _system().replace(
        "<ConfigurableSystemProcessorUnits>48<",
        f"<ConfigurableSystemProcessorUnits>{text}<",
    )
    reading = await _read(FakeClient(systems=parse_feed(_feed(xml))))

    assert reading.cpu.configurable is None


def test_rollup_of_no_systems_counts_zero() -> None:
    total = rollup([])

    assert total.systems == 0
    assert (total.memory.configurable, total.cpu.free, total.partitions.running) == (
        0,
        0,
        0,
    )
    assert total.unknown == ()


@asynccontextmanager
async def _opened(client: Any):
    yield client


class StalledClient(FakeClient):
    """Logs on, then never answers the partition feed."""

    async def list_logical_partitions(self, system_uuid=None):
        await asyncio.sleep(60)
        return []


@pytest.mark.asyncio
async def test_survey_fleet_names_each_failed_profile() -> None:
    def open_client(profile: str):
        if profile == "bad-config":
            raise ValueError("profile 'bad-config' has no host")
        if profile == "refused":
            return _opened(FakeClient(systems=HMCError("logon failed", 401)))
        if profile == "stalled":
            return _opened(StalledClient())
        if profile == "slow":

            @asynccontextmanager
            async def hang():
                await asyncio.sleep(60)
                yield FakeClient()

            return hang()
        return _opened(FakeClient())

    survey = await survey_fleet(
        ["ok", "bad-config", "refused", "slow", "stalled", "ok"],
        open_client,
        hmc_timeout=0.2,
    )

    assert survey.profiles == ("ok", "bad-config", "refused", "slow", "stalled")
    assert [reading.profile for reading in survey.readings] == ["ok"]
    assert survey.failures == (
        ProfileFailure("bad-config", "ValueError: profile 'bad-config' has no host"),
        ProfileFailure("refused", "HMCError: logon failed (HTTP 401)"),
        ProfileFailure("slow", "no answer within 0.2 s"),
        ProfileFailure("stalled", "no answer within 0.2 s"),
    )


@pytest.mark.asyncio
async def test_cleanup_failure_after_a_complete_read_keeps_the_readings(caplog) -> None:
    @asynccontextmanager
    async def logoff_fails(profile: str):
        yield FakeClient()
        raise HMCError("HMC logoff failed", 500)

    survey = await survey_fleet(["hmc-1"], logoff_fails)

    assert [reading.profile for reading in survey.readings] == ["hmc-1"]
    assert survey.failures == ()
    assert "hmc-1" in caplog.text
    assert "logoff failed" in caplog.text


@pytest.mark.asyncio
async def test_survey_fleet_bounds_concurrency() -> None:
    active = 0
    peak = 0

    @asynccontextmanager
    async def open_client(profile: str):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        try:
            yield FakeClient()
        finally:
            active -= 1

    survey = await survey_fleet([f"p{i}" for i in range(6)], open_client, concurrency=2)

    assert peak == 2
    assert len(survey.readings) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("concurrency", "timeout", "message"),
    [
        (0, 1.0, "concurrency"),
        (1, 0.0, "hmc_timeout"),
        (1, float("nan"), "hmc_timeout"),
        (1, float("inf"), "hmc_timeout"),
    ],
)
async def test_survey_fleet_rejects_invalid_bounds(
    concurrency, timeout, message
) -> None:
    with pytest.raises(ValueError, match=message):
        await survey_fleet(["p"], _opened, concurrency=concurrency, hmc_timeout=timeout)


@pytest.mark.asyncio
async def test_fleet_systems_dedups_by_mtms_preferring_complete_reading() -> None:
    complete = await _read(FakeClient())
    partial = await read_system(
        FakeClient(vios=HMCError("down", 500)),
        "hmc-0",
        parse_feed(_feed(_system()))[0],
    )
    other = await read_system(
        FakeClient(), "hmc-0", parse_feed(_feed(_system("system-b", "SER0002")))[0]
    )

    systems = fleet_systems([partial, complete, other])

    assert [system.reading.name for system in systems] == ["system-a", "system-b"]
    assert systems[0].reading is complete
    assert systems[0].profiles == ("hmc-0", "hmc-1")
    twin = await read_system(FakeClient(), "hmc-0", parse_feed(_feed(_system()))[0])
    assert fleet_systems([complete, twin])[0].reading is twin


@pytest.mark.asyncio
async def test_rollup_sums_each_figure_and_names_shortfalls() -> None:
    complete = await _read(FakeClient())
    partial = await _read(FakeClient(vios=HMCError("down", 500)))

    total = rollup([complete, complete, partial])

    assert total.systems == 3
    assert total.memory.configurable == 3000000
    assert total.memory.vios == 40000
    assert total.memory.other_reserved == 30000
    assert total.cpu.client_active == 28.5
    assert total.cpu.other_reserved == 5.0
    assert total.partitions.profile_claim_memory == 49152
    assert (total.cpu_util_pct, total.mem_util_pct) == (37.5, 40.0)
    assert (total.cpu_allocated, total.mem_allocated) == (54.0, 1200000)
    assert total.unknown == (
        ("cpu", "vios", 1),
        ("cpu", "other_reserved", 1),
        ("cpu", "dedicated", 1),
        ("cpu", "shared", 1),
        ("memory", "vios", 1),
        *(("disk", item, 1) for item in DISK_FIELDS),
    )


@pytest.mark.asyncio
async def test_rollup_utilization_skips_systems_missing_a_capacity_figure() -> None:
    complete = await _read(FakeClient())
    systems = parse_feed(_feed(_system(omit=("CurrentAvailableSystemMemory",))))
    no_free = await _read(FakeClient(systems=systems))

    total = rollup([complete, no_free])

    assert (total.mem_allocated, total.mem_util_pct) == (400000, 40.0)
    assert ("memory", "free", 1) in total.unknown


def test_utilization_pct() -> None:
    assert utilization_pct(48, 30) == 37.5
    assert utilization_pct(None, 30) is None
    assert utilization_pct(48, None) is None
    assert utilization_pct(0, 0) is None


@pytest.mark.asyncio
async def test_survey_hmc_through_client_issues_only_gets(mock_hmc) -> None:
    def ok(text: str) -> httpx.Response:
        return httpx.Response(200, text=text)

    base = f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}"
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"UUID": SYSTEM_UUID, "SystemName": "system-a", "State": "operating"}
            ],
        )
    )
    entry = _system().replace(
        "<entry>", '<entry xmlns="http://www.w3.org/2005/Atom">', 1
    )
    mock_hmc.get(base).mock(return_value=ok(entry))
    mock_hmc.get(f"{base}/LogicalPartition").mock(return_value=ok(LPARS))
    mock_hmc.get(f"{base}/VirtualIOServer").mock(
        return_value=httpx.Response(500, text="ViosStorage")
    )
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{NEVER_UUID}/LogicalPartitionProfile"
    ).mock(return_value=ok(PROFILES))

    async with HMCClient(make_config()) as hmc:
        readings = await survey_hmc(hmc, "hmc-1")

    assert [reading.name for reading in readings] == ["system-a"]
    assert readings[0].partitions.profile_claim_memory == 16384
    assert readings[0].memory.vios is None
    assert readings[0].gaps[0].startswith("VirtualIOServer feed:")
    assert_no_mutating_requests(mock_hmc)


DISK_FIELDS = (
    "internal_total",
    "internal_assigned",
    "internal_free",
    "san_total",
    "san_assigned",
    "san_free",
)
UNKNOWN_DISK = DiskFigures(None, None, None, None, None, None)


@pytest.mark.asyncio
async def test_disk_and_adapter_figures_per_adr_0185() -> None:
    reading = await _read(FakeClient())

    assert reading.disk == DiskFigures(286102, 286102, 0, 102400, 0, 102400)
    assert reading.adapters == AdapterFigures(1, 1, 1, 1, 1, 48, 2)
    assert capacity_pairs(reading)["disk"] == (388502, 102400)
    assert capacity_pairs(reading)["slots"] == (3, 1)
    assert capacity_pairs(reading)["sriov"] == (48, 2)
    assert reading.gaps == ()


@pytest.mark.asyncio
async def test_disk_figures_count_a_shared_lun_once() -> None:
    mapped_san = _volume("UDID-9", 102400, fc="true", available="false")
    spare = _volume("UDID-2", 50000, available="true")
    vios = parse_feed(
        _feed(_vios(1, _volumes(INTERNAL, SAN)), _vios(2, _volumes(mapped_san, spare)))
    )
    reading = await _read(FakeClient(vios=vios))

    assert reading.disk == DiskFigures(336102, 286102, 50000, 102400, 102400, 0)


@pytest.mark.asyncio
async def test_failed_vios_feed_leaves_disk_unknown() -> None:
    reading = await _read(FakeClient(vios=HMCError("ViosStorage", 500)))

    assert reading.disk == UNKNOWN_DISK
    assert reading.adapters == AdapterFigures(1, 1, 1, 1, 1, 48, 2)
    assert reading.gaps == ("VirtualIOServer feed: ViosStorage (HTTP 500)",)


@pytest.mark.asyncio
async def test_vios_without_physical_volumes_leaves_disk_unknown() -> None:
    vios = parse_feed(
        _feed(
            _vios(1, _volumes(INTERNAL)),
            _vios(2, "", state="not activated"),
            _vios(3, "", state="running"),
        )
    )
    reading = await _read(FakeClient(vios=vios))

    assert reading.disk == UNKNOWN_DISK
    assert reading.gaps == (
        "PhysicalVolumes: VIOS vios-2 (not activated) reported no storage",
        "PhysicalVolumes: VIOS vios-3 (running) reported no storage",
    )


@pytest.mark.asyncio
async def test_volume_figures_the_hmc_omits_are_unknown() -> None:
    unflagged = _volume("UDID-1", 286102).replace(
        "<AvailableForUsage>false</AvailableForUsage>", ""
    )
    unbacked = SAN.replace("<IsFibreChannelBacked>true</IsFibreChannelBacked>", "")
    vios = parse_feed(_feed(_vios(1, _volumes(unflagged, SAN))))
    reading = await _read(FakeClient(vios=vios))

    assert reading.disk == DiskFigures(286102, None, None, 102400, 0, 102400)
    assert reading.gaps == (
        "PhysicalVolumes: VIOS vios-1 volume hdisk0 has no readable AvailableForUsage",
    )
    vios = parse_feed(_feed(_vios(1, _volumes(INTERNAL, unbacked))))
    reading = await _read(FakeClient(vios=vios))

    assert reading.disk == UNKNOWN_DISK
    assert reading.gaps == (
        (
            "PhysicalVolumes: VIOS vios-1 volume hdisk0 has no readable "
            "IsFibreChannelBacked/IsISCSIBacked"
        ),
    )
    pre_iscsi = INTERNAL.replace("<IsISCSIBacked>false</IsISCSIBacked>", "")
    vios = parse_feed(_feed(_vios(1, _volumes(pre_iscsi, SAN))))
    reading = await _read(FakeClient(vios=vios))

    assert reading.disk == DiskFigures(286102, 286102, 0, 102400, 0, 102400)


@pytest.mark.asyncio
async def test_volume_without_device_id_is_counted_per_vios_with_a_gap() -> None:
    blank = _volume("UDID-0", 1000).replace("UDID-0", "")
    vios = parse_feed(
        _feed(
            _vios(1, _volumes(_volume(None, 1000))),
            _vios(2, _volumes(blank)),
        )
    )
    reading = await _read(FakeClient(vios=vios))

    assert reading.disk.internal_total == 2000
    assert reading.gaps == tuple(
        f"PhysicalVolumes: VIOS vios-{n} lists a volume without UniqueDeviceID; "
        "it is not deduplicated"
        for n in (1, 2)
    )


@pytest.mark.asyncio
async def test_empty_containers_report_zero() -> None:
    fully_configured = _sriov_adapter(
        553713696, "Sriov", 48, '<UnconfiguredLogicalPorts kb="ROR" kxe="false"/>'
    )
    io = (
        '<IOSlots kb="CUD" kxe="false"/>'
        f"<SRIOVAdapters>{fully_configured}"
        f"{_sriov_adapter(553713700, 'Sriov', 0, '')}</SRIOVAdapters>"
    )
    systems = parse_feed(_feed(_system(io=io)))
    vios = parse_feed(_feed(_vios(1, '<PhysicalVolumes kb="CUD" kxe="false"/>')))
    reading = await _read(FakeClient(systems=systems, vios=vios))

    assert reading.disk == DiskFigures(0, 0, 0, 0, 0, 0)
    assert reading.adapters == AdapterFigures(0, 0, 0, 0, 2, 48, 0)
    assert reading.gaps == ()
    reading = await _read(FakeClient(vios=parse_feed(_feed())))

    assert reading.disk == DiskFigures(0, 0, 0, 0, 0, 0)


@pytest.mark.asyncio
async def test_system_without_io_configuration_leaves_adapters_unknown() -> None:
    reading = await _read(FakeClient(systems=parse_feed(_feed(_system(io=None)))))

    assert reading.adapters == AdapterFigures(None, None, None, None, None, None, None)
    assert reading.gaps == (
        "SRIOVAdapters: the system reported no SR-IOV adapter list",
        "IOSlots: the system reported no I/O slot list",
    )
    reading = await _read(FakeClient(systems=parse_feed(_feed(_system(io=IO_SLOTS)))))

    assert reading.adapters == AdapterFigures(1, None, None, 1, None, None, None)
    unreported = _sriov_adapter(553713696, "Sriov", 48, "")
    io = f"{IO_SLOTS}<SRIOVAdapters>{unreported}</SRIOVAdapters>"
    reading = await _read(FakeClient(systems=parse_feed(_feed(_system(io=io)))))

    assert reading.adapters == AdapterFigures(1, 1, 1, 1, 1, 48, None)
    assert reading.gaps == (
        "SRIOVAdapters: adapter 553713696 has no readable UnconfiguredLogicalPorts",
    )


@pytest.mark.asyncio
async def test_rollup_sums_disk_and_adapter_figures() -> None:
    complete = await _read(FakeClient())
    no_io = await _read(FakeClient(systems=parse_feed(_feed(_system(io=None)))))

    total = rollup([complete, complete, no_io])

    assert total.disk == DiskFigures(858306, 858306, 0, 307200, 0, 307200)
    assert total.adapters == AdapterFigures(2, 2, 2, 2, 2, 96, 4)
    assert total.disk_util_pct == 73.6
    assert total.slots_util_pct == 66.7
    assert total.sriov_util_pct == 95.8
    assert ("adapters", "slots_assigned", 1) in total.unknown


@pytest.mark.parametrize("feed", ["lpars", "vios", "profiles"])
@pytest.mark.asyncio
async def test_pin_mismatch_aborts_system_survey(feed):
    failure = HMCTLSFingerprintError("pin mismatch")
    with pytest.raises(HMCTLSFingerprintError) as caught:
        await _read(FakeClient(**{feed: failure}))
    assert caught.value is failure
