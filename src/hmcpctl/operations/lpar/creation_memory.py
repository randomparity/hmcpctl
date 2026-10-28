"""Contained creation-memory admission and independent configured observations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from ...client.client_profiles import PartitionKind
from ...documents.common import UOM_NS
from ...documents.lpar import LparResources
from ...documents.lpar_creation_memory import LparCreationMemory
from ...errors import HMCError
from ...resource_identity import is_uuid
from ...xmlutil import ATOM_NS, leaf_text, parse_feed
from . import profiles
from .ownership import authorize_lpar_ownership_description
from .workflow_contract import WorkflowStep

if TYPE_CHECKING:
    from ...client.core import HMCClient
    from ...ssh.lpar import _CreationMemorySubmissionError

_LIMIT = 5 * 1024 * 1024
Environment = Literal["AIX/Linux", "OS400", "Virtual IO Server"]


@dataclass(frozen=True)
class PagingProvider:
    uuid: str
    name: str
    partition_id: int


@dataclass(frozen=True)
class CreationMemoryAdmission:
    memory: LparCreationMemory
    system_uuid: str
    system_name: str
    kind: PartitionKind
    environment: Environment
    name: str
    partition_id: int | None
    preexisting: frozenset[str]
    primary: tuple[PagingProvider, ...]
    secondary: tuple[PagingProvider, ...]


@dataclass(frozen=True)
class CreationMemoryObservation:
    status: Literal["verified", "mismatch", "unavailable"]
    observed: dict[str, object]
    unavailable: tuple[str, ...]


@dataclass(frozen=True)
class CreationMemoryVerification:
    requested: LparCreationMemory
    saved: CreationMemoryObservation | None
    current: CreationMemoryObservation | None


def _text(value: object) -> str:
    if isinstance(value, dict) and set(value) - {"text", "@attrs"}:
        raise ValueError("Creation memory scalar is malformed")
    value = leaf_text(value)
    if not isinstance(value, str):
        raise TypeError("Creation memory scalar is unavailable")
    return value


def _boolean(value: object) -> bool:
    text = _text(value)
    if text not in {"true", "false", "1", "0"}:
        raise ValueError("Creation memory boolean is unavailable")
    return text in {"true", "1"}


def _integer(value: object, minimum: int = 1) -> int:
    text = _text(value)
    if not text.isascii() or not text.isdecimal() or len(text) > 4096:
        raise ValueError("Creation memory integer is unavailable")
    number = int(text)
    if number < minimum:
        raise ValueError("Creation memory integer is outside its domain")
    return number


def _entries(body: object, kind: str, *, detail: bool = False) -> list[dict[str, Any]]:
    if not isinstance(body, str) or len(body) > _LIMIT or len(body.encode()) > _LIMIT:
        raise ValueError("Creation memory evidence exceeds 5 MiB or is malformed")
    try:
        root = DET.fromstring(body)
    except (DET.ParseError, DefusedXmlException):
        raise ValueError("Creation memory XML evidence is malformed") from None
    if root.tag == f"{{{ATOM_NS}}}feed":
        elements = [e for e in root if e.tag == f"{{{ATOM_NS}}}entry"]
        if any(e.tag.rsplit("}", 1)[-1] == "entry" and e not in elements for e in root):
            raise ValueError("Creation memory feed has foreign entries")
    elif detail and root.tag == f"{{{ATOM_NS}}}entry":
        elements = [root]
    else:
        raise ValueError(
            "Creation memory requires a complete Atom feed or detail entry"
        )
    if any(
        e.tag == f"{{{ATOM_NS}}}link" and e.get("rel", "").casefold() == "next"
        for e in root.iter()
    ):
        raise ValueError("Creation memory inventory is incomplete")
    if detail and len(elements) != 1:
        raise ValueError("Creation memory detail requires exactly one entry")
    for entry in elements:
        ids = entry.findall(f"{{{ATOM_NS}}}id")
        content = entry.findall(f"{{{ATOM_NS}}}content")
        if (
            len(ids) != 1
            or len(ids[0])
            or len(content) != 1
            or len(content[0]) != 1
            or content[0][0].tag != f"{{{UOM_NS}}}{kind}"
        ):
            raise ValueError(
                "Creation memory entry has ambiguous identity or resource kind"
            )
        if any(
            not element.tag.startswith(f"{{{UOM_NS}}}")
            and element.tag != f"{{{ATOM_NS}}}link"
            for element in content[0][0].iter()
        ):
            raise ValueError(
                "Creation memory resource contains foreign field namespaces"
            )
        if any(
            child.tag.rsplit("}", 1)[-1] in {"id", "content"}
            and child.tag not in {f"{{{ATOM_NS}}}id", f"{{{ATOM_NS}}}content"}
            for child in entry
        ):
            raise ValueError("Creation memory entry contains foreign identity fields")
    entries = parse_feed(body)
    ids = [e.get("UUID") for e in entries]
    if any(not isinstance(uuid, str) or not is_uuid(uuid) for uuid in ids):
        raise ValueError("Creation memory resource UUID is unavailable")
    for entry in entries:
        entry["UUID"] = entry["UUID"].lower()
        if not isinstance(entry.get("Resource"), dict):
            raise TypeError("Creation memory resource is malformed")
    if len({e["UUID"] for e in entries}) != len(entries):
        raise ValueError("Creation memory inventory contains duplicate identities")
    return entries


async def _read(
    hmc: HMCClient, path: str, kind: str, ids: dict[str, str], *, detail: bool = False
) -> list[dict[str, Any]]:
    if any(not is_uuid(uuid) for uuid in ids.values()):
        raise ValueError("Creation memory path requires UUID arguments")
    body = await hmc._get("/rest/api/uom/" + path, kind, uuid_path_arguments=ids)
    return _entries(body, kind, detail=detail)


async def _detail(hmc: HMCClient, kind: str, uuid: str) -> dict[str, Any]:
    entries = await _read(hmc, f"{kind}/{uuid}", kind, {"uuid": uuid}, detail=True)
    if entries[0]["UUID"] != uuid.lower():
        raise ValueError("Creation memory detail identity differs")
    return entries[0]


def _identity(entry: object, kind: PartitionKind) -> PagingProvider:
    if not isinstance(entry, dict) or entry.get("ResourceType") != kind:
        raise ValueError("Creation memory partition kind differs")
    uuid, resource = entry.get("UUID"), entry.get("Resource")
    if not isinstance(uuid, str) or not is_uuid(uuid) or not isinstance(resource, dict):
        raise ValueError("Creation memory partition identity is unavailable")
    name = _text(resource.get("PartitionName"))
    if not name or len(name.encode()) > 65536:
        raise ValueError("Creation memory partition name is unavailable")
    return PagingProvider(uuid.lower(), name, _integer(resource.get("PartitionID")))


async def _inventory(hmc: HMCClient, system: str) -> tuple[dict[str, Any], ...]:
    entries = []
    for kind in ("LogicalPartition", "VirtualIOServer"):
        rows = await _read(
            hmc, f"ManagedSystem/{system}/{kind}", kind, {"system_uuid": system}
        )
        for entry in rows:
            _identity(entry, kind)
        entries.extend(rows)
    identities = [_identity(e, e["ResourceType"]) for e in entries]
    for values in (
        [p.uuid for p in identities],
        [p.name for p in identities],
        [p.partition_id for p in identities],
    ):
        if len(set(values)) != len(values):
            raise ValueError("Creation memory inventory contains ambiguous identity")
    return tuple(entries)


def _provider_uuid(hmc: HMCClient, system: str, value: object) -> str:
    if not isinstance(value, dict):
        raise TypeError("Paging provider link is unavailable")
    if "link" in value:
        if set(value) - {"link", "@attrs"} or not isinstance(value["link"], dict):
            raise ValueError("Paging provider link is ambiguous")
        value = value["link"]
    href = value.get("href")
    if (
        not isinstance(href, str)
        or not href.isprintable()
        or any(c.isspace() for c in href)
    ):
        raise ValueError("Paging provider link is malformed")
    url = urlsplit(href)
    if (
        url.scheme != "https"
        or url.hostname != hmc.config.host
        or (url.port if url.port is not None else 443) != hmc.config.port
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Paging provider link is foreign")
    parts = tuple(
        part.casefold() if is_uuid(part) else part for part in url.path.split("/")
    )
    for prefix in (
        "/rest/api/uom/VirtualIOServer/",
        f"/rest/api/uom/ManagedSystem/{system}/VirtualIOServer/",
    ):
        expected = tuple(
            part.casefold() if is_uuid(part) else part
            for part in prefix.split("/")[:-1]
        )
        if parts[:-1] == expected and is_uuid(parts[-1]):
            return parts[-1].lower()
    raise ValueError("Paging provider link is outside the selected system VIOS route")


def _selected(
    memory: LparCreationMemory, role: str, providers: tuple[PagingProvider, ...]
) -> tuple[PagingProvider, ...]:
    name = getattr(memory, f"{role}_paging_vios_name")
    number = getattr(memory, f"{role}_paging_vios_id")
    if number == "none":
        return ()
    if name is None and number is None:
        return providers
    matches = tuple(p for p in providers if p.name == name or p.partition_id == number)
    if len(matches) != 1:
        raise ValueError("Paging provider selector is missing or ambiguous")
    return matches


async def admit_creation_memory(
    hmc: HMCClient,
    system_uuid: str,
    system_name: str,
    memory: LparCreationMemory,
    resources: LparResources,
    *,
    kind: PartitionKind,
    environment: Environment,
    name: str,
    partition_id: int | None = None,
) -> CreationMemoryAdmission:
    """Establish complete candidate identities and authorize every possible provider."""
    if not isinstance(memory, LparCreationMemory):
        raise TypeError("creation_memory must be LparCreationMemory")
    if (
        (kind == "LogicalPartition" and environment not in {"AIX/Linux", "OS400"})
        or (kind == "VirtualIOServer" and environment != "Virtual IO Server")
        or kind not in {"LogicalPartition", "VirtualIOServer"}
    ):
        raise ValueError("Creation memory kind and environment disagree")
    if memory.mem_mode == "shared" and (
        resources.dedicated is not False or resources.desired_procs is None
    ):
        raise ValueError("Shared memory requires known completed shared processors")
    inventory = await _inventory(hmc, system_uuid)
    if any(
        _identity(e, e["ResourceType"]).name == name
        or partition_id is not None
        and _identity(e, e["ResourceType"]).partition_id == partition_id
        for e in inventory
    ):
        raise ValueError("Creation memory pending name or ID already exists")
    system = await _detail(hmc, "ManagedSystem", system_uuid)
    if _text(system["Resource"].get("SystemName")) != system_name:
        raise ValueError("Creation memory selected system identity differs")
    caps = system["Resource"].get("AssociatedSystemCapabilities")
    required = []
    if memory.mem_mode == "shared":
        required.append("ActiveMemorySharingCapable")
    if memory.mem_expansion is not None and memory.mem_expansion > 0:
        required.append("ActiveMemoryExpansionCapable")
    if any(
        not isinstance(caps, dict) or _boolean(caps.get(key)) is not True
        for key in required
    ):
        raise ValueError(
            "Creation memory requires positive selected-system capabilities"
        )
    primary: tuple[PagingProvider, ...] = ()
    secondary: tuple[PagingProvider, ...] = ()
    if memory.mem_mode == "shared":
        pools = await _read(
            hmc,
            f"ManagedSystem/{system_uuid}/SharedMemoryPool",
            "SharedMemoryPool",
            {"system_uuid": system_uuid},
        )
        if len(pools) != 1:
            raise ValueError("Shared memory requires exactly one identified pool")
        pool = pools[0]["Resource"]
        uuids = [
            _provider_uuid(hmc, system_uuid, pool[key])
            for key in ("PagingServicePartitionOne", "PagingServicePartitionTwo")
            if key in pool and pool[key] != ""
        ]
        if not uuids or len(set(uuids)) != len(uuids):
            raise ValueError("Paging provider topology is unavailable or duplicate")
        providers = []
        details = {}
        for uuid in uuids:
            rows = [
                e
                for e in inventory
                if e["UUID"] == uuid and e["ResourceType"] == "VirtualIOServer"
            ]
            if len(rows) != 1:
                raise ValueError(
                    "Paging provider is outside the selected system VIOS inventory"
                )
            expected = _identity(rows[0], "VirtualIOServer")
            detail = await _detail(hmc, "VirtualIOServer", uuid)
            provider = _identity(detail, "VirtualIOServer")
            mem = detail["Resource"].get("PartitionMemoryConfiguration")
            if (
                provider != expected
                or _text(detail["Resource"].get("PartitionType")) != "Virtual IO Server"
                or not isinstance(mem, dict)
                or _boolean(mem.get("SharedMemoryEnabled")) is not False
            ):
                raise ValueError(
                    "Paging provider identity or dedicated memory is unestablished"
                )
            providers.append(provider)
            details[uuid] = detail
        primary = _selected(memory, "primary", tuple(providers))
        secondary = _selected(memory, "secondary", tuple(providers))
        if not primary or (
            len(primary) == len(secondary) == 1
            and primary == secondary
            and (
                memory.secondary_paging_vios_name is not None
                or memory.secondary_paging_vios_id is not None
            )
        ):
            raise ValueError(
                "Primary and secondary paging provider identities must differ"
            )
        for provider in set(primary + secondary):
            authorize_lpar_ownership_description(
                hmc,
                system_name,
                provider.name,
                _text(details[provider.uuid]["Resource"].get("Description")),
                operation="lpar-mutation",
            )
    return CreationMemoryAdmission(
        memory,
        system_uuid.lower(),
        system_name,
        kind,
        environment,
        name,
        partition_id,
        frozenset(e["UUID"] for e in inventory),
        primary,
        secondary,
    )


async def confirm_creation_memory_identity(
    hmc: HMCClient, admission: CreationMemoryAdmission, candidate: object
) -> dict[str, Any]:
    """Require a fresh contained inactive identity before stamping or observing."""
    target = _identity(candidate, admission.kind)
    if (
        target.uuid in admission.preexisting
        or target.name != admission.name
        or admission.partition_id is not None
        and target.partition_id != admission.partition_id
    ):
        raise ValueError("Creation memory identity is old or differs from request")
    assert isinstance(candidate, dict)  # _identity validated the entry above.
    resource = candidate["Resource"]
    if (
        _text(resource.get("PartitionType")) != admission.environment
        or _text(resource.get("PartitionState")) != "not activated"
    ):
        raise ValueError("Creation memory environment or inactive state differs")
    rows = await _inventory(hmc, admission.system_uuid)
    matches = [e for e in rows if e["UUID"] == target.uuid]
    if len(matches) != 1 or _identity(matches[0], admission.kind) != target:
        raise ValueError("Creation memory identity is not contained")
    detail = await _detail(hmc, admission.kind, target.uuid)
    if (
        _identity(detail, admission.kind) != target
        or _text(detail["Resource"].get("PartitionType")) != admission.environment
        or _text(detail["Resource"].get("PartitionState")) != "not activated"
    ):
        raise ValueError("Creation memory contained detail identity or state differs")
    # These values were independently checked; normalize attributed XML leaves
    # for the existing creation/stamping owners, which consume plain scalars.
    return {
        **detail,
        "Resource": {
            **detail["Resource"],
            "PartitionName": target.name,
            "PartitionID": str(target.partition_id),
            "PartitionType": admission.environment,
            "PartitionState": "not activated",
        },
    }


def _provider_key(memory: LparCreationMemory, role: str) -> str:
    return f"{role}_paging_vios_" + (
        "id" if getattr(memory, f"{role}_paging_vios_id") is not None else "name"
    )


def _scalar_observation(fields: dict[str, Any], key: str, current: bool) -> object:
    if key == "mem_mode":
        return (
            "shared"
            if _boolean(
                fields.get(
                    "SharedMemoryEnabled" if current else "ActiveMemorySharingEnabled"
                )
            )
            else "ded"
        )
    if key == "desired_io_entitled_mem":
        auto = _boolean(
            fields.get(
                "AutoEntitledMemoryEnabled" if current else "ManualEntitledModeEnabled"
            )
        )
        if auto == current:
            return "auto"
        return _integer(
            fields.get("CurrentEntitledMemory" if current else "DesiredEntitledMemory")
        )
    if key == "mem_weight":
        weight = _integer(
            fields.get("CurrentMemoryWeight" if current else "MemoryWeight"), 0
        )
        if weight > 255:
            raise ValueError("Creation memory observed weight is outside its domain")
        return weight
    if not _boolean(
        fields.get(
            "MemoryExpansionEnabled" if current else "ActiveMemoryExpansionEnabled"
        )
    ):
        return Decimal(0)
    factor = Decimal(
        _text(fields.get("CurrentExpansionFactor" if current else "ExpansionFactor"))
    )
    if not factor.is_finite() or not 1 <= factor <= 10:
        raise ValueError("Creation memory expansion factor is unavailable")
    return factor


def _observe(
    hmc: HMCClient,
    admission: CreationMemoryAdmission,
    fields: dict[str, Any],
    *,
    current: bool,
    controller: bool,
) -> CreationMemoryObservation:
    memory = admission.memory
    observed: dict[str, object] = {}
    unavailable = []
    mismatch = False
    scalars = dict(memory.pairs())
    scalars.setdefault("mem_mode", None)
    for key in scalars:
        if "paging_vios" in key:
            continue
        requested = getattr(memory, key)
        try:
            if key == "desired_io_entitled_mem" and requested == "auto":
                mismatch |= (
                    _boolean(
                        fields.get(
                            "AutoEntitledMemoryEnabled"
                            if current
                            else "ManualEntitledModeEnabled"
                        )
                    )
                    != current
                )
            elif key == "mem_expansion" and requested == 0:
                mismatch |= _boolean(
                    fields.get(
                        "MemoryExpansionEnabled"
                        if current
                        else "ActiveMemoryExpansionEnabled"
                    )
                )
            observed[key] = _scalar_observation(fields, key, current)
            mismatch |= requested is not None and observed[key] != requested
        except (ValueError, TypeError, InvalidOperation):
            unavailable.append(key)
    if memory.mem_mode == "shared" and (not current or controller):
        pair = []
        for role, leaf, candidates in (
            ("primary", "PrimaryPagingServicePartition", admission.primary),
            ("secondary", "SecondaryPagingServicePartition", admission.secondary),
        ):
            key = _provider_key(memory, role)
            try:
                if controller or leaf not in fields:
                    raise ValueError("Designated paging evidence is unavailable")
                if fields[leaf] == "" and role == "secondary":
                    observed[key] = "none"
                    mismatch |= bool(
                        getattr(memory, f"{role}_paging_vios_name") is not None
                        or type(getattr(memory, f"{role}_paging_vios_id")) is int
                    )
                    continue
                uuid = _provider_uuid(hmc, admission.system_uuid, fields[leaf])
                matches = [
                    p
                    for p in set(admission.primary + admission.secondary)
                    if p.uuid == uuid
                ]
                if len(matches) != 1:
                    mismatch = True
                    raise ValueError(
                        "Designated paging provider differs from authorized candidates"
                    )
                observed[key] = asdict(matches[0])
                mismatch |= matches[0] not in candidates
                pair.append(uuid)
            except (ValueError, TypeError):
                unavailable.append(key)
        mismatch |= len(pair) == 2 and pair[0] == pair[1]
    status = "mismatch" if mismatch else "unavailable" if unavailable else "verified"
    return CreationMemoryObservation(status, observed, tuple(unavailable))


async def creation_memory_step(
    hmc: HMCClient,
    admission: CreationMemoryAdmission,
    entry: dict[str, Any] | None,
    *,
    controller_mode: bool,
    current: bool = False,
    saved: CreationMemoryObservation | None = None,
) -> WorkflowStep:
    """Saved proof and current configured proof remain separate after optional apply."""
    current = current or controller_mode
    try:
        confirmed = await confirm_creation_memory_identity(hmc, admission, entry)
        if current:
            fields = confirmed["Resource"].get("PartitionMemoryConfiguration")
        else:
            record = await profiles.read_partition_profile(
                hmc,
                admission.system_uuid,
                confirmed["UUID"],
                "default_profile",
                partition_type=admission.kind,
            )
            fields = record.fields.get("ProfileMemory")
        if not isinstance(fields, dict):
            fields = {}
        observation = _observe(
            hmc, admission, fields, current=current, controller=controller_mode
        )
    except Exception:  # noqa: BLE001 — post-create read failures retain only bounded observations.
        observation = _observe(
            hmc, admission, {}, current=current, controller=controller_mode
        )
    verification = CreationMemoryVerification(
        admission.memory,
        saved if current else observation,
        observation if current else None,
    )
    ok = observation.status == "verified" and (
        saved is None or saved.status == "verified"
    )
    return WorkflowStep("creation_memory", "ok" if ok else "error", verification)


async def _raise_creation_memory_failure(
    hmc: HMCClient,
    admission: CreationMemoryAdmission,
    error: _CreationMemorySubmissionError,
) -> None:
    """Reconcile one uncertain submission without stamping, replay or deletion."""
    outcome = "unknown"
    try:
        inventory = await _inventory(hmc, admission.system_uuid)
        matches = [
            e
            for e in inventory
            if _identity(e, e["ResourceType"]).name == admission.name
            or admission.partition_id is not None
            and _identity(e, e["ResourceType"]).partition_id == admission.partition_id
        ]
        if not matches and error.remote_refusal_code is not None:
            outcome = "rejected"
        elif len(matches) == 1:
            candidate = matches[0]
            identity = _identity(candidate, admission.kind)
            if (
                identity.uuid not in admission.preexisting
                and identity.name == admission.name
                and (
                    admission.partition_id is None
                    or identity.partition_id == admission.partition_id
                )
            ):
                detail = await _detail(hmc, admission.kind, identity.uuid)
                if (
                    _identity(detail, admission.kind) == identity
                    and _text(detail["Resource"].get("PartitionType"))
                    == admission.environment
                    and _text(detail["Resource"].get("PartitionState"))
                    == "not activated"
                ):
                    outcome = "partial"
    except Exception:  # noqa: BLE001 — post-submission evidence failure is always unknown.
        outcome = "unknown"
    code = error.remote_refusal_code
    suffix = (
        f" ({code})"
        if isinstance(code, str) and re.fullmatch(r"HSCL[A-Z0-9]{4}", code)
        else ""
    )
    identity = (
        "scoped inventory observed no partition"
        if outcome == "rejected"
        else "partition exists or may exist"
    )
    raise HMCError(
        f"Creation memory outcome {outcome}{suffix}; {identity}. Inspect scoped identity before retrying; no retry, ownership stamp or rollback performed"
    ) from None
