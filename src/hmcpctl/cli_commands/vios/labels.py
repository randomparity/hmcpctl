"""CLI commands for bounded VIOS Fibre Channel label administration."""

from __future__ import annotations

import sys
from contextlib import redirect_stdout

import typer

from ...operations.vios.labels import (
    create_vios_vfc_group_label,
    list_vios_fc_port_labels,
    list_vios_vfc_group_labels,
    remove_vios_fc_port_label,
    remove_vios_vfc_group_label,
    set_vios_fc_port_label,
    update_vios_vfc_group_label,
)
from ...ssh.vios_labels import ViosGroupUpdateAction
from ..output import output, print_json, terminal_field
from ..runtime import run_cli_coroutine, ssh_config


def _confirm_on_stderr(prompt: str) -> bool:
    with redirect_stdout(sys.stderr):
        return typer.confirm(terminal_field(prompt, err=True), err=True)


def _prompt_value(value: object) -> str:
    """Render caller input without allowing terminal control structure."""
    return ascii(value)


def _selected(vios_name: str | None, vios_id: int | None) -> str:
    if vios_name is not None:
        return f"vios_name={_prompt_value(vios_name)}"
    return f"vios_id={_prompt_value(vios_id)}"


def _members(vios_names: list[str] | None, vios_ids: list[int] | None) -> str:
    if vios_names is not None:
        return f"vios_names={_prompt_value(vios_names)}"
    return f"vios_ids={_prompt_value(vios_ids)}"


def vios_list_fc_port_labels(
    system_name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    vios_name: str | None = typer.Option(None, "--vios-name"),
    vios_id: int | None = typer.Option(None, "--vios-id"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List FC-port labels on a managed system."""
    rows = run_cli_coroutine(
        lambda: list_vios_fc_port_labels(
            ssh_config(), system_name_or_uuid, vios_name=vios_name, vios_id=vios_id
        )
    )
    output(rows, as_json, None, "No VIOS FC-port labels found")


def vios_set_fc_port_label(
    system_name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    port_name: str = typer.Argument(..., help="VIOS FC port name"),
    label: str = typer.Argument(..., help="New FC-port label"),
    vios_name: str | None = typer.Option(None, "--vios-name"),
    vios_id: int | None = typer.Option(None, "--vios-id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Set one FC-port label without changing adapter configuration."""
    if not yes and not _confirm_on_stderr(
        f"Set FC-port label on system={_prompt_value(system_name_or_uuid)}, "
        f"port={_prompt_value(port_name)}, {_selected(vios_name, vios_id)}, "
        f"label={_prompt_value(label)}?"
    ):
        raise typer.Abort()
    result = run_cli_coroutine(
        lambda: set_vios_fc_port_label(
            ssh_config(),
            system_name_or_uuid,
            label,
            port_name,
            vios_name=vios_name,
            vios_id=vios_id,
        )
    )
    print_json(result)


def vios_remove_fc_port_label(
    system_name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    port_name: str = typer.Argument(..., help="VIOS FC port name"),
    vios_name: str | None = typer.Option(None, "--vios-name"),
    vios_id: int | None = typer.Option(None, "--vios-id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove one FC-port label without deleting the FC adapter."""
    if not yes and not _confirm_on_stderr(
        f"Remove FC-port label on system={_prompt_value(system_name_or_uuid)}, "
        f"port={_prompt_value(port_name)}, {_selected(vios_name, vios_id)}?"
    ):
        raise typer.Abort()
    result = run_cli_coroutine(
        lambda: remove_vios_fc_port_label(
            ssh_config(),
            system_name_or_uuid,
            port_name,
            vios_name=vios_name,
            vios_id=vios_id,
        )
    )
    print_json(result)


def vios_list_vfc_group_labels(
    system_name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List vFC placement group labels on a managed system."""
    rows = run_cli_coroutine(
        lambda: list_vios_vfc_group_labels(ssh_config(), system_name_or_uuid)
    )
    output(rows, as_json, None, "No VIOS vFC group labels found")


def vios_create_vfc_group_label(
    system_name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    label: str = typer.Argument(..., help="New vFC group label, at most 16 characters"),
    vios_names: list[str] | None = typer.Option(None, "--vios-name"),
    vios_ids: list[int] | None = typer.Option(None, "--vios-id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Create one vFC placement group without changing adapters."""
    if not yes and not _confirm_on_stderr(
        f"Create vFC group on system={_prompt_value(system_name_or_uuid)}, "
        f"label={_prompt_value(label)}, "
        f"{_members(vios_names, vios_ids)}?"
    ):
        raise typer.Abort()
    result = run_cli_coroutine(
        lambda: create_vios_vfc_group_label(
            ssh_config(),
            system_name_or_uuid,
            label,
            vios_names=vios_names,
            vios_ids=vios_ids,
        )
    )
    print_json(result)


def vios_update_vfc_group_label(
    system_name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    label: str = typer.Argument(..., help="Existing vFC group label"),
    action: ViosGroupUpdateAction = typer.Option(..., "--action"),
    new_name: str | None = typer.Option(
        None, "--new-name", help="Replacement label for rename, at most 16 characters"
    ),
    vios_names: list[str] | None = typer.Option(None, "--vios-name"),
    vios_ids: list[int] | None = typer.Option(None, "--vios-id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Rename or change membership of one vFC placement group."""
    detail = (
        f"new_name={_prompt_value(new_name)}"
        if action == "rename"
        else _members(vios_names, vios_ids)
    )
    if not yes and not _confirm_on_stderr(
        f"Update vFC group on system={_prompt_value(system_name_or_uuid)}, "
        f"label={_prompt_value(label)}, action={_prompt_value(action)}, {detail}?"
    ):
        raise typer.Abort()
    result = run_cli_coroutine(
        lambda: update_vios_vfc_group_label(
            ssh_config(),
            system_name_or_uuid,
            label,
            action,
            new_name=new_name,
            vios_names=vios_names,
            vios_ids=vios_ids,
        )
    )
    print_json(result)


def vios_remove_vfc_group_label(
    system_name_or_uuid: str = typer.Argument(..., help="Managed system name or UUID"),
    label: str = typer.Argument(..., help="Existing vFC group label"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove one named vFC placement group without deleting adapters."""
    if not yes and not _confirm_on_stderr(
        f"Remove vFC group on system={_prompt_value(system_name_or_uuid)}, "
        f"label={_prompt_value(label)}?"
    ):
        raise typer.Abort()
    result = run_cli_coroutine(
        lambda: remove_vios_vfc_group_label(ssh_config(), system_name_or_uuid, label)
    )
    print_json(result)


def register_commands(group: typer.Typer) -> None:
    """Register this module's commands on *group*."""
    group.command("list-fc-port-labels")(vios_list_fc_port_labels)
    group.command("set-fc-port-label")(vios_set_fc_port_label)
    group.command("remove-fc-port-label")(vios_remove_fc_port_label)
    group.command("list-vfc-group-labels")(vios_list_vfc_group_labels)
    group.command("create-vfc-group-label")(vios_create_vfc_group_label)
    group.command("update-vfc-group-label")(vios_update_vfc_group_label)
    group.command("remove-vfc-group-label")(vios_remove_vfc_group_label)
