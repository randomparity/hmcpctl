"""C12 creation grammar and device relationship contracts."""

from dataclasses import fields
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.operations.lpar.assignments import SriovLogicalPortAssignment
from hmcpctl.operations.lpar.physical_io import compare_creation_io
from hmcpctl.ssh.lpar_io import creation_io_record
from hmcpctl.ssh.profiles import ProfileIoSlot

DRC = "21010010"
NAMES = (
    "io_slots",
    "lpar_io_pool_ids",
    "load_source_slot",
    "alt_restart_device_slot",
    "console_slot",
    "alt_console_slot",
    "op_console_slot",
    "virtual_opti_pool_id",
)


def test_field_grammar():
    assert tuple(f.name for f in fields(LparPhysicalIO)) == NAMES
    assert not LparPhysicalIO().requested
    assert LparPhysicalIO(virtual_opti_pool_id=0).requested
    assert LparPhysicalIO(console_slot="hmc").console_slot == "hmc"


@pytest.mark.parametrize(
    "name,value",
    [
        ("console_slot", "3"),
        ("load_source_slot", "12"),
        ("alt_restart_device_slot", "99"),
        ("op_console_slot", "hmc"),
        ("alt_console_slot", "x,console_slot=hmc"),
        ("console_slot", "lowercase"),
        ("virtual_opti_pool_id", True),
        ("virtual_opti_pool_id", 2),
        ("lpar_io_pool_ids", ("1", "1")),
        ("lpar_io_pool_ids", ("x",)),
        ("io_slots", (ProfileIoSlot(DRC, None, 1),)),
        ("io_slots", (ProfileIoSlot(DRC, "a", False),)),
        ("io_slots", (ProfileIoSlot(DRC, None, False),) * 2),
    ],
)
def test_malformed_or_unresolvable_values_fail(name, value):
    with pytest.raises((ValueError, TypeError)):
        LparPhysicalIO(**{name: value})


def test_record_roundtrip():
    io = LparPhysicalIO(
        io_slots=(
            ProfileIoSlot(DRC, "3", True),
            ProfileIoSlot("21010012", None, False),
        ),
        lpar_io_pool_ids=("3", "4"),
        console_slot="hmc",
        virtual_opti_pool_id=0,
    )
    ports = (
        SriovLogicalPortAssignment(
            "default_profile", "1", "0", "27004002", Decimal("2.5")
        ),
    )
    assert creation_io_record(io, ports) == (
        '"io_slots=21010010/3/1,21010012//0","lpar_io_pool_ids=3,4",'
        "console_slot=hmc,virtual_opti_pool_id=0,"
        "sriov_eth_logical_ports=adapter_id=1:phys_port_id=0:logical_port_id=27004002:capacity=2.5"
    )


@pytest.mark.parametrize("logical", ["27004002:x=1", "x", "00000001"])
def test_nested_port_identity_refuses_injection(logical):
    with pytest.raises(ValueError):
        creation_io_record(
            LparPhysicalIO(console_slot="hmc"),
            (
                SriovLogicalPortAssignment(
                    "default_profile", "1", "0", logical, Decimal(2)
                ),
            ),
        )


def _profile():
    return {
        "IOConfigurationInstance": {
            "PoolIDs": "3",
            "ProfileIOSlots": {
                "ProfileIOSlot": {
                    "AssociatedIOSlot": {
                        "SlotDynamicReconfigurationConnectorIndex": str(int(DRC, 16))
                    },
                    "IsRequired": "true",
                    "PoolID": "3",
                }
            },
            "TaggedIO": {"LoadSource": str(int(DRC, 16)), "Console": "HMC"},
            "VirtualOpticonnectPool": "false",
        }
    }


def test_native_schema_normalization_and_postcondition():
    io = LparPhysicalIO(
        io_slots=(ProfileIoSlot(DRC, "3", True),),
        lpar_io_pool_ids=("3",),
        load_source_slot=DRC,
        console_slot="hmc",
        virtual_opti_pool_id=0,
    )
    assert compare_creation_io(io, (), _profile())
    bad = _profile()
    bad["IOConfigurationInstance"]["TaggedIO"]["Console"] = "none"
    assert not compare_creation_io(io, (), bad)
    assert not compare_creation_io(io, (), {})


def test_unknown_pool_or_required_slot_does_not_compare_equal():
    io = LparPhysicalIO(io_slots=(ProfileIoSlot(DRC, None, False),))
    assert not compare_creation_io(io, (), _profile())
    bad = _profile()
    bad["IOConfigurationInstance"]["ProfileIOSlots"]["ProfileIOSlot"]["IsRequired"] = (
        "unknown"
    )
    with pytest.raises(ValueError):
        compare_creation_io(io, (), bad)


@pytest.fixture
def admission(monkeypatch):
    from hmcpctl.operations.lpar import physical_io as module

    hmc = SimpleNamespace(config=object())
    slots = AsyncMock(
        return_value=[
            {
                "drc_index": DRC,
                "lpar_id": "none",
                "slot_io_pool_id": "3",
                "lpar_assignment_capable": "1",
                "pci_vendor_id": "1014",
            }
        ]
    )
    profiles = AsyncMock(return_value=[])
    monkeypatch.setattr(module, "list_io_slots", slots)
    monkeypatch.setattr(module, "read_profile_io_slot_rows", profiles)
    monkeypatch.setattr(
        module, "resolve_system_cli_name", AsyncMock(return_value="system")
    )
    monkeypatch.setattr(
        module,
        "read_system_entry",
        AsyncMock(
            return_value={
                "Resource": {
                    "AssociatedSystemCapabilities": {
                        "IBMiCapable": "true",
                        "IBMiNativeIOCapable": "true",
                    },
                    "AssociatedSystemIOConfiguration": {
                        "IOSlots": {
                            "IOSlot": {
                                "SlotDynamicReconfigurationConnectorIndex": str(
                                    int(DRC, 16)
                                ),
                                "RelatedIBMiIOSlot": {
                                    "IOPInfoStale": "false",
                                    "LoadSourceCapable": "true",
                                    "ConsoleCapable": "true",
                                    "OperationsConsoleCapable": "true",
                                },
                            }
                        }
                    },
                }
            }
        ),
    )
    return SimpleNamespace(module=module, hmc=hmc, slots=slots, profiles=profiles)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem",
    [
        "foreign",
        "missing",
        "ambiguous",
        "pool",
        "ineligible",
        "empty",
        "profile",
        "required",
    ],
)
async def test_reject_relationship_before_create(admission, problem):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.core import LparCreation

    b = admission
    slot = b.slots.return_value[0]
    if problem == "foreign":
        slot["lpar_id"] = "4"
    if problem == "missing":
        b.slots.return_value = []
    if problem == "ambiguous":
        b.slots.return_value *= 2
    if problem == "pool":
        slot["slot_io_pool_id"] = "4"
    if problem == "ineligible":
        slot["lpar_assignment_capable"] = "0"
    if problem == "empty":
        slot["pci_vendor_id"] = "FFFF"
    if problem == "profile":
        b.profiles.return_value = [
            {"lpar_name": "other", "name": "p", "io_slots": DRC + "/none/0"}
        ]
    creation = LparCreation(
        "new",
        "OS400",
        LparResources(desired_memory=256, desired_procs=0.1),
        physical_io=LparPhysicalIO(
            io_slots=(ProfileIoSlot(DRC, "3", problem != "required"),),
            lpar_io_pool_ids=("3",),
            load_source_slot=DRC,
        ),
    )
    messages = {
        "foreign": "in use",
        "missing": "membership",
        "ambiguous": "ambiguous",
        "pool": "membership",
        "ineligible": "assignment capable",
        "empty": "empty",
        "profile": "in use",
        "required": "required",
    }
    with pytest.raises(ValueError, match=messages[problem]):
        await b.module.admit_creation_io(b.hmc, "system", creation)


@pytest.mark.asyncio
async def test_console_only_checks_capability_without_allocating_slots(admission):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.core import LparCreation

    b = admission
    creation = LparCreation(
        "new",
        "OS400",
        LparResources(desired_memory=256, desired_procs=0.1),
        physical_io=LparPhysicalIO(console_slot="hmc"),
    )
    await b.module.admit_creation_io(b.hmc, "system", creation)
    b.slots.assert_not_awaited()
    b.profiles.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["error", "ok"])
async def test_workflow_io_gate_consumes_sriov_once(monkeypatch, status):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.assignments import (
        AssignmentResult,
        LparPcieAssignments,
    )
    from hmcpctl.operations.lpar.core import LparCreation, LparCreationResult
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
    from hmcpctl.operations.lpar.workflows import create_lpar

    module = "hmcpctl.operations.lpar.workflows."
    monkeypatch.setattr(module + "prevalidate_lpar_pcie_assignments", AsyncMock())
    port = SriovLogicalPortAssignment(
        "default_profile", "1", "0", "27004002", Decimal(2)
    )
    create = AsyncMock(
        return_value=LparCreationResult(
            True,
            {"UUID": "new"},
            True,
            (),
            apply_step=WorkflowStep("apply_profile", "ok") if status == "ok" else None,
            physical_io_step=WorkflowStep("physical_io", status),
        )
    )
    apply = AsyncMock(return_value=AssignmentResult(True, False, ()))
    monkeypatch.setattr(module + "create_and_stamp_lpar", create)
    monkeypatch.setattr(module + "apply_validated_lpar_pcie_assignments", apply)
    result = await create_lpar(
        object(),
        "s",
        LparCreation(
            "new",
            "OS400",
            LparResources(),
            physical_io=LparPhysicalIO(console_slot="27004002"),
        ),
        LparPcieAssignments(sriov=(port,)),
    )
    assert result.workflow_completed is (status == "ok")
    assert create.call_args.args[2].creation_sriov == (port,)
    assert any(step.step == "physical_io" for step in result.steps)
    if status == "ok":
        assert apply.call_args.args[3].sriov == ()
        names = [step.step for step in result.steps]
        assert names.index("physical_io") < names.index("apply_profile")
    else:
        apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [True, False])
async def test_core_io_verification_precedes_profile_application(monkeypatch, matched):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar import core
    from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    events = []
    hmc = SimpleNamespace(
        config=object(),
        find_partition_by_name=AsyncMock(side_effect=[None, {"UUID": "new"}]),
    )
    monkeypatch.setattr(core, "resolve_system_uuid", AsyncMock(return_value="system"))
    monkeypatch.setattr(
        core, "resolve_system_cli_name", AsyncMock(return_value="system")
    )
    monkeypatch.setattr(core, "require_memory_within_system", AsyncMock())
    monkeypatch.setattr(core, "admit_creation_io", AsyncMock())
    monkeypatch.setattr(core, "create_lpar_via_cli", AsyncMock())
    monkeypatch.setattr(
        core, "stamp_created_lpar_ownership", AsyncMock(return_value=(True, ()))
    )

    async def read(*args):
        events.append("verify")
        return WorkflowStep("physical_io", "ok" if matched else "error")

    async def apply(*args):
        events.append("apply")
        return WorkflowStep("apply_profile", "ok")

    monkeypatch.setattr(core, "created_io_step", read)
    monkeypatch.setattr(core, "_apply_created_profile", apply)
    result = await create_and_stamp_lpar(
        hmc,
        "system",
        LparCreation(
            "new",
            "OS400",
            LparResources(desired_memory=256, desired_procs=0.1),
            apply_profile=True,
            physical_io=LparPhysicalIO(console_slot="hmc"),
        ),
    )
    assert events == (["verify", "apply"] if matched else ["verify"])
    assert result.physical_io_step.status == ("ok" if matched else "error")


@pytest.mark.asyncio
async def test_provision_io_mismatch_stops_network_storage_and_power(monkeypatch):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar import provision
    from hmcpctl.operations.lpar.core import LparCreationResult
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

    request = provision.ProvisionRequest(
        "new",
        provision.ProvisionAdapters(1),
        provision.ProvisionStorage("vios", "disk"),
        LparResources(),
        partition_type="OS400",
        physical_io=LparPhysicalIO(console_slot="hmc"),
    )
    monkeypatch.setattr(provision, "_preflight_provision_request", AsyncMock())
    create = AsyncMock(
        return_value=LparCreationResult(
            True,
            {"UUID": "new"},
            True,
            (),
            physical_io_step=WorkflowStep("physical_io", "error"),
        )
    )
    legs = AsyncMock()
    monkeypatch.setattr(provision, "create_and_stamp_lpar", create)
    monkeypatch.setattr(provision, "_run_legs_through_power", legs)
    result = await provision.provision_lpar(object(), "system", request)
    assert not result.workflow_completed
    assert create.call_args.args[2].physical_io.console_slot == "hmc"
    legs.assert_not_awaited()


@pytest.mark.asyncio
async def test_physical_tag_requires_fresh_device_role_capability(admission):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.core import LparCreation

    creation = LparCreation(
        "new",
        "OS400",
        LparResources(desired_memory=256),
        physical_io=LparPhysicalIO(
            io_slots=(ProfileIoSlot(DRC, "3", True),),
            lpar_io_pool_ids=("3",),
            load_source_slot=DRC,
        ),
    )
    admission.module.read_system_entry.return_value["Resource"][
        "AssociatedSystemCapabilities"
    ].pop("IBMiNativeIOCapable")
    with pytest.raises(ValueError, match="capability"):
        await admission.module.admit_creation_io(admission.hmc, "s", creation)


@pytest.mark.asyncio
async def test_admitted_physical_relationship_roundtrip(admission):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.core import LparCreation

    io = LparPhysicalIO(
        io_slots=(ProfileIoSlot(DRC, "3", True),),
        lpar_io_pool_ids=("3",),
        load_source_slot=DRC,
        alt_restart_device_slot=DRC,
        console_slot=DRC,
        alt_console_slot=DRC,
        op_console_slot=DRC,
        virtual_opti_pool_id=1,
    )
    creation = LparCreation(
        "new", "OS400", LparResources(desired_memory=256), physical_io=io
    )
    await admission.module.admit_creation_io(admission.hmc, "s", creation)
    profile = _profile()
    profile["IOConfigurationInstance"]["VirtualOpticonnectPool"] = "true"
    profile["IOConfigurationInstance"]["TaggedIO"] = {
        name: str(int(DRC, 16))
        for name in (
            "LoadSource",
            "AlternateLoadSource",
            "Console",
            "AlternateConsole",
            "OperationsConsole",
        )
    }
    assert compare_creation_io(io, (), profile)


@pytest.mark.parametrize("mode", ["ok", "identity", "capacity", "missing", "malformed"])
def test_direct_sriov_profile_postconditions(mode):
    port = SriovLogicalPortAssignment(
        "default_profile", "1", "0", "27004002", Decimal(2)
    )
    profile = _profile()
    p = {
        "LogicalPortID": str(int("27004002", 16)),
        "AdapterID": "1",
        "PhysicalPortID": "0",
        "ConfiguredCapacityPercentage": "2.0",
    }
    profile["ProfileSRIOVEthernetLogicalPorts"] = {"ProfileSRIOVEthernetLogicalPort": p}
    if mode == "identity":
        p["PhysicalPortID"] = "1"
    if mode == "capacity":
        p["ConfiguredCapacityPercentage"] = "3"
    if mode == "missing":
        profile.pop("ProfileSRIOVEthernetLogicalPorts")
    if mode == "malformed":
        p["LogicalPortID"] = "unknown"
    if mode == "malformed":
        with pytest.raises(ValueError):
            compare_creation_io(LparPhysicalIO(console_slot="hmc"), (port,), profile)
    else:
        assert compare_creation_io(
            LparPhysicalIO(console_slot="hmc"), (port,), profile
        ) is (mode == "ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mismatch", "unavailable"])
async def test_created_step_never_confirms_unverified_profile(monkeypatch, mode):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar import physical_io
    from hmcpctl.operations.lpar.core import LparCreation

    read = AsyncMock(return_value=SimpleNamespace(fields={}))
    if mode == "unavailable":
        read.side_effect = HMCError("read unavailable")
    monkeypatch.setattr(physical_io, "read_partition_profile", read)
    result = await physical_io.created_io_step(
        object(),
        "system",
        LparCreation(
            "new",
            "OS400",
            LparResources(),
            physical_io=LparPhysicalIO(console_slot="hmc"),
        ),
        {"UUID": "new"},
    )
    assert result.status == "error"
    assert "unverified" in str(result.result) or "mismatch" in str(result.result)
    read.assert_awaited_once_with(
        read.call_args.args[0],
        "system",
        "new",
        "default_profile",
    )


@pytest.mark.parametrize(
    "args",
    [
        {"io_slots": []},
        {"io_slots": (ProfileIoSlot(DRC, None, False),) * 257},
        {"lpar_io_pool_ids": ("01",)},
        {"load_source_slot": "00000001"},
    ],
)
def test_bounded_identity_and_collection_inputs(args):
    with pytest.raises((ValueError, TypeError)):
        LparPhysicalIO(**args)


@pytest.mark.asyncio
async def test_native_dispatch_failure_reports_unknown_without_retry(monkeypatch):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar import core

    hmc = SimpleNamespace(
        config=object(), find_partition_by_name=AsyncMock(return_value=None)
    )
    monkeypatch.setattr(core, "resolve_system_uuid", AsyncMock(return_value="system"))
    monkeypatch.setattr(core, "require_memory_within_system", AsyncMock())
    monkeypatch.setattr(core, "admit_creation_io", AsyncMock())
    dispatch = AsyncMock(side_effect=HMCError("SSH response lost"))
    stamp = AsyncMock()
    monkeypatch.setattr(core, "_create_via_cli", dispatch)
    monkeypatch.setattr(core, "stamp_created_lpar_ownership", stamp)
    with pytest.raises(HMCError, match="outcome is unknown.*Inspect.*before retrying"):
        await core.create_and_stamp_lpar(
            hmc,
            "system",
            core.LparCreation(
                "new",
                "OS400",
                LparResources(desired_memory=256, desired_procs=0.1),
                physical_io=LparPhysicalIO(console_slot="hmc"),
            ),
        )
    dispatch.assert_awaited_once()
    stamp.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_unknown_create_preserves_uncertainty_and_stops_legs(
    monkeypatch,
):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.errors import HMCError
    from hmcpctl.operations.lpar import provision

    request = provision.ProvisionRequest(
        "new",
        provision.ProvisionAdapters(1),
        provision.ProvisionStorage("vios", "disk"),
        LparResources(),
        partition_type="OS400",
        physical_io=LparPhysicalIO(console_slot="hmc"),
    )
    monkeypatch.setattr(provision, "_preflight_provision_request", AsyncMock())
    create = AsyncMock(side_effect=HMCError("outcome unknown: inspect before retrying"))
    legs = AsyncMock()
    monkeypatch.setattr(provision, "create_and_stamp_lpar", create)
    monkeypatch.setattr(provision, "_run_legs_through_power", legs)
    result = await provision.provision_lpar(object(), "system", request)
    assert result.resource_created is None and not result.workflow_completed
    assert "inspect before retrying" in result.steps[0].result
    create.assert_awaited_once()
    legs.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_creation", [False, True])
async def test_combined_creation_and_vnic_capacity_rejects_before_create(
    monkeypatch, explicit_creation
):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar import assignments, workflows
    from hmcpctl.operations.lpar.core import LparCreation
    from hmcpctl.operations.virtualization.vnic import VnicBackingSelector

    direct = SriovLogicalPortAssignment(
        "default_profile", "1", "0", "27004002", Decimal(60)
    )
    vnic = assignments.VnicAssignment(
        VnicBackingSelector("vios", "2", "1", "0", Decimal(60)), 1
    )
    collection = assignments.LparPcieAssignments(
        sriov=() if explicit_creation else (direct,), vnics=(vnic,)
    )
    creation = LparCreation(
        "new",
        "OS400",
        LparResources(desired_memory=256, desired_procs=0.1),
        physical_io=LparPhysicalIO(console_slot="hmc"),
        creation_sriov=(direct,) if explicit_creation else (),
    )
    monkeypatch.setattr(
        assignments, "resolve_ssh_names", AsyncMock(return_value=("system", None))
    )
    monkeypatch.setattr(assignments, "require_admitted_environment", AsyncMock())
    monkeypatch.setattr(
        assignments,
        "list_sriov_adapters",
        AsyncMock(
            return_value=SimpleNamespace(
                capability="available",
                system="system",
                items=[SimpleNamespace(mode="sriov", availability="1")],
            )
        ),
    )
    monkeypatch.setattr(
        assignments,
        "list_sriov_physical_ports",
        AsyncMock(
            return_value=SimpleNamespace(
                capability="available",
                items=[
                    SimpleNamespace(
                        availability="up",
                        minimum_capacity_granularity_percent=Decimal(1),
                    )
                ],
            )
        ),
    )
    monkeypatch.setattr(
        assignments,
        "list_sriov_logical_ports",
        AsyncMock(return_value=SimpleNamespace(capability="available")),
    )
    monkeypatch.setattr(
        assignments, "_existing_capacity", AsyncMock(return_value=Decimal(0))
    )
    monkeypatch.setattr(
        assignments,
        "read_vios_identity",
        AsyncMock(
            return_value={"name": "vios", "lpar_id": "2", "lpar_env": "vioserver"}
        ),
    )
    create = AsyncMock(
        side_effect=AssertionError("creation reached before aggregate admission")
    )
    monkeypatch.setattr(workflows, "create_and_stamp_lpar", create)
    with pytest.raises(ValueError, match="120% requested"):
        await workflows.create_lpar(
            SimpleNamespace(config=object()), "system", creation, collection
        )
    create.assert_not_awaited()


@pytest.fixture
def sriov_creation_inventory(admission, monkeypatch):
    from hmcpctl.operations.lpar import assignments, core
    from hmcpctl.operations.lpar.workflow_contract import WorkflowStep
    from hmcpctl.operations.virtualization import pcie

    raw = [
        {"adapter_id": "1", "logical_port_id": "27004002", "location_code": "port-0-S1"}
    ]
    configured = AsyncMock(return_value=[])
    unused = AsyncMock(return_value=raw)
    observed = AsyncMock(return_value=raw)
    monkeypatch.setattr(
        pcie,
        "read_sriov_environment",
        AsyncMock(
            return_value=("Version: 10\nRelease: 3\nService Pack: 1060", "8375-42A")
        ),
    )
    monkeypatch.setattr(
        pcie,
        "list_sriov_adapter_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": "1",
                    "config_state": "sriov",
                    "functional_state": "1",
                    "phys_loc": "adapter",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        pcie,
        "list_sriov_physical_port_rows",
        AsyncMock(
            return_value=[
                {
                    "adapter_id": "1",
                    "phys_port_id": str(index),
                    "state": "1",
                    "phys_port_loc": f"port-{index}",
                    "min_eth_capacity_granularity": "1",
                }
                for index in (0, 1)
            ]
        ),
    )
    monkeypatch.setattr(pcie, "list_sriov_configured_logical_port_rows", configured)
    monkeypatch.setattr(
        assignments, "list_sriov_configured_logical_port_rows", configured
    )
    monkeypatch.setattr(
        assignments, "list_vnic_backing_rows", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(pcie, "list_sriov_unconfigured_logical_port_rows", unused)
    monkeypatch.setattr(
        admission.module, "list_sriov_unconfigured_logical_port_rows", observed
    )
    admission.hmc.find_partition_by_name = AsyncMock(return_value=None)
    monkeypatch.setattr(core, "resolve_system_uuid", AsyncMock(return_value="system"))
    monkeypatch.setattr(core, "require_memory_within_system", AsyncMock())
    dispatch = AsyncMock(return_value=("system", None, {"UUID": "created"}, None))
    monkeypatch.setattr(core, "_create_via_cli", dispatch)
    monkeypatch.setattr(
        core, "stamp_created_lpar_ownership", AsyncMock(return_value=(True, ()))
    )
    monkeypatch.setattr(
        core,
        "created_io_step",
        AsyncMock(return_value=WorkflowStep("physical_io", "ok")),
    )
    return SimpleNamespace(
        core=core,
        hmc=admission.hmc,
        raw=raw,
        configured=configured,
        unused=unused,
        observed=observed,
        dispatch=dispatch,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem", ["valid", "missing", "duplicate", "configured", "wrong-parent"]
)
async def test_sriov_creation_identity_admission_precedes_native_dispatch(
    sriov_creation_inventory, problem
):
    from hmcpctl.documents.lpar import LparResources
    from hmcpctl.operations.lpar.core import LparCreation

    b = sriov_creation_inventory
    if problem == "missing":
        b.observed.return_value = []
    elif problem == "duplicate":
        b.observed.return_value = b.raw * 2
    elif problem == "configured":
        b.unused.return_value = []
        b.configured.return_value = [
            {
                "adapter_id": "1",
                "phys_port_id": "0",
                "logical_port_id": "27004002",
                "functional_state": "1",
                "lpar_name": "other",
                "lpar_id": "4",
                "capacity": "2",
                "max_capacity": "100",
            }
        ]
    elif problem == "wrong-parent":
        b.raw[0]["location_code"] = "port-1-S1"
    creation = LparCreation(
        "new",
        "OS400",
        LparResources(desired_memory=256, desired_procs=0.1),
        physical_io=LparPhysicalIO(console_slot="hmc"),
        creation_sriov=(
            SriovLogicalPortAssignment(
                "default_profile", "1", "0", "27004002", Decimal(2)
            ),
        ),
    )
    if problem == "valid":
        result = await b.core.create_and_stamp_lpar(b.hmc, "system", creation)
        assert result.physical_io_step.status == "ok"
        b.dispatch.assert_awaited_once()
    else:
        message = (
            "uniquely unconfigured"
            if problem in {"missing", "duplicate"}
            else "selected physical port"
        )
        with pytest.raises(ValueError, match=message):
            await b.core.create_and_stamp_lpar(b.hmc, "system", creation)
        b.dispatch.assert_not_awaited()
