"""CLI commands for VIOS storage and the virtual media repository."""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import asdict
from enum import Enum
from typing import Any

import typer
from typer._click.globals import get_current_context

from hmcpctl.client.core import HMCClient

from ...documents.storage import StorageKind, validate_virtual_disk
from ...jobs import job_outcome
from ...operations.lpar.profile_sync import ChangeLocation
from ...operations.lpar.provision import (
    AttachDiskResult,
    ProvisionStorage,
    attach_disk_to_lpar,
)
from ...operations.storage.resources import (
    StorageMapResult,
    create_media_repository,
    create_optical_media,
    create_virtual_disk,
    create_volume_group,
    delete_media_repository,
    delete_optical_media,
    delete_virtual_disk,
    detach_storage_mapping,
    get_media_repository,
    list_optical_media,
    list_storage_mappings,
    list_volume_groups,
    map_storage,
    mount_optical_media,
    resolve_volume_group_uuid,
    unmount_optical_media,
    upload_iso,
)
from ...resource_identity import resolve_vios_uuid
from ..output import (
    InventoryFormat,
    confirm_on_stderr,
    err_console,
    print_inventory,
    resolve_inventory_format,
    terminal_field,
    usage_error,
)
from ..runtime import with_client


def _confirm_storage(yes: bool, text: str) -> None:
    if yes:
        return
    ctx = get_current_context()
    options = ["hmcpctl", "storage", str(ctx.info_name)]
    arguments: list[str] = []
    for parameter in ctx.command.params:
        value = ctx.params.get(parameter.name)
        if parameter.name == "yes" or value is None or value is False:
            continue
        if isinstance(value, Enum):
            value = value.value
        if parameter.param_type_name == "argument":
            arguments.append(str(value))
        else:
            options.append(parameter.opts[0])
            if value is not True:
                options.append(str(value))
    tokens = []
    for value in [*options, "--yes", "--", *arguments]:
        if value.isprintable():
            tokens.append(shlex.quote(value))
        else:
            # ANSI-C quotes preserve control characters without emitting terminal controls.
            escaped = []
            for char in value:
                if char in "\\'":
                    escaped.append("\\" + char)
                elif char.isprintable():
                    escaped.append(char)
                else:
                    escaped.extend(f"\\x{byte:02x}" for byte in os.fsencode(char))
            tokens.append("$'" + "".join(escaped) + "'")
    invocation = " ".join(tokens)
    if sys.stdin is None or not sys.stdin.isatty():
        err_console.print(
            "Error: noninteractive storage mutation requires --yes/-y.\n"
            "With the same connection options, run in Bash/Zsh:\n"
            f"{terminal_field(invocation, err=True)}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        raise typer.Exit(2)
    confirm_on_stderr(
        f"{terminal_field(text, err=True)}\n"
        f"Command (Bash/Zsh): {terminal_field(invocation, err=True)}",
        abort=True,
    )


def _print_mutation(
    summary: dict[str, Any],
    raw: Any,
    selected: InventoryFormat,
    details: bool,
    *,
    location: ChangeLocation | None = None,
) -> None:
    summary = {
        "outcome": "request_succeeded",
        "verification": "final state not verified",
        **summary,
    }
    if location is not None:
        summary["change_location"] = asdict(location)
        summary["change_location_summary"] = location.summary()
    source = raw.get("resource", raw) if isinstance(raw, dict) else None
    resource = source.get("Resource", source) if isinstance(source, dict) else None
    if isinstance(resource, dict) and resource.get("JobID"):
        job = job_outcome(summary["operation"], {"Resource": resource})
        summary.update(job_id=job.job_id, job_status=job.status, error=job.error)
        summary["outcome"] = "completed"
        if job.error:
            summary["outcome"] = "failed"
        elif job.timed_out:
            summary["outcome"] = "submitted"
        summary["verification"] = "returned job status"
    print_inventory(raw if details else summary, selected, title="Storage result")
    if summary["outcome"] == "failed":
        raise typer.Exit(1)


def storage_list_vgs(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """List Volume Groups on a VIOS (free space, PVs, virtual disks)."""
    selected = resolve_inventory_format(as_json, format)
    vgs = with_client(
        lambda hmc: list_volume_groups(hmc, vios, system_name_or_uuid=system)
    )
    print_inventory(
        [asdict(vg) for vg in vgs],
        selected,
        ("Name", "UUID", "Free (GiB)", "Capacity (GiB)", "Diagnostic"),
        [
            (v.name, v.uuid, v.free_space_gib, v.capacity_gib, v.free_space_diagnostic)
            for v in vgs
        ],
        title=f"Volume Groups on {vios}",
        empty_msg="No volume groups found",
    )


def storage_create_vg(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    name: str = typer.Option(..., "--name", "-n", help="Volume Group name"),
    pvs: str = typer.Option(
        ..., "--pvs", help="Comma-separated physical volumes, e.g. hdisk10,hdisk11"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Create a Volume Group on a VIOS from physical volumes."""
    selected = resolve_inventory_format(as_json or details, format)
    pv_list = [p.strip() for p in pvs.split(",") if p.strip()]
    if not pv_list:
        usage_error("Provide at least one physical volume via --pvs")
    _confirm_storage(
        yes,
        f"Create VG '{terminal_field(name, err=True)}' from {terminal_field(pv_list, err=True)} on VIOS "
        f"{terminal_field(vios, err=True)}?",
    )

    vg = with_client(
        lambda hmc: create_volume_group(
            hmc, vios, name, pv_list, system_name_or_uuid=system
        )
    )

    _print_mutation(
        {
            "operation": "create-vg",
            "target": name,
            "vios": vios,
            "system": system,
            "physical_volumes": pv_list,
        },
        vg,
        selected,
        details,
    )


def storage_create_disk(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    name: str = typer.Option(
        ..., "--name", "-n", help="Virtual disk name (at most 15 characters)"
    ),
    capacity_mib: int = typer.Option(
        ..., "--capacity-mib", help="Virtual disk capacity in MiB"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Create a Virtual Disk (logical volume) in a Volume Group."""
    selected = resolve_inventory_format(as_json or details, format)
    _confirm_storage(
        yes,
        f"Create {terminal_field(capacity_mib, err=True)} MiB virtual disk '"
        f"{terminal_field(name, err=True)}' in VG {terminal_field(vg, err=True)}?",
    )

    disk = with_client(
        lambda hmc: create_virtual_disk(
            hmc, vios, vg, name, capacity_mib=capacity_mib, system_name_or_uuid=system
        )
    )

    _print_mutation(
        {
            "operation": "create-disk",
            "target": name,
            "vios": vios,
            "system": system,
            "vg": vg,
            "capacity_mib": capacity_mib,
        },
        disk,
        selected,
        details,
    )


def storage_delete_disk(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    name: str = typer.Option(..., "--name", "-n", help="Virtual disk name"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Delete a Virtual Disk from a Volume Group.

    Validates that the disk is not mapped to any LPAR before deletion.
    """
    selected = resolve_inventory_format(as_json or details, format)
    _confirm_storage(
        yes,
        f"Delete virtual disk '{terminal_field(name, err=True)}' from VG {terminal_field(vg, err=True)}?",
    )

    disk = with_client(
        lambda hmc: delete_virtual_disk(hmc, vios, vg, name, system_name_or_uuid=system)
    )

    _print_mutation(
        {
            "operation": "delete-disk",
            "target": name,
            "vios": vios,
            "system": system,
            "vg": vg,
        },
        disk,
        selected,
        details,
    )


def storage_attach_disk(
    lpar: str = typer.Argument(..., help="Target LPAR name or UUID"),
    vios: str = typer.Option(..., "--vios", help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    name: str = typer.Option(
        ..., "--name", "-n", help="New virtual disk name (at most 15 characters)"
    ),
    capacity_mib: int = typer.Option(
        ..., "--capacity-mib", help="Disk capacity in MiB"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate without mutation"),
    as_json: bool = typer.Option(
        False, "--json", "-j", help="Output JSON (existing result envelope)"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Create a virtual disk and attach it to an existing LPAR."""
    selected = resolve_inventory_format(as_json or details, format)
    if not dry_run:
        _confirm_storage(
            yes,
            f"Create {terminal_field(capacity_mib, err=True)} MiB disk '{terminal_field(name, err=True)}"
            f"' and attach it to LPAR '{terminal_field(lpar, err=True)}'?",
        )

    async def _go(hmc: HMCClient) -> AttachDiskResult:
        validate_virtual_disk(name, capacity_mib)
        vios_uuid = await resolve_vios_uuid(hmc, vios, system_name_or_uuid=system)
        vg_uuid = await resolve_volume_group_uuid(hmc, vios_uuid, vg)
        return await attach_disk_to_lpar(
            hmc,
            system,
            lpar,
            ProvisionStorage(vios_uuid, name, vg_uuid=vg_uuid),
            capacity_mib=capacity_mib,
            dry_run=dry_run,
            ownership_override=ownership_override,
        )

    result = with_client(_go)
    raw = asdict(result)
    outcome = "request_succeeded"
    if result.dry_run:
        outcome = "dry_run"
    elif not result.workflow_completed:
        outcome = "incomplete"
    summary = {
        "operation": "attach-disk",
        "target": name,
        "vios": vios,
        "vg": vg,
        "lpar": lpar,
        "lpar_uuid": result.lpar_uuid,
        "system": system,
        "outcome": outcome,
        "steps": [
            {
                "step": step.step,
                "status": step.status,
                **(
                    {"detail": step.result}
                    if step.status != "ok" and step.result is not None
                    else {}
                ),
            }
            for step in result.steps
        ],
        "warnings": result.warnings,
    }
    for step_summary, step in zip(summary["steps"], result.steps, strict=True):
        resource = (
            step.result.get("Resource") if isinstance(step.result, dict) else None
        )
        if isinstance(resource, dict) and resource.get("JobID"):
            job = job_outcome(step.step, step.result)
            step_summary.update(
                job_id=job.job_id, job_status=job.status, error=job.error
            )
    if result.dry_run:
        summary["verification"] = "preconditions validated; no mutation"
    _print_mutation(
        summary,
        raw,
        selected,
        details or selected == InventoryFormat.json,
        location=result.change_location,
    )
    if not result.dry_run and not result.workflow_completed:
        raise typer.Exit(1)


def storage_map(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    lpar: str = typer.Option(..., "--lpar", help="Target LPAR name or UUID"),
    disk: str = typer.Option(
        ..., "--name", "-n", help="Storage name (DiskName or hdiskN)"
    ),
    kind: StorageKind = typer.Option(
        "VirtualDisk", "--kind", help="VirtualDisk or PhysicalVolume"
    ),
    target: str | None = typer.Option(
        None, "--target", help="Pin the vtscsi device name"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Map backing storage to an LPAR via a vSCSI mapping on a VIOS."""
    selected = resolve_inventory_format(as_json or details, format)
    _confirm_storage(
        yes,
        f"Map {terminal_field(kind, err=True)} '{terminal_field(disk, err=True)}' on VIOS "
        f"{terminal_field(vios, err=True)} to LPAR '{terminal_field(lpar, err=True)}'?",
    )

    result: StorageMapResult = with_client(
        lambda hmc: map_storage(
            hmc,
            vios,
            lpar,
            system_name_or_uuid=system,
            kind=kind,
            storage_name=disk,
            target=target,
            ownership_override=ownership_override,
        )
    )

    _print_mutation(
        {
            "operation": "map",
            "target": disk,
            "vios": vios,
            "system": system,
            "lpar": lpar,
            "lpar_uuid": result.lpar_uuid,
            "kind": kind,
            "target_device": target,
        },
        asdict(result),
        selected,
        details,
        location=result.change_location,
    )


def storage_create_media_repo(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    size_mib: int = typer.Option(
        ...,
        "--size-mib",
        help="Repository size in MiB, a multiple of 1024 (sent to the HMC as whole GiB)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Create the Virtual Media Repository (VMLibrary) on a volume group."""
    selected = resolve_inventory_format(as_json or details, format)
    _confirm_storage(
        yes,
        f"Create {terminal_field(size_mib, err=True)} MiB media repository on VG "
        f"{terminal_field(vg, err=True)} (VIOS {terminal_field(vios, err=True)})?",
    )

    result = with_client(
        lambda hmc: create_media_repository(
            hmc, vios, vg, size_mib, system_name_or_uuid=system
        )
    )

    _print_mutation(
        {
            "operation": "create-media-repo",
            "target": vg,
            "vios": vios,
            "system": system,
            "vg": vg,
            "size_mib": size_mib,
        },
        result,
        selected,
        details,
    )


def storage_create_media(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    name: str = typer.Option(
        ..., "--name", "-n", help="Media file name (e.g. aix.iso)"
    ),
    size_mib: int = typer.Option(
        ...,
        "--size-mib",
        help="Media size in MiB, a multiple of 1024 (sent to the HMC as whole GiB)",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Create a blank optical media (ISO container) in the media repository."""
    selected = resolve_inventory_format(as_json or details, format)
    _confirm_storage(
        yes,
        f"Create media '{terminal_field(name, err=True)}' ({terminal_field(size_mib, err=True)}"
        f" MiB) on VG {terminal_field(vg, err=True)} (VIOS {terminal_field(vios, err=True)})?",
    )

    result = with_client(
        lambda hmc: create_optical_media(
            hmc, vios, vg, name, size_mib, system_name_or_uuid=system
        )
    )

    _print_mutation(
        {
            "operation": "create-media",
            "target": name,
            "vios": vios,
            "system": system,
            "vg": vg,
            "size_mib": size_mib,
        },
        result,
        selected,
        details,
    )


def storage_delete_media_repo(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Delete the Virtual Media Repository from a volume group."""
    selected = resolve_inventory_format(as_json or details, format)
    _confirm_storage(
        yes,
        f"Delete media repository on VG {terminal_field(vg, err=True)} (VIOS {terminal_field(vios, err=True)})?",
    )

    result = with_client(
        lambda hmc: delete_media_repository(hmc, vios, vg, system_name_or_uuid=system)
    )
    _print_mutation(
        {
            "operation": "delete-media-repo",
            "target": vg,
            "vios": vios,
            "system": system,
            "vg": vg,
        },
        result,
        selected,
        details,
    )


def storage_delete_media(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    media_name: str = typer.Option(
        ..., "--name", "-n", help="ISO image name to delete"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Delete an ISO image from the media repository."""
    selected = resolve_inventory_format(as_json or details, format)
    _confirm_storage(
        yes,
        f"Delete media '{terminal_field(media_name, err=True)}' on VG {terminal_field(vg, err=True)}"
        f" (VIOS {terminal_field(vios, err=True)})?",
    )

    result = with_client(
        lambda hmc: delete_optical_media(
            hmc, vios, vg, media_name, system_name_or_uuid=system
        )
    )
    _print_mutation(
        {
            "operation": "delete-media",
            "target": media_name,
            "vios": vios,
            "system": system,
            "vg": vg,
        },
        result,
        selected,
        details,
    )


def storage_get_media_repo(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """Get the Virtual Media Repository (RepositorySize is in GiB)."""
    selected = resolve_inventory_format(as_json, format)
    result = with_client(
        lambda hmc: get_media_repository(hmc, vios, vg, system_name_or_uuid=system)
    )
    print_inventory(
        result,
        selected,
        title="Media Repository (RepositorySize in GiB)",
        empty_msg="No media repository found",
    )


def storage_list_optical_media(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """List Virtual Optical Media in the Virtual Media Repository."""
    selected = resolve_inventory_format(as_json, format)
    media = with_client(
        lambda hmc: list_optical_media(hmc, vios, vg, system_name_or_uuid=system)
    )
    print_inventory(
        [asdict(item) for item in media],
        selected,
        ("Media Name", "Size (MiB)"),
        [(item.name, item.size_mib) for item in media],
        title=f"Optical Media in VG {vg}",
        empty_msg="No optical media found",
    )


def storage_mount_optical_media(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    lpar: str = typer.Option(..., "--lpar", help="Target LPAR name or UUID"),
    media_name: str = typer.Option(..., "--name", "-n", help="ISO image name to mount"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    target_device: str | None = typer.Option(
        None, "--target-device", help="Pin the vtscsi target device name"
    ),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass LPAR ownership protection after operator approval",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Mount an ISO from a VIOS media repository to an LPAR.

    A partition not activated since its profile changed refuses the mount with
    REST0269: activate or apply a profile once first. A refused mount can still
    leave a VIOS server adapter with no mapping; the error names the lshwres
    listing that shows it.
    """
    selected = resolve_inventory_format(as_json or details, format)
    target = f"; target device {target_device}" if target_device else ""
    selected_system = f" in managed system {system}" if system else ""
    _confirm_storage(
        yes,
        f"Mount optical media '{terminal_field(media_name, err=True)}' from VIOS "
        f"{terminal_field(vios, err=True)} to LPAR {terminal_field(lpar, err=True)}"
        f"{terminal_field(selected_system, err=True)}{terminal_field(target, err=True)}?",
    )

    result = with_client(
        lambda hmc: mount_optical_media(
            hmc,
            vios,
            lpar,
            media_name=media_name,
            target_device=target_device,
            ownership_override=ownership_override,
            system_name_or_uuid=system,
        )
    )
    _print_mutation(
        {
            "operation": "mount-optical-media",
            "target": media_name,
            "vios": vios,
            "system": system,
            "lpar": lpar,
            "lpar_uuid": result.lpar_uuid,
            "target_device": target_device,
        },
        asdict(result),
        selected,
        details,
        location=result.change_location,
    )


def storage_unmount_optical_media(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    lpar: str = typer.Option(..., "--lpar", help="Target LPAR name or UUID"),
    media_name: str = typer.Option(
        ..., "--name", "-n", help="ISO image name to unmount"
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass LPAR ownership protection after operator approval",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Unmount an ISO mapping while preserving the backing ISO."""
    selected = resolve_inventory_format(as_json or details, format)
    _confirm_storage(
        yes,
        f"Unmount optical media '{terminal_field(media_name, err=True)}' from LPAR "
        f"{terminal_field(lpar, err=True)} on VIOS {terminal_field(vios, err=True)}"
        f"? The backing ISO remains available.",
    )

    async def _go(hmc: HMCClient) -> ChangeLocation:
        return await unmount_optical_media(
            hmc,
            vios,
            lpar,
            media_name=media_name,
            ownership_override=ownership_override,
            system_name_or_uuid=system,
        )

    location = with_client(_go)
    _print_mutation(
        {
            "operation": "unmount-optical-media",
            "target": media_name,
            "vios": vios,
            "system": system,
            "lpar": lpar,
            "backing_storage": "retained",
        },
        asdict(location),
        selected,
        details,
        location=location,
    )


def storage_list_mappings(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    lpar: str | None = typer.Option(
        None, "--lpar", help="Scope to single LPAR by name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """List VirtualSCSIMappings on a VIOS (optionally scoped to an LPAR)."""
    selected = resolve_inventory_format(as_json, format)
    mappings = with_client(
        lambda hmc: list_storage_mappings(hmc, vios, lpar, system_name_or_uuid=system)
    )
    print_inventory(
        [asdict(mapping) for mapping in mappings],
        selected,
        ("Mapping ID", "Client LPAR", "Backing Storage", "Type"),
        [(m.id, m.lpar_uuid, m.backing_name, m.backing_kind) for m in mappings],
        title=f"Storage Mappings on {vios}",
        empty_msg="No storage mappings found",
    )


def storage_detach_mapping(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    mapping_id: str = typer.Argument(
        ...,
        help="Exact mapping ID (e.g. vhost0/vtscsi0) shown by storage list-mappings",
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass LPAR ownership protection after operator approval",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Confirm this mutation; required without terminal stdin",
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Detach a VirtualSCSIMapping while preserving its backing storage."""
    selected = resolve_inventory_format(as_json or details, format)
    _confirm_storage(
        yes,
        f"Delete storage mapping {terminal_field(mapping_id, err=True)} on VIOS "
        f"{terminal_field(vios, err=True)}? The backing storage (PhysicalVolume or "
        f"VirtualDisk) will be preserved.",
    )

    async def _go(hmc: HMCClient) -> ChangeLocation:
        return await detach_storage_mapping(
            hmc,
            vios,
            mapping_id,
            system_name_or_uuid=system,
            ownership_override=ownership_override,
        )

    location = with_client(_go)
    _print_mutation(
        {
            "operation": "detach-mapping",
            "target": mapping_id,
            "vios": vios,
            "system": system,
            "backing_storage": "retained",
        },
        asdict(location),
        selected,
        details,
        location=location,
    )


def storage_upload_iso(
    vios: str = typer.Argument(..., help="VIOS name or UUID"),
    vg: str = typer.Option(..., "--vg", help="Volume Group name or UUID"),
    media_name: str = typer.Option(
        ..., "--name", "-n", help="Target name for the ISO in the repository"
    ),
    iso_source: str = typer.Argument(
        ...,
        help="http(s) URL to download the ISO from; its host must be on "
        "HMC_ISO_URL_ALLOWLIST",
    ),
    as_json: bool = typer.Option(
        False, "--json", "-j", help="Output JSON (existing result envelope)"
    ),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Original result as JSON; may contain hardware identifiers",
    ),
) -> None:
    """Upload an ISO to a VIOS media repository via the HMC web File API.

    ISO_SOURCE must be an http(s) URL; a local file path is not accepted. Its host
    must be on HMC_ISO_URL_ALLOWLIST (or iso_url_allowlist in the profile) — with
    no allowlist configured every URL is refused — and redirects are not followed.
    Computes the SHA-256 and size of the downloaded bytes before upload, refuses a
    volume group without a media repository and name collisions, reports success
    only once the repository lists the media, and releases the HMC upload handle
    on every outcome.
    """
    selected = resolve_inventory_format(as_json or details, format)

    async def _go(hmc: HMCClient) -> dict[str, Any]:
        return await upload_iso(
            hmc,
            vios,
            vg,
            media_name,
            iso_source,
            system_name_or_uuid=system,
        )

    result = with_client(_go)

    summary = {
        "operation": "upload-iso",
        "target": media_name,
        "vios": vios,
        "vg": vg,
        "system": system,
        "outcome": result.get("status", "unknown"),
        "media_size_bytes": result.get("media_size_bytes"),
        "source_sha256": result.get("source_sha256"),
        "verification": "repository entry observed"
        if result.get("status") == "uploaded"
        else "final state not verified",
    }
    _print_mutation(
        summary, result, selected, details or selected == InventoryFormat.json
    )
    if result.get("status") != "uploaded":
        raise typer.Exit(1)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list-vgs")(storage_list_vgs)
    group.command("create-vg")(storage_create_vg)
    group.command("create-disk")(storage_create_disk)
    group.command("delete-disk")(storage_delete_disk)
    group.command("attach-disk")(storage_attach_disk)
    group.command("map")(storage_map)
    group.command("create-media-repo")(storage_create_media_repo)
    group.command("create-media")(storage_create_media)
    group.command("delete-media-repo")(storage_delete_media_repo)
    group.command("delete-media")(storage_delete_media)
    group.command("get-media-repo")(storage_get_media_repo)
    group.command("list-optical-media")(storage_list_optical_media)
    group.command("mount-optical-media")(storage_mount_optical_media)
    group.command("unmount-optical-media")(storage_unmount_optical_media)
    group.command("list-mappings")(storage_list_mappings)
    group.command("detach-mapping")(storage_detach_mapping)
    group.command("upload-iso")(storage_upload_iso)
