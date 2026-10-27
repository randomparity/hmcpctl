"""Synthetic schema-shaped C07 evidence exercises the real creation owners.

Memory leaves follow retained profile/current schemas (8b14f694 / 3043acbf),
pool links follow 2f95cc34, and BasePartition e3fa8fb8 owns memory nesting.
These fixtures establish configured facts only; no live AMS observation is implied.
"""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.sax.saxutils import escape, quoteattr

import pytest

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_creation_memory import LparCreationMemory as Memory
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import core, linux_roles, serial, workflows
from hmcpctl.operations.lpar import creation_memory as cm
from hmcpctl.operations.lpar.assignments import (
    DedicatedPcieAssignment,
    LparPcieAssignments,
)
from hmcpctl.operations.lpar.profiles import PartitionProfileRecord
from hmcpctl.ssh import lpar as native
from hmcpctl.ssh.lpar_serial import SerialProfile
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import ATOM_NS

pytestmark = pytest.mark.asyncio

SYSTEM = "11111111-1111-4111-8111-111111111111"
LOCAL = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PRIMARY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SECONDARY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
POOL = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
PRIVATE = "private-provider-diagnostic"
RESOURCES = LparResources(desired_memory=16384, desired_procs=0.5)
SHARED = Memory(
    mem_mode="shared",
    desired_io_entitled_mem="auto",
    mem_weight=0,
    primary_paging_vios_name="paging-a",
    secondary_paging_vios_id="none",
)


def resource(uuid, name, native_id, kind="VirtualIOServer", environment=None):
    return {
        "UUID": uuid,
        "ResourceType": kind,
        "Resource": {
            "PartitionName": name,
            "PartitionID": str(native_id),
            "PartitionType": environment
            or ("Virtual IO Server" if kind == "VirtualIOServer" else "AIX/Linux"),
            "PartitionState": "not activated",
            "Description": "",
            "DefaultProfileName": "default_profile",
            "PartitionIOConfiguration": {"MaximumVirtualIOSlots": "8"},
            "PartitionMemoryConfiguration": {"SharedMemoryEnabled": "false"},
        },
    }


def xml_fields(fields):
    return "".join(
        f"<{k}>"
        + (xml_fields(v) if isinstance(v, dict) and k != "link" else escape(str(v)))
        + f"</{k}>"
        if k != "link"
        else "<link "
        + " ".join(f"{a}={quoteattr(str(b))}" for a, b in v.items())
        + "/>"
        for k, v in fields.items()
    )


def xml_entry(entry):
    return (
        f'<entry xmlns="{ATOM_NS}"><id>urn:uuid:{entry["UUID"]}</id><content>'
        f'<{entry["ResourceType"]} xmlns="{UOM_NS}">{xml_fields(entry["Resource"])}</{entry["ResourceType"]}>'
        "</content></entry>"
    )


def feed(rows, continuation=""):
    return (
        f'<feed xmlns="{ATOM_NS}">'
        + "".join(xml_entry(r) for r in rows)
        + continuation
        + "</feed>"
    )


def link(uuid, host="hmc.test", path=None):
    return {
        "link": {
            "href": f"https://{host}"
            + (path or f"/rest/api/uom/VirtualIOServer/{uuid}")
        }
    }


@pytest.fixture
def memory_target(monkeypatch):
    local = resource(LOCAL, "new", 9, "LogicalPartition")
    primary, secondary = (
        resource(PRIMARY, "paging-a", 7),
        resource(SECONDARY, "paging-b", 8),
    )
    system = {
        "UUID": SYSTEM,
        "ResourceType": "ManagedSystem",
        "Resource": {
            "SystemName": "system",
            "IsClassicHMCManagement": "true",
            "IsHMCPowerVMManagementController": "false",
            "IsPowerVMManagementController": "false",
            "AssociatedSystemMemoryConfiguration": {"ConfigurableSystemMemory": "8192"},
            "AssociatedSystemCapabilities": {
                "ActiveMemorySharingCapable": "true",
                "ActiveMemoryExpansionCapable": "true",
            },
        },
    }
    pool = {
        "UUID": POOL,
        "ResourceType": "SharedMemoryPool",
        "Resource": {
            "PagingServicePartitionOne": link(PRIMARY),
            "PagingServicePartitionTwo": link(SECONDARY),
        },
    }
    saved = {
        "ActiveMemorySharingEnabled": "true",
        "ManualEntitledModeEnabled": "false",
        "MemoryWeight": "0",
        "PrimaryPagingServicePartition": link(PRIMARY),
        "SecondaryPagingServicePartition": "",
        "ActiveMemoryExpansionEnabled": "false",
    }
    current = {
        "SharedMemoryEnabled": "true",
        "AutoEntitledMemoryEnabled": "true",
        "CurrentMemoryWeight": "0",
        "MemoryExpansionEnabled": "false",
    }
    b = SimpleNamespace(
        local=local,
        providers=[primary, secondary],
        system=system,
        pools=[pool],
        before=[],
        saved=saved,
        current=current,
        created=False,
        events=[],
        fault=None,
        raw_fault=None,
        controller=False,
        stamp=True,
    )
    hmc = AsyncMock(spec=HMCClient)
    hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "test", "agent_id": "agent"}
    )
    b.hmc = hmc
    hmc.get_managed_system.side_effect = lambda uuid: deepcopy(system)
    hmc.list_system_summaries.return_value = [
        {"UUID": SYSTEM, "SystemName": "system", "State": "operating"}
    ]

    def rows(kind):
        result = [p for p in b.before + b.providers if p["ResourceType"] == kind]
        if b.created and b.local["ResourceType"] == kind:
            result.append(b.local)
        return deepcopy(result)

    hmc.list_logical_partitions.side_effect = lambda system: rows("LogicalPartition")
    hmc.list_vios.side_effect = lambda system: rows("VirtualIOServer")

    async def raw(
        path,
        resource_type=None,
        include_schema_version=True,
        *,
        uuid_path_arguments=None,
    ):
        assert path.startswith("/rest/api/uom/")
        assert uuid_path_arguments and all(
            v in path for v in uuid_path_arguments.values()
        )
        b.events.append("read:" + path)
        if b.raw_fault and path.endswith(b.raw_fault[0]):
            return b.raw_fault[1]
        end = path.rsplit("/", 1)[-1]
        if end in {"LogicalPartition", "VirtualIOServer"}:
            return feed(rows(end))
        if end == "SharedMemoryPool":
            return feed(b.pools)
        if end == SYSTEM:
            return xml_entry(b.system)
        matches = [e for e in b.providers + [b.local] if e["UUID"] == end]
        assert len(matches) == 1
        result = deepcopy(matches[0])
        if end == LOCAL and b.created:
            result["Resource"]["PartitionMemoryConfiguration"] = deepcopy(b.current)
        return xml_entry(result)

    hmc._get.side_effect = raw
    hmc.get_uom.side_effect = lambda kind, uuid: deepcopy(
        next(e for e in b.providers + [b.local] if e["UUID"] == uuid)
    )
    hmc.get_logical_partition.side_effect = lambda uuid: deepcopy(b.local)

    async def find(name, **kwargs):
        if b.created and b.fault == "lookup":
            raise RuntimeError(PRIVATE)
        return deepcopy(b.local) if b.created else None

    hmc.find_partition_by_name.side_effect = find
    hmc.find_vios_by_name.side_effect = find

    async def command(config, cmd):
        if cmd.startswith("lssyscfg -r sys"):
            return f"{SYSTEM},system"
        if cmd.startswith("lscomgmt"):
            return "1" if b.controller else "null"
        if cmd.startswith("mksyscfg"):
            b.events.append("create")
            b.created = b.fault != "refusal"
            if b.fault in {"timeout", "refusal"}:
                raise HMCCLIError(
                    PRIVATE,
                    remote_refusal_code="HSCL07C1" if b.fault == "refusal" else None,
                )
            return ""
        raise AssertionError(cmd)

    monkeypatch.setattr(native, "run_hmc_command", command)
    monkeypatch.setattr(linux_roles, "run_hmc_command", command)

    async def stamp(*args, **kwargs):
        b.events.append("stamp")
        return b.stamp, ()

    async def apply(*args, **kwargs):
        b.events.append("apply")
        if b.fault == "apply":
            raise HMCCLIError(PRIVATE)
        return ""

    monkeypatch.setattr(core, "stamp_created_lpar_ownership", stamp)
    monkeypatch.setattr(core, "apply_lpar_profile_via_cli", apply)

    async def profile(*args, **kwargs):
        b.events.append("saved")
        if b.fault == "saved":
            raise HMCError(PRIVATE)
        return PartitionProfileRecord(
            POOL,
            "default_profile",
            "setting",
            None,
            {"ProfileMemory": deepcopy(b.saved)},
        )

    monkeypatch.setattr(cm.profiles, "read_partition_profile", profile)
    monkeypatch.setattr(
        serial,
        "read_serial_profile",
        AsyncMock(
            return_value=SerialProfile(
                "new",
                9,
                "default_profile",
                8,
                {2: ("2", "server", "0", "", "", "", "0")},
                False,
            )
        ),
    )

    async def assignments(*args):
        b.events.append("assign")
        return SimpleNamespace(workflow_completed=True, steps=())

    monkeypatch.setattr(workflows, "apply_validated_lpar_pcie_assignments", assignments)
    return b


async def admit(b, memory=SHARED):
    return await cm.admit_creation_memory(
        b.hmc,
        SYSTEM,
        "system",
        memory,
        native.complete_create_resources(RESOURCES),
        kind=b.local["ResourceType"],
        environment=b.local["Resource"]["PartitionType"],
        name="new",
        partition_id=9,
    )


async def create(b, memory=SHARED, *, resources=RESOURCES, **kwargs):
    creation = core.LparCreation(
        "new",
        b.local["Resource"]["PartitionType"],
        resources,
        partition_id=9,
        creation_memory=memory,
        **kwargs,
    )
    return await core.create_and_stamp_lpar(b.hmc, SYSTEM, creation)


@pytest.mark.parametrize(
    "value", [None, "false", "1.0", [], {"text": "true", "other": "x"}]
)
@pytest.mark.parametrize(
    "capability,memory",
    [
        ("ActiveMemorySharingCapable", SHARED),
        ("ActiveMemoryExpansionCapable", Memory(mem_expansion=1.1)),
    ],
)
async def test_capability_requires_positive_evidence(
    memory_target, value, capability, memory
):
    b = memory_target
    caps = b.system["Resource"]["AssociatedSystemCapabilities"]
    if value is None:
        del caps[capability]
    else:
        caps[capability] = value
    with pytest.raises((ValueError, TypeError)):
        await create(b, memory, resources=LparResources(desired_memory=1024))
    assert not b.created and "stamp" not in b.events and "apply" not in b.events


@pytest.mark.parametrize(
    "fault",
    [
        "missing-pool",
        "duplicate-pool",
        "duplicate-uuid",
        "duplicate-name",
        "duplicate-id",
        "unknown-primary",
        "wrong-provider-kind",
        "shared-provider",
        "absent-provider-memory",
        "incomplete",
        "oversized",
        "non-atom",
        "duplicate-detail",
    ],
)
async def test_incomplete_topology_refuses_before_effects(memory_target, fault):
    b = memory_target
    if fault == "missing-pool":
        b.pools = []
    elif fault == "duplicate-pool":
        b.pools *= 2
    elif fault == "duplicate-uuid":
        b.providers.append(deepcopy(b.providers[0]))
    elif fault == "duplicate-name":
        b.providers[1]["Resource"]["PartitionName"] = "paging-a"
    elif fault == "duplicate-id":
        b.providers[1]["Resource"]["PartitionID"] = "7"
    elif fault == "unknown-primary":
        b.pools[0]["Resource"]["PagingServicePartitionOne"] = link(LOCAL)
    elif fault == "wrong-provider-kind":
        b.providers[0]["ResourceType"] = "LogicalPartition"
    elif fault == "shared-provider":
        b.providers[0]["Resource"]["PartitionMemoryConfiguration"][
            "SharedMemoryEnabled"
        ] = "true"
    elif fault == "absent-provider-memory":
        del b.providers[0]["Resource"]["PartitionMemoryConfiguration"]
    elif fault == "incomplete":
        b.raw_fault = (
            "ManagedSystem/" + SYSTEM + "/VirtualIOServer",
            feed(b.providers, '<link rel="next" href="https://hmc.test/next"/>'),
        )
    elif fault == "oversized":
        b.raw_fault = (
            "ManagedSystem/" + SYSTEM + "/VirtualIOServer",
            " " * (5 * 1024 * 1024 + 1),
        )
    elif fault == "non-atom":
        b.raw_fault = ("ManagedSystem/" + SYSTEM + "/VirtualIOServer", "<feed/>")
    elif fault == "duplicate-detail":
        b.raw_fault = (
            "VirtualIOServer/" + PRIMARY,
            feed([b.providers[0], b.providers[0]]),
        )
    with pytest.raises((ValueError, TypeError, HMCError)):
        await create(b)
    assert not b.created and "stamp" not in b.events and "apply" not in b.events


@pytest.mark.parametrize(
    "href",
    [
        f"http://hmc.test/rest/api/uom/VirtualIOServer/{PRIMARY}",
        f"https://foreign.test/rest/api/uom/VirtualIOServer/{PRIMARY}",
        f"https://hmc.test:12443/rest/api/uom/VirtualIOServer/{PRIMARY}",
        f"https://user@hmc.test/rest/api/uom/VirtualIOServer/{PRIMARY}",
        f"https://hmc.test/rest/api/uom/VirtualIOServer/{PRIMARY}?x=1",
        f"https://hmc.test/rest/api/uom/LogicalPartition/{PRIMARY}",
        f"https://hmc.test/rest/api/uom/ManagedSystem/{LOCAL}/VirtualIOServer/{PRIMARY}",
    ],
)
async def test_provider_origin_check(memory_target, href):
    b = memory_target
    b.pools[0]["Resource"]["PagingServicePartitionOne"] = {"link": {"href": href}}
    with pytest.raises(ValueError):
        await create(b)
    assert not b.created


@pytest.mark.parametrize("explicit", [True, False])
async def test_provider_ownership_check(memory_target, explicit):
    b = memory_target
    b.providers[0]["Resource"]["Description"] = (
        "[hmcpctl owner:foreign created:2026-10-27]"
    )
    memory = SHARED if explicit else Memory(mem_mode="shared")
    with pytest.raises(PermissionError):
        await create(b, memory)
    assert not b.created


async def test_selectors_narrow_authorized_candidates_and_reject_alias(memory_target):
    b = memory_target
    b.providers[1]["Resource"]["Description"] = (
        "[hmcpctl owner:foreign created:2026-10-27]"
    )
    a = await admit(b)
    assert [p.uuid for p in a.primary] == [PRIMARY] and a.secondary == ()
    with pytest.raises(ValueError):
        await admit(
            b,
            Memory(
                mem_mode="shared",
                primary_paging_vios_name="paging-a",
                secondary_paging_vios_id=7,
            ),
        )
    with pytest.raises(PermissionError):
        await admit(b, Memory(mem_mode="shared"))


@pytest.mark.parametrize("memory", [Memory(mem_mode="ded"), Memory(mem_expansion=0)])
async def test_dedicated_requests_retain_physical_bounds(memory_target, memory):
    with pytest.raises(ValueError, match="memory|Memory"):
        await create(memory_target, memory)
    assert not memory_target.created


@pytest.mark.parametrize(
    "resources",
    [
        LparResources(),
        LparResources(desired_memory=1024, dedicated=True, desired_procs=1),
    ],
)
async def test_shared_unknown_or_dedicated_cpu_refuses_before_network(
    memory_target, resources
):
    b = memory_target
    with pytest.raises(ValueError):
        await core.create_and_stamp_lpar(
            b.hmc,
            SYSTEM,
            core.LparCreation("new", "AIX/Linux", resources, creation_memory=SHARED),
        )
    assert not b.hmc._get.called and not b.hmc.find_partition_by_name.called


async def test_shared_direct_assignment_refuses_before_network(memory_target):
    b = memory_target
    with pytest.raises(ValueError):
        await workflows.create_lpar(
            b.hmc,
            SYSTEM,
            core.LparCreation("new", "AIX/Linux", RESOURCES, creation_memory=SHARED),
            LparPcieAssignments(
                dedicated=(DedicatedPcieAssignment("default_profile", "1234"),)
            ),
        )
    assert not b.hmc.find_partition_by_name.called and not b.created


async def test_shared_creation_saved_then_current_only_after_apply(memory_target):
    b = memory_target
    result = await create(b, apply_profile=True)
    assert (
        result.ownership_stamped is True and result.creation_memory_step.status == "ok"
    )
    proof = result.creation_memory_step.result
    assert proof.saved.status == proof.current.status == "verified"
    assert proof.saved.observed["primary_paging_vios_name"] == {
        "uuid": PRIMARY,
        "name": "paging-a",
        "partition_id": 7,
    }
    assert proof.saved.observed["secondary_paging_vios_id"] == "none"
    assert all("paging" not in k for k in proof.current.observed)
    assert b.events.count("create") == b.events.count("apply") == 1
    assert b.events.index("saved") < b.events.index("apply")


async def test_no_apply_has_saved_only_and_no_runtime_claim(memory_target):
    b = memory_target
    result = await create(b)
    proof = result.creation_memory_step.result
    assert proof.saved.status == "verified" and proof.current is None
    assert "apply" not in b.events


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("MemoryWeight", "1", "mismatch"),
        ("MemoryWeight", None, "unavailable"),
        ("ManualEntitledModeEnabled", "true", "mismatch"),
        ("PrimaryPagingServicePartition", None, "unavailable"),
        ("SecondaryPagingServicePartition", None, "unavailable"),
        ("ActiveMemorySharingEnabled", "false", "mismatch"),
    ],
)
async def test_saved_observation_status_preserves_partial_facts(
    memory_target, field, value, expected
):
    b = memory_target
    if value is None:
        del b.saved[field]
    else:
        b.saved[field] = value
    result = await create(b, apply_profile=True)
    proof = result.creation_memory_step.result
    assert (
        result.creation_memory_step.status == "error" and proof.saved.status == expected
    )
    assert proof.saved.observed
    assert proof.current is None and "apply" not in b.events
    assert PRIVATE not in repr(result)


async def test_known_mismatch_precedes_unavailable(memory_target):
    b = memory_target
    b.saved["MemoryWeight"] = "1"
    del b.saved["PrimaryPagingServicePartition"]
    result = await create(b)
    assert result.creation_memory_step.result.saved.status == "mismatch"
    assert (
        "primary_paging_vios_name"
        in result.creation_memory_step.result.saved.unavailable
    )


@pytest.mark.parametrize("mode", ["missing", "explicit-empty", "wrong"])
async def test_omitted_secondary_requires_evidence_and_authorized_identity(
    memory_target, mode
):
    b = memory_target
    if mode == "missing":
        del b.saved["SecondaryPagingServicePartition"]
    elif mode == "wrong":
        b.saved["SecondaryPagingServicePartition"] = link(PRIMARY)
    result = await create(b, Memory(mem_mode="shared"), apply_profile=True)
    assert result.creation_memory_step.status == (
        "ok" if mode == "explicit-empty" else "error"
    )


@pytest.mark.parametrize("environment", ["AIX/Linux", "OS400"])
@pytest.mark.parametrize("factor", [0, Decimal("1.10")])
async def test_ame_saved_contract_in_both_lpar_environments(
    memory_target, environment, factor
):
    b = memory_target
    b.local["Resource"]["PartitionType"] = environment
    b.saved.update(
        ActiveMemorySharingEnabled="false",
        ActiveMemoryExpansionEnabled="true" if factor else "false",
        ExpansionFactor="1.1" if factor else "stale-unused",
    )
    result = await core.create_and_stamp_lpar(
        b.hmc,
        SYSTEM,
        core.LparCreation(
            "new",
            environment,
            LparResources(desired_memory=1024),
            creation_memory=Memory(mem_mode="ded", mem_expansion=factor),
        ),
    )
    assert result.creation_memory_step.status == "ok"
    assert result.creation_memory_step.result.saved.observed["mem_expansion"] == factor
    assert result.creation_memory_step.result.current is None


async def test_controller_shared_designations_are_unavailable(memory_target):
    b = memory_target
    b.controller = True
    b.system["Resource"]["IsClassicHMCManagement"] = "false"
    b.system["Resource"]["IsHMCPowerVMManagementController"] = "true"
    result = await create(b)
    proof = result.creation_memory_step.result
    assert result.creation_memory_step.status == "error" and proof.saved is None
    assert proof.current.observed["mem_weight"] == 0
    assert "primary_paging_vios_name" in proof.current.unavailable
    assert "saved" not in b.events


@pytest.mark.parametrize(
    "fault,classification",
    [("timeout", "partial"), ("refusal", "rejected"), ("lookup", "unknown")],
)
async def test_replay_barrier(memory_target, fault, classification):
    b = memory_target
    b.fault = "timeout" if fault == "lookup" else fault
    if fault == "lookup":
        b.raw_fault = ("ManagedSystem/" + SYSTEM + "/LogicalPartition", "<broken")
        # Admission must succeed first; fail only reconciliation's fresh feed.
        original = b.hmc._get.side_effect

        async def read(*args, **kwargs):
            if not b.created:
                old = b.raw_fault
                b.raw_fault = None
                try:
                    return await original(*args, **kwargs)
                finally:
                    b.raw_fault = old
            return await original(*args, **kwargs)

        b.hmc._get.side_effect = read
    with pytest.raises(HMCError, match=classification) as error:
        await create(b, apply_profile=True)
    assert PRIVATE not in str(error.value) and error.value.__cause__ is None
    assert (
        b.events.count("create") == 1
        and "stamp" not in b.events
        and "apply" not in b.events
    )
    if fault == "refusal":
        assert "HSCL07C1" in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionType", "Virtual IO Server"),
        ("PartitionState", "running"),
        ("PartitionName", "other"),
        ("PartitionID", "10"),
    ],
)
async def test_wrong_created_identity_never_stamps(memory_target, field, value):
    b = memory_target
    original = b.hmc.find_partition_by_name.side_effect

    async def find(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result:
            result["Resource"][field] = value
        return result

    b.hmc.find_partition_by_name.side_effect = find
    result = await create(b, apply_profile=True)
    assert result.lpar is None and "stamp" not in b.events and "apply" not in b.events
    assert result.creation_memory_step.status == "error"


async def test_stamp_failure_stops_apply_and_assignments(memory_target):
    b = memory_target
    b.stamp = False
    result = await workflows.create_lpar(
        b.hmc,
        SYSTEM,
        core.LparCreation(
            "new", "AIX/Linux", RESOURCES, creation_memory=SHARED, apply_profile=True
        ),
        LparPcieAssignments(),
    )
    assert (
        not result.workflow_completed
        and "apply" not in b.events
        and "assign" not in b.events
    )


async def test_current_mismatch_preserves_saved_and_stops_assignments(memory_target):
    b = memory_target
    b.current["CurrentMemoryWeight"] = "1"
    result = await workflows.create_lpar(
        b.hmc,
        SYSTEM,
        core.LparCreation(
            "new", "AIX/Linux", RESOURCES, creation_memory=SHARED, apply_profile=True
        ),
        LparPcieAssignments(),
    )
    proof = next(s.result for s in result.steps if s.step == "creation_memory")
    assert proof.saved.status == "verified" and proof.current.status == "mismatch"
    assert "apply" in b.events and "assign" not in b.events


@pytest.mark.parametrize("missing", ["memory", "serial"])
async def test_c11_and_c07_both_gate_apply(memory_target, missing):
    b = memory_target
    if missing == "memory":
        del b.saved["MemoryWeight"]
    else:
        serial.read_serial_profile.side_effect = ValueError(PRIVATE)
    result = await create(
        b,
        max_virtual_slots=8,
        virtual_serial_adapters=(VirtualSerialAdapter(2, "server", False),),
        apply_profile=True,
    )
    assert (
        result.creation_memory_step if missing == "memory" else result.serial_step
    ).status == "error"
    assert "apply" not in b.events


async def test_c11_and_c07_success_compose(memory_target):
    result = await create(
        memory_target,
        max_virtual_slots=8,
        virtual_serial_adapters=(VirtualSerialAdapter(2, "server", False),),
        apply_profile=True,
    )
    assert (
        result.creation_memory_step.status
        == result.serial_step.status
        == result.apply_step.status
        == "ok"
    )


@pytest.mark.parametrize("missing", [False, True])
async def test_ame_positive_capability_is_required_on_valid_capacity(
    memory_target, missing
):
    b = memory_target
    b.saved.update(
        ActiveMemorySharingEnabled="false",
        ActiveMemoryExpansionEnabled="true",
        ExpansionFactor="1.10",
    )
    if missing:
        del b.system["Resource"]["AssociatedSystemCapabilities"][
            "ActiveMemoryExpansionCapable"
        ]
        with pytest.raises((TypeError, ValueError)):
            await create(
                b,
                Memory(mem_expansion=Decimal("1.1")),
                resources=LparResources(desired_memory=1024),
            )
        assert not b.created
    else:
        result = await create(
            b,
            Memory(mem_expansion=Decimal("1.1")),
            resources=LparResources(desired_memory=1024),
        )
        assert result.creation_memory_step.status == "ok"


async def test_ame_zero_does_not_need_capability(memory_target):
    b = memory_target
    del b.system["Resource"]["AssociatedSystemCapabilities"]
    b.saved["ExpansionFactor"] = "unused-stale"
    result = await create(
        b, Memory(mem_expansion=0), resources=LparResources(desired_memory=1024)
    )
    assert result.creation_memory_step.status == "ok"


async def test_ame_selected_linux_intent_does_not_infer_runtime_prohibition(
    memory_target, monkeypatch
):
    from hmcpctl.documents.lpar import LparSettings

    b = memory_target
    settings = LparSettings(kvm_capable=False)
    b.system["Resource"]["AssociatedSystemCapabilities"]["KvmOnPowerVMCapable"] = "true"
    b.hmc.get_console_info.return_value = {
        "Resource": {
            "VersionInfo": {
                "Version": "10",
                "Release": "3",
                "ServicePackName": "1060",
                "Minor": "1",
            }
        }
    }
    b.local["Resource"]["KvmCapable"] = "false"
    b.saved.update(ActiveMemoryExpansionEnabled="true", ExpansionFactor="1.10")
    monkeypatch.setattr(
        linux_roles, "get_proc_compat_modes", AsyncMock(return_value=["POWER10"])
    )
    monkeypatch.setattr(core, "get_lpar_settings", AsyncMock(return_value=settings))
    result = await create(
        b,
        Memory(mem_expansion=1.1),
        resources=LparResources(desired_memory=1024),
        settings=settings,
    )
    assert result.creation_memory_step.status == result.settings_step.status == "ok"
    assert result.creation_memory_step.result.current is None


async def test_completed_dedicated_memory_retains_second_lpar_bound(memory_target):
    b = memory_target
    b.system["Resource"]["AssociatedSystemMemoryConfiguration"][
        "ConfigurableSystemMemory"
    ] = "2048"
    with pytest.raises(ValueError, match="memory|Memory"):
        await create(
            b, Memory(mem_mode="ded"), resources=LparResources(desired_procs=0.5)
        )
    assert not b.created


@pytest.mark.parametrize("where", ["initial-slot", "initial-sriov", "later-sriov"])
async def test_shared_direct_hardware_refuses_before_network(memory_target, where):
    from hmcpctl.documents.lpar_io import LparPhysicalIO
    from hmcpctl.operations.lpar.assignments import SriovLogicalPortAssignment
    from hmcpctl.ssh.profiles import ProfileIoSlot

    b = memory_target
    sriov = SriovLogicalPortAssignment("default_profile", "1", "0", "1", Decimal(10))
    creation = core.LparCreation("new", "AIX/Linux", RESOURCES, creation_memory=SHARED)
    assignments = LparPcieAssignments()
    if where == "initial-slot":
        creation = replace(
            creation,
            physical_io=LparPhysicalIO(
                io_slots=(ProfileIoSlot("21000010", None, False),)
            ),
        )
    elif where == "initial-sriov":
        creation = replace(creation, creation_sriov=(sriov,))
    else:
        assignments = LparPcieAssignments(sriov=(sriov,))
    with pytest.raises(ValueError, match="direct physical"):
        await workflows.create_lpar(b.hmc, SYSTEM, creation, assignments)
    assert not b.hmc.find_partition_by_name.called and not b.hmc._get.called


@pytest.mark.parametrize("selector", ["name", "id"])
async def test_redundant_saved_provider_designations_and_manual_entitlement(
    memory_target, selector
):
    b = memory_target
    b.saved.update(
        SecondaryPagingServicePartition=link(SECONDARY),
        ManualEntitledModeEnabled="true",
        DesiredEntitledMemory="128",
    )
    memory = Memory(
        mem_mode="shared",
        desired_io_entitled_mem=128,
        primary_paging_vios_id=7,
        **(
            {"secondary_paging_vios_name": "paging-b"}
            if selector == "name"
            else {"secondary_paging_vios_id": 8}
        ),
    )
    result = await create(b, memory)
    proof = result.creation_memory_step.result.saved
    assert (
        proof.status == "verified" and proof.observed["desired_io_entitled_mem"] == 128
    )
    assert proof.observed["primary_paging_vios_id"]["uuid"] == PRIMARY
    assert proof.observed[f"secondary_paging_vios_{selector}"]["uuid"] == SECONDARY


@pytest.mark.parametrize(
    "field,value",
    [
        ("ExpansionFactor", "2"),
        ("ExpansionFactor", None),
        ("ActiveMemoryExpansionEnabled", "false"),
    ],
)
async def test_saved_ame_mismatch_or_missing_factor_stops_apply(
    memory_target, field, value
):
    b = memory_target
    b.saved.update(ActiveMemoryExpansionEnabled="true", ExpansionFactor="1.10")
    if value is None:
        del b.saved[field]
    else:
        b.saved[field] = value
    result = await create(
        b,
        Memory(mem_expansion=1.1),
        resources=LparResources(desired_memory=1024),
        apply_profile=True,
    )
    assert result.creation_memory_step.status == "error"
    assert "apply" not in b.events


@pytest.mark.parametrize("fault", ["saved", "apply", "lookup"])
async def test_readback_and_apply_diagnostics_are_sanitized(memory_target, fault):
    b = memory_target
    b.fault = fault
    result = await workflows.create_lpar(
        b.hmc,
        SYSTEM,
        core.LparCreation(
            "new", "AIX/Linux", RESOURCES, creation_memory=SHARED, apply_profile=True
        ),
        LparPcieAssignments(),
    )
    assert not result.workflow_completed and "assign" not in b.events
    assert PRIVATE not in repr(result)


@pytest.mark.parametrize("change", ["preexisting", "foreign", "environment", "state"])
async def test_identity_readback_requires_fresh_contained_inactive_resource(
    memory_target, change
):
    b = memory_target
    admission = await admit(b)
    if change == "preexisting":
        admission = replace(admission, preexisting=frozenset({LOCAL}))
    elif change == "environment":
        b.local["Resource"]["PartitionType"] = "OS400"
    elif change == "state":
        b.local["Resource"]["PartitionState"] = "running"
    b.created = change != "foreign"
    with pytest.raises((ValueError, TypeError)):
        await cm.confirm_creation_memory_identity(b.hmc, admission, b.local)


@pytest.mark.parametrize(
    "fault", ["duplicate-capability", "foreign-namespace", "duplicate-saved-leaf"]
)
async def test_malformed_evidence_cannot_supply_configured_proof(memory_target, fault):
    b = memory_target
    if fault == "duplicate-saved-leaf":
        b.saved["MemoryWeight"] = ["0", "0"]
        result = await create(b)
        assert result.creation_memory_step.status == "error"
    else:
        body = xml_entry(b.system)
        if fault == "duplicate-capability":
            body = body.replace(
                "<ActiveMemorySharingCapable>true</ActiveMemorySharingCapable>",
                "<ActiveMemorySharingCapable>true</ActiveMemorySharingCapable>" * 2,
            )
        else:
            body = body.replace(
                "<ActiveMemorySharingCapable>",
                '<ActiveMemorySharingCapable xmlns="urn:foreign">',
            )
        b.raw_fault = ("ManagedSystem/" + SYSTEM, body)
        with pytest.raises((ValueError, TypeError)):
            await create(b)
        assert not b.created


async def test_controller_apply_refuses_before_submission(memory_target):
    b = memory_target
    b.controller = True
    with pytest.raises(ValueError, match="named profile"):
        await create(b, apply_profile=True)
    assert not b.created


@pytest.mark.parametrize(
    "field,missing",
    [
        ("desired_io_entitled_mem", "DesiredEntitledMemory"),
        ("mem_expansion", "ExpansionFactor"),
    ],
)
async def test_known_enablement_mismatch_precedes_missing_scalar(
    memory_target, field, missing
):
    b = memory_target
    if field == "desired_io_entitled_mem":
        b.saved["ManualEntitledModeEnabled"] = "true"
        b.saved.pop(missing, None)
        memory = SHARED
    else:
        b.saved["ActiveMemoryExpansionEnabled"] = "true"
        b.saved.pop(missing, None)
        memory = Memory(mem_expansion=0)
    result = await create(b, memory, resources=LparResources(desired_memory=1024))
    proof = result.creation_memory_step.result.saved
    assert proof.status == "mismatch" and field in proof.unavailable


async def test_omitted_mode_keeps_observed_fact_without_inventing_request(
    memory_target,
):
    b = memory_target
    result = await create(
        b, Memory(mem_expansion=0), resources=LparResources(desired_memory=1024)
    )
    proof = result.creation_memory_step.result
    assert (
        proof.requested.mem_mode is None
        and proof.saved.observed["mem_mode"] == "shared"
    )


async def test_wrong_role_retains_known_admitted_identity(memory_target):
    b = memory_target
    b.saved["SecondaryPagingServicePartition"] = link(PRIMARY)
    result = await create(b)
    proof = result.creation_memory_step.result.saved
    assert proof.status == "mismatch"
    assert proof.observed["secondary_paging_vios_id"]["uuid"] == PRIMARY


async def test_explicit_zero_provider_port_is_not_default_https_origin(memory_target):
    b = memory_target
    b.pools[0]["Resource"]["PagingServicePartitionOne"] = link(
        PRIMARY, host="hmc.test:0"
    )
    with pytest.raises(ValueError, match="foreign"):
        await create(b)
    assert not b.created


@pytest.mark.parametrize("refusal", [False, True])
async def test_c06_c11_c07_share_one_writer_and_preserve_refusal(
    memory_target, monkeypatch, refusal
):
    from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout

    b = memory_target
    layout = LparMemoryLayout(desired_num_huge_pages=1)
    monkeypatch.setattr(core, "get_lpar_memory_layout", AsyncMock(return_value=layout))
    if refusal:
        b.fault = "refusal"
        with pytest.raises(HMCError, match="rejected.*HSCL07C1"):
            await create(
                b,
                memory_layout=layout,
                max_virtual_slots=8,
                virtual_serial_adapters=(VirtualSerialAdapter(2, "server", False),),
                apply_profile=True,
            )
        assert "stamp" not in b.events and "apply" not in b.events
    else:
        result = await create(
            b,
            memory_layout=layout,
            max_virtual_slots=8,
            virtual_serial_adapters=(VirtualSerialAdapter(2, "server", False),),
            apply_profile=True,
        )
        assert (
            result.creation_memory_step.status
            == result.serial_step.status
            == result.memory_layout_step.status
            == "ok"
        )
    assert b.events.count("create") == 1


@pytest.mark.parametrize("missing", [False, True])
async def test_c10_access_and_ame_saved_expansion_are_separate(memory_target, missing):
    b = memory_target
    b.system["Resource"]["AssociatedSystemCapabilities"][
        "HardwareMemoryCompressionCapable"
    ] = "true"
    if not missing:
        b.current["MemoryExpansionHardwareAccessEnabled"] = "false"
    b.saved.update(ActiveMemoryExpansionEnabled="true", ExpansionFactor="1.10")
    b.current.update(MemoryExpansionEnabled="true", CurrentExpansionFactor="1.10")
    result = await create(
        b,
        Memory(mem_expansion=1.1),
        resources=LparResources(desired_memory=1024),
        hardware_mem_expansion=False,
        apply_profile=True,
    )
    assert result.creation_memory_step.result.saved.status == "verified"
    assert result.hardware_memory_step.status == ("error" if missing else "ok")
    assert ("apply" in b.events) is not missing


async def test_identity_metadata_attributes_preserve_confirmed_scalar_values(
    memory_target,
):
    b = memory_target
    body = xml_entry(b.local).replace("<PartitionName>", '<PartitionName ksv="V1_5_0">')
    b.raw_fault = ("LogicalPartition/" + LOCAL, body)
    result = await create(b)
    assert result.ownership_stamped is True
    assert result.creation_memory_step.status == "ok"
    assert result.lpar["Resource"]["PartitionName"] == "new"


@pytest.mark.parametrize("primary_selector", [None, "name", "id"])
async def test_single_provider_keeps_omitted_secondary_selection_and_verifies_empty(
    memory_target, primary_selector
):
    b = memory_target
    b.providers = b.providers[:1]
    del b.pools[0]["Resource"]["PagingServicePartitionTwo"]
    selectors = (
        {}
        if primary_selector is None
        else {
            f"primary_paging_vios_{primary_selector}": "paging-a"
            if primary_selector == "name"
            else 7
        }
    )
    memory = Memory(mem_mode="shared", **selectors)
    admission = await admit(b, memory)
    assert admission.primary == admission.secondary
    assert [p.uuid for p in admission.secondary] == [PRIMARY]
    assert memory.secondary_paging_vios_name is memory.secondary_paging_vios_id is None
    result = await create(b, memory)
    proof = result.creation_memory_step.result.saved
    assert proof.status == "verified"
    assert proof.observed["secondary_paging_vios_name"] == "none"
    assert (
        proof.observed[f"primary_paging_vios_{primary_selector or 'name'}"]["uuid"]
        == PRIMARY
    )
    assert b.events.count("create") == 1


@pytest.mark.parametrize("secondary_selector", ["name", "id"])
async def test_single_provider_cannot_satisfy_explicit_same_secondary(
    memory_target, secondary_selector
):
    b = memory_target
    b.providers = b.providers[:1]
    del b.pools[0]["Resource"]["PagingServicePartitionTwo"]
    memory = Memory(
        mem_mode="shared",
        **{
            f"secondary_paging_vios_{secondary_selector}": "paging-a"
            if secondary_selector == "name"
            else 7
        },
    )
    with pytest.raises(ValueError, match="identities must differ"):
        await create(b, memory)
    assert not b.created


async def test_single_provider_omission_keeps_candidate_authorization(memory_target):
    b = memory_target
    b.providers = b.providers[:1]
    del b.pools[0]["Resource"]["PagingServicePartitionTwo"]
    b.providers[0]["Resource"]["Description"] = (
        "[hmcpctl owner:foreign created:2026-10-27]"
    )
    with pytest.raises(PermissionError):
        await create(b, Memory(mem_mode="shared"))
    assert not b.created


async def test_single_provider_same_saved_pair_is_still_mismatch(memory_target):
    b = memory_target
    b.providers = b.providers[:1]
    del b.pools[0]["Resource"]["PagingServicePartitionTwo"]
    b.saved["SecondaryPagingServicePartition"] = link(PRIMARY)
    result = await create(b, Memory(mem_mode="shared"), apply_profile=True)
    assert result.creation_memory_step.result.saved.status == "mismatch"
    assert (
        result.creation_memory_step.result.saved.observed["primary_paging_vios_name"]
        == result.creation_memory_step.result.saved.observed[
            "secondary_paging_vios_name"
        ]
    )
    assert "apply" not in b.events
