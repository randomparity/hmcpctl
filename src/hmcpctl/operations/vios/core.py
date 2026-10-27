"""Presentation-neutral VIOS operations."""

from __future__ import annotations

import csv
import io
import shlex
from dataclasses import replace
from typing import Any, Literal

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.partition_state import PARTITION_STATES, PartitionState
from hmcpctl.operations.systems.fleet import (
    FleetListing,
    operating_system_entry,
    read_fleet,
)

from ...documents.lpar import (
    LparResources,
    build_vios_document,
    reject_update_weight_on_create,
)
from ...documents.lpar_hardware_memory import hardware_memory_pairs
from ...documents.lpar_memory_layout import LparMemoryLayout
from ...documents.lpar_security_policy import LparSecurityPolicy
from ...documents.lpar_serial import VirtualSerialAdapter, validate_serial_input
from ...documents.lpar_vtpm import LparVtpmSettings
from ...errors import HMCError
from ...jobs import (
    DEFAULT_JOB_POLL_INTERVAL,
    DEFAULT_JOB_TIMEOUT_SECONDS,
    validate_wait_timing,
    wait_for_submitted_job,
)
from ...resource_identity import (
    is_uuid,
    optional_system_selector,
    resolve_system_uuid,
    resolve_vios_uuid,
)
from ...ssh.commands import HMC_NO_RESULTS, build_filter
from ...ssh.lpar import (
    complete_create_resources,
    create_lpar_via_cli,
    resolve_system_cli_name,
)
from ...ssh.lpar_memory_layout import admit_memory_layout, get_lpar_memory_layout
from ...ssh.lpar_vtpm import get_lpar_vtpm_state
from ...ssh.transport import run_hmc_cli
from ...xmlutil import render_mtms
from ..lpar.hardware_memory import admit_hardware_memory, created_hardware_memory_step
from ..lpar.linux_roles import read_role_capabilities
from ..lpar.memory_bound import require_memory_within_system
from ..lpar.ownership import _resolve_system_name
from ..lpar.security_policy import (
    admit_security_policy,
    read_lpar_security_policy,
    read_security_policy_capabilities,
)
from ..lpar.serial import (
    admit_serial_creation,
    confirm_serial_created,
    serial_creation_step,
)
from ..lpar.vtpm import admit_vtpm, read_vtpm_capabilities


async def list_vios(
    hmc: HMCClient,
    system_name_or_uuid: str | None = None,
    state: PartitionState | None = None,
) -> FleetListing:
    """List VIOSes, optionally scoped to one system or partition state.

    Without a system, each operating managed system is read in turn and the
    others are named in ``unreadable_systems`` (ADR 0197).
    """
    if state is not None and state not in PARTITION_STATES:
        allowed = ", ".join(sorted(PARTITION_STATES))
        raise ValueError(f"state must be one of: {allowed}")
    selector = optional_system_selector(system_name_or_uuid)
    system_uuid = (
        await resolve_system_uuid(hmc, selector) if selector is not None else None
    )
    listing = (
        FleetListing(await hmc.list_vios(system_uuid))
        if system_uuid is not None
        else await read_fleet(hmc, hmc.list_vios, "VIOSes")
    )
    if state is None:
        return listing
    return replace(
        listing,
        entries=[
            entry
            for entry in listing.entries
            if (entry.get("Resource") or {}).get("PartitionState") == state
        ],
    )


async def get_vios_storage_detail(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Get VIOS storage detail by UUID or an optionally system-scoped name.

    System scope disambiguates duplicate VIOS names; UUID selectors do not
    require it. Return None when the resolved VIOS read has no entry, rather
    than when a VIOS name cannot be resolved.

    Raises:
        ResourceNotFoundError: If the VIOS name or supplied system name cannot be resolved.
        ValueError: If a name is ambiguous or a UUID lies outside the supplied system.
        HMCError: If the HMC request fails or absence cannot be established.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    return await hmc.get_vios_storage_detail(vios_uuid)


async def create_vios(
    hmc: HMCClient,
    system_name_or_uuid: str,
    name: str,
    resources: LparResources,
    *,
    vtpm_settings: LparVtpmSettings | None = None,
    security_policy: LparSecurityPolicy | None = None,
    hardware_mem_encryption: bool | None = None,
    hardware_mem_expansion: bool | None = None,
    memory_layout: LparMemoryLayout | None = None,
    max_virtual_slots: int | None = None,
    virtual_serial_adapters: tuple[VirtualSerialAdapter, ...] = (),
) -> dict[str, Any] | None:
    """Create a VIOS partition on a managed system and return its entry.

    The VIOS goes to the system's ``VirtualIOServer`` collection as a
    VirtualIOServer document; V10R3 refuses one sent as a LogicalPartition
    (#1214).
    """
    reject_update_weight_on_create(resources)
    validate_serial_input(virtual_serial_adapters, max_virtual_slots)
    serial_requested = bool(virtual_serial_adapters)
    if memory_layout is not None:
        if not isinstance(memory_layout, LparMemoryLayout):
            raise TypeError("memory_layout must be LparMemoryLayout")
        memory_layout.validate_creation("Virtual IO Server")
    layout_requested = memory_layout is not None and bool(memory_layout.pairs())
    hardware_requested = bool(
        hardware_memory_pairs(hardware_mem_encryption, hardware_mem_expansion)
    )
    if security_policy is not None and not isinstance(
        security_policy, LparSecurityPolicy
    ):
        raise TypeError("security_policy must be LparSecurityPolicy")
    policy_requested = security_policy is not None and bool(security_policy.pairs())
    if security_policy is not None:
        security_policy.validate_creation("Virtual IO Server")
    if vtpm_settings is not None and not isinstance(vtpm_settings, LparVtpmSettings):
        raise TypeError("vtpm_settings must be LparVtpmSettings")
    vtpm_requested = vtpm_settings is not None and bool(vtpm_settings.pairs())
    if (
        vtpm_requested
        or policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
    ):
        completed = complete_create_resources(resources)
        if completed is None:
            raise ValueError(
                f"VIOS {'native' if policy_requested or hardware_requested or layout_requested or serial_requested else 'vTPM'} creation requires explicit memory/processor resource values; implicit all_resources allocation is not supported"
            )
        resources = completed
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    if not (
        vtpm_requested
        or policy_requested
        or hardware_requested
        or layout_requested
        or serial_requested
    ):
        return await hmc.create_vios(
            system_uuid,
            build_vios_document(
                name=name, resources=resources, max_virtual_slots=max_virtual_slots
            ),
        )
    if vtpm_settings is not None:
        vtpm_settings.validate_creation("Virtual IO Server")
    if await hmc.find_vios_by_name(name, system_uuid=system_uuid):
        raise ValueError(
            "A VIOS with that name already exists on the selected system; nothing was created"
        )
    await require_memory_within_system(hmc, system_uuid, resources)
    if vtpm_settings is not None and vtpm_requested:
        admit_vtpm(vtpm_settings, await read_vtpm_capabilities(hmc, system_uuid))
    if security_policy is not None and policy_requested:
        admit_security_policy(
            security_policy,
            await read_security_policy_capabilities(hmc, system_uuid),
            environment="Virtual IO Server",
        )
    await admit_hardware_memory(
        hmc, system_uuid, hardware_mem_encryption, hardware_mem_expansion
    )
    system = (
        await resolve_system_cli_name(hmc.config, system_uuid)
        if layout_requested or serial_requested
        else await _resolve_system_name(hmc, system_uuid, system_name_or_uuid)
    )
    controller_mode = False
    if layout_requested or serial_requested:
        controller_mode = (
            await read_role_capabilities(
                hmc, system_uuid, need_kvm=False, need_management=False
            )
        ).controller_mode
        if layout_requested:
            await admit_memory_layout(hmc.config, system, memory_layout)
    serial_admission = (
        await admit_serial_creation(
            hmc,
            system_uuid,
            name=name,
            partition_id=None,
            maximum=max_virtual_slots,
            adapters=virtual_serial_adapters,
            kind="VirtualIOServer",
        )
        if serial_requested
        else None
    )
    native_options: dict[str, Any] = {}
    if max_virtual_slots is not None:
        native_options["max_virtual_slots"] = max_virtual_slots
    if serial_requested:
        native_options["virtual_serial_adapters"] = virtual_serial_adapters
    if layout_requested:
        native_options["memory_layout"] = memory_layout
    if controller_mode:
        native_options["profile_name"] = None
    native_failed = False
    try:
        await create_lpar_via_cli(
            hmc.config,
            system,
            name,
            "Virtual IO Server",
            resources,
            vtpm_settings=vtpm_settings,
            security_policy=security_policy,
            hardware_mem_encryption=hardware_mem_encryption,
            hardware_mem_expansion=hardware_mem_expansion,
            **native_options,
        )
    except Exception as error:
        if not serial_requested and not isinstance(error, HMCError):
            raise
        if hardware_requested or layout_requested or serial_requested:
            native_failed = True
        elif not policy_requested:
            raise
        else:
            raise HMCError(
                "VIOS exists or may exist; creation outcome unknown. Inspect before retrying. No second create or rollback performed"
            ) from error
    if native_failed:
        raise HMCError(
            "VIOS exists or may exist; creation outcome unknown. Inspect before retrying. No second create or rollback performed"
        )
    verification_failed = False
    try:
        try:
            entry = await hmc.find_vios_by_name(name, system_uuid=system_uuid)
        except Exception:
            if not (hardware_requested or layout_requested or serial_requested):
                raise
            # Native creation succeeded; discard unavailable lookup diagnostics.
            entry = None
        if (
            entry is None
            or (layout_requested and not isinstance(entry, dict))
            or not entry.get("UUID")
        ):
            raise ValueError("Created VIOS identity was not confirmed")
        if (
            policy_requested
            or hardware_requested
            or layout_requested
            or serial_requested
        ) and (
            not isinstance(entry.get("UUID"), str)
            or not is_uuid(entry["UUID"])
            or not isinstance(entry.get("Resource"), dict)
            or entry["Resource"].get("PartitionName") != name
        ):
            raise ValueError("Created VIOS identity unconfirmed")
        if serial_admission is not None:
            entry = await confirm_serial_created(hmc, serial_admission, entry)
            serial_step = await serial_creation_step(
                hmc, serial_admission, entry, controller_mode=controller_mode
            )
            if serial_step.status != "ok":
                raise ValueError("Serial creation observation incomplete")
        if layout_requested:
            if controller_mode:
                raise ValueError(
                    "Controller current ratio mappings unavailable; no named profile was read"
                )
            observed_layout = await get_lpar_memory_layout(hmc.config, system, name)
            if any(
                getattr(observed_layout, field) != value
                for field, value in memory_layout.pairs()
            ):
                raise ValueError("Created VIOS memory ratios unavailable or mismatched")
        if vtpm_settings is not None and vtpm_requested:
            observed = await get_lpar_vtpm_state(hmc.config, system, name, vios=True)
            if any(
                getattr(observed.settings, field) != getattr(vtpm_settings, field)
                for field, _ in vtpm_settings.pairs()
            ):
                raise ValueError(
                    "Created VIOS vTPM readback differs from the requested fields"
                )
        if security_policy is not None and policy_requested:
            policy = await read_lpar_security_policy(
                hmc, entry["UUID"], system_name_or_uuid=system_uuid, vios=True
            )
            if any(
                getattr(policy.settings, field) != getattr(security_policy, field)
                for field, _ in security_policy.pairs()
            ):
                raise ValueError(
                    "Created VIOS policy readback differs from requested fields"
                )
    except Exception as error:
        if not serial_requested and not isinstance(
            error, (HMCError, ValueError, PermissionError)
        ):
            raise
        if hardware_requested or layout_requested or serial_requested:
            verification_failed = True
        elif policy_requested:
            raise HMCError(
                "VIOS exists or may exist; creation verification unavailable or mismatched. Inspect before retrying. No second create or rollback performed"
            ) from error
        else:
            raise HMCError(
                f"VIOS creation returned, but verification is unavailable or mismatched: {error}. The VIOS exists or may exist; inspect before retrying. No second create or rollback performed"
            ) from error
    if verification_failed:
        raise HMCError(
            "VIOS exists or may exist; creation verification unavailable or mismatched. Inspect before retrying. No second create or rollback performed"
            if layout_requested or serial_requested
            else "VIOS exists or may exist; hardware memory access readback-unavailable. Inspect before retrying. No second create or rollback performed"
        )
    assert (
        entry is not None
    )  # Successful identity verification above requires an entry.
    hardware_step = await created_hardware_memory_step(
        hmc,
        system_uuid,
        entry,
        name,
        "Virtual IO Server",
        hardware_mem_encryption,
        hardware_mem_expansion,
        vios=True,
    )
    if hardware_step is not None and hardware_step.status != "ok":
        raise HMCError(
            f"VIOS exists or may exist; hardware memory access {hardware_step.result['status']}. "
            "Inspect before retrying. No second create or rollback performed"
        )
    if serial_admission is not None:
        return {**entry, "serial_verification": serial_step.result}
    return entry


async def delete_vios(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> str:
    """Delete an inactive VIOS partition.

    Raises:
        HMCError: If the VIOS is not in the ``not activated`` state.
    """
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    state = await hmc.get_quick_property("VirtualIOServer", vios_uuid, "PartitionState")
    if state != "not activated":
        raise HMCError(
            f"Cannot delete VIOS {vios_uuid} — current state is {state!r}; it "
            "must be 'not activated' to delete. Power it off "
            "(hmc_power_off_vios) and confirm with hmc_list_vios before retrying.",
            status_code=409,
        )
    await hmc.delete_vios(vios_uuid)
    return vios_uuid


async def power_vios(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
    power_on: bool,
    immediate: bool = False,
    wait: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
) -> dict[str, Any] | None:
    """Submit a VIOS power job and optionally wait for completion.

    Raises:
        ValueError: If the polling controls are invalid.
    """
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    vios_uuid = await resolve_vios_uuid(
        hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
    )
    if power_on:
        job = await hmc.power_on_vios(vios_uuid)
    else:
        job = await hmc.power_off_vios(vios_uuid, immediate=immediate)
    return await wait_for_submitted_job(hmc, job, wait, timeout_seconds, poll_interval)


async def _resolve_vios_backup_system_name(
    hmc: HMCClient, system_name_or_uuid: str
) -> str:
    """Resolve a system UUID to its unique CLI MTMS identity."""
    if not is_uuid(system_name_or_uuid):
        return system_name_or_uuid
    entry = await operating_system_entry(hmc, system_name_or_uuid, "the MTMS")
    resource = (entry or {}).get("Resource") or {}
    mtms = render_mtms(resource)
    if mtms is not None:
        return mtms
    raise ValueError(
        f"Managed system {system_name_or_uuid!r} has no complete, valid "
        "MachineTypeModelAndSerialNumber (MTMS). Use hmc_list_systems to inspect "
        "the managed system before retrying."
    )


BackupType = Literal["vios", "viosioconfig", "ssp"]
RestoreBackupType = Literal["viosioconfig", "ssp"]
_VALID_BACKUP_TYPES: frozenset[BackupType] = frozenset({"vios", "viosioconfig", "ssp"})
_VALID_RESTORE_BACKUP_TYPES: frozenset[RestoreBackupType] = frozenset(
    {"viosioconfig", "ssp"}
)


async def _resolve_vios_backup_selectors(
    hmc: HMCClient,
    system_name_or_uuid: str,
    vios_name_or_uuid: str,
) -> tuple[str, str]:
    """Resolve backup selectors to the identities required by the HMC CLI.

    The selector is required: ``resolve_vios_uuid`` reads a blank one as absent
    and would resolve the VIOS fleet-wide while ``-m`` carried the blank.
    """
    selector = optional_system_selector(system_name_or_uuid)
    if selector is None:
        raise ValueError("system_name_or_uuid is required for VIOS backup and restore")
    system_name_or_uuid = selector
    system_name = system_name_or_uuid
    vios_uuid = vios_name_or_uuid
    if is_uuid(system_name_or_uuid) or not is_uuid(vios_name_or_uuid):
        if is_uuid(system_name_or_uuid):
            system_name = await _resolve_vios_backup_system_name(
                hmc, system_name_or_uuid
            )
        if not is_uuid(vios_name_or_uuid):
            vios_uuid = await resolve_vios_uuid(
                hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
            )
    return system_name, vios_uuid


async def list_vios_backups(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    *,
    system_name_or_uuid: str | None = None,
) -> list[dict[str, str]]:
    """Return the validated backup catalog for one VIOS."""
    vios_uuid = vios_name_or_uuid
    if not is_uuid(vios_name_or_uuid):
        vios_uuid = await resolve_vios_uuid(
            hmc, vios_name_or_uuid, system_name_or_uuid=system_name_or_uuid
        )
    command = (
        f"lsviosbk --filter {shlex.quote(build_filter([('vios_uuids', vios_uuid)]))} "
        "-F name,type --header"
    )
    output = await run_hmc_cli(command, hmc.config)
    if output.strip() in ("", HMC_NO_RESULTS):
        return []
    try:
        reader = csv.DictReader(io.StringIO(output, newline=""), strict=True)
        if reader.fieldnames != ["name", "type"]:
            raise ValueError(
                "Malformed lsviosbk CSV: expected the exact header 'name,type'."
            )
        results: list[dict[str, str]] = []
        for row in reader:
            name = row.get("name")
            backup_type = row.get("type")
            if None in row or not name or not backup_type:
                raise ValueError(
                    "Malformed lsviosbk CSV: each row must contain exactly one "
                    "nonempty name and type."
                )
            results.append({"name": name, "type": backup_type})
    except csv.Error as exc:
        raise ValueError(f"Malformed lsviosbk CSV: {exc}") from exc
    return results


def validate_vios_backup_name(backup_name: str) -> None:
    """Refuse a name that could identify anything except one catalog entry.

    Catalog operations stay bounded by the VIOS selected with ``--uuid`` only
    while the backup name cannot be interpreted as a path or command option.
    Deliberately avoid a character-set or length rule so valid names outside a
    guessed HMC grammar remain usable.
    """
    if (
        not backup_name
        or backup_name != backup_name.strip()
        or "/" in backup_name
        or "\\" in backup_name
        or backup_name.strip(".") == ""
        or backup_name.startswith("-")
    ):
        raise ValueError(
            f"backup_name {backup_name!r} must be a nonempty, unpadded catalog "
            "name without path separators; it must not consist only of dots or "
            "start with '-'. It is resolved inside the declared VIOS's own backup "
            "catalog."
        )


def validate_vios_backup_request(backup_name: str, backup_type: BackupType) -> None:
    """Validate a backup request before opening any HMC connection."""
    if backup_type not in _VALID_BACKUP_TYPES:
        raise ValueError(
            f"Invalid backup_type {backup_type!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_BACKUP_TYPES))}"
        )
    validate_vios_backup_name(backup_name)


def validate_vios_restore_request(
    backup_name: str, backup_type: RestoreBackupType
) -> None:
    """Validate a restore request before opening any HMC connection."""
    if backup_type not in _VALID_RESTORE_BACKUP_TYPES:
        raise ValueError(
            f"Invalid backup_type {backup_type!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_RESTORE_BACKUP_TYPES))}"
        )
    validate_vios_backup_name(backup_name)


async def backup_vios(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    *,
    system_name_or_uuid: str,
    backup_name: str,
    backup_type: BackupType = "vios",
) -> str:
    """Create a named VIOS backup and return the raw HMC CLI output."""
    validate_vios_backup_request(backup_name, backup_type)
    system_name, vios_uuid = await _resolve_vios_backup_selectors(
        hmc, system_name_or_uuid, vios_name_or_uuid
    )
    command = (
        f"mkviosbk -t {shlex.quote(backup_type)} "
        f"-m {shlex.quote(system_name)} --uuid {shlex.quote(vios_uuid)} "
        f"-f {shlex.quote(backup_name)}"
    )
    return await run_hmc_cli(command, hmc.config)


async def restore_vios(
    hmc: HMCClient,
    vios_name_or_uuid: str,
    backup_name: str,
    *,
    system_name_or_uuid: str,
    backup_type: RestoreBackupType,
    restart_if_required: bool = False,
) -> str:
    """Restore a VIOS backup and return the raw HMC CLI output."""
    validate_vios_restore_request(backup_name, backup_type)
    system_name, vios_uuid = await _resolve_vios_backup_selectors(
        hmc, system_name_or_uuid, vios_name_or_uuid
    )
    command = (
        f"rstviosbk -t {shlex.quote(backup_type)} "
        f"-m {shlex.quote(system_name)} --uuid {shlex.quote(vios_uuid)} "
        f"-f {shlex.quote(backup_name)}"
        f"{' -r' if restart_if_required else ''}"
    )
    return await run_hmc_cli(command, hmc.config)
