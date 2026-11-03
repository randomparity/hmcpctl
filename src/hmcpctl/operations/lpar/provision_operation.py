"""``hmc_provision_lpar``'s operation body and effect classifiers (ADR 0313).

Each HMC write is a journaled effect, so a repeat never writes twice and an
interrupted write is classified by a live read before anything else is written.
A leg's step error keeps its exception text, bounded to 512 characters.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import TypeAdapter

from ...client.core import HMCClient
from ...documents.lpar import LparResources, validate_partition_type
from ...documents.partition_profile import ProfilePatch
from ...errors import HMCError
from ...jobs import JobOutcome, job_identifier
from ...resource_identity import resolve_system_uuid
from ...ssh.affinity import MinimumAffinityPolicy
from ...ssh.transport import HMCCLIError
from ...xmlutil import leaf_text, localname
from ..affinity.ssh import set_minimum_affinity_policy
from ..logical import engine, profile_write, store
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
from .assignments import (
    LparPcieAssignments,
    apply_validated_lpar_pcie_assignments,
    assignment_step_names,
)
from .core import LparCreation, create_and_stamp_lpar
from .ownership import (
    lpar_ownership_entry,
    parse_lpar_ownership_caller_token,
    stamp_created_lpar_ownership,
)
from .power import open_client
from .profiles import save_current_partition_profile, set_default_partition_profile
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
    virtual_disk_names,
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
    "profile": profile_write.KIND,
    "default_profile": "lpar.default_profile",
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
    "profile": "save_profile",
    "default_profile": "default_profile",
    "power_on": "power_on",
}
# Ledger kinds (ADR 0313 Decision 6); #1842 adds the reserved "installer_media".
PARTITION_LEDGER = "partition"
DISK_LEDGER = "virtual_disk"
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
_PERSIST_STEPS = ("save_profile", "profile_readback", "default_profile")
_LEG_ERRORS = (HMCError, HMCCLIError, ValueError, LookupError, EffectNotApplied)


def request_from_arguments(
    arguments: Mapping[str, Any],
) -> tuple[str, ProvisionRequest]:
    """The system selector and validated request in an operation's JSON arguments."""
    fields = {
        key: value
        for key, value in arguments.items()
        if key
        not in (
            "system_name_or_uuid",
            "expected_plan_digest",
            "exclusive_writer_window",
        )
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


def profile_name_for(operation_id: str) -> str:
    """The saved profile an operation's provision creates (ADR 0313 Decision 10)."""
    return f"hmcpctl_{operation_id[:12]}"


def expected_profile(request: ProvisionRequest) -> ProfilePatch:
    """The requested memory, processors and units the saved profile must hold."""
    resources = request.resources
    values: dict[str, Any] = {
        "min_memory": resources.min_memory,
        "desired_memory": resources.desired_memory,
        "max_memory": resources.max_memory,
    }
    if resources.dedicated:
        procs = resources.desired_procs
        if procs is not None and float(procs).is_integer():
            values["desired_processors"] = int(procs)
    else:
        values["desired_processors"] = resources.desired_vcpus
        if resources.desired_procs is not None:
            values["desired_units"] = float(resources.desired_procs)
    return ProfilePatch(**{k: v for k, v in values.items() if v is not None})


def _count(element: Any, *names: str) -> int:
    return sum(1 for node in element.iter() if localname(node.tag) in names)


def _vlan_adapter(element: Any, vlan: int) -> bool:
    for node in element.iter():
        if localname(node.tag) == "ProfileClientNetworkAdapter":
            for child in node:
                if localname(child.tag) == "PortVLANID" and (
                    child.text or ""
                ).strip() == str(vlan):
                    return True
    return False


def readback_problem(element: Any, request: ProvisionRequest, sriov: int) -> str | None:
    """Why the saved profile does not hold what provision built, or None when it does."""
    if not profile_write.holds(element, expected_profile(request)):
        return "the saved profile's memory, processors or units differ from the request"
    if not _vlan_adapter(element, request.adapters.port_vlan_id):
        return "the saved profile has no client network adapter on the requested VLAN"
    if not _count(element, "ProfileVirtualSCSIClientAdapter"):
        return "the saved profile has no virtual SCSI client adapter"
    vnics = len(request.assignments.vnics)
    if _count(element, "ProfileVirtualNICDedicated", "ProfileVirtualNICShared") < vnics:
        return "the saved profile lacks a requested vNIC"
    if _count(element, "ProfileSRIOVEthernetLogicalPort") < sriov:
        return "the saved profile lacks a requested SR-IOV logical port"
    return None


async def _partition_resource(hmc: HMCClient, lpar_uuid: str) -> Mapping[str, Any]:
    entry = await hmc.get_logical_partition(lpar_uuid)
    resource = (entry or {}).get("Resource") if isinstance(entry, dict) else None
    if not isinstance(resource, dict):
        raise HMCError(f"LPAR {lpar_uuid} could not be read")
    return resource


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
        profile_uuid = (
            await self._persist(system_uuid, lpar_uuid) if completed else None
        )
        completed = profile_uuid is not None
        if completed and self.request.power_on:
            completed = (
                await self._leg(
                    "power_on",
                    f"lpar:{lpar_uuid}",
                    lambda: self._write_power(lpar_uuid, profile_uuid),
                )
                is not None
            )
        if completed:
            completed, warnings = await _run_affinity_stage(
                self.steps, self.hmc, self.request
            )
            self.warnings.extend(warnings)
        elif self.request.affinity_assessment is not None:
            _skip_steps(self.steps, ["affinity_assessment"])
        outcome = "configured" if completed else "needs_attention"
        return BodyResult(outcome, self._result(lpar_uuid, completed))

    def _skip_rest(self, *steps: str) -> None:
        _skip_steps(
            self.steps, [*steps, *(["power_on"] if self.request.power_on else [])]
        )

    async def _persist(self, system_uuid: str, lpar_uuid: str) -> str | None:
        """Save, read back and default the profile; its UUID, or None to pause."""
        name = profile_name_for(self.ctx.operation_id)
        identity = await self._leg(
            "profile",
            profile_write.target_by_name(lpar_uuid, name),
            lambda: self._write_profile(system_uuid, lpar_uuid, name),
        )
        if identity is None:
            self._skip_rest("profile_readback", "default_profile")
            return None
        profile = await profile_write.profile_by_name(
            self.hmc, lpar_uuid, system_uuid, name
        )
        problem = (
            "the saved profile is missing"
            if profile is None
            else readback_problem(
                profile.element, self.request, len(self._assignments().sriov)
            )
        )
        if problem is not None:
            self.steps.append(WorkflowStep("profile_readback", "error", problem))
            self._skip_rest("default_profile")
            return None
        self.steps.append(
            WorkflowStep("profile_readback", "ok", {"profile_name": name})
        )
        if (
            await self._leg(
                "default_profile",
                f"lpar:{lpar_uuid}",
                lambda: self._write_default(lpar_uuid, name),
            )
            is None
        ):
            self._skip_rest()
            return None
        return str(identity["profile_uuid"])

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
            # Bounded like an engine warning, so a verbose HMC error cannot push the
            # result past store.MAX_RESULT_BYTES.
            self.steps.append(
                WorkflowStep(
                    STEP_NAMES[key], "error", f"{type(exc).__name__}: {exc}"[:512]
                )
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
        for index, (key, target, write) in enumerate(legs):
            identity = await self._leg(key, target, write)
            if key == "assignments":
                self._replace_assignment_step()
            if identity is None:
                self._skip_rest(
                    *(STEP_NAMES[k] for k, _, _ in legs[index + 1 :]), *_PERSIST_STEPS
                )
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

    def _assignments(self) -> LparPcieAssignments:
        assignments = self.request.assignments
        if self.request.physical_io.requested:
            # The create applied these with its physical I/O.
            return dataclasses.replace(assignments, sriov=())
        return assignments

    def _replace_assignment_step(self) -> None:
        if self.assignment_steps and self.steps[-1].step == "assignments":
            self.steps[-1:] = list(self.assignment_steps)

    async def _write_policy(self, policy: MinimumAffinityPolicy) -> dict[str, Any]:
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

    async def _write_profile(
        self, system_uuid: str, lpar_uuid: str, name: str
    ) -> dict[str, Any]:
        # #637's helper can misread a landed save (#1867), so the profile itself decides
        # (operator decision D1, 2026-11-03), including after the helper raised.
        failure: Exception | None = None
        try:
            await save_current_partition_profile(
                self.hmc, self.system, lpar_uuid, name, exclusive_writer_window=True
            )
        except Exception as exc:  # noqa: BLE001 - the by-name read below decides it
            failure = exc
        profile = await profile_write.profile_by_name(
            self.hmc, lpar_uuid, system_uuid, name
        )
        if profile is None:
            reason = f": {type(failure).__name__}" if failure is not None else ""
            raise EffectNotApplied(
                f"no saved profile was created for LPAR {lpar_uuid}{reason}"
            ) from failure
        return {"profile_uuid": profile.uuid, "profile_name": name}

    async def _write_default(self, lpar_uuid: str, name: str) -> dict[str, Any]:
        outcome = await set_default_partition_profile(
            self.hmc, self.system, lpar_uuid, name, exclusive_writer_window=True
        )
        if (outcome.execution, outcome.requested_change) == ("completed", "verified"):
            return {"profile_name": name}
        resource = await _partition_resource(self.hmc, lpar_uuid)
        if leaf_text(resource.get("DefaultProfileName")) == name:
            return {"profile_name": name}
        raise HMCError(
            f"the default profile of LPAR {lpar_uuid} is unconfirmed ({outcome.execution})"
        )

    async def _write_power(self, lpar_uuid: str, profile_uuid: str) -> dict[str, Any]:
        result = await _power_on(
            self.hmc,
            self.system,
            lpar_uuid,
            self.request.affinity_assessment,
            partition_profile_uuid=profile_uuid,
            warnings=self.warnings,
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
        if storage.storage_name not in virtual_disk_names(group[0]):
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
            resource = await _partition_resource(hmc, _target_uuid(effect))
        state = str(leaf_text(resource.get("PartitionState")) or "").strip().lower()
        if state not in ACTIVATED_STATES:
            return Classification(
                "needs_attention",
                reason="a power-on job may still be queued, so it is not resubmitted",
            )
        if leaf_text(resource.get("LastActivatedProfile")) != profile_name_for(
            ctx.operation_id
        ):
            return Classification(
                "needs_attention",
                reason="the partition is active from another profile; inspect, then abandon",
            )
        return Classification("applied", {"job_id": None})

    async def default_profile(
        ctx: OperationContext, effect: EffectRecord
    ) -> Classification:
        name = profile_name_for(ctx.operation_id)
        async with client_factory(ctx.connection) as hmc:
            resource = await _partition_resource(hmc, _target_uuid(effect))
        if leaf_text(resource.get("DefaultProfileName")) == name:
            return Classification("applied", {"profile_name": name})
        return Classification("not_applied")

    expected = expected_profile(request)

    return {
        EFFECT_KINDS["create"]: create,
        EFFECT_KINDS["stamp"]: stamp,
        EFFECT_KINDS["network"]: network,
        EFFECT_KINDS["disk"]: disk,
        EFFECT_KINDS["storage"]: mapping,
        EFFECT_KINDS["profile"]: profile_write.profile_write_classifier(
            lambda _ctx, _effect: (expected, expected), client_factory
        ),
        EFFECT_KINDS["default_profile"]: default_profile,
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
    if record.result and record.outcome in ("configured", "needs_attention"):
        result = _RESULT.validate_python(record.result)
        if record.outcome == "configured" or _agrees(result, record):
            # A pause the engine added (an open effect, a classifier's reason) is why
            # the operation stopped, so it joins the body's own warnings.
            warnings = tuple(dict.fromkeys((*result.warnings, *record.warnings)))
            return dataclasses.replace(result, warnings=warnings, operation=record)
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
            # A write still in flight has no outcome to report yet.
            if record.state != "running" or effect.status in ("applied", "not_applied")
        ),
        warnings=record.warnings,
        operation=record,
    )
