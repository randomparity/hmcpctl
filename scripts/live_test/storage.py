"""Storage inventory scenarios for the live HMC test harness.

ST3 reads the VIOS's volume groups and the HMC's clusters and shared storage
pools, and records each read it can check against independent evidence through
`record_verified` (#1348).
"""

from __future__ import annotations

import shlex
import uuid as uuid_module
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp import Client

from hmcpctl.ssh.commands import HMC_NO_RESULTS, build_filter

from .observation import Assertion, CallFailure
from .results import entries
from .results import resource as get_resource

if TYPE_CHECKING:
    from live_test.runner import RunState


def _mapping_row(entry: object) -> tuple[object, ...] | None:
    if not isinstance(entry, Mapping):
        return None
    return tuple(
        entry.get(key) for key in ("id", "lpar_uuid", "backing_kind", "backing_name")
    )


def storage_rows(data: object) -> frozenset[tuple[object, ...]] | None:
    """``hmc_list_storage_mappings`` as comparable rows; None when malformed."""
    if not isinstance(data, list):
        return None
    rows = [_mapping_row(entry) for entry in data]
    if None in rows:
        return None
    return frozenset(row for row in rows if row is not None)


def scsi_adapter_listing(system_name: str, attribute: str, value: object) -> str:
    """The vSCSI adapter rows of the partitions the one filter pair selects (#1237)."""
    return (
        f"lshwres -r virtualio --rsubtype scsi -m {shlex.quote(system_name)} "
        f"--level lpar --filter {shlex.quote(build_filter([(attribute, value)]))} "
        "-F slot_num,remote_lpar_name,remote_slot_num"
    )


def adapter_rows(listing: object) -> frozenset[str] | None:
    """An adapter listing as a set of lines; None when it is not text."""
    if not isinstance(listing, str):
        return None
    text = listing.strip()
    if text == HMC_NO_RESULTS:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


# ---------------------------------------------------------------------------
# ST3 — Storage & SSP Inventory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfiguredVolumeGroup:
    """The listed volume group named by ``LIVE_TEST_VDISK_VOLUME_GROUP_NAME``."""

    uuid: str
    resource: Mapping[str, Any]
    free_space_mib: int | None


def _free_space_mib(
    entry: Mapping[str, Any], resource: Mapping[str, Any]
) -> int | None:
    """Convert the HMC's GiB free-space figure to MiB; None when unknown."""
    gib = entry.get("free_space_gib")
    if gib is None:
        gib = resource.get("FreeSpace")
    try:
        return None if gib is None else int(float(gib) * 1024)
    except (TypeError, ValueError):
        return None


def resolve_configured_volume_group(
    state: RunState, stage: int, data: object, dependents: Sequence[str]
) -> ConfiguredVolumeGroup | None:
    """Return the configured volume group from a listing, or SKIP ``dependents``.

    The VIOS lists volume groups in no fixed order, so selection is by name
    only; a missing group is never replaced by another one (issue #967).
    """
    artifacts = state.artifacts
    name = state.config.vdisk_volume_group_name
    for entry in entries(data):
        resource = get_resource(entry)
        uuid = entry.get("uuid") or entry.get("UUID")
        listed_name = entry.get("name") or resource.get("GroupName")
        if listed_name == name and isinstance(uuid, str) and uuid:
            artifacts.vg_uuid, artifacts.vdisk_vg_name = uuid, name
            return ConfiguredVolumeGroup(
                uuid, resource, _free_space_mib(entry, resource)
            )
    artifacts.vg_uuid = artifacts.vdisk_vg_name = None
    for dependent in dependents:
        state.skip(stage, dependent, f"configured volume group {name!r} not listed")
    return None


def configured_vg_uuid(state: RunState) -> str | None:
    """Return ``vg_uuid`` only when the resolver recorded it for the configured group.

    A results document restored for a subset run can carry another group's UUID.
    """
    artifacts = state.artifacts
    if artifacts.vdisk_vg_name != state.config.vdisk_volume_group_name:
        return None
    return artifacts.vg_uuid


INVENTORY_SCENARIO = "st3-storage-inventory"
POOL_SCENARIO = "st3-pool-read"
_READ_FAILED = Assertion("read-failed", False)


def vios_command(system_name: str, vios_id: int, command: str) -> str:
    """Run *command* on the VIOS with partition id *vios_id* through the HMC."""
    return (
        f"viosvrcmd -m {shlex.quote(system_name)} --id {int(vios_id)} "
        f"-c {shlex.quote(command)}"
    )


def volume_group_names(listing: object) -> frozenset[str] | None:
    """The VIOS's ``lsvg`` output as group names; None when a line is not one name."""
    if not isinstance(listing, str):
        return None
    names = [line.strip() for line in listing.splitlines() if line.strip()]
    if any(len(name.split()) != 1 for name in names):
        return None
    return frozenset(names)


async def _verify_volume_groups(
    client: Client, state: RunState, data: list[Any], configured_listed: bool
) -> None:
    """Check the REST listing against the VIOS's own ``lsvg`` (independent CLI)."""
    vios_id = state.artifacts.vios_partition_id
    if type(vios_id) is not int:
        state.record(3, "hmc_list_volume_groups", "PASS", data)
        return
    st, listing = await state.call(
        client,
        "hmc_run_command",
        cmd=vios_command(state.config.system_name, vios_id, "lsvg"),
    )
    state.record(3, "hmc_run_command lsvg (VIOS volume groups)", st, listing)
    vios_names = volume_group_names(listing) if st == "PASS" else None
    listed = {entry.get("name") for entry in data if isinstance(entry, Mapping)}
    state.record_verified(
        3,
        "hmc_list_volume_groups",
        operation="storage.list_volume_groups",
        scenario=INVENTORY_SCENARIO,
        assertions=[
            Assertion("configured-group-listed", configured_listed),
            Assertion(
                "groups-match-vios", vios_names is not None and listed == vios_names
            ),
            Assertion(
                "no-free-space-diagnostic",
                all(
                    isinstance(entry, Mapping)
                    and entry.get("free_space_diagnostic") is None
                    for entry in data
                ),
            ),
        ],
        cleanup="not-required",
        data=data,
    )


async def _discover_volume_group(client: Client, state: RunState) -> None:
    artifacts = state.artifacts
    if not artifacts.vios_uuid:
        state.skip(
            3,
            "hmc_list_volume_groups",
            "no VIOS UUID in context (ST0/ST1 failed)",
        )
        return
    st, data = await state.call(
        client, "hmc_list_volume_groups", vios_name_or_uuid=artifacts.vios_uuid
    )
    if st != "PASS" or not isinstance(data, list):
        state.record_verified(
            3,
            "hmc_list_volume_groups",
            operation="storage.list_volume_groups",
            scenario=INVENTORY_SCENARIO,
            assertions=[_READ_FAILED],
            cleanup="not-required",
            data=data,
        )
        return
    group = resolve_configured_volume_group(
        state, 3, data, ("select configured volume group",)
    )
    await _verify_volume_groups(client, state, data, group is not None)
    print(f"  VG UUID: {artifacts.vg_uuid}")


def _identified(entry: object, name_field: str) -> bool:
    """Whether a raw UOM entry carries its UUID and its name field."""
    if not isinstance(entry, Mapping):
        return False
    name = get_resource(entry).get(name_field)
    return bool(entry.get("UUID")) and isinstance(name, str) and bool(name)


def _collection_read(
    state: RunState, tool: str, status: str, data: object
) -> list[Any] | None:
    """The listing to check, or None for a failed read.

    A failed read is recorded by its caller as a failed observation. An empty feed
    proves no entry shape, so it is recorded here as a plain row.
    """
    if status != "PASS" or not isinstance(data, list):
        return None
    if not data:
        state.record(3, f"{tool} (empty)", status, data)
    return data


async def _verify_clusters(client: Client, state: RunState) -> None:
    """Every listed cluster names itself (`docs/refs/hmc-rest-api-p10/002-cluster.md:22`)."""
    st, data = await state.call(client, "hmc_list_clusters")
    clusters = _collection_read(state, "hmc_list_clusters", st, data)
    if clusters == []:
        return
    state.record_verified(
        3,
        "hmc_list_clusters",
        operation="cluster.list",
        scenario=INVENTORY_SCENARIO,
        assertions=[
            Assertion(
                "clusters-identified",
                all(_identified(entry, "ClusterName") for entry in clusters),
            )
        ]
        if clusters is not None
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )


async def _verify_pools(client: Client, state: RunState) -> list[Any] | None:
    """Every listed pool names itself.

    The field is ``StoragePoolName``
    (`docs/refs/hmc-rest-api-p10/cluster/005-shared-storage-pool.md:20`).
    """
    st, data = await state.call(client, "hmc_list_shared_storage_pools")
    pools = _collection_read(state, "hmc_list_shared_storage_pools", st, data)
    if pools == []:
        return pools
    state.record_verified(
        3,
        "hmc_list_shared_storage_pools",
        operation="cluster.list_pools",
        scenario=INVENTORY_SCENARIO,
        assertions=[
            Assertion(
                "pools-identified",
                all(_identified(entry, "StoragePoolName") for entry in pools),
            )
        ]
        if pools is not None
        else [_READ_FAILED],
        cleanup="not-required",
        data=data,
    )
    return pools


def _absent_pool_refused(status: str, data: object) -> bool:
    """Null, or a failure the HMC answered with 404 or a not-found message.

    A tool returning None reaches the runner with no content, which it decodes as
    the empty string.
    """
    if status == "PASS":
        return data is None or data == ""
    return isinstance(data, CallFailure) and (
        data.http_status == 404 or "not found" in data.message.lower()
    )


async def _verify_pool_read(
    client: Client, state: RunState, pools: list[Any] | None
) -> None:
    """Read an absent pool, and the first listed pool when there is one."""
    absent = str(uuid_module.uuid4())
    st, data = await state.call(client, "hmc_get_shared_storage_pool", ssp_uuid=absent)
    state.record(3, "hmc_get_shared_storage_pool (absent pool)", st, data)
    absent_refused = _absent_pool_refused(st, data)
    listed_returned: bool | None = None
    listed = next(
        (entry.get("UUID") for entry in pools or [] if isinstance(entry, Mapping)),
        None,
    )
    if isinstance(listed, str) and listed:
        st, data = await state.call(
            client, "hmc_get_shared_storage_pool", ssp_uuid=listed
        )
        state.record(3, "hmc_get_shared_storage_pool (listed pool)", st, data)
        listed_returned = (
            st == "PASS"
            and isinstance(data, Mapping)
            and str(data.get("UUID", "")).casefold() == listed.casefold()
        )
    state.record_verified(
        3,
        "hmc_get_shared_storage_pool",
        operation="cluster.get_pool",
        scenario=POOL_SCENARIO,
        assertions=[
            Assertion("absent-pool-not-returned", absent_refused),
            *(
                [Assertion("listed-pool-returned", listed_returned)]
                if listed_returned is not None
                else []
            ),
        ],
        cleanup="not-required",
        data={"absent_uuid": absent, "listed_uuid": listed},
    )


async def _record_storage_collections(client: Client, state: RunState) -> None:
    config = state.config
    await _verify_clusters(client, state)
    await _verify_pool_read(client, state, await _verify_pools(client, state))

    st, data = await state.call(
        client, "hmc_list_io_slots", system_name_or_uuid=config.system_name
    )
    state.record(3, "hmc_list_io_slots", st, data)

    st, data = await state.call(
        client, "hmc_list_memory_pools", system_name_or_uuid=config.system_name
    )
    state.record(3, "hmc_list_memory_pools", st, data)


async def inventory_storage(client: Client, state: RunState) -> None:
    print("\n=== ST3: Storage & SSP Inventory ===")
    await _discover_volume_group(client, state)
    await _record_storage_collections(client, state)
