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
vSCSI adapter serving a `hmcpctl-live-pwr-` partition. Complete round2 runs carry
an ST0 snapshot: recovery independently recaptures system and VIOS tables and
reads the original partition's description/profile compatibility. Missing witness
data or scope mismatches fail closed. Every other
subtask the run dispatched is listed as NOT WITNESSED, to be checked by hand
(docs/live-testing.md, step 4).

Exit 0 means every dispatched subtask was witnessed and nothing is stranded.
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
import re
import shlex
import sys
from collections.abc import Awaitable, Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

import live_test_baseline as independent_baseline
import live_test_runner as runner
from live_test import (
    lpar_config,
    lpar_power,
    network,
    storage_lifecycle,
    storage_volume_group,
    vios_backup,
)
from live_test.pcie import (
    _DEFAULT_DEDICATED_PROFILE,
    _io_slots_contains,
    partition_not_found,
    select_profile_io_slots,
)
from live_test.results import entries
from live_test.users import profile_rows, scratch_users
from live_test.vmedia import (
    _BOOT_BASELINE_STEP,
    _mapping_identity,
    is_run_media_name,
    scsi_adapter_listing,
)

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
        "hmc_list_virtual_networks",
        "hmc_list_adapters",
        "hmc_list_vios_fc_port_labels",
        "hmc_list_vios_vfc_group_labels",
    }
)

#: The HMC refuses `rmsyscfg` on a partition outside this state; a survivor
#: found in any other state needs a shutdown first (#950).
_NOT_ACTIVATED = "Not Activated"

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

#: The vMedia arm (`SUBTASK_GROUPS["vmedia"]`) and the PCIe arms the marker
#: checks cover. Any other dispatched subtask is reported as not witnessed.
_VMEDIA_SUBTASKS = frozenset(range(16, 23))
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
    _VMEDIA_SUBTASKS
    | _STORAGE_SUBTASKS
    | {
        _USERS_SUBTASK,
        24,
        25,
        vios_backup.SUBTASK,
        network.INVENTORY_SUBTASK,
        network.SUBTASK,
        lpar_config.SUBTASK,
        lpar_power.SUBTASK,
    }
)

#: The vMedia calls that make the repository the run's own. Media calls are not
#: among them: the arm creates and removes its own media inside a repository it
#: did not create (#1347), and those are read by name instead.
_REPOSITORY_TOOLS = frozenset(
    {"hmc_create_media_repository", "hmc_delete_media_repository"}
)

#: Every mutating call the vMedia arm makes, each covered by a class below:
#: power-off is the arm's own end state, and mount/unmount residue is read
#: whenever the arm ran. A test pins this against the arm's source.
_VMEDIA_MUTATIONS = _REPOSITORY_TOOLS | {
    "hmc_upload_iso",
    "hmc_create_optical_media",
    "hmc_delete_optical_media",
    "hmc_power_on_lpar",
    "hmc_power_off_lpar",
    "hmc_set_lpar_boot_order",
    "hmc_mount_optical_media",
    "hmc_unmount_optical_media",
}


class MutatingCallRefused(RuntimeError):
    """Raised when a call would leave the read-only surface."""


class StateUnreadable(RuntimeError):
    """Raised when a check could not read the state it is there to judge.

    Distinct from "nothing stranded". A check that cannot see the system has
    not found it clean, and the caller turns this into exit 2 rather than the
    exit 0 that a returned empty list would mean.

    It carries the findings already confirmed so an operator still sees what
    was found before the read failed.
    """

    def __init__(self, message: str, findings: list[Finding] | None = None) -> None:
        super().__init__(message)
        self.findings: list[Finding] = findings or []


@dataclass(frozen=True)
class RecoveryInputs:
    """What one run created, as recorded in its results document."""

    system_name: str
    run_marker: str
    fixture_lpar: str
    drc_index: str | None
    baseline_io_slots: str | None
    profile_name: str


@dataclass(frozen=True)
class Finding:
    """One thing a run left behind, and the command that clears it."""

    what: str
    detail: str
    remedy: str


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
    if not (isinstance(artifacts, dict) and "test_user_name" in artifacts) and _calls(
        document, {_USERS_SUBTASK}, {"hmc_create_user"}
    ):
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
    """The run's system when it dispatched ST39, else `None`.

    The reserved name prefix is the identity, as the users arm's is: a run
    interrupted before it wrote its document still has its partition found.
    """
    config = document.get("config")
    system = config.get("system_name") if isinstance(config, dict) else None
    if lpar_config.SUBTASK not in subtasks or not system:
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
            if states[name] == _NOT_ACTIVATED
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
    """The system the lpar-power arm (ST41) created its partitions and volume on."""

    system_name: str
    volume_group: str = ""


def lpar_power_inputs_from_document(
    document: dict[str, Any], subtasks: list[int]
) -> LparPowerInputs | None:
    """The run's system when it dispatched ST41, else `None`; found by prefix."""
    config = document.get("config")
    system = config.get("system_name") if isinstance(config, dict) else None
    if lpar_power.SUBTASK not in subtasks or not system:
        return None
    group = config.get("vdisk_volume_group_name") if isinstance(config, dict) else ""
    return LparPowerInputs(str(system), str(group or ""))


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
            if state == _NOT_ACTIVATED
            else f"chsysstate -m {system} -r lpar -n {quoted} -o shutdown --immed; "
        )
        findings.append(
            Finding(
                what=f"partition {name}",
                detail=f"an lpar-power partition is still defined ({state})",
                remedy=f"{shutdown}rmsyscfg -r lpar -m {system} -n {quoted}",
            )
        )
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
    names = lpar_power._volume_names(listing) if status == "PASS" else None
    if names is None:
        raise StateUnreadable(f"the volumes of {group!r} on VIOS {vios}")
    system = shlex.quote(inputs.system_name)
    return [
        Finding(
            what=f"volume {name} on VIOS {vios}",
            detail=f"an lpar-power volume is still in volume group {group}",
            remedy=f'viosvrcmd -m {system} --id {vios_id} -c "rmlv -f {name}"',
        )
        for name in lpar_power.scratch_volumes(names)
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
            detail=f"maps lpar-power volume {entry.get('backing_name')}",
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
        and lpar_power.scratch_volumes([str(entry.get("backing_name") or "")])
    ]
    adapters = await _cli_rows(
        call,
        scsi_adapter_listing(inputs.system_name, "lpar_names", vios),
        f"the vSCSI adapters of VIOS {vios}",
    )
    findings += [
        Finding(
            what=f"vSCSI server adapter {slot} on VIOS {vios}",
            detail=f"serves lpar-power partition {remote}",
            remedy=(
                f"chhwres -r virtualio --rsubtype scsi -m {system} -o r "
                f"-p {shlex.quote(vios)} -s {slot}"
            ),
        )
        for slot, remote, _ in (row for row in adapters if len(row) == 3)
        if lpar_power.scratch_partitions([remote])
    ]
    return findings


@dataclass(frozen=True)
class VIOSBackupInputs:
    """What the vios-backup arm (ST37) recorded before its backup."""

    system_name: str
    lpar_name: str
    vios: str
    vios_uuid: str
    backup_name: str
    mapping_id: str
    backing: str
    #: The arm kept its backup because it could not prove the VIOS at baseline.
    off_baseline: bool = False


def vios_backup_inputs_from_document(
    document: Any, subtasks: list[int]
) -> VIOSBackupInputs | None:
    """ST37's inputs, or `None` when it did not run far enough to change anything.

    The arm records them before its backup, so their absence means it stopped at
    its preconditions or baseline, which change nothing.
    """
    if vios_backup.SUBTASK not in subtasks or not isinstance(document, dict):
        return None
    config, artifacts = document.get("config"), document.get("artifacts")
    if not isinstance(config, dict) or not isinstance(artifacts, dict):
        return None
    values = (
        config.get("system_name"),
        config.get("lp3_name"),
        artifacts.get("vios_backup_vios"),
        artifacts.get("vios_backup_vios_uuid"),
        artifacts.get("vios_backup_name"),
        artifacts.get("vios_backup_mapping"),
        artifacts.get("vios_backup_backing"),
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    rows = document.get("results")
    off_baseline = isinstance(rows, list) and any(
        isinstance(row, dict) and row.get("tool") == vios_backup.KEPT_ROW
        for row in rows
    )
    return VIOSBackupInputs(*values, off_baseline=off_baseline)


async def check_vios_backup(call, inputs: VIOSBackupInputs) -> list[Finding]:
    """A backup ST37 kept, and a disk mapping it did not put back."""
    parts = inputs.mapping_id.split("/")
    if len(parts) != 2 or not all(
        vios_backup._DEVICE_NAME.fullmatch(part) for part in [*parts, inputs.backing]
    ):
        raise StateUnreadable(
            f"the document's disk mapping {inputs.mapping_id!r} is not plain device names"
        )
    findings: list[Finding] = []
    # By UUID: a bare VIOS name is resolved across every managed system.
    status, data = await call(
        "hmc_list_vios_backups", vios_name_or_uuid=inputs.vios_uuid
    )
    if status != "PASS" or not isinstance(data, list):
        raise StateUnreadable(f"could not list the backups of {inputs.vios} ({status})")
    # Containment, not equality: the catalog may render the name with a prefix or
    # suffix, and a projection that did is no reason to report the backup gone.
    # The catalog lists the backup under its own name (`<name>.tar.gz` on V10R3),
    # which is the one name `rstviosbk` and `rmviosbk` accept.
    names = [
        str(row.get("name"))
        for row in data
        if isinstance(row, dict) and inputs.backup_name in str(row.get("name", ""))
    ]
    listed = bool(names)
    catalog = names[0] if names else inputs.backup_name
    remove = vios_backup.rmviosbk_command(inputs.system_name, inputs.vios, catalog)
    if inputs.off_baseline:
        findings.append(
            Finding(
                "VIOS off baseline, backup kept"
                if listed
                else "VIOS off baseline, backup gone",
                f"the run could not prove {inputs.vios} back at its baseline and "
                "kept the backup; check the VIOS through its HMC console",
                f"rstviosbk -t viosioconfig -m {shlex.quote(inputs.system_name)} "
                f"-p {shlex.quote(inputs.vios)} -f {shlex.quote(catalog)} "
                f"-r, re-check the baseline, then {remove}"
                if listed
                else "restore the I/O configuration by hand from the run's baseline rows",
            )
        )
    elif listed:
        findings.append(
            Finding(
                "VIOS backup left",
                f"{catalog} is still in the backup catalog of {inputs.vios}",
                remove,
            )
        )
    status, data = await call(
        "hmc_list_storage_mappings",
        vios_name_or_uuid=inputs.vios_uuid,
        lpar_name_or_uuid=inputs.lpar_name,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(data, list):
        raise StateUnreadable(
            f"could not list the mappings of {inputs.vios} ({status})", findings
        )
    if not any(
        isinstance(row, dict)
        and row.get("id") == inputs.mapping_id
        and row.get("backing_name") == inputs.backing
        for row in data
    ):
        adapter, vtd = parts
        findings.append(
            Finding(
                "disk mapping missing",
                f"{inputs.mapping_id} on {inputs.vios} no longer maps {inputs.backing} "
                f"to {inputs.lpar_name}",
                vios_backup.vios_command(
                    inputs.system_name,
                    inputs.vios,
                    vios_backup.mkvdev_command(adapter, inputs.backing, vtd),
                ),
            )
        )
    return findings


@dataclass(frozen=True)
class NetworkInputs:
    """What the network arm (ST9) read before changing anything."""

    system_name: str
    lpar_name: str
    #: The run's test VLAN, when its VLAN round trip reached the create.
    vlan: int | None
    #: Adapter type -> its baseline listing on the test partition.
    adapters: dict[str, Any]
    #: The serving VIOS's FC-port label rows, when that round trip read them.
    fc_labels: list[Any] | None
    #: Whether the vFC group-label round trip read its baseline.
    groups_read: bool = False


def _baseline_row(rows: list[Any], tool: str) -> Any:
    """The data of the ST9 row recording *tool* (``<tool> (<label>)``), or None."""
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("subtask") == network.SUBTASK
            and row.get("tool") == tool
            and row.get("status") == "PASS"
        ):
            return row.get("data")
    return None


def network_inputs_from_document(
    document: Any, subtasks: list[int]
) -> NetworkInputs | None:
    """ST9's baselines, or `None` when it changed nothing (it SKIPped or never ran)."""
    if network.SUBTASK not in subtasks or not isinstance(document, dict):
        return None
    config, artifacts = document.get("config"), document.get("artifacts")
    rows = document.get("results")
    if not (
        isinstance(config, dict)
        and isinstance(artifacts, dict)
        and isinstance(rows, list)
    ):
        return None
    adapters = {
        kind: data
        for kind in network.ROUND_TRIP_ADAPTER_TYPES
        if (data := _baseline_row(rows, f"hmc_list_adapters ({kind}, baseline)"))
        is not None
    }
    # Any create row, whatever its status: a refused create can still have applied.
    created = any(
        isinstance(row, dict)
        and row.get("subtask") == network.SUBTASK
        and row.get("tool") == "hmc_create_virtual_network (create)"
        for row in rows
    )
    fc_labels = _baseline_row(
        rows, "hmc_list_vios_fc_port_labels (FC-port labels, baseline)"
    )
    vlan = artifacts.get("test_vlan_id") if created else None
    groups_read = (
        _baseline_row(
            rows, "hmc_list_vios_vfc_group_labels (vFC group labels, baseline)"
        )
        is not None
    )
    if not adapters and vlan is None and fc_labels is None and not groups_read:
        return None
    return NetworkInputs(
        str(config.get("system_name")),
        str(config.get("lp3_name")),
        vlan if isinstance(vlan, int) else None,
        adapters,
        fc_labels if isinstance(fc_labels, list) else None,
        groups_read,
    )


async def check_network(call, inputs: NetworkInputs) -> list[Finding]:
    """A network on the test VLAN, adapters and labels off ST9's baselines."""
    findings: list[Finding] = []
    system = inputs.system_name
    status, data = await call("hmc_list_virtual_networks", system_name_or_uuid=system)
    if status != "PASS" or not isinstance(data, list):
        raise StateUnreadable(f"could not list the virtual networks ({status})")
    if inputs.vlan is not None:
        for entry in data:
            resource = entry.get("Resource", entry) if isinstance(entry, dict) else {}
            if network.as_int(resource.get("NetworkVLANID")) == inputs.vlan:
                findings.append(
                    Finding(
                        "network left on the test VLAN",
                        f"{resource.get('NetworkName')!r} ({entry.get('UUID')}) is on "
                        f"VLAN {inputs.vlan}, which was unused before the run",
                        "hmc_delete_virtual_network with that network_uuid, after "
                        "confirming it is the run's (its name starts "
                        f"{network.NAME_PREFIX})",
                    )
                )
    for kind, baseline in inputs.adapters.items():
        before = network.adapter_placements(baseline)
        status, data = await call(
            "hmc_list_adapters",
            lpar_name_or_uuid=inputs.lpar_name,
            adapter_type=kind,
            system_name_or_uuid=system,
        )
        now = network.adapter_placements(data) if status == "PASS" else None
        if before is None or now is None:
            raise StateUnreadable(
                f"could not compare {inputs.lpar_name}'s {kind} adapters", findings
            )
        if now != before:
            findings.append(
                Finding(
                    f"{kind} off its baseline",
                    f"{inputs.lpar_name}'s {kind} adapters differ from the run's baseline",
                    network.placement_drift(before, now),
                )
            )
    findings += await _network_labels(call, inputs, findings)
    return findings


async def _network_labels(
    call, inputs: NetworkInputs, found: list[Finding]
) -> list[Finding]:
    findings: list[Finding] = []
    status, groups = await call(
        "hmc_list_vios_vfc_group_labels", system_name_or_uuid=inputs.system_name
    )
    if status == "PASS" and isinstance(groups, list):
        left = sorted(
            str(network.group_name(row))
            for row in groups
            if isinstance(row, dict)
            and str(network.group_name(row)).startswith(network.LABEL_PREFIX)
        )
        if left:
            findings.append(
                Finding(
                    "vFC group label left",
                    f"{left} carry the run's prefix",
                    f"labelvios -m {shlex.quote(inputs.system_name)} -o r -l <label>",
                )
            )
    elif inputs.groups_read:
        raise StateUnreadable("could not list the vFC group labels", found + findings)
    if inputs.fc_labels is None:
        return findings
    before = {
        (str(row.get("name")), str(row.get("port_name"))): str(
            row.get("port_label") or ""
        )
        for row in inputs.fc_labels
        if isinstance(row, dict)
    }
    for vios in sorted({name for name, _ in before}):
        status, rows = await call(
            "hmc_list_vios_fc_port_labels",
            system_name_or_uuid=inputs.system_name,
            vios_name=vios,
        )
        if status != "PASS" or not isinstance(rows, list):
            raise StateUnreadable(
                f"could not list {vios}'s FC-port labels", found + findings
            )
        now = {
            (str(row.get("name")), str(row.get("port_name"))): str(
                row.get("port_label") or ""
            )
            for row in rows
            if isinstance(row, dict)
        }
        for key, label in before.items():
            if key[0] == vios and now.get(key) != label:
                findings.append(
                    Finding(
                        "FC-port label off its original",
                        f"{vios} {key[1]} reads {now.get(key)!r}, was {label!r}",
                        "hmc_set_vios_fc_port_label with the original label"
                        if label
                        else "hmc_remove_vios_fc_port_label",
                    )
                )
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
    if state != _NOT_ACTIVATED:
        # The HMC refuses to delete a partition outside Not Activated (#950): a
        # run interrupted while its fixture is running needs a shutdown first.
        remedy = (
            f"hmc chsysstate -m {inputs.system_name} -r lpar -n {inputs.fixture_lpar} "
            f"-o shutdown --immed; once it is {_NOT_ACTIVATED}, {remedy}"
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


@dataclass(frozen=True)
class LparResidueInputs:
    """What a run did to its configured test partition, as its document records.

    A subset run restores artifacts from an earlier document, so ST17-ST22 can
    act on ownership a previous invocation recorded. The flags therefore read the
    outstanding artifacts as well as this document's rows.
    """

    system_name: str
    lpar_name: str
    vios_uuid: str | None
    vios_partition_id: int | None
    vg_uuid: str | None
    iso_names: frozenset[str]
    vmedia_ran: bool
    provisioned: bool
    repository_owned: bool
    powered_on: bool
    boot_written: bool
    boot_baseline: str | None
    storage_ran: bool = False
    scratch_vg_ran: bool = False
    scratch_vg_pending: str | None = None
    volume_group: str = ""

    @property
    def applies(self) -> bool:
        return (
            self.vmedia_ran
            or self.storage_ran
            or self.scratch_vg_ran
            or self.provisioned
            or self.repository_owned
            or self.powered_on
            or self.boot_written
        )


def dispatched_subtasks(document: Any) -> list[int] | None:
    """The subtasks the run dispatched, or `None` when the document does not say."""
    run = document.get("run") if isinstance(document, dict) else None
    subtasks = run.get("subtasks") if isinstance(run, dict) else None
    if not isinstance(subtasks, list) or any(type(n) is not int for n in subtasks):
        return None
    return subtasks


def _calls(
    document: dict[str, Any], subtasks: AbstractSet[int], tools: AbstractSet[str]
) -> list[dict]:
    """Rows in *subtasks* recording a call to one of *tools*.

    A row's label opens with the tool it called. A `SKIP` row is a call never
    made, or one answered with a declared expected outcome, which by declaration
    changed nothing.
    """
    rows = document.get("results")
    return [
        row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        and row.get("subtask") in subtasks
        and row.get("status") != "SKIP"
        and str(row.get("tool", "")).split(" ", 1)[0] in tools
    ]


def _boot_baseline(document: dict[str, Any], saved: list[str]) -> str | None:
    """ST20's pending boot string before its write, else the saved boot order."""
    for row in _calls(document, {20}, {"hmc_read_lpar_boot_order"}):
        data = row.get("data")
        if (
            row.get("tool") == _BOOT_BASELINE_STEP
            and row.get("status") == "PASS"
            and isinstance(data, dict)
            and isinstance(data.get("pending_boot_string"), str)
        ):
            return data["pending_boot_string"]
    return " ".join(saved) if saved else None


def lpar_inputs_from_document(
    document: Any, subtasks: list[int]
) -> LparResidueInputs | None:
    """Read the test-partition residue a run could have left, or `None` for none."""
    if not isinstance(document, dict):
        return None
    config = document.get("config")
    artifacts = document.get("artifacts")
    if not isinstance(config, dict) or not isinstance(artifacts, dict):
        return None
    saved_boot = artifacts.get("vmedia_orig_boot_order")
    saved_boot = saved_boot if isinstance(saved_boot, list) else []
    inputs = LparResidueInputs(
        system_name=str(config.get("system_name") or ""),
        lpar_name=str(config.get("lp3_name") or ""),
        vios_uuid=artifacts.get("vios_uuid"),
        vios_partition_id=artifacts.get("vios_partition_id"),
        # A document written before #1347 names the repository's group `vg_uuid`.
        vg_uuid=artifacts.get("vmedia_vg_uuid") or artifacts.get("vg_uuid"),
        # Only names the run created: the configured ISO name may be an operator's,
        # and so may a name an older arm recorded.
        iso_names=frozenset(
            name
            for name in (
                artifacts.get("vmedia_iso_name"),
                artifacts.get("vmedia_blank_name"),
            )
            if is_run_media_name(name, str(config.get("iso_media_name") or ""))
        ),
        vmedia_ran=bool(_VMEDIA_SUBTASKS & set(subtasks)),
        provisioned=bool(
            _calls(document, {14}, {"hmc_provision_lpar", "hmc_delete_lpar"})
        ),
        repository_owned=artifacts.get("vmedia_repo_created") is True
        or bool(_calls(document, _VMEDIA_SUBTASKS, _REPOSITORY_TOOLS)),
        powered_on=bool(_calls(document, {20}, {"hmc_power_on_lpar"})),
        boot_written=bool(saved_boot)
        or bool(_calls(document, {20, 22}, {"hmc_set_lpar_boot_order"})),
        boot_baseline=_boot_baseline(document, saved_boot),
        storage_ran=storage_lifecycle.SUBTASK in subtasks,
        scratch_vg_ran=storage_volume_group.SUBTASK in subtasks
        or artifacts.get("storage_volume_group_name") is not None,
        scratch_vg_pending=artifacts.get("storage_volume_group_name"),
        volume_group=str(config.get("vdisk_volume_group_name") or ""),
    )
    return inputs if inputs.applies else None


def _required(value: Any, field: str, purpose: str) -> Any:
    if not value:
        raise StateUnreadable(
            f"the document records no {field}, needed to read {purpose}"
        )
    return value


def _q(value: object) -> str:
    return shlex.quote(str(value))


async def _optical_mapping_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether the run's ISO is still mounted to the test partition."""
    if not inputs.vmedia_ran:
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "optical mappings")
    status, data = await call(
        "hmc_list_optical_mappings",
        vios_name_or_uuid=vios,
        lpar_name_or_uuid=inputs.lpar_name,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(data, list):
        raise StateUnreadable(
            f"could not list optical mappings for {inputs.lpar_name} ({status})"
        )
    identities = [_mapping_identity(entry) for entry in data]
    if None in identities:
        raise StateUnreadable(
            f"an optical mapping for {inputs.lpar_name} names no partition or media"
        )
    left = sorted(
        {
            media
            for _, media in cast(list[tuple[str, str]], identities)
            if media in inputs.iso_names
        }
    )
    if not left:
        return None
    return Finding(
        "optical mapping left",
        f"{', '.join(left)} is still mounted to {inputs.lpar_name} from VIOS {vios}",
        "; ".join(
            f"hmcpctl storage unmount-optical-media {_q(vios)} --lpar {_q(inputs.lpar_name)} "
            f"--name {_q(media)} --system {_q(inputs.system_name)}"
            for media in left
        ),
    )


def _adapter_slots_toward(listing: str, lpar_name: str) -> list[str]:
    """Slots of the listed server adapters whose client is *lpar_name*."""
    text = listing.strip()
    if not text or text == HMC_NO_RESULTS:
        return []
    slots = []
    for line in text.splitlines():
        fields = line.strip().split(",")
        if len(fields) < 2 or not fields[0].isdigit():
            raise StateUnreadable(f"unexpected server adapter row {line!r}")
        if fields[1] == lpar_name:
            slots.append(fields[0])
    return slots


async def _unmapped_server_adapters(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether a VIOS server adapter toward the test partition has no mapping.

    A mapping names its adapter (`vhost0`), the listing its slot, so the two are
    compared by count. REST shows an unmapped adapter as a mapping holding only its
    `ServerAdapter`, with no partition link, so the partition's mappings never include
    it and a join would need the slot the projection does not carry (#1250).
    """
    if not (inputs.vmedia_ran or inputs.provisioned or inputs.storage_ran):
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "server adapters")
    vios_id = inputs.vios_partition_id
    if type(vios_id) is not int:
        raise StateUnreadable(
            "the document records no integer artifacts.vios_partition_id, "
            "needed to list server adapters"
        )
    status, listing = await call(
        "hmc_run_command",
        cmd=scsi_adapter_listing(inputs.system_name, "lpar_ids", vios_id),
    )
    if status != "PASS" or not isinstance(listing, str):
        raise StateUnreadable(
            f"could not list server adapters on VIOS {vios_id} ({status})"
        )
    slots = _adapter_slots_toward(listing, inputs.lpar_name)
    status, mappings = await call(
        "hmc_list_storage_mappings",
        vios_name_or_uuid=vios,
        lpar_name_or_uuid=inputs.lpar_name,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(mappings, list):
        raise StateUnreadable(
            f"could not list storage mappings for {inputs.lpar_name} ({status})"
        )
    ids = [entry.get("id") if isinstance(entry, dict) else None for entry in mappings]
    compare = (
        f"compare slots {', '.join(slots) or '(none)'} with `hmcpctl storage "
        f"list-mappings {_q(vios)} --lpar {_q(inputs.lpar_name)} "
        f"--system {_q(inputs.system_name)}`"
    )
    if any(not isinstance(id_, str) or "/" not in id_ for id_ in ids):
        raise StateUnreadable(
            f"a storage mapping for {inputs.lpar_name} names no server adapter; {compare}"
        )
    mapped = {id_.split("/", 1)[0] for id_ in cast(list[str], ids)}
    if len(slots) == len(mapped):
        return None
    if len(slots) < len(mapped):
        # A mapped adapter the listing does not name for this partition would
        # cancel out an unmapped one, so the count cannot say which is which.
        raise StateUnreadable(
            f"{len(mapped)} mapped server adapter(s) toward {inputs.lpar_name} but "
            f"{len(slots)} listed for it; {compare}"
        )
    return Finding(
        "unmapped server adapter",
        f"VIOS {vios_id} has {len(slots)} vSCSI server adapter(s) toward "
        f"{inputs.lpar_name} (slots {', '.join(slots)}) but {len(mapped)} mapped",
        f"{compare}; remove each unmapped one with `chhwres -r virtualio --rsubtype "
        f"scsi -m {_q(inputs.system_name)} -o r --id {vios_id} -s <slot>`",
    )


async def _repository_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether the media repository this run owned is still there."""
    if not inputs.repository_owned:
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "the media repository")
    vg = _required(inputs.vg_uuid, "artifacts.vmedia_vg_uuid", "the media repository")
    status, data = await call(
        "hmc_get_media_repository",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS":
        raise StateUnreadable(f"could not read the media repository in {vg} ({status})")
    if not data:
        return None
    where = f"{_q(vios)} --vg {_q(vg)}"
    system = f"--system {_q(inputs.system_name)}"
    return Finding(
        "media repository left",
        f"the repository the run created in volume group {vg} on VIOS {vios} "
        "still exists",
        f"hmcpctl storage list-optical-media {where} {system}; "
        f"hmcpctl storage delete-media {where} --name <each ISO> {system}; "
        f"hmcpctl storage delete-media-repo {where} {system}",
    )


async def _run_media_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether a medium the run created is still in the repository."""
    if not (inputs.vmedia_ran and inputs.iso_names):
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "the run's media")
    vg = _required(inputs.vg_uuid, "artifacts.vmedia_vg_uuid", "the run's media")
    status, data = await call(
        "hmc_list_optical_media",
        vios_name_or_uuid=vios,
        vg_uuid=vg,
        system_name_or_uuid=inputs.system_name,
    )
    names = (
        [entry.get("name") for entry in data if isinstance(entry, dict)]
        if (status == "PASS" and isinstance(data, list))
        else None
    )
    if names is None:
        raise StateUnreadable(f"could not list the media in {vg} ({status})")
    left = sorted(inputs.iso_names & set(names))
    if not left:
        return None
    return Finding(
        "run media left",
        f"{', '.join(left)} created by the run is still in volume group {vg} on "
        f"VIOS {vios}",
        "; ".join(
            f"hmcpctl storage delete-media {_q(vios)} --vg {_q(vg)} --name {_q(name)} "
            f"--system {_q(inputs.system_name)}"
            for name in left
        ),
    )


async def _run_disk_mapping_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether a mapping backed by a run-named volume is still on the VIOS."""
    if not inputs.storage_ran:
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "storage mappings")
    status, data = await call(
        "hmc_list_storage_mappings",
        vios_name_or_uuid=vios,
        system_name_or_uuid=inputs.system_name,
    )
    if status != "PASS" or not isinstance(data, list):
        raise StateUnreadable(f"could not list storage mappings on {vios} ({status})")
    left = sorted(
        (str(entry.get("id")), str(entry.get("backing_name")))
        for entry in storage_lifecycle.disk_mapping_rows(data)
    )
    if not left:
        return None
    system = f"--system {_q(inputs.system_name)}"
    return Finding(
        "run disk mapping left",
        "; ".join(f"{name} is still mapped as {id_}" for id_, name in left)
        + f" on VIOS {vios}",
        "; ".join(
            f"hmcpctl storage detach-mapping {_q(vios)} {_q(id_)} {system}"
            for id_, _ in left
        ),
    )


async def _run_disk_left(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether a run-named volume is still in the configured volume group."""
    if not inputs.storage_ran:
        return None
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "logical volumes")
    vios_id = inputs.vios_partition_id
    group = inputs.volume_group
    if type(vios_id) is not int or not all(
        storage_lifecycle.LISTING_NAME.fullmatch(name)
        for name in (inputs.system_name, group)
    ):
        raise StateUnreadable(
            "the document records no integer artifacts.vios_partition_id, or a "
            "system or volume-group name the read-only listing cannot carry"
        )
    status, listing = await call(
        "hmc_run_command",
        cmd=storage_lifecycle.volume_listing(inputs.system_name, vios_id, group),
    )
    names = storage_lifecycle.volume_names(listing) if status == "PASS" else None
    if names is None:
        raise StateUnreadable(f"could not list the volumes in {group} ({status})")
    left = sorted(name for name in names if storage_lifecycle.is_run_disk_name(name))
    if not left:
        return None
    system = f"--system {_q(inputs.system_name)}"
    return Finding(
        "run disk left",
        f"{', '.join(left)} created by the run is still in volume group {group} on "
        f"VIOS {vios}",
        f"hmcpctl storage list-vgs {_q(vios)} {system} (for the group's UUID); "
        + "; ".join(
            f"hmcpctl storage delete-disk {_q(vios)} --vg <{group} UUID> --name "
            f"{name} {system}"
            for name in left
        ),
    )


async def _partition_running(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether ST20 left the test partition running after its boot probe."""
    if not inputs.powered_on:
        return None
    status, state = await call(
        "hmc_get_lpar_state",
        system_name_or_uuid=inputs.system_name,
        lpar_name_or_uuid=inputs.lpar_name,
    )
    if status != "PASS" or not isinstance(state, str):
        raise StateUnreadable(
            f"could not read the state of {inputs.lpar_name} ({status})"
        )
    # REST's PartitionState is lower case ("not activated"); only the CLI title-cases it.
    if state.strip().lower() == _NOT_ACTIVATED.lower():
        return None
    return Finding(
        "test partition running",
        f"{inputs.lpar_name} on {inputs.system_name} is {state!r}; ST20 powered it "
        f"on and its cleanup leaves it {_NOT_ACTIVATED}",
        f"hmcpctl lpars power-off {_q(inputs.lpar_name)} --system "
        f"{_q(inputs.system_name)} --immediate --yes",
    )


async def _boot_string_drift(call, inputs: LparResidueInputs) -> Finding | None:
    """Whether the pending boot string differs from the one the run captured."""
    if not inputs.boot_written:
        return None
    baseline = inputs.boot_baseline
    if not baseline:
        raise StateUnreadable(
            f"the run wrote the boot order of {inputs.lpar_name} but the document "
            "records no baseline to compare with"
        )
    status, data = await call(
        "hmc_read_lpar_boot_order",
        system_name_or_uuid=inputs.system_name,
        lpar_name_or_uuid=inputs.lpar_name,
    )
    observed = data.get("pending_boot_string") if isinstance(data, dict) else None
    if (
        status != "PASS"
        or not isinstance(data, dict)
        or not isinstance(observed or "", str)
    ):
        raise StateUnreadable(
            f"could not read the boot order of {inputs.lpar_name} ({status})"
        )
    if (observed or "").split() == baseline.split():
        return None
    return Finding(
        "boot string drift",
        f"pending boot string of {inputs.lpar_name} is {observed!r}, not the "
        f"captured baseline {baseline!r}",
        f"hmcpctl lpars set-boot-order {_q(inputs.system_name)} "
        f"{_q(inputs.lpar_name)} {' '.join(_q(path) for path in baseline.split())}",
    )


async def _run_volume_group_left(call, inputs: LparResidueInputs) -> Finding | None:
    if not inputs.scratch_vg_ran:
        return None
    pending = inputs.scratch_vg_pending
    if pending is not None and not storage_volume_group.is_run_group(pending):
        raise StateUnreadable("document records an invalid pending scratch group")
    vios = _required(inputs.vios_uuid, "artifacts.vios_uuid", "scratch volume groups")
    status, data = await call(
        "hmc_list_volume_groups",
        vios_name_or_uuid=vios,
        system_name_or_uuid=inputs.system_name,
    )
    names = storage_volume_group.rest_group_names(data) if status == "PASS" else None
    if names is None:
        raise StateUnreadable("could not list scratch volume groups")
    left = sorted(name for name in names if storage_volume_group.is_run_group(name))
    if not left and pending is None:
        return None
    return Finding(
        "run volume group left" if left else "scratch group restoration unconfirmed",
        f"{', '.join(left) or pending} on VIOS {vios}; "
        + (
            f"baseline restoration for pending {pending} remains unconfirmed"
            if pending
            else "group remains listed"
        ),
        "compare the durable run and private before/after snapshots with independent "
        "PV/group reads; follow ST42 cleanup guards before removal; do not retry create "
        "or repair physical-volume metadata automatically",
    )


#: In the order an operator should clear them.
_PARTITION_CHECKS = (
    _partition_running,
    _optical_mapping_left,
    _run_media_left,
    _run_disk_mapping_left,
    _run_disk_left,
    _run_volume_group_left,
    _unmapped_server_adapters,
    _repository_left,
    _boot_string_drift,
)


async def check_test_partition(call, inputs: LparResidueInputs) -> list[Finding]:
    """Every applicable test-partition class, each read even if another fails.

    The reads are independent, unlike the PCIe chain, so one unreadable class
    does not hide another's residue.
    """
    findings: list[Finding] = []
    unread: list[str] = []
    for check_one in _PARTITION_CHECKS:
        try:
            finding = await check_one(call, inputs)
        except StateUnreadable as unreadable:
            unread.append(str(unreadable))
            continue
        if finding:
            findings.append(finding)
    if unread:
        raise StateUnreadable("; ".join(unread), findings)
    return findings


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
    config = runner._decode_saved_config(document.get("config"))
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
        from live_test.observation import plain_data

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


async def check_run(
    call,
    pcie: RecoveryInputs | None,
    partition: LparResidueInputs | None,
    vios: VIOSBackupInputs | None = None,
    network_inputs: NetworkInputs | None = None,
    users: bool = False,
    lpar_config_inputs: LparConfigInputs | None = None,
    lpar_power_inputs: LparPowerInputs | None = None,
    round2_inputs: Round2Inputs | None = None,
) -> list[Finding]:
    """Each arm's checks in turn, each read whatever the others found."""
    findings: list[Finding] = []
    unread: list[str] = []
    if users:
        try:
            findings += await _scratch_users_left(call)
        except StateUnreadable as unreadable:
            unread.append(str(unreadable))
    for run_checks, inputs in (
        (check, pcie),
        (check_test_partition, partition),
        (check_vios_backup, vios),
        (check_network, network_inputs),
        (check_lpar_config, lpar_config_inputs),
        (check_lpar_power, lpar_power_inputs),
        (check_round2, round2_inputs),
    ):
        if inputs is None:
            continue
        try:
            findings += await cast(Callable[..., Awaitable[list[Finding]]], run_checks)(
                call, inputs
            )
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
    pcie: RecoveryInputs | None,
    partition: LparResidueInputs | None,
    vios: VIOSBackupInputs | None = None,
    network_inputs: NetworkInputs | None = None,
    users: bool = False,
    lpar_config_inputs: LparConfigInputs | None = None,
    lpar_power_inputs: LparPowerInputs | None = None,
    round2_inputs: Round2Inputs | None = None,
) -> list[Finding]:
    state = runner.RunState(config=runner.LiveTestConfig())
    # The checks need the live run's composition, not bare ``create_mcp``:
    # ``hmc_run_command`` is an opt-in escape hatch that only it registers. Without
    # it the profile-drift check was answered "Unknown tool", which read as "no
    # drift" before ``_profile_drift`` raised, so the script reported a system
    # clean it had never looked at. The 2026-09-21 live run showed review alone had
    # missed that, so a test asserts the read-only tools are registered here.
    async with runner.served_client() as client:
        return await check_run(
            _read_only_caller(client, state),
            pcie,
            partition,
            vios,
            network_inputs,
            users,
            lpar_config_inputs,
            lpar_power_inputs,
            round2_inputs,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=Path("test-results-dedicated.json"),
        help="the run's results document (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        document = json.loads(args.results.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        print(f"ERROR: cannot read {args.results}: {error}", file=sys.stderr)
        return 2

    subtasks = dispatched_subtasks(document)
    if subtasks is None:
        print(
            f"ERROR: {args.results} records no run.subtasks, so what the run "
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
    witnessed = _WITNESSED_SUBTASKS | (
        set(runner.SUBTASK_GROUPS["round2"]) if round2_inputs else set()
    )
    unwitnessed = sorted(set(subtasks) - witnessed)
    pcie = inputs_from_document(document)
    partition = lpar_inputs_from_document(document, subtasks)
    vios = vios_backup_inputs_from_document(document, subtasks)
    network_inputs = network_inputs_from_document(document, subtasks)
    lpar_config_inputs = lpar_config_inputs_from_document(document, subtasks)
    lpar_power_inputs = lpar_power_inputs_from_document(document, subtasks)

    findings: list[Finding] = []
    unread: list[str] = [round2_error] if round2_error else []
    try:
        users = users_witnessed(document, subtasks)
    except ValueError as error:
        users = False
        unread.append(str(error))
    if users or any(
        inputs is not None
        for inputs in (
            pcie,
            partition,
            vios,
            network_inputs,
            lpar_config_inputs,
            lpar_power_inputs,
            round2_inputs,
        )
    ):
        if not runner._bootstrap_config():
            print("ERROR: no HMC credentials; cannot check the system", file=sys.stderr)
            return 2
        try:
            findings = asyncio.run(
                _run_checks(
                    pcie,
                    partition,
                    vios,
                    network_inputs,
                    users,
                    lpar_config_inputs,
                    lpar_power_inputs,
                    round2_inputs,
                )
            )
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


if __name__ == "__main__":
    raise SystemExit(main())
