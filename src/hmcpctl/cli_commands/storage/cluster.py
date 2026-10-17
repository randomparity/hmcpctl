"""CLI commands for clusters / Shared Storage Pools (logical units)."""

from __future__ import annotations

import typer

from ...jobs import DeviceType, LuType
from ...operations.storage.cluster import (
    create_logical_unit,
    delete_logical_unit,
    validate_logical_unit_create,
    validate_logical_unit_wait,
)
from ..output import (
    VerbatimTable,
    console,
    first_field,
    output,
    print_json,
    terminal_field,
)
from ..runtime import with_client


def cluster_list(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List Clusters (VIOS node sets sharing a storage pool)."""

    clusters = with_client(lambda hmc: hmc.list_clusters())

    table = None
    if not as_json:
        table = VerbatimTable(title="Clusters")
        for col in ("Name", "UUID"):
            table.add_column(col)
        for c in clusters:
            table.add_row(first_field(c, "ClusterName"), c.get("UUID") or "-")
    output(clusters, as_json, table, "No clusters found")


def cluster_list_ssps(
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List Shared Storage Pools (capacity, free space, logical units)."""

    ssps = with_client(lambda hmc: hmc.list_shared_storage_pools())

    table = None
    if not as_json:
        table = VerbatimTable(title="Shared Storage Pools")
        for col in ("Name", "UUID", "Capacity (GB)", "Free (GB)"):
            table.add_column(col)
        for s in ssps:
            table.add_row(
                first_field(s, "StoragePoolName"),
                s.get("UUID") or "-",
                first_field(s, "Capacity"),
                first_field(s, "FreeSpace"),
            )
    output(ssps, as_json, table, "No shared storage pools found")


def cluster_create_lu(
    cluster: str = typer.Argument(..., help="Cluster UUID"),
    name: str = typer.Option(..., "--name", "-n", help="Logical unit name"),
    lu_size_gib: int = typer.Option(
        ..., "--lu-size-gib", help="Logical unit size in GiB"
    ),
    lu_type: LuType = typer.Option("THIN", "--type", help="THIN or THICK"),
    device_type: DeviceType = typer.Option(
        "VirtualIO_Disk", "--device-type", help="VirtualIO_Disk or VirtualIO_Image"
    ),
    cloned_from: str | None = typer.Option(
        None, "--cloned-from", help="Source LU UDID to clone"
    ),
    wait: bool = typer.Option(False, "--wait", help="Wait for the job to finish"),
    timeout_seconds: int = typer.Option(
        300, "--timeout", help="Wait timeout in seconds"
    ),
    poll_interval: int = typer.Option(
        5, "--poll-interval", help="Polling interval in seconds"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Create a Logical Unit in a Cluster/SSP (submits a job)."""
    validate_logical_unit_create(
        lu_type, device_type, wait, timeout_seconds, poll_interval
    )
    if not yes and not typer.confirm(
        f"Create {terminal_field(lu_size_gib)} GiB {terminal_field(lu_type)} LU '"
        f"{terminal_field(name)}' in cluster {terminal_field(cluster)}?"
    ):
        raise typer.Abort()

    job = with_client(
        lambda hmc: create_logical_unit(
            hmc,
            cluster,
            name,
            lu_size_gib,
            lu_type,
            device_type,
            cloned_from=cloned_from,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
    )

    console.print(
        f"Submitted CreateLogicalUnit job for '{terminal_field(name)}'",
        style="green",
        markup=False,
    )
    print_json(job)


def cluster_delete_lu(
    cluster: str = typer.Argument(..., help="Cluster UUID"),
    udid: str = typer.Option(..., "--udid", help="Logical unit UDID to delete"),
    wait: bool = typer.Option(False, "--wait", help="Wait for the job to finish"),
    timeout_seconds: int = typer.Option(
        300, "--timeout", help="Wait timeout in seconds"
    ),
    poll_interval: int = typer.Option(
        5, "--poll-interval", help="Polling interval in seconds"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete a Logical Unit from a Cluster/SSP (submits a job)."""
    validate_logical_unit_wait(wait, timeout_seconds, poll_interval)
    if not yes and not typer.confirm(
        f"Delete LU {terminal_field(udid)} from cluster {terminal_field(cluster)}"
        f"? This is irreversible."
    ):
        raise typer.Abort()

    job = with_client(
        lambda hmc: delete_logical_unit(
            hmc,
            cluster,
            udid,
            wait=wait,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
    )

    console.print(
        f"Submitted DeleteLogicalUnit job for {terminal_field(udid)}",
        style="green",
        markup=False,
    )
    print_json(job)


def register_commands(group: typer.Typer) -> None:
    """Register this module’s commands on *group*."""
    group.command("list")(cluster_list)
    group.command("list-ssps")(cluster_list_ssps)
    group.command("create-lu")(cluster_create_lu)
    group.command("delete-lu")(cluster_delete_lu)
