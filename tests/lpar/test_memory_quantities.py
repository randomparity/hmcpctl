"""D01 non-pool quantity grammar and guarded effective-memory outcomes."""

import shlex
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_memory import LparMemoryChange
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.memory import change_lpar_memory
from hmcpctl.ssh.memory import change_lpar_memory_quantity

CONFIG = HMCConfig.from_mapping({})


@pytest.mark.parametrize("operation", ["add", "remove", "set"])
@pytest.mark.parametrize("quantity", [0, 1, 512, 1024])
def test_quantity_request_preserves_integral_mib(operation, quantity):
    change = LparMemoryChange(operation=operation, quantity_mib=quantity)
    assert change.quantity_mib == quantity
    assert change.entitled_mib is None


@pytest.mark.parametrize("entitled", [1, 100, "auto"])
@pytest.mark.parametrize("operation", ["add", "remove", "set"])
def test_shared_entitlement_request_can_stand_alone_or_combine(operation, entitled):
    assert (
        LparMemoryChange(operation=operation, entitled_mib=entitled).entitled_mib
        == entitled
    )
    combined = LparMemoryChange(
        operation=operation, quantity_mib=512, entitled_mib=entitled
    )
    assert combined.quantity_mib == 512
    assert combined.entitled_mib == entitled


@pytest.mark.parametrize(
    "values",
    [
        {"operation": "add"},
        {"operation": "move", "quantity_mib": 512},
        {"operation": "add", "quantity_mib": -1},
        {"operation": "add", "quantity_mib": True},
        {"operation": "add", "quantity_mib": 1.0},
        {"operation": "add", "quantity_mib": "512"},
        {"operation": "add", "entitled_mib": 0},
        {"operation": "add", "entitled_mib": -1},
        {"operation": "add", "entitled_mib": True},
        {"operation": "add", "entitled_mib": 1.0},
        {"operation": "add", "entitled_mib": "100"},
        {"operation": "add", "entitled_mib": "AUTO"},
        {"operation": "set", "quantity_mib": 512, "force": 1},
        {"operation": "set", "quantity_mib": 512, "target": "other"},
    ],
)
def test_quantity_request_refuses_untyped_empty_or_foreign_scope(values):
    with pytest.raises(ValueError):
        LparMemoryChange(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,native", [("add", "a"), ("remove", "r"), ("set", "s")]
)
@pytest.mark.parametrize("entitled", [None, 100, "auto"])
async def test_fixed_native_quantity_grammar(monkeypatch, operation, native, entitled):
    command = AsyncMock(return_value="native output")
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    change = LparMemoryChange(
        operation=operation, quantity_mib=512, entitled_mib=entitled, force=True
    )
    assert (
        await change_lpar_memory_quantity(CONFIG, "system; quoted ' name", 7, change)
        == "native output"
    )
    words = shlex.split(command.call_args.args[1])
    assert words[:9] == [
        "chhwres",
        "-r",
        "mem",
        "-m",
        "system; quoted ' name",
        "-o",
        native,
        "--id",
        "7",
    ]
    assert words[9:] == [
        "-q",
        "512",
        *([] if entitled is None else ["--entitled", str(entitled)]),
        "--force",
    ]
    assert command.await_count == 1


@pytest.mark.asyncio
async def test_entitlement_only_omits_quantity_and_force(monkeypatch):
    command = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    await change_lpar_memory_quantity(
        CONFIG, "sys-R1", 2, LparMemoryChange(operation="set", entitled_mib="auto")
    )
    assert shlex.split(command.call_args.args[1]) == [
        "chhwres",
        "-r",
        "mem",
        "-m",
        "sys-R1",
        "-o",
        "s",
        "--id",
        "2",
        "--entitled",
        "auto",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("partition_id", [0, -1, True, "1", 1.0])
async def test_native_quantity_transport_refuses_invalid_partition_id(
    monkeypatch, partition_id
):
    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    with pytest.raises(ValueError, match="partition_id"):
        await change_lpar_memory_quantity(
            CONFIG,
            "sys-R1",
            partition_id,
            LparMemoryChange(operation="add", quantity_mib=512),
        )
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_zero_set_is_explicit_reset_not_omitted(monkeypatch):
    command = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    await change_lpar_memory_quantity(
        CONFIG, "sys-R1", 2, LparMemoryChange(operation="set", quantity_mib=0)
    )
    assert shlex.split(command.call_args.args[1])[-2:] == ["-q", "0"]


@pytest.mark.asyncio
async def test_native_quantity_requires_validated_model(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    with pytest.raises(TypeError, match="typed LparMemoryChange"):
        await change_lpar_memory_quantity(CONFIG, "sys-R1", 2, {"operation": "add"})
    command.assert_not_awaited()


SYSTEM_UUID = "cccc0000-0000-0000-0000-000000000001"
LPAR_UUID = "aaaa0000-0000-0000-0000-000000000001"


def memory_entry():
    return {
        "UUID": LPAR_UUID,
        "Resource": {
            "PartitionName": "lpar-R1",
            "PartitionID": "7",
            "PartitionType": "AIX/Linux",
            "PartitionState": "not activated",
            "ResourceMonitoringControlState": "inactive",
            "CurrentProfileSync": "Disabled",
            "PartitionCapabilities": {"DynamicLogicalPartitionMemoryCapable": "true"},
            "PartitionMemoryConfiguration": {
                "CurrentMemory": "1024",
                "CurrentMinimumMemory": "512",
                "CurrentMaximumMemory": "4096",
                "DesiredMemory": "2048",
                "MinimumMemory": "512",
                "MaximumMemory": "4096",
                "RuntimeMemory": "0",
                "SharedMemoryEnabled": "false",
                "AutoEntitledMemoryEnabled": "false",
                "CurrentEntitledMemory": "100",
                "RuntimeEntitledMemory": "0",
            },
            "PartitionProcessorConfiguration": {
                "HasDedicatedProcessors": "true",
                "CurrentHasDedicatedProcessors": "true",
                "CurrentDedicatedProcessorConfiguration": {
                    "CurrentMinimumProcessors": "1",
                    "CurrentProcessors": "1",
                    "CurrentMaximumProcessors": "2",
                },
            },
        },
    }


@pytest.fixture
def quantity_boundary(monkeypatch):
    before = memory_entry()
    after = deepcopy(before)
    after["Resource"]["PartitionMemoryConfiguration"]["CurrentMemory"] = "1536"
    system = {
        "UUID": SYSTEM_UUID,
        "Resource": {
            "SystemName": "sys-R1",
            "State": "operating",
            "AssociatedSystemMemoryConfiguration": {
                "MemoryRegionSize": "256",
                "CurrentAvailableSystemMemory": "8192",
                "ConfigurableSystemMemory": "16384",
            },
        },
    }
    pool = {
        "UUID": "bbbb0000-0000-0000-0000-000000000001",
        "Resource": {
            "CurrentAvailablePoolMemory": "500",
            "CurrentPoolMemory": "1000",
            "PoolID": "0",
        },
    }
    boundary = SimpleNamespace(
        before=before,
        after=after,
        system=system,
        pool=pool,
        submitted=False,
        submit_error=None,
        read_error=None,
    )

    async def read(uuid):
        if boundary.submitted and boundary.read_error:
            raise boundary.read_error
        return deepcopy(boundary.after if boundary.submitted else boundary.before)

    async def submit(config, command, **kwargs):
        boundary.submitted = True
        if boundary.submit_error:
            raise boundary.submit_error
        return "native completion"

    hmc = SimpleNamespace(
        config=CONFIG,
        get_logical_partition=AsyncMock(side_effect=read),
        get_managed_system=AsyncMock(
            side_effect=lambda uuid: deepcopy(boundary.system)
        ),
        list_child=AsyncMock(side_effect=lambda *args: [deepcopy(boundary.pool)]),
    )
    boundary.hmc = hmc
    boundary.authorize = AsyncMock()
    hmc.list_logical_partitions = AsyncMock(return_value=[before])
    hmc.list_vios = AsyncMock(return_value=[])
    hmc.list_system_summaries = AsyncMock(
        return_value=[{"UUID": SYSTEM_UUID, "SystemName": "sys-R1"}]
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.memory.authorize_lpar_mutation", boundary.authorize
    )
    boundary.command = AsyncMock(side_effect=submit)
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", boundary.command)
    return boundary


async def change(boundary, **kwargs):
    return await change_lpar_memory(
        boundary.hmc,
        LPAR_UUID,
        LparMemoryChange(**kwargs),
        system_name_or_uuid=SYSTEM_UUID,
    )


@pytest.mark.asyncio
async def test_add_uses_current_not_desired_and_reports_profile_location(
    quantity_boundary,
):
    b = quantity_boundary
    result = await change(b, operation="add", quantity_mib=512)
    assert result["outcome"] == "applied" and result["workflow_completed"] is True
    assert result["expected"]["quantity_mib"] == 1536
    assert result["before"]["current_memory_mib"] == 1024
    assert result["after"]["current_memory_mib"] == 1536
    assert result["change_location"]["lives_in"] == "current-configuration"
    assert result["warnings"]
    assert b.command.await_count == 1
    b.authorize.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [1, 257, 5000, -1])
async def test_quantity_bounds_alignment_and_types_refuse_before_submission(
    quantity_boundary, quantity
):
    with pytest.raises(ValueError):
        await change(quantity_boundary, operation="add", quantity_mib=quantity)
    quantity_boundary.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_capacity_and_entitlement_mode_refuse_before_submission(
    quantity_boundary,
):
    b = quantity_boundary
    b.system["Resource"]["AssociatedSystemMemoryConfiguration"][
        "CurrentAvailableSystemMemory"
    ] = "256"
    with pytest.raises(ValueError, match="capacity|available"):
        await change(b, operation="add", quantity_mib=512)
    with pytest.raises(ValueError, match="shared"):
        await change(b, operation="set", entitled_mib=100)
    b.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_memory_growth_is_not_capped_to_system_physical_capacity(
    quantity_boundary,
):
    b = quantity_boundary
    for entry in [b.before, b.after]:
        entry["Resource"]["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = (
            "true"
        )
    b.system["Resource"]["AssociatedSystemMemoryConfiguration"].update(
        CurrentAvailableSystemMemory="0", ConfigurableSystemMemory="1024"
    )
    result = await change(b, operation="add", quantity_mib=512)
    assert result["outcome"] == "applied"
    b.hmc.list_child.assert_awaited_once_with(
        "ManagedSystem", SYSTEM_UUID, "SharedMemoryPool"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entitled", ["auto", 150])
async def test_shared_entitlement_verifies_automatic_or_manual_mode(
    quantity_boundary, entitled
):
    b = quantity_boundary
    for entry in [b.before, b.after]:
        entry["Resource"]["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = (
            "true"
        )
    after = b.after["Resource"]["PartitionMemoryConfiguration"]
    after["AutoEntitledMemoryEnabled"] = "true" if entitled == "auto" else "false"
    after["CurrentEntitledMemory"] = "175" if entitled == "auto" else "150"
    result = await change(b, operation="set", entitled_mib=entitled)
    assert result["outcome"] == "applied"
    assert "-q" not in shlex.split(b.command.call_args.args[1])


@pytest.mark.asyncio
async def test_partial_combined_change_is_not_completed(quantity_boundary):
    b = quantity_boundary
    for entry in [b.before, b.after]:
        entry["Resource"]["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = (
            "true"
        )
    result = await change(b, operation="add", quantity_mib=512, entitled_mib=50)
    assert result["outcome"] == "partial" and result["workflow_completed"] is False
    assert b.command.await_count == 1


@pytest.mark.asyncio
async def test_failed_post_write_read_is_unverified_without_replay(quantity_boundary):
    b = quantity_boundary
    b.read_error = HMCError("independent read unavailable")
    result = await change(b, operation="add", quantity_mib=512)
    assert result["outcome"] == "unverified" and result["workflow_completed"] is False
    assert b.command.await_count == 1


@pytest.mark.asyncio
async def test_independent_matching_state_retains_uncertain_submission_error(
    quantity_boundary,
):
    b = quantity_boundary
    b.submit_error = HMCError("transport lost after submission")
    result = await change(b, operation="add", quantity_mib=512)
    assert result["outcome"] == "applied"
    assert "transport lost" in result["submission_error"]
    assert b.command.await_count == 1


@pytest.mark.asyncio
async def test_authorization_refusal_precedes_native_submission(quantity_boundary):
    b = quantity_boundary
    b.authorize.side_effect = PermissionError("foreign owner")
    with pytest.raises(PermissionError, match="foreign"):
        await change(b, operation="add", quantity_mib=512)
    b.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["AutoEntitledMemoryEnabled", "CurrentEntitledMemory"]
)
async def test_missing_entitlement_readback_is_unverified(quantity_boundary, field):
    b = quantity_boundary
    b.before["Resource"]["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = "true"
    b.after = deepcopy(b.before)
    b.after["Resource"]["PartitionMemoryConfiguration"]["AutoEntitledMemoryEnabled"] = (
        "true"
    )
    del b.after["Resource"]["PartitionMemoryConfiguration"][field]
    result = await change(b, operation="set", entitled_mib="auto")
    assert result["outcome"] == "unverified"
    assert not result["workflow_completed"]
    b.command.assert_awaited_once()


@pytest.mark.asyncio
async def test_transport_loss_with_unchanged_readback_is_not_known_rejection(
    quantity_boundary,
):
    b = quantity_boundary
    b.after = deepcopy(b.before)
    b.submit_error = HMCError("SSH connection lost")
    result = await change(b, operation="add", quantity_mib=512)
    assert result["outcome"] == "unverified"
    b.command.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["1e999999", "NaN", "Infinity", "-1", "2.5"])
async def test_non_integral_schema_lexemes_refuse_before_submission(
    quantity_boundary, value
):
    b = quantity_boundary
    b.before["Resource"]["PartitionMemoryConfiguration"]["CurrentMemory"] = value
    with pytest.raises(ValueError):
        await change(b, operation="add", quantity_mib=512)
    b.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,kind,rmc,capability,force,accepted,deferred",
    [
        ("running", "AIX/Linux", "active", "true", False, True, False),
        ("running", "Virtual IO Server", "active", "true", False, True, False),
        ("running", "AIX/Linux", "inactive", "false", True, True, True),
        ("running", "AIX/Linux", "inactive", "true", False, False, None),
        ("running", "AIX/Linux", "active", "false", False, False, None),
        ("running", "OS400", "inactive", "true", False, True, False),
        ("running", "OS400", "active", "true", True, False, None),
        ("starting", "AIX/Linux", "active", "true", False, False, None),
        ("not activated", "unknown", "inactive", "true", False, False, None),
    ],
)
async def test_state_rmc_capability_and_force_admission(
    quantity_boundary, state, kind, rmc, capability, force, accepted, deferred
):
    b = quantity_boundary
    resource = b.before["Resource"]
    resource.update(
        PartitionState=state, PartitionType=kind, ResourceMonitoringControlState=rmc
    )
    resource["PartitionCapabilities"]["DynamicLogicalPartitionMemoryCapable"] = (
        capability
    )
    b.after = deepcopy(b.before)
    b.after["Resource"]["PartitionMemoryConfiguration"].update(
        CurrentMemory="1536", RuntimeMemory="1536"
    )
    if not accepted:
        with pytest.raises(ValueError):
            await change(b, operation="add", quantity_mib=512, force=force)
        b.command.assert_not_awaited()
        return
    result = await change(b, operation="add", quantity_mib=512, force=force)
    assert result["outcome"] == "applied"
    assert result["expected"]["deferred"] is deferred
    assert ("--force" in b.command.await_args.args[1]) is force


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "remove"])
async def test_zero_delta_is_noop_without_native_submission(
    quantity_boundary, operation
):
    result = await change(quantity_boundary, operation=operation, quantity_mib=0)
    assert result["outcome"] == "noop"
    assert result["workflow_completed"]
    quantity_boundary.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_processors", [False, True])
async def test_shutdown_below_minimum_verifies_coupled_cpu_reset(
    quantity_boundary, shared_processors
):
    b = quantity_boundary
    cpu = b.before["Resource"]["PartitionProcessorConfiguration"]
    if shared_processors:
        cpu.clear()
        cpu.update(
            CurrentHasDedicatedProcessors="false",
            CurrentSharedProcessorConfiguration={
                "CurrentMinimumProcessingUnits": "0.05",
                "CurrentProcessingUnits": "0.1",
                "CurrentMaximumProcessingUnits": "1.0",
                "CurrentMinimumVirtualProcessors": "1",
                "AllocatedVirtualProcessors": "1",
                "CurrentMaximumVirtualProcessors": "2",
            },
        )
    b.after = deepcopy(b.before)
    b.after["Resource"]["PartitionMemoryConfiguration"].update(
        CurrentMemory="0", CurrentMinimumMemory="0", CurrentMaximumMemory="0"
    )
    after_cpu = b.after["Resource"]["PartitionProcessorConfiguration"]
    vector = after_cpu[
        "CurrentSharedProcessorConfiguration"
        if shared_processors
        else "CurrentDedicatedProcessorConfiguration"
    ]
    for key in vector:
        vector[key] = "0"
    result = await change(b, operation="remove", quantity_mib=768)
    assert result["outcome"] == "applied"
    assert result["expected"]["reset"]
    assert result["expected"]["quantity_mib"] == 0
    assert any("processor" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_shutdown_cpu_reset_partial_effect_is_not_completed(quantity_boundary):
    b = quantity_boundary
    b.after = deepcopy(b.before)
    b.after["Resource"]["PartitionMemoryConfiguration"].update(
        CurrentMemory="0", CurrentMinimumMemory="0", CurrentMaximumMemory="0"
    )
    result = await change(b, operation="set", quantity_mib=0)
    assert result["outcome"] == "partial"
    assert not result["workflow_completed"]


@pytest.mark.asyncio
async def test_partition_drift_refuses_before_submit(quantity_boundary):
    b = quantity_boundary
    fresh = deepcopy(b.before)
    fresh["Resource"]["PartitionMemoryConfiguration"]["CurrentMemory"] = "1280"
    b.hmc.get_logical_partition.side_effect = [b.before, fresh]
    with pytest.raises(ValueError, match="changed before"):
        await change(b, operation="add", quantity_mib=512)
    b.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("CurrentAvailablePoolMemory", "10"),
        ("CurrentPoolMemory", "100"),
        ("PoolID", None),
    ],
)
async def test_pool_capacity_or_source_refusal_precedes_submit(
    quantity_boundary, field, value
):
    b = quantity_boundary
    b.before["Resource"]["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = "true"
    b.pool["Resource"][field] = value
    with pytest.raises(ValueError):
        await change(b, operation="add", entitled_mib=50)
    b.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_numeric_entitlement_underflow_refuses_before_submit(quantity_boundary):
    b = quantity_boundary
    b.before["Resource"]["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = "true"
    with pytest.raises(ValueError, match="subtraction"):
        await change(b, operation="remove", entitled_mib=101)
    b.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_entitlement_mode_mismatch_is_partial(quantity_boundary):
    b = quantity_boundary
    b.before["Resource"]["PartitionMemoryConfiguration"]["SharedMemoryEnabled"] = "true"
    b.after = deepcopy(b.before)
    b.after["Resource"]["PartitionMemoryConfiguration"].update(
        CurrentEntitledMemory="150", AutoEntitledMemoryEnabled="true"
    )
    result = await change(b, operation="set", entitled_mib=150)
    assert result["outcome"] == "partial"
    assert not result["workflow_completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_system", [False, True])
async def test_real_containment_and_ownership_guard_precedes_memory_submit(
    monkeypatch, foreign_system
):
    native = AsyncMock()
    description = AsyncMock(return_value="[hmcpctl owner:foreign created:2026-10-22]")
    hmc = SimpleNamespace(
        config=CONFIG,
        list_vios=AsyncMock(return_value=[]),
        list_logical_partitions=AsyncMock(
            return_value=[] if foreign_system else [memory_entry()]
        ),
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM_UUID, "SystemName": "sys-R1"}]
        ),
        get_logical_partition=AsyncMock(return_value=memory_entry()),
        get_managed_system=AsyncMock(),
    )
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.get_lpar_description", description
    )
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", native)
    with pytest.raises(
        ValueError if foreign_system else PermissionError,
        match="outside the selected system"
        if foreign_system
        else "ownership_override=true",
    ):
        await change_lpar_memory(
            hmc,
            LPAR_UUID,
            LparMemoryChange(operation="add", quantity_mib=512),
            system_name_or_uuid=SYSTEM_UUID,
        )
    native.assert_not_awaited()
    hmc.get_managed_system.assert_not_awaited()
    if foreign_system:
        description.assert_not_awaited()
    else:
        description.assert_awaited_once_with(CONFIG, "sys-R1", "lpar-R1")


@pytest.mark.asyncio
async def test_native_process_failure_with_unchanged_readback_is_rejected(
    quantity_boundary,
):
    import asyncssh

    from hmcpctl.ssh.transport import HMCCLIError

    b = quantity_boundary
    b.after = deepcopy(b.before)
    error = HMCCLIError("native resource refusal")
    error.__cause__ = asyncssh.ProcessError(
        None, "chhwres", None, 1, None, 1, "", "refused"
    )
    b.submit_error = error
    result = await change(b, operation="add", quantity_mib=512)
    assert result["outcome"] == "rejected"
    assert not result["workflow_completed"]
    assert result["submission_error"] == "native resource refusal"
    b.command.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_submit_containment_loss_cannot_be_reported_applied(
    quantity_boundary, monkeypatch
):
    b = quantity_boundary
    containment = AsyncMock(
        side_effect=[
            [memory_entry()],
            [],
        ]
    )
    b.hmc.list_logical_partitions = containment
    result = await change(b, operation="add", quantity_mib=512)
    assert result["outcome"] == "unverified"
    assert not result["workflow_completed"]
    b.command.assert_awaited_once()
    assert containment.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "set"])
async def test_growth_from_zero_below_configured_minimum_refuses(
    quantity_boundary, operation
):
    b = quantity_boundary
    b.before["Resource"]["PartitionMemoryConfiguration"].update(
        CurrentMemory="0", CurrentMinimumMemory="0", CurrentMaximumMemory="0"
    )
    b.after = deepcopy(b.before)
    b.submit_error = HMCError("native refusal")
    with pytest.raises(ValueError, match="below.*minimum"):
        await change(b, operation=operation, quantity_mib=256)
    b.command.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "set"])
async def test_growth_from_zero_at_minimum_preserves_requested_target(
    quantity_boundary, operation
):
    b = quantity_boundary
    b.before["Resource"]["PartitionMemoryConfiguration"].update(
        CurrentMemory="0", CurrentMinimumMemory="0", CurrentMaximumMemory="0"
    )
    b.after["Resource"]["PartitionMemoryConfiguration"]["CurrentMemory"] = "512"
    result = await change(b, operation=operation, quantity_mib=512)
    assert result["outcome"] == "applied"
    assert result["expected"]["quantity_mib"] == 512
    assert not result["expected"]["reset"]


@pytest.mark.asyncio
async def test_duplicate_partition_name_across_kinds_refuses(quantity_boundary):
    b = quantity_boundary
    b.hmc.list_vios.return_value = [deepcopy(b.before)]
    with pytest.raises(ValueError, match="ambiguous"):
        await change_lpar_memory(
            b.hmc,
            "lpar-R1",
            LparMemoryChange(operation="add", quantity_mib=512),
            system_name_or_uuid=SYSTEM_UUID,
        )
    b.authorize.assert_not_awaited()
    b.command.assert_not_awaited()


@pytest.mark.asyncio
async def test_partition_kind_change_after_submit_is_unverified(quantity_boundary):
    b = quantity_boundary
    b.hmc.list_logical_partitions.side_effect = [[b.before], []]
    b.hmc.list_vios.side_effect = [[], [b.after]]
    result = await change(b, operation="add", quantity_mib=512)
    assert result["outcome"] == "unverified"
    assert not result["workflow_completed"]
    b.command.assert_awaited_once()


@pytest.mark.asyncio
async def test_forced_auto_entitlement_does_not_claim_runtime_convergence(
    quantity_boundary,
):
    b = quantity_boundary
    b.before["Resource"]["PartitionState"] = "running"
    b.before["Resource"]["PartitionMemoryConfiguration"].update(
        SharedMemoryEnabled="true", RuntimeMemory="1024", RuntimeEntitledMemory="100"
    )
    b.after = deepcopy(b.before)
    b.after["Resource"]["PartitionMemoryConfiguration"].update(
        CurrentEntitledMemory="175", AutoEntitledMemoryEnabled="true"
    )
    result = await change(b, operation="set", entitled_mib="auto", force=True)
    assert result["outcome"] == "applied"
    assert result["expected"]["deferred"]
    assert result["after"]["runtime_entitled_mib"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current,runtime,outcome",
    [
        ("175", "175", "applied"),
        ("175", "100", "partial"),
        ("175", None, "unverified"),
        ("175", "auto", "unverified"),
        ("auto", "auto", "unverified"),
    ],
)
async def test_live_auto_entitlement_requires_numeric_convergence(
    quantity_boundary, current, runtime, outcome
):
    b = quantity_boundary
    b.before["Resource"].update(
        PartitionState="running", ResourceMonitoringControlState="active"
    )
    b.before["Resource"]["PartitionMemoryConfiguration"].update(
        SharedMemoryEnabled="true", RuntimeMemory="1024", RuntimeEntitledMemory="100"
    )
    b.after = deepcopy(b.before)
    b.after["Resource"]["PartitionMemoryConfiguration"].update(
        CurrentEntitledMemory=current,
        RuntimeEntitledMemory=runtime,
        AutoEntitledMemoryEnabled="true",
    )
    result = await change(b, operation="set", entitled_mib="auto")
    assert result["outcome"] == outcome
    assert result["workflow_completed"] is (outcome == "applied")
    b.command.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [LPAR_UUID, "lpar-R1"])
async def test_vios_quantities_use_contained_vios_http_resource(
    mock_hmc, monkeypatch, selector
):
    import xml.etree.ElementTree as ET

    import httpx
    from conftest import make_config, mock_system_summary

    from hmcpctl.client.core import HMCClient

    atom = "http://www.w3.org/2005/Atom"
    uom = "http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/"
    before = memory_entry()
    before["Resource"]["PartitionType"] = "Virtual IO Server"
    after = deepcopy(before)
    after["Resource"]["PartitionMemoryConfiguration"]["CurrentMemory"] = "1536"

    def xml(entry, kind):
        root = ET.Element(f"{{{atom}}}entry")
        ET.SubElement(root, f"{{{atom}}}id").text = f"urn:uuid:{entry['UUID']}"
        resource = ET.SubElement(
            ET.SubElement(root, f"{{{atom}}}content"), f"{{{uom}}}{kind}"
        )

        def fields(parent, values):
            for name, value in values.items():
                child = ET.SubElement(parent, f"{{{uom}}}{name}")
                if isinstance(value, dict):
                    fields(child, value)
                else:
                    child.text = str(value)

        fields(resource, entry["Resource"])
        return ET.tostring(root, encoding="unicode")

    mock_system_summary(mock_hmc, SYSTEM_UUID, "sys-R1")
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/LogicalPartition").respond(
        204
    )
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}/VirtualIOServer").respond(
        200, text=xml(before, "VirtualIOServer")
    )
    wrong = mock_hmc.get(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}").respond(404)
    submitted = False

    def read(request):
        return httpx.Response(
            200, text=xml(after if submitted else before, "VirtualIOServer")
        )

    right = mock_hmc.get(f"/rest/api/uom/VirtualIOServer/{LPAR_UUID}").mock(
        side_effect=read
    )
    system = {
        "UUID": SYSTEM_UUID,
        "Resource": {
            "SystemName": "sys-R1",
            "State": "operating",
            "AssociatedSystemMemoryConfiguration": {
                "MemoryRegionSize": "256",
                "CurrentAvailableSystemMemory": "8192",
                "ConfigurableSystemMemory": "16384",
            },
        },
    }
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{SYSTEM_UUID}").respond(
        200, text=xml(system, "ManagedSystem")
    )
    description = AsyncMock(return_value="")
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.get_lpar_description", description
    )

    async def native(*args, **kwargs):
        nonlocal submitted
        submitted = True
        return ""

    command = AsyncMock(side_effect=native)
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    async with HMCClient(make_config()) as hmc:
        result = await change_lpar_memory(
            hmc,
            selector,
            LparMemoryChange(operation="add", quantity_mib=512),
            system_name_or_uuid=SYSTEM_UUID,
        )
    assert result["outcome"] == "applied"
    assert not wrong.called
    assert right.call_count >= 3
    description.assert_awaited_once()
    command.assert_awaited_once()
