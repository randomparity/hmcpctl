"""One native processor move with two authorized endpoint observations (ADR 0300)."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, localcontext
from typing import Any

import asyncssh

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar_processors import (
    PROCESSOR_NUMERIC_TOKEN_BYTES,
    LparProcessorMove,
    bounded_processor_number,
)
from hmcpctl.errors import HMCError
from hmcpctl.resource_identity import (
    is_uuid,
    resolve_system_uuid,
    system_name_from_uuid,
)
from hmcpctl.ssh.processors import (
    move_lpar_processor_quantity,
    read_lpar_processor_quantities,
)

from . import memory, processors
from .ownership import authorize_lpar_mutation

_QUANTITIES = ("processors", "processing_units")


async def _read_endpoint(hmc, system_uuid, system_name, selector):
    if (await resolve_system_uuid(hmc, system_name)).lower() != system_uuid.lower():
        raise ValueError("Processor move CLI system identity changed")
    kind, uuid = await memory._contained_partition(hmc, system_uuid, selector)
    read = hmc.get_vios if kind == "VirtualIOServer" else hmc.get_logical_partition
    entry = await read(uuid)
    identity = processors._bounded_memory_snapshot(entry, uuid)
    if not isinstance(identity["name"], str) or not identity["name"].strip():
        raise ValueError("Processor move requires a reported partition name")
    if not is_uuid(selector) and identity["name"] != selector:
        raise ValueError("Processor move partition name changed")
    if (identity["partition_type"] == "Virtual IO Server") != (
        kind == "VirtualIOServer"
    ):
        raise ValueError("Processor move partition kind differs from contained feed")
    row = await read_lpar_processor_quantities(
        hmc.config, system_name, identity["partition_id"]
    )
    return kind, processors._snapshot(entry, uuid, row)


def _same_identity(left, right):
    return left[0] == right[0] and left[1]["uuid"].lower() == right[1]["uuid"].lower()


def _same_evidence(left, right):
    if not _same_identity(left, right):
        return False
    return {k: v for k, v in left[1].items() if k != "uuid"} == {
        k: v for k, v in right[1].items() if k != "uuid"
    }


def _deferred(before, force):
    if (
        before["state"] not in ("running", "not activated")
        or before["operation_in_progress"]
    ):
        raise ValueError(
            "Processor move requires stable running or not activated endpoints"
        )
    if before["state"] == "not activated":
        return True
    capability = before["dynamic_processor"]
    if capability not in (True, False, "true", "false", "1", "0"):
        raise ValueError("Processor move requires reported DLPAR capability")
    aix = before["partition_type"] in ("AIX/Linux", "Virtual IO Server")
    if aix and before["rmc"] != "active" and force:
        return True
    if capability not in (True, "true", "1") or aix and before["rmc"] != "active":
        raise ValueError(
            "Processor move requires DLPAR capability and applicable active RMC"
        )
    return False


def _interval(before, quantity):
    low = before[f"current_minimum_{quantity}"]
    high = before[f"current_maximum_{quantity}"]
    if before["state"] == "not activated" and high == 0:
        low = before[f"pending_minimum_{quantity}"]
        high = before[f"pending_maximum_{quantity}"]
    return (
        max(low, before[f"pending_minimum_{quantity}"]),
        min(high, before[f"pending_maximum_{quantity}"]),
    )


def _require_feasible(before, expected, capacity):
    low, high = _interval(before, "processors")
    high = min(high, capacity["count_limit"], capacity["os_limit"])
    count, units = expected["processors"], expected["processing_units"]
    if count is not None:
        if not low <= count <= high:
            raise ValueError(
                "Processor move crosses source minimum or destination count maximum"
            )
        low = high = count
    if units is None:
        if high > capacity["total"] or low < 0:
            raise ValueError("Processor move exceeds dedicated system capacity")
        return
    minimum, maximum = _interval(before, "processing_units")
    if not minimum <= units <= min(maximum, capacity["total"]):
        raise ValueError("Processor move crosses entitlement bounds")
    with localcontext(prec=2 * PROCESSOR_NUMERIC_TOKEN_BYTES + 2):
        low = max(low, int(units.to_integral_value(rounding=ROUND_CEILING)))
        high = min(
            high,
            int((units / capacity["floor"]).to_integral_value(rounding=ROUND_FLOOR)),
        )
    if low > high or low < 0:
        raise ValueError(
            "Processor move has no feasible virtual-processor entitlement envelope"
        )


def _targets(source, destination, move, system):
    modes = source["processor_mode"], destination["processor_mode"]
    if modes == ("ded", "ded") and move.processing_units is not None:
        raise ValueError("Dedicated moves require processors, not processing_units")
    if modes == ("ded", "shared") and move.processing_units is not None:
        raise ValueError("Dedicated-to-shared moves require processors only")
    if modes == ("shared", "ded"):
        if move.processors is not None or move.processing_units is None:
            raise ValueError("Shared-to-dedicated moves require processing_units only")
        bounded_processor_number(move.processing_units, whole=True)
    if move.force and not any(
        p["partition_type"] in ("AIX/Linux", "Virtual IO Server")
        for p in (source, destination)
    ):
        raise ValueError(
            "Processor move force applies only to AIX/Linux/VIOS endpoints"
        )
    mixed = modes[0] != modes[1]
    noop = all(value in (None, 0) for value in (move.processors, move.processing_units))
    results = []
    for before, sign in ((source, -1), (destination, 1)):
        shared = before["processor_mode"] == "shared"
        count = before["current_processors"]
        units = before.get("current_processing_units")
        with localcontext(prec=2 * PROCESSOR_NUMERIC_TOKEN_BYTES + 2):
            if mixed:
                amount = move.processors if modes[0] == "ded" else move.processing_units
                if shared:
                    units += sign * amount
                    count = before["current_processors"] if noop else None
                else:
                    count += sign * int(amount)
            else:
                count += sign * (move.processors or 0)
                if shared:
                    units += sign * (move.processing_units or 0)
        expected = {
            "processors": count,
            "processing_units": units,
            "reset": False,
            "deferred": _deferred(before, move.force),
            "noop": noop,
        }
        for quantity in _QUANTITIES:
            if expected[quantity] is not None:
                expected[quantity] = bounded_processor_number(
                    expected[quantity], whole=quantity == "processors"
                )
        _require_feasible(before, expected, processors._capacity(system, before))
        results.append(expected)
    return tuple(results)


def _postconditions(before, after, expected, capacity):
    for key in (
        "name",
        "partition_id",
        "state",
        "partition_type",
        "processor_mode",
        "shared",
    ):
        if before[key] != after[key]:
            raise ValueError("Processor move readback identity, state or mode changed")
    if before["uuid"].lower() != after["uuid"].lower():
        raise ValueError("Processor move readback UUID changed")
    actual = {key: after.get(f"current_{key}") for key in _QUANTITIES}
    _require_feasible(before, actual, capacity)
    _require_feasible(after, actual, capacity)
    checks = [not after["operation_in_progress"]]
    for quantity in _QUANTITIES:
        if actual[quantity] is None:
            continue
        if expected[quantity] is not None:
            checks.append(actual[quantity] == expected[quantity])
        if not expected["deferred"]:
            checks.append(after[f"runtime_{quantity}"] == actual[quantity])
        initialized = before["state"] == "not activated" and all(
            before[f"current_{bound}{quantity}"] == 0
            for bound in ("minimum_", "", "maximum_")
        )
        for bound in ("minimum_", "maximum_"):
            key = f"current_{bound}{quantity}"
            checks.append(
                before[f"pending_minimum_{quantity}"]
                <= after[key]
                <= before[f"pending_maximum_{quantity}"]
                if initialized
                else after[key] == before[key]
            )
    checks.extend(before[key] == after[key] for key in processors._MEMORY)
    return all(checks)


def _endpoint_result(before, after, expected, outcome, error=None, read_error=None):
    result = processors._result(before, after, expected, outcome, error, read_error)
    return {
        **result,
        "read_error": None if read_error is None else read_error[:2048],
        "verified_quantities": [
            key for key in _QUANTITIES if expected[key] is not None
        ],
        "observed_only_quantities": ["processors"]
        if expected["processors"] is None
        else [],
    }


async def _observe(
    hmc, system_uuid, system_name, peer, expected, capacity, error, rejected
):
    kind, before = peer
    after = None
    try:
        fresh = await _read_endpoint(hmc, system_uuid, system_name, before["uuid"])
        after = fresh[1]
        if not _same_identity(peer, fresh):
            raise ValueError("Processor move readback containment or kind changed")
        matched = _postconditions(before, after, expected, capacity)
    except (HMCError, ValueError) as exc:
        return _endpoint_result(before, after, expected, "unverified", error, str(exc))
    changed = not _same_evidence(peer, (kind, after))
    outcome = (
        "applied"
        if matched
        else "partial"
        if changed
        else "rejected"
        if rejected
        else "unverified"
    )
    return _endpoint_result(before, after, expected, outcome, error)


def _aggregate(move, results, error, rejected):
    if all(r["outcome"] == "noop" for r in results):
        outcome = "noop"
    elif all(r["outcome"] == "applied" for r in results):
        outcome = "applied"
    elif any(r["after"] is None or r["read_error"] for r in results):
        outcome = "unverified"
    elif any(r["outcome"] in ("applied", "partial") for r in results):
        outcome = "partial"
    else:
        outcome = "rejected" if rejected else "unverified"
    return {
        "outcome": outcome,
        "workflow_completed": outcome in ("applied", "noop"),
        "move": move.model_dump(mode="json"),
        "submission_error": error,
        "source": results[0],
        "destination": results[1],
    }


async def move_lpar_processors(
    hmc: HMCClient,
    lpar_name_or_uuid: str,
    target_lpar_name_or_uuid: str,
    move: LparProcessorMove,
    *,
    system_name_or_uuid: str,
    ownership_override: bool = False,
) -> dict[str, Any]:
    """Authorize both endpoints, submit once and retain two independent observations."""
    if not isinstance(move, LparProcessorMove):
        raise TypeError("move must be a typed LparProcessorMove")
    if any(
        not isinstance(s, str) or not s.strip()
        for s in (lpar_name_or_uuid, target_lpar_name_or_uuid, system_name_or_uuid)
    ):
        raise ValueError(
            "Processor move requires source, destination and managed system"
        )
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    system_name = await system_name_from_uuid(hmc, system_uuid)
    if (
        not system_name
        or (await resolve_system_uuid(hmc, system_name)).lower() != system_uuid.lower()
    ):
        raise ValueError("Processor move system has no unique CLI name")
    peers = [
        await _read_endpoint(hmc, system_uuid, system_name, selector)
        for selector in (lpar_name_or_uuid, target_lpar_name_or_uuid)
    ]
    source, destination = (p[1] for p in peers)
    if (
        source["uuid"].lower() == destination["uuid"].lower()
        or source["partition_id"] == destination["partition_id"]
    ):
        raise ValueError("Processor move requires distinct endpoints")
    for _, before in peers:
        await authorize_lpar_mutation(
            hmc,
            system_name,
            before["name"],
            ownership_override=ownership_override,
            max_output_bytes=65536,
        )
    system = await hmc.get_managed_system(system_uuid) or {}
    capacities = [processors._capacity(system, p[1]) for p in peers]
    if any(
        str(c["uuid"]).lower() != system_uuid.lower() or c["name"] != system_name
        for c in capacities
    ):
        raise ValueError("Processor move system capacity identity differs")
    targets = _targets(source, destination, move, system)
    fresh = [
        await _read_endpoint(hmc, system_uuid, system_name, p[1]["uuid"]) for p in peers
    ]
    fresh_system = await hmc.get_managed_system(system_uuid) or {}
    fresh_capacities = [processors._capacity(fresh_system, p[1]) for p in fresh]
    if any(not _same_evidence(a, b) for a, b in zip(peers, fresh, strict=True)) or any(
        {**a, "uuid": str(a["uuid"]).lower()} != {**b, "uuid": str(b["uuid"]).lower()}
        for a, b in zip(capacities, fresh_capacities, strict=True)
    ):
        raise ValueError("Processor move decision changed; nothing was sent")
    if (await resolve_system_uuid(hmc, system_name)).lower() != system_uuid.lower():
        raise ValueError("Processor move system changed; nothing was sent")
    for _, before in fresh:
        await authorize_lpar_mutation(
            hmc,
            system_name,
            before["name"],
            ownership_override=ownership_override,
            max_output_bytes=65536,
        )
    if all(t["noop"] for t in targets):
        results = [
            _endpoint_result(p[1], f[1], t, "noop")
            for p, f, t in zip(peers, fresh, targets, strict=True)
        ]
        return _aggregate(move, results, None, False)
    error, rejected = None, False
    try:
        await move_lpar_processor_quantity(
            hmc.config,
            system_name,
            source["partition_id"],
            destination["partition_id"],
            move,
        )
    except HMCError as exc:
        error = str(exc)[:2048]
        cause = exc.__cause__
        rejected = (
            isinstance(cause, asyncssh.ProcessError)
            and cause.exit_status is not None
            and cause.exit_status > 0
            and not cause.exit_signal
        )
    results = [
        await _observe(hmc, system_uuid, system_name, p, t, c, error, rejected)
        for p, t, c in zip(peers, targets, capacities, strict=True)
    ]
    return _aggregate(move, results, error, rejected)
