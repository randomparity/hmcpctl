"""ST70: hmc_reconfigure_lpar's persistence across profile activation (ADRs 0314, 0322).

One owned scratch partition, lpar-config's fixture and teardown. Inactive, a processor
and memory change must read back `profile` and survive a profile activation; a rename
must survive both activations; at SMS (`open firmware`, RMC inactive) a change must read
back `pending_activation`, stay out of the running partition, and appear after a power
off and a second profile activation. Live DLPAR is a recorded SKIP: the operator has not
authorized it on the boundary partition (2026-11-02), and a scratch partition has no
operating system to bring RMC up.

Added disks (#1846): while inactive, one `lprcf<8 hex>` disk is added in the configured
volume group. It must read back `profile`, its client adapter must be in the default
profile, and after the first activation in the running partition. After the final power
off a detach must remove only its mapping. Teardown detaches what is left, deletes the
disk, and reports a VIOS server adapter the run left as a manual step.
"""

from __future__ import annotations

import csv
import re
import shlex
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hmcpctl.ssh.commands import HMC_NO_RESULTS, build_filter
from hmcpctl.xmlutil import leaf_text

from . import storage_lifecycle
from .lpar_config import (
    NAME_PREFIX,
    RESOURCES,
    TOKEN_PREFIX,
    _cli,
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
from .results import entries, resource
from .results import field as result_field
from .storage import scsi_adapter_listing

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
VOLUME_PREFIX = "lprcf"
DISK_MIB = 1024
_VIOS_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_RECOVERY_PREFIX = "hmcprec-"
DISK_ASSERTIONS = (
    "added-disk-classified-profile",
    "added-disk-in-default-profile",
    "added-disk-survives-profile-activation",
    "detach-removes-only-the-mapping",
)
LIVE_DLPAR_SKIP = (
    "live DLPAR on the boundary test LPAR is not authorized (operator 2026-11-02); "
    "a scratch partition never reaches RMC active"
)


def scratch_volumes(names: Iterable[str]) -> list[str]:
    """The volume names only this arm may have created."""
    return sorted(name for name in names if name.startswith(VOLUME_PREFIX))


@dataclass
class _Disk:
    """The VIOS holding the configured group, and what the run did there."""

    vios_uuid: str
    partition_id: int
    group: str
    group_uuid: str
    name: str
    adapters: frozenset[str] | None = None
    attempted: bool = False


def _lines(text: str | None) -> frozenset[str] | None:
    if text is None:
        return None
    if text.strip() == HMC_NO_RESULTS:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


async def _find_disk_group(
    client: Client, state: RunState, run: _Run, name: str
) -> _Disk | str:
    """The one VIOS listing the configured group with 1 GiB free, or why not."""
    group = state.config.vdisk_volume_group_name
    if not _VIOS_NAME.fullmatch(group):
        return f"volume group name {group!r} cannot cross the VIOS command line"
    st, data = await state.call(client, "hmc_list_vios", system_name_or_uuid=run.system)
    if st != "PASS":
        return "the VIOS listing failed"
    found = []
    for entry in entries(data):
        vios_uuid = entry.get("UUID") or entry.get("uuid")
        partition_id = str(resource(entry).get("PartitionID") or "")
        if not isinstance(vios_uuid, str) or not partition_id.isdigit():
            continue
        st, groups = await state.call(
            client,
            "hmc_list_volume_groups",
            vios_name_or_uuid=vios_uuid,
            system_name_or_uuid=run.system,
        )
        for item in groups if st == "PASS" and isinstance(groups, list) else []:
            free = item.get("free_space_gib") if isinstance(item, Mapping) else None
            if (
                item.get("name") == group
                and isinstance(free, (int, float))
                and free >= 1
            ):
                found.append(
                    _Disk(vios_uuid, int(partition_id), group, item["uuid"], name)
                )
    if len(found) != 1:
        return (
            f"{len(found)} VIOS list {group!r} with 1 GiB free; exactly one is needed"
        )
    return found[0]


async def _server_adapters(
    client: Client, state: RunState, run: _Run, disk: _Disk
) -> frozenset[str] | None:
    listing = scsi_adapter_listing(run.system, "lpar_ids", disk.partition_id)
    return _lines(await _cli(client, state, listing))


async def _disk_mappings(
    client: Client, state: RunState, run: _Run, disk: _Disk
) -> list[Mapping[str, Any]] | None:
    """The VIOS mappings backed by the run disk; None when unreadable."""
    st, data = await state.call(
        client,
        "hmc_list_storage_mappings",
        vios_name_or_uuid=disk.vios_uuid,
        system_name_or_uuid=run.system,
    )
    if st != "PASS" or not isinstance(data, list):
        return None
    return [
        entry
        for entry in data
        if isinstance(entry, Mapping) and entry.get("backing_name") == disk.name
    ]


async def _mapped_here(client: Client, state: RunState, run: _Run, disk: _Disk) -> bool:
    found = await _disk_mappings(client, state, run, disk)
    return bool(found) and all(
        str(entry.get("lpar_uuid") or "").lower() == (run.uuid or "").lower()
        for entry in found
    )


async def _volumes(client: Client, state: RunState, run: _Run, disk: _Disk) -> Any:
    command = (
        f"viosvrcmd -m {shlex.quote(run.system)} --id {disk.partition_id} "
        f"-c 'lsvg -lv {disk.group}'"
    )
    return storage_lifecycle.volume_names(await _cli(client, state, command))


async def _profile_lists_vscsi(client: Client, state: RunState, run: _Run) -> bool:
    """Whether the partition's one non-recovery profile lists a vSCSI client adapter."""
    listing = await _cli(
        client,
        state,
        f"lssyscfg -r prof -m {shlex.quote(run.system)} --filter "
        f"{shlex.quote(build_filter([('lpar_names', run.name)]))} "
        "-F name,virtual_scsi_adapters",
    )
    rows = [
        row
        for row in csv.reader((listing or "").splitlines())
        if row and not row[0].startswith(_RECOVERY_PREFIX)
    ]
    return len(rows) == 1 and any("client" in item for item in rows[0][1:])


async def _running_vscsi(client: Client, state: RunState, run: _Run) -> bool:
    """Whether the activated partition holds a vSCSI client adapter."""
    listing = await _cli(
        client,
        state,
        f"lshwres -r virtualio --rsubtype scsi -m {shlex.quote(run.system)} "
        f"--level lpar --filter {shlex.quote(build_filter([('lpar_names', run.name)]))} "
        "-F adapter_type",
    )
    return "client" in (_lines(listing) or frozenset())


async def _add_disk(
    client: Client, state: RunState, run: _Run, disk: _Disk, held: dict[str, bool]
) -> None:
    disk.adapters = await _server_adapters(client, state, run, disk)
    disk.attempted = True
    add = {
        "vios_uuid": disk.vios_uuid,
        "vg_uuid": disk.group_uuid,
        "name": disk.name,
        "capacity_mib": DISK_MIB,
    }
    record = await _reconfigure(
        client, state, run, "disk-add", {"disks": {"add": [add]}}
    )
    held["added-disk-classified-profile"] = _applied(record) == {"profile"}
    held["added-disk-in-default-profile"] = record.get(
        "outcome"
    ) == "completed" and await _profile_lists_vscsi(client, state, run)


async def _detach_disk(
    client: Client, state: RunState, run: _Run, disk: _Disk, held: dict[str, bool]
) -> None:
    patch = {"disks": {"detach": [{"vios_uuid": disk.vios_uuid, "name": disk.name}]}}
    record = await _reconfigure(client, state, run, "disk-detach", patch)
    left = await _disk_mappings(client, state, run, disk)
    volumes = await _volumes(client, state, run, disk)
    held["detach-removes-only-the-mapping"] = (
        record.get("outcome") == "completed"
        and left == []
        and volumes is not None
        and disk.name in volumes
    )


def _manual(state: RunState, label: str, text: str) -> None:
    state.record(SUBTASK, label, "FAIL", f"MANUAL RECOVERY REQUIRED: {text}")


async def _release_mappings(
    client: Client, state: RunState, run: _Run, disk: _Disk
) -> bool:
    """Detach any mapping of the run disk while the partition still exists."""
    for entry in await _disk_mappings(client, state, run, disk) or []:
        st, data = await state.call(
            client,
            "hmc_detach_storage_mapping",
            vios_name_or_uuid=disk.vios_uuid,
            mapping_id=entry.get("id"),
            system_name_or_uuid=run.system,
        )
        state.record(SUBTASK, "hmc_detach_storage_mapping (run disk)", st, data)
    return await _disk_mappings(client, state, run, disk) == []


async def _delete_disk(client: Client, state: RunState, run: _Run, disk: _Disk) -> None:
    """Delete the run disk and report a server adapter the run left (manual)."""
    system = shlex.quote(run.system)
    volumes = await _volumes(client, state, run, disk)
    if volumes is not None and disk.name in volumes:
        st, data = await state.call(
            client,
            "hmc_delete_virtual_disk",
            vios_name_or_uuid=disk.vios_uuid,
            vg_uuid=disk.group_uuid,
            disk_name=disk.name,
            system_name_or_uuid=run.system,
        )
        state.record(SUBTASK, "hmc_delete_virtual_disk (run disk)", st, data)
        volumes = await _volumes(client, state, run, disk)
    if volumes is None or disk.name in volumes:
        _manual(
            state,
            "run disk teardown",
            f"volume {disk.name!r} may still be in {disk.group!r}: `viosvrcmd -m {system} "
            f"--id {disk.partition_id} -c 'lsvg -lv {disk.group}'`, then rmlv -f it.",
        )
    final = await _server_adapters(client, state, run, disk)
    if final is None or disk.adapters is None:
        _manual(
            state,
            "VIOS server adapter teardown",
            f"the vSCSI adapters of VIOS id {disk.partition_id} could not be compared; "
            "look for one serving the deleted scratch partition",
        )
        return
    for row in sorted(final - disk.adapters):
        slot = row.split(",", 1)[0]
        _manual(
            state,
            "VIOS server adapter teardown",
            f"VIOS id {disk.partition_id} has server adapter slot {slot} ({row}) it did "
            f"not have before the run: `chhwres -r virtualio --rsubtype scsi -m {system} "
            f"-o r --id {disk.partition_id} -s {slot}`.",
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
    """Desired memory and virtual processors in the partition's one profile that is not
    a force-save's recovery copy."""
    st, data = await state.call(
        client,
        "hmc_list_partition_profiles",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
    )
    profiles = result_field(plain_data(data), "profiles") if st == "PASS" else None
    if isinstance(profiles, list):
        profiles = [
            p
            for p in profiles
            if not str((p or {}).get("name") or "").startswith(_RECOVERY_PREFIX)
        ]
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
    client: Client,
    state: RunState,
    run: _Run,
    held: dict[str, bool],
    disk: _Disk | None,
) -> None:
    """Run the steps in order; a failed step ends the scenario, never the cleanup."""
    if not await _create(client, state, run):
        return
    if disk is not None:
        await _add_disk(client, state, run, disk, held)
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
    if disk is not None and held.get("added-disk-in-default-profile"):
        held["added-disk-survives-profile-activation"] = await _running_vscsi(
            client, state, run
        ) and await _mapped_here(client, state, run, disk)
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
    if disk is not None and disk.attempted and await _power_off(client, state, run):
        run.data["activated"] = False
        await _detach_disk(client, state, run, disk, held)


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
    found = await _find_disk_group(client, state, run, VOLUME_PREFIX + suffix)
    disk = found if isinstance(found, _Disk) else None
    if disk is None:
        state.record(SUBTASK, f"{TOOL} (disks)", "FAIL", None, str(found))
    released = True
    try:
        await _scenarios(client, state, run, held, disk)
    finally:
        await _abandon_open(client, state, run)
        if run.data.get("activated") and run.uuid is not None:
            await _power_off(client, state, run)
        if disk is not None and disk.attempted and run.uuid is not None:
            released = await _release_mappings(client, state, run, disk)
        await _teardown(client, state, run, inactive_only=True)
        if disk is not None and disk.attempted:
            if released:
                await _delete_disk(client, state, run, disk)
            else:
                _manual(
                    state,
                    "run disk mapping teardown",
                    f"a mapping backed by {disk.name!r} is still on VIOS id "
                    f"{disk.partition_id}; rmvdev it, then rmlv -f {disk.name}",
                )
    same = await _compare(client, state, run, baseline[0])
    names = (
        "inactive-change-classified-profile",
        "inactive-change-survives-profile-activation",
        "firmware-change-classified-pending-activation",
        "pending-change-survives-profile-activation",
        "rename-persists-across-activations",
        *DISK_ASSERTIONS,
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
            "disk_mib": DISK_MIB,
            "live": "skipped",
        },
    )
