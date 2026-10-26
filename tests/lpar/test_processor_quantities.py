"""D05 native processor quantities; constructed fixtures use captured field shapes."""

import csv
import io
import shlex
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig

CONFIG = HMCConfig.from_mapping({})
FIELDS = [
    "lpar_id",
    "lpar_name",
    "curr_proc_mode",
    "curr_min_procs",
    "curr_procs",
    "curr_max_procs",
    "pend_proc_mode",
    "pend_min_procs",
    "pend_procs",
    "pend_max_procs",
    "run_procs",
    "curr_min_proc_units",
    "curr_proc_units",
    "curr_max_proc_units",
    "pend_min_proc_units",
    "pend_proc_units",
    "pend_max_proc_units",
    "run_proc_units",
]
ROW = [
    "7",
    "lpar-R1",
    "shared",
    "1",
    "2",
    "4",
    "shared",
    "1",
    "3",
    "4",
    "2",
    "0.10",
    "0.50",
    "1.00",
    "0.10",
    "0.75",
    "1.00",
    "0.50",
]


@pytest.mark.parametrize("operation", ["add", "remove", "set"])
@pytest.mark.parametrize("units", [0, 1, 0.1, "0.10", "1.000", Decimal("12.34")])
def test_request_units_and_values(operation, units):
    from hmcpctl.documents.lpar_processors import LparProcessorChange

    change = LparProcessorChange(
        operation=operation, processors=0, processing_units=units
    )
    assert change.processors == 0
    assert change.processing_units == Decimal(str(units))
    with pytest.raises(ValueError):
        change.processors = 2


@pytest.mark.parametrize(
    "values",
    [
        {"operation": "add"},
        {"operation": "move", "processors": 1},
        *({"operation": "add", "processors": value} for value in [-1, True, 1.5, "1"]),
        *(
            {"operation": "add", "processing_units": value}
            for value in [
                True,
                -1,
                "NaN",
                "Infinity",
                "-Infinity",
                "0.001",
                0.001,
                [],
                "no",
            ]
        ),
        {"operation": "set", "processors": 1, "force": 1},
        {"operation": "set", "processors": 1, "pool": 1},
    ],
)
def test_request_units_and_invalid_values(values):
    from hmcpctl.documents.lpar_processors import LparProcessorChange

    with pytest.raises(ValueError):
        LparProcessorChange(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,native", [("add", "a"), ("remove", "r"), ("set", "s")]
)
@pytest.mark.parametrize("counts,units", [(0, None), (None, "0"), (2, "0.10")])
async def test_processor_native_commands(monkeypatch, operation, native, counts, units):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.ssh.processors import change_lpar_processor_quantity

    command = AsyncMock(return_value="result")
    monkeypatch.setattr("hmcpctl.ssh.processors.run_hmc_command", command)
    change = LparProcessorChange(
        operation=operation, processors=counts, processing_units=units, force=True
    )
    assert (
        await change_lpar_processor_quantity(CONFIG, "system; quoted ' name", 7, change)
        == "result"
    )
    expected = [
        "chhwres",
        "-r",
        "proc",
        "-m",
        "system; quoted ' name",
        "-o",
        native,
        "--id",
        "7",
    ]
    if counts is not None:
        expected += ["--procs", str(counts)]
    if units is not None:
        expected += ["--procunits", f"{Decimal(units):.2f}"]
    assert shlex.split(command.call_args.args[1]) == expected + ["--force"]
    assert command.call_args.kwargs == {
        "include_stderr": True,
        "max_output_bytes": 65536,
    }
    assert command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("partition_id", [0, -1, True, "7", 1.5])
async def test_processor_native_commands_refuse_ids(monkeypatch, partition_id):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.ssh.processors import (
        change_lpar_processor_quantity,
        read_lpar_processor_quantities,
    )

    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.processors.run_hmc_command", command)
    with pytest.raises(ValueError, match="partition_id"):
        await change_lpar_processor_quantity(
            CONFIG,
            "sys-R1",
            partition_id,
            LparProcessorChange(operation="add", processors=1),
        )
    with pytest.raises(ValueError, match="partition_id"):
        await read_lpar_processor_quantities(CONFIG, "sys-R1", partition_id)
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_native_commands_require_typed_request(monkeypatch):
    from hmcpctl.ssh.processors import change_lpar_processor_quantity

    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.processors.run_hmc_command", command)
    with pytest.raises(TypeError):
        await change_lpar_processor_quantity(CONFIG, "sys-R1", 7, {"operation": "set"})
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_read_projection(monkeypatch):
    from hmcpctl.ssh.processors import read_lpar_processor_quantities

    row = [*ROW]
    row[1] = 'lpar, "quoted"'
    output = io.StringIO()
    csv.writer(output).writerow(row)
    command = AsyncMock(return_value=output.getvalue())
    monkeypatch.setattr("hmcpctl.ssh.processors.run_hmc_command", command)
    assert await read_lpar_processor_quantities(
        CONFIG, "system; quoted ' name", 7
    ) == dict(zip(FIELDS, row, strict=True))
    assert shlex.split(command.call_args.args[1]) == [
        "lshwres",
        "-r",
        "proc",
        "-m",
        "system; quoted ' name",
        "--level",
        "lpar",
        "--filter",
        "lpar_ids=7",
        "-F",
        ",".join(FIELDS),
    ]
    assert command.call_args.kwargs == {"max_output_bytes": 65536}
    assert command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        "",
        ",".join(ROW[:-1]),
        ",".join(ROW + ["extra"]),
        ",".join(["8", *ROW[1:]]),
        ",".join(["07", *ROW[1:]]),
        ",".join(ROW) + "\n" + ",".join(ROW),
        '7,"unterminated',
        ",".join(["7", "", *ROW[2:]]),
    ],
)
async def test_processor_read_projection_refuses_malformed(monkeypatch, output):
    from hmcpctl.ssh.processors import read_lpar_processor_quantities
    from hmcpctl.ssh.transport import HMCCLIError

    monkeypatch.setattr(
        "hmcpctl.ssh.processors.run_hmc_command", AsyncMock(return_value=output)
    )
    with pytest.raises(HMCCLIError):
        await read_lpar_processor_quantities(CONFIG, "sys-R1", 7)


# Constructed boundaries preserve REST/native shapes from the cited V10R3/P9 captures.
SYSTEM_UUID = "cccc0000-0000-0000-0000-000000000001"
LPAR_UUID = "aaaa0000-0000-0000-0000-000000000001"


def processor_entry(dedicated=False):
    cpu = {
        "HasDedicatedProcessors": str(dedicated).lower(),
        "CurrentHasDedicatedProcessors": str(dedicated).lower(),
        "CurrentDedicatedProcessorConfiguration": {
            "CurrentMinimumProcessors": "1",
            "CurrentProcessors": "2",
            "CurrentMaximumProcessors": "4",
        },
        "DedicatedProcessorConfiguration": {
            "MinimumProcessors": "1",
            "DesiredProcessors": "3",
            "MaximumProcessors": "4",
        },
        "CurrentSharedProcessorConfiguration": {
            "CurrentMinimumVirtualProcessors": "1",
            "AllocatedVirtualProcessors": "2",
            "CurrentMaximumVirtualProcessors": "4",
            "CurrentMinimumProcessingUnits": "0.10",
            "CurrentProcessingUnits": "0.50",
            "CurrentMaximumProcessingUnits": "1.00",
        },
        "SharedProcessorConfiguration": {
            "MinimumVirtualProcessors": "1",
            "DesiredVirtualProcessors": "3",
            "MaximumVirtualProcessors": "4",
            "MinimumProcessingUnits": "0.10",
            "DesiredProcessingUnits": "0.75",
            "MaximumProcessingUnits": "1.00",
        },
    }
    return {
        "UUID": LPAR_UUID,
        "Resource": {
            "PartitionName": "lpar-R1",
            "PartitionID": "7",
            "PartitionType": "AIX/Linux",
            "PartitionState": "not activated",
            "ResourceMonitoringControlState": "inactive",
            "CurrentProfileSync": "Disabled",
            "IsOperationInProgress": "false",
            "PartitionCapabilities": {
                "DynamicLogicalPartitionProcessorCapable": "true"
            },
            "PartitionProcessorConfiguration": cpu,
            "PartitionMemoryConfiguration": {
                "CurrentMemory": "2048",
                "CurrentMinimumMemory": "1024",
                "CurrentMaximumMemory": "4096",
                "DesiredMemory": "2048",
                "MinimumMemory": "1024",
                "MaximumMemory": "4096",
                "RuntimeMemory": "0",
                "SharedMemoryEnabled": "false",
            },
        },
    }


def processor_system():
    return {
        "UUID": SYSTEM_UUID,
        "Resource": {
            "SystemName": "sys-R1",
            "State": "operating",
            "AssociatedSystemProcessorConfiguration": {
                "ConfigurableSystemProcessorUnits": "20",
                "CurrentAvailableSystemProcessorUnits": "16",
                "CurrentMaximumAllowedProcessorsPerPartition": "256",
                "MaximumAllowedVirtualProcessorsPerPartition": "256",
                "MinimumProcessorUnitsPerVirtualProcessor": "0.05",
                **{
                    f"CurrentMaximum{mode}ProcessorsPer{kind}Partition": "64"
                    for mode in ["", "Virtual"]
                    for kind in ["AIXOrLinux", "IBMi", "VirtualIOServer"]
                },
            },
        },
    }


def processor_row(dedicated=False):
    row = dict(zip(FIELDS, ROW, strict=True))
    if dedicated:
        row.update(curr_proc_mode="ded", pend_proc_mode="ded")
        row.update({key: "" for key in row if "units" in key})
    return row


def snapshot(dedicated=False):
    from hmcpctl.operations.lpar.processors import _snapshot

    return _snapshot(processor_entry(dedicated), LPAR_UUID, processor_row(dedicated))


@pytest.mark.parametrize("dedicated", [False, True])
@pytest.mark.parametrize(
    "operation,quantity,target", [("add", 1, 3), ("remove", 1, 1), ("set", 4, 4)]
)
def test_targets_use_current_by_mode(dedicated, operation, quantity, target):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    expected = _targets(
        snapshot(dedicated),
        LparProcessorChange(operation=operation, processors=quantity),
        processor_system(),
    )
    assert expected["processors"] == target
    assert expected["processing_units"] == (None if dedicated else Decimal("0.50"))


@pytest.mark.parametrize(
    "operation,amount,target",
    [("add", "0.10", "0.60"), ("remove", "0.10", "0.40"), ("set", "0.30", "0.30")],
)
def test_targets_decimal_entitlement(operation, amount, target):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    expected = _targets(
        snapshot(),
        LparProcessorChange(operation=operation, processing_units=amount),
        processor_system(),
    )
    assert expected["processors"] == 2
    assert expected["processing_units"] == Decimal(target)


@pytest.mark.parametrize(
    "values,field,value",
    [
        ({"processors": 5}, None, None),
        ({"processing_units": "1.01"}, None, None),
        ({"processing_units": "0.15", "processors": 4}, None, None),
        ({"processing_units": "0.60"}, "CurrentAvailableSystemProcessorUnits", "0"),
        ({"processors": 3}, "MaximumAllowedVirtualProcessorsPerPartition", "2"),
        (
            {"processors": 3},
            "CurrentMaximumVirtualProcessorsPerAIXOrLinuxPartition",
            "2",
        ),
        ({"processors": 3}, "MinimumProcessorUnitsPerVirtualProcessor", "0.20"),
        ({"processors": 2}, "ConfigurableSystemProcessorUnits", "0"),
        ({"processors": 2}, "CurrentAvailableSystemProcessorUnits", "NaN"),
    ],
)
def test_capacity_and_ratio_refusals(values, field, value):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    system = processor_system()
    if field:
        system["Resource"]["AssociatedSystemProcessorConfiguration"][field] = value
    with pytest.raises(ValueError):
        _targets(snapshot(), LparProcessorChange(operation="set", **values), system)


@pytest.fixture
def processor_boundary(monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace

    from hmcpctl.operations.lpar import processors

    b = SimpleNamespace(
        before=processor_entry(),
        after=processor_entry(),
        system=processor_system(),
        row=processor_row(),
        after_row=processor_row(),
        changed=False,
    )
    b.after["Resource"]["PartitionProcessorConfiguration"][
        "CurrentSharedProcessorConfiguration"
    ]["CurrentProcessingUnits"] = "0.60"
    b.after_row["curr_proc_units"] = "0.60"

    async def read(uuid):
        return deepcopy(b.after if b.changed else b.before)

    async def native(*args, **kwargs):
        return deepcopy(b.after_row if b.changed else b.row)

    async def submit(*args, **kwargs):
        b.changed = True

    b.hmc = SimpleNamespace(
        config=CONFIG,
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM_UUID, "SystemName": "sys-R1"}]
        ),
        list_logical_partitions=AsyncMock(return_value=[b.before]),
        list_vios=AsyncMock(return_value=[]),
        get_logical_partition=AsyncMock(side_effect=read),
        get_vios=AsyncMock(side_effect=read),
        get_managed_system=AsyncMock(side_effect=lambda uuid: deepcopy(b.system)),
    )
    b.read = AsyncMock(side_effect=native)
    b.write = AsyncMock(side_effect=submit)
    b.description = AsyncMock(return_value="[hmcpctl owner:hmcpctl created:2026-01-01]")
    monkeypatch.setattr(processors, "read_lpar_processor_quantities", b.read)
    monkeypatch.setattr(processors, "change_lpar_processor_quantity", b.write)
    monkeypatch.setattr("hmcpctl.ssh.profiles.run_hmc_command", b.description)
    return b


async def cpu_change(b, **values):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import change_lpar_processors

    return await change_lpar_processors(
        b.hmc, "lpar-R1", LparProcessorChange(**values), system_name_or_uuid="sys-R1"
    )


@pytest.mark.asyncio
async def test_independent_matching_outcome(processor_boundary):
    b = processor_boundary
    result = await cpu_change(b, operation="add", processing_units="0.10")
    assert result["outcome"] == "applied" and result["workflow_completed"]
    assert result["expected"]["processing_units"] == "0.60"
    assert result["before"]["current_processing_units"] == "0.50"
    assert b.write.await_count == 1 and b.read.await_count == 3
    assert b.description.call_args.kwargs["max_output_bytes"] == 65536


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionState", "migrating"),
        ("PartitionType", "unknown"),
        ("IsOperationInProgress", "true"),
        ("IsOperationInProgress", None),
        ("PartitionName", "other"),
    ],
)
async def test_admission_guards(processor_boundary, field, value):
    b = processor_boundary
    b.before["Resource"][field] = value
    with pytest.raises(ValueError):
        await cpu_change(b, operation="add", processors=1)
    b.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_admission_ownership_denial(processor_boundary):
    b = processor_boundary
    b.description.return_value = "[hmcpctl owner:other created:2026-01-01]"
    with pytest.raises(PermissionError):
        await cpu_change(b, operation="add", processors=1)
    b.read.assert_not_awaited()
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("CurrentProfileSync", "Enabled"),
        ("PartitionState", "running"),
        ("PartitionName", "renamed"),
    ],
)
async def test_changed_evidence_prevents_submission(processor_boundary, field, value):
    from copy import deepcopy

    b = processor_boundary
    changed = deepcopy(b.before)
    changed["Resource"][field] = value
    b.hmc.get_logical_partition.side_effect = [b.before, changed]
    with pytest.raises(ValueError):
        await cpu_change(b, operation="add", processing_units="0.10")
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,quantity", [("add", "0"), ("remove", "0"), ("set", "0.50")]
)
async def test_zero_noop_is_authorized_and_rechecked(
    processor_boundary, operation, quantity
):
    b = processor_boundary
    result = await cpu_change(b, operation=operation, processing_units=quantity)
    assert result["outcome"] == "noop" and result["workflow_completed"]
    assert b.read.await_count == 2 and b.description.await_count == 1
    b.write.assert_not_awaited()


@pytest.mark.parametrize(
    "state,values,reset",
    [
        ("not activated", {"processing_units": "0"}, True),
        ("not activated", {"processors": 0}, True),
        ("running", {"processors": 0}, False),
        ("not activated", {"processors": 0, "processing_units": "0.60"}, False),
    ],
)
def test_zero_and_coupled_reset(state, values, reset):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    before = snapshot()
    before.update(state=state, rmc="active")
    if reset:
        expected = _targets(
            before, LparProcessorChange(operation="set", **values), processor_system()
        )
        assert (
            expected["reset"]
            and expected["processors"] == expected["processing_units"] == 0
        )
        assert not expected["noop"]
    else:
        with pytest.raises(ValueError):
            _targets(
                before,
                LparProcessorChange(operation="set", **values),
                processor_system(),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,outcome",
    [
        ("unchanged", "unverified"),
        ("partial", "partial"),
        ("matching", "applied"),
        ("unreadable", "unverified"),
    ],
)
async def test_independent_uncertain_mutation_never_replays(
    processor_boundary, failure, outcome
):
    from hmcpctl.ssh.transport import HMCCLIError

    b = processor_boundary

    async def submit(*args, **kwargs):
        b.changed = failure != "unchanged"
        if failure == "partial":
            b.after_row["curr_proc_units"] = "0.55"
            b.after["Resource"]["PartitionProcessorConfiguration"][
                "CurrentSharedProcessorConfiguration"
            ]["CurrentProcessingUnits"] = "0.55"
        if failure == "unreadable":
            b.read.side_effect = HMCCLIError("post-read output limit exceeded")
        raise HMCCLIError("Output limit exceeded; completion unknown")

    b.write.side_effect = submit
    result = await cpu_change(b, operation="add", processing_units="0.10")
    assert result["outcome"] == outcome and result["submission_error"]
    assert b.write.await_count == 1


@pytest.mark.asyncio
async def test_profile_and_deferred_warnings(processor_boundary):
    b = processor_boundary
    for entry in [b.before, b.after]:
        entry["Resource"].update(
            PartitionState="running", ResourceMonitoringControlState="inactive"
        )
    result = await cpu_change(b, operation="add", processing_units="0.10", force=True)
    assert result["outcome"] == "applied"
    assert any("restart" in warning for warning in result["warnings"])
    assert result["change_location"]["lives_in"] == "current-configuration"


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_procs", "NaN"),
        ("curr_procs", "3"),
        ("curr_proc_mode", "other"),
        ("pend_proc_mode", "ded"),
        ("pend_proc_units", "0.50"),
        ("run_proc_units", "0.001"),
        ("lpar_name", "other"),
    ],
)
def test_admission_native_evidence_refuses(field, value):
    from hmcpctl.operations.lpar.processors import _snapshot

    row = processor_row()
    row[field] = value
    with pytest.raises(ValueError):
        _snapshot(processor_entry(), LPAR_UUID, row)


@pytest.mark.parametrize(
    "kind,rmc,capability,force,accepted",
    [
        ("AIX/Linux", "active", "true", False, True),
        ("Virtual IO Server", "active", "true", False, True),
        ("OS400", "inactive", "true", False, True),
        ("OS400", "active", "true", True, False),
        ("AIX/Linux", "inactive", "true", False, False),
        ("AIX/Linux", "active", None, False, False),
        ("AIX/Linux", "inactive", None, True, False),
        ("AIX/Linux", "inactive", "false", True, True),
    ],
)
def test_admission_running_contexts(kind, rmc, capability, force, accepted):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    before = snapshot()
    before.update(
        state="running", partition_type=kind, rmc=rmc, dynamic_processor=capability
    )
    change = LparProcessorChange(operation="add", processing_units="0.10", force=force)
    if accepted:
        assert _targets(before, change, processor_system())[
            "processing_units"
        ] == Decimal("0.60")
    else:
        with pytest.raises(ValueError):
            _targets(before, change, processor_system())


@pytest.mark.parametrize(
    "damage", ["none", "memory", "cpu", "runtime", "omitted", "bounds", "identity"]
)
def test_independent_postconditions(damage):
    from copy import deepcopy

    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _postconditions, _targets

    before = snapshot()
    before.update(state="running", rmc="active")
    expected = _targets(
        before,
        LparProcessorChange(operation="add", processing_units="0.10"),
        processor_system(),
    )
    after = deepcopy(before)
    after.update(
        current_processing_units=Decimal("0.60"),
        runtime_processing_units=Decimal("0.60"),
    )
    changes = {
        "memory": ("current_memory_mib", 1024),
        "cpu": ("current_processing_units", Decimal("0.55")),
        "runtime": ("runtime_processors", 1),
        "omitted": ("current_processors", 3),
        "bounds": ("current_maximum_processors", 5),
        "identity": ("uuid", "other"),
    }
    if damage in changes:
        key, value = changes[damage]
        after[key] = value
    if damage == "identity":
        with pytest.raises(ValueError):
            _postconditions(before, after, expected)
    else:
        assert _postconditions(before, after, expected) is (damage == "none")


@pytest.mark.parametrize(
    "remaining",
    [
        None,
        "current_memory_mib",
        "current_minimum_mib",
        "current_maximum_mib",
        "current_minimum_processors",
        "current_processors",
        "current_maximum_processors",
        "current_minimum_processing_units",
        "current_processing_units",
        "current_maximum_processing_units",
    ],
)
def test_zero_complete_reset_required(remaining):
    from copy import deepcopy

    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _postconditions, _targets

    before = snapshot()
    expected = _targets(
        before, LparProcessorChange(operation="set", processors=0), processor_system()
    )
    after = deepcopy(before)
    for key in [
        "current_memory_mib",
        "current_minimum_mib",
        "current_maximum_mib",
        "current_minimum_processors",
        "current_processors",
        "current_maximum_processors",
        "current_minimum_processing_units",
        "current_processing_units",
        "current_maximum_processing_units",
    ]:
        after[key] = 1 if key == remaining else 0
    assert _postconditions(before, after, expected) is (remaining is None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exit_status,signal,outcome",
    [
        (3, None, "rejected"),
        (-1, ("TERM", False, "", ""), "unverified"),
        (None, None, "unverified"),
    ],
)
async def test_independent_exit_classification(
    processor_boundary, exit_status, signal, outcome
):
    import asyncssh

    from hmcpctl.ssh.transport import HMCCLIError

    b = processor_boundary
    error = HMCCLIError("native refusal")
    error.__cause__ = asyncssh.ProcessError(
        {}, "chhwres", None, exit_status, signal, exit_status, "", "refusal"
    )
    b.write.side_effect = error
    result = await cpu_change(b, operation="add", processing_units="0.10")
    assert result["outcome"] == outcome
    assert b.write.await_count == 1


@pytest.mark.asyncio
async def test_independent_cancellation_propagates(processor_boundary):
    import asyncio

    b = processor_boundary
    b.write.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await cpu_change(b, operation="add", processing_units="0.10")
    assert b.write.await_count == 1 and b.read.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout_size,stderr_size,overflow",
    [(65537, 0, True), (0, 65537, True), (32768, 32769, True), (65536, 0, False)],
)
async def test_ownership_precheck_overflow(
    processor_boundary, monkeypatch, stdout_size, stderr_size, overflow
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import asyncssh

    from hmcpctl.ssh.transport import HMCCLIError, run_hmc_command

    b = processor_boundary
    b.hmc.config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "u", "password": "p"}
    )

    def reader(data):
        stream = asyncio.StreamReader()
        stream.feed_data(data)
        stream.feed_eof()
        return stream

    stamp = b"[hmcpctl owner:hmcpctl created:2026-01-01]"
    output = (stamp + b" " * stdout_size)[:stdout_size]
    process = SimpleNamespace(
        stdout=reader(output),
        stderr=reader(b"x" * stderr_size),
        env={},
        command="lssyscfg",
        subsystem=None,
        exit_status=0,
        exit_signal=None,
        returncode=0,
        close=MagicMock(),
        wait_closed=AsyncMock(),
    )
    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.create_process.return_value = process
    conn.abort = MagicMock()
    conn.run.side_effect = AssertionError("unbounded ownership collector")
    monkeypatch.setattr(asyncssh, "connect", MagicMock(return_value=conn))
    monkeypatch.setattr("hmcpctl.ssh.profiles.run_hmc_command", run_hmc_command)
    if overflow:
        with pytest.raises(HMCCLIError, match="ownership"):
            await cpu_change(b, operation="add", processing_units="0.10")
        conn.abort.assert_called_once()
        b.read.assert_not_awaited()
        b.write.assert_not_awaited()
    else:
        assert (await cpu_change(b, operation="add", processing_units="0.10"))[
            "outcome"
        ] == "applied"
        conn.abort.assert_not_called()
    conn.create_process.assert_awaited_once()
    conn.run.assert_not_awaited()


@pytest.mark.parametrize("dedicated", [True, False])
def test_targets_zero_unallocated_envelope_can_use_configured_bounds(dedicated):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    before = snapshot(dedicated)
    for key in list(before):
        if key.startswith("current_") and (
            key.endswith(("processors", "processing_units"))
        ):
            before[key] = 0
    change = LparProcessorChange(
        operation="add", processors=2, processing_units=None if dedicated else "0.50"
    )
    target = _targets(before, change, processor_system())
    assert target["processors"] == 2 and not target["reset"]


@pytest.mark.parametrize("quantity", ["processors", "processing_units"])
def test_targets_subtraction_beyond_current_refuses(quantity):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    with pytest.raises(ValueError, match="subtraction"):
        _targets(
            snapshot(),
            LparProcessorChange(operation="remove", **{quantity: 3}),
            processor_system(),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("CurrentMaximumAllowedProcessorsPerPartition", "2"),
        ("CurrentMaximumProcessorsPerAIXOrLinuxPartition", "2"),
        ("CurrentAvailableSystemProcessorUnits", "0"),
    ],
)
def test_capacity_dedicated_limits(field, value):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    system = processor_system()
    system["Resource"]["AssociatedSystemProcessorConfiguration"][field] = value
    with pytest.raises(ValueError):
        _targets(
            snapshot(True), LparProcessorChange(operation="add", processors=1), system
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["system", "containment", "name_collision", "current", "cpu_mode"]
)
async def test_changed_evidence_capacity_containment_and_cpu(
    processor_boundary, change
):
    from copy import deepcopy

    b = processor_boundary
    if change == "system":
        other = deepcopy(b.system)
        other["Resource"]["AssociatedSystemProcessorConfiguration"][
            "CurrentAvailableSystemProcessorUnits"
        ] = "15"
        b.hmc.get_managed_system.side_effect = [b.system, other]
    elif change == "containment":
        b.hmc.list_logical_partitions.side_effect = [[b.before], []]
    elif change == "name_collision":
        normal = [{"UUID": SYSTEM_UUID, "SystemName": "sys-R1"}]
        collision = [*normal, {"UUID": LPAR_UUID, "SystemName": "sys-R1"}]
        b.hmc.list_system_summaries.side_effect = [normal, normal, normal, collision]
    else:
        other = deepcopy(b.row)
        other["curr_procs" if change == "current" else "curr_proc_mode"] = (
            "3" if change == "current" else "ded"
        )
        b.read.side_effect = [b.row, other]
    with pytest.raises(ValueError):
        await cpu_change(b, operation="add", processing_units="0.10")
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["pre", "post"])
async def test_independent_read_overflow(processor_boundary, where):
    from hmcpctl.ssh.transport import HMCCLIError

    b = processor_boundary
    if where == "pre":
        b.read.side_effect = HMCCLIError("read overflow")
        with pytest.raises(HMCCLIError):
            await cpu_change(b, operation="add", processing_units="0.10")
        b.write.assert_not_awaited()
    else:
        b.read.side_effect = [b.row, b.row, HMCCLIError("read overflow")]
        result = await cpu_change(b, operation="add", processing_units="0.10")
        assert result["outcome"] == "unverified" and not result["workflow_completed"]
        assert b.write.await_count == 1


@pytest.mark.asyncio
async def test_admission_vios_uses_own_contained_reader(processor_boundary):
    b = processor_boundary
    b.before["Resource"]["PartitionType"] = b.after["Resource"]["PartitionType"] = (
        "Virtual IO Server"
    )
    b.hmc.list_vios.return_value = [b.before]
    b.hmc.list_logical_partitions.return_value = []
    result = await cpu_change(b, operation="add", processing_units="0.10")
    assert result["outcome"] == "applied"
    assert b.hmc.get_vios.await_count == 3
    b.hmc.get_logical_partition.assert_not_awaited()


@pytest.mark.asyncio
async def test_ownership_override_is_audited_and_skips_only_description(
    processor_boundary, monkeypatch
):
    from unittest.mock import MagicMock

    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import change_lpar_processors

    b = processor_boundary
    audit = MagicMock()
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.audit.record_ownership_override", audit
    )
    result = await change_lpar_processors(
        b.hmc,
        "lpar-R1",
        LparProcessorChange(operation="add", processing_units="0.10"),
        system_name_or_uuid="sys-R1",
        ownership_override=True,
    )
    assert result["outcome"] == "applied"
    audit.assert_called_once()
    b.description.assert_not_awaited()
    assert b.read.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    [
        "foreign",
        "ambiguous",
        "wrong_kind",
        "system_uuid",
        "system_name",
        "system_state",
    ],
)
async def test_admission_containment_and_system_identity(processor_boundary, defect):
    b = processor_boundary
    if defect == "foreign":
        b.hmc.list_logical_partitions.return_value = []
    elif defect == "ambiguous":
        b.hmc.list_vios.return_value = [b.before]
    elif defect == "wrong_kind":
        b.before["Resource"]["PartitionType"] = "Virtual IO Server"
    elif defect == "system_uuid":
        b.system["UUID"] = LPAR_UUID
    elif defect == "system_name":
        b.system["Resource"]["SystemName"] = "other"
    else:
        b.system["Resource"]["State"] = "powering off"
    with pytest.raises(ValueError):
        await cpu_change(b, operation="add", processing_units="0.10")
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dedicated,partial", [(False, False), (True, False), (False, True), (True, True)]
)
async def test_zero_reset_independent_complete_readback(
    processor_boundary, dedicated, partial
):
    b = processor_boundary
    b.before = processor_entry(dedicated)
    b.after = processor_entry(dedicated)
    b.hmc.list_logical_partitions.return_value = [b.before]
    b.row, b.after_row = processor_row(dedicated), processor_row(dedicated)
    cpu = b.after["Resource"]["PartitionProcessorConfiguration"]
    container = cpu[
        "CurrentDedicatedProcessorConfiguration"
        if dedicated
        else "CurrentSharedProcessorConfiguration"
    ]
    for field in container:
        container[field] = "0"
    memory = b.after["Resource"]["PartitionMemoryConfiguration"]
    for field in ("CurrentMemory", "CurrentMinimumMemory", "CurrentMaximumMemory"):
        memory[field] = "0"
    if partial:
        memory.update(
            CurrentMemory="2048",
            CurrentMinimumMemory="1024",
            CurrentMaximumMemory="4096",
        )
    for field in b.after_row:
        if field.startswith("curr_") and field != "curr_proc_mode":
            b.after_row[field] = "" if dedicated and "units" in field else "0"
    result = await cpu_change(b, operation="set", processors=0)
    assert result["outcome"] == ("partial" if partial else "applied")
    assert any("CPU and memory" in warning for warning in result["warnings"])
    assert b.write.await_count == 1 and b.read.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["0.5", "0.500"])
async def test_independent_equivalent_decimal_spelling_is_unchanged(
    processor_boundary, spelling
):
    import asyncssh

    from hmcpctl.ssh.transport import HMCCLIError

    b = processor_boundary
    b.before["Resource"]["PartitionProcessorConfiguration"][
        "CurrentSharedProcessorConfiguration"
    ]["CurrentProcessingUnits"] = spelling
    b.after["Resource"]["PartitionProcessorConfiguration"][
        "CurrentSharedProcessorConfiguration"
    ]["CurrentProcessingUnits"] = "0.50"
    b.after_row["curr_proc_units"] = "0.50"

    async def reject(*args, **kwargs):
        b.changed = True
        raise HMCCLIError("native refusal") from asyncssh.ProcessError(
            {}, "chhwres", None, 3, None, 3, "", "refusal"
        )

    b.write.side_effect = reject
    result = await cpu_change(b, operation="add", processing_units="0.10")
    assert result["outcome"] == "rejected"
    assert result["before"]["processors"]["CurrentProcessingUnits"] == "0.50"
    assert result["before"] == result["after"]
    assert b.write.await_count == 1


def test_independent_zero_units_have_one_canonical_spelling():
    from hmcpctl.operations.lpar.processors import _snapshot

    entry, row = processor_entry(), processor_row()
    cpu = entry["Resource"]["PartitionProcessorConfiguration"][
        "CurrentSharedProcessorConfiguration"
    ]
    for field in cpu:
        cpu[field] = "-0.00" if "Units" in field else "0"
    for field in row:
        if field.startswith("curr_") and field != "curr_proc_mode":
            row[field] = "0"
    evidence = _snapshot(entry, LPAR_UUID, row)
    assert evidence["processors"]["CurrentProcessingUnits"] == "0.00"
    assert f"{evidence['current_processing_units']:.2f}" == "0.00"


@pytest.mark.parametrize(
    "value,whole,accepted",
    [
        ("1e2", False, True),
        ("1.000", False, True),
        ("0e999999", False, True),
        ("-0.00", False, True),
        ("1e1051", False, True),
        ("1e1052", False, False),
        ("1e1056", True, True),
        ("1e1057", True, False),
        ("0" * 1057, False, True),
        ("0" * 1058, False, False),
        ("٠" * 529, False, False),
        (Decimal("1e1052"), False, False),
        (10**1057, True, False),
        ("0.001", False, False),
        ("1e-999999", False, False),
        ("NaN", False, False),
        ("Infinity", False, False),
        (True, True, False),
    ],
    ids=[
        "small-exponent",
        "trailing-zeros",
        "extreme-zero",
        "negative-zero",
        "unit-limit",
        "unit-over",
        "whole-limit",
        "whole-over",
        "raw-limit",
        "raw-over",
        "utf8-over",
        "decimal-over",
        "integer-over",
        "fraction",
        "tiny-exponent",
        "nan",
        "infinite",
        "boolean",
    ],
)
def test_numeric_representation_before_conversion(value, whole, accepted):
    import json

    from hmcpctl.operations.lpar.processors import _number

    if not accepted:
        with pytest.raises(ValueError):
            _number(value, "quantity", whole=whole)
        return
    number = _number(value, "quantity", whole=whole)
    token = json.dumps(number if whole else f"{number:.2f}")
    assert len(token.encode()) <= 65536 // 62
    if not number:
        assert token == ("0" if whole else '"0.00"')


@pytest.mark.parametrize(
    "values",
    [
        {"processing_units": "1e1052"},
        {"processing_units": Decimal("1e1052")},
        {"processing_units": "0" * 1058},
        {"processors": 10**1057},
    ],
    ids=["unit-exponent", "decimal-exponent", "raw-size", "whole-size"],
)
def test_numeric_representation_typed_request(values):
    from hmcpctl.documents.lpar_processors import LparProcessorChange

    with pytest.raises(ValueError):
        LparProcessorChange(operation="add", **values)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["processing_units", "processors", "partition_id"])
async def test_numeric_representation_native_builder(monkeypatch, field):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.ssh.processors import change_lpar_processor_quantity

    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.processors.run_hmc_command", command)
    # Deliberately bypass the model to verify the command boundary independently.
    change = LparProcessorChange.model_construct(
        operation="set", processing_units=Decimal("1e1052")
    )
    if field != "processing_units":
        change = LparProcessorChange.model_construct(
            operation="set", processors=10**1057 if field == "processors" else 1
        )
    with pytest.raises(ValueError):
        await change_lpar_processor_quantity(
            CONFIG, "sys-R1", 10**1057 if field == "partition_id" else 7, change
        )
    command.assert_not_awaited()


NUMERIC_SITES = [
    *(("native", field) for field in FIELDS if field.endswith(("procs", "proc_units"))),
    ("resource", "PartitionID"),
    *(
        ("memory", field)
        for field in (
            "CurrentMemory",
            "CurrentMinimumMemory",
            "CurrentMaximumMemory",
            "DesiredMemory",
            "MinimumMemory",
            "MaximumMemory",
            "RuntimeMemory",
            "CurrentEntitledMemory",
            "RuntimeEntitledMemory",
        )
    ),
    *(
        ("current", field)
        for field in processor_entry()["Resource"]["PartitionProcessorConfiguration"][
            "CurrentSharedProcessorConfiguration"
        ]
    ),
    *(
        ("pending", field)
        for field in processor_entry()["Resource"]["PartitionProcessorConfiguration"][
            "SharedProcessorConfiguration"
        ]
    ),
    *(
        ("capacity", field)
        for field in (
            "ConfigurableSystemProcessorUnits",
            "CurrentAvailableSystemProcessorUnits",
            "MaximumAllowedVirtualProcessorsPerPartition",
            "CurrentMaximumVirtualProcessorsPerAIXOrLinuxPartition",
            "MinimumProcessorUnitsPerVirtualProcessor",
        )
    ),
]


def pad_numeric_observation(b, site, field, *, after=False):
    entry = b.after if after else b.before
    resource = entry["Resource"]
    containers = {
        "native": b.after_row if after else b.row,
        "resource": resource,
        "memory": resource["PartitionMemoryConfiguration"],
        "current": resource["PartitionProcessorConfiguration"][
            "CurrentSharedProcessorConfiguration"
        ],
        "pending": resource["PartitionProcessorConfiguration"][
            "SharedProcessorConfiguration"
        ],
        "capacity": b.system["Resource"]["AssociatedSystemProcessorConfiguration"],
    }
    container = containers[site]
    container[field] = "0" * 1057 + container.get(field, "1")


@pytest.mark.asyncio
@pytest.mark.parametrize("site,field", NUMERIC_SITES)
async def test_numeric_representation_noop_refuses_all_readers(
    processor_boundary, site, field
):
    b = processor_boundary
    pad_numeric_observation(b, site, field)
    with pytest.raises(ValueError):
        await cpu_change(b, operation="add", processing_units="0")
    b.write.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("site,field", [s for s in NUMERIC_SITES if s[0] != "capacity"])
async def test_numeric_representation_postread_is_unverified(
    processor_boundary, site, field
):
    b = processor_boundary
    pad_numeric_observation(b, site, field, after=True)
    result = await cpu_change(b, operation="add", processing_units=".10")
    assert result["outcome"] == "unverified" and result["after"] is None
    assert not result["workflow_completed"] and b.write.await_count == 1
    assert all("0" * 30 not in warning for warning in result["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,accepted", [("1e2", True), ("1e1051", True), ("1e1052", False)]
)
async def test_numeric_representation_actual_noop_result(
    processor_boundary, token, accepted
):
    import json

    b = processor_boundary
    b.row["run_proc_units"] = token
    if not accepted:
        with pytest.raises(ValueError):
            await cpu_change(b, operation="add", processing_units="0")
    else:
        result = await cpu_change(b, operation="add", processing_units="0")
        assert result["outcome"] == "noop"
        assert len(json.dumps(result["before"]["runtime_processing_units"])) <= 1057
        assert result["before"] == result["after"]
    b.write.assert_not_awaited()


def test_numeric_representation_result_budget_and_guard():
    import json

    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _result, _snapshot, _targets

    entry = processor_entry()
    entry["Resource"]["PartitionMemoryConfiguration"].update(
        CurrentEntitledMemory="1", RuntimeEntitledMemory="1"
    )
    before = _snapshot(entry, LPAR_UUID, processor_row())
    expected = _targets(
        before, LparProcessorChange(operation="add", processors=0), processor_system()
    )
    result = _result(before, before, expected, "noop")
    numeric_tokens = []
    for key, original in [
        ("before", before),
        ("after", before),
        ("expected", expected),
    ]:
        numeric_tokens.extend(
            json.dumps(result[key][name])
            for name, value in original.items()
            if type(value) in (int, Decimal)
        )
        if isinstance(result[key].get("processors"), dict):
            numeric_tokens.extend(
                json.dumps(value) for value in result[key]["processors"].values()
            )
    assert len(numeric_tokens) == 62
    assert sum(len(token.encode()) for token in numeric_tokens) <= 65536


def large_processor_capacity():
    before, system = snapshot(), processor_system()
    before.update(
        current_processors=10**31,
        current_processing_units=Decimal("1e30"),
        current_maximum_processing_units=Decimal("1e31"),
        pending_maximum_processing_units=Decimal("1e31"),
    )
    system["Resource"]["AssociatedSystemProcessorConfiguration"].update(
        ConfigurableSystemProcessorUnits="1e32",
        CurrentAvailableSystemProcessorUnits="1e32",
        MaximumAllowedVirtualProcessorsPerPartition="1e32",
        CurrentMaximumVirtualProcessorsPerAIXOrLinuxPartition="1e32",
    )
    return before, system


def test_numeric_representation_arithmetic_is_exact_and_local():
    from decimal import getcontext

    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    before, system = large_processor_capacity()
    old_precision = getcontext().prec
    target = _targets(
        before, LparProcessorChange(operation="add", processing_units=".10"), system
    )
    assert target["processing_units"] == Decimal("1000000000000000000000000000000.10")
    assert not target["noop"] and getcontext().prec == old_precision


def test_numeric_representation_respects_integer_json_limit():
    import os
    import subprocess
    import sys

    code = """
import json
from hmcpctl.documents.lpar_processors import LparProcessorChange
from hmcpctl.operations.lpar.processors import _number
assert len(json.dumps(_number('1e639', 'count', whole=True))) == 640
for call in [lambda: _number('1e640', 'count', whole=True),
             lambda: LparProcessorChange(operation='add', processors=10**640)]:
    try: call()
    except ValueError: pass
    else: raise AssertionError('numeric boundary ignored configured JSON integer limit')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONINTMAXSTRDIGITS": "640"},
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("constraint", ["floor", "capacity"])
def test_numeric_representation_arithmetic_constraints(constraint):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _targets

    before, system = large_processor_capacity()
    if constraint == "floor":
        before["current_processors"] = 2 * 10**30 + 1
        units = "100000000000000000000000000000.04"
        message = "units per virtual processor"
    else:
        units = "2000000000000000000000000000000.10"
        system["Resource"]["AssociatedSystemProcessorConfiguration"][
            "CurrentAvailableSystemProcessorUnits"
        ] = "1000000000000000000000000000000.05"
        message = "available system capacity"
    with pytest.raises(ValueError, match=message):
        _targets(
            before, LparProcessorChange(operation="set", processing_units=units), system
        )


@pytest.mark.parametrize("site", ["current", "pending", "count-limit", "os-limit"])
def test_numeric_representation_dedicated_readers(site):
    from hmcpctl.operations.lpar.processors import _capacity, _snapshot

    entry, row, system = processor_entry(True), processor_row(True), processor_system()
    if site in ("current", "pending"):
        config = entry["Resource"]["PartitionProcessorConfiguration"]
        name, field = (
            ("CurrentDedicatedProcessorConfiguration", "CurrentProcessors")
            if site == "current"
            else ("DedicatedProcessorConfiguration", "DesiredProcessors")
        )
        config[name][field] = "0" * 1057 + config[name][field]
    else:
        config = system["Resource"]["AssociatedSystemProcessorConfiguration"]
        field = (
            "CurrentMaximumAllowedProcessorsPerPartition"
            if site == "count-limit"
            else "CurrentMaximumProcessorsPerAIXOrLinuxPartition"
        )
        config[field] = "0" * 1057 + config[field]
    with pytest.raises(ValueError):
        _capacity(system, _snapshot(entry, LPAR_UUID, row))


@pytest.mark.parametrize(
    "field,value",
    [("runtime_processors", 10**1057), ("runtime_processing_units", Decimal("1e1052"))],
)
def test_numeric_representation_result_rechecks_observations(field, value):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.operations.lpar.processors import _result, _targets

    before = snapshot()
    expected = _targets(
        before, LparProcessorChange(operation="add", processors=0), processor_system()
    )
    before[field] = value
    with pytest.raises(ValueError):
        _result(before, before, expected, "noop")


@pytest.mark.parametrize("site", ["id", "memory", "cpu"])
def test_numeric_representation_precedes_inherited_snapshot(monkeypatch, site):
    from unittest.mock import Mock

    from hmcpctl.operations.lpar import processors

    entry = processor_entry()
    resource = entry["Resource"]
    if site == "id":
        container, field = resource, "PartitionID"
    elif site == "memory":
        container, field = resource["PartitionMemoryConfiguration"], "RuntimeMemory"
    else:
        container = resource["PartitionProcessorConfiguration"][
            "CurrentSharedProcessorConfiguration"
        ]
        field = "CurrentProcessingUnits"
    container[field] = "0" * 1057 + container[field]
    inherited = Mock(wraps=processors.memory._snapshot)
    monkeypatch.setattr(processors.memory, "_snapshot", inherited)
    with pytest.raises(ValueError):
        processors._snapshot(entry, LPAR_UUID, processor_row())
    inherited.assert_not_called()


@pytest.mark.parametrize("value,whole", [("1.001", False), ("1.5", True)])
def test_numeric_representation_never_rounds(value, whole):
    from hmcpctl.operations.lpar.processors import _number

    with pytest.raises(ValueError):
        _number(value, "quantity", whole=whole)


def zero_processor_boundary(b, dedicated):
    b.before, b.after = processor_entry(dedicated), processor_entry(dedicated)
    b.row, b.after_row = processor_row(dedicated), processor_row(dedicated)
    b.hmc.list_logical_partitions.return_value = [b.before]
    name = (
        "CurrentDedicatedProcessorConfiguration"
        if dedicated
        else "CurrentSharedProcessorConfiguration"
    )
    current = b.before["Resource"]["PartitionProcessorConfiguration"][name]
    for field in current:
        current[field] = "0"
    for field in b.row:
        if field.startswith("curr_") and field != "curr_proc_mode":
            b.row[field] = "" if dedicated and "units" in field else "0"
    for entry, row in [(b.before, b.row), (b.after, b.after_row)]:
        for field in (
            "CurrentMinimumMemory",
            "CurrentMemory",
            "CurrentMaximumMemory",
            "RuntimeMemory",
        ):
            entry["Resource"]["PartitionMemoryConfiguration"][field] = "0"
        row["run_procs"] = "0"
        if not dedicated:
            row["run_proc_units"] = "0"
    return current, b.after["Resource"]["PartitionProcessorConfiguration"][name]


@pytest.mark.asyncio
@pytest.mark.parametrize("dedicated", [True, False])
@pytest.mark.parametrize("operation", ["add", "set"])
@pytest.mark.parametrize(
    "minimum,maximum,unit_minimum,unit_maximum",
    [
        ("1", "4", "0.10", "1.00"),
        ("2", "3", "0.20", "0.80"),
        ("0", "5", "0.00", "2.00"),
    ],
    ids=["configured-limits", "different-limits", "no-configured-limit-mapping"],
)
async def test_independent_zero_envelope_initialization(
    processor_boundary,
    dedicated,
    operation,
    minimum,
    maximum,
    unit_minimum,
    unit_maximum,
):
    b = processor_boundary
    _, after = zero_processor_boundary(b, dedicated)
    suffix = "Processors" if dedicated else "VirtualProcessors"
    after["CurrentMinimum" + suffix] = minimum
    after["CurrentMaximum" + suffix] = maximum
    b.after_row.update(curr_min_procs=minimum, curr_max_procs=maximum)
    if not dedicated:
        after.update(
            CurrentMinimumProcessingUnits=unit_minimum,
            CurrentMaximumProcessingUnits=unit_maximum,
        )
        b.after_row.update(
            curr_min_proc_units=unit_minimum, curr_max_proc_units=unit_maximum
        )
    result = await cpu_change(
        b,
        operation=operation,
        processors=2,
        processing_units=None if dedicated else "0.50",
    )
    assert result["outcome"] == "applied" and result["workflow_completed"]
    assert result["after"]["current_processors"] == 2
    assert result["after"]["current_minimum_processors"] == int(minimum)
    assert result["after"]["current_maximum_processors"] == int(maximum)
    if not dedicated:
        assert result["after"]["current_processing_units"] == "0.50"
        assert result["after"]["current_minimum_processing_units"] == unit_minimum
        assert result["after"]["current_maximum_processing_units"] == unit_maximum
    assert (
        result["before"]["current_memory_mib"]
        == result["after"]["current_memory_mib"]
        == 0
    )
    assert any(
        "Inactive current configuration only" in warning
        for warning in result["warnings"]
    )
    assert b.write.await_count == 1 and b.read.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("dedicated", [True, False])
@pytest.mark.parametrize(
    "damage,outcome",
    [
        ("assigned", "partial"),
        ("nonzero-before-maximum", "partial"),
        ("memory", "partial"),
        ("unordered", "unverified"),
        ("negative", "unverified"),
        ("missing", "unverified"),
        ("native-mismatch", "unverified"),
    ],
)
async def test_independent_zero_envelope_negative(
    processor_boundary, dedicated, damage, outcome
):
    b = processor_boundary
    before, after = zero_processor_boundary(b, dedicated)
    assigned = "CurrentProcessors" if dedicated else "AllocatedVirtualProcessors"
    suffix = "Processors" if dedicated else "VirtualProcessors"
    if damage == "assigned":
        after[assigned] = b.after_row["curr_procs"] = "3"
    elif damage == "nonzero-before-maximum":
        before["CurrentMaximum" + suffix] = b.row["curr_max_procs"] = "4"
        after["CurrentMaximum" + suffix] = b.after_row["curr_max_procs"] = "5"
    elif damage == "memory":
        b.after["Resource"]["PartitionMemoryConfiguration"].update(
            CurrentMemory="128", CurrentMaximumMemory="128"
        )
    elif damage in ("unordered", "negative"):
        after["CurrentMinimum" + suffix] = b.after_row["curr_min_procs"] = (
            "3" if damage == "unordered" else "-1"
        )
    elif damage == "missing":
        del after["CurrentMaximum" + suffix]
    else:
        after["CurrentMaximum" + suffix] = "5"
    result = await cpu_change(
        b, operation="add", processors=2, processing_units=None if dedicated else "0.50"
    )
    assert result["outcome"] == outcome and not result["workflow_completed"]
    assert (result["after"] is None) is (outcome == "unverified")
    assert b.write.await_count == 1 and b.read.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["omitted-count-limits", "wrong-units"])
async def test_independent_zero_envelope_shared_preservation(
    processor_boundary, damage
):
    b = processor_boundary
    before, after = zero_processor_boundary(b, False)
    before.update(
        CurrentMinimumVirtualProcessors="1",
        AllocatedVirtualProcessors="2",
        CurrentMaximumVirtualProcessors="4",
    )
    b.row.update(curr_min_procs="1", curr_procs="2", curr_max_procs="4")
    if damage == "omitted-count-limits":
        after["CurrentMaximumVirtualProcessors"] = b.after_row["curr_max_procs"] = "5"
    else:
        after["CurrentProcessingUnits"] = b.after_row["curr_proc_units"] = "0.60"
    result = await cpu_change(b, operation="add", processing_units="0.50")
    assert result["outcome"] == "partial" and not result["workflow_completed"]
    assert b.write.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("dedicated", [True, False])
@pytest.mark.parametrize("operation", ["add", "remove"])
async def test_independent_zero_envelope_noop(processor_boundary, dedicated, operation):
    b = processor_boundary
    zero_processor_boundary(b, dedicated)
    result = await cpu_change(b, operation=operation, processors=0)
    assert result["outcome"] == "noop" and result["before"] == result["after"]
    assert result["after"]["current_maximum_processors"] == 0
    b.write.assert_not_awaited()
    assert b.read.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("dedicated", [True, False])
async def test_independent_zero_envelope_running_refuses(processor_boundary, dedicated):
    b = processor_boundary
    zero_processor_boundary(b, dedicated)
    b.before["Resource"].update(
        PartitionState="running", ResourceMonitoringControlState="active"
    )
    with pytest.raises(ValueError, match="maximum"):
        await cpu_change(
            b,
            operation="add",
            processors=2,
            processing_units=None if dedicated else "0.50",
        )
    b.write.assert_not_awaited()


@pytest.mark.parametrize("guard", ["zero-target", "running", "reset"])
def test_independent_zero_envelope_postcondition_guards(processor_boundary, guard):
    from hmcpctl.operations.lpar.processors import _postconditions, _snapshot

    b = processor_boundary
    _, after_cpu = zero_processor_boundary(b, True)
    expected = {
        "processors": 2,
        "processing_units": None,
        "reset": False,
        "deferred": True,
    }
    if guard == "zero-target":
        expected["processors"] = 0
        after_cpu.update(CurrentMinimumProcessors="0", CurrentProcessors="0")
        b.after_row.update(curr_min_procs="0", curr_procs="0")
    elif guard == "running":
        for entry in (b.before, b.after):
            entry["Resource"]["PartitionState"] = "running"
    else:
        expected["reset"] = True
    before = _snapshot(b.before, LPAR_UUID, b.row)
    after = _snapshot(b.after, LPAR_UUID, b.after_row)
    assert not _postconditions(before, after, expected)
