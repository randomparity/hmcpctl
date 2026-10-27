"""CLI command for creating an LPAR."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter

from ...documents.lpar import (
    PARTITION_TYPES,
    LparReportingSettings,
    LparResources,
    LparSettings,
    validate_partition_type,
)
from ...documents.lpar_affinity_group import parse_affinity_group_cli
from ...documents.lpar_creation_memory import LparCreationMemory
from ...documents.lpar_memory_layout import LparMemoryLayout
from ...documents.lpar_serial import validate_serial_input
from ...documents.lpar_vtpm import LparVtpmSettings
from ...operations.lpar.core import LparCreation
from ...operations.lpar.workflows import create_lpar
from ...ssh.lpar import validate_caller_token
from ..output import console, err_console, print_json, terminal_field, usage_error
from ..runtime import with_client
from .assignment_input import (
    SECURITY_POLICY_WARNING,
    SERIAL_INPUT_HELP,
    load_pcie_assignments,
    load_physical_io,
    load_security_policy,
    load_virtual_serial_adapters,
)


def lpars_create(
    name: str = typer.Argument(..., help="Name for the new partition"),
    system: str = typer.Option(
        ..., "--system", "-s", help="Target managed system UUID"
    ),
    partition_type: str = typer.Option(
        "AIX/Linux", "--type", help=f"One of: {', '.join(PARTITION_TYPES)}"
    ),
    partition_id: int | None = typer.Option(
        None, "--id", help="Partition ID (auto-assigned if omitted)"
    ),
    min_memory: int = typer.Option(256, "--min-mem", help="Minimum memory (MiB)"),
    memory: int = typer.Option(4096, "--mem", help="Desired memory (MiB)"),
    max_memory: int = typer.Option(8192, "--max-mem", help="Maximum memory (MiB)"),
    dedicated: bool = typer.Option(
        False, "--dedicated", help="Dedicated CPUs instead of shared"
    ),
    min_procs: float | None = typer.Option(
        None, "--min-procs", help="Min processing units / dedicated CPUs"
    ),
    procs: float | None = typer.Option(
        None, "--procs", help="Desired processing units / dedicated CPUs"
    ),
    max_procs: float | None = typer.Option(
        None, "--max-procs", help="Max processing units / dedicated CPUs"
    ),
    min_vcpus: int | None = typer.Option(
        None, "--min-vcpus", help="Min virtual processors (shared)"
    ),
    vcpus: int | None = typer.Option(
        1, "--vcpus", help="Desired virtual processors (shared)"
    ),
    max_vcpus: int | None = typer.Option(
        2, "--max-vcpus", help="Max virtual processors (shared)"
    ),
    capped: bool = typer.Option(
        False, "--capped", help="Cap shared CPU (default uncapped)"
    ),
    physical_io: Path | None = typer.Option(
        None,
        "--physical-io",
        help="Typed creation I/O JSON (64 KiB maximum); no implicit console default",
    ),
    pcie_assignments: Path | None = typer.Option(
        None,
        "--pcie-assignments",
        help="JSON file using the declarative LparPcieAssignments schema",
    ),
    caller_token: str | None = typer.Option(
        None,
        "--caller-token",
        help="Optional tracking reference embedded in the partition description "
        "as '\\[caller <token>]' (ADR 0064); 1–64 printable ASCII characters, "
        'no whitespace or , = " [ ] \\',
    ),
    no_apply: bool = typer.Option(
        False,
        "--no-apply",
        help="Leave the new partition profile unapplied when the HMC creates the "
        "partition through mksyscfg (the partition then has no current configuration)",
    ),
    allow_perf_collection: bool | None = typer.Option(
        None, "--allow-perf-collection/--no-allow-perf-collection"
    ),
    lpar_avail_priority: int | None = typer.Option(
        None, "--lpar-avail-priority", min=0, max=255
    ),
    time_ref: bool | None = typer.Option(None, "--time-ref/--no-time-ref"),
    simplified_remote_restart_capable: bool | None = typer.Option(
        None,
        "--simplified-remote-restart-capable/--no-simplified-remote-restart-capable",
    ),
    migration_disabled: bool | None = typer.Option(
        None, "--migration-disabled/--no-migration-disabled"
    ),
    powervm_mgmt_capable: bool | None = typer.Option(
        None,
        "--powervm-mgmt-capable/--no-powervm-mgmt-capable",
        help="Linux management capability; selecting either value declares intent, not installed OS proof",
    ),
    kvm_capable: bool | None = typer.Option(
        None,
        "--kvm-capable/--no-kvm-capable",
        help="Linux KVM eligibility; configuration only, requires system capability and HMC >=10.3.1060.1",
    ),
    os400_restricted_io_mode: bool | None = typer.Option(
        None, "--os400-restricted-io-mode/--no-os400-restricted-io-mode"
    ),
    work_group_id: str | None = typer.Option(
        None, "--work-group-id", help="Integer EWLM ID or none"
    ),
    electronic_err_reporting: bool | None = typer.Option(
        None, "--electronic-err-reporting/--no-electronic-err-reporting"
    ),
    redundant_err_path_reporting: bool | None = typer.Option(
        None, "--redundant-err-path-reporting/--no-redundant-err-path-reporting"
    ),
    vtpm_enabled: bool | None = typer.Option(None, "--vtpm-enabled/--no-vtpm-enabled"),
    vtpm_version: str | None = typer.Option(
        None, "--vtpm-version", help="Advertised version"
    ),
    vtpm_encryption: str | None = typer.Option(
        None, "--vtpm-encryption", help="Advertised 2.0 level"
    ),
    security_policy: str | None = typer.Option(
        None,
        "--security-policy",
        help="Inline strict policy JSON (64 KiB); signed/Linux enablement irreversible, Linux selection declares intent only",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    hardware_mem_encryption: Annotated[
        bool | None,
        typer.Option(
            "--hardware-mem-encryption/--no-hardware-mem-encryption",
            help="Hardware memory encryption access; omitted preserves defaults",
        ),
    ] = None,
    hardware_mem_expansion: Annotated[
        bool | None,
        typer.Option(
            "--hardware-mem-expansion/--no-hardware-mem-expansion",
            help="Hardware memory expansion access; omitted preserves defaults",
        ),
    ] = None,
    min_num_huge_pages: Annotated[
        int | None,
        typer.Option(
            "--min-num-huge-pages",
            help="AIX/Linux huge-page count; separate pool, capacity checked at activation",
        ),
    ] = None,
    desired_num_huge_pages: Annotated[
        int | None,
        typer.Option(
            "--desired-num-huge-pages",
            help="AIX/Linux huge-page count; separate pool, capacity checked at activation",
        ),
    ] = None,
    max_num_huge_pages: Annotated[
        int | None,
        typer.Option(
            "--max-num-huge-pages",
            help="AIX/Linux huge-page count; separate pool, capacity checked at activation",
        ),
    ] = None,
    hpt_ratio: Annotated[
        str | None,
        typer.Option(
            "--hpt-ratio",
            help="Advertised selected-system ratio; omission preserves defaults",
        ),
    ] = None,
    ppt_ratio: Annotated[
        str | None,
        typer.Option(
            "--ppt-ratio",
            help="Advertised selected-system ratio; omission preserves defaults",
        ),
    ] = None,
    kvm_reserved_mem_percent: Annotated[
        str | None,
        typer.Option(
            "--kvm-reserved-mem-percent",
            help="Exact finite reservation, at most two decimals; <=65536 canonical ASCII bytes; selecting zero also declares Linux intent; HMC decides numeric limits",
        ),
    ] = None,
    max_virtual_slots: Annotated[
        int | None,
        typer.Option(
            "--max-virtual-slots",
            help="Maximum virtual I/O slots; required explicitly for serial input",
        ),
    ] = None,
    virtual_serial_adapters: Annotated[
        str | None,
        typer.Option("--virtual-serial-adapters", help=SERIAL_INPUT_HELP),
    ] = None,
    mem_mode: Annotated[
        str | None,
        typer.Option(
            "--mem-mode",
            help="Explicit ded/shared memory mode; omitted preserves defaults",
        ),
    ] = None,
    desired_io_entitled_mem: Annotated[
        str | None,
        typer.Option(
            "--desired-io-entitled-mem",
            help="Shared-mode positive integer MiB or exact auto",
        ),
    ] = None,
    mem_weight: Annotated[
        int | None,
        typer.Option(
            "--mem-weight", help="Shared-mode memory weight 0–255; zero is explicit"
        ),
    ] = None,
    primary_paging_vios_name: Annotated[
        str | None,
        typer.Option(
            "--primary-paging-vios-name",
            help="Exact contained paging VIOS name; excludes primary ID",
        ),
    ] = None,
    primary_paging_vios_id: Annotated[
        int | None,
        typer.Option(
            "--primary-paging-vios-id",
            help="Positive paging VIOS PartitionID; excludes primary name",
        ),
    ] = None,
    secondary_paging_vios_name: Annotated[
        str | None,
        typer.Option(
            "--secondary-paging-vios-name",
            help="Exact contained secondary VIOS name; excludes secondary ID",
        ),
    ] = None,
    secondary_paging_vios_id: Annotated[
        str | None,
        typer.Option(
            "--secondary-paging-vios-id",
            help="Positive PartitionID or exact none to disable designation",
        ),
    ] = None,
    mem_expansion: Annotated[
        str | None,
        typer.Option(
            "--mem-expansion",
            help="Saved AME zero or exact numeric 1–10; no AIX intent or runtime proof",
        ),
    ] = None,
    affinity_group_id: Annotated[
        str | None,
        typer.Option(
            "--affinity-group-id",
            help="Saved affinity group: 1–255 or none; native observation is separate; omitted preserves defaults",
        ),
    ] = None,
) -> None:
    """Create a new LPAR on a managed system.

    Creates the partition powered off with a default profile; storage/network
    and boot settings are configured afterwards via the HMC. When the HMC
    creates it through mksyscfg, the profile is then applied (an
    ``apply_profile`` step) unless --no-apply is given. Settings preserve omitted
    values; a running time-reference partition can govern system time.
    Role selection declares Linux intent. KVM enablement adds fixed POWER10 in
    the creation record and requires explicit dedicated never-share CPUs, which
    are never changed implicitly. Controller mode requires --no-apply and cannot
    use initial-profile I/O or profile-bearing assignments. Readback proves only
    configuration, not KVM or NovaLink deployment. Creation memory verifies saved
    and current configured facts separately; unavailable evidence stops follow-ups.
    Saved AME declares no AIX intent; AIX-only runtime compression is not proven.
    """
    try:
        creation_memory = TypeAdapter(LparCreationMemory).validate_python(
            {
                "mem_mode": mem_mode,
                "desired_io_entitled_mem": None
                if desired_io_entitled_mem is None
                else "auto"
                if desired_io_entitled_mem == "auto"
                else int(desired_io_entitled_mem),
                "mem_weight": mem_weight,
                "primary_paging_vios_name": primary_paging_vios_name,
                "primary_paging_vios_id": primary_paging_vios_id,
                "secondary_paging_vios_name": secondary_paging_vios_name,
                "secondary_paging_vios_id": None
                if secondary_paging_vios_id is None
                else "none"
                if secondary_paging_vios_id == "none"
                else int(secondary_paging_vios_id),
                "mem_expansion": None
                if mem_expansion is None
                else Decimal(mem_expansion),
            }
        )
    except (ValueError, InvalidOperation) as error:
        raise typer.BadParameter(
            terminal_field(error, err=True), param_hint="creation memory"
        ) from None
    serial = load_virtual_serial_adapters(virtual_serial_adapters)
    try:
        validate_serial_input(serial, max_virtual_slots)
    except (ValueError, TypeError):
        raise typer.BadParameter(
            "Unique local slots >=2 must be below explicit --max-virtual-slots; encoded serial collection must fit 64 KiB",
            param_hint="--virtual-serial-adapters",
        ) from None
    try:
        layout = LparMemoryLayout(
            min_num_huge_pages=min_num_huge_pages,
            desired_num_huge_pages=desired_num_huge_pages,
            max_num_huge_pages=max_num_huge_pages,
            hpt_ratio=hpt_ratio,
            ppt_ratio=ppt_ratio,
            kvm_reserved_mem_percent=None
            if kvm_reserved_mem_percent is None
            else Decimal(kvm_reserved_mem_percent),
        )
        layout.validate_creation(partition_type)
    except (ValueError, InvalidOperation) as error:
        raise typer.BadParameter(
            terminal_field(error, err=True), param_hint="memory layout"
        ) from None
    try:
        affinity = parse_affinity_group_cli(affinity_group_id)
    except ValueError as error:
        raise typer.BadParameter(
            terminal_field(error, err=True), param_hint="--affinity-group-id"
        ) from None
    policy = load_security_policy(security_policy)
    try:
        policy.validate_creation(partition_type)
    except ValueError as error:
        usage_error(f"Invalid --security-policy: {terminal_field(error, err=True)}")
    if policy.pairs():
        err_console.print(
            terminal_field(SECURITY_POLICY_WARNING, err=True), markup=False
        )
    try:
        vtpm = LparVtpmSettings(vtpm_enabled, vtpm_version, vtpm_encryption)
        vtpm.validate_creation(partition_type)
        reporting = LparReportingSettings(
            os400_restricted_io_mode=os400_restricted_io_mode,
            work_group_id=None
            if work_group_id is None
            else "none"
            if work_group_id == "none"
            else int(work_group_id),
            electronic_err_reporting=electronic_err_reporting,
            redundant_err_path_reporting=redundant_err_path_reporting,
        )
        reporting.validate_context("create", partition_type)
    except ValueError as error:
        usage_error(
            f"Invalid reporting settings: {error}; work-group ID must be integer or none"
        )
    if caller_token is not None:
        validate_caller_token(caller_token)
    try:
        lpar_type = validate_partition_type(partition_type)
    except ValueError as exc:
        usage_error(f"--type: {exc}")
    if not yes:
        typer.confirm(
            (
                f"Create LPAR '{terminal_field(name)}' ({terminal_field(partition_type)}, "
                f"{terminal_field(memory)} MiB) on system {terminal_field(system)}?"
            ),
            abort=True,
        )
    resources = LparResources(
        min_memory=min_memory,
        desired_memory=memory,
        max_memory=max_memory,
        dedicated=dedicated,
        min_procs=min_procs,
        desired_procs=procs,
        max_procs=max_procs,
        min_vcpus=min_vcpus,
        desired_vcpus=vcpus,
        max_vcpus=max_vcpus,
        uncapped=not capped,
    )
    assignments = load_pcie_assignments(pcie_assignments)

    result = with_client(
        lambda hmc: create_lpar(
            hmc,
            system,
            LparCreation(
                name,
                lpar_type,
                resources,
                partition_id=partition_id,
                max_virtual_slots=max_virtual_slots,
                creation_memory=creation_memory,
                virtual_serial_adapters=serial,
                caller_token=caller_token,
                physical_io=load_physical_io(physical_io),
                apply_profile=not no_apply,
                reporting_settings=reporting,
                vtpm_settings=vtpm,
                security_policy=policy,
                hardware_mem_encryption=hardware_mem_encryption,
                hardware_mem_expansion=hardware_mem_expansion,
                memory_layout=layout,
                affinity_group_id=affinity,
                settings=LparSettings(
                    allow_perf_collection=allow_perf_collection,
                    lpar_avail_priority=lpar_avail_priority,
                    time_ref=time_ref,
                    simplified_remote_restart_capable=simplified_remote_restart_capable,
                    migration_disabled=migration_disabled,
                    powervm_mgmt_capable=powervm_mgmt_capable,
                    kvm_capable=kvm_capable,
                ),
            ),
            assignments,
        )
    )

    console.print(f"Created LPAR '{terminal_field(name)}'", style="green", markup=False)
    print_json(result.lpar)
    for warning in result.warnings:
        err_console.print(
            f"Warning: {terminal_field(warning, err=True)}",
            style="yellow",
            markup=False,
        )
    if result.steps:
        print_json(asdict(result))
    if not result.workflow_completed:
        raise typer.Exit(1)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("create")(lpars_create)
