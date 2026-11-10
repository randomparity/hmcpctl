"""Independent partition recovery admission and read-only witnesses."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from hmcpctl.ssh.commands import HMC_NO_RESULTS
from live_test.arms import (
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
    processor_quantities,
    processor_sharing_weight,
    provision_boot,
    storage_lifecycle,
)
from live_test.arms.storage import scsi_adapter_listing
from live_test.recovery.common import NOT_ACTIVATED, Finding, StateUnreadable

from .common import VOLUME_LISTING


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
    if not VOLUME_LISTING.fullmatch(command):
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
