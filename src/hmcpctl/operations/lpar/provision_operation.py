"""``hmc_provision_lpar``'s operation body and effect classifiers (ADR 0313).

Each HMC write is a journaled effect, so a repeat never writes twice and an
interrupted write is classified by a live read before anything else is written.
A leg's step error keeps its exception text, bounded to 512 characters.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import TypeAdapter

from ...client.client_storage import mapping_lpar_uuid
from ...client.core import HMCClient
from ...documents.lpar import LparResources, validate_partition_type
from ...documents.partition_profile import ProfilePatch
from ...errors import HMCError
from ...jobs import JobOutcome, job_identifier
from ...resource_identity import resolve_system_uuid
from ...ssh.affinity import MinimumAffinityPolicy
from ...ssh.lpar import apply_lpar_profile_via_cli
from ...ssh.refcodes import list_lpar_refcodes
from ...ssh.selectors import resolve_ssh_names
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
from ..storage.resources import mount_optical_media, storage_mapping, upload_iso
from .activation import ACTIVATED_STATES
from .assignments import (
    LparPcieAssignments,
    apply_validated_lpar_pcie_assignments,
    assignment_step_names,
)
from .boot_order import read_lpar_boot_order, set_lpar_boot_order
from .core import LparCreation, create_and_stamp_lpar
from .install_media import (
    UNVERIFIED,
    MediaBinding,
    MediaMismatch,
    bind_built,
    bind_prepared,
    client_slot,
    device_path,
    media_name_for,
    parse_result,
    repository_group,
    target_request,
)
from .media_build import run_build_entry
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
    require_install_url,
    validate_provision_assignments,
    validate_provision_install,
    validate_provision_storage,
    virtual_disk_names,
)
from .provision_boot import REFCODE_ROWS, row_identity, wait_for_boot
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
    # ADR 0318: the dedicated slots, written into the saved profile.
    "dedicated": "lpar.pcie_assignments",
    "default_profile": "lpar.default_profile",
    "power_on": "lpar.power_on",
    # ADR 0331: a run that does not power on applies its saved profile instead.
    "apply_saved": "lpar.profile_apply",
    # Installer media (ADR 0319); the build is built media's (ADR 0324).
    "build": "media.iso_build",
    "upload": "vios.optical_media_upload",
    "mount": "vios.optical_mapping",
    "boot_order": "lpar.boot_order",
    # The installer boot (ADR 0326); its power-on is the `power_on` effect above.
    "refcode_baseline": "lpar.refcode_baseline",
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
    "dedicated": "dedicated",
    "default_profile": "default_profile",
    "power_on": "power_on",
    "apply_saved": "apply_saved_profile",
    "build": "build_media",
    "upload": "upload_media",
    "mount": "mount_media",
    "boot_order": "set_boot_order",
    "refcode_baseline": "refcode_baseline",
}
# Ledger kinds (ADR 0313 Decision 6).
PARTITION_LEDGER = "partition"
DISK_LEDGER = "virtual_disk"
MEDIA_LEDGER = "installer_media"
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
# Legs whose result is reported as their per-assignment steps.
_ASSIGNMENT_KEYS = ("assignments", "dedicated")
_MEDIA_STEPS = ("bind_media", "upload_media", "mount_media")
_BOOT_STEPS = ("revalidate_boot", "refcode_baseline", "power_on", "wait_boot_started")
_REST0269_MOUNT = (
    "the HMC refused the installer media mapping because the partition has no applied "
    "current configuration (REST0269). A refused mapping create can leave a VIOS virtual "
    "SCSI server adapter with no target device; compare `lshwres -r virtualio --rsubtype "
    "scsi -m <managed-system> --level lpar --filter lpar_names=<vios-name> -F "
    "slot_num,remote_lpar_name,remote_slot_num` with the VIOS mappings, then decommission"
)
_LEG_ERRORS = (HMCError, HMCCLIError, ValueError, LookupError, EffectNotApplied)
_BUILD_INTERRUPTED = (
    "the build entry's run was interrupted and its result is unrecorded; hmcpctl "
    "does not run it twice: abandon, decommission the partition, remove any ISO it "
    "published, then provision again"
)


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
    validate_provision_install(request)
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


def _vscsi_in_slot(element: Any, slot: int) -> bool:
    for node in element.iter():
        if localname(node.tag) == "ProfileVirtualSCSIClientAdapter":
            for child in node:
                if localname(child.tag) == "VirtualSlotNumber" and (
                    child.text or ""
                ).strip() == str(slot):
                    return True
    return False


def readback_problem(
    element: Any,
    request: ProvisionRequest,
    sriov: int,
    optical_slot: int | None = None,
) -> str | None:
    """Why the saved profile does not hold what provision built, or None when it does.

    With installer media mounted, the profile must carry the optical mapping's client
    adapter, or activating it would drop the media (ADR 0319).
    """
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
    if optical_slot is not None and not _vscsi_in_slot(element, optical_slot):
        return (
            "the saved profile has no virtual SCSI client adapter in the installer "
            f"media's slot {optical_slot}"
        )
    return None


async def _partition_resource(hmc: HMCClient, lpar_uuid: str) -> Mapping[str, Any]:
    entry = await hmc.get_logical_partition(lpar_uuid)
    resource = (entry or {}).get("Resource") if isinstance(entry, dict) else None
    if not isinstance(resource, dict):
        raise HMCError(f"LPAR {lpar_uuid} could not be read")
    return resource


def _state(resource: Mapping[str, Any]) -> str:
    return str(leaf_text(resource.get("PartitionState")) or "").strip().lower()


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


async def _media_paths(
    hmc: HMCClient, vios_uuid: str, lpar_uuid: str, media_name: str
) -> list[str | None]:
    """Device paths of *lpar_uuid*'s optical mappings that hold *media_name*."""
    paths = []
    for mapping in await hmc.list_optical_mappings(vios_uuid, lpar_uuid):
        storage = mapping.get("Storage") if isinstance(mapping, dict) else None
        optical = (
            storage.get("VirtualOpticalMedia") if isinstance(storage, dict) else None
        )
        if (
            isinstance(optical, dict)
            and leaf_text(optical.get("MediaName")) == media_name
        ):
            paths.append(device_path(mapping))
    return paths


def _mount_identity(vios_uuid: str, media_name: str, path: str) -> dict[str, Any]:
    return {
        "vios_uuid": vios_uuid,
        "media_name": media_name,
        "client_slot": client_slot(path),
        "device_path": path,
    }


async def _root_disk_mapped(
    hmc: HMCClient, request: ProvisionRequest, lpar_uuid: str
) -> bool:
    """Whether the request's root disk is still mapped to *lpar_uuid*."""
    storage = request.storage
    return any(
        found.backing_name == storage.storage_name
        and (found.lpar_uuid or "").lower() == lpar_uuid
        for found in map(
            storage_mapping, await hmc.list_storage_mappings(storage.vios_uuid)
        )
    )


async def boot_devices(
    hmc: HMCClient, request: ProvisionRequest, lpar_uuid: str, optical_path: str
) -> list[str]:
    """Installer media first, then the root disk (ADR 0191 Decision 6)."""
    storage = request.storage
    for entry in await hmc.list_storage_mappings(storage.vios_uuid):
        found = storage_mapping(entry)
        if found.backing_name == storage.storage_name and (
            (found.lpar_uuid or "").lower() == lpar_uuid
        ):
            disk = device_path(entry)
            if disk is not None:
                return [optical_path, disk]
    raise HMCError(f"the root disk mapping of LPAR {lpar_uuid} lists no device path")


async def _adapters_on_vlan(
    hmc: HMCClient, lpar_uuid: str, vlan: int
) -> list[dict[str, Any]]:
    adapters = await hmc.list_adapters(lpar_uuid, "ClientNetworkAdapter")
    return [
        a
        for a in adapters
        if str(leaf_text((a.get("Resource") or {}).get("PortVLANID"))) == str(vlan)
    ]


def _vios_mappings(resource: Mapping[str, Any], block: str, item: str) -> list[Any]:
    found = (resource.get(block) or {}) if isinstance(resource, Mapping) else {}
    items = found.get(item) if isinstance(found, Mapping) else None
    return [m for m in (items if isinstance(items, list) else [items]) if m]


async def _partition_disks(
    hmc: HMCClient, system_uuid: str, lpar_uuid: str
) -> list[tuple[str, str | None]]:
    """``(vios_uuid, backing)`` of every mapping to *lpar_uuid* on the system that is
    not optical media; a virtual Fibre Channel mapping has no backing name."""
    disks: list[tuple[str, str | None]] = []
    for vios in await hmc.list_vios(system_uuid):
        vios_uuid = str(vios.get("UUID") or "").lower()
        resource = vios.get("Resource") or {}
        for kind in ("VirtualSCSIMapping", "VirtualFibreChannelMapping"):
            for mapping in _vios_mappings(resource, f"{kind}s", kind):
                if (mapping_lpar_uuid(mapping) or "").lower() != lpar_uuid:
                    continue
                if kind == "VirtualFibreChannelMapping":
                    disks.append((vios_uuid, None))
                    continue
                found = storage_mapping(mapping)
                if mapping.get("Storage") and (
                    found.backing_kind != "VirtualOpticalMedia"
                ):
                    disks.append((vios_uuid, found.backing_name))
    return disks


def _recorded_field(ctx: OperationContext, key: str, field: str) -> Any:
    effect = ctx.recorded(key)
    return ((effect.identity if effect else None) or {}).get(field)


async def boot_problem(
    hmc: HMCClient,
    ctx: OperationContext,
    request: ProvisionRequest,
    system_uuid: str,
    lpar_uuid: str,
) -> str | None:
    """Why the partition is no longer as the run left it before power-on, or None
    (ADR 0191 Decision 1, ADR 0326 Decision 2)."""
    resource = await _partition_resource(hmc, lpar_uuid)
    state = _state(resource)
    if state != "not activated":
        return f"the partition is {state or 'in an unread state'}, not powered off"
    vios = request.storage.vios_uuid.lower()
    name = media_name_for(ctx.operation_id)
    try:
        group = await repository_group(hmc, vios)
    except ValueError:
        return "the media repository is no longer one volume group"
    if not any(
        leaf_text(m.get("MediaName")) == name
        for m in await hmc.list_optical_media(vios, group)
    ):
        return "the installer media is no longer in the media repository"
    if await _media_paths(hmc, vios, lpar_uuid, name) != [
        _recorded_field(ctx, "mount", "device_path")
    ]:
        return "the installer media is not mapped once at its recorded device path"
    observed = await read_lpar_boot_order(
        hmc, lpar_uuid, system_name_or_uuid=system_uuid
    )
    if observed["pending_boot_string"] != _recorded_field(
        ctx, "boot_order", "pending_boot_string"
    ):
        return "the pending boot order differs from the one this operation wrote"
    adapters = await _adapters_on_vlan(hmc, lpar_uuid, request.adapters.port_vlan_id)
    identity = _adapter_identity(adapters[0]) if len(adapters) == 1 else None
    if identity is None or identity["mac"] != _recorded_field(ctx, "network", "mac"):
        return (
            "the client adapter on the VLAN no longer carries the MAC the media binds"
        )
    if await _partition_disks(hmc, system_uuid, lpar_uuid) != [
        (vios, request.storage.storage_name)
    ]:
        return "the owned root disk is no longer the partition's only disk"
    return None


class _Run:
    """One execution of the body: the steps so far and the writes still to make."""

    def __init__(
        self,
        ctx: OperationContext,
        hmc: HMCClient,
        system: str,
        request: ProvisionRequest,
        wait_seconds: int = 0,
    ) -> None:
        self.ctx, self.hmc, self.system, self.request = ctx, hmc, system, request
        # The boot wait ends when the call's own wait does (ADR 0326 Decision 5).
        self.wait_deadline = time.monotonic() + wait_seconds
        self.steps: list[WorkflowStep] = []
        self.warnings: list[str] = []
        self.change_location = None
        self.assignment_steps: tuple[WorkflowStep, ...] = ()
        self.optical_slot: int | None = None
        self.optical_path = ""

    def _result(
        self, lpar_uuid: str, completed: bool, unverified: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        result = ProvisionResult(
            resource_created=True,
            workflow_completed=completed,
            lpar_uuid=lpar_uuid,
            dry_run=False,
            ownership_stamped=True,
            steps=tuple(self.steps),
            warnings=tuple(self.warnings),
            change_location=self.change_location,
            unverified=unverified,
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
        if completed and self.request.install is not None:
            completed = await self._media(system_uuid, lpar_uuid)
        profile_uuid = (
            await self._persist(system_uuid, lpar_uuid) if completed else None
        )
        completed = profile_uuid is not None
        if self.request.install is not None:
            return await self._ready(system_uuid, lpar_uuid, profile_uuid)
        if completed and self.request.power_on:
            completed = (
                await self._leg(
                    "power_on",
                    f"lpar:{lpar_uuid}",
                    lambda: self._write_power(lpar_uuid, profile_uuid),
                )
                is not None
            )
        elif completed:
            completed = (
                await self._leg(
                    "apply_saved",
                    f"lpar:{lpar_uuid}",
                    lambda: self._write_apply(system_uuid, lpar_uuid),
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
        if self.request.install is not None:
            tail = ["set_boot_order", *(_BOOT_STEPS if self._booting() else ())]
        else:
            tail = ["power_on" if self.request.power_on else "apply_saved_profile"]
        _skip_steps(self.steps, [*steps, *tail])

    async def _media(self, system_uuid: str, lpar_uuid: str) -> bool:
        """Bind, upload and mount the installer media before the save (ADR 0319)."""
        request = self.request
        install = request.install
        assert install is not None
        self.ctx.set_phase("media_bound")
        disk = self.ctx.recorded("disk")
        if disk is None or (disk.identity or {}).get("disk_name") != (
            request.storage.storage_name
        ):
            raise OperationFailed(
                "the media's root disk is not one this operation created"
            )
        if install.media.mode == "built":
            built = await self._leg(
                "build",
                f"lpar:{lpar_uuid}",
                lambda: self._write_build(lpar_uuid),
                definite=True,
            )
            if built is None:
                self._skip_rest(*_MEDIA_STEPS, *self._persist_steps())
                return False
            binding = MediaBinding(
                built["url"], built["iso_sha256"], built["iso_size"], built["mac"]
            )
        else:
            try:
                binding = bind_prepared(install, request.adapters.mac)
            except MediaMismatch as exc:
                raise OperationFailed(str(exc)) from exc
        network = self.ctx.recorded("network")
        if network is None or (network.identity or {}).get("mac") != binding.mac:
            raise OperationFailed(
                f"the adapter on LPAR {lpar_uuid} does not carry the MAC the media "
                "binds; nothing was uploaded"
            )
        name = media_name_for(self.ctx.operation_id)
        self.steps.append(
            WorkflowStep(
                "bind_media",
                "ok",
                {"media_name": name, "iso_sha256": binding.iso_sha256},
            )
        )
        vios = request.storage.vios_uuid.lower()
        rest = ("mount_media", *self._persist_steps())
        uploaded = await self._leg(
            "upload",
            f"vios:{vios}",
            lambda: self._write_upload(binding, name),
            definite=True,
        )
        if uploaded is None:
            self._skip_rest(*rest)
            return False
        self.ctx.ledger_add(
            MEDIA_LEDGER,
            MEDIA_LEDGER,
            system_uuid,
            lpar_uuid,
            {"vios_uuid": vios, "media_name": name},
        )
        mounted = await self._leg(
            "mount",
            f"lpar:{lpar_uuid}",
            lambda: self._write_mount(lpar_uuid, name),
            definite=True,
        )
        if mounted is None:
            self._skip_rest(*self._persist_steps())
            return False
        self.optical_slot = int(mounted["client_slot"])
        self.optical_path = str(mounted["device_path"])
        return True

    def _booting(self) -> bool:
        """Whether this run boots: immediate, `continuation: boot`, or a resume of a
        boot already begun (ADR 0326 Decision 1)."""
        return (
            self.request.boot == "immediate"
            or self.ctx.continuation == "boot"
            or self.ctx.recorded("refcode_baseline") is not None
        )

    async def _ready(
        self, system_uuid: str, lpar_uuid: str, profile_uuid: str | None
    ) -> BodyResult:
        """Write the boot order after the default profile (ADR 0319), then pause or boot."""
        completed = profile_uuid is not None and (
            await self._leg(
                "boot_order",
                f"lpar:{lpar_uuid}",
                lambda: self._write_boot(lpar_uuid, self.optical_path),
            )
            is not None
        )
        if not completed:
            return BodyResult("needs_attention", self._result(lpar_uuid, False))
        if not self._booting():
            self.ctx.set_phase("ready_to_boot")
            return BodyResult(
                "ready_to_boot", self._result(lpar_uuid, True, UNVERIFIED)
            )
        assert profile_uuid is not None
        return await self._boot(system_uuid, lpar_uuid, profile_uuid)

    def _attention(self, lpar_uuid: str, *skipped: str) -> BodyResult:
        _skip_steps(self.steps, list(skipped))
        return BodyResult("needs_attention", self._result(lpar_uuid, False, UNVERIFIED))

    async def _boot(
        self, system_uuid: str, lpar_uuid: str, profile_uuid: str
    ) -> BodyResult:
        """Revalidate, journal the baseline, power on once, then wait (ADR 0326)."""
        self.ctx.set_phase("booting")
        lpar = f"lpar:{lpar_uuid}"
        if self.ctx.recorded("power_on") is None:
            problem = await boot_problem(
                self.hmc, self.ctx, self.request, system_uuid, lpar_uuid
            )
            self.steps.append(
                WorkflowStep("revalidate_boot", "error" if problem else "ok", problem)
            )
            if problem is not None:
                return self._attention(lpar_uuid, *_BOOT_STEPS[1:])
        names = await resolve_ssh_names(self.hmc, system_uuid, lpar_uuid)
        baseline = await self._leg(
            "refcode_baseline", lpar, lambda: self._write_baseline(*names)
        )
        if baseline is None:
            return self._attention(lpar_uuid, *_BOOT_STEPS[2:])
        if (
            await self._leg(
                "power_on", lpar, lambda: self._write_power(lpar_uuid, profile_uuid)
            )
            is None
        ):
            return self._attention(lpar_uuid, _BOOT_STEPS[3])
        wait = await wait_for_boot(
            lambda: list_lpar_refcodes(self.hmc.config, *names, REFCODE_ROWS),
            baseline,
            max(0.0, self.wait_deadline - time.monotonic()),
        )
        if wait.started is not None:
            self.steps.append(WorkflowStep("wait_boot_started", "ok", wait.started))
            self.ctx.set_phase("boot_started")
            return BodyResult("boot_started", self._result(lpar_uuid, True, UNVERIFIED))
        detail = {
            "reason": wait.reason,
            "baseline": dict(baseline),
            "observed": wait.observed,
        }
        self.steps.append(WorkflowStep("wait_boot_started", "error", detail))
        return BodyResult("needs_attention", self._result(lpar_uuid, False, UNVERIFIED))

    async def _persist(self, system_uuid: str, lpar_uuid: str) -> str | None:
        """Save, read back and default the profile; its UUID, or None to pause."""
        name = profile_name_for(self.ctx.operation_id)
        identity = await self._leg(
            "profile",
            profile_write.target_by_name(lpar_uuid, name),
            lambda: self._write_profile(system_uuid, lpar_uuid, name),
        )
        if identity is None:
            self._skip_rest(
                "profile_readback", *self._dedicated_steps(), "default_profile"
            )
            return None
        profile = await profile_write.profile_by_name(
            self.hmc, lpar_uuid, system_uuid, name
        )
        problem = (
            "the saved profile is missing"
            if profile is None
            else readback_problem(
                profile.element,
                self.request,
                len(self._assignments().sriov),
                self.optical_slot,
            )
        )
        if problem is not None:
            self.steps.append(WorkflowStep("profile_readback", "error", problem))
            self._skip_rest(*self._dedicated_steps(), "default_profile")
            return None
        self.steps.append(
            WorkflowStep("profile_readback", "ok", {"profile_name": name})
        )
        if self.request.assignments.dedicated:
            written = await self._leg(
                "dedicated", f"lpar:{lpar_uuid}", lambda: self._write_dedicated(name)
            )
            self._replace_assignment_step()
            if written is None:
                self._skip_rest("default_profile")
                return None
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
        *,
        definite: bool = False,
    ) -> Mapping[str, Any] | None:
        """Run one effect; an error pauses the run, or with *definite* an
        ``EffectNotApplied`` propagates so the run ends ``failed``."""
        # Only the steps this leg's own write produced may replace its summary step.
        self.assignment_steps = ()
        try:
            identity = await self.ctx.effect(key, EFFECT_KINDS[key], target, write)
        except _LEG_ERRORS as exc:
            if definite and isinstance(exc, EffectNotApplied):
                raise
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
                    *(STEP_NAMES[k] for k, _, _ in legs[index + 1 :]),
                    *self._media_steps(),
                    *self._persist_steps(),
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
        """The current-configuration assignments; dedicated slots go to the saved
        profile instead (ADR 0318)."""
        assignments = dataclasses.replace(self.request.assignments, dedicated=())
        if self.request.physical_io.requested:
            # The create applied these with its physical I/O.
            return dataclasses.replace(assignments, sriov=())
        return assignments

    def _media_steps(self) -> tuple[str, ...]:
        install = self.request.install
        if install is None:
            return ()
        built = ("build_media",) if install.media.mode == "built" else ()
        return (*built, *_MEDIA_STEPS)

    def _persist_steps(self) -> list[str]:
        return [
            "save_profile",
            "profile_readback",
            *self._dedicated_steps(),
            "default_profile",
        ]

    def _dedicated_steps(self) -> list[str]:
        return assignment_step_names(
            LparPcieAssignments(self.request.assignments.dedicated)
        )

    def _replace_assignment_step(self) -> None:
        if self.assignment_steps and self.steps[-1].step in _ASSIGNMENT_KEYS:
            self.steps[-1:] = list(self.assignment_steps)

    async def _write_policy(self, policy: MinimumAffinityPolicy) -> dict[str, Any]:
        await set_minimum_affinity_policy(
            self.hmc, self.request.name, policy, system_name_or_uuid=self.system
        )
        return {}

    async def _write_network(self, lpar_uuid: str) -> dict[str, Any]:
        vlan = self.request.adapters.port_vlan_id
        created = await _add_network(
            self.hmc, lpar_uuid, vlan, self.request.adapters.mac
        )
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

    async def _write_build(self, lpar_uuid: str) -> dict[str, Any]:
        """Run the configured build entry once and bind its result (ADR 0324).

        The runner returns or raises only once its child has been reaped, so every
        error here is a build known not to have produced bound media.
        """
        install = self.request.install
        assert install is not None
        try:
            command = self.hmc.config.iso_build_command
            if command is None:
                raise ValueError(
                    "HMC_ISO_BUILD_ENTRY is no longer configured on the server"
                )
            network = self.ctx.recorded("network")
            mac = (network.identity or {}).get("mac") if network else None
            if not isinstance(mac, str):
                raise TypeError(f"no adapter MAC is recorded for LPAR {lpar_uuid}")
            request = target_request(
                install, self.request.name, mac, self.ctx.operation_id
            )
            result = parse_result(await run_build_entry(command, request))
            binding = bind_built(
                install,
                result,
                mac,
                self.ctx.operation_id,
                self.hmc.config.iso_url_allowlist_entries,
            )
        except Exception as exc:  # every outcome here is definite (ADR 0324 D3)
            raise EffectNotApplied(
                f"the installer media was not built: {type(exc).__name__}: {exc}"
            ) from exc
        return {
            "url": binding.url,
            "iso_sha256": binding.iso_sha256,
            "iso_size": binding.iso_size,
            "manifest_sha256": result["manifest_sha256"],
            "mac": binding.mac,
        }

    async def _write_upload(self, binding: MediaBinding, name: str) -> dict[str, Any]:
        storage = self.request.storage
        try:
            require_install_url(self.hmc, binding.url)
            group = await repository_group(self.hmc, storage.vios_uuid)
            await upload_iso(
                self.hmc,
                storage.vios_uuid,
                group,
                name,
                binding.url,
                system_name_or_uuid=self.system,
                expected_sha256=binding.iso_sha256,
                expected_size=binding.iso_size,
            )
        except ValueError as exc:
            # Every ValueError here is raised before the web File exists.
            raise EffectNotApplied(
                f"the installer media was not uploaded: {exc}"
            ) from exc
        return {
            "vios_uuid": storage.vios_uuid.lower(),
            "vg_uuid": group,
            "media_name": name,
        }

    async def _write_mount(self, lpar_uuid: str, name: str) -> dict[str, Any]:
        vios = self.request.storage.vios_uuid.lower()
        try:
            await mount_optical_media(
                self.hmc,
                vios,
                lpar_uuid,
                system_name_or_uuid=self.system,
                media_name=name,
            )
        except HMCError as exc:
            if "REST0269" in f"{exc} {exc.body or ''}":
                raise EffectNotApplied(_REST0269_MOUNT) from exc
            raise
        paths = await _media_paths(self.hmc, vios, lpar_uuid, name)
        if len(paths) != 1 or paths[0] is None:
            raise HMCError(
                f"the installer media mapping of LPAR {lpar_uuid} is not listed once "
                "with a device path"
            )
        return _mount_identity(vios, name, paths[0])

    async def _write_baseline(self, system_name: str, lpar_name: str) -> dict[str, Any]:
        """The newest reference code before power-on; a failed read changed nothing."""
        try:
            rows = await list_lpar_refcodes(self.hmc.config, system_name, lpar_name, 1)
        except (HMCCLIError, ValueError, TypeError) as exc:
            raise EffectNotApplied(
                f"the refcode baseline was not read: {type(exc).__name__}: {exc}"
            ) from exc
        return row_identity(rows[0] if rows else None)

    async def _write_boot(self, lpar_uuid: str, optical: str) -> dict[str, Any]:
        devices = await boot_devices(self.hmc, self.request, lpar_uuid, optical)
        await set_lpar_boot_order(
            self.hmc, lpar_uuid, devices, system_name_or_uuid=self.system
        )
        return {"pending_boot_string": " ".join(devices)}

    async def _write_assignments(self) -> dict[str, Any]:
        result = await apply_validated_lpar_pcie_assignments(
            self.hmc, self.system, self.request.name, self._assignments()
        )
        self.assignment_steps = result.steps
        if not result.workflow_completed:
            failed = [s.step for s in result.steps if s.status == "error"]
            raise HMCError(f"assignment steps reported errors: {', '.join(failed)}")
        return {"steps": [[s.step, s.status] for s in result.steps]}

    async def _write_dedicated(self, profile_name: str) -> dict[str, Any]:
        """Write each requested slot into the saved profile (ADR 0318)."""
        dedicated = tuple(
            dataclasses.replace(item, profile_name=profile_name)
            for item in self.request.assignments.dedicated
        )
        result = await apply_validated_lpar_pcie_assignments(
            self.hmc, self.system, self.request.name, LparPcieAssignments(dedicated)
        )
        self.assignment_steps = result.steps
        if not result.workflow_completed:
            failed = [s.step for s in result.steps if s.status == "error"]
            raise HMCError(f"dedicated slot steps reported errors: {', '.join(failed)}")
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
            reason = (
                f": {type(failure).__name__}: {failure}" if failure is not None else ""
            )
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

    async def _write_apply(self, system_uuid: str, lpar_uuid: str) -> dict[str, Any]:
        """Apply the saved profile without activation, then prove it (ADR 0331).

        A partition never activated keeps its creation profile as last activated, and
        ``hmc_reconfigure_lpar`` refuses a last-activated profile other than the
        default (ADR 0314 Decision 2).
        """
        name = profile_name_for(self.ctx.operation_id)
        resource = await _partition_resource(self.hmc, lpar_uuid)
        state = _state(resource)
        if state != "not activated":
            raise HMCError(
                f"LPAR {lpar_uuid} is {state or 'unreadable'}, not not activated: "
                "its saved profile is not applied"
            )
        system_name, _ = await resolve_ssh_names(self.hmc, system_uuid, None)
        lpar_name = str(leaf_text(resource.get("PartitionName")) or "")
        if not system_name or not lpar_name:
            raise HMCError(f"LPAR {lpar_uuid} or its system has no name to apply by")
        await apply_lpar_profile_via_cli(
            self.hmc.config, system_name, lpar_name, profile_name=name
        )
        resource = await _partition_resource(self.hmc, lpar_uuid)
        profiles = {
            leaf_text(resource.get(field))
            for field in ("LastActivatedProfile", "DefaultProfileName")
        }
        if profiles != {name} or _state(resource) != "not activated":
            raise HMCError(
                f"LPAR {lpar_uuid} did not read back {name} as its default and "
                "last-activated profile after the apply"
            )
        if not await _adapters_on_vlan(
            self.hmc, lpar_uuid, self.request.adapters.port_vlan_id
        ) or not await _root_disk_mapped(self.hmc, self.request, lpar_uuid):
            raise HMCError(
                f"LPAR {lpar_uuid} lost its network adapter or root disk mapping "
                "after the apply"
            )
        return {"profile_name": name}

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
    system: str,
    request: ProvisionRequest,
    client_factory: ClientFactory = open_client,
    *,
    wait_seconds: int = 0,
) -> engine.Body:
    """The operation body for one ``hmc_provision_lpar`` call; a boot waits for
    refcodes up to *wait_seconds*, the call's own (ADR 0326 Decision 5)."""

    async def body(ctx: OperationContext) -> BodyResult:
        async with client_factory(ctx.connection) as hmc:
            return await _Run(ctx, hmc, system, request, wait_seconds).execute()

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
        pinned = request.adapters.mac
        if pinned is not None and identity["mac"] != pinned:
            return Classification(
                "needs_attention", reason="the adapter's MAC is not adapters.mac"
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
        state = _state(resource)
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

    async def apply_saved(
        ctx: OperationContext, effect: EffectRecord
    ) -> Classification:
        name = profile_name_for(ctx.operation_id)
        async with client_factory(ctx.connection) as hmc:
            resource = await _partition_resource(hmc, _target_uuid(effect))
        if leaf_text(resource.get("LastActivatedProfile")) == name:
            return Classification("applied", {"profile_name": name})
        if _state(resource) != "not activated":
            return Classification(
                "needs_attention",
                reason="the partition is active from another profile; inspect, then abandon",
            )
        return Classification("not_applied")

    async def default_profile(
        ctx: OperationContext, effect: EffectRecord
    ) -> Classification:
        name = profile_name_for(ctx.operation_id)
        async with client_factory(ctx.connection) as hmc:
            resource = await _partition_resource(hmc, _target_uuid(effect))
        if leaf_text(resource.get("DefaultProfileName")) == name:
            return Classification("applied", {"profile_name": name})
        return Classification("not_applied")

    async def build(_ctx: OperationContext, _effect: EffectRecord) -> Classification:
        return Classification("needs_attention", reason=_BUILD_INTERRUPTED)

    async def upload(ctx: OperationContext, effect: EffectRecord) -> Classification:
        vios, name = request.storage.vios_uuid.lower(), media_name_for(ctx.operation_id)
        async with client_factory(ctx.connection) as hmc:
            try:
                group = await repository_group(hmc, vios)
            except ValueError:
                return Classification(
                    "needs_attention", reason="the media repository is not one group"
                )
            media = await hmc.list_optical_media(vios, group)
        if any(leaf_text(m.get("MediaName")) == name for m in media):
            identity = {"vios_uuid": vios, "vg_uuid": group, "media_name": name}
            return Classification("applied", identity)
        return Classification("not_applied")

    async def mount(ctx: OperationContext, effect: EffectRecord) -> Classification:
        vios, name = request.storage.vios_uuid.lower(), media_name_for(ctx.operation_id)
        async with client_factory(ctx.connection) as hmc:
            paths = await _media_paths(hmc, vios, _target_uuid(effect), name)
        if not paths:
            return Classification("not_applied")
        if len(paths) > 1 or paths[0] is None:
            return Classification(
                "needs_attention",
                reason="the media's mappings are several or list no device path",
            )
        return Classification("applied", _mount_identity(vios, name, paths[0]))

    async def baseline(_ctx: OperationContext, _effect: EffectRecord) -> Classification:
        # A read: the power-on is recorded only after it settles (ADR 0326 Decision 3).
        return Classification("not_applied")

    async def boot_order(ctx: OperationContext, effect: EffectRecord) -> Classification:
        mount = ctx.recorded("mount")
        optical = (mount.identity or {}).get("device_path") if mount else None
        if not optical or ctx.partition is None:
            return Classification(
                "needs_attention", reason="no mounted media is recorded"
            )
        lpar_uuid = _target_uuid(effect)
        async with client_factory(ctx.connection) as hmc:
            devices = await boot_devices(hmc, request, lpar_uuid, str(optical))
            observed = await read_lpar_boot_order(
                hmc, lpar_uuid, system_name_or_uuid=ctx.partition[0]
            )
        wanted = " ".join(devices)
        if observed["pending_boot_string"] == wanted:
            return Classification("applied", {"pending_boot_string": wanted})
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
        EFFECT_KINDS["apply_saved"]: apply_saved,
        EFFECT_KINDS["build"]: build,
        EFFECT_KINDS["upload"]: upload,
        EFFECT_KINDS["mount"]: mount,
        EFFECT_KINDS["boot_order"]: boot_order,
        EFFECT_KINDS["refcode_baseline"]: baseline,
    }


def _agrees(result: ProvisionResult, record: OperationRecord) -> bool:
    """Whether *result*'s steps match the journal: a pause the body did not return
    (a failed run, or a reconcile pause after progress) keeps an older result."""
    steps = {step.step: step for step in reversed(result.steps)}
    for effect in record.effects:
        step = steps.get(STEP_NAMES.get(effect.key, effect.key))
        if effect.key in _ASSIGNMENT_KEYS and step is None:
            continue  # reported as its per-assignment steps
        if step is None or (step.status == "ok") != (effect.status == "applied"):
            return False
    return True


def result_from_record(record: OperationRecord) -> ProvisionResult:
    """The tool result: the body's own result where it agrees with the journal, else
    one rebuilt from the effects (ADR 0313 Decision 2)."""
    finished = ("configured", "ready_to_boot", "boot_started")
    if record.result and record.outcome in (*finished, "needs_attention"):
        result = _RESULT.validate_python(record.result)
        if record.outcome in finished or _agrees(result, record):
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
