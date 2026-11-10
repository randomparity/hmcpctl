"""One native processor move with two authorized endpoint observations (ADR 0300)."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Any, Literal, cast

import asyncssh

from hmcpctl.client.core import HMCClient
from hmcpctl.documents.lpar_processors import (
    PROCESSOR_NUMERIC_TOKEN_BYTES,
    LparProcessorMove,
    bounded_processor_number,
)
from hmcpctl.errors import TLS_OBSERVATION_ABORTED, HMCError, HMCTLSFingerprintError
from hmcpctl.operations.lpar import processors
from hmcpctl.operations.lpar.observation import contained_partition
from hmcpctl.resource_identity import (
    is_uuid,
    resolve_system_uuid,
    system_name_from_uuid,
)
from hmcpctl.ssh.processors import (
    move_lpar_processor_quantity,
    read_lpar_processor_quantities,
)

from .ownership import authorize_lpar_mutation

_Peer = tuple[str, processors._ProcessorSnapshot]

_QUANTITIES: tuple[Literal["processors", "processing_units"], ...] = (
    "processors",
    "processing_units",
)


async def _read_endpoint(
    hmc: HMCClient, system_uuid: str, system_name: str, selector: str
) -> _Peer:
    if (await resolve_system_uuid(hmc, system_name)).lower() != system_uuid.lower():
        raise ValueError("Processor move CLI system identity changed")
    kind, uuid = await contained_partition(hmc, system_uuid, selector)
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


def _same_identity(left: _Peer, right: _Peer) -> bool:
    return left[0] == right[0] and left[1]["uuid"].lower() == right[1]["uuid"].lower()


def _same_evidence(left: _Peer, right: _Peer) -> bool:
    if not _same_identity(left, right):
        return False
    return {k: v for k, v in left[1].items() if k != "uuid"} == {
        k: v for k, v in right[1].items() if k != "uuid"
    }


def _deferred(before: processors._ProcessorSnapshot, force: bool) -> bool:
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


def _interval(
    before: processors._ProcessorSnapshot,
    quantity: Literal["processors", "processing_units"],
) -> tuple[int | Decimal, int | Decimal]:
    values = processors._quantity(before, quantity)
    low, high = values["minimum"], values["maximum"]
    if before["state"] == "not activated" and high == 0:
        low, high = values["pending_minimum"], values["pending_maximum"]
    return max(low, values["pending_minimum"]), min(high, values["pending_maximum"])


def _require_feasible(
    before: processors._ProcessorSnapshot,
    expected: processors._ProcessorQuantities,
    capacity: processors._ProcessorCapacity,
) -> None:
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


def _targets(
    source: processors._ProcessorSnapshot,
    destination: processors._ProcessorSnapshot,
    move: LparProcessorMove,
    system: dict[str, Any],
) -> tuple[processors._ProcessorTargets, processors._ProcessorTargets]:
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
    results: list[processors._ProcessorTargets] = []
    for before, sign in ((source, -1), (destination, 1)):
        shared = before["processor_mode"] == "shared"
        count = before["current_processors"]
        units = before.get("current_processing_units")
        with localcontext(prec=2 * PROCESSOR_NUMERIC_TOKEN_BYTES + 2):
            if mixed:
                # Mixed-mode admission above requires the selected quantity.
                amount = cast(
                    int | Decimal,
                    move.processors if modes[0] == "ded" else move.processing_units,
                )
                if shared:
                    units = before["current_processing_units"] + sign * amount
                    count = before["current_processors"] if noop else None
                else:
                    count += sign * int(amount)
            else:
                count += sign * (move.processors or 0)
                if shared:
                    units = before["current_processing_units"] + sign * (
                        move.processing_units or 0
                    )
        expected: processors._ProcessorTargets = {
            "processors": count,
            "processing_units": units,
            "reset": False,
            "deferred": _deferred(before, move.force),
            "noop": noop,
        }
        if expected["processors"] is not None:
            expected["processors"] = cast(
                int, bounded_processor_number(expected["processors"], whole=True)
            )
        if expected["processing_units"] is not None:
            expected["processing_units"] = cast(
                Decimal, bounded_processor_number(expected["processing_units"])
            )
        _require_feasible(before, expected, processors._capacity(system, before))
        results.append(expected)
    return results[0], results[1]


def _postconditions(
    before: processors._ProcessorSnapshot,
    after: processors._ProcessorSnapshot,
    expected: processors._ProcessorTargets,
    capacity: processors._ProcessorCapacity,
) -> bool:
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
    actual: processors._ProcessorQuantities = {
        "processors": after.get("current_processors"),
        "processing_units": after.get("current_processing_units"),
    }
    _require_feasible(before, actual, capacity)
    _require_feasible(after, actual, capacity)
    checks = [not after["operation_in_progress"]]
    for quantity in _QUANTITIES:
        value = actual[quantity]
        if value is None:
            continue
        original = processors._quantity(before, quantity)
        observed = processors._quantity(after, quantity)
        if expected[quantity] is not None:
            checks.append(value == expected[quantity])
        if not expected["deferred"]:
            checks.append(observed["runtime"] == value)
        initialized = before["state"] == "not activated" and all(
            original[bound] == 0 for bound in ("minimum", "current", "maximum")
        )
        for bound in ("minimum", "maximum"):
            checks.append(
                original["pending_minimum"]
                <= observed[bound]
                <= original["pending_maximum"]
                if initialized
                else observed[bound] == original[bound]
            )
    checks.extend(before[key] == after[key] for key in processors._MEMORY)
    return all(checks)


def _endpoint_result(
    before: processors._ProcessorSnapshot,
    after: processors._ProcessorSnapshot | None,
    expected: processors._ProcessorTargets,
    outcome: str,
    error: str | None = None,
    read_error: str | None = None,
) -> dict[str, Any]:
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
    hmc: HMCClient,
    system_uuid: str,
    system_name: str,
    peer: _Peer,
    expected: processors._ProcessorTargets,
    capacity: processors._ProcessorCapacity,
    error: str | None,
    rejected: bool,
) -> dict[str, Any]:
    kind, before = peer
    after = None
    try:
        fresh = await _read_endpoint(hmc, system_uuid, system_name, before["uuid"])
        after = fresh[1]
        if not _same_identity(peer, fresh):
            raise ValueError("Processor move readback containment or kind changed")
        matched = _postconditions(before, after, expected, capacity)
    except HMCTLSFingerprintError:
        raise
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


def _aggregate(
    move: LparProcessorMove,
    results: list[dict[str, Any]],
    error: str | None,
    rejected: bool,
) -> dict[str, Any]:
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
    results: list[dict[str, Any]] = []
    tls_failed = False
    for peer, target, capacity in zip(peers, targets, capacities, strict=True):
        if not tls_failed:
            try:
                results.append(
                    await _observe(
                        hmc,
                        system_uuid,
                        system_name,
                        peer,
                        target,
                        capacity,
                        error,
                        rejected,
                    )
                )
                continue
            except HMCTLSFingerprintError:
                tls_failed = True
        results.append(
            _endpoint_result(
                peer[1], None, target, "unverified", error, TLS_OBSERVATION_ABORTED
            )
        )
    return _aggregate(move, results, error, rejected)
