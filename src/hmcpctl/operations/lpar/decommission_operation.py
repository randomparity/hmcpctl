"""``hmc_decommission_lpar``'s operation body, effect classifiers and result (ADR 0320).

Each HMC write of a decommission is a journaled effect keyed by the resource it removes,
so a repeat never writes twice and an interrupted write is classified by a live read
before anything else is written. Messages name partitions by UUID and never echo a
caller's selector (``engine`` module note).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import TypeAdapter

from ...client.core import HMCClient
from ...errors import HMCError
from ..logical import engine
from ..logical.engine import BodyResult, Classification, OperationContext
from ..logical.models import EffectRecord, OperationRecord
from ..logical.store import OperationRefused
from .decommission import (
    DELETE_LPAR_KEY,
    EFFECT_KINDS,
    DecommissionBlastRadius,
    DecommissionResult,
    _inventory_adapters,
    _vscsi_detach_inventory,
    decommission_lpar,
)
from .power import open_client
from .workflow_contract import WorkflowStep

TOOL_NAME = "hmc_decommission_lpar"
ClientFactory = Callable[[str], HMCClient]
# A new operation's recorded arguments start from these (the tool's former defaults).
DEFAULT_ARGUMENTS: Mapping[str, Any] = {
    "ownership_override": False,
    "immediate": False,
    "timeout_seconds": 300,
    "poll_interval": 5,
}
_REQUIRED = ("system_name_or_uuid", "lpar_name_or_uuid")
# The four ADR 0027 steps, in order, and the effects each one reports (ADR 0320 D7).
_STEPS = ("power_off", "detach_storage_mappings", "detach_adapters", "delete_lpar")
_BODY_ERRORS = (HMCError, ValueError, LookupError, PermissionError)
_RESULT = TypeAdapter(DecommissionResult)
_NO_INVENTORY = (
    "the partition's inventory is unavailable; the operation's effects record what was "
    "removed"
)


def given_arguments(**values: Any) -> dict[str, Any]:
    """The arguments the caller supplied; ADR 0094 reads a blank string as absent."""
    return {
        key: value
        for key, value in values.items()
        if value is not None and not (isinstance(value, str) and not value.strip())
    }


def effective_arguments(
    given: Mapping[str, Any], agent_id: str, request_id: str, continuation: str
) -> tuple[dict[str, Any], bool]:
    """The arguments to run, and whether an operation is already recorded for them."""
    recorded = engine.recorded_arguments(agent_id, request_id, TOOL_NAME)
    if continuation != "none":
        if recorded is None:
            raise OperationRefused(
                "not_found",
                f"no operation has request_id {request_id}; omit continuation to start one",
            )
        return {**recorded, **given}, True
    missing = [name for name in _REQUIRED if name not in given]
    if missing:
        raise ValueError(f"a new operation needs {', '.join(missing)}")
    return {**DEFAULT_ARGUMENTS, **given}, recorded is not None


async def preview(hmc: HMCClient, arguments: Mapping[str, Any]) -> DecommissionResult:
    """The dry-run inventory of *arguments*; it raises what a new request must refuse."""
    return await decommission_lpar(
        hmc,
        arguments["lpar_name_or_uuid"],
        system_name_or_uuid=arguments["system_name_or_uuid"],
        dry_run=True,
        ownership_override=arguments["ownership_override"],
        immediate=arguments["immediate"],
        timeout_seconds=arguments["timeout_seconds"],
        poll_interval=arguments["poll_interval"],
    )


def decommission_body(
    arguments: Mapping[str, Any], client_factory: ClientFactory = open_client
) -> engine.Body:
    """The operation body for one ``hmc_decommission_lpar`` request."""

    async def body(ctx: OperationContext) -> BodyResult:
        deleted = ctx.recorded(DELETE_LPAR_KEY)
        if deleted is not None and deleted.status == "applied":
            # The partition is gone; result_from_record rebuilds from the journal.
            return BodyResult("completed")
        async with client_factory(ctx.connection) as hmc:
            try:
                result = await decommission_lpar(
                    hmc,
                    arguments["lpar_name_or_uuid"],
                    system_name_or_uuid=arguments["system_name_or_uuid"],
                    ownership_override=arguments["ownership_override"],
                    immediate=arguments["immediate"],
                    timeout_seconds=arguments["timeout_seconds"],
                    poll_interval=arguments["poll_interval"],
                    journal=ctx,
                )
            except _BODY_ERRORS as exc:
                lpar = ctx.partition[1] if ctx.partition else "unresolved"
                raise RuntimeError(
                    f"decommission of LPAR {lpar} stopped ({type(exc).__name__}); "
                    "inspect it, then resume or abandon"
                ) from exc
        outcome = "completed" if result.workflow_completed else "needs_attention"
        return BodyResult(outcome, _RESULT.dump_python(result, mode="json"))

    return body


def _target(effect: EffectRecord) -> tuple[str, str]:
    """The ``<kind>:<id>`` head of *effect*'s target and the rest after the first ``/``."""
    head, _, rest = effect.target.partition("/")
    _, _, uuid = head.partition(":")
    if not uuid:
        raise ValueError(f"effect {effect.key} has no target UUID")
    return uuid, rest


def decommission_classifiers(
    client_factory: ClientFactory = open_client,
) -> Mapping[str, engine.Classifier]:
    """The live checks for decommission's effects (ADR 0320 Decision 4)."""

    async def power_off(ctx: OperationContext, effect: EffectRecord) -> Classification:
        lpar_uuid, _ = _target(effect)
        async with client_factory(ctx.connection) as hmc:
            state = await hmc.get_quick_property(
                "LogicalPartition", lpar_uuid, "PartitionState"
            )
        state = state.strip().lower() if isinstance(state, str) else ""
        if state == "not activated":
            return Classification("applied", {"job_id": None})
        return Classification(
            "needs_attention",
            reason=(
                f"the partition is {state!r}; a power-off job may still be queued, so it "
                "is not resubmitted. Resume once it reads 'not activated', or abandon"
            ),
        )

    async def unmap(ctx: OperationContext, effect: EffectRecord) -> Classification:
        vios_uuid, rest = _target(effect)
        mapping_id = rest.removeprefix("mapping:")
        if ctx.partition is None or not mapping_id:
            return Classification("needs_attention", reason="no guarded partition")
        async with client_factory(ctx.connection) as hmc:
            detail = await hmc.get_vios_storage_detail(vios_uuid)
        resource = (detail or {}).get("Resource")
        if not isinstance(resource, dict):
            raise HMCError(f"VIOS {vios_uuid} returned no readable storage detail")
        found, errors = _vscsi_detach_inventory(resource, vios_uuid, ctx.partition[1])
        if errors:
            raise HMCError(f"VIOS {vios_uuid} vSCSI mappings are incomplete")
        if any(item["mapping_id"] == mapping_id for item in found):
            return Classification("not_applied")
        return Classification(
            "applied", {"vios_uuid": vios_uuid, "mapping_id": mapping_id}
        )

    async def adapter(ctx: OperationContext, effect: EffectRecord) -> Classification:
        lpar_uuid, rest = _target(effect)
        adapter_type, _, adapter_uuid = rest.partition(":")
        async with client_factory(ctx.connection) as hmc:
            listed = await _inventory_adapters(hmc, lpar_uuid)
        if any(item["uuid"].lower() == adapter_uuid for item in listed):
            return Classification("not_applied")
        return Classification("applied", {"type": adapter_type, "uuid": adapter_uuid})

    async def delete(ctx: OperationContext, effect: EffectRecord) -> Classification:
        lpar_uuid, _ = _target(effect)
        if ctx.partition is None:
            return Classification("needs_attention", reason="no guarded partition")
        async with client_factory(ctx.connection) as hmc:
            entries = await hmc.list_logical_partitions(ctx.partition[0])
        if any(str(e.get("UUID") or "").lower() == lpar_uuid for e in entries):
            return Classification("not_applied")
        return Classification("applied", {"lpar_uuid": lpar_uuid})

    return {
        EFFECT_KINDS["power_off"]: power_off,
        EFFECT_KINDS["unmap"]: unmap,
        EFFECT_KINDS["adapter"]: adapter,
        EFFECT_KINDS["delete_lpar"]: delete,
    }


def _step_of(key: str) -> str:
    if key.startswith("unmap."):
        return "detach_storage_mappings"
    if key.startswith("adapter."):
        return "detach_adapters"
    return key


def _settled(record: OperationRecord) -> dict[str, list[EffectRecord]]:
    """Each step's effects; a write still in flight has no outcome to report yet."""
    grouped: dict[str, list[EffectRecord]] = {}
    for effect in record.effects:
        if record.state != "running" or effect.status in ("applied", "not_applied"):
            grouped.setdefault(_step_of(effect.key), []).append(effect)
    return grouped


def _all_applied(effects: list[EffectRecord]) -> bool:
    return all(effect.status == "applied" for effect in effects)


def _agrees(result: DecommissionResult, record: OperationRecord) -> bool:
    """Whether *result*'s steps match the journal: a pause the body did not return
    (a reconcile pause after progress) keeps an older result."""
    steps = {step.step: step for step in result.steps}
    for name, effects in _settled(record).items():
        step = steps.get(name)
        if step is None or (step.status == "ok") != _all_applied(effects):
            return False
    return True


def _stored(record: OperationRecord) -> DecommissionResult | None:
    if not record.result or "lpar_uuid" not in record.result:
        return None
    return _RESULT.validate_python(record.result)


def _minimal_blast_radius(lpar_uuid: str) -> DecommissionBlastRadius:
    return {
        "lpar_uuid": lpar_uuid,
        "lpar_name": "",
        "partition_id": None,
        "state": None,
        "owner": None,
        "adapters": (),
        "storage_mappings": (),
        "unresolved_storage_mapping_count": 0,
        "unavailable_storage_source_count": 0,
    }


def result_from_record(record: OperationRecord) -> DecommissionResult:
    """The tool result: the body's own result where it agrees with the journal, else
    one rebuilt from the effects (ADR 0320 Decision 7)."""
    stored = _stored(record)
    warnings = record.warnings
    if stored is not None and (
        record.outcome == "completed"
        or (record.outcome == "needs_attention" and _agrees(stored, record))
    ):
        merged = tuple(dict.fromkeys((*stored.warnings, *warnings)))
        return dataclasses.replace(stored, warnings=merged, operation=record)
    lpar_uuid = record.partition_uuid or ""
    if stored is not None:
        blast_radius = stored.blast_radius
    else:
        blast_radius = _minimal_blast_radius(lpar_uuid)
        warnings = (*warnings, _NO_INVENTORY)
    grouped = _settled(record)
    delete = grouped.get(DELETE_LPAR_KEY)
    return DecommissionResult(
        resource_deleted=bool(delete) and _all_applied(delete or []),
        workflow_completed=record.outcome == "completed",
        lpar_uuid=lpar_uuid,
        dry_run=False,
        steps=tuple(
            WorkflowStep(
                name,
                "ok" if _all_applied(grouped[name]) else "error",
                tuple(e.identity for e in grouped[name] if e.status == "applied"),
            )
            for name in _STEPS
            if name in grouped
        ),
        warnings=tuple(warnings),
        blast_radius=blast_radius,
        operation=record,
    )
