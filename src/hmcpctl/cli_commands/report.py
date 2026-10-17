"""Fleet reports surveyed across every configured connection profile (ADR 0184)."""

from __future__ import annotations

import csv
import math
import os
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import typer
from pydantic import ValidationError
from rich.text import Text

from hmcpctl.client.core import HMCClient
from hmcpctl.config import ConfigError, config_inventory, env_var_value, load_profile
from hmcpctl.operations.inventory.utilization import (
    FIGURE_GROUPS,
    FleetSurvey,
    FleetSystem,
    Rollup,
    SystemReading,
    allocated,
    capacity_pairs,
    fleet_systems,
    rollup,
    survey_fleet,
    utilization_pct,
)

from .output import err_console, fail, terminal_field, usage_error
from .report_html import render_html
from .runtime import current_options, run_cli_coroutine

COLUMNS = (
    "row_type",
    "profiles",
    "system",
    "machine_type",
    "model",
    "serial",
    "firmware",
    "state",
    "systems",
    "cpu_installed",
    "cpu_configurable",
    "cpu_vios",
    "cpu_client_active",
    "cpu_idle_reserved",
    "cpu_other_reserved",
    "cpu_free",
    "cpu_dedicated",
    "cpu_shared",
    "cpu_allocated",
    "cpu_util_pct",
    "shared_pools",
    "mem_installed_mib",
    "mem_configurable_mib",
    "mem_hypervisor_mib",
    "mem_vios_mib",
    "mem_client_active_mib",
    "mem_idle_reserved_mib",
    "mem_other_reserved_mib",
    "mem_free_mib",
    "mem_allocated_mib",
    "mem_util_pct",
    "partitions_running",
    "partitions_not_activated",
    "partitions_other",
    "profile_claims",
    "profile_claim_mem_mib",
    "profile_claim_cpu",
    "notes",
    "disk_internal_total_mib",
    "disk_internal_assigned_mib",
    "disk_internal_free_mib",
    "disk_san_total_mib",
    "disk_san_assigned_mib",
    "disk_san_free_mib",
    "disk_util_pct",
    "slots_assigned",
    "slots_unassigned",
    "slots_sriov",
    "slots_empty",
    "slots_util_pct",
    "sriov_adapters",
    "sriov_logical_ports",
    "sriov_logical_ports_free",
    "sriov_util_pct",
)
# Utilizations beyond CPU and memory, each a capacity_pairs key with a *_util_pct column.
_UTILIZATIONS = ("disk", "slots", "sriov")
_UNKNOWN = "unknown"
# An exported value would override every profile's own, sending one HMC's host,
# credentials or TLS setting to all of them (load_profile's env-over-TOML precedence).
_PROFILE_OVERRIDES = (
    "HMC_HOST",
    "HMC_USER",
    "HMC_PASSWORD",
    "HMC_PORT",
    "HMC_VERIFY_SSL",
)
# Columns holding HMC- or operator-supplied text, which a spreadsheet would evaluate
# when it starts with a formula character.
_TEXT_COLUMNS = (
    "profiles",
    "system",
    "machine_type",
    "model",
    "serial",
    "firmware",
    "state",
    "notes",
)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_PARTITION_COLUMNS = {
    "running": "partitions_running",
    "not_activated": "partitions_not_activated",
    "other": "partitions_other",
    "profile_claims": "profile_claims",
    "profile_claim_memory": "profile_claim_mem_mib",
    "profile_claim_units": "profile_claim_cpu",
}


def _cell(value: object) -> str:
    if value is None:
        return _UNKNOWN
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _column(group: str, figure: str) -> str:
    if group == "cpu":
        return f"cpu_{figure}"
    if group == "memory":
        return f"mem_{figure}_mib"
    if group == "disk":
        return f"disk_{figure}_mib"
    if group == "adapters":
        return figure
    return _PARTITION_COLUMNS[figure]


def _figure_cells(source: SystemReading | Rollup) -> dict[str, str]:
    return {
        _column(group, item.name): _cell(getattr(figures, item.name))
        for group in FIGURE_GROUPS
        for figures in (getattr(source, group),)
        for item in fields(figures)
    }


def _system_row(system: FleetSystem) -> dict[str, str]:
    reading = system.reading
    pools = (
        _UNKNOWN
        if reading.cpu.shared is None
        else ";".join(str(pool) for pool in reading.shared_pools)
    )
    return {
        "row_type": "system",
        "profiles": ";".join(system.profiles),
        "system": reading.name,
        "machine_type": _cell(reading.machine_type),
        "model": _cell(reading.model),
        "serial": _cell(reading.serial),
        "firmware": _cell(reading.firmware),
        "state": _cell(reading.state),
        "shared_pools": pools,
        "notes": "; ".join(reading.gaps),
        "cpu_allocated": _cell(allocated(reading.cpu.configurable, reading.cpu.free)),
        "cpu_util_pct": _cell(
            utilization_pct(reading.cpu.configurable, reading.cpu.free)
        ),
        "mem_allocated_mib": _cell(
            allocated(reading.memory.configurable, reading.memory.free)
        ),
        "mem_util_pct": _cell(
            utilization_pct(reading.memory.configurable, reading.memory.free)
        ),
        **{
            f"{name}_util_pct": _cell(utilization_pct(*capacity_pairs(reading)[name]))
            for name in _UTILIZATIONS
        },
        **_figure_cells(reading),
    }


def _rollup_row(row_type: str, profiles: str, total: Rollup) -> dict[str, str]:
    notes = "; ".join(
        f"{_column(group, figure)}: {count} of {total.systems} systems unknown"
        for group, figure, count in total.unknown
    )
    return {
        "row_type": row_type,
        "profiles": profiles,
        "systems": str(total.systems),
        "cpu_allocated": _cell(total.cpu_allocated),
        "cpu_util_pct": _cell(total.cpu_util_pct),
        "mem_allocated_mib": _cell(total.mem_allocated),
        "mem_util_pct": _cell(total.mem_util_pct),
        "notes": notes,
        **{
            f"{name}_util_pct": _cell(getattr(total, f"{name}_util_pct"))
            for name in _UTILIZATIONS
        },
        **_figure_cells(total),
    }


def _fleet_shortfalls(
    survey: FleetSurvey, systems: tuple[FleetSystem, ...]
) -> list[str]:
    """What the fleet totals miss beyond per-figure unknowns."""
    shortfalls: list[str] = []
    if survey.failures:
        names = ", ".join(failure.profile for failure in survey.failures)
        shortfalls.append(
            f"{len(survey.failures)} of {len(survey.profiles)} profiles failed "
            f"({names}); systems only they manage are absent"
        )
    unidentified = sum(1 for system in systems if system.reading.serial is None)
    if unidentified:
        shortfalls.append(
            f"{unidentified} systems have no machine type-model-serial, so one managed "
            "by two HMCs is counted twice"
        )
    return shortfalls


def report_rows(survey: FleetSurvey) -> list[dict[str, str]]:
    """System rows, then per-profile roll-ups, the fleet roll-up, and failures."""
    systems = fleet_systems(survey.readings)
    failed = {failure.profile for failure in survey.failures}
    rows = [_system_row(system) for system in systems]
    rows.extend(
        _rollup_row(
            "hmc",
            profile,
            rollup(r for r in survey.readings if r.profile == profile),
        )
        for profile in survey.profiles
        if profile not in failed
    )
    fleet = _rollup_row("fleet", "", rollup(s.reading for s in systems))
    notes = [*_fleet_shortfalls(survey, systems), fleet["notes"]]
    fleet["notes"] = "; ".join(note for note in notes if note)
    rows.append(fleet)
    rows.extend(
        {"row_type": "failure", "profiles": failure.profile, "notes": failure.reason}
        for failure in survey.failures
    )
    return rows


def write_csv(stream: TextIO, rows: list[dict[str, str]]) -> None:
    """Write *rows* to *stream* under the stable ``COLUMNS`` header."""
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, restval="")
    writer.writeheader()
    writer.writerows(_inert(row) for row in rows)


def _inert(row: dict[str, str]) -> dict[str, str]:
    """Quote text cells a spreadsheet would otherwise evaluate as a formula."""
    return {
        column: f"'{value}"
        if column in _TEXT_COLUMNS and value.startswith(_FORMULA_START)
        else value
        for column, value in row.items()
    }


def _scratch_file(path: Path) -> tuple[int, Path]:
    """Create the owner-only temporary file a report is written to, beside *path*.

    The open descriptor is returned so the report is written to the file mkstemp
    created, not to whatever its name points at by the time the survey ends.
    """
    try:
        handle, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as exc:
        usage_error(f"cannot write the report beside {path}: {exc}")
    return handle, Path(name)


def _write_reports(
    csv_path: Path | None,
    html_path: Path | None,
    survey_once: Callable[[], FleetSurvey],
) -> FleetSurvey:
    """Survey once and write each requested report through its own scratch file.

    Every scratch file exists before the survey starts, so an unwritable directory fails
    before any HMC is contacted, and none is renamed until every report is written.
    """
    targets = [
        (path, kind) for path, kind in ((csv_path, "csv"), (html_path, "html")) if path
    ]
    try:
        with ExitStack() as cleanup:
            outputs = []
            for path, kind in targets:
                handle, scratch = _scratch_file(path)
                cleanup.callback(scratch.unlink, missing_ok=True)
                stream = cleanup.enter_context(
                    os.fdopen(handle, "w", newline="", encoding="utf-8")
                )
                outputs.append((path, kind, scratch, stream))
            survey = survey_once()
            generated = datetime.now(UTC)
            rows = report_rows(survey)
            for _, kind, _, stream in outputs:
                if kind == "csv":
                    write_csv(stream, rows)
                else:
                    stream.write(render_html(rows, survey.profiles, generated))
                stream.close()
            for path, _, scratch, _ in outputs:
                scratch.replace(path)
    except OSError as exc:
        fail(exc)
    return survey


def _selected_profiles(requested: list[str] | None) -> list[str]:
    inventory = config_inventory()
    known = [str(entry["name"]) for entry in inventory["profiles"]]
    if not known:
        where = inventory["config_file"] or "the platform config directory"
        fail(
            ValueError(
                f"no connection profiles are configured in {where}; add one as "
                "docs/configuration.md describes"
            )
        )
    unknown = [name for name in requested or [] if name not in known]
    if unknown:
        usage_error(
            f"unknown profile(s) {', '.join(unknown)}; "
            f"configured profiles: {', '.join(known)}"
        )
    return list(requested or known)


def report_utilization(
    csv_path: Path | None = typer.Option(
        None, "--csv", dir_okay=False, help="Write the CSV report to this file."
    ),
    html_path: Path | None = typer.Option(
        None,
        "--html",
        dir_okay=False,
        help="Write the self-contained HTML report to this file.",
    ),
    profiles: list[str] | None = typer.Option(
        None,
        "--profile",
        help="Survey only this profile; repeat for more. Default: every profile.",
    ),
    concurrency: int = typer.Option(
        4, "--concurrency", min=1, help="Profiles surveyed at the same time."
    ),
    hmc_timeout: float = typer.Option(
        300.0, "--hmc-timeout", min=1.0, help="Seconds allowed per profile."
    ),
) -> None:
    """Survey configured HMCs and write CPU, memory, disk and adapter allocation.

    Give --csv PATH, --html PATH, or both; both are written from one survey.

    The report holds internal hostnames, system names and serials: never commit it
    or post it publicly.
    """
    if not math.isfinite(hmc_timeout):
        usage_error(
            f"--hmc-timeout must be a finite number of seconds, got {hmc_timeout}"
        )
    if csv_path is None and html_path is None:
        usage_error("give --csv PATH, --html PATH, or both")
    if (
        csv_path is not None
        and html_path is not None
        and str(csv_path.resolve()).casefold() == str(html_path.resolve()).casefold()
    ):
        usage_error("--csv and --html name the same file; give two paths")
    exported = [name for name in _PROFILE_OVERRIDES if env_var_value(name) is not None]
    if exported:
        usage_error(
            "report utilization logs on to each profile's own host with that profile's "
            f"credentials and TLS setting; unset {', '.join(exported)}"
        )
    if current_options().command_line_options:
        usage_error(
            "report utilization connects to each profile's own host; drop the global "
            "--host, --user, --password, --verify-ssl and --profile options"
        )
    try:
        selected = _selected_profiles(profiles)
    except (typer.Exit, typer.Abort):
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary: a config error becomes fail(exc)
        fail(exc)

    def opened(profile: str) -> HMCClient:
        try:
            config = load_profile(profile)
        except ValidationError as exc:
            # The failure reason lands in a circulated CSV on one line; pydantic's own
            # text spans several lines and ends with a docs URL. errors() still carries
            # each rejected input, which can be the password, so render only loc and msg.
            problems = "; ".join(
                f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                for error in exc.errors()
            )
            raise ConfigError(f"profile {profile!r} is invalid: {problems}") from None
        return HMCClient(config)

    def survey_once() -> FleetSurvey:
        survey = run_cli_coroutine(
            lambda: survey_fleet(
                selected, opened, concurrency=concurrency, hmc_timeout=hmc_timeout
            )
        )
        for failure in survey.failures:
            err_console.print(
                Text.assemble(
                    (terminal_field(failure.profile, err=True), "yellow"),
                    f": {terminal_field(failure.reason, err=True)}",
                )
            )
        return survey

    survey = _write_reports(csv_path, html_path, survey_once)
    surveyed = len(survey.profiles) - len(survey.failures)
    written = " and ".join(str(path) for path in (csv_path, html_path) if path)
    err_console.print(
        (
            f"Wrote {terminal_field(len(fleet_systems(survey.readings)), err=True)}"
            f" systems from {terminal_field(surveyed, err=True)} of "
            f"{terminal_field(len(survey.profiles), err=True)} profiles to "
            f"{terminal_field(written, err=True)}"
        ),
        markup=False,
    )


def register_commands(group: typer.Typer) -> None:
    """Register this module's commands on *group*."""
    group.command("utilization")(report_utilization)
