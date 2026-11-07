"""Confirm from outside a run that it left nothing behind.

Arm cleanup guards refuse to mutate on a mismatch and emit a manual-recovery
row, which is correct. But the only witness that recovery happened is the same
run that failed to complete it. This script is the outside witness.

Usage:
    uv run --no-sync python scripts/live_test_recovery.py --results PATH

It witnesses the dedicated PCIe and bare-cec arms (subtasks 24-25) by their run
marker, the users arm (subtask 11) by any HMC user still carrying its reserved
`hmcpctl-live-` prefix, and the vMedia arm (subtasks 16-22) by what it can leave on the run's
configured test partition: a running partition, a changed pending boot string,
an optical mapping, a VIOS vSCSI server adapter with no mapping, a medium it
created, and the media repository it created. It witnesses the vios-backup arm (subtask 37) by the backup
and disk mapping that run recorded: a backup still in the catalog (remedy:
`rmviosbk`) and a disk mapping not put back (remedy: `mkvdev`). It witnesses
the network arm (subtask 9) by the baselines that run recorded: a network left on
its test VLAN, a client adapter on the test partition off its baseline, an FC-port
label off its original, and a vFC group label it named. Subtask 2 only reads, so
there is nothing for it to leave. It witnesses the lpar-config arm (subtask 39) by
any partition still carrying its reserved `hmcpctl-live-lpar-` prefix, and the
lpar-power arm (subtask 41) by a `hmcpctl-live-pwr-` partition, a VIOS mapping backed
by an `lppwr` volume, an `lppwr` volume left in the configured volume group, or a VIOS
vSCSI adapter serving a `hmcpctl-live-pwr-` partition. The lpar-reconfigure arm
(subtask 70) is witnessed the same way for its `lprcf` volume and the
`hmcpctl-live-lpar-` partition it served, beside lpar-config's prefix check. Complete round2 runs carry
an ST0 snapshot: recovery independently recaptures system and VIOS tables and
reads the original partition's description/profile compatibility. Missing witness
data or scope mismatches fail closed. Every other
subtask the run dispatched is listed as NOT WITNESSED, to be checked by hand
(docs/live-testing.md, step 4).

Exit 0 means every dispatched subtask was witnessed and nothing is stranded,
or a complete unarmed ST58 record was classified SKIPPED with no fixture work.
That SKIPPED disposition is not independent baseline or live verification.
Exit 1 means something is, and the output names it with the command that
clears it. Exit 2 means some state could not be read, or the run dispatched a
subtask this script does not witness, which is not the same as clean. A run
that an exception or interrupt stopped (`run.partial` present and not `false`)
also exits 2 whatever the checks found, under a PARTIAL header: the call in
flight at the stop may have no row, so complete execution is not attested.

**This never remediates.** It issues no mutating call: a remediator acting on
a partial read strands exactly what the arm's cleanup guards exist to refuse.
Every command it prints is for a human to run and check.

Its inputs come from the run's own results document: `run.subtasks`, the
`config` the run used, and its `artifacts` and result rows. The PCIe marker is
per-run random (`pcie-<8 hex>`), so a run's traces cannot be identified without
them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import stat
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from live_test import baseline as independent_baseline
from live_test import config as live_config
from live_test import (
    creation_memory,
    creation_memory_layout,
    logical_workflow,
    lpar_affinity_group,
    lpar_config,
    lpar_hardware_memory,
    lpar_linux_roles,
    lpar_physical_io,
    lpar_power,
    lpar_reconfigure,
    lpar_security_policy,
    lpar_vtpm,
    memory_quantities,
    network,
    processor_quantities,
    processor_sharing_weight,
    provision_boot,
    runner,
    storage_lifecycle,
    storage_volume_group,
    vios_backup,
)
from live_test.dedicated_pcie import (
    _DEFAULT_DEDICATED_PROFILE,
    _io_slots_contains,
    partition_not_found,
    select_profile_io_slots,
)
from live_test.observation import plain_data
from live_test.profiles import ARTIFACT_PREFIX, backup_artifact_names
from live_test.recovery.backup import (
    check_vios_backup,
    vios_backup_inputs_from_document,
)
from live_test.recovery.common import (
    NOT_ACTIVATED,
    Finding,
    StateUnreadable,
    recorded_calls,
)
from live_test.recovery.memory import (
    check_creation_memory,
    check_creation_memory_layout,
    creation_memory_inputs_from_document,
    creation_memory_layout_inputs_from_document,
    creation_memory_layout_skip_reason,
    creation_memory_skip_reason,
)
from live_test.recovery.network import check_network, network_inputs_from_document
from live_test.recovery.storage import (
    MEDIA_SUBTASKS,
    check_provision_boot_media,
    check_test_partition,
    lpar_inputs_from_document,
    provision_boot_media_inputs_from_document,
)
from live_test.results import entries
from live_test.storage import scsi_adapter_listing
from live_test.users import profile_rows, scratch_users

from hmcpctl.config import HMCConfig
from hmcpctl.operations.lpar.ownership import parse_lpar_ownership_caller_token
from hmcpctl.ssh.commands import HMC_NO_RESULTS
from hmcpctl.ssh.profiles import profile_io_slot_rows_command
from hmcpctl.ssh.transport import HMCCLIError

#: Every tool this script may call. Enforced on the call path rather than left
#: to review: the whole point of the script is that it cannot make things worse
#: while checking whether they are bad.
_READ_ONLY_TOOLS = frozenset(
    {
        "hmc_list_dedicated_pcie_slots",
        "hmc_list_backup_lpar_profiles",
        "hmc_get_lpar_description",
        "hmc_get_lpar_proc_compat",
        "hmc_get_lpar_state",
        "hmc_run_command",
        "hmc_read_lpar_boot_order",
        "hmc_list_optical_mappings",
        "hmc_list_storage_mappings",
        "hmc_list_volume_groups",
        "hmc_get_media_repository",
        "hmc_get_console_info",
        "hmc_list_users",
        "hmc_list_optical_media",
        "hmc_list_vios_backups",
        "hmc_list_vios",
        "hmc_list_lpars",
        "hmc_get_system",
        "hmc_list_virtual_networks",
        "hmc_list_adapters",
        "hmc_list_vios_fc_port_labels",
        "hmc_list_vios_vfc_group_labels",
    }
)

#: `hmc_run_command` is read-only only for the commands given. `lssyscfg` and
#: `lshwres` list; `chsyscfg` would mutate, and shares the tool.
_READ_ONLY_COMMAND_PREFIXES = ("lssyscfg ", "lshwres ")

#: The one VIOS command admitted: a volume group's `lsvg -lv` listing, read by the
#: storage and lpar-power arms' checks. A `viosvrcmd` prefix would admit any VIOS
#: command (`rmlv` included), so the whole shape must match, built from a closed
#: character set.
_VOLUME_LISTING = re.compile(
    r"viosvrcmd -m [A-Za-z0-9_.-]+ --id [0-9]+ -c 'lsvg -lv [A-Za-z0-9_.-]+'"
)

#: A command built from a results document must not be able to chain a second
#: one behind an admitted prefix.
_SHELL_METACHARACTERS = frozenset(";|&$`<>()\n")
#: The users arm (#632). Its scratch users are found by their reserved prefix,
#: not by the document's name, so a run killed before it wrote its document, or
#: overwritten by a later run's, still has its user reported.
_USERS_SUBTASK = 11
#: The storage arm (#1348): ST40's volume and mapping are read back by name prefix;
#: ST0 and ST3 only read.
_STORAGE_SUBTASKS = frozenset(
    {0, 3, storage_lifecycle.SUBTASK, storage_volume_group.SUBTASK}
)
_WITNESSED_SUBTASKS = (
    MEDIA_SUBTASKS
    | _STORAGE_SUBTASKS
    | {
        _USERS_SUBTASK,
        43,
        24,
        25,
        vios_backup.SUBTASK,
        network.INVENTORY_SUBTASK,
        network.SUBTASK,
        lpar_config.SUBTASK,
        lpar_config.SETTINGS_SUBTASK,
        lpar_config.REPORTING_SUBTASK,
        lpar_physical_io.SUBTASK,
        lpar_vtpm.SUBTASK,
        lpar_reconfigure.SUBTASK,
        memory_quantities.SUBTASK,
        processor_quantities.SUBTASK,
        processor_sharing_weight.SUBTASK,
        lpar_security_policy.SUBTASK,
        lpar_hardware_memory.SUBTASK,
        creation_memory.SUBTASK,
        creation_memory_layout.SUBTASK,
        lpar_affinity_group.SUBTASK,
        lpar_linux_roles.SUBTASK,
        lpar_power.SUBTASK,
        provision_boot.SUBTASK,
        logical_workflow.SUBTASK,
    }
)


class MutatingCallRefused(RuntimeError):
    """Raised when a call would leave the read-only surface."""


@dataclass(frozen=True)
class RecoveryInputs:
    """What one run created, as recorded in its results document."""

    system_name: str
    run_marker: str
    fixture_lpar: str
    drc_index: str | None
    baseline_io_slots: str | None
    profile_name: str


def inputs_from_document(document: Any) -> RecoveryInputs | None:
    """Read a run's recovery inputs, or `None` when it recorded none.

    A run that never reached ST29 created nothing, so an absent marker means
    there is nothing to look for — not that the check could not run.
    """
    if not isinstance(document, dict):
        return None
    artifacts = document.get("artifacts")
    config = document.get("config")
    if not isinstance(artifacts, dict) or not isinstance(config, dict):
        return None
    marker = artifacts.get("pcie_run_marker")
    fixture = artifacts.get("pcie_fixture_lpar")
    system = config.get("dedicated_pcie_system_name")
    if not marker or not fixture or not system:
        return None
    return RecoveryInputs(
        system_name=str(system),
        run_marker=str(marker),
        fixture_lpar=str(fixture),
        drc_index=artifacts.get("pcie_drc_index"),
        baseline_io_slots=artifacts.get("pcie_baseline_io_slots"),
        # The arm's own default, imported rather than restated: a run that
        # left the key unset records an empty string here, and a second
        # spelling of the fallback would query and remediate a profile name
        # the arm never used.
        profile_name=str(
            config.get("dedicated_pcie_profile_name") or _DEFAULT_DEDICATED_PROFILE
        ),
    )


def users_witnessed(document: dict[str, Any], subtasks: list[int]) -> bool:
    """Whether to look for users-arm scratch users.

    Raises `ValueError` for a document written before #632: its ST11 created a
    configured user the prefix scan cannot recognise, so it cannot be witnessed.
    """
    if _USERS_SUBTASK not in subtasks:
        return False
    artifacts = document.get("artifacts")
    if not (
        isinstance(artifacts, dict) and "test_user_name" in artifacts
    ) and recorded_calls(document, {_USERS_SUBTASK}, {"hmc_create_user"}):
        raise ValueError(
            "this document's ST11 predates the users arm (#632) and created a "
            "configured user the check cannot recognise; confirm by hand that "
            "it is gone"
        )
    return True


async def _scratch_users_left(call) -> list[Finding]:
    """One finding per HMC user carrying the users arm's reserved prefix."""
    status, console = await call("hmc_get_console_info")
    console_uuid = (
        console.get("uuid") or console.get("UUID")
        if status == "PASS" and isinstance(console, dict)
        else None
    )
    if not isinstance(console_uuid, str):
        raise StateUnreadable("the console UUID, to list HMC users")
    status, listing = await call("hmc_list_users", console_uuid=console_uuid)
    rows = profile_rows(listing) if status == "PASS" else {}
    if status != "PASS" or len(rows) != len(entries(listing)):
        raise StateUnreadable("the HMC user list")
    return [
        Finding(
            what=f"HMC user {name}",
            detail="a users-arm scratch user is still defined (viewer role)",
            remedy=f"rmhmcusr -u {name}",
        )
        for name in scratch_users(rows)
    ]


@dataclass(frozen=True)
class LparConfigInputs:
    """The system the lpar-config arm (ST39) created its scratch partition on."""

    system_name: str


def lpar_config_inputs_from_document(
    document: dict[str, Any], subtasks: list[int]
) -> LparConfigInputs | None:
    """The system for a creation/configuration arm using the shared scratch prefix.

    The reserved name prefix is the identity, as the users arm's is: a run
    interrupted before it wrote its document still has its partition found.
    """
    config = document.get("config")
    system = config.get("system_name") if isinstance(config, dict) else None
    if processor_sharing_weight.SUBTASK in subtasks and (
        not isinstance(system, str) or not system.strip()
    ):
        raise ValueError(
            "ST59 recovery requires a readable nonempty config.system_name"
        )
    if (
        not {
            lpar_config.SUBTASK,
            lpar_config.SETTINGS_SUBTASK,
            lpar_config.REPORTING_SUBTASK,
            lpar_physical_io.SUBTASK,
            lpar_vtpm.SUBTASK,
            lpar_reconfigure.SUBTASK,
            memory_quantities.SUBTASK,
            processor_quantities.SUBTASK,
            processor_sharing_weight.SUBTASK,
            lpar_security_policy.SUBTASK,
            lpar_hardware_memory.SUBTASK,
            creation_memory_layout.SUBTASK,
            lpar_affinity_group.SUBTASK,
            lpar_linux_roles.SUBTASK,
        }.intersection(subtasks)
        or not system
    ):
        return None
    return LparConfigInputs(str(system))


async def check_lpar_config(call, inputs: LparConfigInputs) -> list[Finding]:
    """One finding per partition carrying the lpar-config arm's reserved prefix."""
    system = shlex.quote(inputs.system_name)
    status, listing = await call(
        "hmc_run_command", cmd=f"lssyscfg -r lpar -m {system} -F name,state"
    )
    if status != "PASS" or not isinstance(listing, str):
        raise StateUnreadable(f"the partitions of {inputs.system_name}")
    states = dict(line.split(",", 1) for line in listing.splitlines() if "," in line)
    findings = []
    for name in lpar_config.scratch_partitions(states):
        quoted = shlex.quote(name)
        shutdown = (
            ""
            if states[name] == NOT_ACTIVATED
            else f"chsysstate -m {system} -r lpar -n {quoted} -o shutdown --immed; "
        )
        findings.append(
            Finding(
                what=f"partition {name}",
                detail=(
                    f"an lpar-config scratch partition is still defined "
                    f"({states[name]}); its description should carry caller token "
                    f"{lpar_config.TOKEN_PREFIX}<the name's 8 hex>. Before removal, inspect "
                    "all scratch profile io_slots and dedicated-slot ownership; restore "
                    "any run-added slot only after confirming the saved baseline"
                ),
                remedy=f"{shutdown}rmsyscfg -r lpar -m {system} -n {quoted}",
            )
        )
    return findings


@dataclass(frozen=True)
class LparPowerInputs:
    """The system an arm with VIOS storage residue created its partitions and volume on.

    ``arm`` supplies the arm's ``scratch_partitions`` and ``scratch_volumes`` prefix
    checks: lpar-power (ST41) by default, or lpar-reconfigure (ST70).
    """

    system_name: str
    volume_group: str = ""
    arm: Any = lpar_power
    label: str = "lpar-power"


def lpar_power_inputs_from_document(
    document: dict[str, Any], subtasks: list[int]
) -> LparPowerInputs | None:
    """The run's system when it dispatched ST41, ST72 or ST73, else `None`; found by prefix.

    ST72 (provision-boot) and ST73 (logical-workflow) reuse lpar-power's partition and
    volume names.
    """
    config = document.get("config")
    system = config.get("system_name") if isinstance(config, dict) else None
    witnessed = {lpar_power.SUBTASK, provision_boot.SUBTASK, logical_workflow.SUBTASK}
    if not witnessed & set(subtasks) or not system:
        return None
    group = config.get("vdisk_volume_group_name") if isinstance(config, dict) else ""
    return LparPowerInputs(str(system), str(group or ""))


def lpar_reconfigure_inputs_from_document(
    document: dict[str, Any], subtasks: list[int]
) -> LparPowerInputs | None:
    """The run's system and group when it dispatched ST70, else `None`."""
    config = document.get("config")
    system = config.get("system_name") if isinstance(config, dict) else None
    if lpar_reconfigure.SUBTASK not in subtasks or not system:
        return None
    group = config.get("vdisk_volume_group_name") if isinstance(config, dict) else ""
    return LparPowerInputs(
        str(system), str(group or ""), lpar_reconfigure, "lpar-reconfigure"
    )


async def _cli_rows(call, command: str, what: str) -> list[list[str]]:
    status, listing = await call("hmc_run_command", cmd=command)
    if status != "PASS" or not isinstance(listing, str):
        raise StateUnreadable(what)
    if listing.strip() == HMC_NO_RESULTS:
        return []
    return [line.split(",") for line in listing.splitlines() if "," in line]


async def check_lpar_power(call, inputs: LparPowerInputs) -> list[Finding]:
    """Run partitions, and on each VIOS run volumes, mappings backed by one, and
    adapters serving a run partition."""
    system = shlex.quote(inputs.system_name)
    rows = await _cli_rows(
        call,
        f"lssyscfg -r lpar -m {system} -F name,state,lpar_env,lpar_id",
        f"the partitions of {inputs.system_name}",
    )
    findings = []
    for name, state, _, _ in (row for row in rows if len(row) == 4):
        if not lpar_power.scratch_partitions([name]):
            continue
        quoted = shlex.quote(name)
        shutdown = (
            ""
            if state == NOT_ACTIVATED
            else f"chsysstate -m {system} -r lpar -n {quoted} -o shutdown --immed; "
        )
        findings.append(
            Finding(
                what=f"partition {name}",
                detail=f"an lpar-power partition is still defined ({state})",
                remedy=f"{shutdown}rmsyscfg -r lpar -m {system} -n {quoted}",
            )
        )
    return findings + await _vios_storage_residue(call, inputs, rows)


async def check_lpar_reconfigure_storage(
    call, inputs: LparPowerInputs
) -> list[Finding]:
    """ST70's run disk, its mappings and server adapters; lpar-config checks the
    partitions themselves."""
    rows = await _cli_rows(
        call,
        f"lssyscfg -r lpar -m {shlex.quote(inputs.system_name)} "
        "-F name,state,lpar_env,lpar_id",
        f"the partitions of {inputs.system_name}",
    )
    return await _vios_storage_residue(call, inputs, rows)


async def _vios_storage_residue(
    call, inputs: LparPowerInputs, rows: list[list[str]]
) -> list[Finding]:
    findings = []
    for vios, _, env, vios_id in (row for row in rows if len(row) == 4):
        if env == "vioserver":
            findings += await _lpar_power_vios_residue(call, inputs, vios)
            findings += await _lpar_power_volumes_left(call, inputs, vios, vios_id)
    return findings


async def _lpar_power_volumes_left(
    call, inputs: LparPowerInputs, vios: str, vios_id: str
) -> list[Finding]:
    """Run volumes still in the configured group, on the VIOS that lists the group."""
    status, groups = await call(
        "hmc_list_volume_groups",
        vios_name_or_uuid=vios,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(groups, list):
        raise StateUnreadable(f"the volume groups of VIOS {vios}")
    group = inputs.volume_group
    if not any(isinstance(g, dict) and g.get("name") == group for g in groups):
        return []
    command = f"viosvrcmd -m {inputs.system_name} --id {vios_id} -c 'lsvg -lv {group}'"
    if not _VOLUME_LISTING.fullmatch(command):
        raise StateUnreadable(
            f"the volumes of {group!r} (a name outside the read shape)"
        )
    status, listing = await call("hmc_run_command", cmd=command)
    names = storage_lifecycle.volume_names(listing) if status == "PASS" else None
    if names is None:
        raise StateUnreadable(f"the volumes of {group!r} on VIOS {vios}")
    system = shlex.quote(inputs.system_name)
    return [
        Finding(
            what=f"volume {name} on VIOS {vios}",
            detail=f"an {inputs.label} volume is still in volume group {group}",
            remedy=f'viosvrcmd -m {system} --id {vios_id} -c "rmlv -f {name}"',
        )
        for name in inputs.arm.scratch_volumes(names)
    ]


async def _lpar_power_vios_residue(
    call, inputs: LparPowerInputs, vios: str
) -> list[Finding]:
    system = shlex.quote(inputs.system_name)
    status, mappings = await call(
        "hmc_list_storage_mappings",
        vios_name_or_uuid=vios,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(mappings, list):
        raise StateUnreadable(f"the storage mappings of VIOS {vios}")
    findings = [
        Finding(
            what=f"mapping {entry.get('id')} on VIOS {vios}",
            detail=f"maps {inputs.label} volume {entry.get('backing_name')}",
            remedy=(
                f"viosvrcmd -m {system} -p {shlex.quote(vios)} -c "
                f'"rmvdev -vtd {str(entry.get("id")).rpartition("/")[2]}"'
                if entry.get("id")
                else f"viosvrcmd -m {system} -p {shlex.quote(vios)} -c 'lsmap -all' "
                "to find its virtual target device, then rmvdev -vtd <device>"
            ),
        )
        for entry in mappings
        if isinstance(entry, dict)
        and inputs.arm.scratch_volumes([str(entry.get("backing_name") or "")])
    ]
    adapters = await _cli_rows(
        call,
        scsi_adapter_listing(inputs.system_name, "lpar_names", vios),
        f"the vSCSI adapters of VIOS {vios}",
    )
    findings += [
        Finding(
            what=f"vSCSI server adapter {slot} on VIOS {vios}",
            detail=f"serves {inputs.label} partition {remote}",
            remedy=(
                f"chhwres -r virtualio --rsubtype scsi -m {system} -o r "
                f"-p {shlex.quote(vios)} -s {slot}"
            ),
        )
        for slot, remote, _ in (row for row in adapters if len(row) == 3)
        if inputs.arm.scratch_partitions([remote])
    ]
    return findings


def guard_read_only(tool: str, arguments: dict[str, Any]) -> None:
    """Refuse a call that could change the managed system."""
    if tool not in _READ_ONLY_TOOLS:
        raise MutatingCallRefused(f"{tool} is not on the read-only allowlist")
    if tool != "hmc_run_command":
        return
    command = str(arguments.get("cmd", "")).strip()
    if _VOLUME_LISTING.fullmatch(command):
        return
    if not command.startswith(_READ_ONLY_COMMAND_PREFIXES):
        raise MutatingCallRefused(
            "hmc_run_command is read-only only for lssyscfg and lshwres; "
            f"refused: {command.split()[0] if command else '(empty)'}"
        )
    if _SHELL_METACHARACTERS & set(command):
        raise MutatingCallRefused("hmc_run_command refused a shell metacharacter")


async def _read_slots(call, inputs: RecoveryInputs) -> list[dict[str, Any]]:
    """The system's dedicated slots, and the reachability probe for every check.

    This runs first and raises rather than returning empty. It proves the HMC is
    answering, but not that every later lookup will succeed: a later check still
    reads only the HMC's "no such partition" answer as absent, and any other
    failure as unreadable.
    """
    status, data = await call(
        "hmc_list_dedicated_pcie_slots", system_name_or_uuid=inputs.system_name
    )
    if status != "PASS" or not isinstance(data, dict):
        raise StateUnreadable(
            f"could not list dedicated slots on {inputs.system_name} ({status})"
        )
    return [item for item in (data.get("items") or []) if isinstance(item, dict)]


async def _surviving_fixture(call, inputs: RecoveryInputs) -> Finding | None:
    """Whether a partition this run created is still there.

    Ownership is confirmed by the run marker before reporting, so a partition
    that merely shares the name is never attributed to this run — the same rule
    the arm applies before it will delete anything.

    Only HSCL8012, the HMC's "no such partition" answer, reads as gone (see
    `partition_not_found`). Any other failure -- an authentication refusal, a
    lost connection, a different HSCL code -- or an answer that is not a
    description says nothing about whether the partition survives, so it raises
    rather than reporting the system clean. So does a description carrying this
    run's marker in a stamp this code cannot parse.
    """
    status, data = await call(
        "hmc_get_lpar_description",
        system_name_or_uuid=inputs.system_name,
        lpar_name_or_uuid=inputs.fixture_lpar,
    )
    if partition_not_found(status, data):
        return None
    if status != "PASS" or not isinstance(data, str):
        raise StateUnreadable(
            f"could not look up {inputs.fixture_lpar} on {inputs.system_name} "
            f"({status}): {getattr(data, 'message', data)!s}"
        )
    if parse_lpar_ownership_caller_token(data) != inputs.run_marker:
        # The marker is per-run random, so a description that names it came
        # from this run even when its stamp does not parse — typically a run on
        # pre-rename code, whose `[hmc-mcp ...]` stamp this code reads as unowned.
        if f"[caller {inputs.run_marker}]" in data:
            raise StateUnreadable(
                f"{inputs.fixture_lpar} on {inputs.system_name} carries this run's "
                f"marker {inputs.run_marker} in a stamp this checkout cannot read; "
                "re-run the check from the run's tested commit"
            )
        return None
    state_status, state = await call(
        "hmc_get_lpar_state",
        system_name_or_uuid=inputs.system_name,
        lpar_name_or_uuid=inputs.fixture_lpar,
    )
    if state_status != "PASS" or not isinstance(state, str):
        raise StateUnreadable(
            f"could not read the state of {inputs.fixture_lpar} on "
            f"{inputs.system_name} ({state_status}), so its remedy cannot be chosen"
        )
    remedy = f"hmc rmsyscfg -m {inputs.system_name} -r lpar -n {inputs.fixture_lpar}"
    # REST's PartitionState is lower case ("not activated"); only the CLI title-cases it.
    if state.strip().lower() != NOT_ACTIVATED.lower():
        # The HMC refuses to delete a partition outside Not Activated (#950): a
        # run interrupted while its fixture is running needs a shutdown first.
        remedy = (
            f"hmc chsysstate -m {inputs.system_name} -r lpar -n {inputs.fixture_lpar} "
            f"-o shutdown --immed; once it is {NOT_ACTIVATED}, {remedy}"
        )
    return Finding(
        "surviving partition",
        f"{inputs.fixture_lpar} on {inputs.system_name} still exists in state "
        f"{state!r} and carries this run's marker {inputs.run_marker}",
        remedy,
    )


def _stranded_slot(
    slots: list[dict[str, Any]], inputs: RecoveryInputs
) -> Finding | None:
    """Whether the dedicated slot is still owned rather than back in the pool."""
    if inputs.drc_index is None:
        return None
    for item in slots:
        if item.get("drc_index") != inputs.drc_index:
            continue
        owner = (item.get("owner_lpar") or "").strip()
        if not owner or owner == "null":
            return None
        return Finding(
            "stranded slot",
            f"dedicated slot {inputs.drc_index} on {inputs.system_name} is owned "
            f"by {owner}; the run left it assigned",
            f"hmc chsyscfg -m {inputs.system_name} -r prof -i "
            f'"name={inputs.profile_name},lpar_name={owner},'
            f'io_slots-={inputs.drc_index}//0"',
        )
    return None


async def _profile_drift(call, inputs: RecoveryInputs) -> Finding | None:
    """Whether the fixture profile's io_slots still differs from its baseline."""
    if inputs.baseline_io_slots is None or inputs.drc_index is None:
        return None
    status, data = await call(
        "hmc_run_command",
        cmd=profile_io_slot_rows_command(inputs.system_name),
    )
    if status != "PASS" or not isinstance(data, str):
        raise StateUnreadable(
            f"could not read profile io_slots for {inputs.fixture_lpar} ({status})"
        )
    try:
        observed = select_profile_io_slots(
            data, inputs.fixture_lpar, inputs.profile_name
        )
    except HMCCLIError as error:
        raise StateUnreadable(
            f"profile io_slots for {inputs.fixture_lpar} is unreadable: {error}"
        ) from error
    if observed == inputs.baseline_io_slots:
        return None
    still_assigned = _io_slots_contains(observed, inputs.drc_index)
    return Finding(
        "profile drift",
        f"profile io_slots for {inputs.fixture_lpar} is {observed!r}, not the "
        f"captured baseline {inputs.baseline_io_slots!r}"
        + (f"; slot {inputs.drc_index} is still listed" if still_assigned else ""),
        f"hmc chsyscfg -m {inputs.system_name} -r prof -i "
        f'"name={inputs.profile_name},lpar_name={inputs.fixture_lpar},'
        f'io_slots={inputs.baseline_io_slots}"',
    )


async def check(call, inputs: RecoveryInputs) -> list[Finding]:
    """Every stranded condition, in the order an operator should clear them."""
    slots = await _read_slots(call, inputs)
    stranded = _stranded_slot(slots, inputs)
    try:
        fixture = await _surviving_fixture(call, inputs)
    except StateUnreadable as unreadable:
        unreadable.findings = [stranded] if stranded else []
        raise
    findings = [finding for finding in (fixture, stranded) if finding]
    if fixture is None:
        # A profile belongs to its partition. With the fixture gone there is no
        # profile left to have drifted, and asking for one answers HSCL8012 —
        # which would otherwise be read as an unreadable system and exit 2 on
        # every clean run.
        return findings
    try:
        drift = await _profile_drift(call, inputs)
    except StateUnreadable as unreadable:
        unreadable.findings = findings
        raise
    return findings + [drift] if drift else findings


# ---------------------------------------------------------------------------
# The configured test partition (vMedia arm, and round2's ST14 provision)
# ---------------------------------------------------------------------------


def dispatched_subtasks(document: Any) -> list[int] | None:
    """The subtasks the run dispatched, or `None` when the document does not say."""
    run = document.get("run") if isinstance(document, dict) else None
    subtasks = run.get("subtasks") if isinstance(run, dict) else None
    if not isinstance(subtasks, list) or any(type(n) is not int for n in subtasks):
        return None
    return subtasks


@dataclass(frozen=True)
class Round2Inputs:
    """Fresh pre-mutation baseline and identity for the complete round2 dispatch."""

    snapshot: dict[str, Any]
    hmc: dict[str, Any]
    lpar: str
    description: str
    compatibility: dict[str, Any]


def round2_inputs_from_document(
    document: dict[str, Any], subtasks: list[int]
) -> Round2Inputs | None:
    if document.get("run", {}).get("group") != "round2":
        return None
    if subtasks != runner.SUBTASK_GROUPS["round2"]:
        raise ValueError("round2 dispatch does not match the complete named arm")
    config = live_config.decode_saved_config(document.get("config"))
    artifacts = document.get("artifacts")
    saved = artifacts.get("lp3_baseline") if isinstance(artifacts, dict) else None
    if not isinstance(artifacts, dict) or not isinstance(saved, dict):
        raise TypeError("round2 has no producer baseline")
    captured = independent_baseline.validate(saved.get("round2_snapshot"))
    hmc = document.get("hmc")
    if (
        not isinstance(hmc, dict)
        or captured["scope"]["host"] != hmc.get("host")
        or captured["scope"]["system"] != config.system_name
    ):
        raise ValueError("round2 snapshot scope does not match its run")
    if captured["scope"]["vios_ids"] != [artifacts.get("vios_partition_id")]:
        raise ValueError("round2 snapshot omits its serving VIOS")
    description, compatibility = saved.get("description"), saved.get("proc_compat")
    if (
        not isinstance(description, str)
        or not isinstance(compatibility, dict)
        or not compatibility.get("profile")
        or not compatibility.get("profile_mode")
    ):
        raise ValueError("round2 description/profile baseline is incomplete")
    return Round2Inputs(captured, hmc, config.lp3_name, description, compatibility)


async def check_round2(call, inputs: Round2Inputs) -> list[Finding]:
    config = HMCConfig()
    if runner._hmc_identity(config) != inputs.hmc:
        raise StateUnreadable("current HMC identity does not match round2 results")
    scope = inputs.snapshot["scope"]
    # This installed capture interface performs only REST GETs and listing SSH commands.
    now = independent_baseline.validate(
        await independent_baseline.capture(config, scope["system"], scope["vios_ids"])
    )
    findings = []
    if now != inputs.snapshot:
        findings.append(
            Finding(
                "round2 baseline drift",
                "independent system/VIOS tables differ",
                "inspect the retained baseline and current system; restore manually",
            )
        )
    for tool, expected in (
        ("hmc_get_lpar_description", inputs.description),
        ("hmc_get_lpar_proc_compat", inputs.compatibility),
    ):
        status, data = await call(
            tool, system_name_or_uuid=scope["system"], lpar_name_or_uuid=inputs.lpar
        )

        data = plain_data(data)
        if status != "PASS" or type(data) is not type(expected):
            raise StateUnreadable("round2 property read is incomplete", findings)
        if tool == "hmc_get_lpar_description":
            data = data.removesuffix("\n").removesuffix("\r")
        if data != expected:
            findings.append(
                Finding(
                    "round2 property drift",
                    f"{tool} differs from baseline",
                    "restore the property from the retained private baseline",
                )
            )
    return findings


async def check_profile_artifacts(call, system_name: str) -> list[Finding]:
    """Independently list reserved-prefix residue without deleting or restoring."""
    if not isinstance(system_name, str) or not system_name.strip():
        raise StateUnreadable("the profile-artifacts managed-system identity")
    status, listing = await call(
        "hmc_list_backup_lpar_profiles", system_name_or_uuid=system_name
    )
    names = backup_artifact_names(listing) if status == "PASS" else None
    if names is None:
        raise StateUnreadable("the complete profile-backup inventory")
    return [
        Finding(
            f"profile backup {name}",
            "an artifact-arm scratch backup remains; no automatic cleanup attempted",
            "inspect the retained run and selected system, then explicitly delete only the exact disposable backup after operator approval",
        )
        for name in sorted(names)
        if name.startswith(ARTIFACT_PREFIX)
    ]


async def check_run(
    call,
    checks: tuple[tuple[Callable[..., Awaitable[list[Finding]]], object], ...],
    *,
    users: bool = False,
) -> list[Finding]:
    """Each arm's checks in turn, each read whatever the others found."""
    findings: list[Finding] = []
    unread: list[str] = []
    if users:
        try:
            findings += await _scratch_users_left(call)
        except StateUnreadable as unreadable:
            unread.append(str(unreadable))
    for run_checks, inputs in checks:
        try:
            findings += await run_checks(call, inputs)
        except StateUnreadable as unreadable:
            findings += unreadable.findings
            unread.append(str(unreadable))
    if unread:
        raise StateUnreadable("; ".join(unread), findings)
    return findings


def _read_only_caller(client, state: runner.RunState):
    """A call path that refuses anything off the read-only surface."""

    async def call(tool: str, **arguments: Any) -> tuple[str, Any]:
        guard_read_only(tool, arguments)
        return await state.call(client, tool, **arguments)

    return call


async def _run_checks(
    checks: tuple[tuple[Callable[..., Awaitable[list[Finding]]], object], ...],
    *,
    users: bool = False,
) -> list[Finding]:
    state = runner.RunState(config=live_config.LiveTestConfig())
    # The checks need the live run's composition, not bare ``create_mcp``:
    # ``hmc_run_command`` is an opt-in escape hatch that only it registers. Without
    # it the profile-drift check was answered "Unknown tool", which read as "no
    # drift" before ``_profile_drift`` raised, so the script reported a system
    # clean it had never looked at. The 2026-09-21 live run showed review alone had
    # missed that, so a test asserts the read-only tools are registered here.
    async with runner.served_client() as client:
        return await check_run(
            _read_only_caller(client, state),
            checks,
            users=users,
        )


def _is_partial(document: dict[str, Any]) -> bool:
    """Whether the run was interrupted; anything but an absent key or `false` is."""
    run = document.get("run")
    return isinstance(run, dict) and run.get("partial", False) is not False


def _report(
    document: dict[str, Any],
    findings: list[Finding],
    unwitnessed: list[int],
    unread: list[str],
) -> None:
    run = value if isinstance(value := document.get("run"), dict) else {}
    artifacts = document.get("artifacts")
    partial = _is_partial(document)
    print(
        f"{'PARTIAL ' if partial else ''}recovery check for the "
        f"{run.get('group') or '(no group)'} run at "
        f"{run.get('tested_commit') or '(no commit)'}, "
        f"{'interrupted' if partial else 'finished'} "
        f"{run.get('finished') or '(unknown)'}"
    )
    marker = artifacts.get("pcie_run_marker") if isinstance(artifacts, dict) else None
    if marker:
        print(f"PCIe run marker {marker}")
    print("=" * 60)
    for finding in findings:
        print(f"STRANDED  {finding.what}")
        print(f"          {finding.detail}")
        print(f"  clear with:  {finding.remedy}")
    for message in unread:
        print(f"NOT READ  {message}")
    if unwitnessed:
        print(
            f"NOT WITNESSED  subtasks {', '.join(map(str, unwitnessed))}: check what "
            "they change by hand (docs/live-testing.md, step 4)"
        )
    if findings:
        print(
            "\nThis script issues no mutating call. Run the commands above "
            "yourself, then re-run this check."
        )
    if partial:
        print(
            "PARTIAL  the run was interrupted: the call in flight has no row, so "
            "inspect its partial evidence and independent checks (docs/live-testing.md)"
        )
    elif not findings and not unwitnessed and not unread:
        print("CLEAN  nothing this run dispatched was left behind")


def _paging_recovery(path: Path) -> int:
    from live_test import paging_switchover

    def unique_members(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Paging recovery JSON has duplicate members")
            result[key] = value
        return result

    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Paging recovery requires a regular results file")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Paging recovery results exceed 1 MiB")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_members)
        if not runner._bootstrap_config():
            raise ValueError("Paging recovery credentials unavailable")
        result = asyncio.run(paging_switchover.recover_document(HMCConfig(), document))
    except Exception as error:  # noqa: BLE001 - uncertain evidence never confirms recovery
        print(f"paging recovery unconfirmed: {type(error).__name__}", file=sys.stderr)
        return 2
    print(
        f"PAGING {result['status']}: read-only original-baseline comparison; historical forward result unchanged"
    )
    return {"clean": 0, "drift": 1, "unverified": 2}[result["status"]]


def _statistics_recovery(path: Path) -> int:
    from live_test import memory_statistics

    def unique_members(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Statistics recovery JSON has duplicate members")
            result[key] = value
        return result

    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Statistics recovery requires a regular results file")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Statistics recovery raw results exceed 1 MiB")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_members)
        if (
            not isinstance(document, dict)
            or document.get("run", {}).get("group") != memory_statistics.GROUP
        ):
            raise ValueError("Statistics recovery requires its named run")
        if not runner._bootstrap_config():
            raise ValueError("Statistics recovery credentials unavailable")
        asyncio.run(memory_statistics.recover_document(HMCConfig(), document))
    except Exception as error:  # noqa: BLE001 — malformed/uncertain evidence cannot confirm recovery
        print(
            f"statistics recovery unconfirmed: {type(error).__name__}", file=sys.stderr
        )
        return 2
    print(
        "OBSERVED: finite reset-attempt record and current environment confirmed; reset verification unavailable"
    )
    return 0


def _recovery_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=Path("test-results-dedicated.json"),
        help="the run's results document (default: %(default)s)",
    )
    source_packets = parser.add_mutually_exclusive_group()
    source_packets.add_argument(
        "--paging-switchover",
        action="store_true",
        help="ST66 bounded read-only original-baseline recovery",
    )
    source_packets.add_argument("--source-packet", type=Path)
    source_packets.add_argument("--activation-source-packet", type=Path)
    source_packets.add_argument("--serial-procedure", action="store_true")
    source_packets.add_argument("--boot-start-procedure", action="store_true")
    source_packets.add_argument("--ethernet-update-procedure", action="store_true")
    source_packets.add_argument(
        "--memory-statistics",
        action="store_true",
        help="ST67 bounded raw-record recovery; never restores counter history",
    )
    source_packets.add_argument("--vios-ame-source-packet", type=Path)
    source_packets.add_argument("--vios-boot-source-packet", type=Path)
    source_packets.add_argument("--vnic-creation-policy-packet", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--packet-sha256")
    return parser


def _delegate_procedure_recovery(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> int | None:
    if args.ethernet_update_procedure:
        from live_test import ethernet_update

        if not args.output or args.packet_sha256:
            parser.error(
                "ST69 recovery requires original results and a fresh private output"
            )
        return ethernet_update.main(
            ["recover", "--results", str(args.results), "--output", str(args.output)]
        )
    if args.paging_switchover:
        if args.output or args.packet_sha256:
            parser.error("ST66 recovery uses retained results only")
        return _paging_recovery(args.results)
    if args.memory_statistics:
        if args.output or args.packet_sha256:
            parser.error("ST67 recovery uses retained results only")
        return _statistics_recovery(args.results)
    for selected, module_name, refusal in (
        (
            args.boot_start_procedure,
            "lpar_boot_start",
            "ST68 recovery derives its binding from original private results only",
        ),
        (
            args.serial_procedure,
            "lpar_serial_adapter",
            "ST63 recovery uses retained results only",
        ),
    ):
        if selected:
            module = import_module(f"live_test.{module_name}")
            if args.output or args.packet_sha256:
                parser.error(refusal)
            return module.main(["recover", "--results", str(args.results)])
    return None


def _delegate_packet_recovery(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> int | None:
    for packet, module_name, refusal in (
        (
            args.vnic_creation_policy_packet,
            "vnic_creation_policy",
            "vNIC policy recovery requires packet digest and private output",
        ),
        (
            args.vios_ame_source_packet,
            "vios_ame_source",
            "VIOS AME source recovery requires packet digest and private output",
        ),
        (
            args.vios_boot_source_packet,
            "vios_boot_source",
            "VIOS boot source recovery requires packet digest and private output",
        ),
        (
            args.activation_source_packet,
            "system_profile_activation_source",
            "activation source recovery requires packet digest and private output",
        ),
    ):
        if packet:
            module = import_module(f"live_test.{module_name}")
            if not args.packet_sha256 or not args.output:
                parser.error(refusal)
            return module.main(
                [
                    "recover",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    args.packet_sha256,
                    "--results",
                    str(args.results),
                    "--output",
                    str(args.output),
                ]
            )
    if args.output:
        parser.error("--output requires --activation-source-packet")
    if args.source_packet:
        from live_test import system_profile_source

        if not args.packet_sha256:
            parser.error("source recovery requires --packet-sha256")
        return system_profile_source.main(
            [
                "recover",
                "--packet",
                str(args.source_packet),
                "--packet-sha256",
                args.packet_sha256,
                "--results",
                str(args.results),
            ]
        )
    if args.packet_sha256:
        parser.error("--packet-sha256 requires --source-packet")
    return None


def _recover_document_group(document: dict[str, Any]) -> int | None:
    run = document.get("run")
    group = run.get("group") if isinstance(run, dict) else None
    if group == "lpar-memory-statistics":
        print(
            "statistics recovery requires --memory-statistics for bounded raw admission",
            file=sys.stderr,
        )
        return 2
    protocols = {
        "partition-profiles": (
            "partition_profiles",
            "partition-profiles recovery unreadable",
            "partition-profiles recovery",
            "CLEAN: partition-profile original baseline and terminal sequence confirmed",
        ),
        "partition-profile-contract": (
            "partition_profile_contract",
            "profile contract recovery",
            "profile contract recovery",
            "CLEAN: independent original baseline and fixture absence confirmed",
        ),
        "system-profiles": (
            "system_profiles",
            "system-profiles recovery unreadable",
            "system-profiles recovery",
            "CLEAN: system-profile original baseline and finite terminal sequence confirmed",
        ),
        "lpar-memory-move": (
            "memory_move",
            "memory-move recovery",
            "memory-move recovery",
            "CLEAN: finite memory-move success and both original baselines confirmed; no writes",
        ),
        "lpar-accelerator-qos": (
            "accelerator_qos",
            "accelerator-qos recovery",
            "accelerator-qos recovery",
            "CLEAN: finite accelerator-qos proof and original baselines confirmed; no writes",
        ),
        "lpar-processor-move": (
            "processor_move",
            "processor-move recovery",
            "processor-move recovery",
            "CLEAN: finite processor-move success and both original baselines confirmed; no writes",
        ),
    }
    # Unknown or malformed group values must continue to generic witness admission.
    protocol = protocols.get(group) if isinstance(group, str) else None
    if protocol is None:
        return None
    module_name, credentials_label, error_label, success = protocol
    module = import_module(f"live_test.{module_name}")
    if not runner._bootstrap_config():
        print(f"{credentials_label}: credentials unavailable", file=sys.stderr)
        return 2
    try:
        asyncio.run(module.recover_document(HMCConfig(), document))
    except Exception as error:  # noqa: BLE001 — incomplete evidence never attests CLEAN
        print(f"{error_label} unconfirmed: {type(error).__name__}", file=sys.stderr)
        return 2
    print(success)
    return 0


def _recover_generic_document(document: dict[str, Any], results_path: Path) -> int:
    subtasks = dispatched_subtasks(document)
    if subtasks is None:
        print(
            f"ERROR: {results_path} records no run.subtasks, so what the run "
            "changed is unknown; check the system by hand",
            file=sys.stderr,
        )
        return 2
    round2_inputs = None
    round2_error = None
    try:
        round2_inputs = round2_inputs_from_document(document, subtasks)
    except (ValueError, TypeError, KeyError) as error:
        round2_error = str(error)
    memory_inputs = None
    memory_error = None
    try:
        skip_reason = creation_memory_skip_reason(document, subtasks)
        if skip_reason:
            print(f"SKIPPED  ST65 no fixture work: {skip_reason}")
            print(
                "Saved unarmed result only; not baseline, configured-value or runtime proof."
            )
            return 0
        memory_inputs = creation_memory_inputs_from_document(document, subtasks)
    except (ValueError, TypeError, KeyError) as error:
        memory_error = str(error)
    creation_inputs = None
    creation_error = None
    try:
        skip_reason = creation_memory_layout_skip_reason(document, subtasks)
        if skip_reason:
            print(f"SKIPPED  ST58 no fixture work: {skip_reason}")
            print(
                "Saved unarmed result only; not baseline, configured-value or runtime proof."
            )
            return 0
        creation_inputs = creation_memory_layout_inputs_from_document(
            document, subtasks
        )
    except (ValueError, TypeError, KeyError) as error:
        creation_error = str(error)
    witnessed = _WITNESSED_SUBTASKS | (
        set(runner.SUBTASK_GROUPS["round2"]) if round2_inputs else set()
    )
    unwitnessed = sorted(set(subtasks) - witnessed)
    pcie = inputs_from_document(document)
    partition = lpar_inputs_from_document(document, subtasks)
    vios = vios_backup_inputs_from_document(document, subtasks)
    network_inputs = network_inputs_from_document(document, subtasks)
    try:
        lpar_config_inputs = lpar_config_inputs_from_document(document, subtasks)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    lpar_power_inputs = lpar_power_inputs_from_document(document, subtasks)
    boot_media = provision_boot_media_inputs_from_document(document, subtasks)
    reconfigure_inputs = lpar_reconfigure_inputs_from_document(document, subtasks)

    artifact_system = None
    if 43 in subtasks:
        artifact_config = document.get("config")
        artifact_system = ""
        if isinstance(artifact_config, dict):
            artifact_system = artifact_config.get("system_name") or ""
    checks = tuple(
        (checker, inputs)
        for checker, inputs in (
            (check, pcie),
            (check_test_partition, partition),
            (check_vios_backup, vios),
            (check_network, network_inputs),
            (check_lpar_config, lpar_config_inputs),
            (check_lpar_power, lpar_power_inputs),
            (check_provision_boot_media, boot_media),
            (check_lpar_reconfigure_storage, reconfigure_inputs),
            (check_creation_memory, memory_inputs),
            (check_creation_memory_layout, creation_inputs),
            (check_round2, round2_inputs),
            (check_profile_artifacts, artifact_system),
        )
        if inputs is not None
    )

    findings: list[Finding] = []
    unread: list[str] = [
        error for error in (round2_error, creation_error, memory_error) if error
    ]
    try:
        users = users_witnessed(document, subtasks)
    except ValueError as error:
        users = False
        unread.append(str(error))
    if users or checks:
        if not runner._bootstrap_config():
            print("ERROR: no HMC credentials; cannot check the system", file=sys.stderr)
            return 2
        try:
            findings = asyncio.run(_run_checks(checks, users=users))
        except MutatingCallRefused as refused:
            print(f"ERROR: refused a mutating call: {refused}", file=sys.stderr)
            return 2
        except StateUnreadable as unreadable:
            findings = unreadable.findings
            unread.append(str(unreadable))
        except Exception as error:  # noqa: BLE001 - an unreadable system is not a clean one
            print(f"ERROR: could not read the managed system: {error}", file=sys.stderr)
            return 2

    _report(document, findings, unwitnessed, unread)
    if unread or unwitnessed or _is_partial(document):
        print("The system was NOT confirmed clean.", file=sys.stderr)
        return 2
    return 1 if findings else 0


def main(argv: list[str] | None = None) -> int:
    parser = _recovery_parser()
    args = parser.parse_args(argv)
    for delegate in (_delegate_procedure_recovery, _delegate_packet_recovery):
        result = delegate(args, parser)
        if result is not None:
            return result
    try:
        document = json.loads(args.results.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"ERROR: cannot read {args.results}: {error}", file=sys.stderr)
        return 2
    if document.get("group") == "client-ethernet-update":
        parser.error(
            "ST69 requires --ethernet-update-procedure and --output; generic recovery cannot handle it"
        )
    result = _recover_document_group(document)
    return (
        result
        if result is not None
        else _recover_generic_document(document, args.results)
    )


if __name__ == "__main__":
    raise SystemExit(main())
