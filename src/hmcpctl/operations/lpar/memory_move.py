"""One native memory move with two contained, authorized observations (ADR 0286)."""

from __future__ import annotations

from typing import Any

import asyncssh

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar_memory import LparMemoryChange, LparMemoryMove
from hmcpctl.errors import HMCError
from hmcpctl.operations.inventory.composite import resource_container
from hmcpctl.resource_identity import (
    is_uuid,
    resolve_system_uuid,
    system_name_from_uuid,
)
from hmcpctl.ssh.memory import move_lpar_memory_quantity

from .memory import (
    _admit_state,
    _contained_partition,
    _integer,
    _postconditions,
    _require_cpu_vector,
    _result,
    _snapshot,
)
from .ownership import authorize_lpar_mutation


async def _read_endpoint(
    hmc: HMCClient, system_uuid: str, selector: str
) -> tuple[str, dict[str, Any]]:
    kind, uuid = await _contained_partition(hmc, system_uuid, selector)
    read = hmc.get_vios if kind == "VirtualIOServer" else hmc.get_logical_partition
    snapshot = _snapshot(await read(uuid), uuid)
    try:
        _require_cpu_vector(snapshot["processors"])
    except ValueError as error:
        raise ValueError(
            "Memory move requires reported numeric processor assignments"
        ) from error
    if not isinstance(snapshot["name"], str) or not snapshot["name"].strip():
        raise ValueError("Memory move requires a reported partition name")
    if not is_uuid(selector) and snapshot["name"] != selector:
        raise ValueError("Memory move name changed after containment resolution")
    return kind, snapshot


async def _system_bounds(hmc: HMCClient, uuid: str, name: str) -> tuple[int, int]:
    entry = await hmc.get_managed_system(uuid)
    if not entry or str(entry.get("UUID", "")).lower() != uuid.lower():
        raise ValueError("Memory move system read has missing or different identity")
    resource = resource_container(entry, "Resource")
    if resource.get("SystemName") != name or resource.get("State") != "operating":
        raise ValueError("Memory move requires the selected operating system")
    memory = resource_container(resource, "AssociatedSystemMemoryConfiguration")
    region = _integer(memory.get("MemoryRegionSize"), "MemoryRegionSize")
    total = _integer(memory.get("ConfigurableSystemMemory"), "ConfigurableSystemMemory")
    if region < 1 or total < region:
        raise ValueError("Memory move system region or capacity is invalid")
    return region, total


def _move_targets(
    source: dict[str, Any],
    destination: dict[str, Any],
    move: LparMemoryMove,
    region: int,
    total: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if source["shared"] or destination["shared"]:
        raise ValueError("Memory moves are unsupported for shared-memory partitions")
    targets = []
    for before, operation in ((source, "remove"), (destination, "add")):
        change = LparMemoryChange(
            operation=operation, quantity_mib=move.quantity_mib, force=move.force
        )
        deferred = _admit_state(before, change)
        delta = -move.quantity_mib if operation == "remove" else move.quantity_mib
        quantity = before["current_memory_mib"] + delta
        minimum, maximum = before["current_minimum_mib"], before["current_maximum_mib"]
        if (
            move.quantity_mib
            and operation == "add"
            and maximum == 0
            and before["state"] == "not activated"
        ):
            raise ValueError(
                "Memory move cannot verify an inactive destination with a zero current envelope"
            )
        if move.quantity_mib and not minimum <= quantity <= maximum:
            raise ValueError(
                "Memory move would cross source minimum or destination maximum"
            )
        if (
            quantity < 0
            or quantity > total
            or quantity % region
            or move.quantity_mib % region
        ):
            raise ValueError(
                "Memory move quantity violates capacity or region alignment"
            )
        targets.append(
            {
                "quantity_mib": quantity,
                "entitled_mib": None,
                "reset": False,
                "deferred": deferred,
                "noop": move.quantity_mib == 0,
            }
        )
    return targets[0], targets[1]


def _endpoint_matched(
    before: dict[str, Any], after: dict[str, Any], targets: dict[str, Any]
) -> bool:
    return _postconditions(before, after, targets) and all(
        before[key] == after[key]
        for key in ("current_minimum_mib", "current_maximum_mib", "processors")
    )


async def _observe_endpoint(
    hmc: HMCClient,
    system_uuid: str,
    kind: str,
    before: dict[str, Any],
    targets: dict[str, Any],
    submission_error: str | None,
    native_rejection: bool,
) -> dict[str, Any]:
    after = None
    try:
        observed_kind, after = await _read_endpoint(hmc, system_uuid, before["uuid"])
        if observed_kind != kind:
            raise ValueError("Memory move readback partition kind changed")
        if any(
            before[key] != after[key]
            for key in (
                "uuid",
                "name",
                "partition_id",
                "partition_type",
                "state",
                "shared",
            )
        ):
            raise ValueError("Memory move readback identity, state or mode changed")
        matched = _endpoint_matched(before, after, targets)
    except (HMCError, ValueError) as error:
        return {
            **_result(
                before, after, targets, "unverified", submission_error, str(error)
            ),
            "read_error": str(error)[:2048],
        }
    if matched:
        outcome = "applied"
    elif after != before:
        outcome = "partial"
    else:
        outcome = "rejected" if native_rejection else "unverified"
    return {
        **_result(before, after, targets, outcome, submission_error),
        "read_error": None,
    }


def _aggregate(
    move: LparMemoryMove,
    source: dict[str, Any],
    destination: dict[str, Any],
    error: str | None,
    native_rejection: bool,
) -> dict[str, Any]:
    peers = (source, destination)
    if all(peer["outcome"] == "noop" for peer in peers):
        outcome = "noop"
    elif all(peer["outcome"] == "applied" for peer in peers):
        outcome = "applied"
    elif any(peer["after"] is None or peer.get("read_error") for peer in peers):
        outcome = "unverified"
    elif any(peer["after"] != peer["before"] for peer in peers):
        outcome = "partial"
    else:
        outcome = "rejected" if native_rejection else "unverified"
    return {
        "outcome": outcome,
        "workflow_completed": outcome in ("applied", "noop"),
        "quantity_mib": move.quantity_mib,
        "submission_error": error,
        "source": source,
        "destination": destination,
    }


async def move_lpar_memory(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    target_lpar_name_or_uuid: str,
    move: LparMemoryMove,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Authorize both peers, submit once, and independently retain both observations."""
    if not isinstance(move, LparMemoryMove):
        raise TypeError("move must be a typed LparMemoryMove")
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (lpar_name_or_uuid, target_lpar_name_or_uuid, system_name_or_uuid)
    ):
        raise ValueError("Memory move requires source, destination and managed system")
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    source_kind, source = await _read_endpoint(hmc, system_uuid, lpar_name_or_uuid)
    target_kind, destination = await _read_endpoint(
        hmc, system_uuid, target_lpar_name_or_uuid
    )
    if (
        source["uuid"].lower() == destination["uuid"].lower()
        or source["partition_id"] == destination["partition_id"]
    ):
        raise ValueError(
            "Memory move requires distinct source and destination partitions"
        )
    system_name = await system_name_from_uuid(hmc, system_uuid)
    if not system_name:
        raise ValueError("Memory move system has no unique CLI name")
    peers = ((source_kind, source), (target_kind, destination))
    for _, before in peers:
        await authorize_lpar_mutation(
            hmc, system_name, before["name"], ownership_override=ownership_override
        )
    bounds = await _system_bounds(hmc, system_uuid, system_name)
    targets = _move_targets(source, destination, move, *bounds)
    for kind, before in peers:
        if await _read_endpoint(hmc, system_uuid, before["uuid"]) != (kind, before):
            raise ValueError("Partition changed before memory move; nothing was sent")
    if await _system_bounds(hmc, system_uuid, system_name) != bounds:
        raise ValueError("System changed before memory move; nothing was sent")
    for _, before in peers:
        await authorize_lpar_mutation(
            hmc, system_name, before["name"], ownership_override=ownership_override
        )
    if move.quantity_mib == 0:
        results = [
            {**_result(before, before, target, "noop"), "read_error": None}
            for (_, before), target in zip(peers, targets, strict=True)
        ]
        return _aggregate(move, results[0], results[1], None, False)
    error = None
    native_rejection = False
    try:
        await move_lpar_memory_quantity(
            hmc.config,
            system_name,
            source["partition_id"],
            destination["partition_id"],
            move,
        )
    except HMCError as exc:
        error = str(exc)[:2048]
        native_rejection = isinstance(exc.__cause__, asyncssh.ProcessError) and (
            exc.__cause__.exit_status is not None
            and exc.__cause__.exit_status > 0
            and exc.__cause__.exit_signal is None
        )
    results = [
        await _observe_endpoint(
            hmc, system_uuid, kind, before, target, error, native_rejection
        )
        for (kind, before), target in zip(peers, targets, strict=True)
    ]
    return _aggregate(move, results[0], results[1], error, native_rejection)
