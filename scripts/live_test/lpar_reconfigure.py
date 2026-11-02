"""ST70: hmc_reconfigure_lpar's persistence across profile activation (ADR 0314, #1226).

One owned scratch partition, lpar-config's fixture and teardown. Inactive, a processor
and memory change must read back `profile` and survive a profile activation; a rename
must survive both activations; at SMS (`open firmware`, RMC inactive) a change must read
back `pending_activation`, stay out of the running partition, and appear after a power
off and a second profile activation. Live DLPAR is a recorded SKIP: the operator has not
authorized it on the boundary partition (2026-11-02), and a scratch partition has no
operating system to bring RMC up.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hmcpctl.xmlutil import leaf_text

from .lpar_config import (
    NAME_PREFIX,
    RESOURCES,
    TOKEN_PREFIX,
    _compare,
    _profile_uuid,
    _read_lpar,
    _read_pools,
    _Run,
    _section,
    _teardown,
    _wait_for_state,
    scratch_partitions,
)
from .observation import Assertion, judge_create_result, plain_data
from .results import field as result_field

if TYPE_CHECKING:
    from fastmcp import Client

    from .runner import RunState

GROUP = "lpar-reconfigure"
SUBTASK = 70
SCENARIO = "st70-lpar-reconfigure"
TOOL = "hmc_reconfigure_lpar"
_JOB_TIMEOUT_S = 900
_JOB_POLL_INTERVAL_S = 10
_NOT_ACTIVATED = "not activated"
_FIRMWARE_STATES = frozenset({"open firmware", "running"})
#: Inside RESOURCES' saved bounds (memory 1024–4096 MiB, 1–2 virtual processors), so
#: no bound widens and the classification is the state's alone.
INACTIVE_PATCH = {"memory_mib": 3072, "processors": 2}
FIRMWARE_PATCH = {"memory_mib": 2560, "processors": 1}
LIVE_DLPAR_SKIP = (
    "live DLPAR on the boundary test LPAR is not authorized (operator 2026-11-02); "
    "a scratch partition never reaches RMC active"
)


async def _reconfigure(
    client: Client, state: RunState, run: _Run, label: str, patch: dict[str, Any]
) -> Mapping[str, Any]:
    """One operation, waited for; the record, or an empty mapping on a refusal."""
    request_id = f"{run.token}-{label}"
    run.data.setdefault("requests", []).append(request_id)
    st, data = await state.call(
        client,
        "hmc_reconfigure_lpar",
        request_id=request_id,
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        patch=patch,
        exclusive_writer_window=any(k != "name" for k in patch),
        wait_seconds=600,
    )
    record = plain_data(data) if st == "PASS" else None
    done = isinstance(record, Mapping) and record.get("outcome") == "completed"
    state.record(SUBTASK, f"{TOOL} ({label})", "PASS" if done else "FAIL", data)
    return record if isinstance(record, Mapping) else {}


def _applied(record: Mapping[str, Any]) -> set[str]:
    result = record.get("result") or {}
    return {change.get("applied") for change in result.get("changes") or []}


async def _current(client: Client, state: RunState, run: _Run) -> dict[str, Any]:
    """Name, current memory and allocated virtual processors, as the HMC reports them."""
    st, data = await _read_lpar(client, state, run)
    if st != "PASS":
        return {}
    memory = _section(data, ("PartitionMemoryConfiguration",))
    cpu = _section(
        data,
        ("PartitionProcessorConfiguration", "CurrentSharedProcessorConfiguration"),
    )
    return {
        "name": leaf_text(_section(data, ()).get("PartitionName")),
        "memory_mib": _int(memory.get("CurrentMemory")),
        "processors": _int(cpu.get("AllocatedVirtualProcessors")),
    }


async def _saved(client: Client, state: RunState, run: _Run) -> dict[str, Any]:
    """Desired memory and virtual processors in the partition's only profile."""
    st, data = await state.call(
        client,
        "hmc_list_partition_profiles",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
    )
    profiles = result_field(plain_data(data), "profiles") if st == "PASS" else None
    if not isinstance(profiles, list) or len(profiles) != 1:
        return {}
    fields = profiles[0].get("fields") or {}
    shared = (fields.get("ProcessorAttributes") or {}).get(
        "SharedProcessorConfiguration"
    ) or {}
    return {
        "memory_mib": _int((fields.get("ProfileMemory") or {}).get("DesiredMemory")),
        "processors": _int(shared.get("DesiredVirtualProcessors")),
    }


def _int(value: object) -> int | None:
    try:
        return int(str(leaf_text(value)))
    except ValueError:
        return None


def _holds(observed: Mapping[str, Any], patch: Mapping[str, Any]) -> bool:
    return all(observed.get(key) == value for key, value in patch.items())


async def _activate(client: Client, state: RunState, run: _Run, label: str) -> bool:
    """Activate the partition's profile to SMS; whether it reached firmware."""
    st, data = await _read_lpar(client, state, run)
    profile_uuid = _profile_uuid(data) if st == "PASS" else None
    if profile_uuid is None:
        state.record(
            SUBTASK, f"profile activation ({label})", "FAIL", data, "no profile"
        )
        return False
    st, data = await state.call(
        client,
        "hmc_power_on_lpar",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        partition_profile_uuid=profile_uuid,
        boot_mode="sms",
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    reached = await _wait_for_state(client, state, run, _FIRMWARE_STATES)
    run.data["activated"] = True
    ok = st == "PASS" and reached in _FIRMWARE_STATES
    state.record(
        SUBTASK, f"hmc_power_on_lpar ({label})", "PASS" if ok else "FAIL", data
    )
    return ok


async def _power_off(client: Client, state: RunState, run: _Run) -> bool:
    """Immediate power off, recorded under this arm's own subtask."""
    _, data = await state.call(
        client,
        "hmc_power_off_lpar",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        immediate=True,
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    stopped = (
        await _wait_for_state(client, state, run, frozenset({_NOT_ACTIVATED}))
        == _NOT_ACTIVATED
    )
    state.record(
        SUBTASK, "hmc_power_off_lpar (immediate)", "PASS" if stopped else "FAIL", data
    )
    return stopped


async def _create(client: Client, state: RunState, run: _Run) -> bool:
    run.create_attempted = True
    st, data = await state.call(
        client,
        "hmc_create_lpar",
        system_name_or_uuid=run.system,
        name=run.name,
        caller_token=run.token,
        resources=RESOURCES,
    )
    status, reason = judge_create_result(st, data)
    state.record(SUBTASK, "hmc_create_lpar (scratch)", status, data, reason)
    created = result_field(data, "lpar") if st == "PASS" else None
    if isinstance(created, Mapping) and isinstance(created.get("UUID"), str):
        run.uuid = created["UUID"]
    run.created = status == "PASS" and run.uuid is not None
    return run.created


async def _scenarios(
    client: Client, state: RunState, run: _Run, held: dict[str, bool]
) -> None:
    """Run the steps in order; a failed step ends the scenario, never the cleanup."""
    if not await _create(client, state, run):
        return
    record = await _reconfigure(client, state, run, "inactive", INACTIVE_PATCH)
    held["inactive-change-classified-profile"] = _applied(record) == {"profile"}
    if not held["inactive-change-classified-profile"]:
        return
    renamed = f"{run.name}-r"
    record = await _reconfigure(client, state, run, "rename", {"name": renamed})
    if record.get("outcome") != "completed":
        return
    run.name = renamed
    if not await _activate(client, state, run, "profile, after the inactive change"):
        return
    current = await _current(client, state, run)
    held["inactive-change-survives-profile-activation"] = _holds(
        current, INACTIVE_PATCH
    )
    record = await _reconfigure(client, state, run, "firmware", FIRMWARE_PATCH)
    held["firmware-change-classified-pending-activation"] = (
        _applied(record) == {"pending_activation"}
        and _holds(await _saved(client, state, run), FIRMWARE_PATCH)
        and _holds(await _current(client, state, run), INACTIVE_PATCH)
    )
    if not await _power_off(client, state, run):
        return
    if not await _activate(client, state, run, "profile, after the pending change"):
        return
    current = await _current(client, state, run)
    held["pending-change-survives-profile-activation"] = _holds(current, FIRMWARE_PATCH)
    held["rename-persists-across-activations"] = current.get("name") == renamed


async def _abandon_open(client: Client, state: RunState, run: _Run) -> None:
    for request_id in run.data.get("requests", []):
        st, data = await state.call(
            client,
            "hmc_reconfigure_lpar",
            request_id=request_id,
            continuation="abandon",
            wait_seconds=0,
        )
        outcome = result_field(plain_data(data), "outcome") if st == "PASS" else None
        if outcome not in {"completed", "abandoned", "failed"}:
            state.record(SUBTASK, f"{TOOL} (abandon {request_id})", "FAIL", data)


async def exercise_lpar_reconfigure(client: Client, state: RunState) -> None:
    """One owned scratch partition; profile and activation persistence only."""
    if state.group != GROUP:
        state.skip(SUBTASK, GROUP, "requires the opt-in lpar-reconfigure group")
        return
    state.skip(SUBTASK, f"{TOOL} (live DLPAR)", LIVE_DLPAR_SKIP)
    baseline = await _read_pools(client, state, state.config.system_name)
    if baseline is None or scratch_partitions(baseline[0].names):
        state.skip(
            SUBTASK, GROUP, "baseline unavailable or stranded scratch; run recovery"
        )
        return
    suffix = uuid.uuid4().hex[:8]
    run = _Run(
        state.config.system_name,
        NAME_PREFIX + suffix,
        TOKEN_PREFIX + suffix,
        baseline[1],
        subtask=SUBTASK,
    )
    held: dict[str, bool] = {}
    try:
        await _scenarios(client, state, run, held)
    finally:
        await _abandon_open(client, state, run)
        if run.data.get("activated") and run.uuid is not None:
            await _power_off(client, state, run)
        await _teardown(client, state, run, inactive_only=True)
    same = await _compare(client, state, run, baseline[0])
    names = (
        "inactive-change-classified-profile",
        "inactive-change-survives-profile-activation",
        "firmware-change-classified-pending-activation",
        "pending-change-survives-profile-activation",
        "rename-persists-across-activations",
    )
    state.record_verified(
        SUBTASK,
        TOOL,
        operation="lpar.reconfigure",
        scenario=SCENARIO,
        assertions=[Assertion(name, held.get(name, False)) for name in names],
        cleanup="passed" if run.deleted and same else "failed",
        data={
            "inactive": INACTIVE_PATCH,
            "firmware": FIRMWARE_PATCH,
            "live": "skipped",
        },
    )
