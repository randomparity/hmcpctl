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
    DecommissionStorage,
    _Direct,
    _inventory_adapters,
    _vscsi_detach_inventory,
    decommission_lpar,
)
from .decommission_storage import (
    INCOMPLETE,
    clean_storage,
    deleted_from_effects,
    owned_items,
    plan_storage,
    same_item,
    storage_classifiers,
    storage_from_effects,
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
    "storage_cleanup": "retain",
}
STORAGE_CLEANUP = ("retain", "delete_owned")
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


def new_arguments(given: Mapping[str, Any]) -> dict[str, Any]:
    """A new request's arguments: the caller's, over the tool's defaults."""
    missing = [name for name in _REQUIRED if name not in given]
    if missing:
        raise ValueError(f"a new operation needs {', '.join(missing)}")
    return {**DEFAULT_ARGUMENTS, **given}


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
    return new_arguments(given), recorded is not None


def deletes_owned(arguments: Mapping[str, Any]) -> bool:
    """Whether *arguments* ask for ADR 0323's cleanup; a record predating the field
    retains."""
    return arguments.get("storage_cleanup", "retain") == "delete_owned"


class _Target(_Direct):
    """The direct journal, keeping the system and partition the inventory resolved."""

    target: tuple[str, str] | None = None

    def guard(self, system_uuid: str, partition_uuid: str) -> None:
        self.target = (system_uuid, partition_uuid)


async def preview_target(
    hmc: HMCClient, arguments: Mapping[str, Any]
) -> tuple[DecommissionResult, tuple[str, str] | None]:
    """The dry-run inventory of *arguments*; it raises what a new request must refuse.

    With ``delete_owned`` the result carries the storage plan (ADR 0323 Decision 6) and
    the (system, partition) UUIDs the inventory resolved come back too; else None.
    """
    journal = _Target() if deletes_owned(arguments) else None
    result = await decommission_lpar(
        hmc,
        arguments["lpar_name_or_uuid"],
        system_name_or_uuid=arguments["system_name_or_uuid"],
        dry_run=True,
        ownership_override=arguments["ownership_override"],
        immediate=arguments["immediate"],
        timeout_seconds=arguments["timeout_seconds"],
        poll_interval=arguments["poll_interval"],
        journal=journal,
    )
    if journal is None or journal.target is None:
        return result, None
    storage = await plan_storage(hmc, *journal.target)
    return dataclasses.replace(result, storage=storage), journal.target


def owned_vios(target: tuple[str, str] | None) -> frozenset[str]:
    """The VIOSes holding the partition's ledger-owned storage (ADR 0323 Decision 5)."""
    if target is None:
        return frozenset()
    items, _ = owned_items(*target)
    return frozenset(item.vios_uuid for item in items)


async def preview(hmc: HMCClient, arguments: Mapping[str, Any]) -> DecommissionResult:
    """The dry-run inventory of *arguments*; it raises what a new request must refuse."""
    return (await preview_target(hmc, arguments))[0]


def decommission_body(
    arguments: Mapping[str, Any],
    client_factory: ClientFactory = open_client,
    *,
    authorized_vios: frozenset[str] = frozenset(),
) -> engine.Body:
    """The operation body for one ``hmc_decommission_lpar`` request.

    With ``delete_owned`` it cleans owned storage once the partition is deleted, on
    VIOSes in *authorized_vios* only (ADR 0323 Decision 5).
    """
    cleanup = deletes_owned(arguments)

    async def run(ctx: OperationContext, hmc: HMCClient, gone: bool) -> BodyResult:
        result = None
        if not gone:
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
            if not (cleanup and result.resource_deleted):
                outcome = (
                    "completed" if result.workflow_completed else "needs_attention"
                )
                return BodyResult(outcome, _RESULT.dump_python(result, mode="json"))
        assert ctx.partition is not None
        ctx.set_phase("cleaning_storage")
        retained, failure = await clean_storage(
            hmc, ctx, *ctx.partition, authorized_vios=authorized_vios
        )
        storage: DecommissionStorage = {
            "deleted": (),
            "retained": retained,
            "pending": (),
        }
        outcome = (
            "needs_attention"
            if any(item.get("reason") == INCOMPLETE for item in retained)
            else "completed"
        )
        if result is None and "lpar_uuid" in ctx.earlier_result:
            # The partition went in an earlier run: keep that run's inventory.
            result = _RESULT.validate_python(ctx.earlier_result)
        # With no earlier inventory either, result_from_record rebuilds the rest.
        stored = (
            {"storage": storage}
            if result is None
            else _RESULT.dump_python(
                dataclasses.replace(result, storage=storage), mode="json"
            )
        )
        if failure is not None:
            raise engine.OperationFailed(failure, result=stored)
        return BodyResult(outcome, stored)

    async def body(ctx: OperationContext) -> BodyResult:
        deleted = ctx.recorded(DELETE_LPAR_KEY)
        gone = deleted is not None and deleted.status == "applied"
        if gone and not cleanup:
            # The partition is gone; result_from_record rebuilds from the journal.
            return BodyResult("completed")
        async with client_factory(ctx.connection) as hmc:
            try:
                return await run(ctx, hmc, gone)
            except _BODY_ERRORS as exc:
                lpar = ctx.partition[1] if ctx.partition else "unresolved"
                raise RuntimeError(
                    f"decommission of LPAR {lpar} stopped ({type(exc).__name__}); "
                    "inspect it, then resume or abandon"
                ) from exc

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
        **storage_classifiers(client_factory),
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


def _storage(
    record: OperationRecord, stored: DecommissionResult | None
) -> DecommissionStorage:
    """``deleted`` and ``pending`` from the journal, so they span every run; ``retained``
    from the latest pass the record holds, less what a later run deleted (ADR 0323
    Decision 6)."""
    latest = (
        stored.storage if stored is not None else (record.result or {}).get("storage")
    )
    journal = storage_from_effects(record.effects)
    deleted = deleted_from_effects(record.effects)
    gone = {same_item(item) for item in deleted}
    earlier = (latest or {}).get("retained", ())
    retained = [i for i in earlier if same_item(i) not in gone]
    return {
        "deleted": deleted,
        "retained": tuple(retained),
        "pending": journal["pending"],
    }


def result_from_record(
    record: OperationRecord, *, delete_owned: bool = False
) -> DecommissionResult:
    """The tool result: the body's own result where it agrees with the journal, else
    one rebuilt from the effects (ADR 0320 Decision 7). *delete_owned* is the recorded
    mode, which the record does not carry."""
    stored = _stored(record)
    storage = _storage(record, stored) if delete_owned else None
    warnings = record.warnings
    if stored is not None and (
        record.outcome == "completed"
        or (record.outcome == "needs_attention" and _agrees(stored, record))
    ):
        merged = tuple(dict.fromkeys((*stored.warnings, *warnings)))
        return dataclasses.replace(
            stored, warnings=merged, operation=record, storage=storage
        )
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
        storage=storage,
    )
