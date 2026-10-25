"""ST55: one inactive true/true hardware memory access fixture, independently read."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.hardware_memory import (
    admit_hardware_memory,
    created_hardware_memory_step,
)
from hmcpctl.resource_identity import is_uuid, resolve_lpar_uuid, resolve_system_uuid

from .lpar_config import (
    NAME_PREFIX,
    RESOURCES,
    TOKEN_PREFIX,
    _compare,
    _read_pools,
    _Run,
    _teardown,
    _token_of,
    _uuid_of,
    scratch_partitions,
)
from .observation import Assertion, plain_data

if TYPE_CHECKING:
    from fastmcp import Client

    from .runner import RunState

SUBTASK = 55
GROUP = "lpar-hardware-memory"
SCENARIO = "st55-lpar-hardware-memory-creation"


async def _create(client: Client, state: RunState, run: _Run) -> None:
    run.create_attempted = True
    status, data = await state.call(
        client,
        "hmc_create_lpar",
        system_name_or_uuid=run.system,
        name=run.name,
        partition_type="AIX/Linux",
        resources=RESOURCES,
        caller_token=run.token,
        hardware_mem_encryption=True,
        hardware_mem_expansion=True,
        apply_partition_profile=False,
    )
    data = plain_data(data)
    if status != "PASS" or not isinstance(data, dict):
        raise ValueError("Hardware memory creation did not return structured success")
    identity = _uuid_of(data.get("lpar"))
    run.uuid = identity if identity and is_uuid(identity) else None
    run.created = run.uuid is not None
    if not (
        data.get("workflow_completed") is True
        and data.get("ownership_stamped") is True
        and run.created
    ):
        raise ValueError("Creation identity, ownership or workflow unconfirmed")


async def _read(client: Client, state: RunState, run: _Run, system_uuid: str) -> bool:
    if await _token_of(client, state, run, run.uuid or "") != run.token:
        raise ValueError("Fixture ownership changed; access verification stopped")
    assert state.hmc_config is not None and run.uuid is not None
    async with HMCClient(state.hmc_config) as hmc:
        await resolve_lpar_uuid(hmc, run.uuid, system_name_or_uuid=system_uuid)
        entry = await hmc.get_logical_partition(run.uuid)
        if _uuid_of(entry) != run.uuid:
            raise ValueError("Independent current partition identity unconfirmed")
        step = await created_hardware_memory_step(
            hmc, system_uuid, entry, run.name, "AIX/Linux", True, True
        )
    return step is not None and step.status == "ok"


async def exercise_lpar_hardware_memory(client: Client, state: RunState) -> None:
    """Observe requested access only; retain any fixture unsafe for shared cleanup."""
    if state.group != GROUP or state.hmc_config is None:
        state.skip(SUBTASK, GROUP, "requires opt-in group and HMC configuration")
        return
    try:
        async with HMCClient(state.hmc_config) as hmc:
            system_uuid = await resolve_system_uuid(hmc, state.config.system_name)
            await admit_hardware_memory(hmc, system_uuid, True, True)
    except (HMCError, OSError, ValueError, TypeError) as error:
        state.skip(SUBTASK, GROUP, f"admission unavailable: {type(error).__name__}")
        return
    baseline = await _read_pools(client, state, state.config.system_name)
    if baseline is None or scratch_partitions(baseline[0].names):
        state.skip(
            SUBTASK, GROUP, "baseline unavailable or stranded scratch; run recovery"
        )
        return
    suffix = uuid.uuid4().hex[:8]
    run = _Run(
        state.config.system_name,
        NAME_PREFIX + suffix,
        TOKEN_PREFIX + suffix,
        baseline[1],
        subtask=SUBTASK,
    )
    verified = False
    try:
        await _create(client, state, run)
        verified = await _read(client, state, run, system_uuid)
    except (HMCError, OSError, ValueError, TypeError) as error:
        state.record(SUBTASK, GROUP, "FAIL", type(error).__name__)
    finally:
        await _teardown(client, state, run, inactive_only=True)
    same = await _compare(client, state, run, baseline[0])
    state.record_verified(
        SUBTASK,
        "hmc_create_lpar",
        operation="lpar.create",
        scenario=SCENARIO,
        assertions=[Assertion("hardware-memory-access-readback", verified)],
        cleanup="passed" if run.deleted and same else "failed",
        data={
            "requested": {
                "hardware_mem_encryption": True,
                "hardware_mem_expansion": True,
            },
            "activation": "not-run",
            "runtime-encryption-expansion": "not-tested",
        },
    )
