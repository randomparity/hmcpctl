"""``hmc_provision_lpar``'s operation body and effect classifiers (ADR 0313).

Each HMC write is a journaled effect, so a repeat never writes twice and an
interrupted write is classified by a live read before anything else is written.
Messages name UUIDs and exception types, never a caller's arguments (``engine`` note).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import TypeAdapter

from ...client.core import HMCClient
from ...documents.lpar import LparResources, validate_partition_type
from ...errors import HMCError
from ...jobs import JobOutcome, job_identifier
from ...resource_identity import resolve_system_uuid
from ...ssh.transport import HMCCLIError
from ...xmlutil import leaf_text
from ..affinity.ssh import set_minimum_affinity_policy
from ..logical import engine, store
from ..logical.engine import (
    BodyResult,
    Classification,
    EffectNotApplied,
    OperationContext,
    OperationFailed,
)
from ..logical.models import EffectRecord, OperationRecord
from ..storage.resources import storage_mapping
from .activation import ACTIVATED_STATES
from .assignments import apply_validated_lpar_pcie_assignments, assignment_step_names
from .core import LparCreation, create_and_stamp_lpar
from .ownership import (
    lpar_ownership_entry,
    parse_lpar_ownership_caller_token,
    stamp_created_lpar_ownership,
)
from .power import open_client
from .provision import (
    ProvisionRequest,
    ProvisionResult,
    _add_network,
    _create_disk,
    _map_storage,
    _power_on,
    _preflight_provision_request,
    _read_change_location,
    _run_affinity_stage,
    _skip_steps,
    validate_provision_assignments,
    validate_provision_storage,
)
from .workflow_contract import WorkflowStep

ClientFactory = Callable[[str], HMCClient]
# Persisted contract (ADR 0313 Decision 3): never change while an operation can be open.
EFFECT_KINDS: Mapping[str, str] = {
    "create": "lpar.create",
    "stamp": "lpar.ownership_stamp",
    "affinity_policy": "lpar.minimum_affinity_policy",
    "network": "lpar.client_network_adapter",
    "disk": "vios.virtual_disk",
    "storage": "vios.storage_map",
    "assignments": "lpar.pcie_assignments",
    "power_on": "lpar.power_on",
}
# The step each effect reports under in ProvisionResult.steps.
STEP_NAMES: Mapping[str, str] = {
    "create": "create",
    "stamp": "stamp",
    "affinity_policy": "minimum_affinity_policy",
    "network": "network",
    "disk": "create_disk",
    "storage": "storage",
    "assignments": "assignments",
    "power_on": "power_on",
}
# Ledger kinds (ADR 0313 Decision 6). INSTALLER_MEDIA is reserved for #1842.
PARTITION_LEDGER = "partition"
DISK_LEDGER = "virtual_disk"
INSTALLER_MEDIA_LEDGER = "installer_media"
_RESULT = TypeAdapter(ProvisionResult)
_REQUEST = TypeAdapter(ProvisionRequest)


def json_value(value: Any) -> Any:
    """*value* as the JSON an operation's recorded arguments hold."""
    return TypeAdapter(type(value)).dump_python(value, mode="json")


# A new operation's recorded arguments start from these (the tool's defaults).
DEFAULT_ARGUMENTS: Mapping[str, Any] = {
    "resources": json_value(
        LparResources(
            min_memory=256,
            desired_memory=4096,
            max_memory=8192,
            desired_vcpus=1,
            max_vcpus=2,
        )
    ),
    "partition_type": "AIX/Linux",
    "power_on": True,
}
_HEX12 = re.compile(r"[0-9a-f]{12}")
_LEG_ERRORS = (HMCError, HMCCLIError, ValueError, LookupError, EffectNotApplied)


def request_from_arguments(
    arguments: Mapping[str, Any],
) -> tuple[str, ProvisionRequest]:
    """The system selector and validated request in an operation's JSON arguments."""
    fields = {
        key: value
        for key, value in arguments.items()
        if key not in ("system_name_or_uuid", "expected_plan_digest")
    }
    # Before the schema, so a VIOS type gets its pointer to hmc_create_vios.
    validate_partition_type(fields.get("partition_type", "AIX/Linux"))
    request = _REQUEST.validate_python(fields)
    validate_provision_assignments(request.assignments)
    validate_provision_storage(request.storage)
    return arguments["system_name_or_uuid"], request


def mac_colon_form(value: object) -> str | None:
    """Return *value* as a lower-case colon MAC, or None when it is not one."""
    text = leaf_text(value)
    if not isinstance(text, str):
        return None
    digits = text.strip().lower().replace(":", "").replace("-", "")
    if not _HEX12.fullmatch(digits):
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def _agent(hmc: HMCClient) -> str:
    # stamp_lpar_ownership's own default when no agent id is configured.
    return hmc.config.agent_id or "hmcpctl"


def _target_uuid(effect: EffectRecord) -> str:
    _, _, uuid = effect.target.partition(":")
    if not uuid:
        raise ValueError(f"effect {effect.key} has no target UUID")
    return uuid


def _named(entries: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [
        e for e in entries if (e.get("Resource") or {}).get("PartitionName") == name
    ]


def _ledgered_elsewhere(
    ctx: OperationContext, kind: str, system_uuid: str, match: Mapping[str, Any]
) -> bool:
    """Whether another operation's ledger already owns the resource *match* names."""
    with store.read_session() as conn:
        entries = [] if conn is None else store.ledger_entries(conn, kind, system_uuid)
    return any(
        operation_id != ctx.operation_id
        and all(identity.get(key) == value for key, value in match.items())
        for operation_id, _partition, identity in entries
    )


async def _stamp_state(
    hmc: HMCClient, system_uuid: str, lpar_uuid: str, caller_token: str | None
) -> str:
    """``ours``, ``absent`` or ``foreign``, from the partition's description."""
    entries = await hmc.list_logical_partitions(system_uuid)
    found = [e for e in entries if str(e.get("UUID") or "").lower() == lpar_uuid]
    if len(found) != 1:
        raise HMCError(f"LPAR {lpar_uuid} is not in its system's partition list")
    facts = lpar_ownership_entry(found[0])
    if facts["owner"] is None:
        return "absent"
    if facts["owner"] != _agent(hmc):
        return "foreign"
    description = facts["description"] or ""
    if caller_token is not None and (
        parse_lpar_ownership_caller_token(description) != caller_token
    ):
        return "absent"
    return "ours"


def _adapter_identity(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    uuid = entry.get("UUID")
    mac = mac_colon_form((entry.get("Resource") or {}).get("MACAddress"))
    if not isinstance(uuid, str) or not uuid or mac is None:
        return None
    return {"adapter_uuid": uuid.lower(), "mac": mac}


async def _adapters_on_vlan(
    hmc: HMCClient, lpar_uuid: str, vlan: int
) -> list[dict[str, Any]]:
    adapters = await hmc.list_adapters(lpar_uuid, "ClientNetworkAdapter")
    return [
        a
        for a in adapters
        if str(leaf_text((a.get("Resource") or {}).get("PortVLANID"))) == str(vlan)
    ]


def _disk_names(entry: Mapping[str, Any]) -> set[str]:
    disks = ((entry.get("Resource") or {}).get("VirtualDisks") or {}).get("VirtualDisk")
    items = disks if isinstance(disks, list) else [disks]
    names = (leaf_text(d.get("DiskName")) for d in items if isinstance(d, Mapping))
    return {name for name in names if isinstance(name, str)}


class _Run:
    """One execution of the body: the steps so far and the writes still to make."""

    def __init__(
        self,
        ctx: OperationContext,
        hmc: HMCClient,
        system: str,
        request: ProvisionRequest,
    ) -> None:
        self.ctx, self.hmc, self.system, self.request = ctx, hmc, system, request
        self.steps: list[WorkflowStep] = []
        self.warnings: list[str] = []
        self.change_location = None
        self.assignment_steps: tuple[WorkflowStep, ...] = ()

    def _result(self, lpar_uuid: str, completed: bool) -> dict[str, Any]:
        result = ProvisionResult(
            resource_created=True,
            workflow_completed=completed,
            lpar_uuid=lpar_uuid,
            dry_run=False,
            ownership_stamped=True,
            steps=tuple(self.steps),
            warnings=tuple(self.warnings),
            change_location=self.change_location,
        )
        return _RESULT.dump_python(result, mode="json")

    async def execute(self) -> BodyResult:
        self.ctx.set_phase("validating")
        created = self.ctx.recorded("create")
        # The submitter validated a first run; a continuation before the partition
        # exists re-validates, since the name check refuses its own partition after.
        if self.ctx.continuation != "none" and (
            created is None or created.status != "applied"
        ):
            try:
                await _preflight_provision_request(self.hmc, self.system, self.request)
            except (HMCError, HMCCLIError, ValueError, LookupError) as exc:
                raise OperationFailed(
                    f"a precondition no longer holds ({type(exc).__name__}); run "
                    "dry_run with the same arguments to see which"
                ) from exc
        system_uuid = (await resolve_system_uuid(self.hmc, self.system)).lower()
        self.ctx.set_phase("creating")
        lpar_uuid = await self._create(system_uuid)
        identity = await self.ctx.effect(
            "stamp",
            EFFECT_KINDS["stamp"],
            f"lpar:{lpar_uuid}",
            lambda: self._write_stamp(system_uuid, lpar_uuid),
        )
        self.steps.append(WorkflowStep("stamp", "ok", dict(identity)))
        completed = await self._legs(system_uuid, lpar_uuid)
        if completed:
            completed, warnings = await _run_affinity_stage(
                self.steps, self.hmc, self.request
            )
            self.warnings.extend(warnings)
        outcome = "completed" if completed else "needs_attention"
        return BodyResult(outcome, self._result(lpar_uuid, completed))

    async def _create(self, system_uuid: str) -> str:
        identity = await self.ctx.effect(
            "create",
            EFFECT_KINDS["create"],
            f"system:{system_uuid}",
            lambda: self._write_create(system_uuid),
        )
        lpar_uuid = identity["lpar_uuid"]
        self.ctx.guard(system_uuid, lpar_uuid)
        self.ctx.ledger_add(
            PARTITION_LEDGER,
            PARTITION_LEDGER,
            system_uuid,
            lpar_uuid,
            {"lpar_uuid": lpar_uuid, "name": identity["name"]},
        )
        self.steps.append(
            WorkflowStep(
                "create", "ok", {"UUID": lpar_uuid, "PartitionName": identity["name"]}
            )
        )
        follow_ups = identity.get("follow_ups") or ()
        for name, status, text in follow_ups:
            self.steps.append(WorkflowStep(name, status, text))
        failed = [name for name, status, _ in follow_ups if status == "error"]
        if failed:
            raise OperationFailed(
                f"LPAR {lpar_uuid} was created, but its {', '.join(failed)} step "
                "reported an error; inspect it, then decommission or repair it"
            )
        if identity.get("resumed"):
            self.warnings.append(
                f"LPAR {lpar_uuid} was found by name after an interrupted create; a "
                "mksyscfg profile apply is unconfirmed"
            )
        return lpar_uuid

    async def _write_create(self, system_uuid: str) -> dict[str, Any]:
        request = self.request
        try:
            creation = await create_and_stamp_lpar(
                self.hmc,
                self.system,
                LparCreation(
                    request.name,
                    request.partition_type,
                    request.resources,
                    caller_token=request.caller_token,
                    apply_profile=True,
                    physical_io=request.physical_io,
                    creation_sriov=request.assignments.sriov
                    if request.physical_io.requested
                    else (),
                ),
            )
        except Exception as exc:
            entries = await self.hmc.list_logical_partitions(system_uuid)
            if not _named(entries, request.name):
                raise EffectNotApplied(
                    f"the HMC did not create the partition ({type(exc).__name__}); "
                    "no partition of the requested name exists"
                ) from exc
            raise
        uuid = (creation.lpar or {}).get("UUID")
        if not isinstance(uuid, str) or not uuid:
            raise HMCError("the create returned no partition UUID")
        follow_ups = [
            # Text only, bounded: an identity is at most 4 KiB (store.MAX_IDENTITY_BYTES).
            [step.step, step.status, str(step.result)[:256] if step.result else None]
            for step in (creation.physical_io_step, creation.apply_step)
            if step is not None
        ]
        return {
            "lpar_uuid": uuid.lower(),
            "name": request.name,
            "follow_ups": follow_ups,
        }

    async def _write_stamp(self, system_uuid: str, lpar_uuid: str) -> dict[str, Any]:
        token = self.request.caller_token
        state = await _stamp_state(self.hmc, system_uuid, lpar_uuid, token)
        if state == "foreign":
            raise HMCError(f"LPAR {lpar_uuid} carries another agent's ownership stamp")
        if state == "absent":
            await stamp_created_lpar_ownership(
                self.hmc,
                system_uuid,
                self.system,
                {"Resource": {"PartitionName": self.request.name}},
                caller_token=token,
            )
            state = await _stamp_state(self.hmc, system_uuid, lpar_uuid, token)
        if state != "ours":
            raise EffectNotApplied(
                f"the ownership stamp did not land on LPAR {lpar_uuid}; a stamp is required"
            )
        return {"owner": _agent(self.hmc)}

    async def _leg(
        self,
        key: str,
        target: str,
        write: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any] | None:
        try:
            identity = await self.ctx.effect(key, EFFECT_KINDS[key], target, write)
        except _LEG_ERRORS as exc:
            self.steps.append(
                WorkflowStep(STEP_NAMES[key], "error", f"{type(exc).__name__}: {exc}")
            )
            return None
        self.steps.append(WorkflowStep(STEP_NAMES[key], "ok", dict(identity)))
        return identity

    async def _legs(self, system_uuid: str, lpar_uuid: str) -> bool:
        request, hmc, lpar = self.request, self.hmc, f"lpar:{lpar_uuid}"
        storage = request.storage
        legs: list[tuple[str, str, Callable[[], Awaitable[Mapping[str, Any]]]]] = []
        if request.minimum_affinity_policy is not None:
            policy = request.minimum_affinity_policy
            legs.append(("affinity_policy", lpar, lambda: self._write_policy(policy)))
        legs.append(("network", lpar, lambda: self._write_network(lpar_uuid)))
        if storage.capacity_mib is not None:
            legs.append(
                (
                    "disk",
                    f"vios:{storage.vios_uuid.lower()}",
                    lambda: self._write_disk(storage.capacity_mib or 0),
                )
            )
        legs.append(("storage", lpar, lambda: self._write_storage(lpar_uuid)))
        if assignment_step_names(self._assignments()):
            legs.append(("assignments", lpar, self._write_assignments))
        if request.power_on:
            legs.append(("power_on", lpar, lambda: self._write_power(lpar_uuid)))
        for index, (key, target, write) in enumerate(legs):
            identity = await self._leg(key, target, write)
            if key == "assignments":
                self._replace_assignment_step()
            if identity is None:
                _skip_steps(
                    self.steps, [STEP_NAMES[k] for k, _, _ in legs[index + 1 :]]
                )
                if request.affinity_assessment is not None:
                    _skip_steps(self.steps, ["affinity_assessment"])
                return False
            if key == "network":
                self.change_location, warnings = await _read_change_location(
                    hmc, lpar_uuid
                )
                self.warnings.extend(warnings)
            if key == "disk":
                self.ctx.ledger_add(
                    "root_disk",
                    DISK_LEDGER,
                    system_uuid,
                    lpar_uuid,
                    {k: identity[k] for k in ("vios_uuid", "vg_uuid", "disk_name")},
                )
        return True

    def _assignments(self):
        assignments = self.request.assignments
        if self.request.physical_io.requested:
            # The create applied these with its physical I/O.
            return dataclasses.replace(assignments, sriov=())
        return assignments

    def _replace_assignment_step(self) -> None:
        if self.assignment_steps and self.steps[-1].step == "assignments":
            self.steps[-1:] = list(self.assignment_steps)

    async def _write_policy(self, policy) -> dict[str, Any]:
        await set_minimum_affinity_policy(
            self.hmc, self.request.name, policy, system_name_or_uuid=self.system
        )
        return {}

    async def _write_network(self, lpar_uuid: str) -> dict[str, Any]:
        vlan = self.request.adapters.port_vlan_id
        created = await _add_network(self.hmc, lpar_uuid, vlan)
        identity = _adapter_identity(created or {})
        if identity is None:
            found = await _adapters_on_vlan(self.hmc, lpar_uuid, vlan)
            identity = _adapter_identity(found[0]) if len(found) == 1 else None
        if identity is None:
            raise HMCError(f"the adapter added to LPAR {lpar_uuid} reported no MAC")
        return identity

    async def _write_disk(self, capacity_mib: int) -> dict[str, Any]:
        storage = self.request.storage
        await _create_disk(self.hmc, storage, capacity_mib)
        return {
            "vios_uuid": storage.vios_uuid.lower(),
            "vg_uuid": (storage.vg_uuid or "").lower(),
            "disk_name": storage.storage_name,
            "capacity_mib": capacity_mib,
        }

    async def _write_storage(self, lpar_uuid: str) -> dict[str, Any]:
        storage = self.request.storage
        await _map_storage(self.hmc, storage, lpar_uuid)
        return {
            "vios_uuid": storage.vios_uuid.lower(),
            "storage_name": storage.storage_name,
        }

    async def _write_assignments(self) -> dict[str, Any]:
        result = await apply_validated_lpar_pcie_assignments(
            self.hmc, self.system, self.request.name, self._assignments()
        )
        self.assignment_steps = result.steps
        if not result.workflow_completed:
            failed = [s.step for s in result.steps if s.status == "error"]
            raise HMCError(f"assignment steps reported errors: {', '.join(failed)}")
        return {"steps": [[s.step, s.status] for s in result.steps]}

    async def _write_power(self, lpar_uuid: str) -> dict[str, Any]:
        result = await _power_on(
            self.hmc, self.system, lpar_uuid, self.request.affinity_assessment
        )
        if isinstance(result, JobOutcome):
            if result.timed_out or result.error is not None:
                raise HMCError(
                    f"PowerOn job {result.job_id} did not succeed"
                    f"{' before its timeout' if result.timed_out else ''}"
                )
            return {"job_id": result.job_id}
        return {"job_id": job_identifier(result) if result else None}


def provision_body(
    system: str, request: ProvisionRequest, client_factory: ClientFactory = open_client
) -> engine.Body:
    """The operation body for one ``hmc_provision_lpar`` request."""

    async def body(ctx: OperationContext) -> BodyResult:
        async with client_factory(ctx.connection) as hmc:
            return await _Run(ctx, hmc, system, request).execute()

    return body


def provision_classifiers(
    request: ProvisionRequest, client_factory: ClientFactory = open_client
) -> Mapping[str, engine.Classifier]:
    """The live checks for provision's effects (ADR 0313 Decision 3)."""

    async def create(ctx: OperationContext, effect: EffectRecord) -> Classification:
        system_uuid = _target_uuid(effect)
        async with client_factory(ctx.connection) as hmc:
            found = _named(await hmc.list_logical_partitions(system_uuid), request.name)
            if not found:
                return Classification("not_applied")
            if len(found) > 1:
                return Classification(
                    "needs_attention", reason="several partitions match"
                )
            lpar_uuid = str(found[0].get("UUID") or "").lower()
            owner = lpar_ownership_entry(found[0])["owner"]
            agent = _agent(hmc)
        if request.physical_io.requested:
            reason = f"LPAR {lpar_uuid} exists; its physical I/O follow-up cannot be verified"
        elif owner not in (None, agent):
            reason = f"LPAR {lpar_uuid} carries another agent's ownership stamp"
        elif not lpar_uuid or _ledgered_elsewhere(
            ctx, PARTITION_LEDGER, system_uuid, {"lpar_uuid": lpar_uuid}
        ):
            reason = f"LPAR {lpar_uuid or '?'} belongs to another operation"
        else:
            identity = {"lpar_uuid": lpar_uuid, "name": request.name, "resumed": True}
            return Classification("applied", identity)
        return Classification(
            "needs_attention", reason=f"{reason}; inspect, then abandon"
        )

    async def stamp(ctx: OperationContext, effect: EffectRecord) -> Classification:
        if ctx.partition is None:
            return Classification("needs_attention", reason="no guarded partition")
        async with client_factory(ctx.connection) as hmc:
            state = await _stamp_state(
                hmc, ctx.partition[0], _target_uuid(effect), request.caller_token
            )
            agent = _agent(hmc)
        if state == "ours":
            return Classification("applied", {"owner": agent})
        if state == "absent":
            return Classification("not_applied")
        return Classification(
            "needs_attention", reason="another agent's stamp is present"
        )

    async def network(ctx: OperationContext, effect: EffectRecord) -> Classification:
        async with client_factory(ctx.connection) as hmc:
            found = await _adapters_on_vlan(
                hmc, _target_uuid(effect), request.adapters.port_vlan_id
            )
        if not found:
            return Classification("not_applied")
        identity = _adapter_identity(found[0]) if len(found) == 1 else None
        if identity is None:
            return Classification(
                "needs_attention",
                reason="the VLAN's adapters are ambiguous or lack a MAC",
            )
        return Classification("applied", identity)

    async def disk(ctx: OperationContext, effect: EffectRecord) -> Classification:
        storage = request.storage
        vg_uuid = (storage.vg_uuid or "").lower()
        async with client_factory(ctx.connection) as hmc:
            groups = await hmc.list_volume_groups(_target_uuid(effect))
        group = [g for g in groups if str(g.get("UUID") or "").lower() == vg_uuid]
        if len(group) != 1:
            return Classification(
                "needs_attention", reason="the volume group is unreadable"
            )
        if storage.storage_name not in _disk_names(group[0]):
            return Classification("not_applied")
        identity = {
            "vios_uuid": storage.vios_uuid.lower(),
            "vg_uuid": vg_uuid,
            "disk_name": storage.storage_name,
        }
        system_uuid = ctx.partition[0] if ctx.partition else ""
        if not system_uuid or _ledgered_elsewhere(
            ctx, DISK_LEDGER, system_uuid, identity
        ):
            return Classification(
                "needs_attention", reason="the disk belongs to another operation"
            )
        return Classification(
            "applied", {**identity, "capacity_mib": storage.capacity_mib}
        )

    async def mapping(ctx: OperationContext, effect: EffectRecord) -> Classification:
        storage, lpar_uuid = request.storage, _target_uuid(effect)
        async with client_factory(ctx.connection) as hmc:
            entries = await hmc.list_storage_mappings(storage.vios_uuid)
        for entry in entries:
            found = storage_mapping(entry)
            if found.backing_name == storage.storage_name and (
                (found.lpar_uuid or "").lower() == lpar_uuid
            ):
                identity = {
                    "vios_uuid": storage.vios_uuid.lower(),
                    "storage_name": storage.storage_name,
                }
                return Classification("applied", identity)
        return Classification("not_applied")

    async def power_on(ctx: OperationContext, effect: EffectRecord) -> Classification:
        async with client_factory(ctx.connection) as hmc:
            state = await hmc.get_quick_property(
                "LogicalPartition", _target_uuid(effect), "PartitionState"
            )
        if str(state or "").strip().lower() in ACTIVATED_STATES:
            return Classification("applied", {"job_id": None})
        return Classification(
            "needs_attention",
            reason="a power-on job may still be queued, so it is not resubmitted",
        )

    return {
        EFFECT_KINDS["create"]: create,
        EFFECT_KINDS["stamp"]: stamp,
        EFFECT_KINDS["network"]: network,
        EFFECT_KINDS["disk"]: disk,
        EFFECT_KINDS["storage"]: mapping,
        EFFECT_KINDS["power_on"]: power_on,
    }


def _agrees(result: ProvisionResult, record: OperationRecord) -> bool:
    """Whether *result*'s steps match the journal: a pause the body did not return
    (a failed run, or a reconcile pause after progress) keeps an older result."""
    steps = {step.step: step for step in reversed(result.steps)}
    for effect in record.effects:
        step = steps.get(STEP_NAMES.get(effect.key, effect.key))
        if effect.key == "assignments" and step is None:
            continue  # reported as its per-assignment steps
        if step is None or (step.status == "ok") != (effect.status == "applied"):
            return False
    return True


def result_from_record(record: OperationRecord) -> ProvisionResult:
    """The tool result: the body's own result where it agrees with the journal, else
    one rebuilt from the effects (ADR 0313 Decision 2)."""
    if record.result and record.outcome in ("completed", "needs_attention"):
        result = _RESULT.validate_python(record.result)
        if record.outcome == "completed" or _agrees(result, record):
            return dataclasses.replace(result, operation=record)
    effects = {effect.key: effect for effect in record.effects}

    def applied(key: str) -> bool | None:
        effect = effects.get(key)
        if effect is None or effect.status == "not_applied":
            return False
        return True if effect.status == "applied" else None

    create = effects.get("create")
    lpar_uuid = ((create.identity or {}).get("lpar_uuid") if create else None) or (
        record.partition_uuid
    )
    return ProvisionResult(
        resource_created=applied("create"),
        workflow_completed=False,
        lpar_uuid=lpar_uuid,
        dry_run=False,
        ownership_stamped=applied("stamp") if "stamp" in effects else None,
        steps=tuple(
            WorkflowStep(
                STEP_NAMES.get(effect.key, effect.key),
                "ok" if effect.status == "applied" else "error",
                effect.identity,
            )
            for effect in record.effects
        ),
        warnings=record.warnings,
        operation=record,
    )
