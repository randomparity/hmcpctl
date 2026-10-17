"""Connectivity and core resource inventory for the live HMC test harness."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from fastmcp import Client

from hmcpctl.xmlutil import console_version

from .observation import Assertion
from .results import entries, field
from .results import resource as get_resource

if TYPE_CHECKING:
    from live_test_runner import RunState

# hmc_list_systems lists from quick/All (#1501), so it succeeds even where the raw
# feed 500s (ADR 0138). ST1 reads the raw feed through
# hmc_list_resources (plain list_uom) so a success the raw feed did not serve
# fails ``feed-served-directly`` instead of promoting.
_FEED_PROBE = "hmc_list_resources (ManagedSystem feed probe)"
# hmc_find_placement's processor request, passed explicitly so the fit check
# compares against the value the call actually used.
_PLACEMENT_PROC_UNITS = 0.5
_PROC_TOLERANCE = 1e-4
# The plan names a new virtual disk it never creates. A fixed name keeps the request
# within the VIOS's 15-character backing-device limit whatever the lab's ST13 storage
# name is; that name is an existing disk's and carries no such bound.
_PLAN_DISK_NAME = "hmcpctl-st1"

# ---------------------------------------------------------------------------
# ST1 — Connectivity & Inventory
# ---------------------------------------------------------------------------


def _items(value: object) -> list[object]:
    """A list result's items; FastMCP delivers dataclass items as generated models."""
    return value if isinstance(value, list) else []


def _same(value: object, expected: object) -> bool:
    return (
        isinstance(value, str)
        and isinstance(expected, str)
        and value.lower() == expected.lower()
    )


def _uuids(items: Sequence[object]) -> set[str]:
    return {
        uuid.lower() for item in items if isinstance(uuid := field(item, "UUID"), str)
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _capacity_consistent(row: object) -> bool:
    """``0 <= free <= total`` and ``assigned == total - free`` for memory and units."""
    for kind, tolerance in (("memory_mib", 0.0), ("proc_units", _PROC_TOLERANCE)):
        total, free, assigned = (
            _number(field(row, f"{part}_{kind}"))
            for part in ("total", "free", "assigned")
        )
        if total is None or free is None or assigned is None:
            return False
        if not -tolerance <= free <= total + tolerance:
            return False
        if abs(assigned - (total - free)) > tolerance:
            return False
    return True


def _covers(row: object, memory_mib: int) -> bool:
    free_memory = _number(field(row, "free_memory_mib"))
    free_units = _number(field(row, "free_proc_units"))
    return (
        free_memory is not None
        and free_units is not None
        and free_memory >= memory_mib
        and free_units >= _PLACEMENT_PROC_UNITS
    )


def _ownership_consistent(row: object) -> bool:
    owned, owner, unparsed = (field(row, k) for k in ("owned", "owner", "unparsed"))
    if not isinstance(owned, bool) or not isinstance(unparsed, bool):
        return False
    if owned != (isinstance(owner, str) and bool(owner)):
        return False
    return not unparsed or (not owned and field(row, "description") is not None)


def _source_ok(section: object, name: str = "source") -> bool:
    return field(field(section, name), "status") == "ok"


async def _discover_console(client: Client, state: RunState) -> object:
    st, data = await state.call(client, "hmc_get_console_info")
    console_uuid = None
    if st == "PASS" and isinstance(data, dict):
        console_uuid = data.get("uuid") or data.get("UUID")
    # console.info reads the ManagementConsole feed, which some HMC firmware builds
    # have answered with HTTP 500 over a null Session/SessionId/Value (#880). #1531's
    # sweep read only ManagedSystem, so it says nothing about this endpoint. No outcome
    # is declared: a 500 records a failed assertion, keeping the gap visible in the
    # maturity catalog.
    state.record_verified(
        1,
        "hmc_get_console_info",
        operation="console.info",
        scenario="st1-console-identity",
        assertions=[Assertion("console-uuid-present", bool(console_uuid))],
        cleanup="not-required",
        data=data,
    )
    if console_uuid:
        state.artifacts.console_uuid = console_uuid
    return data


async def _probe_system_feed(client: Client, state: RunState) -> set[str] | None:
    """The raw ManagedSystem feed's UUIDs, or None when the feed itself failed."""
    st, data = await state.call(
        client, "hmc_list_resources", resource_type="ManagedSystem"
    )
    state.record(1, _FEED_PROBE, st, data)
    return _uuids(entries(data)) if st == "PASS" else None


async def _discover_system(
    client: Client, state: RunState, feed: set[str] | None
) -> object:
    """Record the system reads; return the boundary system's State as read."""
    config = state.config
    artifacts = state.artifacts

    st, data = await state.call(client, "hmc_list_systems")
    listed = entries(data) if st == "PASS" else []
    matched_uuid = next(
        (
            e.get("UUID")
            for e in listed
            if _same(get_resource(e).get("SystemName"), config.system_name)
        ),
        None,
    )
    listed_uuids = _uuids(listed)
    state.record_verified(
        1,
        "hmc_list_systems",
        operation="system.list",
        scenario="st1-system-inventory",
        assertions=[
            Assertion("system-list-non-empty", bool(listed)),
            Assertion("boundary-system-listed", matched_uuid is not None),
            Assertion(
                "entries-carry-uuid",
                bool(listed) and all(isinstance(e.get("UUID"), str) for e in listed),
            ),
            Assertion(
                "feed-served-directly",
                feed is not None and bool(listed_uuids) and listed_uuids == feed,
            ),
        ],
        cleanup="not-required",
        data=data,
    )
    if isinstance(matched_uuid, str):
        artifacts.system_uuid = matched_uuid

    st, data = await state.call(
        client, "hmc_get_system", system_name_or_uuid=config.system_name
    )
    system_uuid = None
    if st == "PASS" and isinstance(data, dict):
        system_uuid = data.get("UUID") or data.get("uuid")
    state.record_verified(
        1,
        "hmc_get_system",
        operation="system.get",
        scenario="st1-system-inventory",
        assertions=[
            Assertion("system-uuid-present", bool(system_uuid)),
        ],
        cleanup="not-required",
        data=data,
    )
    # Fall back to the single-system lookup's UUID when the list did not name
    # the boundary system.
    if system_uuid and not artifacts.system_uuid:
        artifacts.system_uuid = system_uuid
    print(f"  System UUID: {artifacts.system_uuid}")
    return get_resource(data).get("State") if isinstance(data, dict) else None


async def _discover_partitions(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts

    st, data = await state.call(client, "hmc_list_lpars")
    state.record_verified(
        1,
        "hmc_list_lpars",
        operation="lpar.list",
        scenario="st1-lpar-inventory",
        assertions=[
            Assertion("lpar-list-non-empty", bool(st == "PASS" and entries(data))),
        ],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client, "hmc_get_lpar", lpar_name_or_uuid=config.lp3_name
    )
    lpar_uuid = None
    if st == "PASS" and isinstance(data, dict):
        lpar_uuid = data.get("uuid") or data.get("UUID")
    state.record_verified(
        1,
        "hmc_get_lpar",
        operation="lpar.get",
        scenario="st1-lpar-inventory",
        assertions=[
            Assertion("lpar-uuid-present", bool(lpar_uuid)),
        ],
        cleanup="not-required",
        data=data,
    )
    if lpar_uuid and not artifacts.lp3_uuid:
        artifacts.lp3_uuid = lpar_uuid


async def _discover_vios(client: Client, state: RunState) -> None:
    config = state.config
    artifacts = state.artifacts

    st, data = await state.call(
        client, "hmc_list_vios", system_name_or_uuid=config.system_name
    )
    vios_uuid = None
    vios_partition_id = None
    if st == "PASS":
        for e in entries(data):
            resource = get_resource(e)
            uuid = e.get("UUID") or e.get("uuid")
            pid = resource.get("PartitionID") or resource.get("partition_id")
            if isinstance(uuid, str) and uuid:
                if pid is not None and not isinstance(pid, str | int):
                    raise TypeError(
                        f"hmc_list_vios entry {uuid} has a PartitionID of type "
                        f"{type(pid).__name__}; expected a string or integer"
                    )
                vios_uuid = uuid
                vios_partition_id = int(pid) if pid is not None else None
                break
    state.record_verified(
        1,
        "hmc_list_vios",
        operation="vios.list",
        scenario="st1-vios-inventory",
        assertions=[
            Assertion("vios-list-non-empty", bool(st == "PASS" and entries(data))),
            Assertion("vios-uuid-present", bool(vios_uuid)),
        ],
        cleanup="not-required",
        data=data,
    )
    if vios_uuid and not artifacts.vios_uuid:
        artifacts.vios_uuid = vios_uuid
        artifacts.vios_partition_id = vios_partition_id
    print(
        f"  VIOS UUID: {artifacts.vios_uuid}  PartitionID: {artifacts.vios_partition_id}"
    )


async def _probe_capacity_and_resources(
    client: Client, state: RunState, feed: set[str] | None
) -> None:
    config = state.config

    st, data = await state.call(client, "hmc_capacity_report")
    rows = _items(data) if st == "PASS" else []
    boundary = next(
        (row for row in rows if _same(field(row, "system_name"), config.system_name)),
        None,
    )
    state.record_verified(
        1,
        "hmc_capacity_report",
        operation="capacity.report",
        scenario="st1-capacity",
        assertions=[
            Assertion("boundary-system-reported", boundary is not None),
            Assertion(
                "capacity-figures-consistent",
                boundary is not None and _capacity_consistent(boundary),
            ),
            Assertion("feed-served-directly", feed is not None),
        ],
        cleanup="not-required",
        data=data,
    )
    await _find_placement(client, state, feed, boundary)

    st, data = await state.call(
        client, "hmc_list_resources", resource_type="LogicalPartition"
    )
    state.record_verified(
        1,
        "hmc_list_resources",
        operation="console.list_resources",
        scenario="st1-resource-inventory",
        assertions=[
            Assertion(
                "resource-list-non-empty",
                bool(st == "PASS" and entries(data)),
            ),
        ],
        cleanup="not-required",
        data=data,
    )


async def _find_placement(
    client: Client, state: RunState, feed: set[str] | None, boundary: object
) -> None:
    """Placement for ``placement_memory_mib``, judged against the capacity report's row."""
    config = state.config
    request = config.placement_memory_mib
    st, data = await state.call(
        client,
        "hmc_find_placement",
        desired_memory_mib=request,
        desired_proc_units=_PLACEMENT_PROC_UNITS,
    )
    answered = st == "PASS"
    candidates = _items(data) if answered else []
    free = [_number(field(c, "free_memory_mib")) for c in candidates]
    boundary_is_candidate = any(
        _same(field(c, "system_name"), config.system_name) for c in candidates
    )
    state.record_verified(
        1,
        "hmc_find_placement",
        operation="placement.find",
        scenario="st1-capacity",
        assertions=[
            Assertion(
                "candidates-fit",
                answered and all(_covers(c, request) for c in candidates),
            ),
            Assertion(
                "candidates-best-fit-first",
                answered
                and None not in free
                and free == sorted(m for m in free if m is not None),
            ),
            # Unknowable without the capacity report's boundary row, so its
            # absence fails the assertion rather than passing it vacuously.
            Assertion(
                "boundary-candidate-when-it-fits",
                answered
                and boundary is not None
                and (boundary_is_candidate or not _covers(boundary, request)),
            ),
            Assertion("feed-served-directly", feed is not None),
        ],
        cleanup="not-required",
        data=data,
    )


async def _record_inventory_summaries(client: Client, state: RunState) -> None:
    config = state.config

    st, data = await state.call(
        client, "hmc_system_summary", system_name_or_uuid=config.system_name
    )
    # SystemSummary is a dataclass, not a dict; a non-None uuid confirms a real record.
    has_summary = (
        st == "PASS"
        and data is not None
        and (
            (isinstance(data, dict) and (data.get("uuid") or data.get("UUID")))
            or getattr(data, "uuid", None) is not None
        )
    )
    state.record_verified(
        1,
        "hmc_system_summary",
        operation="system.summary",
        scenario="st1-system-inventory",
        assertions=[
            Assertion("system-summary-returned", has_summary),
        ],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client, "hmc_lpar_summary", lpar_name_or_uuid=config.lp3_name
    )
    # LparSummary is a dataclass, not a dict; a non-None uuid confirms a real record.
    has_lpar_summary = (
        st == "PASS"
        and data is not None
        and (
            (isinstance(data, dict) and (data.get("uuid") or data.get("UUID")))
            or getattr(data, "uuid", None) is not None
        )
    )
    state.record_verified(
        1,
        "hmc_lpar_summary",
        operation="lpar.summary",
        scenario="st1-lpar-inventory",
        assertions=[
            Assertion("lpar-summary-returned", has_lpar_summary),
        ],
        cleanup="not-required",
        data=data,
    )


async def _read_partition_views(client: Client, state: RunState) -> None:
    """Ownership, boot order and inspection of the test partition on the boundary system."""
    config = state.config
    lp3_uuid = state.artifacts.lp3_uuid

    st, data = await state.call(
        client, "hmc_list_lpar_ownership", system_name_or_uuid=config.system_name
    )
    rows = _items(field(data, "entries")) if st == "PASS" else []
    state.record_verified(
        1,
        "hmc_list_lpar_ownership",
        operation="lpar.list_ownership",
        scenario="st1-lpar-inventory",
        assertions=[
            Assertion("ownership-entries-non-empty", bool(rows)),
            Assertion(
                "test-partition-listed",
                any(_same(field(r, "lpar_name"), config.lp3_name) for r in rows),
            ),
            Assertion(
                "ownership-facts-consistent",
                bool(rows) and all(_ownership_consistent(r) for r in rows),
            ),
        ],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client,
        "hmc_read_lpar_boot_order",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record_verified(
        1,
        "hmc_read_lpar_boot_order",
        operation="boot_order.read",
        scenario="st1-lpar-inventory",
        assertions=[
            Assertion(
                "boot-order-names-partition",
                st == "PASS" and _same(field(data, "lpar_uuid"), lp3_uuid),
            ),
        ],
        cleanup="not-required",
        data=data,
    )

    st, data = await state.call(
        client,
        "hmc_inspect_lpar",
        lpar_name_or_uuid=config.lp3_name,
        system_name_or_uuid=config.system_name,
        include=["resources", "rmc", "refcodes"],
    )
    answered = st == "PASS"
    state.record_verified(
        1,
        "hmc_inspect_lpar",
        operation="lpar.inspect",
        scenario="st1-lpar-inventory",
        assertions=[
            Assertion(
                "inspection-names-partition",
                answered and _same(field(data, "uuid"), lp3_uuid),
            ),
            Assertion(
                "resources-read",
                answered and _source_ok(field(data, "resources"), "storage_source"),
            ),
            Assertion("rmc-read", answered and _source_ok(field(data, "rmc"))),
            Assertion(
                "refcodes-read", answered and _source_ok(field(data, "refcodes"))
            ),
        ],
        cleanup="not-required",
        data=data,
    )


async def _capture_snapshot(client: Client, state: RunState) -> None:
    """A portable snapshot of the test partition's default profile; it writes nothing."""
    config = state.config
    st, data = await state.call(
        client,
        "hmc_get_lpar_proc_compat",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
    )
    state.record(1, "hmc_get_lpar_proc_compat (snapshot profile)", st, data)
    profile_name = field(data, "profile") if st == "PASS" else None
    if not isinstance(profile_name, str) or not profile_name:
        state.record(
            1,
            "hmc_snapshot_capture",
            "FAIL",
            None,
            "no default profile name to capture",
        )
        return

    st, data = await state.call(
        client,
        "hmc_snapshot_capture",
        system_name_or_uuid=config.system_name,
        lpar_name_or_uuid=config.lp3_name,
        profile_name=profile_name,
    )
    answered = st == "PASS"
    lpar = field(field(data, "source"), "lpar")
    configuration = field(data, "configuration")
    scores = field(field(field(data, "observations"), "scores"), "data")
    state.record_verified(
        1,
        "hmc_snapshot_capture",
        operation="snapshot.capture",
        scenario="st1-lpar-snapshot",
        assertions=[
            Assertion(
                "snapshot-names-partition",
                answered
                and _same(field(lpar, "uuid"), state.artifacts.lp3_uuid)
                and _same(field(lpar, "name"), config.lp3_name),
            ),
            Assertion(
                "snapshot-names-system",
                answered
                and _same(
                    field(field(field(data, "source"), "system"), "uuid"),
                    state.artifacts.system_uuid,
                ),
            ),
            Assertion(
                "profile-captured",
                answered
                and field(configuration, "profile_name") == profile_name
                and bool(field(field(configuration, "native"), "data")),
            ),
            Assertion(
                "scores-name-partition",
                answered
                and _same(
                    field(field(field(scores, "current"), "lpar"), "lpar_name"),
                    config.lp3_name,
                ),
            ),
        ],
        cleanup="not-required",
        data=data,
    )


async def _read_logical_inventory(client: Client, state: RunState) -> None:
    """The logical inventory, selected to the boundary system."""
    config = state.config
    st, data = await state.call(client, "hmc_inventory", systems=[config.system_name])
    answered = st == "PASS"
    systems = _items(field(data, "systems")) if answered else []
    partitions = _items(field(data, "partitions")) if answered else []
    boundary = next(
        (s for s in systems if _same(field(s, "name"), config.system_name)), None
    )
    system_id = field(boundary, "id")
    sources = field(boundary, "sources")
    state.record_verified(
        1,
        "hmc_inventory",
        operation="inventory.logical",
        scenario="st1-logical-inventory",
        assertions=[
            Assertion("boundary-system-listed", isinstance(system_id, str)),
            Assertion(
                "test-partition-listed",
                any(_same(field(p, "name"), config.lp3_name) for p in partitions),
            ),
            Assertion(
                "partitions-belong-to-system",
                isinstance(system_id, str)
                and bool(partitions)
                and all(field(p, "system_id") == system_id for p in partitions),
            ),
            # A denied or failed capacity or ownership part still lists the system
            # and its partitions, with those figures null.
            Assertion(
                "system-sources-read",
                all(
                    _source_ok(sources, part)
                    for part in ("capacity", "partitions", "ownership")
                ),
            ),
        ],
        cleanup="not-required",
        data=data,
    )


async def _read_fleet_health(
    client: Client, state: RunState, feed: set[str] | None, system_state: object
) -> None:
    """Fleet health, whose boundary-system flag must match the State ST1 read."""
    system_uuid = state.artifacts.system_uuid
    st, data = await state.call(client, "hmc_fleet_health")
    answered = st == "PASS"
    flagged = any(
        _same(field(issue, "uuid"), system_uuid)
        for issue in _items(field(data, "systems"))
    )
    state.record_verified(
        1,
        "hmc_fleet_health",
        operation="health.fleet",
        scenario="st1-fleet-health",
        assertions=[
            Assertion(
                "health-sections-present",
                answered
                and all(
                    isinstance(field(data, k), list)
                    for k in ("systems", "vios", "lpars", "warnings")
                ),
            ),
            Assertion(
                "boundary-system-flag-matches-state",
                answered
                and isinstance(system_state, str)
                and flagged != _same(system_state, "operating"),
            ),
            Assertion("feed-served-directly", feed is not None),
        ],
        cleanup="not-required",
        data=data,
    )


async def _plan_lpar(client: Client, state: RunState) -> None:
    """A plan for ST13's dry-run partition on the boundary system; it writes nothing."""
    config = state.config
    system_uuid = state.artifacts.system_uuid
    st, data = await state.call(
        client,
        "hmc_plan_lpar",
        name=config.dry_run_lpar_name,
        adapters={"port_vlan_id": config.provision_vlan_id},
        storage={
            "storage_name": _PLAN_DISK_NAME,
            "capacity_mib": config.provision_disk_mib,
        },
        system_name_or_uuid=config.system_name,
    )
    answered = st == "PASS"
    blockers = field(data, "blockers")
    state.record_verified(
        1,
        "hmc_plan_lpar",
        operation="lpar.plan",
        scenario="st1-lpar-plan",
        assertions=[
            Assertion(
                "plan-targets-boundary-system",
                answered
                and any(
                    _same(
                        field(field(field(c, "targets"), "system"), "uuid"), system_uuid
                    )
                    for c in _items(field(data, "candidates"))
                ),
            ),
            # plan_lpar sets the digest exactly when a candidate is selected and
            # the request carries no blocker of its own.
            Assertion(
                "plan-outcome-consistent",
                answered
                and isinstance(blockers, list)
                and (field(data, "plan_digest") is not None)
                == (field(data, "selected") is not None and not blockers),
            ),
        ],
        cleanup="not-required",
        data=data,
    )


# PlatformUpdate needs HMC 11.1.1111 (docs/refs/hmc-rest-api-p11/jobs/managedsystem-jobs/
# 065-platformupdate_managedsystem-job.md:12). The name is absent on purpose: an HMC past
# the gate would still stop at the name lookup, before any PlatformUpdate PUT.
_PLATFORM_UPDATE_MINIMUM = (11, 1, 1111)
_ABSENT_SYSTEM = "hmcpctl-live-absent-system"
_PLATFORM_UPDATE_CHECK = "hmc_update_firmware (pre-11.1.1111 refusal)"


async def _check_platform_update_refusal(
    client: Client, state: RunState, console: object
) -> None:
    """A pre-11.1.1111 HMC refuses PlatformUpdate before any lookup; a non-promoting check."""
    resource = console.get("Resource") if isinstance(console, dict) else None
    version = console_version(resource) if isinstance(resource, dict) else None
    if version is None or version >= _PLATFORM_UPDATE_MINIMUM:
        state.skip(
            1,
            _PLATFORM_UPDATE_CHECK,
            "runs only on an HMC whose version reads below 11.1.1111",
        )
        return
    status, data = await state.call(
        client,
        "hmc_update_firmware",
        system_name_or_uuid=_ABSENT_SYSTEM,
        platform_update={
            "SystemFirmwareUpdate": {"UpdateType": "Update", "UpdateOrder": 1}
        },
    )
    text = str(data)
    refused = (
        status == "FAIL"
        and "requires HMC 11.1.1111" in text
        and "below the minimum" in text
    )
    state.record(
        1,
        _PLATFORM_UPDATE_CHECK,
        "PASS" if refused else "FAIL",
        data,
        "refused before resolving the system"
        if refused
        else "expected the 11.1.1111 version refusal",
    )


async def inventory_connectivity(client: Client, state: RunState) -> None:
    print("\n=== ST1: Connectivity & Inventory ===")
    console = await _discover_console(client, state)
    feed = await _probe_system_feed(client, state)
    system_state = await _discover_system(client, state, feed)
    await _discover_partitions(client, state)
    await _discover_vios(client, state)
    await _probe_capacity_and_resources(client, state, feed)
    await _record_inventory_summaries(client, state)
    await _read_partition_views(client, state)
    await _capture_snapshot(client, state)
    await _read_logical_inventory(client, state)
    await _read_fleet_health(client, state, feed, system_state)
    await _plan_lpar(client, state)
    await _check_platform_update_refusal(client, state, console)
