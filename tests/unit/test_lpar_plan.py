"""LPAR planning behind hmc_plan_lpar (#1221, ADR 0198)."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from hmcpctl.documents.lpar import LparResources
from hmcpctl.errors import HMCError, HMCTransportError
from hmcpctl.operations.affinity.rest import ProvisionAffinityAssessment
from hmcpctl.operations.lpar.assignments import (
    DedicatedPcieAssignment,
    LparPcieAssignments,
    SriovLogicalPortAssignment,
)
from hmcpctl.operations.lpar.plan import (
    CAPACITY_TOOL,
    NETWORKS_TOOL,
    PARTITIONS_TOOL,
    PLAN_TOOLS,
    STORAGE_DETAIL_TOOL,
    SYSTEMS_TOOL,
    VIOS_TOOL,
    VOLUME_GROUPS_TOOL,
    InstallMedia,
    InstallNetwork,
    InstallRoute,
    LparInstall,
    LparPlan,
    Placement,
    PlanAdapters,
    PlanBlocker,
    PlanRequest,
    PlanResource,
    PlanStorage,
    PlanSystem,
    PlanTargets,
    check_request,
    plan_digest,
    plan_lpar,
    required_tools,
)
from hmcpctl.ssh.affinity import MinimumAffinityPolicy

_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlanKeyMaterial user@host"
_URL = "https://iso.example.test/secret-path/installer.iso"
_MAC = "02:00:00:00:00:01"


def _uuid(number: int) -> str:
    return f"{number:08x}-0000-4000-8000-000000000000"


def _network(**changes: Any) -> InstallNetwork:
    network = InstallNetwork(
        address="10.0.0.5/24",
        routes=(InstallRoute(destination="0.0.0.0/0", gateway="10.0.0.1"),),
        dns=("10.0.0.2",),
    )
    return dataclasses.replace(network, **changes)


def _built(**changes: Any) -> LparInstall:
    install = LparInstall(
        profile="ubuntu-26.04.1",
        network=_network(),
        media=InstallMedia(mode="built"),
        ssh_authorized_keys=(_KEY,),
        login_user="ubuntu",
    )
    return dataclasses.replace(install, **changes)


def _prepared(**changes: Any) -> LparInstall:
    install = LparInstall(
        profile="rocky-9.8",
        network=_network(),
        media=InstallMedia(
            mode="prepared", url=_URL, producer_result={"token": "producer-secret"}
        ),
    )
    return dataclasses.replace(install, **changes)


def _request(**changes: Any) -> PlanRequest:
    request = PlanRequest(
        name="web1",
        adapters=PlanAdapters(port_vlan_id=100),
        storage=PlanStorage(storage_name="web1_root"),
        resources=LparResources(desired_memory=4096, desired_procs=0.5),
        system_name_or_uuid="sys1",
    )
    return dataclasses.replace(request, **changes)


def _install_request(install: LparInstall, **changes: Any) -> PlanRequest:
    mac = _MAC if install.media.mode == "prepared" else None
    fields: dict[str, Any] = {
        "adapters": PlanAdapters(port_vlan_id=100, mac=mac),
        "storage": PlanStorage(storage_name="web1_root", capacity_mib=20480),
        "install": install,
        "exclusive_writer_window": True,
    }
    return _request(**{**fields, **changes})


def _assessment(**changes: Any) -> ProvisionAffinityAssessment:
    assessment = ProvisionAffinityAssessment(
        system_name_or_uuid="sys1",
        lpar_name="web1",
        captured_score=90,
        captured_policy_state="configured",
        captured_minimum=None,
        captured_at=datetime(2026, 10, 2, tzinfo=UTC),
        stale_after_seconds=3600,
        response="warn",
        regression_threshold=10,
        optimization_threshold=5,
    )
    return dataclasses.replace(assessment, **changes)


def _storage(**changes: Any) -> PlanStorage:
    return dataclasses.replace(PlanStorage(storage_name="web1_root"), **changes)


_REJECTED: list[tuple[str, Any, str]] = [
    ("no selector", lambda: _request(system_name_or_uuid=None), "placement"),
    (
        "both selectors",
        lambda: _request(placement=Placement()),
        "placement",
    ),
    (
        "no placement selectors",
        lambda: _request(system_name_or_uuid=None, placement=Placement(systems=())),
        "placement.systems",
    ),
    (
        "17 placement selectors",
        lambda: _request(
            system_name_or_uuid=None,
            placement=Placement(systems=tuple(f"s{i}" for i in range(17))),
        ),
        "placement.systems",
    ),
    (
        "empty placement selector",
        lambda: _request(system_name_or_uuid=None, placement=Placement(systems=("",))),
        "placement.systems",
    ),
    ("empty name", lambda: _request(name=""), "name"),
    ("VLAN 0", lambda: _request(adapters=PlanAdapters(port_vlan_id=0)), "port_vlan_id"),
    (
        "VLAN 4095",
        lambda: _request(adapters=PlanAdapters(port_vlan_id=4095)),
        "port_vlan_id",
    ),
    (
        "vg without vios",
        lambda: _request(storage=_storage(vg_uuid=_uuid(9))),
        "storage.vg_uuid",
    ),
    (
        "vios not a UUID",
        lambda: _request(storage=_storage(vios_uuid="vios1")),
        "storage.vios_uuid",
    ),
    (
        "capacity zero",
        lambda: _request(storage=_storage(capacity_mib=0)),
        "storage.capacity_mib",
    ),
    (
        "capacity negative",
        lambda: _request(storage=_storage(capacity_mib=-1024)),
        "storage.capacity_mib",
    ),
    (
        "capacity with physical volume",
        lambda: _request(storage=_storage(kind="PhysicalVolume", capacity_mib=1024)),
        "storage.capacity_mib",
    ),
    (
        "install without capacity",
        lambda: _install_request(_built(), storage=_storage()),
        "storage.capacity_mib",
    ),
    (
        "unknown profile",
        lambda: _install_request(_built(profile="debian-13")),
        "install.profile",
    ),
    (
        "non-CIDR address",
        lambda: _install_request(_built(network=_network(address="10.0.0.5"))),
        "install.network.address",
    ),
    (
        "prefix 0",
        lambda: _install_request(_built(network=_network(address="10.0.0.5/0"))),
        "install.network.address",
    ),
    (
        "prefix 33",
        lambda: _install_request(_built(network=_network(address="10.0.0.5/33"))),
        "install.network.address",
    ),
    (
        "no routes",
        lambda: _install_request(_built(network=_network(routes=()))),
        "install.network.routes",
    ),
    (
        "17 routes",
        lambda: _install_request(
            _built(
                network=_network(
                    routes=(
                        InstallRoute("0.0.0.0/0", "10.0.0.1"),
                        *(
                            InstallRoute(f"192.168.{i}.0/24", "10.0.0.1")
                            for i in range(16)
                        ),
                    )
                )
            )
        ),
        "install.network.routes",
    ),
    (
        "no default route",
        lambda: _install_request(
            _built(
                network=_network(routes=(InstallRoute("192.168.0.0/24", "10.0.0.1"),))
            )
        ),
        "install.network.routes",
    ),
    (
        "two default routes",
        lambda: _install_request(
            _built(
                network=_network(
                    routes=(
                        InstallRoute("0.0.0.0/0", "10.0.0.1"),
                        InstallRoute("0.0.0.0/0", "10.0.0.3"),
                    )
                )
            )
        ),
        "install.network.routes",
    ),
    (
        "gateway outside network",
        lambda: _install_request(
            _built(network=_network(routes=(InstallRoute("0.0.0.0/0", "10.1.0.1"),)))
        ),
        "install.network.routes[0].gateway",
    ),
    (
        "four DNS servers",
        lambda: _install_request(
            _built(
                network=_network(dns=("10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.6"))
            )
        ),
        "install.network.dns",
    ),
    (
        "IPv6 DNS server",
        lambda: _install_request(_built(network=_network(dns=("fd00::1",)))),
        "install.network.dns",
    ),
    (
        "built without keys",
        lambda: _install_request(_built(ssh_authorized_keys=())),
        "install.ssh_authorized_keys",
    ),
    (
        "built without user",
        lambda: _install_request(_built(login_user=None)),
        "install.login_user",
    ),
    (
        "built with URL",
        lambda: _install_request(_built(media=InstallMedia(mode="built", url=_URL))),
        "install.media.url",
    ),
    (
        "prepared with keys",
        lambda: _install_request(_prepared(ssh_authorized_keys=(_KEY,))),
        "install.ssh_authorized_keys",
    ),
    (
        "prepared with user",
        lambda: _install_request(_prepared(login_user="ubuntu")),
        "install.login_user",
    ),
    (
        "prepared without URL",
        lambda: _install_request(
            _prepared(media=InstallMedia(mode="prepared", producer_result={"a": 1}))
        ),
        "install.media.url",
    ),
    (
        "prepared with ftp URL",
        lambda: _install_request(
            _prepared(
                media=InstallMedia(
                    mode="prepared",
                    url="ftp://iso.example.test/secret-path/x.iso",
                    producer_result={"a": 1},
                )
            )
        ),
        "install.media.url",
    ),
    (
        "prepared without producer result",
        lambda: _install_request(
            _prepared(media=InstallMedia(mode="prepared", url=_URL))
        ),
        "install.media.producer_result",
    ),
    (
        "producer result over 64 KiB",
        lambda: _install_request(
            _prepared(
                media=InstallMedia(
                    mode="prepared",
                    url=_URL,
                    producer_result={"token": "producer-secret" + "x" * 65536},
                )
            )
        ),
        "install.media.producer_result",
    ),
    (
        "prepared without mac",
        lambda: _install_request(_prepared(), adapters=PlanAdapters(port_vlan_id=100)),
        "adapters.mac",
    ),
    (
        "mac with built media",
        lambda: _install_request(
            _built(), adapters=PlanAdapters(port_vlan_id=100, mac=_MAC)
        ),
        "adapters.mac",
    ),
    (
        "mac without install",
        lambda: _request(adapters=PlanAdapters(port_vlan_id=100, mac=_MAC)),
        "adapters.mac",
    ),
    (
        "upper-case mac",
        lambda: _install_request(
            _prepared(),
            adapters=PlanAdapters(port_vlan_id=100, mac="02:00:00:00:00:AB"),
        ),
        "adapters.mac",
    ),
    (
        "multicast mac",
        lambda: _install_request(
            _prepared(),
            adapters=PlanAdapters(port_vlan_id=100, mac="03:00:00:00:00:01"),
        ),
        "adapters.mac",
    ),
    (
        "malformed mac",
        lambda: _install_request(
            _prepared(),
            adapters=PlanAdapters(port_vlan_id=100, mac="02-00-00-00-00-01"),
        ),
        "adapters.mac",
    ),
    (
        "key over 8192 characters",
        lambda: _install_request(_built(ssh_authorized_keys=(_KEY + "A" * 8192,))),
        "install.ssh_authorized_keys[0]",
    ),
    (
        "key with a newline",
        lambda: _install_request(_built(ssh_authorized_keys=(_KEY + "\n" + _KEY,))),
        "install.ssh_authorized_keys[0]",
    ),
    *(
        (
            f"key with {name}",
            lambda char=char: _install_request(
                _built(ssh_authorized_keys=(_KEY + char,))
            ),
            "install.ssh_authorized_keys[0]",
        )
        for name, char in (("U+2028", "\u2028"), ("NEL", "\x85"), ("NUL", "\x00"))
    ),
    *(
        (
            f"prepared URL {name}",
            lambda url=url: _install_request(
                _prepared(
                    media=InstallMedia(
                        mode="prepared", url=url, producer_result={"a": 1}
                    )
                )
            ),
            "install.media.url",
        )
        for name, url in (
            ("with a bracketed non-IP host", "http://[secret-path-host]/x.iso"),
            (
                "with an NFKC-invalid host",
                "https://ex\uff0eample\u2100.com/secret-path",
            ),
        )
    ),
    (
        "17 keys",
        lambda: _install_request(_built(ssh_authorized_keys=(_KEY,) * 17)),
        "install.ssh_authorized_keys",
    ),
    (
        "bad login user",
        lambda: _install_request(_built(login_user="Root")),
        "install.login_user",
    ),
    (
        "shared units above vcpus",
        lambda: _request(resources=LparResources(desired_procs=2.0, desired_vcpus=1)),
        "virtual processor",
    ),
    (
        "VIOS partition type",
        lambda: _request(partition_type="Virtual IO Server"),
        "VIOS",
    ),
    ("empty caller token", lambda: _request(caller_token=""), "caller"),
    ("caller token with a space", lambda: _request(caller_token="a b"), "caller"),
    (
        "affinity score 101",
        lambda: _request(
            minimum_affinity_policy=MinimumAffinityPolicy(
                min_affinity_score=101, min_affinity_score_action="warn"
            )
        ),
        "min_affinity_score",
    ),
    (
        "affinity assessment with placement",
        lambda: _request(
            system_name_or_uuid=None,
            placement=Placement(),
            affinity_assessment=_assessment(),
        ),
        "affinity_assessment",
    ),
    (
        "affinity assessment for another LPAR",
        lambda: _request(affinity_assessment=_assessment(lpar_name="web2")),
        "affinity_assessment",
    ),
    (
        "power_on with install",
        lambda: _install_request(_built(), power_on=True),
        "power_on",
    ),
    ("unknown boot", lambda: _request(boot="later"), "boot"),
    (
        "empty storage name",
        lambda: _request(storage=_storage(storage_name="")),
        "storage_name",
    ),
    (
        "unknown storage kind",
        lambda: _request(storage=_storage(kind="Tape")),
        "storage.kind",
    ),
    (
        "route destination not a network",
        lambda: _install_request(
            _built(network=_network(routes=(InstallRoute("0.0.0.1/0", "10.0.0.1"),)))
        ),
        "install.network.routes[0].destination",
    ),
    (
        "unknown media mode",
        lambda: _install_request(_built(media=InstallMedia(mode=cast(Any, "burned")))),
        "install.media.mode",
    ),
    ("deferred boot without install", lambda: _request(boot="deferred"), "boot"),
]


@pytest.mark.parametrize(
    ("build", "fragment"),
    [pytest.param(build, fragment, id=case) for case, build, fragment in _REJECTED],
)
def test_check_request_rejects(build: Any, fragment: str) -> None:
    with pytest.raises(ValueError, match=r".") as info:
        check_request(build())
    message = str(info.value)
    assert fragment in message
    assert "AAAAC3NzaC1lZDI1NTE5" not in message
    assert "secret-path" not in message
    assert "producer-secret" not in message


def test_check_request_accepts_valid_requests() -> None:
    for request in (
        _request(),
        _request(system_name_or_uuid=None, placement=Placement()),
        _request(system_name_or_uuid=None, placement=Placement(systems=("a", "b"))),
        _request(affinity_assessment=_assessment(), caller_token="ticket-42"),
        _request(storage=_storage(vios_uuid=_uuid(5), vg_uuid=_uuid(6))),
        _request(storage=_storage(kind="PhysicalVolume", vios_uuid=_uuid(5))),
        _install_request(_built()),
        _install_request(_built(), power_on=False),
        _install_request(_prepared(), boot="deferred"),
    ):
        assert check_request(request) == request


def test_check_request_normalizes_blank_selectors() -> None:
    request = _request(
        system_name_or_uuid=" ",
        placement=Placement(),
        storage=_storage(vios_uuid="", vg_uuid=""),
    )
    normalized = check_request(request)
    assert normalized.system_name_or_uuid is None
    assert normalized.storage.vios_uuid is None
    assert normalized.storage.vg_uuid is None
    install = _install_request(
        _built(), adapters=PlanAdapters(port_vlan_id=100, mac="")
    )
    assert check_request(install).adapters.mac is None
    with pytest.raises(ValueError, match="caller"):
        check_request(_request(caller_token=""))


def _targets(
    system: int = 1, vios: int | None = 5, group: int | None = 6
) -> PlanTargets:
    return PlanTargets(
        system=PlanSystem(id=f"lab/{_uuid(system)}", uuid=_uuid(system), name="sys1"),
        vios=PlanResource(uuid=_uuid(vios), name="vios1") if vios else None,
        volume_group=PlanResource(uuid=_uuid(group), name="rootvg") if group else None,
    )


def test_plan_digest_ignores_selector_spelling_and_placement() -> None:
    targets = _targets()
    by_name = plan_digest(_request(), targets, "lab")
    by_uuid = plan_digest(
        _request(system_name_or_uuid=_uuid(1).upper()), targets, "lab"
    )
    by_placement = plan_digest(
        _request(system_name_or_uuid=None, placement=Placement(systems=("sys1",))),
        targets,
        "lab",
    )
    named_storage = plan_digest(
        _request(storage=_storage(vios_uuid=_uuid(5).upper(), vg_uuid=_uuid(6))),
        targets,
        "lab",
    )
    assert by_name == by_uuid == by_placement == named_storage
    blank = _install_request(_built(), adapters=PlanAdapters(port_vlan_id=100, mac=""))
    absent = _install_request(_built())
    assert plan_digest(blank, targets, "lab") == plan_digest(absent, targets, "lab")


_CHANGES: list[tuple[str, Any]] = [
    ("name", lambda: _request(name="web2")),
    ("vlan", lambda: _request(adapters=PlanAdapters(port_vlan_id=101))),
    ("storage name", lambda: _request(storage=_storage(storage_name="other"))),
    ("kind", lambda: _request(storage=_storage(kind="PhysicalVolume"))),
    ("capacity", lambda: _request(storage=_storage(capacity_mib=1024))),
    (
        "resources",
        lambda: _request(
            resources=LparResources(desired_memory=8192, desired_procs=0.5)
        ),
    ),
    ("partition type", lambda: _request(partition_type="OS400")),
    (
        "dedicated assignment",
        lambda: _request(
            assignments=LparPcieAssignments(
                dedicated=(DedicatedPcieAssignment("default", "21010001"),)
            )
        ),
    ),
    (
        "sriov assignment",
        lambda: _request(
            assignments=LparPcieAssignments(
                sriov=(
                    SriovLogicalPortAssignment(
                        "default", "1", "0", "2", Decimal("2.5")
                    ),
                )
            )
        ),
    ),
    ("caller token", lambda: _request(caller_token="ticket-42")),
    (
        "minimum affinity",
        lambda: _request(
            minimum_affinity_policy=MinimumAffinityPolicy(
                min_affinity_score=50, min_affinity_score_action="warn"
            )
        ),
    ),
    ("affinity assessment", lambda: _request(affinity_assessment=_assessment())),
    ("power_on", lambda: _request(power_on=False)),
]
_INSTALL_CHANGES: list[tuple[str, Any]] = [
    ("install profile", lambda: _install_request(_built(profile="rocky-9.8"))),
    (
        "address",
        lambda: _install_request(_built(network=_network(address="10.0.0.6/24"))),
    ),
    ("key", lambda: _install_request(_built(ssh_authorized_keys=(_KEY + "2",)))),
    ("media mode", lambda: _install_request(_prepared())),
    ("boot", lambda: _install_request(_built(), boot="deferred")),
    (
        "writer window",
        lambda: _install_request(_built(), exclusive_writer_window=False),
    ),
]


@pytest.mark.parametrize(
    "build", [pytest.param(build, id=case) for case, build in _CHANGES]
)
def test_plan_digest_changes_with_each_field(build: Any) -> None:
    assert plan_digest(build(), _targets(), "lab") != plan_digest(
        _request(), _targets(), "lab"
    )


@pytest.mark.parametrize(
    "build", [pytest.param(build, id=case) for case, build in _INSTALL_CHANGES]
)
def test_plan_digest_changes_with_each_install_field(build: Any) -> None:
    base = plan_digest(_install_request(_built()), _targets(), "lab")
    assert plan_digest(build(), _targets(), "lab") != base


@pytest.mark.parametrize(
    ("targets", "connection"),
    [
        pytest.param(_targets(), "other", id="connection"),
        pytest.param(_targets(system=2), "lab", id="system"),
        pytest.param(_targets(vios=7), "lab", id="vios"),
        pytest.param(_targets(group=8), "lab", id="volume group"),
    ],
)
def test_plan_digest_changes_with_targets(
    targets: PlanTargets, connection: str
) -> None:
    assert plan_digest(_request(), targets, connection) != plan_digest(
        _request(), _targets(), "lab"
    )


def test_plan_digest_is_pinned() -> None:
    # A SHA-256 digest, not a credential.
    expected = "faf5f7e440e74edb25fcb76dcb90845046af17d8c138607261c8baafb337bd07"  # pragma: allowlist secret
    assert plan_digest(_request(), _targets(), "lab") == expected


# --- planning against a fake HMC (Task 3) -------------------------------------------

_READS = frozenset(
    {
        "list_system_summaries",
        "get_managed_system",
        "list_logical_partitions",
        "list_virtual_networks",
        "list_vios",
        "list_volume_groups",
        "get_vios_storage_detail",
    }
)


def _system_entry(
    number: int,
    *,
    state: str = "operating",
    free_memory: str | None = "65536",
    free_units: str = "8",
) -> dict:
    memory: dict[str, Any] = {"ConfigurableSystemMemory": "1048576"}
    if free_memory is not None:
        memory["CurrentAvailableSystemMemory"] = free_memory
    return {
        "UUID": _uuid(number),
        "Resource": {
            "SystemName": f"sys{number}",
            "State": state,
            "AssociatedSystemMemoryConfiguration": memory,
            "AssociatedSystemProcessorConfiguration": {
                "ConfigurableSystemProcessorUnits": "32",
                "CurrentAvailableSystemProcessorUnits": free_units,
            },
        },
    }


def _vios_uuid(system: int, index: int = 0) -> str:
    return f"{system:08x}-0000-4000-9000-{index:012x}"


def _vg_uuid(system: int, vios: int, index: int = 0) -> str:
    return f"{system:08x}-{vios:04x}-4000-a000-{index:012x}"


def _vios(system: int, index: int = 0, state: str = "running") -> dict:
    return {
        "UUID": _vios_uuid(system, index),
        "Resource": {"PartitionName": f"vios{index}", "PartitionState": state},
    }


def _vg(
    system: int,
    vios: int,
    index: int = 0,
    *,
    free_gib: str | None = "100",
    disks: tuple[str, ...] = (),
) -> dict:
    resource: dict[str, Any] = {"GroupName": f"vg{index}", "GroupCapacity": "200"}
    if free_gib is not None:
        resource["FreeSpace"] = free_gib
    named = [{"DiskName": disk, "DiskCapacity": "20"} for disk in disks]
    if len(named) == 1:
        resource["VirtualDisks"] = {"VirtualDisk": named[0]}
    elif named:
        resource["VirtualDisks"] = {"VirtualDisk": named}
    else:
        resource["VirtualDisks"] = {}
    return {"UUID": _vg_uuid(system, vios, index), "Resource": resource}


def _mapping(disk: str, lpar: int = 3) -> dict:
    return {
        "ServerAdapter": {"AdapterName": "vhost0", "RemoteLogicalPartitionID": "3"},
        "TargetDevice": {"VirtualTargetDevice": {"TargetName": "vtscsi0"}},
        "AssociatedLogicalPartition": {"href": f"/LogicalPartition/{_uuid(lpar)}"},
        "Storage": {"VirtualDisk": {"DiskName": disk}},
    }


class World:
    """One system's planning-relevant state, built for the common happy path."""

    def __init__(self, number: int = 1, **system: Any) -> None:
        self.entry = _system_entry(number, **system)
        self.partitions: list[dict] | Exception = []
        self.vlans: list[int] | Exception = [100]
        self.vioses: list[dict] | Exception = [_vios(number)]
        self.groups: dict[str, list[dict] | Exception] = {
            _vios_uuid(number): [_vg(number, 0)]
        }
        self.mappings: dict[str, list[dict]] = {}


class FakeHMC:
    """The read methods planning may call, each logged; nothing else exists."""

    def __init__(self, *worlds: World, allowlist: str = "iso.example.test") -> None:
        from hmcpctl.config import HMCConfig

        self.worlds = {world.entry["UUID"]: world for world in worlds}
        self.config = HMCConfig.from_mapping({"iso_url_allowlist": allowlist})
        self.calls: list[tuple[str, str | None]] = []
        self.systems_error: Exception | None = None
        self.extra_summaries: list[dict] = []
        self.detail_error: Exception | None = None

    def _world(self, uuid: str) -> World:
        return self.worlds[uuid]

    async def list_system_summaries(self) -> list[dict]:
        self.calls.append(("list_system_summaries", None))
        if self.systems_error is not None:
            raise self.systems_error
        return [
            {
                "UUID": world.entry["UUID"],
                "SystemName": world.entry["Resource"]["SystemName"],
                "State": world.entry["Resource"]["State"],
            }
            for world in self.worlds.values()
        ] + self.extra_summaries

    async def get_managed_system(self, uuid: str) -> dict | None:
        self.calls.append(("get_managed_system", uuid))
        world = self.worlds.get(uuid.lower())
        return world.entry if world else None

    async def list_logical_partitions(self, uuid: str) -> list[dict]:
        self.calls.append(("list_logical_partitions", uuid))
        return _answer(self._world(uuid).partitions)

    async def list_virtual_networks(self, uuid: str) -> list[dict]:
        self.calls.append(("list_virtual_networks", uuid))
        vlans = _answer(self._world(uuid).vlans)
        return [{"Resource": {"NetworkVLANID": str(vlan)}} for vlan in vlans]

    async def list_vios(self, uuid: str) -> list[dict]:
        self.calls.append(("list_vios", uuid))
        return _answer(self._world(uuid).vioses)

    async def list_volume_groups(self, vios_uuid: str) -> list[dict]:
        self.calls.append(("list_volume_groups", vios_uuid))
        for world in self.worlds.values():
            if vios_uuid in world.groups:
                return _answer(world.groups[vios_uuid])
        return []

    async def get_vios_storage_detail(self, vios_uuid: str) -> dict | None:
        self.calls.append(("get_vios_storage_detail", vios_uuid))
        if self.detail_error is not None:
            raise self.detail_error
        for world in self.worlds.values():
            if vios_uuid in world.mappings:
                found = world.mappings[vios_uuid]
                mappings: Any = found[0] if len(found) == 1 else found
                return {
                    "UUID": vios_uuid,
                    "Resource": {
                        "VirtualSCSIMappings": {"VirtualSCSIMapping": mappings}
                    },
                }
        return {"UUID": vios_uuid, "Resource": {"VirtualSCSIMappings": {}}}


def _answer(value: Any) -> Any:
    if isinstance(value, Exception):
        raise value
    return value


class Admit:
    """Denies the (tool, system, vios) triples it is given; ``"*"`` matches any."""

    def __init__(self, *denied: tuple[str, str | None, str | None]) -> None:
        self.denied = set(denied)
        self.calls: list[tuple[str, str | None, str | None]] = []

    def __call__(self, tool: str, system: str | None, vios: str | None) -> str | None:
        self.calls.append((tool, system, vios))
        for rule in ((tool, system, vios), (tool, "*", vios), (tool, system, "*")):
            if rule in self.denied:
                return f"{tool} is not permitted here"
        return None


def _plan(hmc: FakeHMC, request: PlanRequest, admit: Admit | None = None) -> LparPlan:
    plan = asyncio.run(
        plan_lpar(hmc, connection="lab", admit=admit or Admit(), request=request)
    )
    assert {name for name, _ in hmc.calls} <= _READS
    return plan


def _codes(blockers: list[PlanBlocker]) -> list[str]:
    return [blocker.code for blocker in blockers]


def _new_disk(**changes: Any) -> PlanRequest:
    return _request(**{"storage": _storage(capacity_mib=20480), **changes})


def test_explicit_plan_selects_targets_and_digest() -> None:
    world = World()
    plan = _plan(FakeHMC(world), _new_disk())
    assert plan.blockers == []
    assert plan.selected is not None
    assert plan.selected.system.uuid == _uuid(1)
    assert plan.selected.vios == PlanResource(uuid=_vios_uuid(1), name="vios0")
    assert plan.selected.volume_group == PlanResource(uuid=_vg_uuid(1, 0), name="vg0")
    assert plan.plan_digest == plan_digest(_new_disk(), plan.selected, "lab")
    assert [change.kind for change in plan.intended_changes] == [
        "create_partition",
        "stamp_ownership",
        "add_network_adapter",
        "add_vscsi_adapter",
        "create_virtual_disk",
        "map_storage",
        "write_profile",
        "power_on",
    ]
    assert [change.order for change in plan.intended_changes] == list(range(1, 9))
    assert any("not reserved" in text for text in plan.unverified)
    assert any("non-candidate" in text for text in plan.unverified)
    assert len(plan.candidates) == 1
    assert plan.candidates[0].free_memory_mib == 65536


def test_new_disk_plan_never_reads_storage_detail() -> None:
    hmc = FakeHMC(World())
    admit = Admit()
    _plan(hmc, _new_disk(), admit)
    assert "get_vios_storage_detail" not in {name for name, _ in hmc.calls}
    assert STORAGE_DETAIL_TOOL not in {tool for tool, _, _ in admit.calls}


def test_install_plan_adds_media_changes_and_unverified() -> None:
    plan = _plan(FakeHMC(World()), _install_request(_prepared()))
    assert plan.plan_digest is not None
    kinds = [change.kind for change in plan.intended_changes]
    assert kinds[-5:] == [
        "bind_media",
        "upload_media",
        "mount_media",
        "set_boot_order",
        "power_on",
    ]
    assert "boot_started" in " ".join(plan.unverified)
    deferred = _plan(FakeHMC(World()), _install_request(_built(), boot="deferred"))
    assert "deferred" in deferred.intended_changes[-1].detail


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        pytest.param(_new_disk(), [], id="no options"),
        pytest.param(
            _new_disk(
                minimum_affinity_policy=MinimumAffinityPolicy(50, "warn"),
                assignments=LparPcieAssignments(
                    dedicated=(
                        DedicatedPcieAssignment("default", "21010001"),
                        DedicatedPcieAssignment("default", "21010002"),
                    )
                ),
                affinity_assessment=_assessment(),
                power_on=False,
            ),
            [
                "set_minimum_affinity_policy",
                "assign_pcie",
                "assess_affinity",
            ],
            id="every non-install option",
        ),
        pytest.param(
            _install_request(_built(), affinity_assessment=_assessment()),
            ["assess_affinity"],
            id="install with assessment",
        ),
    ],
)
def test_intended_changes_order(request_: PlanRequest, expected: list[str]) -> None:
    plan = _plan(FakeHMC(World()), request_)
    kinds = [change.kind for change in plan.intended_changes]
    optional = [
        kind
        for kind in kinds
        if kind in {"set_minimum_affinity_policy", "assign_pcie", "assess_affinity"}
    ]
    assert optional == expected
    assert kinds.index("create_partition") == 0
    if "set_minimum_affinity_policy" in kinds:
        assert kinds.index("set_minimum_affinity_policy") == 2
    if "assign_pcie" in kinds:
        assert kinds.index("assign_pcie") > kinds.index("map_storage")
        assert kinds.index("write_profile") > kinds.index("assign_pcie")
    if request_.power_on is False:
        assert "power_on" not in kinds
    assert [change.order for change in plan.intended_changes] == list(
        range(1, len(kinds) + 1)
    )


def _scenario(name: str) -> tuple[FakeHMC, PlanRequest, str]:
    world = World()
    request = _new_disk()
    vios = _vios_uuid(1)
    if name == "system_not_operating":
        world = World(state="error")
    elif name == "insufficient_memory":
        world = World(free_memory="2048")
    elif name == "insufficient_processors":
        world = World(free_units="0.2")
    elif name == "capacity unavailable":
        world = World(free_memory=None)
        return FakeHMC(world), request, "unavailable"
    elif name == "name_exists":
        world.partitions = [{"UUID": _uuid(99), "Resource": {"PartitionName": "web1"}}]
    elif name == "vlan_missing":
        world.vlans = [200]
    elif name == "vios_not_running":
        world.vioses = [_vios(1, state="not activated")]
    elif name == "named vios not on system":
        request = _new_disk(storage=_storage(capacity_mib=20480, vios_uuid=_uuid(77)))
        return FakeHMC(world), request, "vios_not_running"
    elif name == "full volume group":
        world.groups[vios] = [_vg(1, 0, free_gib="10")]
        return FakeHMC(world), request, "storage_unplaceable"
    elif name == "disk name already present":
        world.groups[vios] = [_vg(1, 0, disks=("other", "web1_root"))]
        return FakeHMC(world), request, "storage_unplaceable"
    elif name == "unreadable FreeSpace":
        world.groups[vios] = [_vg(1, 0, free_gib=None)]
        return FakeHMC(world), request, "storage_unplaceable"
    elif name == "existing storage absent":
        request = _request()
        return FakeHMC(world), request, "storage_unplaceable"
    elif name == "two VIOSes":
        world.vioses = [_vios(1), _vios(1, 1)]
        world.groups[_vios_uuid(1, 1)] = [_vg(1, 1)]
        return FakeHMC(world), request, "ambiguous"
    elif name == "two groups":
        world.groups[vios] = [_vg(1, 0), _vg(1, 0, 1)]
        return FakeHMC(world), request, "ambiguous"
    elif name == "storage_mapped":
        world.groups[vios] = [_vg(1, 0, disks=("web1_root",))]
        world.mappings[vios] = [_mapping("web1_root")]
        request = _request()
    elif name == "exclusive_writer_window_required":
        request = _install_request(_built(), exclusive_writer_window=False)
    elif name == "url_not_allowlisted":
        return (
            FakeHMC(world, allowlist="other.example.test"),
            _install_request(_prepared()),
            name,
        )
    elif name == "url httpx rejects":
        bad = "https://iso.example.test/secret-path/\x00.iso"
        media = InstallMedia(mode="prepared", url=bad, producer_result={"a": 1})
        return (
            FakeHMC(world),
            _install_request(_prepared(media=media)),
            ("url_not_allowlisted"),
        )
    else:
        world.__dict__[name] = HMCError("feed refused")
        return FakeHMC(world), request, "unavailable"
    return FakeHMC(world), request, name


_REQUEST_CODES = {"exclusive_writer_window_required", "url_not_allowlisted"}


@pytest.mark.parametrize(
    "name",
    [
        "system_not_operating",
        "insufficient_memory",
        "insufficient_processors",
        "capacity unavailable",
        "name_exists",
        "vlan_missing",
        "vios_not_running",
        "named vios not on system",
        "full volume group",
        "disk name already present",
        "unreadable FreeSpace",
        "existing storage absent",
        "two VIOSes",
        "two groups",
        "storage_mapped",
        "exclusive_writer_window_required",
        "url_not_allowlisted",
        "url httpx rejects",
        "partitions",
        "vlans",
        "vioses",
    ],
)
def test_check_blockers(name: str) -> None:
    hmc, request, code = _scenario(name)
    plan = _plan(hmc, request)
    (candidate,) = plan.candidates
    if code in _REQUEST_CODES:
        assert _codes(plan.blockers) == [code]
        assert _codes(candidate.blockers) == []
    else:
        assert _codes(plan.blockers) == ["no_candidate"]
        assert _codes(candidate.blockers) == [code]
    assert plan.plan_digest is None
    for blocker in plan.blockers + candidate.blockers:
        assert "secret-path" not in blocker.detail
        assert len(blocker.detail) <= 500


def test_unavailable_volume_groups_name_the_tool() -> None:
    world = World()
    world.groups[_vios_uuid(1)] = HMCError("feed refused")
    plan = _plan(FakeHMC(world), _new_disk())
    blocker = plan.candidates[0].blockers[0]
    assert (blocker.code, blocker.tool) == ("unavailable", VOLUME_GROUPS_TOOL)


def test_ambiguous_pairs_are_capped_at_5() -> None:
    world = World()
    world.groups[_vios_uuid(1)] = [_vg(1, 0, index) for index in range(10)]
    plan = _plan(FakeHMC(world), _new_disk())
    (blocker,) = plan.candidates[0].blockers
    assert blocker.code == "ambiguous"
    assert blocker.detail.count("-a000-") == 5
    assert blocker.detail.endswith("and 5 more")
    assert len(blocker.detail) <= 500


def test_named_vios_and_group_resolve_without_ambiguity() -> None:
    world = World()
    world.vioses = [_vios(1), _vios(1, 1)]
    world.groups[_vios_uuid(1)] = [_vg(1, 0), _vg(1, 0, 1)]
    world.groups[_vios_uuid(1, 1)] = [_vg(1, 1)]
    storage = _storage(
        capacity_mib=20480, vios_uuid=_vios_uuid(1).upper(), vg_uuid=_vg_uuid(1, 0, 1)
    )
    hmc = FakeHMC(world)
    plan = _plan(hmc, _request(storage=storage))
    assert plan.selected is not None
    assert plan.selected.volume_group is not None
    assert plan.selected.volume_group.uuid == _vg_uuid(1, 0, 1)
    assert ("list_volume_groups", _vios_uuid(1, 1)) not in hmc.calls


def test_existing_virtual_disk_resolves_its_holding_group() -> None:
    world = World()
    world.groups[_vios_uuid(1)] = [
        _vg(1, 0, disks=("a",)),
        _vg(1, 0, 1, disks=("web1_root", "b")),
    ]
    world.mappings[_vios_uuid(1)] = [_mapping("other"), _mapping("a")]
    plan = _plan(FakeHMC(world), _request())
    assert plan.selected is not None
    assert plan.selected.volume_group is not None
    assert plan.selected.volume_group.uuid == _vg_uuid(1, 0, 1)
    assert plan.plan_digest is not None


def test_physical_volume_needs_named_vios_and_is_unverified() -> None:
    unnamed = _plan(FakeHMC(World()), _request(storage=_storage(kind="PhysicalVolume")))
    assert _codes(unnamed.candidates[0].blockers) == ["storage_unplaceable"]
    named = _plan(
        FakeHMC(World()),
        _request(storage=_storage(kind="PhysicalVolume", vios_uuid=_vios_uuid(1))),
    )
    assert named.selected is not None
    assert named.selected.volume_group is None
    assert any("physical volume" in text for text in named.unverified)


def _placement(*worlds: World, systems: tuple[str, ...] | None = None) -> PlanRequest:
    return _new_disk(system_name_or_uuid=None, placement=Placement(systems=systems))


def test_placement_selects_first_unblocked_in_rank_order() -> None:
    # UUID order (1, 2, 3) is the reverse of rank order, so ranking is observable.
    loose = World(1, free_memory="65536")
    middle = World(2, free_memory="16384")
    tight = World(3, free_memory="8192")
    tight.vlans = [200]
    plan = _plan(FakeHMC(loose, middle, tight), _placement())
    assert [c.targets.system.uuid for c in plan.candidates if c.targets] == [
        _uuid(3),
        _uuid(2),
        _uuid(1),
    ]
    assert plan.selected is not None
    assert plan.selected.system.uuid == _uuid(2)
    assert _codes(plan.candidates[0].blockers) == ["vlan_missing"]


def test_new_disk_name_is_checked_across_every_group_on_the_vios() -> None:
    world = World()
    world.groups[_vios_uuid(1)] = [_vg(1, 0), _vg(1, 0, 1, disks=("web1_root",))]
    storage = _storage(
        capacity_mib=20480, vios_uuid=_vios_uuid(1), vg_uuid=_vg_uuid(1, 0)
    )
    plan = _plan(FakeHMC(world), _request(storage=storage))
    assert _codes(plan.candidates[0].blockers) == ["storage_unplaceable"]


def test_ambiguous_better_fit_stays_listed() -> None:
    tight = World(1, free_memory="8192")
    tight.groups[_vios_uuid(1)] = [_vg(1, 0), _vg(1, 0, 1)]
    loose = World(2)
    plan = _plan(FakeHMC(tight, loose), _placement())
    assert plan.selected is not None
    assert plan.selected.system.uuid == _uuid(2)
    first = plan.candidates[0]
    assert _codes(first.blockers) == ["ambiguous"]
    assert _vg_uuid(1, 0, 1) in first.blockers[0].detail


def test_placement_selectors_are_admitted_before_resolution() -> None:
    hmc = FakeHMC(World(1), World(2))
    admit = Admit((PARTITIONS_TOOL, "sys2", None))
    plan = _plan(hmc, _placement(systems=("sys1", "sys2", "sys1")), admit)
    assert hmc.calls.count(("list_system_summaries", None)) == 1
    assert [c.selector for c in plan.candidates] == ["sys1", "sys2"]
    assert _codes(plan.candidates[1].blockers) == ["denied"]
    assert plan.candidates[1].blockers[0].tool == PARTITIONS_TOOL
    assert plan.selected is not None


def test_unresolved_selector_is_unavailable() -> None:
    plan = _plan(FakeHMC(World(1)), _placement(systems=("nope", "sys1")))
    assert plan.candidates[-1].targets is None
    assert _codes(plan.candidates[-1].blockers) == ["unavailable"]


def test_placement_enumeration_bounded_to_16() -> None:
    worlds = [World(number) for number in range(1, 18)]
    hmc = FakeHMC(*worlds)
    plan = _plan(hmc, _placement())
    assert plan.candidates_truncated is True
    assert plan.candidates_limit == 16
    assert len(plan.candidates) == 16
    assert ("list_logical_partitions", _uuid(17)) not in hmc.calls
    assert ("get_managed_system", _uuid(17)) not in hmc.calls


def test_enumeration_denied_or_unavailable_is_a_request_blocker() -> None:
    denied = _plan(FakeHMC(World()), _placement(), Admit((SYSTEMS_TOOL, None, None)))
    assert _codes(denied.blockers) == ["denied", "no_candidate"]
    hmc = FakeHMC(World())
    hmc.systems_error = HMCError("feed refused")
    failed = _plan(hmc, _placement())
    assert _codes(failed.blockers) == ["unavailable", "no_candidate"]


def test_transport_failure_stops_later_candidates() -> None:
    first = World(1, free_memory="8192")
    first.vioses = HMCTransportError("connection reset")
    second = World(2)
    hmc = FakeHMC(first, second)
    plan = _plan(hmc, _placement())
    assert plan.selected is None
    assert _codes(plan.candidates[1].blockers) == ["unavailable"]
    assert ("list_logical_partitions", _uuid(2)) not in hmc.calls


def test_denied_target_is_not_read() -> None:
    hmc = FakeHMC(World())
    admit = Admit((VOLUME_GROUPS_TOOL, "sys1", _vios_uuid(1)))
    plan = _plan(hmc, _new_disk(), admit)
    (blocker,) = plan.candidates[0].blockers
    assert (blocker.code, blocker.tool, blocker.target) == (
        "denied",
        VOLUME_GROUPS_TOOL,
        _vios_uuid(1),
    )
    assert ("list_volume_groups", _vios_uuid(1)) not in hmc.calls


def test_enumerated_systems_are_admitted_by_uuid() -> None:
    admit = Admit()
    _plan(FakeHMC(World()), _placement(), admit)
    assert (PARTITIONS_TOOL, _uuid(1), None) in admit.calls
    assert (VIOS_TOOL, _uuid(1), None) in admit.calls
    assert (VOLUME_GROUPS_TOOL, _uuid(1), _vios_uuid(1)) in admit.calls


def test_denied_capacity_reports_on_every_candidate() -> None:
    admit = Admit((CAPACITY_TOOL, None, None))
    plan = _plan(FakeHMC(World(1), World(2)), _placement(), admit)
    assert all(
        _codes(candidate.blockers) == ["denied"] for candidate in plan.candidates
    )
    assert [call for call in admit.calls if call[0] == CAPACITY_TOOL] == [
        (CAPACITY_TOOL, None, None)
    ]


def test_omitted_desired_figures_are_unverified() -> None:
    plan = _plan(FakeHMC(World()), _new_disk(resources=LparResources()))
    assert plan.selected is not None
    assert any("HMC's default" in text for text in plan.unverified)


def test_assignments_and_affinity_are_unverified() -> None:
    request = _new_disk(
        minimum_affinity_policy=MinimumAffinityPolicy(50, "warn"),
        assignments=LparPcieAssignments(
            dedicated=(DedicatedPcieAssignment("default", "21010001"),)
        ),
    )
    text = " ".join(_plan(FakeHMC(World()), request).unverified)
    assert "PCIe" in text
    assert "minimum-affinity" in text


def test_required_tools_follow_the_request() -> None:
    assert SYSTEMS_TOOL not in required_tools(_new_disk())
    assert STORAGE_DETAIL_TOOL not in required_tools(_new_disk())
    assert STORAGE_DETAIL_TOOL in required_tools(_request())
    assert SYSTEMS_TOOL in required_tools(_placement())
    assert SYSTEMS_TOOL not in required_tools(_placement(systems=("a",)))
    assert set(PLAN_TOOLS) == set(
        required_tools(_request(system_name_or_uuid=None, placement=Placement()))
    )


def test_plan_digest_refuses_an_unencodable_value() -> None:
    media = InstallMedia(mode="prepared", url=_URL, producer_result={"a": {1, 2}})
    with pytest.raises(TypeError, match="set"):
        plan_digest(_install_request(_prepared(media=media)), _targets(), "lab")


@pytest.mark.parametrize(
    ("denied", "request_", "tool"),
    [
        pytest.param(
            (NETWORKS_TOOL, "sys1", None), _new_disk(), NETWORKS_TOOL, id="vlan"
        ),
        pytest.param((VIOS_TOOL, "sys1", None), _new_disk(), VIOS_TOOL, id="vios"),
        pytest.param(
            (STORAGE_DETAIL_TOOL, "sys1", "*"), None, STORAGE_DETAIL_TOOL, id="mapping"
        ),
        pytest.param(
            (PARTITIONS_TOOL, _uuid(1), None),
            _new_disk(system_name_or_uuid=None, placement=Placement()),
            PARTITIONS_TOOL,
            id="enumerated name",
        ),
    ],
)
def test_each_denied_read_is_a_blocker_and_unread(
    denied: tuple[str, str, str | None], request_: PlanRequest | None, tool: str
) -> None:
    world = World()
    if request_ is None:
        world.groups[_vios_uuid(1)] = [_vg(1, 0, disks=("web1_root",))]
    hmc = FakeHMC(world)
    plan = _plan(hmc, request_ or _request(), Admit(denied))
    assert [(b.code, b.tool) for b in plan.candidates[0].blockers] == [("denied", tool)]
    method = {
        NETWORKS_TOOL: "list_virtual_networks",
        VIOS_TOOL: "list_vios",
        STORAGE_DETAIL_TOOL: "get_vios_storage_detail",
        PARTITIONS_TOOL: "list_logical_partitions",
    }[tool]
    assert method not in {name for name, _ in hmc.calls}


def test_unavailable_storage_detail_names_the_tool() -> None:
    world = World()
    world.groups[_vios_uuid(1)] = [_vg(1, 0, disks=("web1_root",))]
    hmc = FakeHMC(world)
    hmc.detail_error = HMCError("group refused")
    plan = _plan(hmc, _request())
    assert [(b.code, b.tool) for b in plan.candidates[0].blockers] == [
        ("unavailable", STORAGE_DETAIL_TOOL)
    ]


def test_unreadable_volume_group_entry_is_unavailable() -> None:
    world = World()
    world.groups[_vios_uuid(1)] = [{"Resource": {"GroupName": "vg0"}}]
    plan = _plan(FakeHMC(world), _new_disk())
    assert [(b.code, b.tool) for b in plan.candidates[0].blockers] == [
        ("unavailable", VOLUME_GROUPS_TOOL)
    ]


def test_uuid_selector_resolves_through_quick_all() -> None:
    hmc = FakeHMC(World())
    plan = _plan(hmc, _new_disk(system_name_or_uuid=_uuid(1).upper()))
    assert ("get_managed_system", _uuid(1)) in hmc.calls
    assert plan.selected is not None


def test_resolution_transport_failure_stops_later_selectors() -> None:
    hmc = FakeHMC(World(1), World(2))
    hmc.systems_error = HMCTransportError("connection reset")
    plan = _plan(hmc, _placement(systems=("sys1", _uuid(2))))
    assert all(_codes(c.blockers) == ["unavailable"] for c in plan.candidates)
    assert hmc.calls == [("list_system_summaries", None)]


def test_intended_changes_collapse_assignments_into_one_bounded_change() -> None:
    assignments = LparPcieAssignments(
        dedicated=tuple(
            DedicatedPcieAssignment("default", f"2101{i:04d}") for i in range(40)
        )
    )
    plan = _plan(FakeHMC(World()), _new_disk(assignments=assignments))
    (change,) = [c for c in plan.intended_changes if c.kind == "assign_pcie"]
    assert "40 dedicated" in change.detail


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(
            _request(), _request(power_on=True), id="power_on omitted vs true"
        ),
        pytest.param(
            _install_request(_built()),
            _install_request(_built(), power_on=False),
            id="power_on under install",
        ),
        pytest.param(
            _request(),
            _request(exclusive_writer_window=True),
            id="writer window unused",
        ),
        pytest.param(
            _request(affinity_assessment=_assessment()),
            _request(
                system_name_or_uuid=_uuid(1),
                affinity_assessment=_assessment(system_name_or_uuid=_uuid(1)),
            ),
            id="assessment by name vs UUID",
        ),
    ],
)
def test_plan_digest_is_equal_for_equivalent_requests(
    left: PlanRequest, right: PlanRequest
) -> None:
    assert plan_digest(left, _targets(), "lab") == plan_digest(right, _targets(), "lab")


def test_ambiguous_system_name_is_an_unresolved_candidate() -> None:
    hmc = FakeHMC(World(1), World(2))
    hmc.extra_summaries = [
        {"UUID": _uuid(9), "SystemName": "sys1", "State": "operating"}
    ]
    plan = _plan(hmc, _placement(systems=("sys1", _uuid(2))))
    unresolved = [c for c in plan.candidates if c.targets is None]
    assert [(c.selector, _codes(c.blockers)) for c in unresolved] == [
        ("sys1", ["unavailable"])
    ]
    assert plan.selected is not None
    assert plan.selected.system.uuid == _uuid(2)


def test_one_system_named_twice_is_evaluated_once() -> None:
    plan = _plan(FakeHMC(World()), _placement(systems=("sys1", _uuid(1).upper())))
    assert len(plan.candidates) == 1
    assert plan.candidates[0].selector == "sys1"


@pytest.mark.parametrize("weight", [0, 64])
def test_check_request_rejects_update_weight(weight, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Planner reached normalization or digest before rejection")

    monkeypatch.setattr("hmcpctl.operations.lpar.plan._normalized", forbidden)
    monkeypatch.setattr("hmcpctl.operations.lpar.plan.plan_digest", forbidden)
    with pytest.raises(ValueError, match="uncapped_weight.*only.*update"):
        check_request(_request(resources=LparResources(uncapped_weight=weight)))
