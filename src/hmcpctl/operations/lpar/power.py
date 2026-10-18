"""``hmc_power_lpar``'s operation body and effect classifiers (ADR 0199).

Each call writes at most one HMC job, journaled as the ``power`` effect, then waits a
bounded time for the job and for the partition to reach the action's state. Messages
name the partition by UUID and never echo a caller's selector (``engine`` module note).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any, Literal

from ...client.client_factory import client_from_env
from ...client.core import HMCClient
from ...errors import HMCError
from ...jobs import (
    SUCCESSFUL_JOB_STATUSES,
    job_identifier,
    job_outcome,
    power_on_lpar_job,
)
from ...resource_identity import resolve_system_uuid
from ..logical import engine
from ..logical.engine import (
    BodyResult,
    Classification,
    OperationContext,
    OperationFailed,
)
from ..logical.models import EffectRecord
from ..logical.store import DEFAULT_CONNECTION
from .activation import ACTIVATED_STATES, submit_power_off
from .ownership import resolve_and_authorize_lpar_mutation

PowerAction = Literal["start", "stop", "restart"]
PowerMode = Literal["graceful", "immediate"]
ACTIONS: tuple[PowerAction, ...] = ("start", "stop", "restart")
MODES: tuple[PowerMode, ...] = ("graceful", "immediate")
# ADR 0189 Decision 2, per action: the specialist whose authority the action uses.
DELEGATED: Mapping[PowerAction, str] = {
    "start": "hmc_power_on_lpar",
    "stop": "hmc_power_off_lpar",
    "restart": "hmc_power_off_lpar",
}
# Persisted contract (ADR 0195 Consequences): never change while an operation can be open.
EFFECT_KEY = "power"
EFFECT_KINDS: Mapping[PowerAction, str] = {
    "start": "lpar.power_on",
    "stop": "lpar.power_off",
    "restart": "lpar.restart",
}
JOB_TIMEOUT_SECONDS = 300
SETTLE_SECONDS = 120
POLL_SECONDS = 5
_NOT_ACTIVATED = "not activated"


def check_inputs(action: str, mode: str) -> None:
    """Refuse an unknown action or mode, and a mode on ``start`` (ADR 0199 Decision 1)."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if action == "start" and mode == "immediate":
        raise ValueError("mode applies to stop and restart only; omit it for start")


def open_client(connection: str) -> HMCClient:
    """A client for the operation's recorded connection (``<default>`` is the environment)."""
    return client_from_env(None if connection == DEFAULT_CONNECTION else connection)


def _norm(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


async def _state(hmc: HMCClient, lpar_uuid: str) -> str:
    return _norm(
        await hmc.get_quick_property("LogicalPartition", lpar_uuid, "PartitionState")
    )


def _match(entries: list[dict[str, Any]], selector: str) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if str(entry.get("UUID") or "").lower() == selector.lower()
        or (entry.get("Resource") or {}).get("PartitionName") == selector
    ]


async def _resolve_and_authorize_power_target(
    hmc: HMCClient, system: str, lpar: str
) -> tuple[str, str, dict[str, Any]]:
    """Resolve the target, enforce configured ownership authorization, and return its entry."""
    try:
        # Lower-cased: the guard key must not depend on the caller's spelling (ADR 0094).
        system_uuid = (await resolve_system_uuid(hmc, system)).lower()
        matches = _match(await hmc.list_logical_partitions(system_uuid), lpar)
    except (HMCError, ValueError, LookupError) as exc:
        raise OperationFailed(
            f"the managed system or its partition list could not be read ({type(exc).__name__})"
        ) from exc
    if len(matches) != 1:
        found = "no partition" if not matches else "more than one partition"
        raise OperationFailed(
            f"the partition selector matched {found} on managed system {system_uuid}"
        )
    entry = matches[0]
    lpar_uuid = str(entry["UUID"]).lower()
    if hmc.config.authorize_power_operations:
        try:
            await resolve_and_authorize_lpar_mutation(
                hmc, system_uuid, lpar_uuid, ownership_override=False
            )
        except (HMCError, ValueError, LookupError, PermissionError) as exc:
            # The refusal text names the partition and an override this tool does not
            # offer (ADR 0199 Decision 7), so only its type is kept.
            raise OperationFailed(
                f"the ADR 0011 ownership check refused or could not read LPAR "
                f"{lpar_uuid} ({type(exc).__name__}); see hmc_list_lpar_ownership"
            ) from exc
    return system_uuid, lpar_uuid, entry


def _precheck(
    action: PowerAction, mode: PowerMode, lpar_uuid: str, entry: dict[str, Any]
) -> bool:
    """Return True when already in the action's state; raise when it cannot start."""
    resource = entry.get("Resource") or {}
    state = _norm(resource.get("PartitionState"))
    if action == "start":
        if state in ACTIVATED_STATES:
            return True
        if state != _NOT_ACTIVATED:
            raise OperationFailed(
                f"LPAR {lpar_uuid} is {state!r}; start needs 'not activated'"
            )
        return False
    if state == _NOT_ACTIVATED:
        if action == "stop":
            return True
        raise OperationFailed(f"LPAR {lpar_uuid} is not activated; use action=start")
    allowed = ACTIVATED_STATES | ({"error"} if mode == "immediate" else set())
    if state not in allowed:
        hint = "" if mode == "immediate" else ", or 'error' with mode=immediate"
        raise OperationFailed(
            f"LPAR {lpar_uuid} is {state!r}; {action} needs it activated{hint}"
        )
    rmc = _norm(resource.get("ResourceMonitoringControlState"))
    if mode == "graceful" and rmc != "active":
        raise OperationFailed(
            f"a graceful {action} asks the operating system and needs RMC 'active'; "
            f"LPAR {lpar_uuid} reports {rmc or 'no RMC state'!r}. Pass mode=immediate "
            "to act without the operating system"
        )
    return False


async def _submit(
    hmc: HMCClient, action: PowerAction, mode: PowerMode, lpar_uuid: str
) -> dict[str, Any]:
    if action == "start":
        job = await hmc.submit_job(
            f"/rest/api/uom/LogicalPartition/{lpar_uuid}/do/PowerOn",
            power_on_lpar_job(),
        )
    else:
        job = await submit_power_off(
            hmc,
            lpar_uuid,
            operation="osshutdown" if mode == "graceful" else "shutdown",
            immediate=mode == "immediate",
            restart=action == "restart",
        )
    job_id = None if job is None else job_identifier(job)
    if job_id is None:
        raise HMCError("the job submission returned no usable JobID")
    return {"job_id": job_id}


def _reached(action: PowerAction, state: str) -> bool:
    return state == _NOT_ACTIVATED if action == "stop" else state in ACTIVATED_STATES


async def _settle(hmc: HMCClient, action: PowerAction, lpar_uuid: str) -> str:
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        state = await _state(hmc, lpar_uuid)
        # 'not activated' right after a PowerOn may be lag, so only 'error' ends a start.
        if _reached(action, state) or (action == "start" and state == "error"):
            return state
        if time.monotonic() >= deadline:
            return state
        await asyncio.sleep(POLL_SECONDS)


async def _read_job(hmc: HMCClient, job_id: str, replay: bool) -> dict[str, Any] | None:
    """Poll the job; on a replay a job the HMC no longer has (404) is None."""
    try:
        return await hmc.wait_for_job_entry(job_id, JOB_TIMEOUT_SECONDS, POLL_SECONDS)
    except HMCError as exc:
        if replay and exc.status_code == 404:
            return None
        raise


async def _finish(
    hmc: HMCClient,
    action: PowerAction,
    lpar_uuid: str,
    result: dict[str, Any],
    *,
    replay: bool,
) -> BodyResult:
    job_id = result["job_id"]
    job = None if job_id is None else await _read_job(hmc, job_id, replay)
    if job is None and action == "restart":
        # A cycled partition and an uncycled one both read activated, so without the
        # job's own success there is no evidence the restart happened (ADR 0199).
        result["observed_state"] = await _state(hmc, lpar_uuid)
        return BodyResult("needs_attention", result)
    if job is not None:
        outcome = job_outcome(job_id, job)
        if outcome.timed_out:
            result["observed_state"] = await _state(hmc, lpar_uuid)
            return BodyResult("needs_attention", result)
        if outcome.status not in SUCCESSFUL_JOB_STATUSES:
            state = await _state(hmc, lpar_uuid)
            raise OperationFailed(
                f"{EFFECT_KINDS[action]} job {job_id} ended {outcome.status}: "
                f"{outcome.error}; LPAR {lpar_uuid} is {state!r}"
            )
    state = await _settle(hmc, action, lpar_uuid)
    result["observed_state"] = state
    if _reached(action, state):
        return BodyResult("completed", result)
    if action == "start" and state == "error":
        raise OperationFailed(
            f"LPAR {lpar_uuid} is 'error' after PowerOn: activation failed"
        )
    return BodyResult("needs_attention", result)


def _result(
    action: PowerAction,
    mode: PowerMode,
    system_uuid: str,
    lpar_uuid: str,
    state: str | None,
) -> dict[str, Any]:
    return {
        "action": action,
        "mode": mode,
        "system_uuid": system_uuid,
        "lpar_uuid": lpar_uuid,
        "already_in_state": False,
        "observed_state": state,
        "job_id": None,
    }


def power_body(
    action: PowerAction, mode: PowerMode, system: str, lpar: str
) -> engine.Body:
    """The operation body for one ``hmc_power_lpar`` request."""

    async def body(ctx: OperationContext) -> BodyResult:
        recorded = ctx.recorded(EFFECT_KEY)
        async with open_client(ctx.connection) as hmc:
            replay = recorded is not None and recorded.status == "applied"
            if replay:
                # Replay after the write: re-resolving could turn a transient read
                # error into a terminal "failed" for a partition already powered.
                ctx.phase("transitioning")
                lpar_uuid = _lpar_uuid(recorded)
                system_uuid = (ctx.partition or ("", lpar_uuid))[0]
                identity = recorded.identity or {}
                result = _result(action, mode, system_uuid, lpar_uuid, None)
            else:
                ctx.phase("validating")
                (
                    system_uuid,
                    lpar_uuid,
                    entry,
                ) = await _resolve_and_authorize_power_target(hmc, system, lpar)
                ctx.guard(system_uuid, lpar_uuid)
                state = _norm((entry.get("Resource") or {}).get("PartitionState"))
                result = _result(action, mode, system_uuid, lpar_uuid, state)
                if _precheck(action, mode, lpar_uuid, entry):
                    result["already_in_state"] = True
                    return BodyResult("completed", result)
                ctx.phase("transitioning")
                identity = await ctx.effect(
                    EFFECT_KEY,
                    EFFECT_KINDS[action],
                    f"lpar:{lpar_uuid}",
                    lambda: _submit(hmc, action, mode, lpar_uuid),
                )
            result["job_id"] = identity.get("job_id")
            return await _finish(hmc, action, lpar_uuid, result, replay=replay)

    return body


def _lpar_uuid(effect: EffectRecord) -> str:
    prefix, _, uuid = effect.target.partition(":")
    if prefix != "lpar" or not uuid:
        raise ValueError(f"effect {effect.key} has no partition target")
    return uuid


async def _classify_power_on(
    ctx: OperationContext, effect: EffectRecord
) -> Classification:
    async with open_client(ctx.connection) as hmc:
        state = await _state(hmc, _lpar_uuid(effect))
    if state in ACTIVATED_STATES:
        return Classification("applied", {"job_id": None})
    # 'not activated' cannot tell a queued PowerOn from none, so it is not resubmitted.
    return Classification(
        "needs_attention",
        reason=(
            f"the partition is {state!r}; a power-on job may still be queued, so it is "
            "not resubmitted. Resume once it is activated, or abandon"
        ),
    )


async def _classify_power_off(
    ctx: OperationContext, effect: EffectRecord
) -> Classification:
    async with open_client(ctx.connection) as hmc:
        state = await _state(hmc, _lpar_uuid(effect))
    if state == _NOT_ACTIVATED:
        return Classification("applied", {"job_id": None})
    return Classification(
        "needs_attention",
        reason=(
            f"the partition is {state!r}; a power-off job may still be queued, so it is "
            "not resubmitted. Resume once it reads 'not activated', or abandon"
        ),
    )


# lpar.restart has none: a finished cycle and no cycle both read as running, so
# an open restart always pauses for attention (ADR 0199 Decision 4).
POWER_CLASSIFIERS: Mapping[str, engine.Classifier] = {
    EFFECT_KINDS["start"]: _classify_power_on,
    EFFECT_KINDS["stop"]: _classify_power_off,
}
