"""ST49: one explicit IBM i HMC-console create, readback and inactive cleanup."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from hmcpctl.errors import HMCError

from .lpar_config import (
    NAME_PREFIX,
    RESOURCES,
    TOKEN_PREFIX,
    _compare,
    _read_pools,
    _Run,
    _teardown,
    scratch_partitions,
)
from .observation import Assertion

if TYPE_CHECKING:
    from fastmcp import Client

    from .runner import RunState

GROUP = "lpar-physical-io"
SUBTASK = 49
SCENARIO = "st49-lpar-physical-io"


async def exercise_lpar_physical_io(client: Client, state: RunState) -> None:
    """No activation, profile apply, physical allocation or automatic retry."""
    if state.group != GROUP or state.hmc_config is None:
        state.skip(SUBTASK, GROUP, "requires opt-in group and HMC configuration")
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
        run.create_attempted = True
        status, data = await state.call(
            client,
            "hmc_create_lpar",
            system_name_or_uuid=run.system,
            name=run.name,
            partition_type="OS400",
            resources=RESOURCES,
            caller_token=run.token,
            physical_io={"console_slot": "hmc"},
            apply_partition_profile=False,
        )
        if status != "PASS" or not isinstance(data, dict):
            raise ValueError(
                "IBM i creation did not return a successful structured result"
            )
        lpar = data.get("lpar")
        run.uuid = lpar.get("UUID") if isinstance(lpar, dict) else None
        run.created = bool(run.uuid)
        steps = data.get("steps")
        if not isinstance(steps, list):
            raise TypeError("IBM i creation returned malformed workflow steps")
        verified = (
            data.get("workflow_completed") is True
            and data.get("ownership_stamped") is True
            and run.created
            and any(
                s.get("step") == "physical_io" and s.get("status") == "ok"
                for s in steps
                if isinstance(s, dict)
            )
        )
        if not verified:
            raise ValueError(
                "IBM i console profile readback/ownership was not confirmed"
            )
        state.record(SUBTASK, "explicit HMC console creation", "PASS", data)
    except (HMCError, ValueError, TypeError, PermissionError) as error:
        state.record(SUBTASK, GROUP, "FAIL", str(error))
    finally:
        await _teardown(client, state, run, inactive_only=True)
    same = await _compare(client, state, run, baseline[0])
    state.record_verified(
        SUBTASK,
        "hmc_create_lpar",
        operation="lpar.create",
        scenario=SCENARIO,
        assertions=[Assertion("explicit-hmc-console-readback", verified)],
        cleanup="passed" if run.deleted and same else "failed",
        data={
            "physical_assignment": "not-run",
            "sriov_assignment": "not-run",
            "activation": "not-run",
            "virtual_slot_forms": "excluded-see-638",
        },
    )
