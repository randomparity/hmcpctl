"""LPAR power and lifecycle scenarios: the lpar-power arm (#1346).

Subtask 41 creates one run-unique partition (A) and exercises create's refusals,
power on and off, the composite `hmc_power_lpar`, console capture and delete on it.
It then provisions a second one (P) with a run-owned 1 GiB logical volume on the
VIOS that holds the configured volume group, decommissions it, and deletes the
volume. It touches no other partition: every mutating call names a run partition
(by UUID once it has one), the run's volume, or a mapping backed by it.

Design: docs/workflow/specs/2026-10-06-lpar-power-contract-verification-design.md.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import shlex
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from hmcpctl.jobs import SUCCESSFUL_JOB_STATUSES, job_outcome
from hmcpctl.operations.lpar.decommission import _vscsi_detach_inventory
from hmcpctl.operations.lpar.ownership import parse_lpar_ownership_caller_token
from hmcpctl.operations.storage.resources import _storage_mapping
from hmcpctl.ssh.commands import HMC_NO_RESULTS
from hmcpctl.ssh.lpar import DEFAULT_PROFILE_NAME

from . import dedicated_pcie, lpar_config
from .bare_cec import _power_operations_authorized
from .network import listed_vlans
from .observation import Assertion, CallFailure, judge_create_result
from .results import entries, resource
from .results import field as result_field
from .vmedia import scsi_adapter_listing

if TYPE_CHECKING:
    from live_test_runner import RunState

SUBTASK = 41
GROUP = "lpar-power"
#: Reserved for this arm: recovery reports any partition or volume carrying them.
NAME_PREFIX = "hmcpctl-live-pwr-"
TOKEN_PREFIX = "lparpwr-"
VOLUME_PREFIX = "lppwr"
SCENARIO = "st41-lpar-power"

RESOURCES = lpar_config.RESOURCES
VOLUME_MIB = 1024
#: Shared units above the one virtual processor that can carry them (#1034).
_UNITS_OVER_VCPUS = {**RESOURCES, "desired_procs": 1.5, "max_procs": 2.0}
_UNITS_OVER_VCPUS |= {"desired_vcpus": 1, "max_vcpus": 2}
#: 64 TiB: above any POWER9 system's configurable memory (#1166).
_HUGE_MIB = 64 * 1024 * 1024
_MEMORY_OVER_SYSTEM = {**RESOURCES, "desired_memory": _HUGE_MIB}
_MEMORY_OVER_SYSTEM |= {"max_memory": _HUGE_MIB}

_JOB_TIMEOUT_S = 900
_JOB_POLL_INTERVAL_S = 10
_STATE_POLL_ATTEMPTS = 30
_STATE_POLL_DELAY_S = 10.0
_OPERATION_POLLS = 10
_OPERATION_POLL_DELAY_S = 30.0
_COMPARE_REREAD_DELAY_S = 30.0
_NOT_ACTIVATED = "not activated"
_FIRMWARE_STATES = frozenset({"open firmware", "running"})
_ACTIVATED_STATES = _FIRMWARE_STATES | {"starting"}
#: A VIOS object name the `viosvrcmd -c '...'` string carries unquoted.
_VIOS_NAME = re.compile(r"[A-Za-z0-9_.-]+")

#: The paths this arm does not take, each with what taking it needs (spec step 13).
GAPS = (
    (
        "hmc_dump_restart_lpar",
        (
            "gap: needs orchestrator approval to run bare-cec with "
            "LIVE_TEST_ACCEPT_PLATFORM_DUMP=true, whose dumprestart row would need to "
            "become a record_verified site; this arm builds no dump path"
        ),
    ),
    (
        "hmc_provision_lpar (SR-IOV and vNIC arguments)",
        (
            "gap: needs orchestrator-relayed operator approval to consume shared SR-IOV "
            "adapter capacity"
        ),
    ),
    (
        "hmc_power_lpar (graceful stop and restart)",
        "gap: needs an operating system with an active RMC connection",
    ),
    (
        "hmc_power_on_system / hmc_power_off_system",
        "gap: needs an operator window to power the whole managed system",
    ),
)


@dataclass(frozen=True)
class Vios:
    """The VIOS holding the configured volume group, and that group."""

    uuid: str
    partition_id: int
    group: str
    group_uuid: str


@dataclass(frozen=True)
class Baseline:
    """The system-wide reads the run must leave as it found them (spec step 3)."""

    partitions: frozenset[str]
    procs: str
    memory: str
    slots: frozenset[str]
    mappings: frozenset[tuple[str, ...]] | None = None
    adapters: frozenset[str] | None = None
    volumes: frozenset[str] | None = None


@dataclass
class Run:
    """What the arm has established, read by teardown and the deferred observations."""

    system: str
    suffix: str
    vios: Vios | None = None
    vlan: int = 0
    a_uuid: str | None = None
    p_uuid: str | None = None
    volume_attempted: bool = False
    decommission_failed: bool = False
    requests: dict[str, bool] = field(default_factory=dict)
    ran: set[str] = field(default_factory=set)
    held: dict[str, bool] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    clean: bool = False

    @property
    def base(self) -> str:
        return NAME_PREFIX + self.suffix

    @property
    def a_name(self) -> str:
        return self.base

    @property
    def p_name(self) -> str:
        return self.base + "-p"

    @property
    def token(self) -> str:
        return TOKEN_PREFIX + self.suffix

    @property
    def volume(self) -> str:
        return VOLUME_PREFIX + self.suffix

    def hold(self, operation: str, assertion: str, value: bool) -> None:
        self.ran.add(operation)
        self.held[f"{operation}:{assertion}"] = bool(value)

    def holds(self, operation: str, assertion: str) -> bool:
        return self.held.get(f"{operation}:{assertion}", False)


def scratch_partitions(names: Iterable[str]) -> list[str]:
    """The partition names only this arm may have created."""
    return sorted(name for name in names if name.startswith(NAME_PREFIX))


def scratch_volumes(names: Iterable[str]) -> list[str]:
    """The logical-volume names only this arm may have created."""
    return sorted(name for name in names if name.startswith(VOLUME_PREFIX))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _lines(text: str | None) -> frozenset[str] | None:
    if text is None:
        return None
    if text.strip() == HMC_NO_RESULTS:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


def _volume_names(text: str | None) -> frozenset[str] | None:
    """The volume names in `lsvg -lv` output: the first token below its header."""
    rows = _lines(text)
    if rows is None:
        return None
    return frozenset(
        row.split()[0]
        for row in rows
        if not row.endswith(":") and not row.startswith("LV NAME")
    )


def _failed_with(status: str, data: object, text: str) -> bool:
    return status == "FAIL" and isinstance(data, CallFailure) and text in data.message


async def _partition_lines(client: Client, state: RunState, system: str) -> Any:
    quoted = shlex.quote(system)
    return _lines(
        await lpar_config._cli(
            client, state, f"lssyscfg -r lpar -m {quoted} -F name,state"
        )
    )


def _names(partitions: Iterable[str]) -> set[str]:
    return {line.rsplit(",", 1)[0] for line in partitions}


async def _mappings(
    client: Client, state: RunState, run: Run
) -> frozenset[tuple[str, ...]] | None:
    assert run.vios is not None
    st, data = await state.call(
        client,
        "hmc_list_storage_mappings",
        vios_name_or_uuid=run.vios.uuid,
        system_name_or_uuid=run.system,
    )
    if st != "PASS" or not isinstance(data, list):
        return None
    return frozenset(
        tuple(
            str(entry.get(key) or "")
            for key in ("id", "lpar_uuid", "backing_kind", "backing_name")
        )
        for entry in data
        if isinstance(entry, Mapping)
    )


async def _complete_mapping_proof(
    client: Client, state: RunState, run: Run
) -> tuple[frozenset[tuple[str, ...]], tuple[str, ...]] | None:
    assert run.vios is not None
    st, data = await state.call(
        client,
        "hmc_get_vios_storage_detail",
        vios_name_or_uuid=run.vios.uuid,
        system_name_or_uuid=run.system,
    )
    state.record(SUBTASK, "decommission vSCSI proof", st, data)
    if st != "PASS" or not isinstance(data, Mapping):
        return None
    resource = data.get("Resource")
    if not isinstance(resource, dict):
        return None
    _, errors = _vscsi_detach_inventory(resource, run.vios.uuid, run.p_uuid or "")
    if errors:
        return None
    block = resource["VirtualSCSIMappings"]
    raw = block.get("VirtualSCSIMapping", []) if isinstance(block, dict) else []
    records = raw if isinstance(raw, list) else [raw]
    projected: set[tuple[str, ...]] = set()
    foreign: list[str] = []
    for item in records:
        decoded = _storage_mapping(item)
        projected.add(
            tuple(
                str(value or "")
                for value in (
                    decoded.id,
                    decoded.lpar_uuid,
                    decoded.backing_kind,
                    decoded.backing_name,
                )
            )
        )
        if (decoded.lpar_uuid or "").casefold() != (run.p_uuid or "").casefold():
            foreign.append(json.dumps(item, sort_keys=True, separators=(",", ":")))
    return frozenset(projected), tuple(sorted(foreign))


async def _volumes(client: Client, state: RunState, run: Run) -> frozenset[str] | None:
    assert run.vios is not None
    command = (
        f"viosvrcmd -m {shlex.quote(run.system)} --id {run.vios.partition_id} "
        f"-c 'lsvg -lv {run.vios.group}'"
    )
    return _volume_names(await lpar_config._cli(client, state, command))


async def _read_baseline(client: Client, state: RunState, run: Run) -> Baseline | None:
    """Every read of spec step 3; None when any of them failed."""
    quoted = shlex.quote(run.system)
    partitions = await _partition_lines(client, state, run.system)
    pools = await lpar_config._read_pools(client, state, run.system)
    slots = _lines(
        await lpar_config._cli(
            client,
            state,
            f"lshwres -r io --rsubtype slot -m {quoted} -F drc_index,lpar_name",
        )
    )
    if partitions is None or pools is None or slots is None:
        return None
    baseline = Baseline(partitions, pools[0].procs, pools[0].memory, slots)
    if run.vios is None:
        return baseline
    mappings = await _mappings(client, state, run)
    adapters = _lines(
        await lpar_config._cli(
            client,
            state,
            scsi_adapter_listing(run.system, "lpar_ids", run.vios.partition_id),
        )
    )
    volumes = await _volumes(client, state, run)
    if mappings is None or adapters is None or volumes is None:
        return None
    return Baseline(
        partitions, baseline.procs, baseline.memory, slots, mappings, adapters, volumes
    )


def _baseline_data(baseline: Baseline | None) -> dict[str, Any] | None:
    if baseline is None:
        return None
    return {
        "partitions": sorted(baseline.partitions),
        "available_processing_units": baseline.procs,
        "available_memory_mib": baseline.memory,
        "io_slots": sorted(baseline.slots),
        "vios_mappings": sorted(baseline.mappings or ()),
        "vios_server_adapters": sorted(baseline.adapters or ()),
        "volumes": sorted(baseline.volumes or ()),
    }


async def _lpar_state(client: Client, state: RunState, run: Run, lpar: str) -> Any:
    st, data = await state.call(
        client,
        "hmc_get_lpar_state",
        lpar_name_or_uuid=lpar,
        system_name_or_uuid=run.system,
    )
    return data.strip().lower() if st == "PASS" and isinstance(data, str) else None


async def _wait_for_state(
    client: Client, state: RunState, run: Run, lpar: str, wanted: frozenset[str]
) -> str | None:
    observed = None
    for attempt in range(_STATE_POLL_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_STATE_POLL_DELAY_S)
        observed = await _lpar_state(client, state, run, lpar)
        if observed in wanted:
            break
    return observed


async def _token_of(client: Client, state: RunState, run: Run, lpar: str) -> Any:
    st, data = await state.call(
        client,
        "hmc_get_lpar_description",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=lpar,
    )
    if st != "PASS" or not isinstance(data, str):
        return None
    return parse_lpar_ownership_caller_token(data)


async def _get_lpar(client: Client, state: RunState, run: Run, lpar: str) -> Any:
    st, data = await state.call(
        client, "hmc_get_lpar", lpar_name_or_uuid=lpar, system_name_or_uuid=run.system
    )
    return data if st == "PASS" and isinstance(data, Mapping) else None


def _reads_back(data: object, requested: Mapping[str, Any]) -> bool:
    """Each configured memory and shared-processor value equals the request."""
    for key, path, element in lpar_config._FIELDS:
        got = lpar_config._number(lpar_config._section(data, path).get(element))
        if got is None or not math.isclose(got, float(requested[key]), abs_tol=1e-6):
            return False
    return True


async def _gone(client: Client, state: RunState, run: Run, name: str) -> bool:
    """Whether the listing names no partition *name*, read twice before believing it."""
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(dedicated_pcie._ABSENCE_REREAD_DELAY_S)
        listing = await _partition_lines(client, state, run.system)
        if listing is not None and name not in _names(listing):
            return True
    return False


def _plain(value: object) -> Any:
    """A served typed result as plain data.

    FastMCP hands a tool whose output schema has properties back as a generated
    dataclass, nested ones included, never as a dict.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def _items(data: object, name: str) -> list[Mapping[str, Any]]:
    """The mapping entries of list field *name* of a tool result."""
    value = result_field(data, name)
    if not isinstance(value, list):
        return []
    return [item for item in map(_plain, value) if isinstance(item, Mapping)]


def _all_steps(data: object, status: str) -> bool:
    """Whether the workflow reported steps and every one has *status*."""
    steps = _items(data, "steps")
    return bool(steps) and all(step.get("status") == status for step in steps)


def _job_ok(job: object) -> bool:
    """Whether a waited job ended successfully (PowerOff returns the job itself)."""
    return isinstance(job, Mapping) and (
        job_outcome("", dict(job)).status in SUCCESSFUL_JOB_STATUSES
    )


# ---------------------------------------------------------------------------
# Partition A: create, power, console, composite, delete
# ---------------------------------------------------------------------------


async def _create(
    client: Client, state: RunState, run: Run, name: str, resources: Mapping[str, Any]
) -> tuple[str, Any]:
    st, data = await state.call(
        client,
        "hmc_create_lpar",
        system_name_or_uuid=run.system,
        name=name,
        caller_token=run.token,
        resources=dict(resources),
    )
    return st, data


async def _refused_create(
    client: Client,
    state: RunState,
    run: Run,
    label: str,
    resources: Mapping[str, Any],
    text: str,
) -> bool:
    st, data = await _create(client, state, run, f"{run.base}-x", resources)
    refused = _failed_with(st, data, text)
    state.record(
        SUBTASK,
        f"hmc_create_lpar ({label})",
        "PASS" if refused else "FAIL",
        data,
        "refused before any HMC write" if refused else f"expected {text!r}",
    )
    return refused


async def _adopt(client: Client, state: RunState, run: Run, name: str) -> str | None:
    """The UUID of partition *name*, only when it carries this run's caller token."""
    if await _token_of(client, state, run, name) != run.token:
        return None
    data = await _get_lpar(client, state, run, name)
    found = (data or {}).get("UUID") or (data or {}).get("uuid")
    return found if isinstance(found, str) else None


async def _create_cases(client: Client, state: RunState, run: Run) -> bool:
    op = "lpar.create"
    run.hold(
        op,
        "units-over-vcpus-refused",
        await _refused_create(
            client,
            state,
            run,
            "units over virtual processors",
            _UNITS_OVER_VCPUS,
            "virtual processor uses at most 1.0",
        ),
    )
    run.hold(
        op,
        "memory-over-configurable-refused",
        await _refused_create(
            client,
            state,
            run,
            "memory over the system",
            _MEMORY_OVER_SYSTEM,
            "configurable memory",
        ),
    )
    st, data = await _create(client, state, run, run.a_name, RESOURCES)
    record_status, note = judge_create_result(st, data)
    state.record(SUBTASK, "hmc_create_lpar (partition A)", record_status, data, note)
    run.data[op] = data
    created = result_field(data, "lpar") if st == "PASS" else None
    if isinstance(created, Mapping):
        run.a_uuid = lpar_config._uuid_of(created)
    if run.a_uuid is None:
        run.a_uuid = await _adopt(client, state, run, run.a_name)
    if run.a_uuid is None:
        return False
    read = await _get_lpar(client, state, run, run.a_uuid)
    run.hold(
        op, "resources-read-back", read is not None and _reads_back(read, RESOURCES)
    )
    token = await _token_of(client, state, run, run.a_uuid)
    run.hold(op, "ownership-stamped", token == run.token)
    st, data = await _create(client, state, run, run.a_name, RESOURCES)
    again = await _get_lpar(client, state, run, run.a_name)
    run.hold(
        op,
        "duplicate-name-refused",
        _failed_with(st, data, "already exists")
        and again is not None
        and lpar_config._uuid_of(again) == run.a_uuid,
    )
    duplicate = run.holds(op, "duplicate-name-refused")
    state.record(
        SUBTASK,
        "hmc_create_lpar (duplicate name)",
        "PASS" if duplicate else "FAIL",
        data,
        "refused, partition A unchanged" if duplicate else "expected 'already exists'",
    )
    return record_status == "PASS"


async def _power_on(
    client: Client,
    state: RunState,
    run: Run,
    *,
    boot_mode: str,
    profile_uuid: str | None = None,
    operation_type: str | None = None,
    keylock: str | None = None,
) -> tuple[str, Any]:
    st, data = await state.call(
        client,
        "hmc_power_on_lpar",
        lpar_name_or_uuid=run.a_uuid,
        system_name_or_uuid=run.system,
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
        boot_mode=boot_mode,
        partition_profile_uuid=profile_uuid,
        operation_type=operation_type,
        keylock=keylock,
    )
    return st, data


async def _power_off(
    client: Client, state: RunState, run: Run, lpar: str, *, immediate: bool
) -> tuple[str, Any]:
    st, data = await state.call(
        client,
        "hmc_power_off_lpar",
        lpar_name_or_uuid=lpar,
        system_name_or_uuid=run.system,
        immediate=immediate,
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    return st, data


async def _profile_activation(client: Client, state: RunState, run: Run) -> bool:
    lpar = run.a_uuid or ""
    data = await _get_lpar(client, state, run, lpar)
    profile_uuid = lpar_config._profile_uuid(data) if data else None
    if profile_uuid is None:
        state.record(SUBTASK, "partition A profile", "FAIL", data, "no profile link")
        return False
    st, data = await _power_on(
        client, state, run, boot_mode="sms", profile_uuid=profile_uuid
    )
    state.record(SUBTASK, "hmc_power_on_lpar (profile, SMS)", st, data)
    run.data["lpar.power_on"] = data
    reached = await _wait_for_state(client, state, run, lpar, _FIRMWARE_STATES)
    activated = (
        st == "PASS"
        and _job_ok(result_field(data, "job"))
        and reached in _FIRMWARE_STATES
    )
    run.hold("lpar.power_on", "profile-activation-reached-firmware", activated)
    return reached in _FIRMWARE_STATES


async def _running_cases(client: Client, state: RunState, run: Run) -> None:
    lpar = run.a_uuid or ""
    before = await _lpar_state(client, state, run, lpar)
    st, data = await state.call(
        client,
        "hmc_power_on_lpar",
        lpar_name_or_uuid=lpar,
        system_name_or_uuid=run.system,
        boot_mode="of",
    )
    state.record(SUBTASK, "hmc_power_on_lpar (already running)", st, data)
    after = await _lpar_state(client, state, run, lpar)
    message = result_field(data, "message")
    run.hold(
        "lpar.power_on",
        "running-reported-without-job",
        st == "PASS"
        and result_field(data, "already_running") is True
        and result_field(data, "job") is None
        and isinstance(message, str)
        and "boot mode was not applied" in message
        and before == after,
    )
    st, data = await state.call(
        client,
        "hmc_delete_lpar",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=lpar,
    )
    refused = _failed_with(st, data, "must be 'not activated'")
    state.record(
        SUBTASK,
        "hmc_delete_lpar (activated)",
        "PASS" if refused else "FAIL",
        data,
        "refused while activated" if refused else "expected the activated refusal",
    )
    run.data["lpar.delete"] = data
    run.hold("lpar.delete", "activated-delete-refused", refused)
    kept = await _get_lpar(client, state, run, lpar)
    run.hold("lpar.delete", "partition-kept", kept is not None)


async def _console(client: Client, state: RunState, run: Run) -> None:
    # The idle cap equals the duration: a partition parked at SMS prints nothing more.
    st, data = await state.call(
        client,
        "hmc_capture_lpar_console",
        lpar_name_or_uuid=run.a_uuid,
        system_name_or_uuid=run.system,
        duration_seconds=30.0,
        idle_timeout_seconds=30.0,
    )
    capture = data if st == "PASS" and isinstance(data, Mapping) else {}
    shape = {key: capture.get(key) for key in ("stop_reason", "released")}
    # The console bytes are HMC output the results document keeps unredacted.
    state.record(
        SUBTASK, "hmc_capture_lpar_console", st, data if st != "PASS" else shape
    )
    run.data["lpar.capture_console"] = data if st != "PASS" else shape
    op = "lpar.capture_console"
    run.hold(
        op, "console-captured", st == "PASS" and capture.get("stop_reason") != "error"
    )
    run.hold(op, "console-released", capture.get("released") is True)


async def _power_off_to(
    client: Client, state: RunState, run: Run, *, immediate: bool, assertion: str
) -> bool:
    lpar = run.a_uuid or ""
    st, data = await _power_off(client, state, run, lpar, immediate=immediate)
    label = "immediate" if immediate else "delayed"
    state.record(SUBTASK, f"hmc_power_off_lpar ({label})", st, data)
    run.data["lpar.power_off"] = data
    observed = await _wait_for_state(
        client, state, run, lpar, frozenset({_NOT_ACTIVATED})
    )
    stopped = observed == _NOT_ACTIVATED
    run.hold("lpar.power_off", assertion, st == "PASS" and _job_ok(data) and stopped)
    return stopped


async def _activate(
    client: Client, state: RunState, run: Run, label: str, **activation: Any
) -> tuple[bool, str | None]:
    """One PowerOn; whether its job succeeded, and the state it then settles in."""
    st, data = await _power_on(client, state, run, **activation)
    state.record(SUBTASK, f"hmc_power_on_lpar ({label})", st, data)
    job_ok = st == "PASS" and _job_ok(result_field(data, "job"))
    lpar = run.a_uuid or ""
    if job_ok:
        return True, await _wait_for_state(client, state, run, lpar, _FIRMWARE_STATES)
    return False, await _lpar_state(client, state, run, lpar)


async def _current_configuration(client: Client, state: RunState, run: Run) -> bool:
    """The current-configuration activation; on a refusal, go on by profile.

    `operation_type="activate"` is stated so the row proves the tool accepts it and
    sends no `OperationType`, which V10R3 refused (#1392). A refused activation
    leaves the partition Not Activated, so the profile activation that is already
    proven brings the partition up for the steps that follow.
    """
    job_ok, reached = await _activate(
        client,
        state,
        run,
        "current configuration",
        boot_mode="of",
        operation_type="activate",
        keylock="norm",
    )
    run.hold(
        "lpar.power_on",
        "current-configuration-reached-firmware",
        job_ok and reached in _FIRMWARE_STATES,
    )
    if reached in _FIRMWARE_STATES or reached != _NOT_ACTIVATED:
        return reached in _FIRMWARE_STATES
    data = await _get_lpar(client, state, run, run.a_uuid or "")
    profile_uuid = lpar_config._profile_uuid(data) if data else None
    if profile_uuid is None:
        return False
    _, reached = await _activate(
        client,
        state,
        run,
        "profile, to continue",
        boot_mode="sms",
        profile_uuid=profile_uuid,
    )
    return reached in _FIRMWARE_STATES


async def _operation(
    client: Client,
    state: RunState,
    run: Run,
    request_id: str,
    action: str,
    mode: str | None = None,
) -> Mapping[str, Any] | None:
    """Start or repeat one composite operation, then poll it to terminal or paused."""
    run.requests.setdefault(request_id, False)
    st, data = await state.call(
        client,
        "hmc_power_lpar",
        request_id=request_id,
        lpar_name_or_uuid=run.a_uuid,
        system_name_or_uuid=run.system,
        action=action,
        mode=mode,
        wait_seconds=600,
    )
    state.record(SUBTASK, f"hmc_power_lpar ({request_id})", st, data)
    record = _plain(data) if st == "PASS" else None
    record = record if isinstance(record, Mapping) else None
    for _ in range(_OPERATION_POLLS):
        if record is None or record.get("state") in {"terminal", "paused"}:
            break
        await asyncio.sleep(_OPERATION_POLL_DELAY_S)
        st, page = await state.call(
            client, "hmc_operation_status", request_id=request_id
        )
        found = [
            item
            for item in _items(page, "operations")
            if item.get("request_id") == request_id
        ]
        record = found[0] if st == "PASS" and found else record
    if record is not None:
        run.requests[request_id] = record.get("state") == "terminal"
        state.record(
            SUBTASK,
            f"hmc_operation_status ({request_id})",
            "PASS",
            {
                key: record.get(key)
                for key in ("state", "outcome", "result", "warnings")
            },
        )
    return record


def _completed(record: Mapping[str, Any] | None, wanted: frozenset[str]) -> bool:
    if record is None or record.get("state") != "terminal":
        return False
    result = record.get("result") or {}
    return (
        record.get("outcome") == "completed" and result.get("observed_state") in wanted
    )


async def _composite_cases(client: Client, state: RunState, run: Run) -> None:
    op = "lpar.power"
    stopped = frozenset({_NOT_ACTIVATED})
    start = await _operation(client, state, run, f"{run.token}-1", action="start")
    run.data[op] = start
    run.hold(op, "start-completed-activated", _completed(start, _ACTIVATED_STATES))
    restart = await _operation(
        client, state, run, f"{run.token}-2", action="restart", mode="immediate"
    )
    run.hold(
        op,
        "restart-immediate-completed-activated",
        _completed(restart, _ACTIVATED_STATES),
    )
    stop = await _operation(
        client, state, run, f"{run.token}-3", action="stop", mode="immediate"
    )
    run.hold(op, "stop-immediate-completed-not-activated", _completed(stop, stopped))
    repeat = await _operation(
        client, state, run, f"{run.token}-4", action="stop", mode="immediate"
    )
    result = (repeat or {}).get("result") or {}
    run.hold(
        op,
        "repeat-stop-already-in-state",
        _completed(repeat, stopped)
        and result.get("already_in_state") is True
        and result.get("job_id") is None,
    )
    replay = await _operation(
        client, state, run, f"{run.token}-3", action="stop", mode="immediate"
    )
    run.hold(
        op,
        "same-request-replays",
        stop is not None
        and replay is not None
        and replay.get("operation_id") == stop.get("operation_id"),
    )


async def _delete_a(client: Client, state: RunState, run: Run) -> None:
    st, data = await state.call(
        client,
        "hmc_delete_lpar",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.a_uuid,
    )
    state.record(SUBTASK, "hmc_delete_lpar (partition A)", st, data)
    run.hold("lpar.delete", "delete-call-succeeded", st == "PASS")
    run.hold(
        "lpar.delete", "lpar-name-absent", await _gone(client, state, run, run.a_name)
    )


async def _partition_a(client: Client, state: RunState, run: Run) -> None:
    """Spec steps 4-9, in the spec's call order; any unsettled state goes to teardown."""
    if not await _create_cases(client, state, run):
        return
    if not await _profile_activation(client, state, run):
        return
    await _running_cases(client, state, run)
    await _console(client, state, run)
    if not await _power_off_to(
        client, state, run, immediate=False, assertion="delayed-shutdown-not-activated"
    ):
        return
    if not await _current_configuration(client, state, run):
        return
    if not await _power_off_to(
        client, state, run, immediate=True, assertion="immediate-shutdown-not-activated"
    ):
        return
    await _composite_cases(client, state, run)
    if await _lpar_state(client, state, run, run.a_uuid or "") == _NOT_ACTIVATED:
        await _delete_a(client, state, run)


# ---------------------------------------------------------------------------
# Partition P: provision, decommission
# ---------------------------------------------------------------------------


async def _find_vios(client: Client, state: RunState, run: Run) -> str | None:
    """Select the one VIOS holding the configured group; return why not, or None."""
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
                found.append(Vios(vios_uuid, int(partition_id), group, item["uuid"]))
    if len(found) != 1:
        return (
            f"{len(found)} VIOS list {group!r} with 1 GiB free; exactly one is needed"
        )
    run.vios = found[0]
    return None


async def _vlan_refusal(client: Client, state: RunState, run: Run) -> str | None:
    run.vlan = state.config.provision_vlan_id
    st, data = await state.call(
        client, "hmc_list_virtual_networks", system_name_or_uuid=run.system
    )
    vlans, _ = listed_vlans(data) if st == "PASS" else (set(), [])
    return None if run.vlan in vlans else f"no virtual network on VLAN {run.vlan}"


async def _dedicated_slot_refused(client: Client, state: RunState, run: Run) -> bool:
    """Provision refuses a dedicated slot before any HMC read (#1390).

    A dry run, so a regressed refusal still writes nothing; the slot is never read,
    so the probe needs no dedicated-PCIe configuration.
    """
    vios = run.vios
    assert vios is not None
    st, data = await state.call(
        client,
        "hmc_provision_lpar",
        system_name_or_uuid=run.system,
        name=run.p_name,
        adapters={"port_vlan_id": run.vlan},
        storage={"vios_uuid": vios.uuid, "storage_name": run.volume},
        resources=RESOURCES,
        dry_run=True,
        assignments={
            "dedicated": [{"profile_name": DEFAULT_PROFILE_NAME, "drc_index": "1"}]
        },
    )
    refused = _failed_with(st, data, "does not assign dedicated PCIe slots")
    state.record(
        SUBTASK,
        "hmc_provision_lpar (dedicated PCIe argument)",
        "PASS" if refused else "FAIL",
        data,
        "refused before any HMC read" if refused else "expected the dedicated refusal",
    )
    return refused


async def _create_volume(client: Client, state: RunState, run: Run) -> bool:
    vios = run.vios
    assert vios is not None
    run.volume_attempted = True
    st, data = await state.call(
        client,
        "hmc_create_virtual_disk",
        vios_name_or_uuid=vios.uuid,
        vg_uuid=vios.group_uuid,
        disk_name=run.volume,
        capacity_mib=VOLUME_MIB,
        system_name_or_uuid=run.system,
    )
    state.record(SUBTASK, "hmc_create_virtual_disk (run volume)", st, data)
    return run.volume in (await _volumes(client, state, run) or ())


async def _provision(client: Client, state: RunState, run: Run) -> bool:
    vios = run.vios
    assert vios is not None
    refused = await _dedicated_slot_refused(client, state, run)
    if not await _create_volume(client, state, run):
        return False
    st, data = await state.call(
        client,
        "hmc_provision_lpar",
        system_name_or_uuid=run.system,
        name=run.p_name,
        caller_token=run.token,
        adapters={"port_vlan_id": run.vlan},
        storage={
            "vios_uuid": vios.uuid,
            "storage_name": run.volume,
            "kind": "VirtualDisk",
            "vg_uuid": vios.group_uuid,
        },
        resources=RESOURCES,
        power_on=True,
    )
    record_status, note = judge_create_result(st, data)
    state.record(SUBTASK, "hmc_provision_lpar", record_status, data, note)
    run.data["provision.lpar"] = data
    found = result_field(data, "lpar_uuid") if st == "PASS" else None
    run.p_uuid = found if isinstance(found, str) else None
    if run.p_uuid is None:
        run.p_uuid = await _adopt(client, state, run, run.p_name)
    run.hold("provision.lpar", "dedicated-slot-refused", refused)
    if run.p_uuid is None:
        run.ran.add("provision.lpar")
        return False
    await _provision_checks(client, state, run, st, data)
    return True


async def _provision_checks(
    client: Client, state: RunState, run: Run, st: str, data: Any
) -> None:
    op = "provision.lpar"
    run.hold(
        op,
        "workflow-completed",
        st == "PASS"
        and result_field(data, "workflow_completed") is True
        # A REST create reports its profile apply as skipped, not ok (#1164).
        and not any(step.get("status") == "error" for step in _items(data, "steps")),
    )
    token = await _token_of(client, state, run, run.p_uuid or "")
    run.hold(op, "ownership-stamped", token == run.token)
    st_adapters, adapters = await state.call(
        client,
        "hmc_list_adapters",
        lpar_name_or_uuid=run.p_uuid,
        adapter_type="ClientNetworkAdapter",
    )
    vlans = {str(resource(item).get("PortVLANID")) for item in entries(adapters)}
    run.hold(
        op, "network-adapter-on-vlan", st_adapters == "PASS" and str(run.vlan) in vlans
    )
    run.hold(
        op, "storage-mapping-listed", _mapped(await _mappings(client, state, run), run)
    )
    reached = await _wait_for_state(
        client, state, run, run.p_uuid or "", _FIRMWARE_STATES
    )
    run.hold(op, "partition-activated", reached in _FIRMWARE_STATES)


def _mapped(mappings: frozenset[tuple[str, ...]] | None, run: Run) -> bool:
    return any(
        backing == run.volume and client_uuid.lower() == (run.p_uuid or "").lower()
        for _, client_uuid, _, backing in mappings or ()
    )


def _run_mapping_ids(
    mappings: frozenset[tuple[str, ...]] | None, run: Run
) -> list[str]:
    return sorted(
        mapping_id
        for mapping_id, _, _, backing in mappings or ()
        if backing == run.volume and mapping_id
    )


async def _detach(client: Client, state: RunState, run: Run) -> bool:
    """Detach every mapping backed by the run volume; whether none is left."""
    assert run.vios is not None
    for mapping_id in _run_mapping_ids(await _mappings(client, state, run), run):
        st, data = await state.call(
            client,
            "hmc_detach_storage_mapping",
            vios_name_or_uuid=run.vios.uuid,
            mapping_id=mapping_id,
            system_name_or_uuid=run.system,
        )
        state.record(SUBTASK, f"hmc_detach_storage_mapping ({mapping_id})", st, data)
        if st != "PASS":
            await _record_vios_rmc(client, state, run)
    left = await _mappings(client, state, run)
    # Decided on the backing, not the id: a mapping the HMC reports without one
    # cannot be detached through the tool and still holds the volume.
    return left is not None and not any(backing == run.volume for *_, backing in left)


async def _record_vios_rmc(client: Client, state: RunState, run: Run) -> None:
    """The VIOS RMC state beside a refused detach: HSCL2957 blames RMC (#1346)."""
    listing = await lpar_config._cli(
        client,
        state,
        f"lssyscfg -r lpar -m {shlex.quote(run.system)} -F name,lpar_env,rmc_state",
    )
    rows = [row for row in (listing or "").splitlines() if ",vioserver," in row]
    state.record(
        SUBTASK,
        "VIOS rmc_state (after a refused detach)",
        "PASS" if listing is not None else "FAIL",
        "\n".join(rows) if listing is not None else "the partition listing failed",
    )


def _dry_run_mapping_ids(data: object) -> frozenset[tuple[str, str]] | None:
    steps = result_field(data, "steps")
    if not isinstance(steps, (list, tuple)):
        return None
    mapping_steps = [
        step
        for step in steps
        if result_field(step, "step") == "detach_storage_mappings"
    ]
    if len(mapping_steps) != 1:
        return None
    result = _plain(result_field(mapping_steps[0], "result"))
    if not isinstance(result, Mapping):
        return None
    mappings = result.get("mappings")
    if not isinstance(mappings, (list, tuple)) or not mappings:
        return None
    identities = []
    for item in mappings:
        if not isinstance(item, Mapping):
            return None
        vios, identity = item.get("vios_uuid"), item.get("mapping_id")
        if not isinstance(vios, str) or not vios:
            return None
        if not isinstance(identity, str) or not identity:
            return None
        identities.append((vios.casefold(), identity))
    return frozenset(identities) if len(set(identities)) == len(identities) else None


def _dry_run_inventoried(data: object, run: Run, expected_adapters: object) -> bool:
    radius = _plain(result_field(data, "blast_radius"))
    radius = radius if isinstance(radius, Mapping) else {}
    listed = radius.get("adapters")
    if not isinstance(listed, (list, tuple)) or expected_adapters is None:
        return False
    adapters = []
    for item in listed:
        if not isinstance(item, Mapping):
            return False
        kind, identity = item.get("type"), item.get("uuid")
        if not isinstance(kind, str) or not isinstance(identity, str) or not identity:
            return False
        adapters.append((kind, identity.casefold()))
    if (
        len(set(adapters)) != len(adapters)
        or tuple(sorted(adapters)) != expected_adapters
    ):
        return False
    backed = {
        item.get("backing_device") for item in radius.get("storage_mappings") or ()
    }
    return (
        result_field(data, "resource_deleted") is False
        and _all_steps(data, "dry_run")
        and {"ClientNetworkAdapter", "VirtualSCSIClientAdapter"}
        <= {kind for kind, _ in adapters}
        and run.volume in backed
    )


async def _decommission(
    client: Client, state: RunState, run: Run, *, dry_run: bool
) -> tuple[str, Any]:
    st, data = await state.call(
        client,
        "hmc_decommission_lpar",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.p_uuid,
        dry_run=dry_run,
        immediate=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    return st, data


async def _client_adapters(client: Client, state: RunState, run: Run) -> Any:
    adapters = []
    for kind in (
        "ClientNetworkAdapter",
        "VirtualSCSIClientAdapter",
        "VirtualFibreChannelClientAdapter",
        "VirtualNICDedicated",
    ):
        st, data = await state.call(
            client, "hmc_list_adapters", lpar_name_or_uuid=run.p_uuid, adapter_type=kind
        )
        if st != "PASS":
            return None
        for item in entries(data):
            identity = item.get("UUID") or item.get("uuid")
            if not isinstance(identity, str) or not identity:
                return None
            adapters.append((kind, identity.casefold()))
    return tuple(sorted(adapters)) if len(set(adapters)) == len(adapters) else None


async def _decommission_cases(client: Client, state: RunState, run: Run) -> None:
    run.decommission_failed = True
    op = "lpar.decommission"
    lpar = run.p_uuid or ""
    before_proof = await _complete_mapping_proof(client, state, run)
    before_volumes = await _volumes(client, state, run)
    before = (
        await _lpar_state(client, state, run, lpar),
        await _client_adapters(client, state, run),
        before_proof,
        before_volumes,
    )
    st, data = await _decommission(client, state, run, dry_run=True)
    state.record(SUBTASK, "hmc_decommission_lpar (dry run)", st, data)
    run.data[op] = data
    expected = (
        frozenset(
            (run.vios.uuid.casefold(), identity)
            for identity, client_uuid, _, _ in before_proof[0]
            if client_uuid.casefold() == lpar.casefold()
        )
        if before_proof is not None and run.vios is not None
        else None
    )
    inventoried = (
        st == "PASS"
        and _dry_run_inventoried(data, run, before[1])
        and expected is not None
        and bool(expected)
        and _dry_run_mapping_ids(data) == expected
    )
    run.hold(op, "dry-run-inventoried", inventoried)
    after = (
        await _lpar_state(client, state, run, lpar),
        await _client_adapters(client, state, run),
        await _complete_mapping_proof(client, state, run),
        await _volumes(client, state, run),
    )
    unchanged = None not in before and before == after
    run.hold(op, "dry-run-changed-nothing", unchanged)
    if not (inventoried and unchanged):
        return
    st, data = await _decommission(client, state, run, dry_run=False)
    state.record(SUBTASK, "hmc_decommission_lpar", st, data)
    run.data[op] = data
    deleted = st == "PASS" and result_field(data, "resource_deleted") is True
    completed = st == "PASS" and result_field(data, "workflow_completed") is True
    run.hold(op, "resource-deleted", deleted)
    run.hold(op, "workflow-completed", completed)
    if not (deleted and completed):
        return
    partition_absent = await _gone(client, state, run, run.p_name)
    run.hold(op, "lpar-name-absent", partition_absent)
    proof = await _complete_mapping_proof(client, state, run)
    volumes = await _volumes(client, state, run)
    left = proof[0] if proof is not None else None
    mapping_absent = left is not None and not any(
        client_uuid.casefold() == lpar.casefold() or backing == run.volume
        for _, client_uuid, _, backing in left
    )
    volume_retained = volumes is not None and run.volume in volumes
    foreign_unchanged = (
        before_proof is not None
        and proof is not None
        and before_proof[1] == proof[1]
        and before_volumes is not None
        and volumes == before_volumes
    )
    run.hold(op, "storage-mapping-absent", mapping_absent and foreign_unchanged)
    run.hold(op, "backing-volume-retained", volume_retained)
    run.decommission_failed = not (
        deleted
        and completed
        and partition_absent
        and mapping_absent
        and volume_retained
        and foreign_unchanged
    )


async def _partition_p(
    client: Client, state: RunState, run: Run, why: str | None
) -> None:
    """Spec steps 10-11, after their preconditions."""
    why = why or await _vlan_refusal(client, state, run)
    if why:
        state.skip(SUBTASK, "hmc_provision_lpar / hmc_decommission_lpar", why)
        return
    if await _provision(client, state, run):
        await _decommission_cases(client, state, run)


# ---------------------------------------------------------------------------
# Teardown, compare, observations
# ---------------------------------------------------------------------------


async def _abandon(client: Client, state: RunState, run: Run) -> None:
    for request_id, terminal in run.requests.items():
        if terminal:
            continue
        st, data = await state.call(
            client, "hmc_power_lpar", request_id=request_id, continuation="abandon"
        )
        state.record(SUBTASK, f"hmc_power_lpar ({request_id} abandon)", st, data)


async def _remove(client: Client, state: RunState, run: Run, name: str) -> str | None:
    """Power off and delete run partition *name*; why it is not gone, or None."""
    lpar = await _adopt(client, state, run, name)
    if lpar is None:
        return "it does not carry this run's caller token"
    if await _lpar_state(client, state, run, lpar) != _NOT_ACTIVATED:
        st, data = await _power_off(client, state, run, lpar, immediate=True)
        state.record(SUBTASK, f"hmc_power_off_lpar (teardown {name})", st, data)
        if (
            await _wait_for_state(client, state, run, lpar, frozenset({_NOT_ACTIVATED}))
            != _NOT_ACTIVATED
        ):
            return "it did not reach Not Activated"
    st, data = await state.call(
        client,
        "hmc_delete_lpar",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=lpar,
    )
    state.record(SUBTASK, f"hmc_delete_lpar (teardown {name})", st, data)
    return None if await _gone(client, state, run, name) else "the delete did not take"


def _manual(state: RunState, label: str, text: str) -> None:
    state.record(SUBTASK, label, "FAIL", f"MANUAL RECOVERY REQUIRED: {text}")


async def _volume_present(client: Client, state: RunState, run: Run) -> bool:
    """Whether the run volume is listed; an unreadable listing counts as present."""
    volumes = await _volumes(client, state, run)
    return volumes is None or run.volume in volumes


async def _teardown_storage(client: Client, state: RunState, run: Run) -> bool:
    vios = run.vios
    if vios is None or not run.volume_attempted:
        return True
    system = shlex.quote(run.system)
    if not await _detach(client, state, run):
        _manual(
            state,
            "run volume mapping teardown",
            f"a mapping backed by {run.volume!r} is still on VIOS id {vios.partition_id}: "
            f"`viosvrcmd -m {system} --id {vios.partition_id} -c 'lsmap -all'`, then "
            f"`rmvdev -vtd <device>` for it, before removing the volume.",
        )
        return False
    if await _volume_present(client, state, run):
        st, data = await state.call(
            client,
            "hmc_delete_virtual_disk",
            vios_name_or_uuid=vios.uuid,
            vg_uuid=vios.group_uuid,
            disk_name=run.volume,
            system_name_or_uuid=run.system,
        )
        state.record(SUBTASK, "hmc_delete_virtual_disk (run volume)", st, data)
    if await _volume_present(client, state, run):
        _manual(
            state,
            "run volume teardown",
            f"volume {run.volume!r} is still in {vios.group!r}: "
            f"`viosvrcmd -m {system} --id {vios.partition_id} -c 'rmlv -f {run.volume}'`.",
        )
        return False
    return True


async def _teardown(client: Client, state: RunState, run: Run) -> None:
    """Spec step 12: abandon, detach, delete partitions, delete the volume."""
    await _abandon(client, state, run)
    if run.decommission_failed:
        _manual(
            state,
            "decommission recovery",
            (
                "decommission did not prove safe completion; retain the run partition, "
                "mapping and backing volume. Inspect the recorded phase errors and current "
                "VIOS mapping/client identities before any operator-directed recovery."
            ),
        )
        run.clean = False
        return
    detached = (
        run.vios is None
        or not run.volume_attempted
        or await _detach(client, state, run)
    )
    listing = await _partition_lines(client, state, run.system)
    names = (
        [n for n in _names(listing) if n.startswith(run.base)]
        if listing is not None
        else [run.a_name, run.p_name]
    )
    removed = listing is not None and detached
    system = shlex.quote(run.system)
    for name in sorted(names):
        if name == run.p_name and not detached:
            # Deleting P would leave a mapping the detach tool can no longer reach.
            removed = False
            _manual(
                state,
                "run partition teardown",
                f"partition {name!r} was kept: a mapping backed by volume "
                f"{run.volume!r} could not be detached. Detach it with "
                "hmc_detach_storage_mapping, then delete the partition and the volume.",
            )
            continue
        why = await _remove(client, state, run, name)
        if why:
            removed = False
            _manual(
                state,
                "run partition teardown",
                f"partition {name!r} was NOT deleted: {why}. Once its description "
                f"shows caller token {run.token!r}: `chsysstate -m {system} -r lpar "
                f"-n {shlex.quote(name)} -o shutdown --immed` (when not Not Activated), "
                f"then `rmsyscfg -r lpar -m {system} -n {shlex.quote(name)}`.",
            )
    run.clean = await _teardown_storage(client, state, run) and removed


async def _compare(
    client: Client, state: RunState, run: Run, baseline: Baseline
) -> bool:
    reads = []
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(_COMPARE_REREAD_DELAY_S)
        reads.append(await _read_baseline(client, state, run))
        if reads[-1] == baseline:
            break
    same = reads[-1] == baseline
    _report_left_adapters(state, run, baseline, reads[-1])
    state.record(
        SUBTASK,
        "system baseline compare",
        "PASS" if same else "FAIL",
        {
            "baseline": _baseline_data(baseline),
            "reads": [_baseline_data(r) for r in reads],
        },
    )
    return same


def _report_left_adapters(
    state: RunState, run: Run, baseline: Baseline, final: Baseline | None
) -> None:
    """A manual-recovery row per VIOS server adapter the run added and left."""
    if run.vios is None or final is None or final.adapters is None:
        return
    system = shlex.quote(run.system)
    for row in sorted(final.adapters - (baseline.adapters or frozenset())):
        slot = row.split(",", 1)[0]
        _manual(
            state,
            "VIOS server adapter teardown",
            f"VIOS id {run.vios.partition_id} has server adapter slot {slot} ({row}) "
            f"it did not have before the run: `chhwres -r virtualio --rsubtype scsi "
            f"-m {system} -o r --id {run.vios.partition_id} -s {slot}`.",
        )


def _provision_assertions(run: Run) -> list[Assertion]:
    """`provision.lpar`'s assertions."""
    return [
        Assertion(
            "workflow-completed", run.holds("provision.lpar", "workflow-completed")
        ),
        Assertion(
            "ownership-stamped", run.holds("provision.lpar", "ownership-stamped")
        ),
        Assertion(
            "network-adapter-on-vlan",
            run.holds("provision.lpar", "network-adapter-on-vlan"),
        ),
        Assertion(
            "storage-mapping-listed",
            run.holds("provision.lpar", "storage-mapping-listed"),
        ),
        Assertion(
            "partition-activated",
            run.holds("provision.lpar", "partition-activated"),
        ),
        Assertion(
            "dedicated-slot-refused",
            run.holds("provision.lpar", "dedicated-slot-refused"),
        ),
    ]


def _observe_a(state: RunState, run: Run, cleanup: str) -> None:
    """Partition A's observations, at literal sites the assertion-id guards read."""
    if "lpar.create" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_create_lpar",
            operation="lpar.create",
            scenario=SCENARIO,
            assertions=[
                Assertion(
                    "units-over-vcpus-refused",
                    run.holds("lpar.create", "units-over-vcpus-refused"),
                ),
                Assertion(
                    "memory-over-configurable-refused",
                    run.holds("lpar.create", "memory-over-configurable-refused"),
                ),
                Assertion(
                    "resources-read-back",
                    run.holds("lpar.create", "resources-read-back"),
                ),
                Assertion(
                    "ownership-stamped", run.holds("lpar.create", "ownership-stamped")
                ),
                Assertion(
                    "duplicate-name-refused",
                    run.holds("lpar.create", "duplicate-name-refused"),
                ),
            ],
            cleanup=cleanup,
            data=run.data.get("lpar.create"),
        )
    if "lpar.power_on" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_power_on_lpar",
            operation="lpar.power_on",
            scenario=SCENARIO,
            assertions=[
                Assertion(
                    "profile-activation-reached-firmware",
                    run.holds("lpar.power_on", "profile-activation-reached-firmware"),
                ),
                Assertion(
                    "running-reported-without-job",
                    run.holds("lpar.power_on", "running-reported-without-job"),
                ),
                Assertion(
                    "current-configuration-reached-firmware",
                    run.holds(
                        "lpar.power_on", "current-configuration-reached-firmware"
                    ),
                ),
            ],
            cleanup=cleanup,
            data=run.data.get("lpar.power_on"),
        )
    if "lpar.capture_console" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_capture_lpar_console",
            operation="lpar.capture_console",
            scenario=SCENARIO,
            assertions=[
                Assertion(
                    "console-captured",
                    run.holds("lpar.capture_console", "console-captured"),
                ),
                Assertion(
                    "console-released",
                    run.holds("lpar.capture_console", "console-released"),
                ),
            ],
            cleanup=cleanup,
            data=run.data.get("lpar.capture_console"),
        )
    if "lpar.power_off" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_power_off_lpar",
            operation="lpar.power_off",
            scenario=SCENARIO,
            assertions=[
                Assertion(
                    "delayed-shutdown-not-activated",
                    run.holds("lpar.power_off", "delayed-shutdown-not-activated"),
                ),
                Assertion(
                    "immediate-shutdown-not-activated",
                    run.holds("lpar.power_off", "immediate-shutdown-not-activated"),
                ),
            ],
            cleanup=cleanup,
            data=run.data.get("lpar.power_off"),
        )


def _observe_lifecycle(state: RunState, run: Run, cleanup: str) -> None:
    """The composite, delete, provision and decommission observations."""
    if "lpar.power" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_power_lpar",
            operation="lpar.power",
            scenario=SCENARIO,
            assertions=[
                Assertion(
                    "start-completed-activated",
                    run.holds("lpar.power", "start-completed-activated"),
                ),
                Assertion(
                    "restart-immediate-completed-activated",
                    run.holds("lpar.power", "restart-immediate-completed-activated"),
                ),
                Assertion(
                    "stop-immediate-completed-not-activated",
                    run.holds("lpar.power", "stop-immediate-completed-not-activated"),
                ),
                Assertion(
                    "repeat-stop-already-in-state",
                    run.holds("lpar.power", "repeat-stop-already-in-state"),
                ),
                Assertion(
                    "same-request-replays",
                    run.holds("lpar.power", "same-request-replays"),
                ),
            ],
            cleanup=cleanup,
            data=run.data.get("lpar.power"),
        )
    if "lpar.delete" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_delete_lpar",
            operation="lpar.delete",
            scenario=SCENARIO,
            assertions=[
                Assertion(
                    "activated-delete-refused",
                    run.holds("lpar.delete", "activated-delete-refused"),
                ),
                Assertion("partition-kept", run.holds("lpar.delete", "partition-kept")),
                Assertion(
                    "delete-call-succeeded",
                    run.holds("lpar.delete", "delete-call-succeeded"),
                ),
                Assertion(
                    "lpar-name-absent", run.holds("lpar.delete", "lpar-name-absent")
                ),
            ],
            cleanup=cleanup,
            data=run.data.get("lpar.delete"),
        )


def _observe_p(state: RunState, run: Run, cleanup: str) -> None:
    """Partition P's observations."""
    if "provision.lpar" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_provision_lpar",
            operation="provision.lpar",
            scenario=SCENARIO,
            assertions=_provision_assertions(run),
            cleanup=cleanup,
            data=run.data.get("provision.lpar"),
        )
    if "lpar.decommission" in run.ran:
        state.record_verified(
            SUBTASK,
            "hmc_decommission_lpar",
            operation="lpar.decommission",
            scenario=SCENARIO,
            assertions=[
                Assertion(
                    "storage-mapping-absent",
                    run.holds("lpar.decommission", "storage-mapping-absent"),
                ),
                Assertion(
                    "backing-volume-retained",
                    run.holds("lpar.decommission", "backing-volume-retained"),
                ),
                Assertion(
                    "dry-run-inventoried",
                    run.holds("lpar.decommission", "dry-run-inventoried"),
                ),
                Assertion(
                    "dry-run-changed-nothing",
                    run.holds("lpar.decommission", "dry-run-changed-nothing"),
                ),
                Assertion(
                    "resource-deleted",
                    run.holds("lpar.decommission", "resource-deleted"),
                ),
                Assertion(
                    "workflow-completed",
                    run.holds("lpar.decommission", "workflow-completed"),
                ),
                Assertion(
                    "lpar-name-absent",
                    run.holds("lpar.decommission", "lpar-name-absent"),
                ),
            ],
            cleanup=cleanup,
            data=run.data.get("lpar.decommission"),
        )


async def _preconditions(client: Client, state: RunState, run: Run) -> Any:
    """The skip reason for the arm, or (baseline, VIOS refusal) to run with."""
    if state.group != GROUP:
        return (
            f"creates, powers and deletes partitions: runs only in the {GROUP!r} group"
        )
    if not _power_operations_authorized():
        return (
            "HMC_AUTHORIZE_POWER_OPERATIONS is off: the power evidence must cover the "
            "ADR 0092 ownership-guarded path"
        )
    why = await _find_vios(client, state, run)
    baseline = await _read_baseline(client, state, run)
    if baseline is None:
        return "the system baseline did not read"
    stranded = scratch_partitions(_names(baseline.partitions))
    stranded += scratch_volumes(baseline.volumes or ())
    if stranded:
        return f"{stranded} already exist: run live_test_recovery.py first"
    return baseline, why


async def exercise_lpar_power(client: Client, state: RunState) -> None:
    print("\n=== ST41: LPAR power and lifecycle (issue #1346) ===")
    run = Run(state.config.system_name, uuid.uuid4().hex[:8])
    ready = await _preconditions(client, state, run)
    if isinstance(ready, str):
        state.skip(SUBTASK, "lpar-power arm", ready)
        return
    baseline, vios_refusal = ready
    state.record(SUBTASK, "system baseline", "PASS", _baseline_data(baseline))
    try:
        await _partition_a(client, state, run)
        await _partition_p(client, state, run, vios_refusal)
    finally:
        await _teardown(client, state, run)
    same = await _compare(client, state, run, baseline)
    for tool, reason in GAPS:
        state.skip(SUBTASK, tool, reason)
    cleanup = "passed" if run.clean and same else "failed"
    _observe_a(state, run, cleanup)
    _observe_lifecycle(state, run, cleanup)
    _observe_p(state, run, cleanup)
