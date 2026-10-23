"""LPAR configuration, DLPAR and boot-order scenarios: the lpar-config arm (#1345).

Subtask 39 creates one run-unique scratch partition, changes its configuration
through the six operations under test, activates it to SMS for the activated
DLPAR case, and deletes it. It touches no other partition: every mutating call
names the scratch partition's UUID.
"""

from __future__ import annotations

import asyncio
import math
import re
import shlex
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar import LparReportingSettings, LparResources, LparSettings
from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.core import LparCreation, create_and_stamp_lpar
from hmcpctl.operations.lpar.ownership import parse_lpar_ownership_caller_token
from hmcpctl.operations.virtualization.pcie import (
    _is_exact_admitted_environment,
    _release_fields,
    require_drc_index,
)
from hmcpctl.ssh.profiles import (
    ProfileIoSlot,
    parse_profile_io_slot_rows,
    parse_profile_io_slots,
    profile_io_slot_rows_command,
)
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import leaf_text

from . import dedicated_pcie
from .dedicated_pcie import _ABSENCE_REREAD_DELAY_S, partition_not_found
from .observation import Assertion, CallFailure, judge_create_result, plain_data
from .results import field as result_field
from .results import resource

if TYPE_CHECKING:
    from live_test.runner import RunState

SUBTASK = 39
GROUP = "lpar-config"
REPORTING_SUBTASK = 48
REPORTING_GROUP = "lpar-reporting"
REPORTING_SCENARIO = "st48-lpar-reporting"
SETTINGS_SUBTASK = 47
SETTINGS_GROUP = "lpar-settings"
SETTINGS_SCENARIO = "st47-lpar-settings"
#: Reserved for this arm: recovery reports any partition carrying it as stranded.
NAME_PREFIX = "hmcpctl-live-lpar-"
TOKEN_PREFIX = "lparcfg-"
SCENARIO = "st39-lpar-config"

#: Shared and uncapped, sized like the bare-cec fixture so it reaches firmware.
RESOURCES: dict[str, Any] = {
    "min_memory": 1024,
    "desired_memory": 2048,
    "max_memory": 4096,
    "dedicated": False,
    "min_procs": 0.1,
    "desired_procs": 0.5,
    "max_procs": 1.0,
    "min_vcpus": 1,
    "desired_vcpus": 1,
    "max_vcpus": 2,
    "uncapped": True,
}
#: Two Open Firmware paths in the `BootDeviceList` shape (#980 design).
BOOT_PATHS = [
    "/vdevice/v-scsi@30000002/disk@8100000000000000",
    "/vdevice/l-lan@30000003",
]
_CLEAR_REFUSAL = "Refusing to clear the pending boot order"
_ACTIVATED_GAP_NOTE = (
    "the HMC refused it while the partition was at firmware: the gap's prerequisite "
    "is a running operating system with an active RMC connection"
)
#: An HMC refusal names its code; anything else (a dispatch defect, a lost
#: session) is not the activated-DLPAR gap and must stay a failure.
_HMC_REFUSAL = re.compile(r"\b(?:HSCL|REST)[0-9A-F]{4}\b")

_MEMORY = ("PartitionMemoryConfiguration",)
_SHARED = ("PartitionProcessorConfiguration", "SharedProcessorConfiguration")
_FIELDS = (
    ("min_memory", _MEMORY, "MinimumMemory"),
    ("desired_memory", _MEMORY, "DesiredMemory"),
    ("max_memory", _MEMORY, "MaximumMemory"),
    ("desired_procs", _SHARED, "DesiredProcessingUnits"),
    ("max_procs", _SHARED, "MaximumProcessingUnits"),
    ("desired_vcpus", _SHARED, "DesiredVirtualProcessors"),
    ("max_vcpus", _SHARED, "MaximumVirtualProcessors"),
)

_JOB_TIMEOUT_S = 900
_JOB_POLL_INTERVAL_S = 10
_STATE_POLL_ATTEMPTS = 30
_STATE_POLL_DELAY_S = 10.0
_COMPARE_REREAD_DELAY_S = 30.0
_NOT_ACTIVATED = "not activated"
_FIRMWARE_STATES = frozenset({"open firmware", "running"})
_UUID_AT_END = re.compile(
    r"/([0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12})/?\Z"
)


@dataclass(frozen=True)
class _Pools:
    """The system-wide reads the run must leave as it found them."""

    names: frozenset[str]
    procs: str
    memory: str


_ProfileMap = dict[tuple[str, str], tuple[ProfileIoSlot, ...]]


def _profile_evidence(profiles: _ProfileMap | None) -> list[dict[str, Any]] | None:
    if profiles is None:
        return None
    return [
        {
            "lpar_name": lpar,
            "profile_name": profile,
            "io_slots": [asdict(slot) for slot in slots],
        }
        for (lpar, profile), slots in sorted(profiles.items())
    ]


@dataclass
class _DedicatedProbe:
    profile_name: str
    drc_index: str
    baseline: _ProfileMap
    scratch_baseline: tuple[ProfileIoSlot, ...] = ()
    attempted: bool = False
    workflow_completed: bool = False
    assigned: bool = False
    others_unchanged: bool = False
    restored: bool = False
    restore_call_succeeded: bool = True
    slot_unowned: bool = False
    baseline_restored: bool = False
    data: dict[str, Any] = field(default_factory=dict)


async def _profile_slots(
    client: Client, state: RunState, system: str
) -> _ProfileMap | None:
    text = await _dedicated_cli(client, state, profile_io_slot_rows_command(system))
    if text is None:
        return None
    try:
        result = {}
        for row in parse_profile_io_slot_rows(text):
            key = row["lpar_name"], row["name"]
            if key in result:
                raise HMCCLIError("duplicate profile identity")
            result[key] = tuple(
                sorted(
                    parse_profile_io_slots(row["io_slots"]),
                    key=lambda slot: slot.drc_index,
                )
            )
        return result
    except HMCCLIError as error:
        state.record(
            SUBTASK,
            "dedicated profile read",
            "FAIL",
            CallFailure("HMCCLIError", str(error), "", None, False),
        )
        return None


@dataclass
class _Run:
    """What the arm has established, read by teardown and the deferred observations."""

    system: str
    name: str
    token: str
    region: int
    dedicated: _DedicatedProbe | None = None
    uuid: str | None = None
    created: bool = False
    create_attempted: bool = False
    deleted: bool = False
    held: dict[str, bool] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    subtask: int = SUBTASK

    def hold(self, operation: str, assertion: str, value: bool) -> None:
        self.held[f"{operation}:{assertion}"] = value


def scratch_partitions(names: Iterable[str]) -> list[str]:
    """The partition names only this arm may have created."""
    return sorted(name for name in names if name.startswith(NAME_PREFIX))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _cli(client: Client, state: RunState, cmd: str) -> str | None:
    st, data = await state.call(client, "hmc_run_command", cmd=cmd)
    return data.strip() if st == "PASS" and isinstance(data, str) else None


async def _dedicated_cli(client: Client, state: RunState, cmd: str) -> str | None:
    status, data = await state.call(client, "hmc_run_command", cmd=cmd)
    if status != "PASS":
        state.record(SUBTASK, "dedicated command read", "FAIL", data)
        return None
    if not isinstance(data, str):
        state.record(SUBTASK, "dedicated command response", "FAIL", data)
        return None
    return data.strip()


async def _dedicated_admitted(client: Client, state: RunState, system: str) -> bool:
    version = await _dedicated_cli(client, state, "lshmc -V")
    model = await _dedicated_cli(
        client, state, f"lssyscfg -r sys -m {shlex.quote(system)} -F type_model"
    )
    if version is None or model is None:
        return False
    fields = _release_fields(version)
    if (
        fields is None
        or any(re.fullmatch(r"[0-9]+", value) is None for value in fields.values())
        or re.fullmatch(r"[0-9]{4}-[A-Z0-9]{3}", model) is None
    ):
        state.record(
            SUBTASK,
            "dedicated admission response",
            "FAIL",
            CallFailure(
                "AdmissionResponseError",
                "malformed release fields or type-model token",
                "",
                None,
                False,
            ),
        )
        return False
    if not _is_exact_admitted_environment(version, model):
        state.skip(
            SUBTASK,
            "dedicated admitted environment",
            "well-formed release/model outside the exact admission envelope",
        )
        return False
    state.record(
        SUBTASK,
        "dedicated admitted environment",
        "PASS",
        {"release_fields": fields, "type_model": model},
    )
    return True


async def _select_dedicated(
    client: Client, state: RunState, run: _Run
) -> _DedicatedProbe | None:
    arm = dedicated_pcie._dedicated_config(state.config)
    if arm is None or arm.system_name != run.system:
        state.skip(
            SUBTASK,
            "hmc_modify_lpar dedicated assignments",
            "requires the dedicated configuration for this same managed system",
        )
        return None
    if not await _dedicated_admitted(client, state, run.system):
        return None
    status, data = await state.call(
        client, "hmc_list_dedicated_pcie_slots", system_name_or_uuid=run.system
    )
    if status != "PASS":
        state.record(SUBTASK, "dedicated inventory read", "FAIL", data)
        return None
    if not isinstance(data, Mapping):
        state.record(SUBTASK, "dedicated inventory shape", "FAIL", data)
        return None
    profiles = await _profile_slots(client, state, run.system)
    if profiles is None:
        return None  # dedicated reader/parser explicitly recorded the failure
    items = data.get("items")
    if not isinstance(items, list) or any(
        not isinstance(row, Mapping) for row in items
    ):
        state.record(SUBTASK, "dedicated inventory shape", "FAIL", data)
        return None
    for row in items:
        try:
            if not isinstance(row.get("drc_index"), str):
                raise HMCCLIError("dedicated inventory DRC must be text")
            require_drc_index(row["drc_index"])
            if "owner_lpar" not in row or (
                row["owner_lpar"] is not None and not isinstance(row["owner_lpar"], str)
            ):
                raise HMCCLIError(
                    "dedicated inventory owner_lpar is required and must be text or null"
                )
        except (HMCCLIError, ValueError) as error:
            state.record(
                SUBTASK,
                "dedicated inventory shape",
                "FAIL",
                CallFailure("InventoryResponseError", str(error), "", None, False),
            )
            return None
    eligible = [
        row
        for row in items
        if dedicated_pcie._slot_unowned(row)
        and (arm.drc_index is None or row.get("drc_index") == arm.drc_index)
        and not any(
            slot.drc_index == row.get("drc_index")
            for slots in profiles.values()
            for slot in slots
        )
    ]
    if not eligible:
        state.skip(
            SUBTASK,
            "hmc_modify_lpar dedicated assignments",
            "requires an unowned dedicated slot listed by no profile",
        )
        return None
    state.record(
        SUBTASK,
        "dedicated recovery baseline",
        "PASS",
        {
            "drc_index": eligible[0]["drc_index"],
            "profile_name": arm.profile_name,
            "profiles": _profile_evidence(profiles),
        },
    )
    return _DedicatedProbe(arm.profile_name, eligible[0]["drc_index"], profiles)


async def _dedicated_inventory(client: Client, state: RunState, run: _Run) -> bool:
    status, data = await state.call(
        client, "hmc_list_dedicated_pcie_slots", system_name_or_uuid=run.system
    )
    if status != "PASS":
        state.record(SUBTASK, "dedicated inventory read", "FAIL", data)
        return False
    items = data.get("items") if isinstance(data, Mapping) else None
    if not isinstance(items, list) or any(
        not isinstance(row, Mapping) for row in items
    ):
        state.record(SUBTASK, "dedicated inventory shape", "FAIL", data)
        return False
    for row in items:
        try:
            if not isinstance(row.get("drc_index"), str):
                raise HMCCLIError("dedicated inventory DRC must be text")
            require_drc_index(row["drc_index"])
            if "owner_lpar" not in row or (
                row["owner_lpar"] is not None and not isinstance(row["owner_lpar"], str)
            ):
                raise HMCCLIError(
                    "dedicated inventory owner_lpar is required and must be text or null"
                )
        except (HMCCLIError, ValueError) as error:
            state.record(
                SUBTASK,
                "dedicated inventory shape",
                "FAIL",
                CallFailure("InventoryResponseError", str(error), "", None, False),
            )
            return False
    probe = run.dedicated
    matches = (
        [
            row
            for row in items
            if isinstance(row, Mapping) and row.get("drc_index") == probe.drc_index
        ]
        if isinstance(items, list) and probe
        else []
    )
    if len(matches) != 1 or (
        matches[0].get("owner_lpar") is not None
        and not isinstance(matches[0]["owner_lpar"], str)
    ):
        state.record(SUBTASK, "dedicated inventory shape", "FAIL", data)
        return False
    return dedicated_pcie._slot_unowned(matches[0])


def _other_profiles(profiles: _ProfileMap, run: _Run) -> _ProfileMap:
    assert run.dedicated is not None
    return {
        key: value
        for key, value in profiles.items()
        if key != (run.name, run.dedicated.profile_name)
    }


async def _dedicated_restore(client: Client, state: RunState, run: _Run) -> None:
    probe = run.dedicated
    assert probe is not None
    profiles = await _profile_slots(client, state, run.system)
    target = (run.name, probe.profile_name)
    expected = (ProfileIoSlot(probe.drc_index, None, False),)
    if profiles is None or _other_profiles(profiles, run) != probe.baseline:
        state.record(
            SUBTASK,
            "dedicated restoration preconditions",
            "FAIL",
            {"profiles": _profile_evidence(profiles)},
        )
        return
    actual = profiles.get(target)
    if actual != probe.scratch_baseline:
        if (
            actual != expected
            or await _token_of(client, state, run, run.uuid or "") != run.token
            or await _lpar_state(client, state, run) != _NOT_ACTIVATED
        ):
            return
        status, data = await state.call(
            client,
            "hmc_unassign_dedicated_pcie_slot",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=run.uuid,
            profile_name=probe.profile_name,
            drc_index=probe.drc_index,
        )
        state.record(
            SUBTASK, "hmc_unassign_dedicated_pcie_slot (ST39 restore)", status, data
        )
        probe.restore_call_succeeded = status == "PASS"
        profiles = await _profile_slots(client, state, run.system)
    probe.slot_unowned = await _dedicated_inventory(client, state, run)
    probe.restored = (
        profiles is not None
        and profiles.get(target) == probe.scratch_baseline
        and _other_profiles(profiles, run) == probe.baseline
        and probe.slot_unowned
    )
    state.record(
        SUBTASK,
        "dedicated profile restoration",
        "PASS" if probe.restored else "FAIL",
        {
            "restored": probe.restored,
            "slot_unowned": probe.slot_unowned,
            "profiles": _profile_evidence(profiles) if not probe.restored else None,
        },
    )


async def _dedicated_case(client: Client, state: RunState, run: _Run) -> bool:
    probe = run.dedicated
    if probe is None:
        return True
    profiles = await _profile_slots(client, state, run.system)
    target = (run.name, probe.profile_name)
    safe = (
        profiles is not None
        and profiles.get(target) == ()
        and _other_profiles(profiles, run) == probe.baseline
        and await _dedicated_inventory(client, state, run)
        and await _token_of(client, state, run, run.uuid or "") == run.token
        and await _lpar_state(client, state, run) == _NOT_ACTIVATED
    )
    state.record(
        SUBTASK,
        "dedicated scratch preconditions",
        "PASS" if safe else "FAIL",
        {"safe": safe, "profiles": _profile_evidence(profiles) if not safe else None},
    )
    if not safe:
        return False
    try:
        probe.attempted = True
        status, data = await state.call(
            client,
            "hmc_modify_lpar",
            lpar_name_or_uuid=run.uuid,
            system_name_or_uuid=run.system,
            assignments={
                "dedicated": [
                    {"profile_name": probe.profile_name, "drc_index": probe.drc_index}
                ]
            },
        )
        probe.data["assignment"] = data
        steps = result_field(data, "steps")
        if not isinstance(steps, (tuple, list)):
            steps = ()
        probe.workflow_completed = (
            status == "PASS"
            and result_field(data, "workflow_completed") is True
            and len(steps) == 1
            and result_field(steps[0], "step") == "dedicated[0]"
            and result_field(steps[0], "status") == "ok"
        )
        state.record(
            SUBTASK,
            "hmc_modify_lpar (dedicated assignments)",
            "PASS" if probe.workflow_completed else "FAIL",
            data,
        )
        profiles = await _profile_slots(client, state, run.system)
        probe.assigned = profiles is not None and profiles.get(target) == (
            ProfileIoSlot(probe.drc_index, None, False),
        )
        probe.others_unchanged = (
            profiles is not None and _other_profiles(profiles, run) == probe.baseline
        )
        state.record(
            SUBTASK,
            "dedicated profile read-back",
            "PASS" if probe.assigned and probe.others_unchanged else "FAIL",
            {
                "assigned": probe.assigned,
                "others_unchanged": probe.others_unchanged,
                "profiles": _profile_evidence(profiles)
                if not (probe.assigned and probe.others_unchanged)
                else None,
            },
        )
    finally:
        await _dedicated_restore(client, state, run)
    return probe.restored


async def _dedicated_compare(client: Client, state: RunState, run: _Run) -> bool:
    probe = run.dedicated
    if probe is None:
        return True
    profiles = await _profile_slots(client, state, run.system)
    slot_unowned = await _dedicated_inventory(client, state, run)
    probe.baseline_restored = profiles == probe.baseline and slot_unowned
    state.record(
        SUBTASK,
        "dedicated final baseline",
        "PASS" if probe.baseline_restored else "FAIL",
        {
            "profiles_restored": profiles == probe.baseline,
            "slot_unowned": slot_unowned,
            "profiles": _profile_evidence(profiles)
            if not probe.baseline_restored
            else None,
        },
    )
    return probe.baseline_restored


def _observe_dedicated(state: RunState, run: _Run, cleanup: str) -> None:
    probe = run.dedicated
    if probe is None or not probe.attempted:
        return
    state.record_verified(
        SUBTASK,
        "hmc_modify_lpar-dedicated",
        operation="lpar.modify",
        scenario=SCENARIO,
        assertions=[
            Assertion("assignment-workflow-completed", probe.workflow_completed),
            Assertion("dedicated-profile-read-back", probe.assigned),
            Assertion("other-profile-slots-unchanged", probe.others_unchanged),
            Assertion(
                "dedicated-profile-restored",
                probe.restored and probe.restore_call_succeeded,
            ),
            Assertion("dedicated-slot-unowned", probe.slot_unowned),
            Assertion("dedicated-baseline-restored", probe.baseline_restored),
        ],
        cleanup=cleanup,
        data=probe.data,
    )


async def _read_pools(
    client: Client, state: RunState, system: str
) -> tuple[_Pools, int] | None:
    """The partition names, free processing units, free memory and region size."""
    quoted = shlex.quote(system)
    names = await _cli(client, state, f"lssyscfg -r lpar -m {quoted} -F name")
    procs = await _cli(
        client,
        state,
        f"lshwres -r proc -m {quoted} --level sys -F curr_avail_sys_proc_units",
    )
    memory = await _cli(
        client,
        state,
        f"lshwres -r mem -m {quoted} --level sys -F curr_avail_sys_mem,mem_region_size",
    )
    if names is None or procs is None or memory is None:
        return None
    available, _, region = memory.partition(",")
    if not region.strip().isdigit():
        return None
    pools = _Pools(frozenset(names.splitlines()), procs, available.strip())
    return pools, int(region)


def _section(data: object, path: tuple[str, ...]) -> Mapping[str, object]:
    node: object = resource(data) if isinstance(data, Mapping) else {}
    for name in path:
        node = node.get(name) if isinstance(node, Mapping) else None
    return node if isinstance(node, Mapping) else {}


def _number(value: object) -> float | None:
    text = leaf_text(value)
    try:
        return float(text) if isinstance(text, str) else None
    except ValueError:
        return None


async def _read_lpar(client: Client, state: RunState, run: _Run) -> tuple[str, Any]:
    return await state.call(
        client,
        "hmc_get_lpar",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
    )


async def _values(client: Client, state: RunState, run: _Run) -> dict[str, float]:
    """The partition's configured memory and shared-processor values, by resource name."""
    st, data = await _read_lpar(client, state, run)
    if st != "PASS":
        return {}
    values = {}
    for key, path, element in _FIELDS:
        number = _number(_section(data, path).get(element))
        if number is not None:
            values[key] = number
    return values


async def _token_of(
    client: Client, state: RunState, run: _Run, lpar: str
) -> str | None:
    st, data = await state.call(
        client,
        "hmc_get_lpar_description",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=lpar,
    )
    if st != "PASS" or not isinstance(data, str):
        return None
    return parse_lpar_ownership_caller_token(data)


async def _absent(client: Client, state: RunState, run: _Run, name: str) -> bool:
    """Whether *name* answers HSCL8012, read twice unless the first answer is plain.

    The `dedicated_pcie.name_absent` rule (#906): an SSH view can lag a REST write, so any
    answer other than HSCL8012 or a readable description is read once more.
    """

    async def lookup() -> tuple[str, Any]:
        return await state.call(
            client,
            "hmc_get_lpar_description",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=name,
        )

    st, data = await lookup()
    if partition_not_found(st, data):
        return True
    if st == "PASS" and isinstance(data, str):
        return False
    await asyncio.sleep(_ABSENCE_REREAD_DELAY_S)
    st, data = await lookup()
    return partition_not_found(st, data)


async def _lpar_state(client: Client, state: RunState, run: _Run) -> str | None:
    st, data = await state.call(
        client,
        "hmc_get_lpar_state",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
    )
    return data.strip().lower() if st == "PASS" and isinstance(data, str) else None


async def _wait_for_state(
    client: Client, state: RunState, run: _Run, wanted: frozenset[str]
) -> str | None:
    observed = None
    for attempt in range(_STATE_POLL_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_STATE_POLL_DELAY_S)
        observed = await _lpar_state(client, state, run)
        if observed in wanted:
            break
    return observed


# ---------------------------------------------------------------------------
# Mutations: one literal dispatch each, always naming the scratch UUID
# ---------------------------------------------------------------------------


async def _dlpar_mem(
    client: Client, state: RunState, run: _Run, resources: dict[str, Any]
) -> tuple[str, Any]:
    st, data = await state.call(
        client,
        "hmc_dlpar_mem",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        resources=resources,
    )
    return st, data


async def _dlpar_proc(
    client: Client, state: RunState, run: _Run, resources: dict[str, Any]
) -> tuple[str, Any]:
    st, data = await state.call(
        client,
        "hmc_dlpar_proc",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        resources=resources,
    )
    return st, data


def _reads_back(
    before: dict[str, float], after: dict[str, float], expected: dict[str, float]
) -> bool:
    """Each expected value reads back, and every other configured value is unchanged."""
    if not before or not after:
        return False
    for key, _, _ in _FIELDS:
        want = expected.get(key, before.get(key))
        got = after.get(key)
        if want is None or got is None or not math.isclose(want, got, abs_tol=1e-6):
            return False
    return True


def _refused_empty(status: str, data: object) -> bool:
    return (
        status == "FAIL"
        and isinstance(data, CallFailure)
        and "Nothing to change" in data.message
    )


async def _resource_cases(
    client: Client,
    state: RunState,
    run: _Run,
    operation: str,
    small: dict[str, float],
    large: dict[str, float],
    no_op: tuple[str, ...],
) -> None:
    """Small, large, no-op and empty requests through one DLPAR tool."""
    change = _dlpar_mem if operation == "lpar.dlpar_mem" else _dlpar_proc
    tool = "hmc_dlpar_mem" if change is _dlpar_mem else "hmc_dlpar_proc"
    readings: dict[str, Any] = {}
    for label, request in (("small", small), ("large", large)):
        before = await _values(client, state, run)
        st, data = await change(client, state, run, request)
        state.record(SUBTASK, f"{tool} ({label})", st, data)
        after = await _values(client, state, run)
        readings[label] = after
        run.hold(
            operation,
            f"{label}-change-read-back",
            st == "PASS" and _reads_back(before, after, request),
        )
    before = await _values(client, state, run)
    request = {key: before[key] for key in no_op if key in before}
    st, data = await change(client, state, run, request)
    state.record(SUBTASK, f"{tool} (no-op)", st, data)
    after = await _values(client, state, run)
    run.hold(
        operation,
        "no-op-accepted-unchanged",
        st == "PASS" and len(request) == len(no_op) and _reads_back(before, after, {}),
    )
    st, data = await change(client, state, run, {})
    refused = _refused_empty(st, data)
    # The refusal is the expected answer, so it is the PASS row.
    state.record(
        SUBTASK, f"{tool} (empty request)", "PASS" if refused else "FAIL", data
    )
    run.hold(operation, "empty-request-refused", refused)
    run.data[tool] = readings


async def _memory_cases(client: Client, state: RunState, run: _Run) -> None:
    current = await _values(client, state, run)
    desired, maximum = current.get("desired_memory"), current.get("max_memory")
    if desired is None or maximum is None:
        state.skip(SUBTASK, "hmc_dlpar_mem", "the partition's memory did not read")
        return
    await _resource_cases(
        client,
        state,
        run,
        "lpar.dlpar_mem",
        {"desired_memory": desired + run.region},
        {"desired_memory": maximum},
        ("desired_memory",),
    )
    before = await _values(client, state, run)
    st, data = await state.call(
        client,
        "hmc_dlpar_mem",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        resources={"desired_memory": maximum + run.region},
    )
    # Recorded, never asserted: the answer itself is what this run captures. The
    # answer is the row's top-level data so the runner redacts a failure's text.
    state.record(
        SUBTASK, "hmc_dlpar_mem (over maximum)", "PASS", data, f"the HMC answered {st}"
    )
    state.record(
        SUBTASK,
        "hmc_dlpar_mem (over maximum read-back)",
        "PASS",
        {"before": before, "after": await _values(client, state, run)},
    )


async def _processor_cases(client: Client, state: RunState, run: _Run) -> None:
    current = await _values(client, state, run)
    if not {"desired_procs", "max_procs", "max_vcpus"} <= current.keys():
        state.skip(SUBTASK, "hmc_dlpar_proc", "the partition's processors did not read")
        return
    await _resource_cases(
        client,
        state,
        run,
        "lpar.dlpar_proc",
        {"desired_procs": round(current["desired_procs"] + 0.1, 2)},
        {"desired_procs": current["max_procs"], "desired_vcpus": current["max_vcpus"]},
        ("desired_procs", "desired_vcpus"),
    )


async def _modify_case(client: Client, state: RunState, run: _Run) -> None:
    """Criterion 3's re-capture: the call ST8 declared an expected HTTP 406."""
    before = await _values(client, state, run)
    request = {
        "desired_memory": RESOURCES["desired_memory"] + run.region,
        "max_memory": RESOURCES["max_memory"] + run.region,
        "desired_procs": 0.6,
    }
    st, data = await state.call(
        client,
        "hmc_modify_lpar",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        resources=request,
    )
    state.record(SUBTASK, "hmc_modify_lpar (memory and units)", st, data)
    after = await _values(client, state, run)
    memory = {key: request[key] for key in ("desired_memory", "max_memory")}
    run.hold(
        "lpar.modify",
        "memory-read-back",
        st == "PASS"
        and all(math.isclose(after.get(k, -1), v) for k, v in memory.items()),
    )
    run.hold(
        "lpar.modify",
        "processing-units-read-back",
        st == "PASS" and math.isclose(after.get("desired_procs", -1), 0.6),
    )
    run.hold(
        "lpar.modify",
        "other-values-unchanged",
        st == "PASS" and _reads_back(before, after, request),
    )
    run.data["hmc_modify_lpar"] = {"request": request, "after": after}


def _uuid_of(data: object) -> str | None:
    if not isinstance(data, Mapping):
        return None
    value = (
        data.get("UUID")
        or data.get("uuid")
        or leaf_text(resource(data).get("PartitionUUID"))
    )
    return value if isinstance(value, str) else None


async def _rename(client: Client, state: RunState, run: _Run, new_name: str) -> bool:
    st, data = await state.call(
        client,
        "hmc_rename_lpar",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
        new_name=new_name,
    )
    state.record(SUBTASK, f"hmc_rename_lpar (to {new_name})", st, data)
    return st == "PASS"


async def _rename_case(client: Client, state: RunState, run: _Run) -> None:
    original, renamed = run.name, f"{run.name}-rn"
    if not await _rename(client, state, run, renamed):
        return
    run.name = renamed
    st, data = await state.call(
        client,
        "hmc_get_lpar",
        lpar_name_or_uuid=renamed,
        system_name_or_uuid=run.system,
    )
    run.hold(
        "lpar.rename",
        "renamed-same-uuid",
        st == "PASS" and (_uuid_of(data) or "").lower() == (run.uuid or "").lower(),
    )
    run.hold(
        "lpar.rename", "old-name-absent", await _absent(client, state, run, original)
    )
    run.hold(
        "lpar.rename",
        "ownership-stamp-kept",
        await _token_of(client, state, run, renamed) == run.token,
    )
    if await _rename(client, state, run, original):
        run.name = original
    run.hold(
        "lpar.rename",
        "original-name-restored",
        run.name == original
        and await _token_of(client, state, run, original) == run.token,
    )
    run.data["hmc_rename_lpar"] = {"renamed_to": renamed, "restored": run.name}


async def _pending_boot_string(
    client: Client, state: RunState, run: _Run
) -> tuple[bool, object]:
    st, data = await state.call(
        client,
        "hmc_read_lpar_boot_order",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
    )
    return st == "PASS", result_field(data, "pending_boot_string")


async def _boot_cases(client: Client, state: RunState, run: _Run) -> None:
    st, data = await state.call(
        client,
        "hmc_set_lpar_boot_order",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
        devices=BOOT_PATHS,
    )
    state.record(SUBTASK, "hmc_set_lpar_boot_order (two paths)", st, data)
    read, pending = await _pending_boot_string(client, state, run)
    run.hold(
        "boot_order.set",
        "pending-boot-string-read-back",
        st == "PASS" and read and pending == " ".join(BOOT_PATHS),
    )
    run.data["hmc_set_lpar_boot_order"] = {"pending_boot_string": pending}

    st, data = await state.call(
        client,
        "hmc_clear_lpar_boot_order",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
    )
    refused = (
        st == "FAIL"
        and isinstance(data, CallFailure)
        and _CLEAR_REFUSAL in data.message
    )
    state.record(
        SUBTASK,
        "hmc_clear_lpar_boot_order (refusal)",
        "PASS" if refused else "FAIL",
        data,
    )
    after_read, after = await _pending_boot_string(client, state, run)
    run.hold("boot_order.clear", "clear-refused-after-authorization", refused)
    run.hold(
        "boot_order.clear",
        "pending-boot-string-unchanged",
        read and after_read and pending == " ".join(BOOT_PATHS) and after == pending,
    )
    run.data["hmc_clear_lpar_boot_order"] = {"pending_boot_string": after}


# ---------------------------------------------------------------------------
# Activated case (#1170): recorded, never observed
# ---------------------------------------------------------------------------


def _profile_uuid(data: object) -> str | None:
    link = (
        resource(data).get("AssociatedPartitionProfile")
        if isinstance(data, Mapping)
        else None
    )
    if isinstance(link, Mapping) and isinstance(link.get("@attrs"), Mapping):
        link = link["@attrs"]
    href = link.get("href") if isinstance(link, Mapping) else None
    match = _UUID_AT_END.search(href) if isinstance(href, str) else None
    return match.group(1) if match else None


async def _configuration(client: Client, state: RunState, run: _Run) -> dict[str, Any]:
    """Both whole configuration sections, `Current*` leaves included, as read."""
    st, data = await _read_lpar(client, state, run)
    if st != "PASS":
        return {}
    return {
        "memory": dict(_section(data, _MEMORY)),
        "processor": dict(_section(data, ("PartitionProcessorConfiguration",))),
    }


async def _record_activated(
    client: Client, state: RunState, run: _Run, tool: str, st: str, data: Any
) -> None:
    after = await _configuration(client, state, run)
    location = result_field(data, "change_location") if st == "PASS" else None
    refused = isinstance(data, CallFailure) and bool(_HMC_REFUSAL.search(data.message))
    if st == "PASS":
        status, note = "PASS", "accepted while activated"
    else:
        status, note = ("SKIP", _ACTIVATED_GAP_NOTE) if refused else ("FAIL", "")
    # The answer is the row's top-level data so the runner redacts a failure's text.
    state.record(SUBTASK, f"{tool} (activated)", status, data, note)
    state.record(
        SUBTASK,
        f"{tool} (activated read-back)",
        "PASS",
        {"change_location": location, "read_back": after},
    )


async def _power_on(client: Client, state: RunState, run: _Run) -> bool:
    st, data = await _read_lpar(client, state, run)
    profile_uuid = _profile_uuid(data) if st == "PASS" else None
    if profile_uuid is None:
        state.skip(SUBTASK, "activated DLPAR", "no associated partition profile read")
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
    state.record(SUBTASK, "hmc_power_on_lpar (to SMS)", st, data)
    reached = await _wait_for_state(client, state, run, _FIRMWARE_STATES)
    if reached not in _FIRMWARE_STATES:
        state.skip(
            SUBTASK,
            "activated DLPAR",
            f"the partition read {reached!r}, not open firmware or running",
        )
        return False
    return True


async def _power_off(client: Client, state: RunState, run: _Run) -> bool:
    st, data = await state.call(
        client,
        "hmc_power_off_lpar",
        lpar_name_or_uuid=run.uuid,
        system_name_or_uuid=run.system,
        immediate=True,
        wait=True,
        timeout_seconds=_JOB_TIMEOUT_S,
        poll_interval=_JOB_POLL_INTERVAL_S,
    )
    state.record(SUBTASK, "hmc_power_off_lpar (immediate)", st, data)
    observed = await _wait_for_state(client, state, run, frozenset({_NOT_ACTIVATED}))
    return observed == _NOT_ACTIVATED


async def _activated_cases(client: Client, state: RunState, run: _Run) -> None:
    if not await _power_on(client, state, run):
        return
    st, data = await _dlpar_mem(
        client, state, run, {"desired_memory": 2048 + run.region}
    )
    await _record_activated(client, state, run, "hmc_dlpar_mem", st, data)
    st, data = await _dlpar_proc(client, state, run, {"desired_procs": 0.7})
    await _record_activated(client, state, run, "hmc_dlpar_proc", st, data)


# ---------------------------------------------------------------------------
# Create, teardown, observations
# ---------------------------------------------------------------------------


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
    record_status, reason = judge_create_result(st, data)
    state.record(SUBTASK, "hmc_create_lpar (scratch)", record_status, data, reason)
    created = result_field(data, "lpar") if st == "PASS" else None
    if isinstance(created, Mapping):
        run.uuid = _uuid_of(created)
    if run.uuid is None and not await _adopt_by_name(client, state, run):
        return False
    run.created = record_status == "PASS"
    return run.created


async def _adopt_by_name(client: Client, state: RunState, run: _Run) -> bool:
    """Resolve the UUID by name, only for a partition carrying this run's token."""
    if await _token_of(client, state, run, run.name) != run.token:
        return False
    st, data = await state.call(
        client,
        "hmc_get_lpar",
        lpar_name_or_uuid=run.name,
        system_name_or_uuid=run.system,
    )
    run.uuid = _uuid_of(data) if st == "PASS" else None
    return run.uuid is not None


async def _gone(client: Client, state: RunState, run: _Run) -> bool:
    """Whether the system lists no partition under this run's name, renamed or not.

    Read from the listing rather than one name, so a rename the HMC applied but
    reported as failed cannot hide the partition; read twice, as `_absent` does.
    """
    base = NAME_PREFIX + run.token.removeprefix(TOKEN_PREFIX)
    command = f"lssyscfg -r lpar -m {shlex.quote(run.system)} -F name"
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(_ABSENCE_REREAD_DELAY_S)
        listing = await _cli(client, state, command)
        if listing is not None and not any(
            name.startswith(base) for name in listing.splitlines()
        ):
            return True
    return False


async def _delete(
    client: Client, state: RunState, run: _Run, *, inactive_only: bool = False
) -> str | None:
    """Delete the scratch partition; return why it is not confirmed gone, or None."""
    if run.uuid is None and not await _adopt_by_name(client, state, run):
        if await _gone(client, state, run):
            run.deleted = True
            return None
        return "its UUID could not be resolved to a partition carrying this run's token"
    if await _lpar_state(client, state, run) != _NOT_ACTIVATED:
        if inactive_only:
            return "inactive-only cleanup refused an active or unreadable state; no power action was taken"
        if not await _power_off(client, state, run):
            return "it did not reach Not Activated"
    if await _token_of(client, state, run, run.uuid or "") != run.token:
        return "its description no longer carries this run's caller token"
    st, data = await state.call(
        client,
        "hmc_delete_lpar",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
    )
    state.record(run.subtask, "hmc_delete_lpar (scratch)", st, data)
    if st != "PASS":
        return "the delete was refused"
    run.deleted = await _gone(client, state, run)
    return (
        None if run.deleted else "its absence could not be confirmed after the delete"
    )


async def _teardown(
    client: Client, state: RunState, run: _Run, *, inactive_only: bool = False
) -> None:
    if not run.create_attempted:
        return
    why = (
        "dedicated restoration is uncertain; inspect profile io_slots and slot ownership first"
        if run.dedicated is not None and not run.dedicated.restored
        else await _delete(client, state, run, inactive_only=inactive_only)
    )
    if why is None:
        return
    system, name = shlex.quote(run.system), shlex.quote(run.name)
    state.record(
        run.subtask,
        "scratch partition teardown",
        "FAIL",
        f"MANUAL RECOVERY REQUIRED: partition {run.name!r} on {run.system!r} was NOT "
        f"deleted: {why}. Once its description shows caller token {run.token!r}: "
        f"`chsysstate -m {system} -r lpar -n {name} -o shutdown --immed` (when not "
        f"Not Activated), then `rmsyscfg -r lpar -m {system} -n {name}`.",
    )


async def _compare(
    client: Client, state: RunState, run: _Run, baseline: _Pools
) -> bool:
    reads = []
    for attempt in range(2):
        if attempt:
            await asyncio.sleep(_COMPARE_REREAD_DELAY_S)
        read = await _read_pools(client, state, run.system)
        reads.append(read[0] if read else None)
        if reads[-1] == baseline:
            break
    same = reads[-1] == baseline
    state.record(
        run.subtask,
        "system baseline compare",
        "PASS" if same else "FAIL",
        {
            "baseline": _pools_data(baseline),
            "reads": [_pools_data(read) if read else None for read in reads],
        },
    )
    return same


def _pools_data(pools: _Pools) -> dict[str, Any]:
    return {
        "partitions": sorted(pools.names),
        "available_processing_units": pools.procs,
        "available_memory_mib": pools.memory,
    }


def _observe(state: RunState, run: _Run, cleanup: str) -> None:
    """The resource observations, at literal call sites the scenario-gap and
    assertion-id guards can read."""
    state.record_verified(
        SUBTASK,
        "hmc_modify_lpar",
        operation="lpar.modify",
        scenario=SCENARIO,
        assertions=[
            Assertion(
                "memory-read-back", run.held.get("lpar.modify:memory-read-back", False)
            ),
            Assertion(
                "processing-units-read-back",
                run.held.get("lpar.modify:processing-units-read-back", False),
            ),
            Assertion(
                "other-values-unchanged",
                run.held.get("lpar.modify:other-values-unchanged", False),
            ),
        ],
        cleanup=cleanup,
        data=run.data.get("hmc_modify_lpar"),
    )
    state.record_verified(
        SUBTASK,
        "hmc_dlpar_mem",
        operation="lpar.dlpar_mem",
        scenario=SCENARIO,
        assertions=[
            Assertion(
                "small-change-read-back",
                run.held.get("lpar.dlpar_mem:small-change-read-back", False),
            ),
            Assertion(
                "large-change-read-back",
                run.held.get("lpar.dlpar_mem:large-change-read-back", False),
            ),
            Assertion(
                "no-op-accepted-unchanged",
                run.held.get("lpar.dlpar_mem:no-op-accepted-unchanged", False),
            ),
            Assertion(
                "empty-request-refused",
                run.held.get("lpar.dlpar_mem:empty-request-refused", False),
            ),
        ],
        cleanup=cleanup,
        data=run.data.get("hmc_dlpar_mem"),
    )
    state.record_verified(
        SUBTASK,
        "hmc_dlpar_proc",
        operation="lpar.dlpar_proc",
        scenario=SCENARIO,
        assertions=[
            Assertion(
                "small-change-read-back",
                run.held.get("lpar.dlpar_proc:small-change-read-back", False),
            ),
            Assertion(
                "large-change-read-back",
                run.held.get("lpar.dlpar_proc:large-change-read-back", False),
            ),
            Assertion(
                "no-op-accepted-unchanged",
                run.held.get("lpar.dlpar_proc:no-op-accepted-unchanged", False),
            ),
            Assertion(
                "empty-request-refused",
                run.held.get("lpar.dlpar_proc:empty-request-refused", False),
            ),
        ],
        cleanup=cleanup,
        data=run.data.get("hmc_dlpar_proc"),
    )


def _observe_identity(state: RunState, run: _Run, cleanup: str) -> None:
    """The rename and boot-order observations; literal sites, as `_observe`."""
    state.record_verified(
        SUBTASK,
        "hmc_rename_lpar",
        operation="lpar.rename",
        scenario=SCENARIO,
        assertions=[
            Assertion(
                "renamed-same-uuid",
                run.held.get("lpar.rename:renamed-same-uuid", False),
            ),
            Assertion(
                "old-name-absent", run.held.get("lpar.rename:old-name-absent", False)
            ),
            Assertion(
                "ownership-stamp-kept",
                run.held.get("lpar.rename:ownership-stamp-kept", False),
            ),
            Assertion(
                "original-name-restored",
                run.held.get("lpar.rename:original-name-restored", False),
            ),
        ],
        cleanup=cleanup,
        data=run.data.get("hmc_rename_lpar"),
    )
    state.record_verified(
        SUBTASK,
        "hmc_set_lpar_boot_order",
        operation="boot_order.set",
        scenario=SCENARIO,
        assertions=[
            Assertion(
                "pending-boot-string-read-back",
                run.held.get("boot_order.set:pending-boot-string-read-back", False),
            ),
        ],
        cleanup=cleanup,
        data=run.data.get("hmc_set_lpar_boot_order"),
    )
    state.record_verified(
        SUBTASK,
        "hmc_clear_lpar_boot_order",
        operation="boot_order.clear",
        scenario=SCENARIO,
        assertions=[
            Assertion(
                "clear-refused-after-authorization",
                run.held.get(
                    "boot_order.clear:clear-refused-after-authorization", False
                ),
            ),
            Assertion(
                "pending-boot-string-unchanged",
                run.held.get("boot_order.clear:pending-boot-string-unchanged", False),
            ),
        ],
        cleanup=cleanup,
        data=run.data.get("hmc_clear_lpar_boot_order"),
    )


async def exercise_lpar_config(client: Client, state: RunState) -> None:
    print("\n=== ST39: LPAR configuration, DLPAR and boot order (issue #1345) ===")
    if state.group != GROUP:
        state.skip(
            SUBTASK,
            "lpar-config arm",
            f"creates and deletes a partition: runs only in the {GROUP!r} group",
        )
        return
    system = state.config.system_name
    read = await _read_pools(client, state, system)
    if read is None:
        state.skip(SUBTASK, "lpar-config arm", "the system baseline did not read")
        return
    baseline, region = read
    state.record(SUBTASK, "system baseline", "PASS", _pools_data(baseline))
    if stranded := scratch_partitions(baseline.names):
        state.skip(
            SUBTASK,
            "lpar-config arm",
            f"{stranded} already exist: run live_test_recovery.py first",
        )
        return
    suffix = uuid.uuid4().hex[:8]
    run = _Run(system, f"{NAME_PREFIX}{suffix}", f"{TOKEN_PREFIX}{suffix}", region)
    run.dedicated = await _select_dedicated(client, state, run)
    for path in ("SR-IOV", "vNIC"):
        state.skip(
            SUBTASK,
            f"hmc_modify_lpar {path} assignments",
            "requires separate operator authorization and eligible backing hardware/RMC",
        )
    try:
        if await _create(client, state, run) and await _dedicated_case(
            client, state, run
        ):
            await _modify_case(client, state, run)
            await _memory_cases(client, state, run)
            await _processor_cases(client, state, run)
            await _rename_case(client, state, run)
            await _boot_cases(client, state, run)
            await _activated_cases(client, state, run)
    finally:
        await _teardown(client, state, run)
    same = await _compare(client, state, run, baseline)
    dedicated_same = await _dedicated_compare(client, state, run)
    same = same and dedicated_same
    if run.created:
        cleanup = "passed" if run.deleted and same else "failed"
        _observe(state, run, cleanup)
        _observe_identity(state, run, cleanup)
        _observe_dedicated(state, run, cleanup)


async def _settings_cases(client: Client, state: RunState, run: _Run) -> bool:
    status, data = await state.call(
        client,
        "hmc_get_lpar_settings",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
    )
    before = plain_data(data)
    if status != "PASS" or not isinstance(before, dict):
        state.record(SETTINGS_SUBTASK, "settings baseline", "FAIL", data)
        return False
    for patch in (
        {"allow_perf_collection": True},
        {"allow_perf_collection": False},
        {"lpar_avail_priority": 255},
        {"lpar_avail_priority": 0},
        {"time_ref": False},
        {"simplified_remote_restart_capable": True},
        {"simplified_remote_restart_capable": False},
        {"migration_disabled": True},
        {"migration_disabled": False},
    ):
        if (
            await _lpar_state(client, state, run) != _NOT_ACTIVATED
            or await _token_of(client, state, run, run.uuid or "") != run.token
        ):
            state.record(
                SETTINGS_SUBTASK,
                "inactive owned fixture",
                "FAIL",
                "Fixture state/ownership changed; stopped before mutation",
            )
            return False
        status, data = await state.call(
            client,
            "hmc_configure_lpar",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=run.uuid,
            settings=patch,
        )
        result = plain_data(data)
        verified = (
            status == "PASS"
            and isinstance(result, dict)
            and result.get("status") == "verified"
        )
        state.record(
            SETTINGS_SUBTASK, "hmc_configure_lpar", "PASS" if verified else "FAIL", data
        )
        if not verified:
            return False
        status, data = await state.call(
            client,
            "hmc_get_lpar_settings",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=run.uuid,
        )
        after = plain_data(data)
        if status != "PASS" or after != before | patch:
            state.record(SETTINGS_SUBTASK, "settings postcondition", "FAIL", data)
            return False
        before = after
    return True


async def exercise_lpar_settings(client: Client, state: RunState) -> None:
    """ST47: one owned inactive fixture, nine bounded writes; no power/profile calls."""
    if state.group != SETTINGS_GROUP or state.hmc_config is None:
        state.skip(
            SETTINGS_SUBTASK,
            SETTINGS_GROUP,
            "requires opt-in group and HMC configuration",
        )
        return
    baseline = await _read_pools(client, state, state.config.system_name)
    if baseline is None or scratch_partitions(baseline[0].names):
        state.skip(
            SETTINGS_SUBTASK,
            SETTINGS_GROUP,
            "baseline unavailable or stranded scratch exists; run recovery",
        )
        return
    suffix = uuid.uuid4().hex[:8]
    run = _Run(
        state.config.system_name,
        NAME_PREFIX + suffix,
        TOKEN_PREFIX + suffix,
        baseline[1],
        subtask=SETTINGS_SUBTASK,
    )
    verified = False
    try:
        run.create_attempted = True
        async with HMCClient(state.hmc_config) as hmc:
            created = await create_and_stamp_lpar(
                hmc,
                run.system,
                LparCreation(
                    run.name,
                    "AIX/Linux",
                    LparResources(**RESOURCES),
                    caller_token=run.token,
                    stamp_policy="required",
                    apply_profile=False,
                    settings=LparSettings(False, 0, False, False, False),
                ),
            )
        run.uuid = _uuid_of(created.lpar) if created.lpar is not None else None
        if (
            not created.resource_created
            or created.ownership_stamped is not True
            or created.settings_step is None
            or created.settings_step.status != "ok"
            or run.uuid is None
        ):
            raise ValueError(
                "Required ownership or creation settings were not confirmed"
            )
        run.created = True
        state.record(
            SETTINGS_SUBTASK,
            "owning core create",
            "PASS",
            asdict(created),
            "Direct operation; no MCP create promotion",
        )
        verified = await _settings_cases(client, state, run)
    except (HMCError, ValueError, PermissionError) as error:
        state.record(SETTINGS_SUBTASK, "settings arm", "FAIL", str(error))
    finally:
        await _teardown(client, state, run, inactive_only=True)
    same = await _compare(client, state, run, baseline[0])
    cleanup = "passed" if run.deleted and same else "failed"
    if run.created:
        state.record_verified(
            SETTINGS_SUBTASK,
            "hmc_configure_lpar",
            operation="lpar.configure",
            scenario=SETTINGS_SCENARIO,
            assertions=[Assertion("settings-round-trip", verified)],
            cleanup=cleanup,
            data={"time_ref_true": "not-run", "activation": "not-run"},
        )
        state.record_verified(
            SETTINGS_SUBTASK,
            "hmc_get_lpar_settings",
            operation="lpar.get_settings",
            scenario=SETTINGS_SCENARIO,
            assertions=[Assertion("typed-settings-readback", verified)],
            cleanup=cleanup,
            data={"time_ref_true": "not-run"},
        )


async def _reporting_cases(client: Client, state: RunState, run: _Run) -> bool:
    status, data = await state.call(
        client,
        "hmc_get_lpar_reporting_settings",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
    )
    before = plain_data(data)
    if status != "PASS" or not isinstance(before, dict):
        state.record(REPORTING_SUBTASK, "settings baseline", "FAIL", data)
        return False
    for patch in (
        {"ipl_source": "a"},
        {"ipl_source": "b"},
        {"ipl_source": "a"},
        {"os400_restricted_io_mode": True},
        {"os400_restricted_io_mode": False},
        {"electronic_err_reporting": True},
        {"electronic_err_reporting": False},
        {"work_group_id": "none"},
    ):
        if (
            await _lpar_state(client, state, run) != _NOT_ACTIVATED
            or await _token_of(client, state, run, run.uuid or "") != run.token
        ):
            state.record(
                REPORTING_SUBTASK,
                "inactive owned fixture",
                "FAIL",
                "Fixture state/ownership changed; stopped before mutation",
            )
            return False
        status, data = await state.call(
            client,
            "hmc_configure_lpar_reporting",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=run.uuid,
            settings=patch,
        )
        result = plain_data(data)
        verified = (
            status == "PASS"
            and isinstance(result, dict)
            and result.get("status") == "verified"
        )
        state.record(
            REPORTING_SUBTASK,
            "hmc_configure_lpar_reporting",
            "PASS" if verified else "FAIL",
            data,
        )
        if not verified:
            return False
        status, data = await state.call(
            client,
            "hmc_get_lpar_reporting_settings",
            system_name_or_uuid=run.system,
            lpar_name_or_uuid=run.uuid,
        )
        after = plain_data(data)
        if status != "PASS" or after != before | patch:
            state.record(REPORTING_SUBTASK, "settings postcondition", "FAIL", data)
            return False
        before = after
    return True


async def exercise_lpar_reporting(client: Client, state: RunState) -> None:
    """ST48: one owned inactive IBM i fixture, eight bounded writes; no power/profile calls."""
    if state.group != REPORTING_GROUP or state.hmc_config is None:
        state.skip(
            REPORTING_SUBTASK,
            REPORTING_GROUP,
            "requires opt-in group and HMC configuration",
        )
        return
    baseline = await _read_pools(client, state, state.config.system_name)
    if baseline is None or scratch_partitions(baseline[0].names):
        state.skip(
            REPORTING_SUBTASK,
            REPORTING_GROUP,
            "baseline unavailable or stranded scratch exists; run recovery",
        )
        return
    suffix = uuid.uuid4().hex[:8]
    run = _Run(
        state.config.system_name,
        NAME_PREFIX + suffix,
        TOKEN_PREFIX + suffix,
        baseline[1],
        subtask=REPORTING_SUBTASK,
    )
    verified = False
    try:
        run.create_attempted = True
        async with HMCClient(state.hmc_config) as hmc:
            created = await create_and_stamp_lpar(
                hmc,
                run.system,
                LparCreation(
                    run.name,
                    "OS400",
                    LparResources(**RESOURCES),
                    caller_token=run.token,
                    stamp_policy="required",
                    apply_profile=False,
                    physical_io=LparPhysicalIO(console_slot="hmc"),
                    reporting_settings=LparReportingSettings(
                        os400_restricted_io_mode=False,
                        work_group_id="none",
                        electronic_err_reporting=False,
                        redundant_err_path_reporting=False,
                    ),
                ),
            )
        run.uuid = _uuid_of(created.lpar) if created.lpar is not None else None
        if (
            not created.resource_created
            or created.ownership_stamped is not True
            or created.reporting_step is None
            or created.reporting_step.status != "ok"
            or created.physical_io_step is None
            or created.physical_io_step.status != "ok"
            or run.uuid is None
        ):
            raise ValueError(
                "Required ownership, console I/O or reporting settings were not confirmed"
            )
        run.created = True
        state.record(
            REPORTING_SUBTASK,
            "owning core create",
            "PASS",
            asdict(created),
            "Direct operation; no MCP create promotion",
        )
        verified = await _reporting_cases(client, state, run)
    except (HMCError, ValueError, PermissionError) as error:
        state.record(REPORTING_SUBTASK, "settings arm", "FAIL", str(error))
    finally:
        await _teardown(client, state, run, inactive_only=True)
    same = await _compare(client, state, run, baseline[0])
    cleanup = "passed" if run.deleted and same else "failed"
    if run.created:
        state.record_verified(
            REPORTING_SUBTASK,
            "hmc_configure_lpar_reporting",
            operation="lpar.configure_reporting",
            scenario=REPORTING_SCENARIO,
            assertions=[Assertion("reporting-round-trip", verified)],
            cleanup=cleanup,
            data={"activation": "not-run", "guest_ipl": "not-run"},
        )
        state.record_verified(
            REPORTING_SUBTASK,
            "hmc_get_lpar_reporting_settings",
            operation="lpar.get_reporting_settings",
            scenario=REPORTING_SCENARIO,
            assertions=[Assertion("typed-reporting-readback", verified)],
            cleanup=cleanup,
            data={"guest_ipl": "not-run"},
        )
