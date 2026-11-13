"""ST73: the logical plan, create, reconfigure, hand-off and destroy workflow on one partition.

One `hmcpctl-live-pwr-<8 hex>-p` partition goes through the primary catalog's workflow
tools only: `hmc_plan_lpar` plans it with provision's own inputs and writes nothing,
`hmc_provision_lpar` creates it inactive on a run-owned 1 GiB volume and the
provision VLAN, `hmc_inventory` lists it on its system, `hmc_reconfigure_lpar` changes its memory and processors (read back
`profile`), `hmc_inspect_lpar` reports the change and the volume's mapping,
`hmc_prepare_host_handoff` holds it for a run-labelled consumer (ADR 0193) while
`hmc_invoke_tool` dispatches a read and a hold-less start that the hold must refuse,
then releases it, `hmc_power_lpar` starts it (the change must be current) and stops it,
and `hmc_decommission_lpar` deletes it and the owned volume. Every other tool the arm
calls is an independent witness read, or teardown. The lpar-power arm's names,
baseline, teardown, comparison and recovery witness are reused.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hmcpctl.xmlutil import leaf_text

from ..observation import Assertion
from ..results import entries, resource
from ..results import field as result_field
from . import lpar_config, lpar_power
from .bare_cec import _power_operations_authorized

if TYPE_CHECKING:
    from fastmcp import Client

    from ..runner import RunState

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
        "hmc_prepare_host_handoff",
        "hmc_invoke_tool",
        "hmc_plan_lpar",
        "hmc_inventory",
    }
)
HANDOFF = "hmc_prepare_host_handoff"
INVOKE = "hmc_invoke_tool"
#: Within lpar-config's maximums (4096 MiB, 2 virtual processors), unlike its desired.
PATCH = {"memory_mib": 3072, "processors": 2}
_STOPPED = frozenset({lpar_power._NOT_ACTIVATED})
_SETTLED = frozenset({"terminal", "paused"})


async def _status(
    client: Client, state: RunState, request_id: str
) -> Mapping[str, Any] | None:
    """The recorded operation for *request_id*, or None when none is listed."""
    st, page = await state.call(client, "hmc_operation_status", request_id=request_id)
    found = [
        item
        for item in lpar_power._items(page, "operations")
        if item.get("request_id") == request_id
    ]
    return found[0] if st == "PASS" and found else None


async def _settle(
    client: Client, state: RunState, request_id: str, record: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """Poll *request_id* through `hmc_operation_status` until terminal or paused."""
    for _ in range(lpar_power._DECOMMISSION_POLLS):
        if record is None or record.get("state") in _SETTLED:
            break
        await asyncio.sleep(lpar_power._OPERATION_POLL_DELAY_S)
        record = await _status(client, state, request_id) or record
    return record


def _operation(st: str, data: object) -> Mapping[str, Any] | None:
    found = result_field(data, "operation") if st == "PASS" else None
    return found if isinstance(found, Mapping) else None


def _storage(run: lpar_power.Run) -> dict[str, Any]:
    """The run volume as provision creates it, which the plan is asked about too."""
    vios = run.vios
    assert vios is not None
    return {
        "vios_uuid": vios.uuid,
        "storage_name": run.volume,
        "kind": "VirtualDisk",
        "vg_uuid": vios.group_uuid,
        "capacity_mib": lpar_power.VOLUME_MIB,
    }


async def _plan(client: Client, state: RunState, run: lpar_power.Run) -> None:
    """Plan P with provision's own inputs; it writes nothing.

    Whether the plan targets the boundary system is judged once `hmc_inventory` has
    named that system's UUID.
    """
    st, data = await state.call(
        client,
        "hmc_plan_lpar",
        name=run.p_name,
        adapters={"port_vlan_id": run.vlan},
        storage=_storage(run),
        resources=lpar_power.RESOURCES,
        system_name_or_uuid=run.system,
    )
    state.record(SUBTASK, "hmc_plan_lpar", st, data)
    run.data["lpar.plan"] = data if st == "PASS" else None
    blockers = _field(data, "blockers")
    # plan_lpar sets the digest exactly when a candidate is selected and the request
    # carries no blocker of its own.
    run.hold(
        "lpar.plan",
        "plan-outcome-consistent",
        st == "PASS"
        and isinstance(blockers, list)
        and (_field(data, "plan_digest") is not None)
        == (_field(data, "selected") is not None and not blockers),
    )


async def _inventory(client: Client, state: RunState, run: lpar_power.Run) -> None:
    """The logical inventory of the run's system lists P; then judge the plan's target."""
    op = "inventory.logical"
    st, data = await state.call(client, "hmc_inventory", systems=[run.system])
    state.record(SUBTASK, "hmc_inventory", st, data)
    run.data[op] = data
    answered = st == "PASS"
    systems = _list(data, "systems") if answered else []
    partitions = _list(data, "partitions") if answered else []
    boundary = next(
        (s for s in systems if str(s.get("name") or "").lower() == run.system.lower()),
        None,
    )
    system_id = _field(boundary, "id")
    run.hold(op, "boundary-system-listed", isinstance(system_id, str))
    run.hold(
        op,
        "run-partition-listed",
        any(
            _same_uuid(p.get("uuid"), run.p_uuid) and p.get("name") == run.p_name
            for p in partitions
        ),
    )
    run.hold(
        op,
        "partitions-belong-to-system",
        isinstance(system_id, str)
        and bool(partitions)
        and all(p.get("system_id") == system_id for p in partitions),
    )
    # A denied or failed capacity or ownership part still lists the system and its
    # partitions, with those figures null.
    run.hold(
        op,
        "system-sources-read",
        all(
            _field(boundary, "sources", part, "status") == "ok"
            for part in ("capacity", "partitions", "ownership")
        ),
    )
    system_uuid = _field(boundary, "uuid")
    run.hold(
        "lpar.plan",
        "plan-targets-boundary-system",
        isinstance(system_uuid, str)
        and any(
            _same_uuid(_field(c, "targets", "system", "uuid"), system_uuid)
            for c in _list(run.data.get("lpar.plan"), "candidates")
        ),
    )


def _list(data: object, key: str) -> list[Mapping[str, Any]]:
    found = _field(data, key)
    return (
        [i for i in found if isinstance(i, Mapping)] if isinstance(found, list) else []
    )


async def _provision(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    """Create P inactive on the run volume; whether it exists as the run's."""
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
        storage=_storage(run),
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
    # ADR 0331: the saved profile is the last-activated one, so reconfigure accepts it.
    partition = lpar_config._section(
        await lpar_power._get_lpar(client, state, run, run.p_uuid), ()
    )
    profiles = {
        leaf_text(partition.get(name))
        for name in ("LastActivatedProfile", "DefaultProfileName")
    }
    run.data["profiles_after_provision"] = sorted(map(str, profiles))
    run.hold(
        op,
        "last-activated-is-default",
        len(profiles) == 1 and str(profiles.pop() or "").startswith("hmcpctl_"),
    )
    run.hold(op, "adapters-after-apply", await _adapters_held(client, state, run))
    return run.holds(op, "workflow-completed")


async def _adapters_held(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    """A client network adapter on the run VLAN and a vSCSI client adapter are listed."""
    st, network = await state.call(
        client,
        "hmc_list_adapters",
        lpar_name_or_uuid=run.p_uuid,
        adapter_type="ClientNetworkAdapter",
    )
    vlans = {str(resource(item).get("PortVLANID")) for item in entries(network)}
    st_scsi, scsi = await state.call(
        client,
        "hmc_list_adapters",
        lpar_name_or_uuid=run.p_uuid,
        adapter_type="VirtualSCSIClientAdapter",
    )
    return (
        st == "PASS"
        and str(run.vlan) in vlans
        and st_scsi == "PASS"
        and bool(entries(scsi))
    )


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
    changes = {
        change.get("field"): (change.get("after"), change.get("applied"))
        for change in result.get("changes") or []
    }
    run.hold(
        "lpar.reconfigure",
        "inactive-change-classified-profile",
        (record or {}).get("outcome") == "completed"
        and changes == {key: (value, "profile") for key, value in PATCH.items()},
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
    found = data if st == "PASS" else None
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


def _field(data: object, *path: str) -> Any:
    for key in path:
        data = data.get(key) if isinstance(data, Mapping) else None
    return data


def _same_uuid(found: object, wanted: str | None) -> bool:
    return (
        isinstance(found, str)
        and wanted is not None
        and found.lower() == wanted.lower()
    )


async def _handoff_call(
    client: Client,
    state: RunState,
    run: lpar_power.Run,
    action: str,
    consumer_label: str | None = None,
    hold_id: str | None = None,
) -> tuple[str, Any]:
    """One hand-off call; a consumer label places the hold, a hold_id releases it."""
    st, data = await state.call(
        client,
        "hmc_prepare_host_handoff",
        lpar_name_or_uuid=run.p_uuid,
        system_name_or_uuid=run.system,
        action=action,
        hold=consumer_label is not None,
        consumer_label=consumer_label,
        hold_id=hold_id,
    )
    hold = "hold" if consumer_label else "report"
    state.record(
        SUBTASK, f"{HANDOFF} ({hold if action == 'prepare' else action})", st, data
    )
    return st, data


async def _hold_reported(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    """Whether a hold-less prepare still reports a hold on P; True when unreadable."""
    st, data = await _handoff_call(client, state, run, "prepare")
    return st != "PASS" or _field(data, "hold") is not None


async def _place_hold(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    """Hold P for the run's label; whether the hold is placed as ADR 0193 states it."""
    op = "lpar.prepare_host_handoff"
    label = f"hmcpctl-live-{run.token}"
    run.data["handoff_label"] = label
    st, data = await _handoff_call(client, state, run, "prepare", label)
    run.data[op] = data
    hold_id = _field(data, "hold", "hold_id") if st == "PASS" else None
    run.data["hold_id"] = hold_id if isinstance(hold_id, str) and hold_id else None
    run.hold(
        op,
        "hold-placed",
        run.data["hold_id"] is not None
        and _field(data, "hold", "consumer_label") == label
        and _same_uuid(
            _field(data, "document", "identity", "partition_uuid"), run.p_uuid
        ),
    )
    document = json.dumps(_field(data, "document"), default=str)
    run.hold(
        op,
        "document-withholds-hold-id",
        run.data["hold_id"] is not None and run.data["hold_id"] not in document,
    )
    st, again = await _handoff_call(client, state, run, "prepare", label)
    run.hold(
        op,
        "repeat-returns-the-hold",
        st == "PASS"
        and run.data["hold_id"] is not None
        and _field(again, "hold", "hold_id") == run.data["hold_id"],
    )
    return run.holds(op, "hold-placed")


async def _invoke_cases(client: Client, state: RunState, run: lpar_power.Run) -> None:
    """Through the gateway: a read answers as directly; a hold-less start is refused."""
    st, data = await state.call(
        client,
        "hmc_invoke_tool",
        name="hmc_inspect_lpar",
        arguments={
            "lpar_name_or_uuid": run.p_uuid,
            "system_name_or_uuid": run.system,
            "include": ["resources"],
        },
    )
    state.record(SUBTASK, f"{INVOKE} (hmc_inspect_lpar)", st, data)
    run.data["tools.invoke.read"] = data
    run.hold(
        "tools.invoke",
        "dispatches-a-read-as-direct",
        st == "PASS"
        and _field(data, "name") == "hmc_inspect_lpar"
        and _same_uuid(_field(data, "result", "uuid"), run.p_uuid)
        and _field(data, "result", "resources", "desired_memory_mib")
        == PATCH["memory_mib"],
    )
    request_id = f"{run.token}-held-start"
    run.requests.setdefault(request_id, False)
    st, data = await state.call(
        client,
        "hmc_invoke_tool",
        name="hmc_power_lpar",
        arguments={
            "request_id": request_id,
            "lpar_name_or_uuid": run.p_uuid,
            "system_name_or_uuid": run.system,
            "action": "start",
            "wait_seconds": 600,
        },
    )
    state.record(SUBTASK, f"{INVOKE} (hmc_power_lpar held start)", st, data)
    record = await _status(client, state, request_id)
    record = await _settle(client, state, request_id, record)
    if record is None or record.get("state") == "terminal":
        run.requests[request_id] = True
    run.data["tools.invoke.held_start"] = {"call": data, "operation": record}
    refused = "partition_held" in json.dumps(
        {"call": data, "operation": record}, default=str
    ) and (record is None or record.get("outcome") == "failed")
    off = await lpar_power._lpar_state(client, state, run, run.p_uuid or "") in _STOPPED
    run.hold("tools.invoke", "dispatched-mutation-meets-the-hold", refused and off)
    run.hold("lpar.prepare_host_handoff", "hold-refuses-a-mutation", refused and off)


async def _release_hold(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    """Release P's hold as its placing agent; whether a later report finds none."""
    st, data = await _handoff_call(
        client, state, run, "release", hold_id=run.data.get("hold_id")
    )
    run.data["handoff_release"] = data
    released = not await _hold_reported(client, state, run)
    if released:
        run.data["hold_id"] = None
        run.data.pop("handoff_label", None)
    return (
        st == "PASS"
        and _field(data, "action") == "release"
        and _field(data, "document") is None
        and released
    )


async def _handoff(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    """Hold, prove the hold through the gateway, release; whether P is free again."""
    if await _place_hold(client, state, run):
        await _invoke_cases(client, state, run)
    released = await _release_hold(client, state, run)
    run.hold("lpar.prepare_host_handoff", "release-drops-the-hold", released)
    return released


async def _abandon_hold(client: Client, state: RunState, run: lpar_power.Run) -> bool:
    """Release a hold the workflow left; whether none is left (teardown needs none)."""
    if "handoff_label" not in run.data:
        return True
    released = await _release_hold(client, state, run)
    if not released:
        state.record(
            SUBTASK,
            f"{HANDOFF} (teardown release)",
            "FAIL",
            run.data.get("handoff_release"),
            "MANUAL RECOVERY REQUIRED: release the hold with hmcpctl holds release",
        )
    return released


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
    found = data if st == "PASS" else None
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
    # Decommission follows only once an independent read also says Not Activated.
    off = await lpar_power._wait_for_state(
        client, state, run, run.p_uuid or "", _STOPPED
    )
    run.hold(
        op,
        "stop-immediate-completed-not-activated",
        _moved(stop, _STOPPED) and off in _STOPPED,
    )
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
    await _plan(client, state, run)
    if not await _provision(client, state, run):
        return
    await _inventory(client, state, run)
    if not await _reconfigure(client, state, run):
        return
    await _inspect(client, state, run)
    if not await _handoff(client, state, run):
        return
    if await _power_cases(client, state, run):
        await lpar_power._decommission_cases(client, state, run)


async def _abandon_reconfigure(
    client: Client, state: RunState, run: lpar_power.Run
) -> bool:
    """Abandon a reconfigure left open; whether none is left open.

    A call that failed may still have recorded the operation, so status is asked once.
    The engine refuses to abandon an operation still running, so only a terminal
    answer counts.
    """
    request_id = run.data.get("reconfigure_request")
    if request_id is None:
        return True
    record = run.data.get("lpar.reconfigure")
    if record is None:
        record = await _status(client, state, request_id)
    if record is None or record.get("state") == "terminal":
        return True
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
    "lpar.plan": ("plan-outcome-consistent", "plan-targets-boundary-system"),
    "inventory.logical": (
        "boundary-system-listed",
        "run-partition-listed",
        "partitions-belong-to-system",
        "system-sources-read",
    ),
    "provision.lpar": (
        "workflow-completed",
        "ownership-stamped",
        "left-not-activated",
        "storage-mapping-listed",
        "last-activated-is-default",
        "adapters-after-apply",
    ),
    "lpar.reconfigure": ("inactive-change-classified-profile",),
    "lpar.inspect": ("reports-the-change-inactive", "reports-the-owned-mapping"),
    "lpar.prepare_host_handoff": (
        "hold-placed",
        "document-withholds-hold-id",
        "repeat-returns-the-hold",
        "hold-refuses-a-mutation",
        "release-drops-the-hold",
    ),
    "tools.invoke": (
        "dispatches-a-read-as-direct",
        "dispatched-mutation-meets-the-hold",
    ),
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
    if "lpar.plan" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_plan_lpar",
            operation="lpar.plan",
            scenario=SCENARIO,
            assertions=_assertions(run, "lpar.plan"),
            cleanup=cleanup,
            data=run.data.get("lpar.plan"),
        )
    if "inventory.logical" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_inventory",
            operation="inventory.logical",
            scenario=SCENARIO,
            assertions=_assertions(run, "inventory.logical"),
            cleanup=cleanup,
            data=run.data.get("inventory.logical"),
        )
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
    if "lpar.prepare_host_handoff" in run.ran:
        state.record_verified(
            SUBTASK,
            HANDOFF,
            operation="lpar.prepare_host_handoff",
            scenario=SCENARIO,
            assertions=_assertions(run, "lpar.prepare_host_handoff"),
            cleanup=cleanup,
            data={
                key: run.data.get(key)
                for key in ("lpar.prepare_host_handoff", "handoff_release")
            },
        )
    if "tools.invoke" in run.ran:
        state.record_verified(
            SUBTASK,
            INVOKE,
            operation="tools.invoke",
            scenario=SCENARIO,
            assertions=_assertions(run, "tools.invoke"),
            cleanup=cleanup,
            data={
                key: run.data.get(key)
                for key in ("tools.invoke.read", "tools.invoke.held_start")
            },
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
    print("\n=== ST73: logical plan, create, reconfigure, hand-off and destroy ===")
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
        try:
            released = await _abandon_hold(client, state, run)
            closed = await _abandon_reconfigure(client, state, run) and released
        finally:
            await lpar_power._teardown(client, state, run)
    same = await lpar_power._compare(client, state, run, ready)
    _observe(state, run, "passed" if closed and run.clean and same else "failed")
