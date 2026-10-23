"""ST52: inactive secure_boot=0 creation and one pending secure_boot=1 update."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.security_policy import read_security_policy_capabilities

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

GROUP = "lpar-security-policy"
SUBTASK = 52
SCENARIO = "st52-lpar-security-policy"


def _inactive_policy(value: object, pending: int, current: int | None) -> bool:
    return (
        isinstance(value, dict)
        and value.get("environment") == "AIX/Linux"
        and value.get("state") == "not activated"
        and isinstance(value.get("settings"), dict)
        and type(value["settings"].get("secure_boot")) is int
        and value["settings"]["secure_boot"] == pending
        and type(value.get("current_secure_boot")) is int
        and value["current_secure_boot"] in range(10)
        and (current is None or value["current_secure_boot"] == current)
    )


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
        security_policy={"secure_boot": 0},
        apply_partition_profile=False,
    )
    data = plain_data(data)
    if status != "PASS" or not isinstance(data, dict):
        raise ValueError("Security policy creation did not return structured success")
    run.uuid = _uuid_of(data.get("lpar"))
    run.created = run.uuid is not None
    steps = data.get("steps")
    if not (
        data.get("workflow_completed") is True
        and data.get("ownership_stamped") is True
        and run.created
        and isinstance(steps, list)
        and any(
            isinstance(step, dict)
            and step.get("step") == "security_policy"
            and step.get("status") == "ok"
            for step in steps
        )
    ):
        raise ValueError("Creation identity, ownership or policy was not confirmed")


async def _read(client: Client, state: RunState, run: _Run) -> object:
    status, data = await state.call(
        client,
        "hmc_get_lpar_security_policy",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
    )
    if status != "PASS":
        raise ValueError("Independent security policy read was unavailable")
    return plain_data(data)


async def _update(client: Client, state: RunState, run: _Run, before: dict) -> None:
    if await _token_of(client, state, run, run.uuid or "") != run.token:
        raise ValueError("Fixture ownership changed; stopped before policy update")
    status, result = await state.call(
        client,
        "hmc_configure_lpar_security_policy",
        system_name_or_uuid=run.system,
        lpar_name_or_uuid=run.uuid,
        settings={"secure_boot": 1},
    )
    result = plain_data(result)
    observed = result.get("observed") if isinstance(result, dict) else None
    if (
        status != "PASS"
        or not isinstance(result, dict)
        or result.get("status") != "verified"
        or not isinstance(observed, dict)
        or not _inactive_policy(observed, 1, before["current_secure_boot"])
        or observed["settings"] != before["settings"] | {"secure_boot": 1}
    ):
        raise ValueError("Pending policy update or preserved current policy unverified")
    if await _read(client, state, run) != observed:
        raise ValueError("Independent security policy read did not confirm update")


async def exercise_lpar_security_policy(client: Client, state: RunState) -> None:
    """One owned inactive fixture, one policy update, guarded cleanup; no key effects."""
    if state.group != GROUP or state.hmc_config is None:
        state.skip(SUBTASK, GROUP, "requires opt-in group and HMC configuration")
        return
    try:
        async with HMCClient(state.hmc_config) as hmc:
            capabilities = await read_security_policy_capabilities(
                hmc, state.config.system_name
            )
        if not capabilities.secure_boot:
            state.skip(SUBTASK, GROUP, "selected-system secure boot capability absent")
            return
    except (HMCError, ValueError) as error:
        state.skip(SUBTASK, GROUP, f"capability unavailable: {type(error).__name__}")
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
    created_verified = False
    updated_verified = False
    try:
        await _create(client, state, run)
        before = await _read(client, state, run)
        if not isinstance(before, dict) or not _inactive_policy(before, 0, None):
            raise ValueError("Independent inactive creation policy/current unconfirmed")
        created_verified = True
        await _update(client, state, run, before)
        updated_verified = True
    except (HMCError, ValueError, TypeError, PermissionError) as error:
        state.record(SUBTASK, GROUP, "FAIL", str(error))
    finally:
        await _teardown(client, state, run, inactive_only=True)
    same = await _compare(client, state, run, baseline[0])
    cleanup = "passed" if run.deleted and same else "failed"
    state.record_verified(
        SUBTASK,
        "hmc_create_lpar",
        operation="lpar.create",
        scenario=SCENARIO,
        assertions=[
            Assertion("explicit-security-policy-creation-readback", created_verified)
        ],
        cleanup=cleanup,
        data={"variant": {"secure_boot": 0}, "activation": "not-run"},
    )
    state.record_verified(
        SUBTASK,
        "hmc_configure_lpar_security_policy",
        operation="lpar.configure_security_policy",
        scenario=SCENARIO,
        assertions=[Assertion("pending-policy-current-preserved", updated_verified)],
        cleanup=cleanup,
        data={"variant": {"secure_boot": 1}, "keystore-dynamic-bypass": "not-run"},
    )
    state.record_verified(
        SUBTASK,
        "hmc_get_lpar_security_policy",
        operation="lpar.get_security_policy",
        scenario=SCENARIO,
        assertions=[
            Assertion(
                "independent-policy-readback", created_verified and updated_verified
            )
        ],
        cleanup=cleanup,
        data={"guest-enforcement-keys": "not-run", "other-platforms": "not-run"},
    )
