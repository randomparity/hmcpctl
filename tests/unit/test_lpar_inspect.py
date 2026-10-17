"""The read-only partition inspection behind hmc_inspect_lpar (#1224, ADR 0200)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from hmcpctl.errors import HMCError, HMCTransportError
from hmcpctl.operations.lpar import inspect as inspect_module
from hmcpctl.operations.lpar.inspect import (
    MAX_REFCODES,
    MAX_VIOS,
    Rmc,
    Section,
    SectionSource,
    inspect_lpar,
    next_actions,
)

SYS = "0000000a-0000-4000-8000-000000000000"
LPAR = "0000000a-0000-4000-8000-0000000000aa"
VIOS_1 = "0000000a-0000-4000-9000-000000000001"
VIOS_2 = "0000000a-0000-4000-9000-000000000002"


def _partition(**resource: Any) -> dict[str, Any]:
    base = {
        "PartitionName": "web1",
        "PartitionID": "7",
        "PartitionState": "running",
        "ResourceMonitoringControlState": "active",
        "PartitionMemoryConfiguration": {
            "CurrentMemory": "4096",
            "DesiredMemory": "8192",
        },
        "PartitionProcessorConfiguration": {
            "HasDedicatedProcessors": "false",
            "SharedProcessorConfiguration": {
                "DesiredProcessingUnits": "0.5",
                "DesiredVirtualProcessors": "2",
            },
            "CurrentSharedProcessorConfiguration": {"CurrentProcessingUnits": "0.5"},
        },
    }
    return {"UUID": LPAR.upper(), "Resource": {**base, **resource}}


def _mapping(partition_id: str) -> dict[str, Any]:
    return {
        "UUID": f"map-{partition_id}",
        "ServerAdapter": {"RemoteLogicalPartitionID": partition_id},
        "Storage": {"VirtualDisk": {"DiskName": f"disk{partition_id}"}},
    }


def _detail(*mappings: dict[str, Any]) -> dict[str, Any]:
    return {
        "Resource": {
            "VirtualSCSIMappings": {"VirtualSCSIMapping": list(mappings)},
        }
    }


class _HMC:
    def __init__(self, partitions: list[dict[str, Any]] | None = None) -> None:
        self.config = object()
        self.partitions = [_partition()] if partitions is None else partitions
        self.vios: list[dict[str, Any]] = [{"UUID": VIOS_1}, {"UUID": VIOS_2}]
        self.vios_error: Exception | None = None
        self.details: dict[str, Any] = {
            VIOS_1: _detail(_mapping("7"), _mapping("8")),
            VIOS_2: _detail(),
        }
        self.calls: list[str] = []

    async def list_logical_partitions(self, uuid: str) -> list[dict[str, Any]]:
        self.calls.append(f"list_logical_partitions {uuid}")
        return self.partitions

    async def list_vios(self, uuid: str) -> list[dict[str, Any]]:
        self.calls.append(f"list_vios {uuid}")
        if self.vios_error is not None:
            raise self.vios_error
        return self.vios

    async def get_vios_storage_detail(self, uuid: str) -> dict[str, Any] | None:
        self.calls.append(f"get_vios_storage_detail {uuid}")
        detail = self.details.get(uuid)
        if isinstance(detail, Exception):
            raise detail
        return detail


@pytest.fixture(autouse=True)
def _seams(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Resolve the system without the HMC and read refcodes without SSH."""
    reads: list[tuple] = []

    async def resolve_system(_hmc: Any, selector: str) -> str:
        if selector not in ("sysA", SYS):
            raise ValueError(f"No managed system named {selector!r}")
        return SYS.upper()

    async def names(_config: Any, system: str, lpar: str) -> tuple[str, str]:
        return "sysA", lpar

    async def refcodes(_config: Any, system: str, lpar: str, count: int) -> list:
        reads.append((system, lpar, count))
        return [{"lpar_name": lpar, "refcode": f"CA00E1{i:02d}"} for i in range(30)]

    monkeypatch.setattr(inspect_module, "resolve_system_uuid", resolve_system)
    monkeypatch.setattr(inspect_module, "resolve_ssh_names", names)
    monkeypatch.setattr(inspect_module, "list_lpar_refcodes", refcodes)
    return reads


def _admit_all(_tool: str, _targets: Mapping[str, str | None]) -> str | None:
    return None


def _deny(*tools: str, vios: str | None = None):
    def admit(tool: str, targets: Mapping[str, str | None]) -> str | None:
        if tool in tools or (vios is not None and targets.get("vios") == vios):
            return f"{tool} denied"
        return None

    return admit


def _inspect(
    hmc: _HMC, include: tuple[Section, ...], admit=_admit_all, lpar: str = "web1"
):
    return asyncio.run(
        inspect_lpar(
            hmc,
            connection="<default>",
            admit=admit,
            system="sysA",
            lpar=lpar,
            include=include,
        )
    )


def test_base_read_reports_identity_and_state():
    result = _inspect(_HMC(), ())
    assert result.id == f"<default>/{SYS}/{LPAR}"
    assert (result.uuid, result.name, result.partition_id) == (LPAR, "web1", 7)
    assert result.state == "running"
    assert (result.rmc, result.resources, result.refcodes) == (None, None, None)
    assert result.profile_drift is None


@pytest.mark.parametrize("selector", [LPAR, LPAR.upper(), "web1"])
def test_selector_matches_uuid_or_name(selector: str):
    assert _inspect(_HMC(), (), lpar=selector).uuid == LPAR


@pytest.mark.parametrize(
    ("partitions", "found"),
    [([], "No LPAR named"), ([_partition(), _partition()], "Ambiguous LPAR name")],
)
def test_selector_must_match_one_partition(partitions: list, found: str):
    with pytest.raises(ValueError, match=found):
        _inspect(_HMC(partitions), ())


def test_rmc_comes_from_the_base_read():
    result = _inspect(
        _HMC([_partition(ResourceMonitoringControlState="inactive")]), ("rmc",)
    )
    assert result.rmc == Rmc(SectionSource("ok", "hmc_get_lpar"), "inactive")


def test_missing_rmc_state_is_unavailable():
    partition = _partition()
    del partition["Resource"]["ResourceMonitoringControlState"]
    result = _inspect(_HMC([partition]), ("rmc",))
    assert result.rmc is not None
    assert result.rmc.source.status == "unavailable"
    hmc = _HMC([_partition(ResourceMonitoringControlState=None)])
    result = _inspect(hmc, ("rmc",))
    assert result.rmc is not None
    assert result.rmc.source.status == "unavailable"
    assert result.rmc.state is None


def test_refcodes_are_capped_and_newest_first(_seams: list[tuple]):
    result = _inspect(_HMC(), ("refcodes",))
    assert result.refcodes is not None
    assert result.refcodes.source.status == "ok"
    assert len(result.refcodes.codes) == MAX_REFCODES
    assert result.refcodes.codes[0]["refcode"] == "CA00E100"
    assert _seams == [("sysA", "web1", MAX_REFCODES)]


def test_denied_refcodes_read_nothing(_seams: list[tuple]):
    result = _inspect(_HMC(), ("refcodes",), _deny("hmc_read_lpar_refcodes"))
    assert result.refcodes is not None
    assert result.refcodes.source.status == "denied"
    assert result.refcodes.codes == []
    assert _seams == []


def test_failed_refcodes_are_unavailable(monkeypatch: pytest.MonkeyPatch):
    async def fail(*_args: Any) -> list:
        raise HMCError("ssh connection failed " + "x" * 900)

    monkeypatch.setattr(inspect_module, "list_lpar_refcodes", fail)
    result = _inspect(_HMC(), ("refcodes", "rmc"))
    assert result.refcodes is not None
    assert result.refcodes.source.status == "unavailable"
    assert len(result.refcodes.source.detail or "") == 500
    assert result.rmc is not None and result.rmc.source.status == "ok"


def test_resources_report_figures_and_this_partitions_mappings():
    result = _inspect(_HMC(), ("resources",))
    resources = result.resources
    assert resources is not None
    assert resources.storage_source.status == "ok"
    assert (resources.current_memory_mib, resources.desired_memory_mib) == (4096, 8192)
    assert (resources.current_proc_units, resources.desired_vcpus) == (0.5, 2)
    assert resources.dedicated_procs is False
    assert [(v.uuid, v.status) for v in resources.vios] == [
        (VIOS_1, "ok"),
        (VIOS_2, "ok"),
    ]
    assert [m["uuid"] for m in resources.mappings] == ["map-7"]


def test_denied_vios_list_reads_no_vios():
    hmc = _HMC()
    result = _inspect(hmc, ("resources",), _deny("hmc_list_vios"))
    assert result.resources is not None
    assert result.resources.storage_source.status == "denied"
    assert (result.resources.vios, result.resources.mappings) == ([], [])
    assert result.resources.current_memory_mib == 4096
    assert not any(call.startswith(("list_vios", "get_vios")) for call in hmc.calls)


def test_one_denied_vios_marks_resources_denied_and_skips_it():
    hmc = _HMC()
    result = _inspect(hmc, ("resources",), _deny(vios=VIOS_2))
    assert result.resources is not None
    assert result.resources.storage_source.status == "denied"
    assert [(v.uuid, v.status) for v in result.resources.vios] == [
        (VIOS_1, "ok"),
        (VIOS_2, "denied"),
    ]
    assert f"get_vios_storage_detail {VIOS_2}" not in hmc.calls
    assert [m["uuid"] for m in result.resources.mappings] == ["map-7"]


@pytest.mark.parametrize("failure", [HMCError("boom"), None])
def test_unread_vios_marks_resources_unavailable(failure: Exception | None):
    hmc = _HMC()
    hmc.details[VIOS_2] = failure
    result = _inspect(hmc, ("resources",))
    assert result.resources is not None
    assert result.resources.storage_source.status == "unavailable"
    assert result.resources.vios[1].status == "unavailable"


def test_vios_without_uuid_is_unavailable_and_not_admitted():
    hmc = _HMC()
    hmc.vios = [{"Resource": {"PartitionName": "vios0"}}, {"UUID": VIOS_1}]
    asked: list = []

    def admit(tool: str, targets: Mapping[str, str | None]) -> str | None:
        asked.append((tool, targets.get("vios")))
        return None

    result = _inspect(hmc, ("resources",), admit)
    assert result.resources is not None
    assert [(v.uuid, v.status) for v in result.resources.vios] == [
        (None, "unavailable"),
        (VIOS_1, "ok"),
    ]
    assert ("hmc_get_vios_storage_detail", None) not in asked
    assert result.resources.storage_source.status == "unavailable"


def test_transport_failure_stops_the_vios_reads():
    hmc = _HMC()
    hmc.details[VIOS_1] = HMCTransportError("timed out")
    result = _inspect(hmc, ("resources",))
    assert result.resources is not None
    assert [(v.status, v.detail) for v in result.resources.vios] == [
        ("unavailable", "the HMC stopped answering"),
        ("unavailable", "the HMC stopped answering"),
    ]
    assert f"get_vios_storage_detail {VIOS_2}" not in hmc.calls


def test_failed_vios_list_is_unavailable():
    hmc = _HMC()

    hmc.vios_error = HMCError("feed refused")
    result = _inspect(hmc, ("resources",))
    assert result.resources is not None
    assert result.resources.storage_source == SectionSource(
        "unavailable", "hmc_list_vios", "feed refused"
    )


def test_more_vioses_than_the_bound_are_not_read():
    hmc = _HMC()
    hmc.vios = [{"UUID": f"0000000a-0000-4000-9000-{i:012d}"} for i in range(20)]
    result = _inspect(hmc, ("resources",))
    assert result.resources is not None
    assert len(result.resources.vios) == MAX_VIOS
    assert result.resources.storage_source.status == "unavailable"


def test_profile_drift_is_unavailable_without_a_read():
    hmc = _HMC()
    result = _inspect(hmc, ("profile_drift",))
    assert result.profile_drift is not None
    assert result.profile_drift.status == "unavailable"
    assert hmc.calls == [f"list_logical_partitions {SYS.upper()}"]


_ACTIVE = Rmc(SectionSource("ok", "hmc_get_lpar"), "active")
_INACTIVE = Rmc(SectionSource("ok", "hmc_get_lpar"), "inactive")


@pytest.mark.parametrize(
    ("state", "rmc", "expected"),
    [
        ("not activated", None, ["hmc_power_lpar"]),
        ("error", None, ["hmc_capture_lpar_console"]),
        ("open firmware", _ACTIVE, ["hmc_capture_lpar_console"]),
        ("starting", None, ["hmc_inspect_lpar"]),
        ("migrating running", None, ["hmc_inspect_lpar"]),
        ("running", _INACTIVE, ["hmc_capture_lpar_console"]),
        ("running", _ACTIVE, []),
        ("running", None, []),
        ("Unknown", None, []),
        (None, None, []),
    ],
)
def test_next_actions_follow_state_and_rmc(state, rmc, expected):
    assert next_actions(state, rmc) == expected


def test_inspection_preserves_enrichment_from_real_reader(monkeypatch):
    from hmcpctl.ssh import refcodes

    async def resolve(*_args):
        return "system", "web1"

    async def transport(*_args):
        return (
            "lpar_name,time_stamp,refcode\n"
            "web1,now,CA00E105 LP=00002\nweb1,earlier,B2001230\n"
        )

    monkeypatch.setattr(inspect_module, "resolve_ssh_names", resolve)
    monkeypatch.setattr(refcodes, "run_hmc_command", transport)
    monkeypatch.setattr(
        inspect_module, "list_lpar_refcodes", refcodes.list_lpar_refcodes
    )
    result = _inspect(_HMC(), ("refcodes",))
    assert result.refcodes.source.status == "ok"
    assert "operating system" in result.refcodes.codes[0]["definition"].lower()
    assert result.refcodes.codes[0]["refcode"] == "CA00E105 LP=00002"
    assert result.refcodes.codes[1]["definition"] is None
