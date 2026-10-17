"""LPAR ownership authorization and identity resolution operations."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

from hmcpctl.audit import records as audit
from hmcpctl.client.client_resolution import system_operating
from hmcpctl.client.core import HMCClient
from hmcpctl.discovery_limits import (
    MAX_PARENT_DISCOVERY_SYSTEMS,
    PARENT_DISCOVERY_TIMEOUT_SECONDS,
)
from hmcpctl.errors import HMCError
from hmcpctl.operations.systems.fleet import FleetListing, read_fleet
from hmcpctl.resource_identity import (
    _hosts_partition,
    is_uuid,
    lpar_name_from_uuid,
    optional_system_selector,
    resolve_lpar_uuid,
    resolve_system_uuid,
    system_name_from_uuid,
)
from hmcpctl.ssh.description_validation import validate_lpar_description
from hmcpctl.ssh.lpar import resolve_system_cli_name, stamp_lpar_ownership
from hmcpctl.ssh.profiles import get_lpar_description, set_lpar_description
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import leaf_text

_logger = logging.getLogger(__name__)


def _fleet_within_discovery_bound(systems: list[Any], lpar_label: str) -> list[Any]:
    """Reject an owning-system search whose request fan-out is too large.

    The shared bound also protects ambiguous-name resolution. Its message is
    not suitable here because the partition UUID resolved uniquely before this
    parent discovery began.
    """
    if len(systems) > MAX_PARENT_DISCOVERY_SYSTEMS:
        raise ValueError(
            f"Cannot identify the managed system owning LPAR {lpar_label!r}: "
            f"discovery exceeds {MAX_PARENT_DISCOVERY_SYSTEMS} managed systems; "
            "supply managed-system scope"
        )
    return systems


def _discovery_candidate(system: object) -> tuple[str, str] | None:
    """Return one quick/All summary's UUID and CLI name, or None if unusable."""
    if not isinstance(system, dict):
        return None
    system_uuid = system.get("UUID")
    system_name = system.get("SystemName")
    if not isinstance(system_uuid, str) or not is_uuid(system_uuid):
        return None
    if not isinstance(system_name, str) or not system_name:
        return None
    return system_uuid, system_name


@dataclass
class _SkippedFrames:
    """Frames the owning-system walk could not read, bounded for reporting."""

    unreadable: list[str] = field(default_factory=list)
    unusable_entries: int = 0

    @property
    def total(self) -> int:
        return len(self.unreadable) + self.unusable_entries

    def describe(self) -> str:
        """Render the skips as bounded evidence, never one line per frame."""
        if not self.total:
            return ""
        shown = self.unreadable[:_MAX_REPORTED_SKIPS]
        parts = list(shown)
        if len(self.unreadable) > len(shown):
            parts.append(f"and {len(self.unreadable) - len(shown)} more")
        if self.unusable_entries:
            parts.append(f"{self.unusable_entries} with incomplete inventory metadata")
        return f" ({self.total} could not be read: {', '.join(parts)})"


_MAX_REPORTED_SKIPS = 5


async def _fleet_inventory(hmc: HMCClient, lpar_label: str) -> list[Any]:
    """Read the fleet, translating an inventory failure into the same remedy.

    The systems come from ``ManagedSystem/quick/All``: the full ``ManagedSystem``
    feed times out while a system has no connection (#1501). Every other
    discovery failure names ``supply managed-system scope``; without this the
    one degraded-dependency case that the selector *does* fix would be the only
    one that never mentions it.
    """
    try:
        return await hmc.list_system_summaries()
    except HMCError as exc:
        raise ValueError(
            f"Cannot identify the managed system owning LPAR {lpar_label!r}: "
            f"managed-system inventory is unavailable ({exc}); "
            "supply managed-system scope"
        ) from exc


async def _search_fleet_for_partition(
    hmc: HMCClient, lpar_uuid: str, lpar_label: str, skipped: _SkippedFrames
) -> tuple[str, str] | None:
    """Return the system containing *lpar_uuid*, recording frames it could not read."""
    for system in _fleet_within_discovery_bound(
        await _fleet_inventory(hmc, lpar_label), lpar_label
    ):
        candidate = _discovery_candidate(system)
        if candidate is None:
            skipped.unusable_entries += 1
            continue
        system_uuid, system_name = candidate
        try:
            partitions = await hmc.list_logical_partitions(system_uuid)
        except HMCError as exc:
            _logger.warning(
                "Owning-system discovery could not read managed system %s: %s",
                system_uuid,
                exc,
                exc_info=exc,
            )
            state = system.get("State")
            if system_operating(state):
                skipped.unreadable.append(system_uuid)
            else:
                state_text = (
                    state.strip()[:100] if isinstance(state, str) else "unknown"
                )
                skipped.unreadable.append(f"{system_uuid} in State {state_text!r}")
            continue
        if _hosts_partition(partitions, lpar_uuid):
            _logger.info(
                "LPAR %s authorized against discovered managed system %s (%s)",
                lpar_uuid,
                system_name,
                system_uuid,
            )
            return system_uuid, system_name
    return None


async def _discover_owning_system(
    hmc: HMCClient, lpar_uuid: str, lpar_label: str
) -> tuple[str, str]:
    """Return the UUID and CLI name of the system that contains *lpar_uuid*.

    ADR 0094. The ADR 0011 guard reads an ownership token by CLI system name
    plus partition name, so an operation whose managed-system selector is
    optional (ADR 0063) has to derive one when the caller omits it. This is the
    bounded parent discovery :meth:`HMCClient.find_partition_by_name` already
    applies to a fleet-ambiguous partition name — the same 100-system fan-out
    cap, the same timeout, and the same "supply managed-system scope" remedy.

    The name comes from the inventory entry that matched, so *on this branch*
    the guard cannot fall back to running ``lssyscfg -m <uuid>``, which the HMC
    CLI cannot satisfy. The selector-supplied branch still hands ``_system_name``
    the caller's raw selector, which that helper's pre-existing degraded path
    can return verbatim.

    A frame whose partition feed errors, or whose inventory entry is unusable,
    is skipped rather than made fatal: the walk crosses frames the caller never
    named, and one unhealthy frame sorting early must not take DLPAR down for
    every partition in the fleet. Skipping never widens what may be mutated —
    the search still ends in a raise unless it *positively* matched the
    partition — and the raise names how many frames went unread, so a degraded
    fleet is diagnosed rather than reported as an absent partition.
    """
    skipped = _SkippedFrames()
    try:
        async with asyncio.timeout(PARENT_DISCOVERY_TIMEOUT_SECONDS):
            found = await _search_fleet_for_partition(
                hmc, lpar_uuid, lpar_label, skipped
            )
    except TimeoutError as exc:
        raise ValueError(
            f"Cannot identify the managed system owning LPAR {lpar_label!r}: "
            "parent discovery timed out; supply managed-system scope"
        ) from exc
    if found is not None:
        return found
    raise ValueError(
        f"Cannot identify the managed system owning LPAR {lpar_label!r}: "
        f"no managed system reports it{skipped.describe()}; "
        "supply managed-system scope"
    )


async def resolve_and_authorize_lpar_mutation(
    hmc: HMCClient,
    system_name_or_uuid: str | None,
    lpar_name_or_uuid: str,
    *,
    ownership_override: bool,
) -> str:
    """Resolve one LPAR, authorize the mutation, and return its UUID.

    The guarded counterpart of the resolve chain in :func:`rename_lpar`, for the
    operations whose managed-system selector is optional. Either way the
    partition is established to live on the system whose token the guard reads:
    by discovery when the selector is omitted, by
    :func:`resolve_lpar_uuid` when it is supplied (ADR 0226).

    An approved override reads no token, so it skips the fleet walk the guard
    alone requires — otherwise an oversized fleet, a slow one, or an unreachable
    owning frame would *block* the operator's exception on exactly the degraded
    inventory that provokes it (ADR 0092 §4). It still resolves the partition
    name, one REST read, so the audit record for a deliberate bypass of the
    ownership control names the partition in the same vocabulary as every other
    override record.

    A blank *system_name_or_uuid* is read as absent on both branches: MCP
    clients that serialise an unset optional string as ``""`` sent it before
    this operation existed, and it was ignored.
    """
    selector = optional_system_selector(system_name_or_uuid)
    if ownership_override:
        return await _authorize_override(hmc, lpar_name_or_uuid, selector)
    if selector is None:
        lpar_uuid = await resolve_lpar_uuid(hmc, lpar_name_or_uuid)
        # Establish the partition exists *before* the walk: `is_uuid` is a format
        # check, so an unknown or non-partition UUID would otherwise drive up to
        # 100 full partition-feed reads from one caller-supplied string.
        lpar_name = await _partition_name(hmc, lpar_uuid, lpar_name_or_uuid)
        system_uuid, system_name = await _discover_owning_system(
            hmc, lpar_uuid, lpar_name_or_uuid
        )
    else:
        system_uuid = await resolve_system_uuid(hmc, selector)
        lpar_uuid = await resolve_lpar_uuid(
            hmc, lpar_name_or_uuid, system_name_or_uuid=system_uuid
        )
        system_name, lpar_name = await resolve_lpar_ownership_names(
            hmc, system_uuid, selector, lpar_uuid
        )
    await authorize_lpar_mutation(hmc, system_name, lpar_name)
    return lpar_uuid


async def _partition_name(hmc: HMCClient, lpar_uuid: str, lpar_label: str) -> str:
    """Read one partition's CLI name, and fail if the UUID has no partition.

    Two different failures, only one of which this function raises. A UUID that
    names nothing is rejected by the GET itself — ``client._get`` raises
    ``HMCError`` on the 404 and it propagates from here unchanged, naming the
    partition the caller got wrong. The ``ValueError`` below covers the narrower
    case of a resource that reads back but carries no ``PartitionName``.

    Both stop the caller before the ADR 0094 fleet walk, which is the point:
    ``is_uuid`` is a format check, so without this the walk would read up to
    ``MAX_PARENT_DISCOVERY_SYSTEMS`` partition feeds before reporting a missing
    partition as a missing *system*.
    """
    lpar_name = await lpar_name_from_uuid(hmc, lpar_uuid)
    if lpar_name is None:
        raise ValueError(
            f"No LPAR {lpar_label!r} found. "
            "Use hmc_list_lpars to list available partitions."
        )
    return lpar_name


async def _authorize_override(
    hmc: HMCClient, lpar_name_or_uuid: str, selector: str | None
) -> str:
    """Audit an operator-approved ownership bypass and return the LPAR's UUID."""
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=selector
    )
    lpar_name = await _partition_name(hmc, lpar_uuid, lpar_name_or_uuid)
    await authorize_lpar_mutation(
        hmc, selector or "", lpar_name, ownership_override=True
    )
    return lpar_uuid


_OWNERSHIP_TOKEN = re.compile(
    r"\[hmcpctl owner:(?P<owner>[^\s\[\]:]+) created:\d{4}-\d{2}-\d{2}\]"
)
_CALLER_TOKEN = re.compile(
    r"\[hmcpctl owner:[^\s\[\]:]+ created:\d{4}-\d{2}-\d{2}\] "
    r"\[caller (?P<token>[^\s\[\]]+)\]"
)
# The first stamp plus the caller segment directly after it, never one that
# belongs to a later stamp.
_STAMP_PREFIX = re.compile(_OWNERSHIP_TOKEN.pattern + r"(?: \[caller [^\s\[\]]+\])?")


def parse_lpar_ownership_owner(description: str) -> str | None:
    """Return the advisory hmcpctl owner token embedded in *description*."""
    match = _OWNERSHIP_TOKEN.search(description)
    return match.group("owner") if match is not None else None


def parse_lpar_ownership_caller_token(description: str) -> str | None:
    """Return the unique caller token following a well-formed ownership stamp."""
    matches = _CALLER_TOKEN.findall(description)
    if len(matches) != 1 or description.count("[caller ") != 1:
        return None
    return matches[0]


def lpar_ownership_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Distill one parsed LogicalPartition feed entry into ownership facts."""
    resource = entry.get("Resource") or {}
    description = leaf_text(resource.get("Description"))
    owner = (
        parse_lpar_ownership_owner(description)
        if isinstance(description, str)
        else None
    )
    return {
        "lpar_name": resource.get("PartitionName"),
        "lpar_uuid": entry.get("UUID"),
        "description": description,
        "owned": owner is not None,
        "owner": owner,
        "unparsed": description is not None and owner is None,
    }


async def list_lpar_ownership(
    hmc: HMCClient,
    system_name_or_uuid: str | None = None,
) -> FleetListing:
    """Read parsed ownership for every LPAR on one system or across the fleet.

    Without a system, each operating managed system is read in turn and the
    others are named in ``unreadable_systems`` (ADR 0197).
    """
    selector = optional_system_selector(system_name_or_uuid)
    if selector is not None:
        system_uuid = await resolve_system_uuid(hmc, selector)
        listing = FleetListing(await hmc.list_logical_partitions(system_uuid))
    else:
        listing = await read_fleet(hmc, hmc.list_logical_partitions, "LPARs")
    return replace(
        listing, entries=[lpar_ownership_entry(entry) for entry in listing.entries]
    )


def _audit_lpar_ownership_override(
    hmc: HMCClient, system_name: str, lpar_name: str
) -> None:
    audit.record_ownership_override(
        system=system_name,
        lpar=lpar_name,
        host=hmc.config.host,
        agent_id=hmc.config.agent_id or "hmcpctl",
    )


def _audit_lpar_ownership_denied(
    hmc: HMCClient,
    system_name: str,
    lpar_name: str,
    *,
    operation: audit.OwnershipOperation,
    denial: audit.OwnershipDenial,
    owner: str | None,
) -> None:
    audit.record_ownership_denied(
        operation=operation,
        denial=denial,
        system=system_name,
        lpar=lpar_name,
        owner=owner,
        host=hmc.config.host,
        agent_id=hmc.config.agent_id or "hmcpctl",
    )


def authorize_lpar_ownership_description(
    hmc: HMCClient,
    system_name: str,
    lpar_name: str,
    description: str,
    *,
    operation: audit.OwnershipOperation,
    ownership_override: bool = False,
) -> str | None:
    """Authorize a supplied description snapshot and return its parsed owner."""
    owner = parse_lpar_ownership_owner(description)
    if ownership_override:
        _audit_lpar_ownership_override(hmc, system_name, lpar_name)
        return owner
    if owner is None:
        if "[hmcpctl" in description:
            _audit_lpar_ownership_denied(
                hmc,
                system_name,
                lpar_name,
                operation=operation,
                denial="malformed-token",
                owner=None,
            )
            raise PermissionError(
                f"LPAR {lpar_name!r} has a malformed hmcpctl ownership token; "
                "retry only with ownership_override=true after operator approval"
            )
        return None
    current_owner = hmc.config.agent_id or "hmcpctl"
    if owner != current_owner:
        _audit_lpar_ownership_denied(
            hmc,
            system_name,
            lpar_name,
            operation=operation,
            denial="foreign-owner",
            owner=owner,
        )
        raise PermissionError(
            f"LPAR {lpar_name!r} is owned by {owner!r}, not {current_owner!r}; "
            "retry only with ownership_override=true after operator approval"
        )
    return owner


async def _authorize_system_lpar_profile_restore(
    hmc: HMCClient,
    system_name_or_uuid: str,
    *,
    ownership_override: bool = False,
) -> str:
    """Authorize a system-wide profile restore and return its HMC CLI name."""
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    system_name = await _resolve_system_name(hmc, system_uuid, system_name_or_uuid)
    if ownership_override:
        _audit_lpar_ownership_override(hmc, system_name, "*")
        return system_name

    try:
        rows = (await list_lpar_ownership(hmc, system_uuid)).entries
    except HMCError as exc:
        raise ValueError(
            "LPAR ownership inventory is unavailable; retry after the managed-system "
            "partition feed recovers"
        ) from exc
    for row in rows:
        lpar_name = row.get("lpar_name")
        description = row.get("description")
        if not isinstance(lpar_name, str) or not lpar_name.strip():
            raise ValueError(
                "LPAR ownership inventory contains a row without a partition name; "
                "retry after the inventory is complete"
            )
        if description is not None and not isinstance(description, str):
            raise ValueError(
                f"LPAR ownership inventory for {lpar_name!r} has an unreadable "
                "description; retry after the inventory is complete"
            )
        authorize_lpar_ownership_description(
            hmc,
            system_name,
            lpar_name,
            description or "",
            operation="lpar-profile-restore",
        )

    raise PermissionError(
        "current LPAR ownership is clear, but the profile backup may contain "
        "backup-only definitions; retry with ownership_override=true after "
        "operator approval"
    )


async def _read_lpar_description_for_authorization(
    hmc: HMCClient, system_name: str, lpar_name: str
) -> str:
    """Read ownership metadata with a remedy for an unavailable SSH precheck."""
    try:
        return await get_lpar_description(hmc.config, system_name, lpar_name)
    except HMCCLIError as exc:
        raise HMCCLIError(
            f"ADR 0011 ownership precheck failed for LPAR {lpar_name!r} on "
            f"managed system {system_name!r}; retry with ownership_override=true "
            f"after operator approval: {exc}"
        ) from exc


async def authorize_lpar_mutation(
    hmc: HMCClient,
    system_name: str,
    lpar_name: str,
    *,
    ownership_override: bool = False,
) -> None:
    """Reject mutations of foreign-owned or malformed ownership-stamped LPARs."""
    if ownership_override:
        _audit_lpar_ownership_override(hmc, system_name, lpar_name)
        return
    description = await _read_lpar_description_for_authorization(
        hmc, system_name, lpar_name
    )
    authorize_lpar_ownership_description(
        hmc, system_name, lpar_name, description, operation="lpar-mutation"
    )


async def resolve_lpar_ownership_names(
    hmc: HMCClient,
    system_uuid: str,
    system_name_or_uuid: str,
    lpar_uuid: str,
) -> tuple[str, str]:
    """Resolve the CLI names required to read an LPAR ownership token."""
    system_name = await _resolve_system_name(hmc, system_uuid, system_name_or_uuid)
    lpar_name = await lpar_name_from_uuid(hmc, lpar_uuid)
    if lpar_name is None:
        raise ValueError(f"LPAR {lpar_uuid!r} has no partition name")
    return system_name, lpar_name


async def resolve_and_authorize_lpar_names(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    ownership_override: bool = False,
) -> tuple[str, str]:
    """Resolve an LPAR's CLI names, authorize its mutation, and return the names."""
    names = await _resolve_contained_lpar_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    await authorize_lpar_mutation(hmc, *names, ownership_override=ownership_override)
    return names


async def _resolve_contained_lpar_names(
    hmc: HMCClient, system_name_or_uuid: str, lpar_name_or_uuid: str
) -> tuple[str, str]:
    """Resolve the CLI names of an LPAR proven to live on the selected system."""
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    lpar_uuid = await resolve_lpar_uuid(
        hmc, lpar_name_or_uuid, system_name_or_uuid=system_uuid
    )
    return await resolve_lpar_ownership_names(
        hmc, system_uuid, system_name_or_uuid, lpar_uuid
    )


async def _resolve_system_name(hmc: HMCClient, system_uuid: str, fallback: str) -> str:
    """Resolve an HMC CLI system name, falling back to the caller's selector."""
    try:
        name = await system_name_from_uuid(hmc, system_uuid)
        if name is not None:
            return name
    except HMCError as exc:
        _logger.debug(
            "REST system-name lookup failed for %s: %s",
            system_uuid,
            exc,
            exc_info=exc,
        )
    try:
        return await resolve_system_cli_name(hmc.config, system_uuid)
    except HMCCLIError as exc:
        _logger.warning(
            "SSH system-name lookup failed for %s; using fallback %r: %s",
            system_uuid,
            fallback,
            exc,
            exc_info=exc,
        )
        return fallback


async def authorize_decommission_lpar_ownership_snapshot(
    hmc: HMCClient,
    system_name: str,
    lpar_name: str,
    *,
    ownership_override: bool,
) -> str | None:
    """Read and authorize one ownership snapshot for LPAR decommission."""
    description = await _read_lpar_description_for_authorization(
        hmc, system_name, lpar_name
    )
    return authorize_lpar_ownership_description(
        hmc,
        system_name,
        lpar_name,
        description,
        operation="lpar-decommission-snapshot",
        ownership_override=ownership_override,
    )


async def stamp_created_lpar_ownership(
    hmc: HMCClient,
    system_uuid: str,
    system_fallback: str,
    created_lpar: dict[str, Any],
    caller_token: str | None = None,
) -> tuple[bool | None, list[str]]:
    """Stamp a newly created LPAR and report confirmed, skipped, or failed."""
    confirmed_name = (created_lpar.get("Resource") or {}).get("PartitionName")
    if not confirmed_name:
        return None, ["ownership stamp skipped: create result has no partition name"]

    system_name = await _resolve_system_name(hmc, system_uuid, system_fallback)
    if system_name == system_uuid:
        return None, [
            (
                f"ownership stamp skipped for LPAR {confirmed_name!r}: "
                "could not resolve the managed-system name"
            )
        ]

    token = await stamp_lpar_ownership(
        hmc.config,
        system_name,
        confirmed_name,
        agent_id=hmc.config.agent_id,
        caller_token=caller_token,
    )
    if token is not None:
        return True, []
    _logger.warning(
        "ownership stamp failed for LPAR %r on %r", confirmed_name, system_name
    )
    return False, [f"ownership stamp failed for LPAR {confirmed_name!r}"]


async def set_lpar_ownership_description(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    description: str,
    *,
    ownership_override: bool = False,
) -> str:
    """Validate, authorize, and write one LPAR ownership description.

    ``chsyscfg`` replaces the whole field, and the ADR 0011 stamp lives in it,
    so a plain-text write must not be what leaves a stamped partition unowned
    (#1169). Without an override, text that carries no stamp-shaped fragment is
    written after the current well-formed stamp and its caller segment. Text
    that carries its own stamp is the ADR 0066 re-stamp or handover and is
    written as given. An approved override reads nothing (ADR 0092 §4) and
    writes the text as given. The kept stamp is the one read here, so a
    handover completed between that read and the write is overwritten with it:
    ownership is advisory, and there is no compare-and-set.
    """
    validate_lpar_description(description)
    _require_parseable_stamp(description)
    system_name, lpar_name = await _resolve_contained_lpar_names(
        hmc, system_name_or_uuid, lpar_name_or_uuid
    )
    if ownership_override:
        await authorize_lpar_mutation(
            hmc, system_name, lpar_name, ownership_override=True
        )
    else:
        current = await _read_lpar_description_for_authorization(
            hmc, system_name, lpar_name
        )
        authorize_lpar_ownership_description(
            hmc, system_name, lpar_name, current, operation="lpar-mutation"
        )
        description = _keep_ownership_prefix(current, description)
    return await set_lpar_description(hmc.config, system_name, lpar_name, description)


def _require_parseable_stamp(description: str) -> None:
    """Refuse a stamp-shaped fragment the ownership parse would not accept.

    Written, it would read back as a malformed token, which the guard refuses
    for every later mutation, the owner's included.
    """
    if (
        "[hmcpctl" in description or "[caller " in description
    ) and parse_lpar_ownership_owner(description) is None:
        raise ValueError(
            "description carries a '[hmcpctl' or '[caller ' fragment but no "
            "well-formed ownership stamp "
            "'[hmcpctl owner:<id> created:<YYYY-MM-DD>]'; write plain text, "
            "which keeps the current stamp, or a complete stamp"
        )


def _keep_ownership_prefix(current: str, description: str) -> str:
    """Prefix plain *description* with *current*'s stamp and caller segment."""
    stamp = _STAMP_PREFIX.search(current)
    if stamp is None or parse_lpar_ownership_owner(description) is not None:
        return description
    return f"{stamp.group(0)} {description}" if description else stamp.group(0)
