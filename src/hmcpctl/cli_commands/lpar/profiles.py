"""CLI commands for LPAR profile-backed boot configuration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import typer
from rich.markup import escape

from ...client.client_profiles import PartitionKind
from ...documents.boot import select_boot_string
from ...documents.partition_profile import ProfilePatch
from ...operations.lpar.boot_order import (
    clear_lpar_boot_order,
    read_lpar_boot_order,
    set_lpar_boot_order,
)
from ...operations.lpar.profile_artifacts import (
    delete_backup_profile_artifact,
    list_backup_profile_artifacts,
)
from ...operations.lpar.profiles import (
    PartitionProfileOutcome,
    delete_partition_profile,
    list_partition_profiles,
    read_partition_profile,
    save_current_partition_profile,
    set_default_partition_profile,
    update_partition_profile,
)
from ..output import console, print_json, terminal_field
from ..runtime import with_client


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
    devices: list[str] | None = typer.Argument(
        None,
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
    boot_string: str | None = typer.Option(
        None,
        "--boot-string",
        help="Exact printable raw pending text; 16KiB UTF-8 budget; mutually exclusive with devices",
    ),
) -> None:
    """Set the pending boot order used on the LPAR's next activation.

    Prints the boot string set and the pending boot string read back from the HMC
    (`-` when the HMC returned none).

    Example:
        lpars set-boot-order system1 lpar-uuid-123 /vdevice/v-scsi@30000002/disk@8100000000000000
    """
    try:
        requested = select_boot_string(devices, boot_string)
    except ValueError as exc:
        raise typer.BadParameter(terminal_field(exc, err=True)) from exc

    result = with_client(
        lambda hmc: set_lpar_boot_order(
            hmc,
            system_name_or_uuid=system_name,
            lpar_name_or_uuid=lpar_name_or_uuid,
            devices=devices,
            ownership_override=ownership_override,
            **({"boot_string": boot_string} if boot_string is not None else {}),
        )
    )

    console.print(
        f"[green]Boot order set to: {escape(terminal_field(requested))}[/green]"
    )
    if as_json:
        print_json(result)
        return
    console.print(f"Pending boot string: {escape(terminal_field(requested))}")


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


def lpars_list_profile_backups(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
) -> None:
    """List bounded HMC-side profile backups, preserving failed-job outcomes."""
    result = with_client(lambda hmc: list_backup_profile_artifacts(hmc, system_name))
    print_json(asdict(result))
    if result.artifacts is None:
        raise typer.Exit(code=1)


def lpars_delete_profile_backup(
    system_name: str = typer.Argument(..., help="Managed system name or UUID"),
    file_name: str = typer.Argument(
        ..., help="Exact HMC backup name, never a local path"
    ),
) -> None:
    """Delete exactly one listed backup and report its fresh absence readback."""
    result = with_client(
        lambda hmc: delete_backup_profile_artifact(hmc, system_name, file_name)
    )
    print_json(asdict(result))
    if result.postcondition != "absent":
        raise typer.Exit(code=1)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list-profiles")(lpars_list_partition_profiles)
    group.command("read-profile")(lpars_read_partition_profile)
    group.command("update-profile")(lpars_update_partition_profile)
    group.command("save-current-profile")(lpars_save_current_partition_profile)
    group.command("set-default-profile")(lpars_set_default_partition_profile)
    group.command("delete-profile")(lpars_delete_partition_profile)
    group.command("list-profile-backups")(lpars_list_profile_backups)
    group.command("delete-profile-backup")(lpars_delete_profile_backup)
    group.command("read-boot-order")(lpars_read_boot_order)
    group.command("set-boot-order")(lpars_set_boot_order)
    group.command("clear-boot-order")(lpars_clear_boot_order)


def _profile_kind(vios: bool) -> PartitionKind:
    return "VirtualIOServer" if vios else "LogicalPartition"


def _show_partition_profile_outcome(result: PartitionProfileOutcome) -> None:
    print_json(asdict(result))
    if result.execution == "rejected":
        raise typer.Exit(code=1)
    if (
        result.execution != "completed"
        or result.requested_change != "verified"
        or result.preservation not in {"verified", "not-applicable"}
        or (result.preservation == "verified" and not result.comparison_complete)
        or (result.recovery_profile is not None and not result.recovery_verified)
    ):
        raise typer.Exit(code=2)


def lpars_list_partition_profiles(
    system_name: Annotated[str, typer.Argument(help="Managed system name or UUID")],
    lpar_name_or_uuid: Annotated[
        str, typer.Argument(help="Contained partition name or UUID")
    ],
    *,
    vios: Annotated[
        bool, typer.Option("--vios", help="Select a VIOS instead of an LPAR")
    ] = False,
) -> None:
    """List complete bounded saved profiles,including names,fields and opaque ETags."""
    result = with_client(
        lambda hmc: list_partition_profiles(
            hmc, system_name, lpar_name_or_uuid, partition_type=_profile_kind(vios)
        )
    )
    print_json(asdict(result))


def lpars_read_partition_profile(
    system_name: Annotated[str, typer.Argument(help="Managed system name or UUID")],
    lpar_name_or_uuid: Annotated[
        str, typer.Argument(help="Contained partition name or UUID")
    ],
    profile_selector: Annotated[
        str, typer.Argument(help="Exact saved-profile name or UUID")
    ],
    *,
    vios: Annotated[
        bool, typer.Option("--vios", help="Select a VIOS instead of an LPAR")
    ] = False,
) -> None:
    """Read one contained saved profile;connection selection uses the global --profile."""
    result = with_client(
        lambda hmc: read_partition_profile(
            hmc,
            system_name,
            lpar_name_or_uuid,
            profile_selector,
            partition_type=_profile_kind(vios),
        )
    )
    print_json(asdict(result))


def lpars_update_partition_profile(
    system_name: Annotated[str, typer.Argument(help="Managed system name or UUID")],
    lpar_name_or_uuid: Annotated[
        str, typer.Argument(help="Contained partition name or UUID")
    ],
    profile_selector: Annotated[
        str, typer.Argument(help="Exact saved-profile name or UUID")
    ],
    expected_etag: Annotated[
        str, typer.Option("--expected-etag", help="Opaque ETag from read-profile")
    ],
    patch: Annotated[
        str,
        typer.Option(
            "--patch",
            help='Typed JSON patch,e.g. {"desired_memory":1024}; unknown fields are rejected',
        ),
    ],
    *,
    exclusive_writer_window: Annotated[
        bool,
        typer.Option(
            "--exclusive-writer-window",
            help="Attest coordination of partition/profile/system-profile writers",
        ),
    ] = False,
    ownership_override: Annotated[
        bool,
        typer.Option(
            "--ownership-override",
            help="Use the audited ownership override after approval",
        ),
    ] = False,
    vios: Annotated[
        bool, typer.Option("--vios", help="Select a VIOS instead of an LPAR")
    ] = False,
) -> None:
    """Patch saved fields with If-Match;exit2 means drift,partial or uncertain readback."""
    try:
        typed = ProfilePatch.model_validate_json(patch)
    except ValueError as error:
        raise typer.BadParameter(
            terminal_field(error, err=True), param_hint="--patch"
        ) from error
    result = with_client(
        lambda hmc: update_partition_profile(
            hmc,
            system_name,
            lpar_name_or_uuid,
            profile_selector,
            expected_etag,
            typed,
            partition_type=_profile_kind(vios),
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
        )
    )
    _show_partition_profile_outcome(result)


def lpars_save_current_partition_profile(
    system_name: Annotated[str, typer.Argument(help="Managed system name or UUID")],
    lpar_name_or_uuid: Annotated[
        str, typer.Argument(help="Contained partition name or UUID")
    ],
    name: Annotated[
        str, typer.Argument(help="Saved profile name to create or overwrite")
    ],
    *,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Explicit overwrite consent; requires verified retained recovery",
        ),
    ] = False,
    expected_etag: Annotated[
        str | None,
        typer.Option(
            "--expected-etag",
            help="Fresh destination ETag;required with existing profiles",
        ),
    ] = None,
    recovery_profile_selector: Annotated[
        str | None,
        typer.Option(
            "--recovery-profile",
            help="Existing contained recovery profile to revalidate; otherwise create one retained copy",
        ),
    ] = None,
    exclusive_writer_window: Annotated[
        bool,
        typer.Option(
            "--exclusive-writer-window",
            help="Attest coordination of partition/profile/system-profile writers",
        ),
    ] = False,
    ownership_override: Annotated[
        bool,
        typer.Option(
            "--ownership-override",
            help="Use the audited ownership override after approval",
        ),
    ] = False,
    timeout_seconds: Annotated[
        int,
        typer.Option(
            "--timeout-seconds",
            help="Native job polling timeout; no uncertain mutation retry",
        ),
    ] = 300,
    poll_interval: Annotated[
        int,
        typer.Option("--poll-interval", help="Positive native job polling interval"),
    ] = 5,
    vios: Annotated[
        bool, typer.Option("--vios", help="Select a VIOS instead of an LPAR")
    ] = False,
) -> None:
    """Save current configuration;retain verified recovery before any overwrite.

    A failed force-save may change the destination. Retained recovery content is
    not an unchanged destination,and no automatic restoration or retry is attempted.
    """
    result = with_client(
        lambda hmc: save_current_partition_profile(
            hmc,
            system_name,
            lpar_name_or_uuid,
            name,
            partition_type=_profile_kind(vios),
            overwrite=overwrite,
            expected_etag=expected_etag,
            recovery_profile_selector=recovery_profile_selector,
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
    )
    _show_partition_profile_outcome(result)


def lpars_set_default_partition_profile(
    system_name: Annotated[str, typer.Argument(help="Managed system name or UUID")],
    lpar_name_or_uuid: Annotated[
        str, typer.Argument(help="Contained partition name or UUID")
    ],
    profile_selector: Annotated[
        str, typer.Argument(help="Exact saved-profile name or UUID")
    ],
    *,
    exclusive_writer_window: Annotated[
        bool,
        typer.Option(
            "--exclusive-writer-window",
            help="Attest coordination of partition/profile/system-profile writers",
        ),
    ] = False,
    ownership_override: Annotated[
        bool,
        typer.Option(
            "--ownership-override",
            help="Use the audited ownership override after approval",
        ),
    ] = False,
    timeout_seconds: Annotated[
        int, typer.Option("--timeout-seconds", help="Native job polling timeout")
    ] = 300,
    poll_interval: Annotated[
        int,
        typer.Option("--poll-interval", help="Positive native job polling interval"),
    ] = 5,
    vios: Annotated[
        bool, typer.Option("--vios", help="Select a VIOS instead of an LPAR")
    ] = False,
) -> None:
    """Select a contained default profile and verify native job/readback results."""
    result = with_client(
        lambda hmc: set_default_partition_profile(
            hmc,
            system_name,
            lpar_name_or_uuid,
            profile_selector,
            partition_type=_profile_kind(vios),
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
    )
    _show_partition_profile_outcome(result)


def lpars_delete_partition_profile(
    system_name: Annotated[str, typer.Argument(help="Managed system name or UUID")],
    lpar_name_or_uuid: Annotated[
        str, typer.Argument(help="Contained partition name or UUID")
    ],
    profile_selector: Annotated[
        str, typer.Argument(help="Exact saved-profile name or UUID")
    ],
    expected_etag: Annotated[
        str, typer.Option("--expected-etag", help="Opaque ETag from read-profile")
    ],
    *,
    exclusive_writer_window: Annotated[
        bool,
        typer.Option(
            "--exclusive-writer-window",
            help="Attest coordination of partition/profile/system-profile writers",
        ),
    ] = False,
    ownership_override: Annotated[
        bool,
        typer.Option(
            "--ownership-override",
            help="Use the audited ownership override after approval",
        ),
    ] = False,
    vios: Annotated[
        bool, typer.Option("--vios", help="Select a VIOS instead of an LPAR")
    ] = False,
) -> None:
    """Delete only unprotected profiles with an empty system-profile inventory."""
    result = with_client(
        lambda hmc: delete_partition_profile(
            hmc,
            system_name,
            lpar_name_or_uuid,
            profile_selector,
            expected_etag,
            partition_type=_profile_kind(vios),
            exclusive_writer_window=exclusive_writer_window,
            ownership_override=ownership_override,
        )
    )
    _show_partition_profile_outcome(result)
