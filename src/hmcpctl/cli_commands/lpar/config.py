"""CLI commands for LPAR configuration and affinity."""

from __future__ import annotations

from dataclasses import asdict

import typer
from rich.text import Text

from hmcpctl.operations.lpar.configuration import configure_lpar_msp
from hmcpctl.operations.lpar.ownership import set_lpar_ownership_description

from ...operations.affinity.ssh import (
    get_lpar_memopt_score,
    get_minimum_affinity_policy,
    get_system_memopt_score,
    list_lpar_memopt_scores,
    list_resource_group_memopt_scores,
    plan_lpar_memopt_scores,
    plan_resource_group_memopt_scores,
    plan_system_memopt_score,
)
from ...operations.lpar.core import (
    ProcessorCompatibilityMode,
)
from ...ssh.affinity import (
    MemoptLparSelector,
    MemoptResourceGroupSelector,
    validate_memopt_scenario,
)
from ...ssh.profiles import (
    get_lpar_description,
    get_lpar_msp,
    get_lpar_proc_compat,
    get_proc_compat_modes,
    set_lpar_proc_compat,
)
from ..output import VerbatimTable, console, print_json, terminal_field, usage_error
from ..runtime import run_cli_coroutine, ssh_config, with_client


def _with_ssh_affinity(operation, *args):
    """Run an affinity operation directly against SSH configuration."""
    return run_cli_coroutine(lambda: operation(ssh_config(), *args))


def _memopt_selectors(
    prioritize_name: list[str] | None,
    prioritize_id: list[int] | None,
    exclude_name: list[str] | None,
    exclude_id: list[int] | None,
) -> tuple[MemoptLparSelector | None, MemoptLparSelector | None]:
    """Build shared planning selectors and fail as CLI usage errors."""
    prioritize_name = prioritize_name or []
    prioritize_id = prioritize_id or []
    exclude_name = exclude_name or []
    exclude_id = exclude_id or []
    try:
        prioritized = (
            MemoptLparSelector(tuple(prioritize_name), tuple(prioritize_id))
            if prioritize_name or prioritize_id
            else None
        )
        excluded = (
            MemoptLparSelector(tuple(exclude_name), tuple(exclude_id))
            if exclude_name or exclude_id
            else None
        )
        validate_memopt_scenario(prioritized, excluded)
        return prioritized, excluded
    except ValueError as error:
        usage_error(str(error))


def lpars_memopt_score(
    lpar_name: str = typer.Argument(..., help="LPAR name or UUID"),
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Get an LPAR's current memory-optimization affinity score."""
    score = _with_ssh_affinity(get_lpar_memopt_score, system_name, lpar_name)
    if as_json:
        print_json(score)
    else:
        console.print(
            (
                f"{terminal_field(score['lpar_name'])} (id {terminal_field(score['lpar_id'])}"
                f"): curr_lpar_score={terminal_field(score['curr_lpar_score'])}"
            ),
            markup=False,
        )


def lpars_get_minimum_affinity_policy(
    lpar_name: str = typer.Argument(..., help="LPAR name or UUID"),
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Get an LPAR's minimum-affinity policy when supported."""
    policy = _with_ssh_affinity(get_minimum_affinity_policy, system_name, lpar_name)
    if as_json:
        print_json(asdict(policy))
        return
    if policy.capability == "capability-unavailable":
        console.print(
            f"unavailable: {terminal_field(policy.unavailable_reason)}", markup=False
        )
        return
    console.print(
        (
            f"minimum affinity score: {terminal_field(policy.min_affinity_score)} ("
            f"{terminal_field(policy.min_affinity_score_action)})"
        ),
        markup=False,
    )


def lpars_memopt_scores(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    lpar_name: str | None = typer.Option(
        None, "--lpar", help="Filter by LPAR name or UUID"
    ),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """List current memory-optimization affinity scores for a system's LPARs."""
    scores = _with_ssh_affinity(list_lpar_memopt_scores, system_name, lpar_name)
    if as_json:
        print_json(scores)
        return
    if not scores:
        console.print("[yellow]No memory-optimization scores reported[/yellow]")
        return
    table = VerbatimTable(title=f"Memory-optimization scores on {system_name}")
    for column in ("lpar_name", "lpar_id", "curr_lpar_score"):
        table.add_column(column)
    for row in scores:
        table.add_row(
            str(row.get("lpar_name", "")),
            str(row.get("lpar_id", "")),
            str(row.get("curr_lpar_score", "")),
        )
    console.print(table)


def lpars_system_memopt_score(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Get a managed system's current memory-optimization affinity score."""
    score = _with_ssh_affinity(get_system_memopt_score, system_name)
    if as_json:
        print_json(score)
        return
    console.print(f"current: {terminal_field(score['curr_sys_score'])}", markup=False)


def _run_memopt_plan(
    operation,
    system_name: str,
    prioritize_name: list[str] | None,
    prioritize_id: list[int] | None,
    exclude_name: list[str] | None,
    exclude_id: list[int] | None,
):
    prioritized, excluded = _memopt_selectors(
        prioritize_name, prioritize_id, exclude_name, exclude_id
    )
    return _with_ssh_affinity(operation, system_name, prioritized, excluded)


def _resource_group_selector(
    names: list[str] | None, ids: list[int] | None, all_groups: bool
) -> MemoptResourceGroupSelector:
    modes = sum((bool(names), bool(ids), all_groups))
    if modes > 1:
        usage_error(
            "Use only one of --resource-group-name, --resource-group-id, or --all"
        )
    try:
        if names:
            return MemoptResourceGroupSelector(names=tuple(names))
        if ids:
            return MemoptResourceGroupSelector(ids=tuple(ids))
        return MemoptResourceGroupSelector(all=True)
    except ValueError as error:
        usage_error(str(error))


def _run_resource_group_memopt(
    operation,
    system_name: str,
    names: list[str] | None,
    ids: list[int] | None,
    all_groups: bool,
    as_json: bool,
) -> None:
    selector = _resource_group_selector(names, ids, all_groups)
    result = with_client(lambda hmc: operation(hmc, system_name, selector))
    if as_json:
        print_json(asdict(result))
        return
    if result.capability == "capability-unavailable":
        console.print(
            Text.assemble(
                ("Capability unavailable:", "yellow"),
                f" {terminal_field(result.unavailable_reason)}",
            )
        )
        return
    if not result.items:
        console.print("[yellow]No resource-group affinity scores reported[/yellow]")
        return
    for item in result.items:
        line = (
            f"{item['resource_group_name']} (id {item['resource_group_id']}): "
            f"current: {item['curr_score']}"
        )
        if result.mode == "calculated":
            line += f"; predicted: {item['predicted_score']}; prediction guaranteed: no"
        console.print(terminal_field(line), markup=False)


def lpars_resource_group_memopt_scores(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    resource_group_name: list[str] | None = typer.Option(None, "--resource-group-name"),
    resource_group_id: list[int] | None = typer.Option(None, "--resource-group-id"),
    all_groups: bool = typer.Option(False, "--all"),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """List current resource-group affinity scores when supported."""
    _run_resource_group_memopt(
        list_resource_group_memopt_scores,
        system_name,
        resource_group_name,
        resource_group_id,
        all_groups,
        as_json,
    )


def lpars_plan_resource_group_memopt_scores(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    resource_group_name: list[str] | None = typer.Option(None, "--resource-group-name"),
    resource_group_id: list[int] | None = typer.Option(None, "--resource-group-id"),
    all_groups: bool = typer.Option(False, "--all"),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Calculate potential resource-group affinity scores without running DPO."""
    _run_resource_group_memopt(
        plan_resource_group_memopt_scores,
        system_name,
        resource_group_name,
        resource_group_id,
        all_groups,
        as_json,
    )


def lpars_plan_memopt_scores(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    prioritize_name: list[str] | None = typer.Option(None, "--prioritize-name"),
    prioritize_id: list[int] | None = typer.Option(None, "--prioritize-id"),
    exclude_name: list[str] | None = typer.Option(None, "--exclude-name"),
    exclude_id: list[int] | None = typer.Option(None, "--exclude-id"),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Predict LPAR affinity scores without applying optimization."""
    scores = _run_memopt_plan(
        plan_lpar_memopt_scores,
        system_name,
        prioritize_name,
        prioritize_id,
        exclude_name,
        exclude_id,
    )
    if as_json:
        print_json(scores)
        return
    for score in scores:
        console.print(
            (
                f"{terminal_field(score['lpar_name'])} (id {terminal_field(score['lpar_id'])}"
                f"): current: {terminal_field(score['curr_lpar_score'])}; predicted: "
                f"{terminal_field(score['predicted_lpar_score'])}; prediction guaranteed: no"
            ),
            markup=False,
        )


def lpars_plan_system_memopt_score(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    prioritize_name: list[str] | None = typer.Option(None, "--prioritize-name"),
    prioritize_id: list[int] | None = typer.Option(None, "--prioritize-id"),
    exclude_name: list[str] | None = typer.Option(None, "--exclude-name"),
    exclude_id: list[int] | None = typer.Option(None, "--exclude-id"),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Predict system affinity score without applying optimization."""
    score = _run_memopt_plan(
        plan_system_memopt_score,
        system_name,
        prioritize_name,
        prioritize_id,
        exclude_name,
        exclude_id,
    )
    if as_json:
        print_json(score)
        return
    console.print(
        (
            f"current: {terminal_field(score['curr_sys_score'])}; predicted: "
            f"{terminal_field(score['predicted_sys_score'])}; prediction guaranteed: no"
        ),
        markup=False,
    )


def lpars_get_description(
    lpar_name: str = typer.Argument(..., help="LPAR name"),
    system_name: str = typer.Argument(..., help="Managed system name"),
) -> None:
    """Get the description field of an LPAR (HMC CLI via SSH)."""
    result = run_cli_coroutine(
        lambda: get_lpar_description(ssh_config(), system_name, lpar_name)
    )

    console.print(
        terminal_field(result.strip() or "(no description set)"), markup=False
    )


def lpars_set_description(
    lpar_name: str = typer.Argument(..., help="LPAR name"),
    system_name: str = typer.Argument(..., help="Managed system name"),
    description: str = typer.Argument(
        ...,
        help="New description text (printable ASCII, no HMC attribute-record "
        "structure; the error names the character). Written after the current "
        "ownership stamp unless it carries its own complete stamp",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass ownership protection and replace the whole description "
        "after operator approval",
    ),
) -> None:
    """Set the description field of an LPAR (HMC CLI via SSH)."""
    if not yes and not typer.confirm(
        f"Set description on '{terminal_field(lpar_name)}' (system {terminal_field(system_name)})?"
    ):
        raise typer.Abort()

    result = with_client(
        lambda hmc: set_lpar_ownership_description(
            hmc,
            system_name,
            lpar_name,
            description,
            ownership_override=ownership_override,
        )
    )

    console.print(
        f"Description updated for '{terminal_field(lpar_name)}'",
        style="green",
        markup=False,
    )
    if result.strip():
        console.print(terminal_field(result.strip()), markup=False)


def lpars_get_msp(
    lpar_name: str = typer.Argument(..., help="LPAR name"),
    system_name: str = typer.Argument(..., help="Managed system name"),
) -> None:
    """Get the MSP (Migratable Service Partition) flag of an LPAR (HMC CLI via SSH)."""
    enabled = run_cli_coroutine(
        lambda: get_lpar_msp(ssh_config(), system_name, lpar_name)
    )

    console.print("enabled" if enabled else "disabled")


def lpars_set_msp(
    lpar_name: str = typer.Argument(..., help="VIOS partition name or UUID"),
    system_name: str = typer.Argument(..., help="Managed system name"),
    enabled: bool = typer.Argument(..., help="True to enable MSP, False to disable"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    ownership_override: bool = typer.Option(
        False,
        "--ownership-override",
        help="Bypass ownership protection after operator approval",
    ),
) -> None:
    """Set the MSP (Migratable Service Partition) flag of a VIOS (HMC CLI via SSH)."""
    if not yes and not typer.confirm(
        f"Set MSP={terminal_field('1' if enabled else '0')} on '"
        f"{terminal_field(lpar_name)}' (system {terminal_field(system_name)})?"
    ):
        raise typer.Abort()
    result = with_client(
        lambda hmc: configure_lpar_msp(
            hmc,
            system_name,
            lpar_name,
            enabled,
            ownership_override=ownership_override,
        )
    )

    console.print(
        f"MSP updated for '{terminal_field(lpar_name)}'", style="green", markup=False
    )
    if result.strip():
        console.print(terminal_field(result.strip()), markup=False)


def lpars_get_proc_compat_modes(
    system_name: str = typer.Argument(..., help="Managed system name"),
) -> None:
    """Get processor compatibility modes supported by a managed system (HMC CLI via SSH)."""
    modes = run_cli_coroutine(lambda: get_proc_compat_modes(ssh_config(), system_name))

    console.print(
        terminal_field(",".join(modes) or "(no modes returned)"), markup=False
    )


def lpars_get_proc_compat(
    lpar_name: str = typer.Argument(..., help="LPAR name"),
    system_name: str = typer.Argument(..., help="Managed system name"),
    profile_name: str | None = typer.Option(
        None, "--profile-name", help="Partition profile to read (default profile)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """Get an LPAR's processor compatibility modes and a profile's mode (HMC CLI via SSH)."""
    info = run_cli_coroutine(
        lambda: get_lpar_proc_compat(ssh_config(), system_name, lpar_name, profile_name)
    )

    desired = info["desired"]
    curr = info["curr"]

    if as_json:
        print_json(info)
    else:
        table = VerbatimTable(title=f"Processor Compatibility Mode: {lpar_name}")
        table.add_column("Property", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Desired Mode", desired or "-")
        table.add_row("Current Mode", curr or "-")
        table.add_row(
            f"Profile Mode ({info['profile'] or '-'})", info["profile_mode"] or "-"
        )
        console.print(table)


def lpars_set_proc_compat(
    lpar_name: str = typer.Argument(..., help="LPAR name"),
    system_name: str = typer.Argument(..., help="Managed system name"),
    mode: ProcessorCompatibilityMode = typer.Argument(
        ..., help="Processor compatibility mode supported by the managed system"
    ),
    profile_name: str | None = typer.Option(
        None, "--profile-name", help="Partition profile to change (default profile)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Set the processor compatibility mode on an LPAR profile (HMC CLI via SSH)."""
    target = f"profile '{profile_name}'" if profile_name else "the default profile"
    if not yes and not typer.confirm(
        f"Set processor compatibility mode to '{terminal_field(mode)}' on "
        f"{terminal_field(target)} of LPAR '{terminal_field(lpar_name)}' (system "
        f"{terminal_field(system_name)})?"
    ):
        raise typer.Abort()
    profile = run_cli_coroutine(
        lambda: set_lpar_proc_compat(
            ssh_config(), system_name, lpar_name, mode, profile_name
        )
    )

    console.print(
        (
            f"Processor compatibility mode updated on profile '{terminal_field(profile)}"
            f"' of '{terminal_field(lpar_name)}'"
        ),
        style="green",
        markup=False,
    )


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("memopt-score")(lpars_memopt_score)
    group.command("get-minimum-affinity-policy")(lpars_get_minimum_affinity_policy)
    group.command("memopt-scores")(lpars_memopt_scores)
    group.command("system-memopt-score")(lpars_system_memopt_score)
    group.command("resource-group-memopt-scores")(lpars_resource_group_memopt_scores)
    group.command("plan-resource-group-memopt-scores")(
        lpars_plan_resource_group_memopt_scores
    )
    group.command("plan-memopt-scores")(lpars_plan_memopt_scores)
    group.command("plan-system-memopt-score")(lpars_plan_system_memopt_score)
    group.command("get-description")(lpars_get_description)
    group.command("set-description")(lpars_set_description)
    group.command("get-msp")(lpars_get_msp)
    group.command("set-msp")(lpars_set_msp)
    group.command("get-proc-compat-modes")(lpars_get_proc_compat_modes)
    group.command("get-proc-compat")(lpars_get_proc_compat)
    group.command("set-proc-compat")(lpars_set_proc_compat)
