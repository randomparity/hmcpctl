"""CLI commands for LPAR profile-backed boot configuration."""

from __future__ import annotations

from typing import Any

import typer
from rich.markup import escape

from ...documents.boot import join_boot_device_paths
from ...operations.lpar.boot_order import (
    clear_lpar_boot_order,
    read_lpar_boot_order,
    set_lpar_boot_order,
)
from ..output import console, print_json, terminal_field
from ..runtime import with_client


def _pending_boot_string(document: dict[str, Any] | None) -> str | None:
    """The pending boot string in an updated LPAR document; an empty field parses as a dict."""
    boot_list = ((document or {}).get("Resource") or {}).get(
        "BootListInformation"
    ) or {}
    value = boot_list.get("PendingBootString")
    if isinstance(value, dict):
        value = value.get("text")
    return value if isinstance(value, str) and value else None


def lpars_read_boot_order(
    system_name: str = typer.Argument(..., help="Managed system name"),
    lpar_name_or_uuid: str = typer.Argument(..., help="Logical partition name or UUID"),
) -> None:
    """Read current, pending, and last-used boot-device state for an LPAR.

    Example:
        lpars read-boot-order system1 aaaa0000-0000-0000-0000-000000000001
    """
    result = with_client(
        lambda hmc: read_lpar_boot_order(
            hmc,
            system_name_or_uuid=system_name,
            lpar_name_or_uuid=lpar_name_or_uuid,
        )
    )

    print_json(result)


def lpars_set_boot_order(
    system_name: str = typer.Argument(..., help="Managed system name"),
    lpar_name_or_uuid: str = typer.Argument(..., help="Logical partition name or UUID"),
    devices: list[str] = typer.Argument(
        ...,
        help=(
            "Open Firmware device paths, first to last, as read-boot-order reports them. "
            "A never-booted partition reports none. Firmware names a virtual optical "
            "(ISO) device disk@<LUN>, not cdrom@: "
            "/vdevice/v-scsi@<0x30000000 + client slot, hex>/disk@<LogicalUnitAddress>"
        ),
    ),
    *,
    ownership_override: bool = typer.Option(
        False, "--ownership-override", help="Skip ownership token validation"
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the full updated LPAR document"
    ),
) -> None:
    """Set the pending boot order used on the LPAR's next activation.

    Prints the boot string set and the pending boot string read back from the HMC
    (`-` when the HMC returned none).

    Example:
        lpars set-boot-order system1 lpar-uuid-123 /vdevice/v-scsi@30000002/disk@8100000000000000
    """
    try:
        boot_string = join_boot_device_paths(devices)
    except ValueError as exc:
        raise typer.BadParameter(terminal_field(exc, err=True)) from exc

    result = with_client(
        lambda hmc: set_lpar_boot_order(
            hmc,
            system_name_or_uuid=system_name,
            lpar_name_or_uuid=lpar_name_or_uuid,
            devices=devices,
            ownership_override=ownership_override,
        )
    )

    console.print(
        f"[green]Boot order set to: {escape(terminal_field(boot_string))}[/green]"
    )
    if as_json:
        print_json(result)
        return
    console.print(
        f"Pending boot string: {escape(terminal_field(_pending_boot_string(result) or '-'))}"
    )


def lpars_clear_boot_order(
    system_name: str = typer.Argument(..., help="Managed system name"),
    lpar_name_or_uuid: str = typer.Argument(..., help="Logical partition name or UUID"),
    *,
    ownership_override: bool = typer.Option(
        False, "--ownership-override", help="Skip ownership token validation"
    ),
) -> None:
    """Refuse to clear a pending boot order: no value tried on V10R3 clears it.

    Authorizes, then exits 1 without writing. A profile activation consumed the
    pending boot order when observed on V10R3; set-boot-order replaces it.

    Example:
        lpars clear-boot-order system1 aaaa0000-0000-0000-0000-000000000001
    """
    with_client(
        lambda hmc: clear_lpar_boot_order(
            hmc,
            system_name_or_uuid=system_name,
            lpar_name_or_uuid=lpar_name_or_uuid,
            ownership_override=ownership_override,
        )
    )


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("read-boot-order")(lpars_read_boot_order)
    group.command("set-boot-order")(lpars_set_boot_order)
    group.command("clear-boot-order")(lpars_clear_boot_order)
