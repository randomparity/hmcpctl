"""CLI commands for Virtual I/O Servers."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import typer
from pydantic import TypeAdapter

from hmcpctl.cli_commands.lpar.assignment_input import (
    SECURITY_POLICY_WARNING,
    SERIAL_INPUT_HELP,
    load_security_policy,
    load_virtual_serial_adapters,
)
from hmcpctl.operations.lpar.security_policy import (
    configure_lpar_security_policy,
    read_lpar_security_policy,
)

from ...documents.lpar import VIOS_DEFAULT_RESOURCES, LparResources
from ...documents.lpar_memory_layout import LparMemoryLayout
from ...documents.lpar_serial import validate_serial_input
from ...documents.lpar_vtpm import LparVtpmSettings
from ...jobs import validate_wait_timing
from ...operations.lpar.vtpm import configure_lpar_vtpm, read_lpar_vtpm
from ...operations.partition_state import PartitionState
from ...operations.vios.core import create_vios, list_vios, power_vios
from ..output import (
    InventoryFormat,
    console,
    err_console,
    inventory_field,
    print_inventory,
    print_json,
    report_unreadable,
    resolve_inventory_format,
    terminal_field,
)
from ..runtime import with_client


def vios_list(
    system: str | None = typer.Option(
        None, "--system", "-s", help="Restrict to this managed system name or UUID"
    ),
    state: PartitionState | None = typer.Option(
        None, "--state", help="Filter by PartitionState"
    ),
    as_json: bool = typer.Option(False, "--json", "-j", help="Output JSON"),
    format: InventoryFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: table on TTY, tsv when redirected)",
    ),
) -> None:
    """List Virtual I/O Servers."""
    selected = resolve_inventory_format(as_json, format)
    listing = with_client(lambda hmc: list_vios(hmc, system, state))
    report_unreadable(listing)
    rows = []
    for entry in listing.entries:
        parent = entry.get("ManagedSystem") or {}
        rows.append(
            (
                parent.get("SystemName"),
                parent.get("UUID"),
                inventory_field(entry, "PartitionName"),
                inventory_field(entry, "PartitionID"),
                entry.get("UUID"),
                inventory_field(entry, "PartitionState"),
                inventory_field(entry, "IOSLevel", "VIOSVersion"),
            )
        )
    print_inventory(
        asdict(listing),
        selected,
        ("System", "System UUID", "Name", "ID", "UUID", "State", "Version"),
        rows,
        title="Virtual I/O Servers",
        empty_msg="No VIOS found",
    )


def vios_power_on(
    name_or_uuid: str = typer.Argument(..., help="VIOS name or UUID"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """Power on a VIOS (submits a PowerOn job)."""
    validate_wait_timing(wait, timeout, interval)
    if not yes and not typer.confirm(
        f"Really PowerOn VIOS {terminal_field(name_or_uuid)}?"
    ):
        raise typer.Abort()

    job = with_client(
        lambda hmc: power_vios(
            hmc,
            name_or_uuid,
            system_name_or_uuid=system,
            power_on=True,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
        )
    )

    console.print(
        f"Submitted PowerOn for {terminal_field(name_or_uuid)}",
        style="green",
        markup=False,
    )
    print_json(job)


def vios_power_off(
    name_or_uuid: str = typer.Argument(..., help="VIOS name or UUID"),
    immediate: bool = typer.Option(False, "--immediate"),
    wait: bool = typer.Option(
        False, "--wait/--no-wait", help="Wait for job completion"
    ),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (with --wait)"),
    interval: int = typer.Option(
        5, "--interval", help="Poll interval seconds (with --wait)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    system: str | None = typer.Option(
        None, "--system", "-s", help="Managed system name or UUID"
    ),
) -> None:
    """Power off a VIOS (submits a PowerOff job)."""
    validate_wait_timing(wait, timeout, interval)
    op = "Immediate PowerOff" if immediate else "PowerOff"
    if not yes and not typer.confirm(
        f"Really {terminal_field(op)} VIOS {terminal_field(name_or_uuid)}?"
    ):
        raise typer.Abort()

    job = with_client(
        lambda hmc: power_vios(
            hmc,
            name_or_uuid,
            system_name_or_uuid=system,
            power_on=False,
            immediate=immediate,
            wait=wait,
            timeout_seconds=timeout,
            poll_interval=interval,
        )
    )

    console.print(
        f"Submitted {terminal_field(op)} for {terminal_field(name_or_uuid)}",
        style="green",
        markup=False,
    )
    print_json(job)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list")(vios_list)
    group.command("create")(vios_create)
    group.command("get-vtpm-settings")(vios_get_vtpm_settings)
    group.command("get-security-policy", short_help="Read policy.")(
        vios_get_security_policy
    )
    group.command("configure-security-policy", short_help="Set policy.")(
        vios_configure_security_policy
    )
    group.command("configure-vtpm")(vios_configure_vtpm)
    group.command("power-on")(vios_power_on)
    group.command("power-off")(vios_power_off)


def vios_create(
    name: str = typer.Argument(...),
    system: str = typer.Option(..., "--system", "-s"),
    resources: str | None = typer.Option(
        None, "--resources", help="Typed resource JSON; omitted uses VIOS defaults"
    ),
    vtpm_settings: str | None = typer.Option(
        None, "--vtpm-settings", help="Three-field typed vTPM JSON"
    ),
    security_policy: str | None = typer.Option(
        None,
        "--security-policy",
        help="Inline strict policy JSON (64 KiB); signed enablement irreversible; no guest/key proof",
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
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
) -> None:
    """Create an inactive VIOS through its existing owner."""
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
            hpt_ratio=hpt_ratio,
            ppt_ratio=ppt_ratio,
        )
        layout.validate_creation("Virtual IO Server")
    except ValueError as error:
        raise typer.BadParameter(
            terminal_field(error, err=True), param_hint="memory layout"
        ) from None
    policy = load_security_policy(security_policy)
    try:
        policy.validate_creation("Virtual IO Server")
    except ValueError as error:
        raise typer.BadParameter(
            terminal_field(error, err=True), param_hint="--security-policy"
        ) from None
    if policy.pairs():
        err_console.print(
            terminal_field(SECURITY_POLICY_WARNING, err=True), markup=False
        )
    try:
        resource_values = (
            VIOS_DEFAULT_RESOURCES
            if resources is None
            else TypeAdapter(LparResources).validate_json(resources)
        )
        settings = (
            None
            if vtpm_settings is None
            else TypeAdapter(LparVtpmSettings).validate_json(vtpm_settings)
        )
    except ValueError as error:
        raise typer.BadParameter(terminal_field(error, err=True)) from error
    if not yes:
        typer.confirm(
            f"Create VIOS {terminal_field(name)} on {terminal_field(system)}?",
            abort=True,
        )
    result = with_client(
        lambda hmc: create_vios(
            hmc,
            system,
            name,
            resource_values,
            vtpm_settings=settings,
            security_policy=policy,
            hardware_mem_encryption=hardware_mem_encryption,
            hardware_mem_expansion=hardware_mem_expansion,
            memory_layout=layout,
            max_virtual_slots=max_virtual_slots,
            virtual_serial_adapters=serial,
        )
    )
    print_json(result)


def vios_get_vtpm_settings(
    vios_name: str = typer.Argument(...), system_name: str = typer.Argument(...)
) -> None:
    """Read contained VIOS vTPM settings."""
    result = with_client(
        lambda hmc: read_lpar_vtpm(
            hmc, vios_name, system_name_or_uuid=system_name, vios=True
        )
    )
    print_json(asdict(result))


def vios_configure_vtpm(
    vios_name: str = typer.Argument(...),
    system_name: str = typer.Argument(...),
    vtpm_enabled: bool | None = typer.Option(None, "--vtpm-enabled/--no-vtpm-enabled"),
    vtpm_version: str | None = typer.Option(None, "--vtpm-version"),
    vtpm_encryption: str | None = typer.Option(None, "--vtpm-encryption"),
    acknowledge_data_loss: bool = typer.Option(
        False,
        "--acknowledge-data-loss",
        help="Consent to key/data loss on enabled disable",
    ),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
) -> None:
    """Configure inactive VIOS vTPM without implicit reset, power or profile action."""
    try:
        settings = LparVtpmSettings(vtpm_enabled, vtpm_version, vtpm_encryption)
    except ValueError as error:
        raise typer.BadParameter(terminal_field(error, err=True)) from error
    result = with_client(
        lambda hmc: configure_lpar_vtpm(
            hmc,
            vios_name,
            settings,
            system_name_or_uuid=system_name,
            vios=True,
            acknowledge_data_loss=acknowledge_data_loss,
            ownership_override=ownership_override,
        )
    )
    print_json(asdict(result))
    if result.status != "verified":
        raise typer.Exit(1)


def vios_get_security_policy(
    vios_name: str = typer.Argument(...), system_name: str = typer.Argument(...)
) -> None:
    """Read pending security policy and separate current secure boot; no runtime proof."""
    result = with_client(
        lambda hmc: read_lpar_security_policy(
            hmc, vios_name, system_name_or_uuid=system_name, vios=True
        )
    )
    print_json(asdict(result))


def vios_configure_security_policy(
    vios_name: str = typer.Argument(...),
    system_name: str = typer.Argument(...),
    security_policy: str = typer.Option(
        ...,
        "--security-policy",
        help="Strict inline policy JSON (64 KiB maximum); no filename",
    ),
    ownership_override: bool = typer.Option(False, "--ownership-override"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Confirm the security-policy consequences"
    ),
) -> None:
    """Configure security policy without power, profile or key actions.

    Secure boot may be weakened. Signed/Linux enables are irreversible; store
    enable/increase requires inactivity, nonzero shrink requires Power11, and
    disabling a store requires HMC emptiness checks. PK bypass skips old-PK
    authentication. Linux selection declares intent only, without guest proof.
    Pending policy may require restart; readback proves no runtime enforcement.
    """
    settings = load_security_policy(security_policy)
    if not settings.pairs():
        raise typer.BadParameter("Supply at least one security policy setting")
    err_console.print(terminal_field(SECURITY_POLICY_WARNING, err=True), markup=False)
    if not yes:
        typer.confirm(
            "Change security policy on the selected partition?",
            abort=True,
        )
    result = with_client(
        lambda hmc: configure_lpar_security_policy(
            hmc,
            vios_name,
            settings,
            system_name_or_uuid=system_name,
            ownership_override=ownership_override,
            vios=True,
        )
    )
    print_json(asdict(result))
    if result.status != "verified":
        raise typer.Exit(1)
