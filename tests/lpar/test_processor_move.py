"""Processor moves with independent REST/native endpoint boundaries."""

import shlex
from copy import deepcopy
from decimal import Decimal, localcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncssh
import pytest
from lpar.test_processor_quantities import (
    processor_entry,
    processor_row,
    processor_system,
)

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_processors import (
    PROCESSOR_NUMERIC_TOKEN_BYTES,
    LparProcessorMove,
)
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import processor_move as operation
from hmcpctl.operations.lpar import processors
from hmcpctl.ssh import processors as native

CONFIG = HMCConfig.from_mapping({})
SOURCE = "aaaaaaaa-1111-1111-1111-111111111111"
TARGET = "bbbbbbbb-2222-2222-2222-222222222222"
SYSTEM = processor_system()["UUID"]


def set_current(entry, row, count, units=None):
    resource = entry["Resource"]
    cpu = resource["PartitionProcessorConfiguration"]
    dedicated = row["curr_proc_mode"] == "ded"
    container = cpu[
        "CurrentDedicatedProcessorConfiguration"
        if dedicated
        else "CurrentSharedProcessorConfiguration"
    ]
    container["CurrentProcessors" if dedicated else "AllocatedVirtualProcessors"] = str(
        count
    )
    row["curr_procs"] = row["run_procs"] = str(count)
    if not dedicated:
        container["CurrentProcessingUnits"] = str(units)
        row["curr_proc_units"] = row["run_proc_units"] = str(units)


def peer(uuid=SOURCE, name="source-R1", number=7, dedicated=False):
    entry, row = processor_entry(dedicated), processor_row(dedicated)
    entry["UUID"] = uuid
    resource = entry["Resource"]
    resource.update(
        PartitionName=name,
        PartitionID=str(number),
        PartitionState="running",
        ResourceMonitoringControlState="active",
    )
    cpu = resource["PartitionProcessorConfiguration"]
    for values in cpu.values():
        if not isinstance(values, dict):
            continue
        for field in values:
            if "Maximum" in field:
                values[field] = "8"
            elif "Minimum" in field:
                values[field] = "0.10" if "Units" in field else "1"
            elif "Units" in field:
                values[field] = "2.00"
            else:
                values[field] = "3"
    row.update(
        lpar_id=str(number),
        lpar_name=name,
        curr_min_procs="1",
        curr_max_procs="8",
        pend_min_procs="1",
        pend_procs="3",
        pend_max_procs="8",
    )
    if not dedicated:
        row.update(
            curr_min_proc_units="0.10",
            curr_max_proc_units="8.00",
            pend_min_proc_units="0.10",
            pend_proc_units="2.00",
            pend_max_proc_units="8.00",
        )
    set_current(entry, row, 3, None if dedicated else "2.00")
    resource["PartitionMemoryConfiguration"]["RuntimeMemory"] = "2048"
    return entry, row


@pytest.fixture
def boundary(monkeypatch):
    b = SimpleNamespace(
        before={},
        rows={},
        after={},
        after_rows={},
        changed=False,
        errors={},
        drift=None,
        fail_owner=None,
        submit_error=None,
        reads=[],
    )
    for uuid, name, number in ((SOURCE, "source-R1", 7), (TARGET, "target-R2", 8)):
        b.before[uuid], b.rows[uuid] = peer(uuid, name, number)
    b.after, b.after_rows = deepcopy(b.before), deepcopy(b.rows)
    set_current(b.after[SOURCE], b.after_rows[SOURCE], 3, "1.90")
    set_current(b.after[TARGET], b.after_rows[TARGET], 3, "2.10")
    b.system = processor_system()
    b.system["Resource"]["AssociatedSystemProcessorConfiguration"][
        "CurrentAvailableSystemProcessorUnits"
    ] = "0"

    async def read(uuid):
        uuid = uuid.lower()
        b.reads.append((uuid, b.changed))
        if b.changed and uuid in b.errors:
            raise b.errors[uuid]
        entry = deepcopy((b.after if b.changed else b.before)[uuid])
        if not b.changed and b.drift == uuid and b.reads.count((uuid, False)) > 1:
            entry["Resource"]["CurrentProfileSync"] = "Enabled"
        return entry

    async def row(config, system_name, partition_id):
        uuid = SOURCE if partition_id == 7 else TARGET
        return deepcopy((b.after_rows if b.changed else b.rows)[uuid])

    async def submit(*args, **kwargs):
        b.changed = True
        if b.submit_error:
            raise b.submit_error
        return ""

    async def description(config, system_name, name, **kwargs):
        owner = "foreign" if name == b.fail_owner else "hmcpctl"
        return f"[hmcpctl owner:{owner} created:2026-10-26]"

    b.hmc = SimpleNamespace(
        config=CONFIG,
        list_system_summaries=AsyncMock(
            return_value=[{"UUID": SYSTEM, "SystemName": "sys-R1"}]
        ),
        list_logical_partitions=AsyncMock(
            side_effect=lambda system: list(deepcopy(b.before).values())
        ),
        list_vios=AsyncMock(return_value=[]),
        get_logical_partition=AsyncMock(side_effect=read),
        get_vios=AsyncMock(side_effect=read),
        get_managed_system=AsyncMock(side_effect=lambda uuid: deepcopy(b.system)),
    )
    b.submit = AsyncMock(side_effect=submit)
    b.native_read = AsyncMock(side_effect=row)
    b.owner = AsyncMock(side_effect=description)
    monkeypatch.setattr(operation, "read_lpar_processor_quantities", b.native_read)
    monkeypatch.setattr(operation, "move_lpar_processor_quantity", b.submit)
    monkeypatch.setattr(
        "hmcpctl.operations.lpar.ownership.get_lpar_description", b.owner
    )
    return b


async def invoke(b, source="source-R1", target="target-R2", **values):
    return await operation.move_lpar_processors(
        b.hmc,
        source,
        target,
        LparProcessorMove(**(values or {"processing_units": "0.10"})),
        system_name_or_uuid="sys-R1",
    )


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"processors": True},
        {"processors": "1"},
        {"processors": -1},
        {"processors": 1.2},
        {"processing_units": "0.001"},
        {"processing_units": "NaN"},
        {"processing_units": "Infinity"},
        {"processing_units": "-1"},
        {"processing_units": True},
        {"processors": 1, "force": 1},
        {"processors": 1, "operation": "move"},
        {"processing_units": "1e999999"},
        {"processing_units": "1" * (PROCESSOR_NUMERIC_TOKEN_BYTES + 1)},
    ],
)
def test_model_rejects_invalid(values):
    with pytest.raises(ValueError):
        LparProcessorMove(**values)


@pytest.mark.parametrize("value", ["0.10", "1.000", Decimal("2.34"), 0, 1])
def test_model_exact_and_frozen(value):
    move = LparProcessorMove(processing_units=value)
    assert move.processing_units == Decimal(str(value))
    with pytest.raises(ValueError):
        move.force = True


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    "counts,units", [(1, None), (None, "0.10"), (1, "0.10"), (0, None), (None, "0")]
)
@pytest.mark.asyncio
async def test_native_exact_grammar(monkeypatch, force, counts, units):
    call = AsyncMock(return_value="ok")
    monkeypatch.setattr(native, "run_hmc_command", call)
    move = LparProcessorMove(processors=counts, processing_units=units, force=force)
    await native.move_lpar_processor_quantity(CONFIG, "system; ' quoted", 7, 8, move)
    args, kwargs = call.call_args
    expected = [
        "chhwres",
        "-r",
        "proc",
        "-m",
        "system; ' quoted",
        "-o",
        "m",
        "--id",
        "7",
        "--tid",
        "8",
    ]
    if counts is not None:
        expected += ["--procs", str(counts)]
    if units is not None:
        expected += ["--procunits", f"{Decimal(units):.2f}"]
    assert shlex.split(args[1]) == expected + (["--force"] if force else [])
    assert call.await_count == 1
    assert kwargs == {"include_stderr": True, "max_output_bytes": 65536}


@pytest.mark.parametrize("ids", [(7, 7), (0, 8), (7, -1), (True, 8), (7, "8")])
@pytest.mark.asyncio
async def test_native_refuses_identity(monkeypatch, ids):
    call = AsyncMock()
    monkeypatch.setattr(native, "run_hmc_command", call)
    with pytest.raises(ValueError):
        await native.move_lpar_processor_quantity(
            CONFIG, "sys-R1", *ids, LparProcessorMove(processors=1)
        )
    call.assert_not_called()


@pytest.mark.parametrize(
    "modes,values,expected",
    [
        ((True, True), {"processors": 1}, ((2, None), (4, None))),
        (
            (False, False),
            {"processing_units": "0.10"},
            ((3, Decimal("1.90")), (3, Decimal("2.10"))),
        ),
        (
            (False, False),
            {"processors": 1, "processing_units": "0.50"},
            ((2, Decimal("1.50")), (4, Decimal("2.50"))),
        ),
        ((True, False), {"processors": 1}, ((2, None), (None, Decimal("3.00")))),
        (
            (False, True),
            {"processing_units": "1"},
            ((None, Decimal("1.00")), (4, None)),
        ),
    ],
)
def test_four_mode_mapping_and_zero_available(modes, values, expected):
    snapshots = [
        processors._snapshot(*[pair[0], pair[0]["UUID"], pair[1]])
        for pair in (peer(dedicated=modes[0]), peer(TARGET, "target-R2", 8, modes[1]))
    ]
    system = processor_system()
    system["Resource"]["AssociatedSystemProcessorConfiguration"][
        "CurrentAvailableSystemProcessorUnits"
    ] = "0"
    with localcontext(prec=2):
        result = operation._targets(*snapshots, LparProcessorMove(**values), system)
    assert tuple((r["processors"], r["processing_units"]) for r in result) == expected


@pytest.mark.parametrize(
    "modes,values",
    [
        ((True, True), {"processing_units": "1"}),
        ((True, False), {"processors": 1, "processing_units": "0"}),
        ((False, True), {"processors": 1}),
        ((False, True), {"processing_units": "0.50"}),
    ],
)
def test_mode_flag_applicability(modes, values):
    snapshots = []
    for dedicated in modes:
        e, r = peer(dedicated=dedicated)
        snapshots.append(processors._snapshot(e, e["UUID"], r))
    with pytest.raises(ValueError):
        operation._targets(*snapshots, LparProcessorMove(**values), processor_system())


@pytest.mark.asyncio
async def test_success_requires_two_observations(boundary):
    result = await invoke(boundary)
    assert result["outcome"] == "applied" and result["workflow_completed"]
    assert result["source"]["after"]["current_processing_units"] == "1.90"
    assert result["destination"]["after"]["current_processing_units"] == "2.10"
    assert (SOURCE, True) in boundary.reads and (TARGET, True) in boundary.reads
    assert boundary.submit.await_count == 1 and boundary.owner.await_count == 4
    assert result["source"]["warnings"] and result["destination"]["warnings"]


@pytest.mark.parametrize("name", ["source-R1", "target-R2"])
@pytest.mark.asyncio
async def test_either_ownership_denial_stops_submission(boundary, name):
    boundary.fail_owner = name
    with pytest.raises(PermissionError):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("uuid", [SOURCE, TARGET])
@pytest.mark.asyncio
async def test_each_endpoint_drift_stops_submission(boundary, uuid):
    boundary.drift = uuid
    with pytest.raises(ValueError, match="decision changed"):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize(
    "source,target",
    [
        (SOURCE, SOURCE.upper()),
        ("source-R1", "source-R1"),
        ("absent", "target-R2"),
        ("source-R1", "absent"),
    ],
)
@pytest.mark.asyncio
async def test_identity_refusal(boundary, source, target):
    with pytest.raises(ValueError):
        await invoke(boundary, source, target)
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("uuid", [SOURCE, TARGET])
@pytest.mark.asyncio
async def test_read_failure_retains_other_endpoint(boundary, uuid):
    boundary.errors[uuid] = HMCError("read unavailable")
    result = await invoke(boundary)
    assert result["outcome"] == "unverified" and not result["workflow_completed"]
    assert (SOURCE, True) in boundary.reads and (TARGET, True) in boundary.reads
    assert result["destination" if uuid == SOURCE else "source"]["after"] is not None
    assert boundary.submit.await_count == 1


@pytest.mark.parametrize("uuid", [SOURCE, TARGET])
@pytest.mark.asyncio
async def test_one_sided_change_is_partial(boundary, uuid):
    boundary.after[uuid], boundary.after_rows[uuid] = (
        deepcopy(boundary.before[uuid]),
        deepcopy(boundary.rows[uuid]),
    )
    result = await invoke(boundary)
    assert result["outcome"] == "partial" and not result["workflow_completed"]
    assert boundary.submit.await_count == 1


@pytest.mark.asyncio
async def test_lost_acknowledgement_with_dual_matching_state(boundary):
    boundary.submit_error = HMCError("timeout after submission")
    result = await invoke(boundary)
    assert result["outcome"] == "applied" and result["submission_error"]
    assert boundary.submit.await_count == 1


@pytest.mark.asyncio
async def test_transport_error_unchanged_is_unverified(boundary):
    boundary.after, boundary.after_rows = (
        deepcopy(boundary.before),
        deepcopy(boundary.rows),
    )
    boundary.submit_error = HMCError("connection lost")
    assert (await invoke(boundary))["outcome"] == "unverified"
    assert boundary.submit.await_count == 1


@pytest.mark.asyncio
async def test_native_exit_unchanged_is_rejected(boundary):
    boundary.after, boundary.after_rows = (
        deepcopy(boundary.before),
        deepcopy(boundary.rows),
    )
    failure = HMCError("native refusal")
    failure.__cause__ = asyncssh.ProcessError(
        env={},
        command="fixed",
        subsystem=None,
        exit_status=1,
        exit_signal=None,
        returncode=1,
        stdout="",
        stderr="refused",
    )
    boundary.submit_error = failure
    assert (await invoke(boundary))["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_guarded_zero_has_no_native_submission(boundary):
    result = await invoke(boundary, processing_units="0")
    assert result["outcome"] == "noop" and boundary.owner.await_count == 4
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("uuid", [SOURCE, TARGET])
@pytest.mark.parametrize(
    "field,value",
    [
        ("PartitionState", "migrating"),
        ("IsOperationInProgress", "true"),
        ("ResourceMonitoringControlState", "inactive"),
    ],
)
@pytest.mark.asyncio
async def test_state_refusal_before_effect(boundary, uuid, field, value):
    boundary.before[uuid]["Resource"][field] = value
    with pytest.raises(ValueError):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("uuid", [SOURCE, TARGET])
@pytest.mark.asyncio
async def test_coupled_memory_change_never_completes(boundary, uuid):
    boundary.after[uuid]["Resource"]["PartitionMemoryConfiguration"][
        "CurrentMemory"
    ] = "1792"
    result = await invoke(boundary)
    assert result["outcome"] == "partial" and not result["workflow_completed"]


@pytest.mark.parametrize("delta", [2, 3, 4])
def test_mixed_shared_vcpu_is_observed_and_bounded(delta):
    left, lr = peer(dedicated=True)
    right, rr = peer(TARGET, "target-R2", 8, False)
    before = processors._snapshot(right, TARGET, rr)
    expected = operation._targets(
        processors._snapshot(left, SOURCE, lr),
        before,
        LparProcessorMove(processors=1),
        processor_system(),
    )[1]
    assert expected["processors"] is None
    set_current(right, rr, delta, "3.00")
    after = processors._snapshot(right, TARGET, rr)
    if delta < 3:
        with pytest.raises(ValueError):
            operation._postconditions(
                before,
                after,
                expected,
                processors._capacity(processor_system(), before),
            )
    else:
        assert operation._postconditions(
            before, after, expected, processors._capacity(processor_system(), before)
        )


@pytest.mark.parametrize("value", ["3.01", "9.00"])
@pytest.mark.asyncio
async def test_source_floor_and_overdraw_fail_before_effect(boundary, value):
    with pytest.raises(ValueError):
        await invoke(boundary, processing_units=value)
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("dynamic_processor", None),
        ("dynamic_processor", "false"),
        ("operation_in_progress", True),
        ("state", "starting"),
    ],
)
def test_capability_and_transition_refusal(shared, field, value):
    e, r = peer(dedicated=not shared)
    source = processors._snapshot(e, SOURCE, r)
    source[field] = value
    with pytest.raises(ValueError):
        operation._targets(
            source,
            deepcopy(source),
            LparProcessorMove(processors=1),
            processor_system(),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_minimum_processing_units", Decimal("2.00")),
        ("pending_minimum_processing_units", Decimal("2.00")),
    ],
)
def test_source_current_and_profile_floors(field, value):
    e, r = peer()
    source = processors._snapshot(e, SOURCE, r)
    source[field] = value
    with pytest.raises(ValueError):
        operation._targets(
            source,
            processors._snapshot(e, SOURCE, r),
            LparProcessorMove(processing_units="0.10"),
            processor_system(),
        )


@pytest.mark.parametrize(
    "field", ["current_maximum_processing_units", "pending_maximum_processing_units"]
)
def test_destination_current_and_profile_ceilings(field):
    e, r = peer()
    source = processors._snapshot(e, SOURCE, r)
    destination = deepcopy(source)
    destination[field] = Decimal("2.00")
    with pytest.raises(ValueError):
        operation._targets(
            source,
            destination,
            LparProcessorMove(processing_units="0.10"),
            processor_system(),
        )


@pytest.mark.parametrize("kind", ["AIX/Linux", "Virtual IO Server"])
def test_force_defers_only_no_rmc_endpoint(kind):
    e, r = peer()
    source = processors._snapshot(e, SOURCE, r)
    destination = deepcopy(source)
    source.update(partition_type=kind, rmc="inactive", dynamic_processor="false")
    expected = operation._targets(
        source,
        destination,
        LparProcessorMove(processing_units="0.10", force=True),
        processor_system(),
    )
    assert expected[0]["deferred"] and not expected[1]["deferred"]


def test_force_does_not_bypass_ibmi_capability():
    e, r = peer()
    source = processors._snapshot(e, SOURCE, r)
    destination = deepcopy(source)
    source.update(rmc="inactive")
    destination.update(partition_type="OS400", dynamic_processor="false")
    with pytest.raises(ValueError):
        operation._targets(
            source,
            destination,
            LparProcessorMove(processing_units="0.10", force=True),
            processor_system(),
        )


def test_inactive_admission_is_deferred_not_runtime_success():
    e, r = peer()
    source = processors._snapshot(e, SOURCE, r)
    source.update(state="not activated", rmc="inactive", dynamic_processor=None)
    expected = operation._targets(
        source,
        deepcopy(source),
        LparProcessorMove(processing_units="0.10"),
        processor_system(),
    )
    assert all(target["deferred"] for target in expected)


@pytest.mark.parametrize("mode", [(True, False), (False, True)])
def test_mixed_feasible_vcpu_envelope_does_not_freeze_current(mode):
    a, ar = peer(dedicated=mode[0])
    b, br = peer(TARGET, "target-R2", 8, mode[1])
    source, destination = (
        processors._snapshot(a, SOURCE, ar),
        processors._snapshot(b, TARGET, br),
    )
    shared = source if not mode[0] else destination
    shared["current_processors"] = 1
    shared["current_processing_units"] = Decimal("1.00") if mode[0] else Decimal("2.00")
    request = (
        LparProcessorMove(processors=1)
        if mode[0]
        else LparProcessorMove(processing_units="1")
    )
    expected = operation._targets(source, destination, request, processor_system())
    assert expected[1 if mode[0] else 0]["processors"] is None


@pytest.mark.asyncio
async def test_wrong_size_readback_is_partial(boundary):
    set_current(boundary.after[TARGET], boundary.after_rows[TARGET], 3, "2.15")
    assert (await invoke(boundary))["outcome"] == "partial"


@pytest.mark.asyncio
async def test_missing_native_readback_is_unverified(boundary):
    boundary.after_rows[SOURCE]["run_procs"] = ""
    result = await invoke(boundary)
    assert result["outcome"] == "unverified"
    assert result["destination"]["after"] is not None


@pytest.mark.asyncio
async def test_endpoint_kind_mismatch_stops_effect(boundary):
    boundary.before[SOURCE]["Resource"]["PartitionType"] = "Virtual IO Server"
    with pytest.raises(ValueError, match="kind"):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_rejects_untyped_request(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(native, "run_hmc_command", call)
    with pytest.raises(TypeError):
        await native.move_lpar_processor_quantity(
            CONFIG, "sys-R1", 7, 8, {"processors": 1}
        )
    call.assert_not_called()


@pytest.mark.parametrize(
    "modes,values,actual",
    [
        ((True, True), {"processors": 1}, ((2, None), (4, None))),
        ((False, False), {"processors": 1}, ((2, "2.00"), (4, "2.00"))),
        ((True, False), {"processors": 1}, ((2, None), (4, "3.00"))),
        ((False, True), {"processing_units": "1"}, ((2, "1.00"), (4, None))),
    ],
)
@pytest.mark.asyncio
async def test_four_pairs_full_workflow(boundary, modes, values, actual):
    for uuid, name, number, dedicated, (count, units) in zip(
        (SOURCE, TARGET), ("source-R1", "target-R2"), (7, 8), modes, actual, strict=True
    ):
        boundary.before[uuid], boundary.rows[uuid] = peer(uuid, name, number, dedicated)
        boundary.after[uuid], boundary.after_rows[uuid] = (
            deepcopy(boundary.before[uuid]),
            deepcopy(boundary.rows[uuid]),
        )
        set_current(boundary.after[uuid], boundary.after_rows[uuid], count, units)
    result = await invoke(boundary, **values)
    assert result["workflow_completed"] and result["outcome"] == "applied"
    assert boundary.submit.await_count == 1
    if modes[0] != modes[1]:
        observed = result["destination" if modes[0] else "source"]
        assert observed["observed_only_quantities"] == ["processors"]
        assert observed["verified_quantities"] == ["processing_units"]


@pytest.mark.parametrize("uuid", [SOURCE, TARGET])
@pytest.mark.parametrize(
    "field,value", [("lpar_id", "9"), ("lpar_name", "other"), ("curr_proc_mode", "ded")]
)
@pytest.mark.asyncio
async def test_native_rest_disagreement_stops_effect(boundary, uuid, field, value):
    boundary.rows[uuid][field] = value
    with pytest.raises(ValueError):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.asyncio
async def test_missing_or_ambiguous_contained_peer_stops_effect(boundary, position):
    entries = list(boundary.before.values())
    boundary.hmc.list_logical_partitions.return_value = (
        entries[:position] + entries[position + 1 :]
    )
    boundary.hmc.list_logical_partitions.side_effect = None
    with pytest.raises(ValueError, match="outside the selected system"):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()
    boundary.hmc.list_logical_partitions.return_value = entries + [entries[position]]
    with pytest.raises(ValueError, match="ambiguous"):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [("UUID", TARGET), ("PartitionName", "renamed"), ("PartitionID", "8")],
)
@pytest.mark.asyncio
async def test_wrong_rest_identity_stops_effect(boundary, field, value):
    original = boundary.hmc.get_logical_partition.side_effect

    async def read(uuid):
        entry = await original(uuid)
        if uuid.lower() == SOURCE:
            if field == "UUID":
                entry[field] = value
            else:
                entry["Resource"][field] = value
        return entry

    boundary.hmc.get_logical_partition.side_effect = read
    with pytest.raises(ValueError):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_numeric_id_refused(boundary):
    boundary.before[TARGET]["Resource"]["PartitionID"] = "7"
    boundary.rows[TARGET]["lpar_id"] = "7"
    # The duplicate ID is visible before the native reader can resolve it.
    with pytest.raises(ValueError):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("which", ["capacity", "identity", "cli-name"])
@pytest.mark.asyncio
async def test_system_drift_stops_effect(boundary, which):
    if which == "cli-name":
        boundary.hmc.list_system_summaries.return_value += [
            {"UUID": TARGET, "SystemName": "sys-R1"}
        ]
    else:
        changed = deepcopy(boundary.system)
        if which == "capacity":
            changed["Resource"]["AssociatedSystemProcessorConfiguration"][
                "MinimumProcessorUnitsPerVirtualProcessor"
            ] = "0.10"
        else:
            changed["UUID"] = TARGET
        boundary.hmc.get_managed_system.side_effect = [boundary.system, changed]
    with pytest.raises(ValueError):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize("denied_call", [3, 4])
@pytest.mark.asyncio
async def test_fresh_ownership_denial_stops_effect(boundary, denied_call):
    async def description(*args, **kwargs):
        owner = "foreign" if boundary.owner.await_count == denied_call else "hmcpctl"
        return f"[hmcpctl owner:{owner} created:2026-10-26]"

    boundary.owner.side_effect = description
    with pytest.raises(PermissionError):
        await invoke(boundary)
    boundary.submit.assert_not_awaited()
    assert all(
        call.kwargs == {"max_output_bytes": 65536}
        for call in boundary.owner.call_args_list
    )


@pytest.mark.asyncio
async def test_uuid_case_changes_do_not_invent_drift(boundary):
    calls = 0

    async def entries(system):
        nonlocal calls
        calls += 1
        values = deepcopy(list(boundary.before.values()))
        if calls > 2:
            for value in values:
                value["UUID"] = value["UUID"].upper()
        return values

    boundary.hmc.list_logical_partitions.side_effect = entries
    assert (await invoke(boundary))["outcome"] == "applied"


@pytest.mark.parametrize("uuid", [SOURCE, TARGET])
@pytest.mark.asyncio
async def test_force_reports_deferred_endpoint_runtime_separately(boundary, uuid):
    for entries in (boundary.before, boundary.after):
        entries[uuid]["Resource"]["ResourceMonitoringControlState"] = "inactive"
        entries[uuid]["Resource"]["PartitionCapabilities"][
            "DynamicLogicalPartitionProcessorCapable"
        ] = "false"
    boundary.after_rows[uuid]["run_proc_units"] = "2.00"
    result = await invoke(boundary, processing_units="0.10", force=True)
    deferred = result["source" if uuid == SOURCE else "destination"]
    normal = result["destination" if uuid == SOURCE else "source"]
    assert result["outcome"] == "applied"
    assert deferred["expected"]["deferred"] and not normal["expected"]["deferred"]
    assert any("restart" in warning for warning in deferred["warnings"])


@pytest.mark.parametrize("uuid", [SOURCE, TARGET])
@pytest.mark.asyncio
async def test_complete_envelope_change_is_partial(boundary, uuid):
    boundary.after[uuid]["Resource"]["PartitionProcessorConfiguration"][
        "CurrentSharedProcessorConfiguration"
    ]["CurrentMaximumVirtualProcessors"] = "7"
    boundary.after_rows[uuid]["curr_max_procs"] = "7"
    assert (await invoke(boundary))["outcome"] == "partial"


@pytest.mark.asyncio
async def test_both_postread_errors_remain_bounded(boundary):
    boundary.errors = {uuid: HMCError("x" * 10000) for uuid in (SOURCE, TARGET)}
    result = await invoke(boundary)
    assert result["outcome"] == "unverified"
    for name in ("source", "destination"):
        assert result[name]["after"] is None
        assert len(result[name]["read_error"]) <= 2048
    assert (SOURCE, True) in boundary.reads and (TARGET, True) in boundary.reads


@pytest.mark.parametrize(
    "field",
    [
        "CurrentMaximumAllowedProcessorsPerPartition",
        "CurrentMaximumProcessorsPerAIXOrLinuxPartition",
        "ConfigurableSystemProcessorUnits",
    ],
)
def test_dedicated_system_and_os_limits(field):
    entries = [peer(dedicated=True), peer(TARGET, "target-R2", 8, True)]
    snapshots = [processors._snapshot(e, e["UUID"], row) for e, row in entries]
    system = processor_system()
    system["Resource"]["AssociatedSystemProcessorConfiguration"][field] = "3"
    with pytest.raises(ValueError):
        operation._targets(*snapshots, LparProcessorMove(processors=1), system)


@pytest.mark.asyncio
async def test_inactive_zero_destination_envelope_uses_pending_bounds(boundary):
    destination = boundary.before[TARGET]["Resource"]
    destination["PartitionState"] = "not activated"
    current = destination["PartitionProcessorConfiguration"][
        "CurrentSharedProcessorConfiguration"
    ]
    for key in current:
        current[key] = "0"
    for key in (
        "curr_min_procs",
        "curr_procs",
        "curr_max_procs",
        "curr_min_proc_units",
        "curr_proc_units",
        "curr_max_proc_units",
        "run_procs",
        "run_proc_units",
    ):
        boundary.rows[TARGET][key] = "0"
    boundary.after[TARGET] = deepcopy(boundary.before[TARGET])
    boundary.after_rows[TARGET] = deepcopy(boundary.rows[TARGET])
    after_current = boundary.after[TARGET]["Resource"][
        "PartitionProcessorConfiguration"
    ]["CurrentSharedProcessorConfiguration"]
    after_current.update(
        CurrentMinimumVirtualProcessors="1",
        CurrentMaximumVirtualProcessors="4",
        CurrentMinimumProcessingUnits="0.10",
        CurrentMaximumProcessingUnits="4.00",
    )
    boundary.after_rows[TARGET].update(
        curr_min_procs="1",
        curr_max_procs="4",
        curr_min_proc_units="0.10",
        curr_max_proc_units="4.00",
    )
    set_current(boundary.after[SOURCE], boundary.after_rows[SOURCE], 2, "1.90")
    set_current(boundary.after[TARGET], boundary.after_rows[TARGET], 1, "0.10")
    boundary.after_rows[TARGET].update(run_procs="0", run_proc_units="0")
    result = await invoke(boundary, processors=1, processing_units="0.10")
    assert (
        result["outcome"] == "applied" and result["destination"]["expected"]["deferred"]
    )
    assert any(
        "no running-guest effect" in warning
        for warning in result["destination"]["warnings"]
    )


@pytest.mark.parametrize(
    "source,target,system",
    [
        ("", "target-R2", "sys-R1"),
        ("source-R1", " ", "sys-R1"),
        ("source-R1", "target-R2", ""),
        (None, "target-R2", "sys-R1"),
    ],
)
@pytest.mark.asyncio
async def test_missing_selector_refused_before_read(boundary, source, target, system):
    with pytest.raises(ValueError):
        await operation.move_lpar_processors(
            boundary.hmc,
            source,
            target,
            LparProcessorMove(processors=1),
            system_name_or_uuid=system,
        )
    boundary.hmc.get_logical_partition.assert_not_awaited()
    boundary.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_operation_requires_typed_move(boundary):
    with pytest.raises(TypeError):
        await operation.move_lpar_processors(
            boundary.hmc, SOURCE, TARGET, {"processors": 1}, system_name_or_uuid=SYSTEM
        )
    boundary.submit.assert_not_awaited()


@pytest.mark.parametrize(
    "values",
    [
        {"processors": 10**2000},
        {"processing_units": "1e-1000"},
        {"processors": None, "processing_units": None},
    ],
)
def test_numeric_budget_and_missing_quantities(values):
    with pytest.raises(ValueError):
        LparProcessorMove(**values)
