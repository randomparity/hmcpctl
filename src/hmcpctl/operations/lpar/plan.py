"""The LPAR plan behind ``hmc_plan_lpar`` (ADR 0198).

Reads what provisioning would need, through the delegated reads ADR 0198 names,
and reports whether a request fits, which targets it would use and what it would
change. It writes nothing and reserves nothing. The contract is
``docs/workflow/specs/2026-10-02-lpar-plan-design.md``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlparse

from ...documents.lpar import (
    LparResources,
    PartitionType,
    reject_update_weight_on_create,
    shared_units_over_vcpus,
    validate_partition_type,
)
from ...documents.storage import STORAGE_KINDS, StorageKind, validate_virtual_disk
from ...errors import HMCError, HMCTransportError
from ...resource_identity import is_uuid, summary_matching
from ...ssh.affinity import MinimumAffinityPolicy, validate_minimum_affinity_policy
from ...ssh.lpar import validate_caller_token
from ...xmlutil import leaf_text
from ..affinity.rest import ProvisionAffinityAssessment, validate_affinity_request
from ..inventory.capacity import system_capacity
from ..storage.resources import (
    VolumeGroup,
    require_allowlisted_iso_url,
    storage_mapping,
    volume_group,
)
from ..systems.fleet import system_entry
from .assignments import LparPcieAssignments
from .provision import check_vlan_exists

InstallProfile = Literal["ubuntu-26.04.1", "rocky-9.8"]
INSTALL_PROFILES: tuple[str, ...] = ("ubuntu-26.04.1", "rocky-9.8")
Boot = Literal["immediate", "deferred"]

MAX_SELECTORS = 16
MAX_ROUTES = 16
MAX_DNS = 3
MAX_KEYS = 16
MAX_KEY_LENGTH = 8192
MAX_PRODUCER_RESULT_BYTES = 64 * 1024
_DEFAULT_ROUTE = ipaddress.IPv4Network("0.0.0.0/0")
_MAC = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}")
_LOGIN_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


@dataclass(frozen=True)
class PlanAdapters:
    """The client virtual Ethernet adapter provisioning would add."""

    port_vlan_id: int = field(
        metadata={"description": "VLAN identifier, 1 to 4094, for the adapter."}
    )
    mac: str | None = field(
        default=None,
        metadata={
            "description": "Adapter MAC in lower-case colon form; prepared media only."
        },
    )


@dataclass(frozen=True)
class PlanStorage:
    """The VIOS-backed storage provisioning would map."""

    storage_name: str = field(
        metadata={"description": "Virtual-disk or physical-volume name to map."}
    )
    kind: StorageKind = field(
        default="VirtualDisk", metadata={"description": "Storage resource kind."}
    )
    vios_uuid: str | None = field(
        default=None,
        metadata={"description": "VIOS UUID to use; omit to let the plan choose."},
    )
    vg_uuid: str | None = field(
        default=None,
        metadata={"description": "Volume-group UUID to use; requires vios_uuid."},
    )
    capacity_mib: int | None = field(
        default=None,
        metadata={
            "description": "Create a new virtual disk of this size; omit to map "
            "existing storage."
        },
    )


@dataclass(frozen=True)
class InstallRoute:
    """One IPv4 route the installed guest gets."""

    destination: str = field(
        metadata={"description": "IPv4 network in CIDR form; 0.0.0.0/0 is default."}
    )
    gateway: str = field(
        metadata={"description": "IPv4 gateway inside the address's network."}
    )


@dataclass(frozen=True)
class InstallNetwork:
    """The installed guest's static IPv4 configuration."""

    address: str = field(
        metadata={"description": "IPv4 interface address in CIDR form."}
    )
    routes: tuple[InstallRoute, ...] = field(
        metadata={"description": "1 to 16 routes, exactly one of them default."}
    )
    dns: tuple[str, ...] = field(
        default=(), metadata={"description": "0 to 3 IPv4 DNS servers."}
    )


@dataclass(frozen=True)
class InstallMedia:
    """Where the installer media comes from (ADR 0191)."""

    mode: Literal["built", "prepared"] = field(
        metadata={"description": "built by hmcpctl, or prepared by a producer."}
    )
    url: str | None = field(
        default=None,
        metadata={"description": "Prepared media's http(s) URL; prepared only."},
    )
    producer_result: dict[str, Any] | None = field(
        default=None,
        metadata={
            "description": "The producer's JSON result, at most 64 KiB; prepared only."
        },
    )


@dataclass(frozen=True)
class LparInstall:
    """The operating-system installation provisioning would start (ADR 0194)."""

    profile: InstallProfile = field(metadata={"description": "Install profile."})
    network: InstallNetwork = field(
        metadata={"description": "Static network configuration."}
    )
    media: InstallMedia = field(metadata={"description": "Installer media."})
    ssh_authorized_keys: tuple[str, ...] = field(
        default=(),
        metadata={"description": "1 to 16 single-line keys; built media only."},
    )
    login_user: str | None = field(
        default=None, metadata={"description": "Guest login user; built media only."}
    )


@dataclass(frozen=True)
class Placement:
    """Choose the system: from these selectors, or from the whole connection."""

    systems: tuple[str, ...] | None = field(
        default=None,
        metadata={
            "description": "1 to 16 system names or UUIDs; omit to enumerate up to 16."
        },
    )


@dataclass(frozen=True)
class PlanRequest:
    """Provisioning's inputs, less what only an execution has (spec, *Inputs*)."""

    name: str
    adapters: PlanAdapters
    storage: PlanStorage
    resources: LparResources
    partition_type: PartitionType = "AIX/Linux"
    system_name_or_uuid: str | None = None
    placement: Placement | None = None
    assignments: LparPcieAssignments = LparPcieAssignments()
    caller_token: str | None = None
    minimum_affinity_policy: MinimumAffinityPolicy | None = None
    affinity_assessment: ProvisionAffinityAssessment | None = None
    power_on: bool | None = None
    boot: Boot = "immediate"
    install: LparInstall | None = None
    exclusive_writer_window: bool = False


def _blank(value: str | None) -> str | None:
    # ADR 0094: a client may send an unset optional string as "".
    return None if value is None or not value.strip() else value


def _normalized(request: PlanRequest) -> PlanRequest:
    return dataclasses.replace(
        request,
        system_name_or_uuid=_blank(request.system_name_or_uuid),
        adapters=dataclasses.replace(
            request.adapters, mac=_blank(request.adapters.mac)
        ),
        storage=dataclasses.replace(
            request.storage,
            vios_uuid=_blank(request.storage.vios_uuid),
            vg_uuid=_blank(request.storage.vg_uuid),
        ),
    )


def _check_selectors(request: PlanRequest) -> None:
    if (request.system_name_or_uuid is None) == (request.placement is None):
        raise ValueError("pass exactly one of system_name_or_uuid and placement")
    systems = request.placement.systems if request.placement else None
    if systems is not None and not (
        1 <= len(systems) <= MAX_SELECTORS
        and all(isinstance(value, str) and value.strip() for value in systems)
    ):
        raise ValueError(
            f"placement.systems: pass 1 to {MAX_SELECTORS} non-empty names or "
            "UUIDs, or omit it to enumerate the connection's systems"
        )


def _check_storage(storage: PlanStorage) -> None:
    if not storage.storage_name:
        raise ValueError("storage.storage_name: must not be empty")
    if storage.kind not in STORAGE_KINDS:
        raise ValueError(f"storage.kind: must be one of {sorted(STORAGE_KINDS)}")
    for label, value in (
        ("vios_uuid", storage.vios_uuid),
        ("vg_uuid", storage.vg_uuid),
    ):
        if value is not None and not is_uuid(value):
            raise ValueError(f"storage.{label}: must be a UUID")
    if storage.vg_uuid is not None and storage.vios_uuid is None:
        raise ValueError(
            "storage.vg_uuid: requires storage.vios_uuid, because a volume group "
            "belongs to one VIOS"
        )
    if storage.capacity_mib is not None:
        _check_new_disk(storage, storage.capacity_mib)


def _check_new_disk(storage: PlanStorage, capacity_mib: int) -> None:
    if storage.kind != "VirtualDisk":
        raise ValueError(
            "storage.capacity_mib: creates a VirtualDisk; kind must be VirtualDisk"
        )
    try:
        validate_virtual_disk(storage.storage_name, capacity_mib)
    except ValueError as exc:
        raise ValueError(f"storage.capacity_mib: {exc}") from None


def _check_mac(request: PlanRequest) -> None:
    mac = request.adapters.mac
    prepared = request.install is not None and request.install.media.mode == "prepared"
    if mac is None:
        if prepared:
            raise ValueError("adapters.mac: prepared media requires the adapter MAC")
        return
    if not prepared:
        raise ValueError("adapters.mac: accepted only with prepared install media")
    if not _MAC.fullmatch(mac) or int(mac[:2], 16) & 1:
        raise ValueError(
            "adapters.mac: must be lower-case colon form, such as "
            "02:00:00:00:00:01, with no group bit"
        )


def _check_affinity(request: PlanRequest) -> None:
    policy = request.minimum_affinity_policy
    if policy is not None:
        validate_minimum_affinity_policy(policy)
    assessment = request.affinity_assessment
    if assessment is None:
        return
    if (
        request.system_name_or_uuid is None
        or assessment.system_name_or_uuid != request.system_name_or_uuid
        or assessment.lpar_name != request.name
    ):
        raise ValueError(
            "affinity_assessment: its system and LPAR identities must equal "
            "system_name_or_uuid and name, so it requires system_name_or_uuid"
        )
    validate_affinity_request(
        assessment, policy.min_affinity_score if policy is not None else None
    )


def _ipv4_address(text: str, label: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(text)
    except ValueError:
        raise ValueError(f"{label}: must be an IPv4 address") from None


def _check_network(network: InstallNetwork) -> None:
    label = "install.network"
    try:
        interface = ipaddress.IPv4Interface(network.address)
    except ValueError:
        interface = None
    if (
        interface is None
        or "/" not in network.address
        or not 1 <= interface.network.prefixlen <= 32
    ):
        raise ValueError(
            f"{label}.address: must be an IPv4 interface in CIDR form, prefix 1 to 32"
        )
    _check_routes(network, interface.network)
    if len(network.dns) > MAX_DNS:
        raise ValueError(f"{label}.dns: pass at most {MAX_DNS} servers")
    for index, server in enumerate(network.dns):
        _ipv4_address(server, f"{label}.dns[{index}]")


def _check_routes(network: InstallNetwork, subnet: ipaddress.IPv4Network) -> None:
    label = "install.network"
    if not 1 <= len(network.routes) <= MAX_ROUTES:
        raise ValueError(f"{label}.routes: pass 1 to {MAX_ROUTES} routes")
    defaults = 0
    for index, route in enumerate(network.routes):
        try:
            destination = ipaddress.IPv4Network(route.destination, strict=True)
        except ValueError:
            raise ValueError(
                f"{label}.routes[{index}].destination: must be an IPv4 network in "
                "CIDR form"
            ) from None
        defaults += destination == _DEFAULT_ROUTE
        gateway = _ipv4_address(route.gateway, f"{label}.routes[{index}].gateway")
        if gateway not in subnet:
            raise ValueError(
                f"{label}.routes[{index}].gateway: must be inside "
                f"{network.address}'s network"
            )
    if defaults != 1:
        raise ValueError(f"{label}.routes: exactly one route must be 0.0.0.0/0")


def _check_login(install: LparInstall) -> None:
    keys = install.ssh_authorized_keys
    if not 1 <= len(keys) <= MAX_KEYS:
        raise ValueError(
            f"install.ssh_authorized_keys: built media needs 1 to {MAX_KEYS} keys"
        )
    for index, key in enumerate(keys):
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= MAX_KEY_LENGTH
            # Rejects every line break (U+2028 and NEL included) and control character.
            or not key.isprintable()
        ):
            raise ValueError(
                f"install.ssh_authorized_keys[{index}]: must be one line of 1 to "
                f"{MAX_KEY_LENGTH} characters"
            )
    if install.login_user is None or not _LOGIN_USER.fullmatch(install.login_user):
        raise ValueError(
            "install.login_user: built media needs 1 to 32 characters matching "
            "[a-z_][a-z0-9_-]*"
        )


def _check_prepared(install: LparInstall) -> None:
    if install.ssh_authorized_keys:
        raise ValueError(
            "install.ssh_authorized_keys: prepared media carries its own keys; omit it"
        )
    if install.login_user is not None:
        raise ValueError(
            "install.login_user: prepared media carries its own user; omit it"
        )
    media = install.media
    try:
        scheme = urlparse(media.url).scheme if media.url is not None else None
    except ValueError:  # its message quotes the URL's host
        scheme = None
    if scheme not in ("http", "https"):
        raise ValueError("install.media.url: prepared media needs an http or https URL")
    result = media.producer_result
    if result is None:
        raise ValueError(
            "install.media.producer_result: prepared media needs the producer's "
            "JSON object"
        )
    size = len(json.dumps(result, separators=(",", ":")).encode())
    if size > MAX_PRODUCER_RESULT_BYTES:
        raise ValueError(
            f"install.media.producer_result: {size} bytes exceeds "
            f"{MAX_PRODUCER_RESULT_BYTES}"
        )


def _check_install(request: PlanRequest) -> None:
    install = request.install
    if install is None:
        if request.boot != "immediate":
            raise ValueError("boot: deferred applies only with install")
        return
    if request.storage.capacity_mib is None:
        raise ValueError(
            "storage.capacity_mib: install needs a new disk (ADR 0191 Decision 6)"
        )
    if request.power_on:
        raise ValueError("power_on: with install, boot governs power-on; omit power_on")
    if install.profile not in INSTALL_PROFILES:
        raise ValueError(f"install.profile: must be one of {list(INSTALL_PROFILES)}")
    _check_network(install.network)
    _check_media(install)


def _check_media(install: LparInstall) -> None:
    if install.media.mode == "built":
        if install.media.url is not None or install.media.producer_result is not None:
            raise ValueError(
                "install.media.url: built media takes no url or producer_result"
            )
        _check_login(install)
    elif install.media.mode == "prepared":
        _check_prepared(install)
    else:
        raise ValueError("install.media.mode: must be built or prepared")


def check_request(request: PlanRequest) -> PlanRequest:
    """Return *request* with blank selectors as ``None``; raise naming the first bad input.

    Messages never echo a key, URL or producer-result content.
    """
    reject_update_weight_on_create(request.resources)
    request = _normalized(request)
    if not request.name:
        raise ValueError("name: must not be empty")
    _check_selectors(request)
    validate_partition_type(request.partition_type)
    if request.resources.dedicated is not True and (
        refusal := shared_units_over_vcpus(request.resources)
    ):
        raise ValueError(f"resources: {refusal}")
    if not 1 <= request.adapters.port_vlan_id <= 4094:
        raise ValueError("adapters.port_vlan_id: must be 1 to 4094")
    _check_storage(request.storage)
    if request.caller_token is not None:
        validate_caller_token(request.caller_token)
    _check_affinity(request)
    if request.boot not in ("immediate", "deferred"):
        raise ValueError("boot: must be immediate or deferred")
    _check_install(request)
    _check_mac(request)
    return request


DIGEST_FORMAT = "hmc-lpar-plan-v1"


@dataclass(frozen=True)
class PlanSystem:
    """The managed system a plan targets. ``id`` is ``<connection>/<uuid>``."""

    id: str
    uuid: str
    name: str | None


@dataclass(frozen=True)
class PlanResource:
    """A VIOS or volume group a plan targets."""

    uuid: str
    name: str | None


@dataclass(frozen=True)
class PlanTargets:
    """The resolved targets: the system, and the VIOS and volume group when known."""

    system: PlanSystem
    vios: PlanResource | None
    volume_group: PlanResource | None


def _canonical(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"plan_digest cannot encode {type(value).__name__}")


def plan_digest(request: PlanRequest, targets: PlanTargets, connection: str) -> str:
    """SHA-256 over the canonical request and resolved targets (#1225 recomputes it)."""
    system = targets.system.uuid.lower()
    vios = targets.vios.uuid.lower() if targets.vios else None
    group = targets.volume_group.uuid.lower() if targets.volume_group else None
    body = asdict(_normalized(request))
    del body["placement"]
    body["system_name_or_uuid"] = system
    body["storage"]["vios_uuid"] = vios
    body["storage"]["vg_uuid"] = group
    # Fields whose meaning depends on another are hashed as what they mean, so two
    # requests that plan the same provisioning share one digest.
    if request.install is None:
        body["power_on"] = request.power_on is not False
        body["exclusive_writer_window"] = False
    else:
        body["power_on"] = None
    if body["affinity_assessment"] is not None:
        body["affinity_assessment"]["system_name_or_uuid"] = system
    document = {
        "format": DIGEST_FORMAT,
        "connection": connection,
        "request": body,
        "targets": {"system": system, "vios": vios, "volume_group": group},
    }
    text = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_canonical,
    )
    return hashlib.sha256(text.encode()).hexdigest()


SYSTEMS_TOOL = "hmc_list_systems"
PARTITIONS_TOOL = "hmc_list_lpars"
CAPACITY_TOOL = "hmc_capacity_report"
NETWORKS_TOOL = "hmc_list_virtual_networks"
VIOS_TOOL = "hmc_list_vios"
VOLUME_GROUPS_TOOL = "hmc_list_volume_groups"
STORAGE_DETAIL_TOOL = "hmc_get_vios_storage_detail"
PLAN_TOOLS = (
    SYSTEMS_TOOL,
    PARTITIONS_TOOL,
    CAPACITY_TOOL,
    NETWORKS_TOOL,
    VIOS_TOOL,
    VOLUME_GROUPS_TOOL,
    STORAGE_DETAIL_TOOL,
)
"""ADR 0198's row: the tools whose data planning reads."""

CANDIDATES_LIMIT = 16
MAX_BLOCKERS = 16
MAX_AMBIGUOUS_PAIRS = 5
_MAX_DETAIL = 500

PlanAdmit = Callable[[str, str | None, str | None], str | None]
"""Asks whether a delegated tool may run, as ``(tool, system, vios)``.

``system`` is the caller's selector text when the caller named the system and the
UUID when placement enumerated it; ``vios`` is a VIOS UUID; both are ``None`` for a
console tool. Returns ``None`` when admitted, otherwise the denial text.
"""

BlockerCode = Literal[
    "denied",
    "unavailable",
    "system_not_operating",
    "insufficient_memory",
    "insufficient_processors",
    "name_exists",
    "vlan_missing",
    "vios_not_running",
    "storage_unplaceable",
    "ambiguous",
    "storage_mapped",
    "exclusive_writer_window_required",
    "url_not_allowlisted",
    "no_candidate",
]
ChangeKind = Literal[
    "create_partition",
    "stamp_ownership",
    "set_minimum_affinity_policy",
    "add_network_adapter",
    "add_vscsi_adapter",
    "create_virtual_disk",
    "map_storage",
    "assign_pcie",
    "write_profile",
    "power_on",
    "assess_affinity",
    "bind_media",
    "upload_media",
    "mount_media",
    "set_boot_order",
]


@dataclass(frozen=True)
class PlanBlocker:
    """Why a candidate or the request cannot be provisioned as asked."""

    code: BlockerCode
    check: str
    target: str | None
    tool: str | None
    detail: str


@dataclass(frozen=True)
class PlanCandidate:
    """One evaluated system; ``targets`` is ``None`` when its selector did not resolve."""

    targets: PlanTargets | None
    selector: str | None
    free_memory_mib: int | None
    free_proc_units: float | None
    blockers: list[PlanBlocker]
    blockers_truncated: bool


@dataclass(frozen=True)
class IntendedChange:
    """One change provisioning would make on the selected targets."""

    order: int
    kind: ChangeKind
    target: str
    detail: str


@dataclass(frozen=True)
class LparPlan:
    """What provisioning this request would need, use and change. Nothing is reserved.

    ``plan_digest`` is set only when a candidate is selected and the request has no
    blocker of its own.
    """

    connection: str
    plan_digest: str | None
    selected: PlanTargets | None
    blockers: list[PlanBlocker]
    candidates: list[PlanCandidate]
    candidates_limit: int
    candidates_truncated: bool
    intended_changes: list[IntendedChange]
    unverified: list[str]


def required_tools(request: PlanRequest) -> tuple[str, ...]:
    """The row tools *request* needs: no enumeration, no storage detail for a new disk."""
    enumerates = request.placement is not None and request.placement.systems is None
    return tuple(
        tool
        for tool in PLAN_TOOLS
        if (tool != SYSTEMS_TOOL or enumerates)
        and (tool != STORAGE_DETAIL_TOOL or request.storage.capacity_mib is None)
    )


def _blocker(
    code: BlockerCode,
    check: str,
    detail: str,
    *,
    target: str | None = None,
    tool: str | None = None,
) -> PlanBlocker:
    return PlanBlocker(code, check, target, tool, detail[:_MAX_DETAIL])


def _text(value: object) -> str | None:
    text = leaf_text(value)
    return text if isinstance(text, str) else None


def _items(value: object, key: str) -> list[Mapping[str, Any]]:
    """``element_to_dict`` gives one child as a dict and several as a list."""
    found = value.get(key) if isinstance(value, Mapping) else None
    items = found if isinstance(found, list) else [found]
    return [item for item in items if isinstance(item, Mapping)]


def _disk_names(entry: Mapping[str, Any]) -> set[str]:
    resource = entry.get("Resource") or {}
    disks = _items(resource.get("VirtualDisks"), "VirtualDisk")
    return {name for disk in disks if (name := _text(disk.get("DiskName")))}


@dataclass(frozen=True)
class _Candidate:
    selector: str | None
    uuid: str
    entry: dict[str, Any]


@dataclass(frozen=True)
class _Pair:
    vios: PlanResource
    group: PlanResource | None


def _ambiguous(pairs: list[_Pair]) -> PlanBlocker:
    shown = ", ".join(
        f"{pair.vios.uuid}/{pair.group.uuid if pair.group else '-'}"
        for pair in pairs[:MAX_AMBIGUOUS_PAIRS]
    )
    more = len(pairs) - MAX_AMBIGUOUS_PAIRS
    text = (
        f"{len(pairs)} VIOS/volume-group pairs qualify; name one with "
        f"storage.vios_uuid and storage.vg_uuid: {shown}"
        + (f", and {more} more" if more > 0 else "")
    )
    return _blocker("ambiguous", "storage", text)


class _Stalled(Exception):
    """The HMC stopped answering; later reads are not attempted."""


_FAILED = object()


@dataclass
class _Evaluator:
    """Runs the spec's checks on one candidate at a time, sharing the capacity grant."""

    hmc: Any
    admit: PlanAdmit
    request: PlanRequest
    connection: str
    enumerated: bool
    stalled: str | None = None
    capacity_asked: bool = False
    capacity_denial: str | None = None

    def _admitted(
        self,
        tool: str,
        system: str | None,
        vios: str | None,
        blockers: list[PlanBlocker],
        check: str,
    ) -> bool:
        denial = self.admit(tool, system, vios)
        if denial is not None:
            target = vios or system
            blockers.append(_blocker("denied", check, denial, target=target, tool=tool))
        return denial is None

    async def _read(
        self,
        tool: str,
        target: str,
        blockers: list[PlanBlocker],
        check: str,
        call: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Await *call*; an HMC failure becomes an ``unavailable`` blocker."""
        try:
            return await call()
        except HMCTransportError as exc:
            self.stalled = f"the HMC stopped answering: {exc}"
            blockers.append(
                _blocker("unavailable", check, self.stalled, target=target, tool=tool)
            )
            raise _Stalled from exc
        except HMCError as exc:
            text = f"{tool} failed for {target}: {exc}"
            blockers.append(
                _blocker("unavailable", check, text, target=target, tool=tool)
            )
            return _FAILED

    def _capacity(
        self, candidate: _Candidate, blockers: list[PlanBlocker]
    ) -> tuple[int | None, float | None]:
        if not self.capacity_asked:
            self.capacity_denial = self.admit(CAPACITY_TOOL, None, None)
            self.capacity_asked = True
        if self.capacity_denial is not None:
            blockers.append(
                _blocker("denied", "capacity", self.capacity_denial, tool=CAPACITY_TOOL)
            )
            return None, None
        try:
            figures = system_capacity(candidate.entry)
        except ValueError as exc:
            blockers.append(
                _blocker(
                    "unavailable",
                    "capacity",
                    str(exc),
                    target=candidate.uuid,
                    tool=CAPACITY_TOOL,
                )
            )
            return None, None
        resources = self.request.resources
        if (
            resources.desired_memory is not None
            and resources.desired_memory > figures.free_memory_mib
        ):
            text = (
                f"desired_memory {resources.desired_memory} MiB exceeds "
                f"{figures.free_memory_mib} MiB free"
            )
            blockers.append(_blocker("insufficient_memory", "capacity", text))
        if (
            resources.desired_procs is not None
            and resources.desired_procs > figures.free_proc_units
        ):
            text = (
                f"desired_procs {resources.desired_procs} exceeds "
                f"{figures.free_proc_units} free processor units"
            )
            blockers.append(_blocker("insufficient_processors", "capacity", text))
        return figures.free_memory_mib, figures.free_proc_units

    async def _check_name_available(
        self, candidate: _Candidate, blockers: list[PlanBlocker]
    ) -> None:
        if self.enumerated and not self._admitted(
            PARTITIONS_TOOL, candidate.uuid, None, blockers, "name"
        ):
            return
        entries = await self._read(
            PARTITIONS_TOOL,
            candidate.uuid,
            blockers,
            "name",
            lambda: self.hmc.list_logical_partitions(candidate.uuid),
        )
        if entries is _FAILED:
            return
        names = {
            _text((entry.get("Resource") or {}).get("PartitionName"))
            for entry in entries
        }
        if self.request.name in names:
            text = f"a partition named {self.request.name!r} exists on this system"
            blockers.append(
                _blocker("name_exists", "name", text, target=candidate.uuid)
            )

    async def _check_vlan_exists(
        self, candidate: _Candidate, spelling: str, blockers: list[PlanBlocker]
    ) -> None:
        if not self._admitted(NETWORKS_TOOL, spelling, None, blockers, "vlan"):
            return

        async def missing() -> str | None:
            try:
                await check_vlan_exists(
                    self.hmc, candidate.uuid, self.request.adapters.port_vlan_id
                )
            except ValueError as exc:
                return str(exc)
            return None

        found = await self._read(
            NETWORKS_TOOL, candidate.uuid, blockers, "vlan", missing
        )
        if isinstance(found, str):
            blockers.append(
                _blocker("vlan_missing", "vlan", found, target=candidate.uuid)
            )

    async def _vioses(
        self, candidate: _Candidate, spelling: str, blockers: list[PlanBlocker]
    ) -> list[PlanResource] | None:
        """The candidate VIOSes: the named one, or every running one."""
        if not self._admitted(VIOS_TOOL, spelling, None, blockers, "storage"):
            return None
        entries = await self._read(
            VIOS_TOOL,
            candidate.uuid,
            blockers,
            "storage",
            lambda: self.hmc.list_vios(candidate.uuid),
        )
        if entries is _FAILED:
            return None
        running = [
            PlanResource(str(entry["UUID"]), _text(resource.get("PartitionName")))
            for entry in entries
            if entry.get("UUID")
            and _text((resource := entry.get("Resource") or {}).get("PartitionState"))
            == "running"
        ]
        named = self.request.storage.vios_uuid
        if named is not None:
            running = [vios for vios in running if vios.uuid.lower() == named.lower()]
        if not running:
            text = (
                f"VIOS {named} is not running on this system"
                if named is not None
                else "no VIOS on this system is running"
            )
            blockers.append(
                _blocker("vios_not_running", "storage", text, target=candidate.uuid)
            )
            return None
        return running

    async def _groups(
        self, vios: PlanResource, spelling: str, blockers: list[PlanBlocker]
    ) -> list[tuple[VolumeGroup, set[str]]] | None:
        if not self._admitted(
            VOLUME_GROUPS_TOOL, spelling, vios.uuid, blockers, "storage"
        ):
            return None
        entries = await self._read(
            VOLUME_GROUPS_TOOL,
            vios.uuid,
            blockers,
            "storage",
            lambda: self.hmc.list_volume_groups(vios.uuid),
        )
        if entries is _FAILED:
            return None
        try:
            return [(volume_group(entry), _disk_names(entry)) for entry in entries]
        except HMCError as exc:
            text = f"{VOLUME_GROUPS_TOOL} returned an unreadable group: {exc}"
            blockers.append(
                _blocker(
                    "unavailable",
                    "storage",
                    text,
                    target=vios.uuid,
                    tool=VOLUME_GROUPS_TOOL,
                )
            )
            return None

    def _qualifying(
        self,
        vios: PlanResource,
        groups: list[tuple[VolumeGroup, set[str]]],
        reasons: list[str],
    ) -> list[_Pair]:
        storage = self.request.storage
        label = vios.name or vios.uuid
        considered = [
            (group, names)
            for group, names in groups
            if storage.vg_uuid is None or group.uuid.lower() == storage.vg_uuid.lower()
        ]
        if storage.capacity_mib is None:
            pairs = [
                _Pair(vios, PlanResource(group.uuid, group.name))
                for group, names in considered
                if storage.storage_name in names
            ]
            if not pairs:
                reasons.append(
                    f"{label}: no volume group holds {storage.storage_name!r}"
                )
            return pairs
        if any(storage.storage_name in names for _, names in groups):
            reasons.append(
                f"{label}: a virtual disk named {storage.storage_name!r} exists"
            )
            return []
        pairs = []
        for group, _ in considered:
            free = group.free_space_gib
            if free is None or group.free_space_diagnostic is not None:
                reasons.append(f"{label}/{group.name}: FreeSpace is unreadable")
            elif free * 1024 < storage.capacity_mib:
                reasons.append(f"{label}/{group.name}: {free} GiB free")
            else:
                pairs.append(_Pair(vios, PlanResource(group.uuid, group.name)))
        return pairs

    async def _pairs(
        self, vioses: list[PlanResource], spelling: str, blockers: list[PlanBlocker]
    ) -> tuple[list[_Pair], list[str]] | None:
        """Every qualifying pair and the reasons others did not; ``None`` if one VIOS
        went unread, since an unread VIOS could make the choice ambiguous."""
        pairs: list[_Pair] = []
        reasons: list[str] = []
        complete = True
        for vios in vioses:
            groups = await self._groups(vios, spelling, blockers)
            if groups is None:
                complete = False
                continue
            pairs.extend(self._qualifying(vios, groups, reasons))
        return (pairs, reasons) if complete else None

    async def _resolve_storage_placement(
        self, candidate: _Candidate, spelling: str, blockers: list[PlanBlocker]
    ) -> _Pair | None:
        """Resolve exactly one (VIOS, volume group) pair, or add the blocker."""
        vioses = await self._vioses(candidate, spelling, blockers)
        if vioses is None:
            return None
        storage = self.request.storage
        if storage.kind == "PhysicalVolume":
            if storage.vios_uuid is None:
                text = "existing PhysicalVolume storage needs storage.vios_uuid"
                blockers.append(_blocker("storage_unplaceable", "storage", text))
                return None
            return _Pair(vioses[0], None)
        found = await self._pairs(vioses, spelling, blockers)
        if found is None:
            return None
        pairs, reasons = found
        if not pairs:
            text = "; ".join(reasons) or "no volume group qualifies"
            blockers.append(_blocker("storage_unplaceable", "storage", text))
            return None
        if len(pairs) > 1:
            blockers.append(_ambiguous(pairs))
            return None
        return pairs[0]

    async def _check_storage_unmapped(
        self, pair: _Pair, spelling: str, blockers: list[PlanBlocker]
    ) -> None:
        vios = pair.vios.uuid
        if not self._admitted(STORAGE_DETAIL_TOOL, spelling, vios, blockers, "mapping"):
            return
        detail = await self._read(
            STORAGE_DETAIL_TOOL,
            vios,
            blockers,
            "mapping",
            lambda: self.hmc.get_vios_storage_detail(vios),
        )
        if detail is _FAILED:
            return
        resource = (detail or {}).get("Resource") or {}
        storage = self.request.storage
        for entry in _items(resource.get("VirtualSCSIMappings"), "VirtualSCSIMapping"):
            mapping = storage_mapping(entry)
            if (mapping.backing_kind, mapping.backing_name) == (
                storage.kind,
                storage.storage_name,
            ):
                text = f"{storage.storage_name!r} is already mapped on VIOS {vios}"
                blockers.append(
                    _blocker("storage_mapped", "mapping", text, target=vios)
                )
                return

    async def evaluate(self, candidate: _Candidate) -> PlanCandidate:
        resource = candidate.entry.get("Resource") or {}
        system = PlanSystem(
            f"{self.connection}/{candidate.uuid}",
            candidate.uuid,
            _text(resource.get("SystemName")),
        )
        blockers: list[PlanBlocker] = []
        free: tuple[int | None, float | None] = (None, None)
        pair: _Pair | None = None
        state = _text(resource.get("State"))
        if self.stalled is not None:
            blockers.append(
                _blocker("unavailable", "system", self.stalled, target=system.uuid)
            )
        elif state != "operating":
            text = f"system state is {state!r}, not 'operating'"
            blockers.append(
                _blocker("system_not_operating", "system", text, target=system.uuid)
            )
        else:
            free = self._capacity(candidate, blockers)
            pair = await self._checks(candidate, blockers)
        targets = PlanTargets(
            system, pair.vios if pair else None, pair.group if pair else None
        )
        return PlanCandidate(
            targets=targets,
            selector=candidate.selector,
            free_memory_mib=free[0],
            free_proc_units=free[1],
            blockers=blockers[:MAX_BLOCKERS],
            blockers_truncated=len(blockers) > MAX_BLOCKERS,
        )

    async def _checks(
        self, candidate: _Candidate, blockers: list[PlanBlocker]
    ) -> _Pair | None:
        spelling = candidate.selector or candidate.uuid
        try:
            await self._check_name_available(candidate, blockers)
            await self._check_vlan_exists(candidate, spelling, blockers)
            pair = await self._resolve_storage_placement(candidate, spelling, blockers)
            if pair is not None and self.request.storage.capacity_mib is None:
                await self._check_storage_unmapped(pair, spelling, blockers)
        except _Stalled:
            return None
        return pair


def _unresolved(selector: str, blocker: PlanBlocker) -> PlanCandidate:
    return PlanCandidate(None, selector, None, None, [blocker], False)


async def _lookup(
    hmc: Any, selector: str, stalled: str | None
) -> tuple[dict[str, Any] | None, str, str | None]:
    """Read one selector's system entry; the third value is a transport stall.

    The selector is matched against ``ManagedSystem/quick/All``; a system that
    is not operating gets its sparse entry (#1501).
    """
    try:
        if stalled is not None:
            raise HMCError(stalled)
        summary = summary_matching(await hmc.list_system_summaries(), selector)
        entry = await system_entry(hmc, summary) if summary is not None else None
    except (HMCError, ValueError) as exc:
        # summary_matching raises ValueError when several systems share a name.
        if isinstance(exc, HMCTransportError):
            stalled = f"the HMC stopped answering: {exc}"
        return None, f"managed system {selector!r} is unavailable: {exc}", stalled
    return entry, f"no managed system matches {selector!r}", stalled


async def _resolve(
    hmc: Any, admit: PlanAdmit, selectors: Sequence[str]
) -> tuple[list[_Candidate], list[PlanCandidate], str | None]:
    """Admit each distinct selector as ``hmc_list_lpars``, then resolve it (ADR 0196)."""
    candidates: list[_Candidate] = []
    unresolved: list[PlanCandidate] = []
    stalled: str | None = None
    for selector in dict.fromkeys(selectors):
        denial = admit(PARTITIONS_TOOL, selector, None)
        if denial is not None:
            blocker = _blocker(
                "denied", "system", denial, target=selector, tool=PARTITIONS_TOOL
            )
            unresolved.append(_unresolved(selector, blocker))
            continue
        entry, text, stalled = await _lookup(hmc, selector, stalled)
        if not entry or not entry.get("UUID"):
            blocker = _blocker(
                "unavailable", "system", text, target=selector, tool=PARTITIONS_TOOL
            )
            unresolved.append(_unresolved(selector, blocker))
            continue
        uuid = str(entry["UUID"])
        if all(candidate.uuid.lower() != uuid.lower() for candidate in candidates):
            candidates.append(_Candidate(selector, uuid, entry))
    return candidates, unresolved, stalled


async def _enumerate(
    hmc: Any, admit: PlanAdmit
) -> tuple[list[_Candidate], PlanBlocker | None, bool]:
    denial = admit(SYSTEMS_TOOL, None, None)
    if denial is not None:
        return [], _blocker("denied", "system", denial, tool=SYSTEMS_TOOL), False
    # quick/All, then each kept candidate's entry: the ManagedSystem feed times
    # out while a system has no connection (#1501).
    try:
        summaries = sorted(
            (
                summary
                for summary in await hmc.list_system_summaries()
                if isinstance(summary, dict)
                and isinstance(summary.get("UUID"), str)
                and summary["UUID"]
            ),
            key=lambda summary: summary["UUID"],
        )
        candidates = [
            _Candidate(None, summary["UUID"], await system_entry(hmc, summary))
            for summary in summaries[:CANDIDATES_LIMIT]
        ]
    except HMCError as exc:
        text = f"managed systems are unavailable: {exc}"
        return [], _blocker("unavailable", "system", text, tool=SYSTEMS_TOOL), False
    return candidates, None, len(summaries) > CANDIDATES_LIMIT


def _request_blockers(hmc: Any, request: PlanRequest) -> list[PlanBlocker]:
    blockers: list[PlanBlocker] = []
    install = request.install
    if install is not None and not request.exclusive_writer_window:
        blockers.append(
            _blocker(
                "exclusive_writer_window_required",
                "writer_window",
                "install shares VIOS state; pass exclusive_writer_window=true once no "
                "other writer changes these VIOSes",
            )
        )
    if install is not None and install.media.url is not None:
        try:
            require_allowlisted_iso_url(
                install.media.url, hmc.config.iso_url_allowlist_entries
            )
        except (ValueError, HMCError):
            blockers.append(
                _blocker(
                    "url_not_allowlisted",
                    "prepared_url",
                    "the prepared media URL's host is not in HMC_ISO_URL_ALLOWLIST",
                )
            )
    return blockers


def _assignment_counts(assignments: LparPcieAssignments) -> dict[str, int]:
    return {
        item.name: len(getattr(assignments, item.name))
        for item in dataclasses.fields(assignments)
    }


def _intended_changes(
    request: PlanRequest, targets: PlanTargets
) -> list[IntendedChange]:
    system = targets.system.id
    vios = targets.vios.uuid if targets.vios else system
    group = targets.volume_group.uuid if targets.volume_group else vios
    storage = request.storage
    steps: list[tuple[ChangeKind, str, str]] = [
        ("create_partition", system, f"create {request.name!r}"),
        ("stamp_ownership", system, "stamp hmcpctl ownership in the description"),
    ]
    if request.minimum_affinity_policy is not None:
        steps.append(
            ("set_minimum_affinity_policy", system, "set the minimum-affinity policy")
        )
    steps.append(
        (
            "add_network_adapter",
            system,
            f"add a client adapter on VLAN {request.adapters.port_vlan_id}",
        )
    )
    steps.append(("add_vscsi_adapter", vios, "add a client/server vSCSI adapter pair"))
    if storage.capacity_mib is not None:
        steps.append(
            (
                "create_virtual_disk",
                group,
                f"create {storage.storage_name!r}, {storage.capacity_mib} MiB",
            )
        )
    steps.append(("map_storage", vios, f"map {storage.kind} {storage.storage_name!r}"))
    counts = _assignment_counts(request.assignments)
    if any(counts.values()):
        detail = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
        steps.append(("assign_pcie", system, f"apply {detail}, in order"))
    steps.append(
        ("write_profile", system, "write and read back the partition profile (#637)")
    )
    if request.install is not None:
        steps.extend(
            [
                ("bind_media", vios, f"bind {request.install.media.mode} media"),
                ("upload_media", vios, "upload the installer media"),
                ("mount_media", vios, "mount the installer media"),
                ("set_boot_order", system, "boot installer media first"),
                ("power_on", system, _power_detail(request.boot)),
            ]
        )
    elif request.power_on is not False:
        steps.append(("power_on", system, "power on the partition"))
    if request.affinity_assessment is not None:
        steps.append(("assess_affinity", system, "assess affinity after activation"))
    return [
        IntendedChange(order, kind, target, detail)
        for order, (kind, target, detail) in enumerate(steps, start=1)
    ]


def _power_detail(boot: Boot) -> str:
    if boot == "deferred":
        return "boot: deferred stops before this step; continuation boot runs it"
    return "power on to the installer"


_UNVERIFIED_INSTALL = (
    (
        "the native envelope: POWER9 or POWER10, HMC V10R3 M1060 or V11R2, and the "
        "VIOS level (ADR 0194)"
    ),
    "the media binding and producer result (#1227), and the built-media build entry",
    "that the VIOS copy matches the upload",
    "that the guest can reach the installer source",
    "boot_started, which no operation reports until #1230",
)


def _unverified(request: PlanRequest) -> list[str]:
    resources = request.resources
    statements = [
        "capacity is observed, not reserved, and may change before provisioning",
        (
            "that no partition on a non-candidate system of the connection has the "
            "name; provision refuses a name used on any system"
        ),
    ]
    if resources.desired_memory is None or resources.desired_procs is None:
        statements.append(
            "the HMC's default for an omitted desired memory or processor figure"
        )
    if request.storage.kind == "PhysicalVolume":
        statements.append("the physical volume's existence")
    if any(_assignment_counts(request.assignments).values()):
        statements.append(
            "PCIe, SR-IOV and vNIC prevalidation, which provision runs over SSH"
        )
    if request.minimum_affinity_policy is not None:
        statements.append(
            "the system's support for the minimum-affinity policy, which provision "
            "checks over SSH"
        )
    if request.install is not None:
        statements.extend(_UNVERIFIED_INSTALL)
    return statements


def _rank(candidate: PlanCandidate) -> tuple[Any, ...]:
    """``find_placement``'s key, tightest fit first, unknown capacity last."""
    targets = candidate.targets
    return (
        candidate.free_memory_mib is None,
        candidate.free_memory_mib or 0,
        candidate.free_proc_units or 0.0,
        (targets.system.name if targets else None) or "",
        targets.system.uuid if targets else "",
    )


async def plan_lpar(
    hmc: Any, *, connection: str, admit: PlanAdmit, request: PlanRequest
) -> LparPlan:
    """Plan *request* on one connection without writing or reserving anything."""
    request = check_request(request)
    blockers = _request_blockers(hmc, request)
    truncated = False
    stalled: str | None = None
    unresolved: list[PlanCandidate] = []
    placement = request.placement
    if placement is not None and placement.systems is None:
        found, refused, truncated = await _enumerate(hmc, admit)
        if refused is not None:
            blockers.insert(0, refused)
    else:
        selectors = (
            placement.systems
            if placement is not None and placement.systems is not None
            else (request.system_name_or_uuid or "",)
        )
        found, unresolved, stalled = await _resolve(hmc, admit, selectors)
    evaluator = _Evaluator(
        hmc,
        admit,
        request,
        connection,
        enumerated=placement is not None and placement.systems is None,
        stalled=stalled,
    )
    evaluated = [await evaluator.evaluate(candidate) for candidate in found]
    candidates = sorted(evaluated, key=_rank) + unresolved
    chosen = next((c for c in candidates if c.targets and not c.blockers), None)
    selected = chosen.targets if chosen else None
    if selected is None:
        blockers.append(
            _blocker("no_candidate", "placement", "no evaluated system is unblocked")
        )
    digest = (
        plan_digest(request, selected, connection)
        if selected is not None and not blockers
        else None
    )
    return LparPlan(
        connection=connection,
        plan_digest=digest,
        selected=selected,
        blockers=blockers,
        candidates=candidates,
        candidates_limit=CANDIDATES_LIMIT,
        candidates_truncated=truncated,
        intended_changes=_intended_changes(request, selected) if selected else [],
        unverified=_unverified(request),
    )
