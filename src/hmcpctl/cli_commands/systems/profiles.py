"""System-profile commands sharing the domain's typed outcomes."""

from __future__ import annotations

from dataclasses import asdict

import typer
from pydantic import ValidationError

from ...operations.systems.profiles import (
    Action,
    SystemProfileInventory,
    SystemProfileOutcome,
    SystemProfilePair,
    activate_system_profile,
    create_system_profile,
    delete_system_profile,
    list_system_profiles,
    update_system_profile,
)
from ..output import console, print_json
from ..runtime import with_client


def _pairs(values: list[str]) -> list[SystemProfilePair]:
    try:
        return [SystemProfilePair.model_validate_json(v) for v in values]
    except ValidationError as exc:
        raise typer.BadParameter(
            "each --member requires exactly partition/profile string keys"
        ) from exc


def _show(result: SystemProfileInventory | SystemProfileOutcome, as_json: bool) -> None:
    data = asdict(result)
    if as_json:
        print_json(data)
    else:
        console.print(data, markup=False)
    state = (
        result.command_status
        if isinstance(result, SystemProfileInventory)
        else result.state
    )
    exit_code = 1 if state == "failed" else 2 if state != "completed" else 0
    if (
        isinstance(result, SystemProfileOutcome)
        and result.postcondition not in {"verified", "not-applicable"}
        and not exit_code
    ):
        exit_code = 2
    if exit_code:
        raise typer.Exit(exit_code)


def profile_list(
    system: str = typer.Argument(...),
    name: str | None = typer.Option(None, "--name"),
    timeout_seconds: int = typer.Option(300),
    poll_interval: int = typer.Option(5),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List bound whole inventory, optionally selecting one exact name."""
    _show(
        with_client(
            lambda hmc: list_system_profiles(
                hmc,
                system,
                name=name,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )
        ),
        as_json,
    )


def profile_create(
    system: str = typer.Argument(...),
    name: str = typer.Argument(...),
    member: list[str] = typer.Option(..., "--member"),
    exclusive_writer_window: bool = typer.Option(False),
    ownership_override: bool = typer.Option(False),
    timeout_seconds: int = typer.Option(300),
    poll_interval: int = typer.Option(5),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Create explicit partition/profile associations under caller coordination."""
    pairs = _pairs(member)
    _show(
        with_client(
            lambda hmc: create_system_profile(
                hmc,
                system,
                name,
                pairs,
                exclusive_writer_window=exclusive_writer_window,
                ownership_override=ownership_override,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )
        ),
        as_json,
    )


def profile_update(
    system: str = typer.Argument(...),
    name: str = typer.Argument(...),
    new_name: str | None = typer.Option(None),
    member: list[str] | None = typer.Option(None, "--member"),
    exclusive_writer_window: bool = typer.Option(False),
    ownership_override: bool = typer.Option(False),
    timeout_seconds: int = typer.Option(300),
    poll_interval: int = typer.Option(5),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Rename, replace paired membership, or both."""
    pairs = _pairs(member) if member is not None else None
    _show(
        with_client(
            lambda hmc: update_system_profile(
                hmc,
                system,
                name,
                new_name=new_name,
                members=pairs,
                exclusive_writer_window=exclusive_writer_window,
                ownership_override=ownership_override,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )
        ),
        as_json,
    )


def profile_delete(
    system: str = typer.Argument(...),
    name: str = typer.Argument(...),
    exclusive_writer_window: bool = typer.Option(False),
    ownership_override: bool = typer.Option(False),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Delete one exact system profile using its selected CLI route."""
    _show(
        with_client(
            lambda hmc: delete_system_profile(
                hmc,
                system,
                name,
                exclusive_writer_window=exclusive_writer_window,
                ownership_override=ownership_override,
            )
        ),
        as_json,
    )


def profile_activate(
    system: str = typer.Argument(...),
    name: str = typer.Argument(...),
    action: Action = typer.Option("activate"),
    continue_on_error: bool = typer.Option(False),
    exclusive_writer_window: bool = typer.Option(False),
    ownership_override: bool = typer.Option(False),
    timeout_seconds: int = typer.Option(300),
    poll_interval: int = typer.Option(5),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Validate, activate, or combine both once; uncertainty remains visible."""
    _show(
        with_client(
            lambda hmc: activate_system_profile(
                hmc,
                system,
                name,
                action=action,
                continue_on_error=continue_on_error,
                exclusive_writer_window=exclusive_writer_window,
                ownership_override=ownership_override,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )
        ),
        as_json,
    )


def register_commands(group: typer.Typer) -> None:
    group.command("profile-create")(profile_create)
    group.command("profile-list")(profile_list)
    group.command("profile-update")(profile_update)
    group.command("profile-delete")(profile_delete)
    group.command("profile-activate")(profile_activate)
