"""An opt-in change and restoration of one empty shared processor pool."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastmcp import Client

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.systems.processor_pool_update import (
    capacity_evidence,
)
from hmcpctl.operations.systems.processor_pools import (
    SharedProcessorPool,
    get_shared_processor_pool,
    list_shared_processor_pools,
)
from hmcpctl.resource_identity import is_uuid

from ..observation import Assertion
from ..recovery.common import Finding, StateUnreadable

if TYPE_CHECKING:
    from ..runner import RunState

GROUP = "shared-processor-pool-update"
SUBTASK = 75
BASELINE = "shared processor pool baseline"
_FIELDS = (
    "uuid",
    "pool_id",
    "name",
    "maximum_processing_units",
    "reserved_processing_units",
    "pending_reserved_processing_units",
)


def eligible(pool: SharedProcessorPool) -> bool:
    return (
        pool.pool_id > 0
        and not pool.configured
        and pool.name == f"SharedPool{pool.pool_id:02d}"
        and pool.maximum_processing_units
        == pool.reserved_processing_units
        == pool.pending_reserved_processing_units
        == 0
        and not pool.assigned_partition_uuids
    )


def baseline_data(system: str, pool: SharedProcessorPool) -> dict[str, Any]:
    values = asdict(pool)
    return {"system_uuid": system, "pool": {key: values[key] for key in _FIELDS}}


def recovery_input(document: dict) -> dict:
    rows = [
        row.get("data")
        for row in document.get("results", [])
        if isinstance(row, dict)
        and row.get("subtask") == SUBTASK
        and row.get("tool") == BASELINE
    ]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("Expected one checkpointed shared processor pool baseline")
    data = rows[0]
    pool = data.get("pool")
    if (
        not isinstance(pool, dict)
        or set(pool) != set(_FIELDS)
        or not isinstance(data.get("system_uuid"), str)
        or not is_uuid(data["system_uuid"])
        or not isinstance(pool["uuid"], str)
        or not is_uuid(pool["uuid"])
        or type(pool["pool_id"]) is not int
        or pool["pool_id"] <= 0
        or pool["name"] != f"SharedPool{pool['pool_id']:02d}"
        or any(
            type(pool[key]) not in (int, float) or pool[key] != 0 for key in _FIELDS[3:]
        )
    ):
        raise ValueError("Incomplete or invalid shared processor pool baseline")
    return data


async def recover(call, baseline: dict) -> list[Finding]:
    original = baseline["pool"]
    status, data = await call(
        "hmc_get_shared_processor_pool",
        system_name_or_uuid=baseline["system_uuid"],
        pool=original["pool_id"],
    )
    if (
        status != "PASS"
        or not isinstance(data, dict)
        or data.get("system_uuid") != baseline["system_uuid"]
        or not isinstance(data.get("pool"), dict)
    ):
        raise StateUnreadable(
            "Could not independently read the original shared processor pool"
        )
    if any(data["pool"].get(key) != original[key] for key in _FIELDS):
        return [
            Finding(
                "Shared processor pool differs from baseline",
                "Original slot identity, name or capacity differs",
                "Inspect the saved baseline and the slot before making a manual correction.",
            )
        ]
    return []


async def _submit(
    client: Client,
    state: RunState,
    system: str,
    pool_id: int,
    name: str,
    maximum: int,
    reserved: str,
) -> None:
    status, data = await state.call(
        client,
        "hmc_update_shared_processor_pool",
        system_name_or_uuid=system,
        pool=pool_id,
        new_name=name,
        maximum_units=maximum,
        reserved_units=reserved,
    )
    if (
        status != "PASS"
        or not isinstance(data, dict)
        or data.get("outcome") != "verified"
        or data.get("route") != "chhwres"
        or data.get("if_match") != "not-applicable"
    ):
        raise ValueError(
            "Public pool update did not confirm the native configuration change"
        )


async def _round_trip(client: Client, hmc: HMCClient, state: RunState) -> None:
    listing = await list_shared_processor_pools(
        hmc, state.config.system_name, include_unconfigured=True
    )
    original = next((pool for pool in listing.pools if eligible(pool)), None)
    if original is None:
        raise ValueError("No unconfigured non-default zero-capacity placeholder slot")
    system = listing.system_uuid
    capacity = await capacity_evidence(
        hmc, system, original.pool_id, original.assigned_partition_uuids
    )
    if capacity.member_count or capacity.available < Decimal("0.05"):
        raise ValueError(
            "The selected pool has members or the system lacks sufficient available units"
        )
    name = "hmcp" + uuid4().hex[:8]
    if any(pool.name == name for pool in listing.pools):
        raise ValueError("Temporary shared processor pool name already exists")
    baseline = baseline_data(system, original)
    state.record(SUBTASK, BASELINE, "PASS", {**baseline, "temporary_name": name})
    if state.checkpoint is None:
        raise ValueError("No durable checkpoint is available for the pool update")
    state.checkpoint()
    before = await get_shared_processor_pool(hmc, system, original.pool_id)
    if before.pool != original:
        raise ValueError("Selected pool changed after its baseline checkpoint")
    try:
        await _submit(client, state, system, original.pool_id, name, 1, "0.05")
    finally:
        observed = (await get_shared_processor_pool(hmc, system, original.pool_id)).pool
        if (
            observed.uuid != original.uuid
            or observed.name != name
            or observed.maximum_processing_units != 1
            or observed.pending_reserved_processing_units != 0.05
        ):
            raise ValueError(
                "Temporary pool change unconfirmed; inspect baseline before recovery"
            )
        current = await capacity_evidence(
            hmc, system, original.pool_id, observed.assigned_partition_uuids
        )
        if current.member_count:
            raise ValueError("Pool acquired a member; manual recovery required")
        await _submit(client, state, system, original.pool_id, original.name, 0, "0")
    restored = (await get_shared_processor_pool(hmc, system, original.pool_id)).pool
    if baseline_data(system, restored) != baseline:
        raise ValueError("Pool restoration differs from checkpoint; run recovery")
    state.record_verified(
        SUBTASK,
        "hmc_update_shared_processor_pool",
        operation="processor_pool.update",
        scenario="st75-shared-processor-pool-update",
        assertions=[
            Assertion("name-maximum-pending-matched", True),
            Assertion(
                "current-reservation-read",
                observed.reserved_processing_units is not None,
            ),
            Assertion("original-slot-restored", True),
        ],
        cleanup="passed",
        data={"route": "chhwres", "if_match": "not-applicable"},
    )


async def exercise_processor_pool(client: Client, state: RunState) -> None:
    if state.group != GROUP or state.hmc_config is None:
        state.record(
            SUBTASK, GROUP, "FAIL", "Expected the named group and an HMC profile"
        )
        return
    try:
        async with HMCClient(state.hmc_config) as hmc:
            await _round_trip(client, hmc, state)
    except (ValueError, HMCError) as error:
        state.record(SUBTASK, GROUP, "FAIL", str(error))
