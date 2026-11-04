"""ST73: the logical create, reconfigure and destroy workflow on one partition (#1854).

One `hmcpctl-live-pwr-<8 hex>-p` partition goes through the primary catalog's workflow
tools only: `hmc_provision_lpar` creates it inactive on a run-owned 1 GiB volume and the
provision VLAN, `hmc_reconfigure_lpar` changes its memory and processors (read back
`profile`), `hmc_inspect_lpar` reports the change and the volume's mapping,
`hmc_power_lpar` starts it (the change must be current) and stops it, and
`hmc_decommission_lpar` deletes it and the owned volume. Every other tool the arm
calls is an independent witness read, or teardown. The lpar-power arm's names,
baseline, teardown, comparison and recovery witness are reused.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from . import lpar_config, lpar_power
from .bare_cec import _power_operations_authorized
from .observation import Assertion
from .results import field as result_field

if TYPE_CHECKING:
    from fastmcp import Client

    from .runner import RunState

GROUP = "logical-workflow"
SUBTASK = 73
SCENARIO = "st73-logical-workflow"
#: The workflow's own calls: each must be in the primary catalog (ADR 0189, ADR 0330).
WORKFLOW_TOOLS = frozenset(
    {
        "hmc_provision_lpar",
        "hmc_reconfigure_lpar",
        "hmc_inspect_lpar",
        "hmc_power_lpar",
        "hmc_decommission_lpar",
        "hmc_operation_status",
    }
)
#: Within lpar-config's maximums (4096 MiB, 2 virtual processors), unlike its desired.
PATCH = {"memory_mib": 3072, "processors": 2}
_STOPPED = frozenset({lpar_power._NOT_ACTIVATED})
_SETTLED = frozenset({"terminal", "paused"})


async def _settle(
    client: Client, state: RunState, request_id: str, record: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """Poll *request_id* through `hmc_operation_status` until terminal or paused."""
    for _ in range(lpar_power._DECOMMISSION_POLLS):
        if record is None or record.get("state") in _SETTLED:
            break
        await asyncio.sleep(lpar_power._OPERATION_POLL_DELAY_S)
        st, page = await state.call(
            client, "hmc_operation_status", request_id=request_id
        )
        found = [
            item
            for item in lpar_power._items(page, "operations")
            if item.get("request_id") == request_id
        ]
        record = found[0] if st == "PASS" and found else record
    return record


def _operation(st: str, data: object) -> Mapping[str, Any] | None:
    found = lpar_power._plain(result_field(data, "operation")) if st == "PASS" else None
    return found if isinstance(found, Mapping) else None


async def _provision(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    """Create P inactive on the run volume; whether it exists as the run's."""
    vios = run.vios
    assert vios is not None
    run.volume_attempted = True
    request_id = f"{run.token}-p"
    st, data = await state.call(
        client,
        "hmc_provision_lpar",
        request_id=request_id,
        wait_seconds=600,
        system_name_or_uuid=run.system,
        name=run.p_name,
        caller_token=run.token,
        adapters={"port_vlan_id": run.vlan},
        storage={
            "vios_uuid": vios.uuid,
            "storage_name": run.volume,
            "kind": "VirtualDisk",
            "vg_uuid": vios.group_uuid,
            "capacity_mib": lpar_power.VOLUME_MIB,
        },
        resources=lpar_power.RESOURCES,
        power_on=False,
        exclusive_writer_window=True,
    )
    state.record(SUBTASK, "hmc_provision_lpar", st, data)
    run.data["provision.lpar"] = data
    operation = _operation(st, data)
    if operation is not None:
        run.provisions.add(request_id)
        operation = await _settle(client, state, request_id, operation)
        run.requests[request_id] = (operation or {}).get("state") == "terminal"
    found = result_field(data, "lpar_uuid") if st == "PASS" else None
    run.p_uuid = found if isinstance(found, str) else None
    if run.p_uuid is None:
        run.p_uuid = await lpar_power._adopt(client, state, run, run.p_name)
    op = "provision.lpar"
    run.hold(
        op,
        "workflow-completed",
        (operation or {}).get("outcome") == "configured"
        and not any(
            step.get("status") == "error" for step in lpar_power._items(data, "steps")
        ),
    )
    if run.p_uuid is None:
        return False
    run.hold(
        op,
        "ownership-stamped",
        await lpar_power._token_of(client, state, run, run.p_uuid) == run.token,
    )
    run.hold(
        op,
        "left-not-activated",
        await lpar_power._lpar_state(client, state, run, run.p_uuid) in _STOPPED,
    )
    run.hold(
        op,
        "storage-mapping-listed",
        lpar_power._mapped(await lpar_power._mappings(client, state, run), run),
    )
    return run.holds(op, "workflow-completed")


def _current(data: object) -> dict[str, Any]:
    """Current memory and allocated virtual processors from an `hmc_get_lpar` read."""
    memory = lpar_config._section(data, ("PartitionMemoryConfiguration",))
    cpu = lpar_config._section(
        data,
        ("PartitionProcessorConfiguration", "CurrentSharedProcessorConfiguration"),
    )
    return {
        "memory_mib": lpar_config._number(memory.get("CurrentMemory")),
        "processors": lpar_config._number(cpu.get("AllocatedVirtualProcessors")),
    }


async def _reconfigure(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    request_id = f"{run.token}-reconfigure"
    run.data["reconfigure_request"] = request_id
    st, data = await state.call(
        client,
        "hmc_reconfigure_lpar",
        request_id=request_id,
        lpar_name_or_uuid=run.p_uuid,
        system_name_or_uuid=run.system,
        patch=PATCH,
        exclusive_writer_window=True,
        wait_seconds=600,
    )
    state.record(SUBTASK, "hmc_reconfigure_lpar", st, data)
    record = await _settle(client, state, request_id, _operation_record(st, data))
    run.data["lpar.reconfigure"] = record
    result = (record or {}).get("result") or {}
    applied = {change.get("applied") for change in result.get("changes") or []}
    run.hold(
        "lpar.reconfigure",
        "inactive-change-classified-profile",
        (record or {}).get("outcome") == "completed" and applied == {"profile"},
    )
    return run.holds("lpar.reconfigure", "inactive-change-classified-profile")


async def _inspect(client: Client, state: RunState, run: lpar_power.Run) -> None:
    st, data = await state.call(
        client,
        "hmc_inspect_lpar",
        lpar_name_or_uuid=run.p_uuid,
        system_name_or_uuid=run.system,
        include=["resources", "rmc"],
    )
    state.record(SUBTASK, "hmc_inspect_lpar", st, data)
    found = lpar_power._plain(data) if st == "PASS" else None
    inspection = found if isinstance(found, Mapping) else {}
    run.data["lpar.inspect"] = inspection
    resources = inspection.get("resources") or {}
    run.hold(
        "lpar.inspect",
        "reports-the-change-inactive",
        str(inspection.get("state") or "").lower() == lpar_power._NOT_ACTIVATED
        and resources.get("desired_memory_mib") == PATCH["memory_mib"]
        and resources.get("desired_vcpus") == PATCH["processors"],
    )
    run.hold(
        "lpar.inspect",
        "reports-the-owned-mapping",
        (resources.get("storage_source") or {}).get("status") == "ok"
        and any(
            mapping.get("backing_device") == run.volume
            for mapping in resources.get("mappings") or []
        ),
    )


async def _power(
    client: Client, state: RunState, run: lpar_power.Run, action: str
) -> Mapping[str, Any] | None:
    request_id = f"{run.token}-{action}"
    run.requests.setdefault(request_id, False)
    st, data = await state.call(
        client,
        "hmc_power_lpar",
        request_id=request_id,
        lpar_name_or_uuid=run.p_uuid,
        system_name_or_uuid=run.system,
        action=action,
        mode="immediate" if action == "stop" else None,
        wait_seconds=600,
    )
    state.record(SUBTASK, f"hmc_power_lpar ({action})", st, data)
    record = await _settle(client, state, request_id, _operation_record(st, data))
    if record is not None:
        run.requests[request_id] = record.get("state") == "terminal"
    run.data[f"lpar.power.{action}"] = record
    return record


def _operation_record(st: str, data: object) -> Mapping[str, Any] | None:
    found = lpar_power._plain(data) if st == "PASS" else None
    return found if isinstance(found, Mapping) else None


async def _power_cases(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    op = "lpar.power"
    start = await _power(client, state, run, "start")
    run.hold(
        op, "start-completed-activated", _moved(start, lpar_power._ACTIVATED_STATES)
    )
    reached = await lpar_power._wait_for_state(
        client, state, run, run.p_uuid or "", lpar_power._FIRMWARE_STATES
    )
    current = _current(await lpar_power._get_lpar(client, state, run, run.p_uuid or ""))
    run.data["current_after_start"] = current
    run.hold(
        op,
        "started-with-the-change",
        reached in lpar_power._FIRMWARE_STATES
        and all(current.get(key) == value for key, value in PATCH.items()),
    )
    stop = await _power(client, state, run, "stop")
    run.hold(op, "stop-immediate-completed-not-activated", _moved(stop, _STOPPED))
    return run.holds(op, "stop-immediate-completed-not-activated")


def _moved(record: Mapping[str, Any] | None, wanted: frozenset[str]) -> bool:
    """Completed in *wanted* by a job of its own, not because it was already there."""
    result = (record or {}).get("result") or {}
    return (
        lpar_power._completed(record, wanted)
        and result.get("already_in_state") is False
    )


async def _workflow(client: Client, state: RunState, run: lpar_power.Run) -> None:
    """Each step needs the one before it; a failed step ends the workflow."""
    if not await _provision(client, state, run):
        return
    if not await _reconfigure(client, state, run):
        return
    await _inspect(client, state, run)
    if await _power_cases(client, state, run):
        await lpar_power._decommission_cases(client, state, run)


async def _abandon_reconfigure(
    client: Client, state: RunState, run: lpar_power.Run
) -> bool:
    """Abandon a reconfigure left open; whether none is left open.

    The engine refuses to abandon an operation still running, so only a terminal
    answer counts.
    """
    record = run.data.get("lpar.reconfigure")
    if record is None or record.get("state") == "terminal":
        return True
    request_id = run.data["reconfigure_request"]
    st, data = await state.call(
        client,
        "hmc_reconfigure_lpar",
        request_id=request_id,
        continuation="abandon",
        wait_seconds=0,
    )
    closed = (_operation_record(st, data) or {}).get("state") == "terminal"
    state.record(
        SUBTASK,
        f"hmc_reconfigure_lpar ({request_id} abandon)",
        "PASS" if closed else "FAIL",
        data,
        "" if closed else "MANUAL RECOVERY REQUIRED: the reconfigure is still open",
    )
    return closed


async def _preconditions(
    client: Client, state: RunState, run: lpar_power.Run
) -> lpar_power.Baseline | str:
    if state.group != GROUP:
        return f"creates, powers and deletes a partition: runs only in {GROUP!r}"
    if not state.config.partition_profile_writer_window:
        return lpar_power.PROVISION_WINDOW_SKIP
    if not _power_operations_authorized():
        return "HMC_AUTHORIZE_POWER_OPERATIONS is off"
    why = await lpar_power._find_vios(client, state, run) or (
        await lpar_power._vlan_refusal(client, state, run)
    )
    if why is not None:
        return why
    # After the VIOS: the baseline then covers its mappings, adapters and volumes.
    baseline = await lpar_power._read_baseline(client, state, run)
    if baseline is None:
        return "the system baseline did not read"
    stranded = lpar_power.scratch_partitions(lpar_power._names(baseline.partitions))
    stranded += lpar_power.scratch_volumes(baseline.volumes or ())
    if stranded:
        return f"{stranded} already exist: run live_test_recovery.py first"
    return baseline


_ASSERTIONS: dict[str, tuple[str, ...]] = {
    "provision.lpar": (
        "workflow-completed",
        "ownership-stamped",
        "left-not-activated",
        "storage-mapping-listed",
    ),
    "lpar.reconfigure": ("inactive-change-classified-profile",),
    "lpar.inspect": ("reports-the-change-inactive", "reports-the-owned-mapping"),
    "lpar.power": (
        "start-completed-activated",
        "started-with-the-change",
        "stop-immediate-completed-not-activated",
    ),
    "lpar.decommission": (
        "dry-run-inventoried",
        "dry-run-changed-nothing",
        "resource-deleted",
        "workflow-completed",
        "lpar-name-absent",
        "storage-mapping-absent",
        "owned-disk-deleted",
        "shared-storage-retained",
    ),
}


def _assertions(run: lpar_power.Run, operation: str) -> list[Assertion]:
    return [
        Assertion(name, run.holds(operation, name)) for name in _ASSERTIONS[operation]
    ]


def _observe(state: RunState, run: lpar_power.Run, cleanup: str) -> None:
    """One verified row per workflow operation the run reached."""
    if "provision.lpar" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_provision_lpar",
            operation="provision.lpar",
            scenario=SCENARIO,
            assertions=_assertions(run, "provision.lpar"),
            cleanup=cleanup,
            data=run.data.get("provision.lpar"),
        )
    if "lpar.reconfigure" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_reconfigure_lpar",
            operation="lpar.reconfigure",
            scenario=SCENARIO,
            assertions=_assertions(run, "lpar.reconfigure"),
            cleanup=cleanup,
            data=run.data.get("lpar.reconfigure"),
        )
    if "lpar.inspect" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_inspect_lpar",
            operation="lpar.inspect",
            scenario=SCENARIO,
            assertions=_assertions(run, "lpar.inspect"),
            cleanup=cleanup,
            data=run.data.get("lpar.inspect"),
        )
    if "lpar.power" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_power_lpar",
            operation="lpar.power",
            scenario=SCENARIO,
            assertions=_assertions(run, "lpar.power"),
            cleanup=cleanup,
            data={
                key: run.data.get(key)
                for key in (
                    "lpar.power.start",
                    "current_after_start",
                    "lpar.power.stop",
                )
            },
        )
    if "lpar.decommission" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_decommission_lpar",
            operation="lpar.decommission",
            scenario=SCENARIO,
            assertions=_assertions(run, "lpar.decommission"),
            cleanup=cleanup,
            data=run.data.get("lpar.decommission"),
        )


async def exercise_logical_workflow(client: Client, state: RunState) -> None:
    print("\n=== ST73: logical create, reconfigure and destroy (issue #1854) ===")
    run = lpar_power.Run(state.config.system_name, uuid.uuid4().hex[:8])
    ready = await _preconditions(client, state, run)
    if isinstance(ready, str):
        state.skip(SUBTASK, GROUP, ready)
        return
    state.record(SUBTASK, "system baseline", "PASS", lpar_power._baseline_data(ready))
    closed = False
    try:
        await _workflow(client, state, run)
    finally:
        closed = await _abandon_reconfigure(client, state, run)
        await lpar_power._teardown(client, state, run)
    same = await lpar_power._compare(client, state, run, ready)
    _observe(state, run, "passed" if closed and run.clean and same else "failed")
