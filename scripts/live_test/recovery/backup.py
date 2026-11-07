"""Independent backup baseline and residue witnesses."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from live_test import vios_backup

from .common import Finding, StateUnreadable


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
