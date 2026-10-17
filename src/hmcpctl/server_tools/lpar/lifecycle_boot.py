"""MCP tools for LPAR boot order, boot progress, and ownership."""

from __future__ import annotations

from typing import Any

from hmcpctl.operations.lpar.ownership import list_lpar_ownership
from hmcpctl.operations.systems.fleet import FleetListing

from ..._app import ssh_with_client, with_client
from ...operations.lpar.boot_order import (
    clear_lpar_boot_order,
    read_lpar_boot_order,
    set_lpar_boot_order,
)
from ...ssh.refcodes import list_lpar_refcodes
from ...tool_registry import tool_module

tool, register_tools, tool_security = tool_module()

# LPAR Boot Order Tools


@tool(effect="read", operation="boot_order.read", target_kind="lpar")
def hmc_read_lpar_boot_order(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile: str | None = None,
) -> dict[str, Any]:
    """Read current, pending, and last-used boot-device state for an LPAR.

    Args:
        system_name_or_uuid: CLI name or UUID of the managed system.
        lpar_name_or_uuid: Name or UUID of the logical partition.
        profile: Configured HMC profile, or the default when omitted.
    """

    async def read_boot_order(hmc) -> dict[str, Any]:
        return await read_lpar_boot_order(
            hmc,
            system_name_or_uuid=system_name_or_uuid,
            lpar_name_or_uuid=lpar_name_or_uuid,
        )

    return with_client(read_boot_order, profile=profile)


@tool(effect="read", operation="lpar.list_refcodes", target_kind="lpar")
def hmc_read_lpar_refcodes(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    count: int = 1,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """Read recent partition reference codes with offline POWER9 definitions.

    Boot progress without a virtual terminal: ``lsrefcode -r lpar`` needs no
    console, so an activation can be followed read-only. Rows come back
    most-recent first, each carrying ``lpar_name``, ``time_stamp`` and
    ``refcode``, plus ``definition`` and ``definition_source``. The added fields
    are null for unknown codes. Definitions are offline POWER9 informational
    summaries with public IBM page/code provenance; they do not establish boot
    success, readiness or failure. The raw code and diagnostic suffix are preserved.
    Whether an SRC is still reported once the partition reaches
    ``Running`` is reported by the caller epic (#871) and has not been
    confirmed against hardware here; #879 owns that observation.

    An empty list means no reference codes for that selector. A partition that
    does not exist is believed to answer the same way rather than failing, but
    that has not been confirmed against hardware; #879 owns the live check.

    Broader reference-code, FRU and LED inventory is #691's; this is the narrow
    partition-scoped read and it reaches no LED control.

    Args:
        system_name_or_uuid: System name or UUID from ``hmc_list_systems``.
        lpar_name_or_uuid: Name or UUID of the logical partition.
        count: How many reference codes to return, 1 to 100, newest first.
        profile: TOML profile name, or the environment-default HMC when omitted.
    """

    return ssh_with_client(
        lambda config, system_name, lpar_name: list_lpar_refcodes(
            config, system_name, lpar_name, count
        ),
        system_name_or_uuid=system_name_or_uuid,
        lpar_name_or_uuid=lpar_name_or_uuid,
        profile=profile,
    )


@tool(effect="mutate", operation="boot_order.set", target_kind="lpar")
def hmc_set_lpar_boot_order(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    devices: list[str],
    *,
    ownership_override: bool = False,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Set the pending boot order used on the LPAR's next activation.

    The write replaces ``BootListInformation/PendingBootString`` by
    read-modify-write of the whole partition, conditioned on its ETag.
    ``hmc_read_lpar_boot_order`` reports the paths the HMC knows in
    ``boot_device_list`` and ``last_booted_device_string``; a never-booted
    partition reports none. Firmware names a virtual optical (ISO) device
    ``disk@<LUN>``, not ``cdrom@``: build
    ``/vdevice/v-scsi@<0x30000000 + client slot, hex>/disk@<LogicalUnitAddress>``
    from the optical mapping, or boot it once from SMS and take
    ``last_booted_device_string`` up to its ``:``. Firmware keywords such as
    ``cd/dvd-all`` are refused.

    Args:
        system_name_or_uuid: CLI name or UUID of the managed system.
        lpar_name_or_uuid: Name or UUID of the logical partition.
        devices: Open Firmware device paths in first-to-last order, such as
            ``/vdevice/v-scsi@30000002/disk@8100000000000000``; each starts with
            ``/`` and is printable ASCII with no whitespace.
        ownership_override: Skip ownership-token validation when true.
        profile: Configured HMC profile, or the default when omitted.
    """

    async def set_boot_order(hmc) -> dict[str, Any] | None:
        return await set_lpar_boot_order(
            hmc,
            system_name_or_uuid=system_name_or_uuid,
            lpar_name_or_uuid=lpar_name_or_uuid,
            devices=devices,
            ownership_override=ownership_override,
        )

    return with_client(set_boot_order, profile=profile)


@tool(effect="mutate", operation="boot_order.clear", target_kind="lpar")
def hmc_clear_lpar_boot_order(
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    ownership_override: bool = False,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Refuse to clear a pending boot order: no value tried on V10R3 clears it.

    Authorizes the caller, then raises without writing. A V10R3 HMC (M1060)
    rejects an empty ``PendingBootString`` with HTTP 500 ``REST0126`` and its CLI
    stores some other forms literally (#1048). A profile activation consumed the
    pending boot order when observed on V10R3; other activation paths are
    unverified. ``hmc_set_lpar_boot_order`` replaces it.

    Args:
        system_name_or_uuid: CLI name or UUID of the managed system.
        lpar_name_or_uuid: Name or UUID of the logical partition.
        ownership_override: Skip ownership-token validation when true.
        profile: Configured HMC profile, or the default when omitted.
    """

    async def clear_boot_order(hmc) -> dict[str, Any] | None:
        return await clear_lpar_boot_order(
            hmc,
            system_name_or_uuid=system_name_or_uuid,
            lpar_name_or_uuid=lpar_name_or_uuid,
            ownership_override=ownership_override,
        )

    return with_client(clear_boot_order, profile=profile)


@tool(effect="read", operation="lpar.list_ownership", target_kind="managed_system")
def hmc_list_lpar_ownership(
    system_name_or_uuid: str | None = None,
    profile: str | None = None,
) -> FleetListing:
    """Read parsed ownership for every LPAR on a system in one REST call.

    Parses the advisory ADR 0011 ownership token out of each partition's
    description via the bulk list feed, so one request covers the whole system
    (#375). Returns ``entries`` and ``unreadable_systems``. Every partition read
    is an entry: ``owned`` partitions carry the ``owner`` agent id; a
    description with no well-formed stamp is reported with ``unparsed=True``; a
    partition with no description at all has ``description=None`` — the three
    facts stay distinct for reconciliation.

    Args:
        system_name_or_uuid: Optional SystemName or UUID whose partitions to
            read. Omitted reads each operating managed system's LPAR feed in
            turn; a system that is not operating is not read and is named in
            unreadable_systems instead. Entries carry no parent-system
            attribution.
        profile: Optional configured HMC profile name; uses the default when
            omitted.
    """

    return with_client(
        lambda hmc: list_lpar_ownership(hmc, system_name_or_uuid),
        profile=profile,
    )
