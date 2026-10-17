"""Presentation-neutral LPAR decommission orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException
from typing_extensions import TypedDict

from hmcpctl.client.client_parse import _parse_feed
from hmcpctl.client.client_storage import mapping_lpar_uuid, storage_mapping_id
from hmcpctl.client.core import HMCClient
from hmcpctl.operations.lpar.ownership import (
    authorize_decommission_lpar_ownership_snapshot,
    resolve_lpar_ownership_names,
)

from ...audit import records as audit
from ...client.client_contracts import AdapterType
from ...errors import HMCError
from ...jobs import (
    DEFAULT_JOB_POLL_INTERVAL,
    DEFAULT_JOB_TIMEOUT_SECONDS,
    SUCCESSFUL_JOB_STATUSES,
    job_identifier,
    job_outcome,
    power_off_lpar_job,
    validate_wait_timing,
    wait_for_submitted_job,
)
from ...resource_identity import is_uuid, resolve_system_uuid
from ...xmlutil import leaf_text
from .workflow_contract import WorkflowStep

_ADAPTER_ORDER: tuple[AdapterType, ...] = (
    "ClientNetworkAdapter",
    "VirtualSCSIClientAdapter",
    "VirtualFibreChannelClientAdapter",
    "VirtualNICDedicated",
)
_STORAGE_MAPPING_TYPES: tuple[tuple[str, str], ...] = (
    ("VirtualSCSIMappings", "VirtualSCSIMapping"),
    ("VirtualFibreChannelMappings", "VirtualFibreChannelMapping"),
)


class _VScsiDetach(TypedDict):
    vios_uuid: str
    mapping_id: str


def _vscsi_detach_inventory(
    resource: dict[str, Any], vios_uuid: str, lpar_uuid: str
) -> tuple[tuple[_VScsiDetach, ...], tuple[str, ...]]:
    if "VirtualSCSIMappings" not in resource:
        return (), (f"VIOS {vios_uuid!r} omitted its vSCSI mapping collection.",)
    block = resource["VirtualSCSIMappings"]
    if block == "" or block == {}:
        return (), ()
    if not isinstance(block, dict):
        return (), (f"VIOS {vios_uuid!r} has malformed vSCSI mappings.",)
    if "Metadata" in block and block["Metadata"] != {"Atom": ""}:
        return (), (f"VIOS {vios_uuid!r} has malformed vSCSI mappings.",)
    members = {
        k: v for k, v in block.items() if not k.startswith("@") and k != "Metadata"
    }
    if not members:
        return (), ()
    if set(members) != {"VirtualSCSIMapping"}:
        return (), (f"VIOS {vios_uuid!r} has malformed vSCSI mappings.",)
    raw = members["VirtualSCSIMapping"]
    items = raw if isinstance(raw, list) else [raw]
    targets: list[_VScsiDetach] = []
    errors: list[str] = []
    identities: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            errors.append(f"VIOS {vios_uuid!r} has a malformed vSCSI mapping.")
            continue
        client = mapping_lpar_uuid(item)
        identity = storage_mapping_id(item)
        if identity is not None:
            identities.append(identity)
        if client is None or not is_uuid(client):
            errors.append(f"VIOS {vios_uuid!r} has an unverified vSCSI client UUID.")
            continue
        if client.casefold() != lpar_uuid.casefold():
            continue
        if identity is None:
            errors.append(
                f"VIOS {vios_uuid!r} has a target mapping without an exact ID."
            )
            continue
        targets.append({"vios_uuid": vios_uuid, "mapping_id": identity})
    for target in targets:
        if identities.count(target["mapping_id"]) != 1:
            errors.append(f"VIOS {vios_uuid!r} has a duplicate target mapping ID.")
    return tuple(sorted(targets, key=lambda item: item["mapping_id"])), tuple(errors)


class DecommissionAdapterRecord(TypedDict):
    """Adapter identity included in a decommission blast-radius inventory."""

    type: AdapterType
    uuid: str


class _MappingEvidence(TypedDict, total=False):
    mapping_evidence: tuple[dict[str, str], ...]


class DecommissionBlastRadius(_MappingEvidence):
    """Stable inventory returned before an LPAR decommission mutates resources."""

    lpar_uuid: str
    lpar_name: str
    partition_id: int | None
    state: str | None
    owner: str | None
    adapters: tuple[DecommissionAdapterRecord, ...]
    storage_mappings: tuple[dict[str, str], ...]
    unresolved_storage_mapping_count: int
    unavailable_storage_source_count: int


@dataclass(frozen=True)
class DecommissionResult:
    """Truthful outcome of an LPAR decommission request."""

    resource_deleted: bool = field(
        metadata={"description": "Whether the final LPAR delete completed."}
    )
    workflow_completed: bool = field(
        metadata={"description": "Whether every requested workflow step completed."}
    )
    lpar_uuid: str = field(
        metadata={"description": "UUID of the resolved target LPAR."}
    )
    dry_run: bool = field(
        metadata={"description": "Whether the call only inventoried the blast radius."}
    )
    steps: tuple[WorkflowStep, ...] = field(
        metadata={"description": "Ordered per-step status and curated result records."}
    )
    warnings: tuple[str, ...] = field(
        metadata={"description": "Non-fatal warnings discovered during inventory."}
    )
    blast_radius: DecommissionBlastRadius = field(
        metadata={
            "description": "Curated inventory of the LPAR, adapters, and observed storage mappings."
        }
    )


@dataclass(frozen=True)
class _Inventory:
    system_name: str
    ownership_lpar_name: str
    lpar_uuid: str
    lpar_name: str
    partition_id: int | None
    state: str | None
    owner: str | None
    adapters: tuple[DecommissionAdapterRecord, ...]
    storage_mappings: tuple[dict[str, str], ...]
    unresolved_storage_mapping_count: int
    unavailable_storage_source_count: int
    warnings: tuple[str, ...]
    vscsi_detaches: tuple[_VScsiDetach, ...]
    vscsi_inventory_errors: tuple[str, ...]
    mapping_evidence: tuple[dict[str, str], ...]

    def blast_radius(self) -> DecommissionBlastRadius:
        return {
            "lpar_uuid": self.lpar_uuid,
            "lpar_name": self.lpar_name,
            "partition_id": self.partition_id,
            "state": self.state,
            "owner": self.owner,
            "adapters": self.adapters,
            "storage_mappings": self.storage_mappings,
            "unresolved_storage_mapping_count": self.unresolved_storage_mapping_count,
            "unavailable_storage_source_count": self.unavailable_storage_source_count,
        }


def _skip_steps(steps: list[WorkflowStep], *names: str) -> None:
    steps.extend(WorkflowStep(name, "skipped") for name in names)


def _resource(entry: dict[str, Any] | None) -> dict[str, Any]:
    value = (entry or {}).get("Resource")
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _records(container: Any, singular: str) -> tuple[dict[str, Any], ...]:
    if isinstance(container, list):
        return tuple(item for item in container if isinstance(item, dict))
    if isinstance(container, dict):
        if singular in container:
            return _records(container[singular], singular)
        return (container,)
    return ()


def _extract_target_uuid(link: Any) -> str | None:
    if isinstance(link, dict):
        href = link.get("href") or link.get("link")
        return _extract_target_uuid(href)
    if not isinstance(link, str) or "/LogicalPartition/" not in link:
        return None
    return link.rsplit("/LogicalPartition/", 1)[-1].strip("/") or None


def _children_matching_uuid(
    children: list[dict[str, Any]], lpar_name_or_uuid: str
) -> list[dict[str, Any]]:
    normalized_selector = lpar_name_or_uuid.casefold()
    return [
        child
        for child in children
        if (child_uuid := _text(child.get("UUID"))) is not None
        and child_uuid.casefold() == normalized_selector
    ]


def _children_matching_name(
    children: list[dict[str, Any]], lpar_name_or_uuid: str
) -> list[dict[str, Any]]:
    return [
        child
        for child in children
        if _text(_resource(child).get("PartitionName")) == lpar_name_or_uuid
    ]


def _missing_target_error(system_uuid: str, lpar_name_or_uuid: str) -> ValueError:
    if is_uuid(lpar_name_or_uuid):
        return ValueError(
            f"No LPAR {lpar_name_or_uuid!r} found on managed system {system_uuid!r}."
        )
    return ValueError(
        f"No LPAR named {lpar_name_or_uuid!r} found on managed system {system_uuid!r}."
    )


def _ambiguous_name_error(
    matches: list[dict[str, Any]], system_uuid: str, lpar_name_or_uuid: str
) -> ValueError:
    identities = ", ".join(
        str(child.get("UUID") or "unknown")
        for child in sorted(matches, key=lambda item: str(item.get("UUID")))
    )
    return ValueError(
        f"Ambiguous LPAR name {lpar_name_or_uuid!r} on managed system "
        f"{system_uuid!r}: {identities}"
    )


def _mapping_matches_target(
    mapping: dict[str, Any], lpar_uuid: str, partition_id: str | None
) -> bool | None:
    link_uuid = _extract_target_uuid(mapping.get("AssociatedLogicalPartition"))
    if link_uuid is not None:
        return link_uuid == lpar_uuid

    candidate_ids = (
        _text(mapping.get("AssociatedLogicalPartitionID")),
        _text(mapping.get("ClientPartitionID")),
        _text(mapping.get("RemoteLogicalPartitionID")),
        _text(
            _resource({"Resource": mapping.get("ClientAdapter")}).get(
                "RemoteLogicalPartitionID"
            )
        ),
        _text(
            _resource({"Resource": mapping.get("ServerAdapter")}).get(
                "RemoteLogicalPartitionID"
            )
        ),
        _text(mapping.get("ConnectingPartitionID")),
    )
    for candidate in candidate_ids:
        if candidate is not None and partition_id is not None:
            return candidate == partition_id
    return None


def _named_storage_value(
    storage: dict[str, Any], section_name: str, field_name: str
) -> str | None:
    section = storage.get(section_name)
    if not isinstance(section, dict):
        return None
    return _text(section.get(field_name))


def _storage_backing_device(storage: dict[str, Any]) -> str | None:
    candidates = (
        ("PhysicalVolume", "VolumeName"),
        ("VirtualDisk", "DiskName"),
        ("LogicalUnit", "UnitName"),
    )
    for section_name, field_name in candidates:
        name = _named_storage_value(storage, section_name, field_name)
        if name is not None:
            return name
    return None


def _backing_device(mapping: dict[str, Any]) -> str | None:
    storage = mapping.get("Storage")
    if isinstance(storage, dict):
        name = _storage_backing_device(storage)
        if name is not None:
            return name
    port = mapping.get("Port")
    if isinstance(port, dict):
        return _text(port.get("WWPNPair"))
    return None


async def resolve_target_lpar(
    hmc: HMCClient, system_uuid: str, lpar_name_or_uuid: str
) -> dict[str, Any]:
    children = await hmc.list_logical_partitions(system_uuid)
    if is_uuid(lpar_name_or_uuid):
        matches = _children_matching_uuid(children, lpar_name_or_uuid)
        if len(matches) == 1:
            return matches[0]
        raise _missing_target_error(system_uuid, lpar_name_or_uuid)

    matches = _children_matching_name(children, lpar_name_or_uuid)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise _missing_target_error(system_uuid, lpar_name_or_uuid)
    raise _ambiguous_name_error(matches, system_uuid, lpar_name_or_uuid)


async def _resolve_inventory_identity(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    ownership_override: bool,
) -> tuple[str, str, str, str, str | None]:
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    child = await resolve_target_lpar(hmc, system_uuid, lpar_name_or_uuid)
    lpar_uuid = _text(child.get("UUID"))
    if lpar_uuid is None:
        raise ValueError("Resolved LPAR child is missing its UUID.")

    system_name, lpar_name = await resolve_lpar_ownership_names(
        hmc, system_uuid, system_name_or_uuid, lpar_uuid
    )
    owner = await authorize_decommission_lpar_ownership_snapshot(
        hmc, system_name, lpar_name, ownership_override=ownership_override
    )
    return system_uuid, system_name, lpar_uuid, lpar_name, owner


async def _partition_snapshot(
    hmc: HMCClient, lpar_uuid: str, fallback_name: str
) -> tuple[str, int | None, str | None]:
    lpar = await hmc.get_logical_partition(lpar_uuid)
    resource = _resource(lpar)
    partition_name = _text(resource.get("PartitionName")) or fallback_name
    partition_id = _as_int(resource.get("PartitionID"))
    state = _text(resource.get("PartitionState"))
    return partition_name, partition_id, state


_ATOM = "{http://www.w3.org/2005/Atom}"
_UOM = "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}"
_FEED_FIELDS = frozenset(
    [
        "id",
        "title",
        "updated",
        "link",
        "generator",
        "category",
        "author",
        "contributor",
        "subtitle",
        "rights",
        "icon",
        "logo",
        "entry",
    ]
)
_ENTRY_FIELDS = frozenset(
    [
        "id",
        "title",
        "updated",
        "published",
        "author",
        "contributor",
        "category",
        "link",
        "summary",
        "rights",
        "content",
    ]
)


def _complete_adapter_feed(xml: str, adapter_type: AdapterType) -> list[dict[str, Any]]:
    try:
        root = DET.fromstring(xml)
    except (DET.ParseError, DefusedXmlException) as exc:
        raise ValueError(f"Incomplete {adapter_type} inventory: malformed XML") from exc
    if root.tag != _ATOM + "feed":
        raise ValueError(f"Incomplete {adapter_type} inventory: expected Atom feed")
    metadata = [child for child in root if child.tag != _ATOM + "entry"]
    for child in root:
        if child.tag not in {_ATOM + name for name in _FEED_FIELDS}:
            raise ValueError(f"Incomplete {adapter_type} inventory: unknown feed child")
        if child.tag == _ATOM + "entry":
            metadata.extend(item for item in child if item.tag != _ATOM + "content")
            if any(
                item.tag
                not in {_ATOM + name for name in _ENTRY_FIELDS} | {_UOM + "etag"}
                for item in child
            ):
                raise ValueError(
                    f"Incomplete {adapter_type} inventory: unknown entry child"
                )
            identities = child.findall(_ATOM + "id")
            contents = child.findall(_ATOM + "content")
            if len(identities) != 1 or not is_uuid(identities[0].text or ""):
                raise ValueError(
                    f"Incomplete {adapter_type} inventory: missing its UUID or invalid UUID"
                )
            if (
                len(contents) != 1
                or len(contents[0]) != 1
                or contents[0][0].tag != _UOM + adapter_type
            ):
                raise ValueError(
                    f"Incomplete {adapter_type} inventory: invalid resource content"
                )
    # Native Atom person fields are metadata, never another resource/entry container.
    person_fields = {_ATOM + name for name in ("name", "uri", "email")}
    if any(
        descendant.tag not in person_fields
        for item in metadata
        for descendant in item.iter()
        if descendant is not item
    ):
        raise ValueError(
            f"Incomplete {adapter_type} inventory: unsupported metadata descendants"
        )
    if any(link.get("rel") == "next" for link in root.iter(_ATOM + "link")):
        raise ValueError(f"Incomplete {adapter_type} inventory: paginated feed")
    return _parse_feed(xml, f"{adapter_type} inventory")


async def _inventory_adapters(
    hmc: HMCClient, lpar_uuid: str
) -> tuple[DecommissionAdapterRecord, ...]:
    adapters: list[DecommissionAdapterRecord] = []
    identities: set[str] = set()
    for adapter_type in _ADAPTER_ORDER:
        response = await hmc._request_with_uuid_path_arguments(
            "GET",
            f"/rest/api/uom/LogicalPartition/{lpar_uuid}/{adapter_type}",
            headers=hmc._uom_headers(adapter_type),
            uuid_path_arguments={"parent_uuid": lpar_uuid},
        )
        if response.status_code == 204 and not response.content:
            continue
        if response.status_code != 200 or not response.content:
            raise HMCError(
                f"Incomplete {adapter_type} inventory: HTTP {response.status_code}",
                response.status_code,
                response.text,
            )
        entries = _complete_adapter_feed(response.text, adapter_type)
        for entry in sorted(entries, key=lambda item: str(item.get("UUID") or "")):
            uuid = _text(entry.get("UUID"))
            if uuid is None or not is_uuid(uuid) or uuid.lower() in identities:
                raise ValueError(
                    f"Incomplete {adapter_type} inventory: invalid or duplicate UUID"
                )
            identities.add(uuid.lower())
            adapters.append({"type": adapter_type, "uuid": uuid})
    return tuple(adapters)


def _mapping_record(
    mapping: dict[str, Any], vios_uuid: str, item_name: str
) -> dict[str, str]:
    record = {
        "vios_uuid": vios_uuid,
        "type": item_name,
        "uuid": _text(mapping.get("UUID")) or "unknown",
    }
    backing_device = _backing_device(mapping)
    if backing_device is not None:
        record["backing_device"] = backing_device
    return record


def collect_storage_records(
    detail_resource: dict[str, Any],
    vios_uuid: str,
    lpar_uuid: str,
    partition_id_text: str | None,
) -> tuple[tuple[dict[str, str], ...], int]:
    storage_mappings: list[dict[str, str]] = []
    unresolved = 0
    for block_name, item_name in _STORAGE_MAPPING_TYPES:
        for mapping in _records(detail_resource.get(block_name), item_name):
            matches = _mapping_matches_target(mapping, lpar_uuid, partition_id_text)
            if matches is True:
                storage_mappings.append(_mapping_record(mapping, vios_uuid, item_name))
            elif matches is None:
                unresolved += 1
    return tuple(storage_mappings), unresolved


def _evidence_backing_device(mapping: dict[str, Any]) -> str:
    storage = _resource({"Resource": mapping.get("Storage")})
    for section, field_name in (
        ("PhysicalVolume", "VolumeName"),
        ("VirtualDisk", "DiskName"),
        ("LogicalUnit", "UnitName"),
        ("VirtualOpticalMedia", "MediaName"),
    ):
        device = _resource({"Resource": storage.get(section)})
        name = _text(leaf_text(device.get(field_name)))
        if name is not None:
            return name
    port = _resource({"Resource": mapping.get("Port")})
    return _text(leaf_text(port.get("WWPNPair"))) or "unknown"


def _mapping_evidence(
    resource: dict[str, Any], vios_uuid: str, lpar_uuid: str, partition_id: str | None
) -> list[dict[str, str]]:
    evidence = []
    for block, kind in _STORAGE_MAPPING_TYPES:
        collection = resource.get(block)
        if isinstance(collection, dict) and all(
            key.startswith("@") or (key == "Metadata" and value == {"Atom": ""})
            for key, value in collection.items()
        ):
            continue
        for mapping in _records(collection, kind):
            if not mapping:
                continue
            client = mapping_lpar_uuid(mapping)
            valid_client = client is not None and is_uuid(client)
            matches = _mapping_matches_target(mapping, lpar_uuid, partition_id)
            if valid_client:
                matches = client.casefold() == lpar_uuid.casefold()
            scsi = kind == "VirtualSCSIMapping"
            if matches is False and (valid_client or not scsi):
                continue
            observed_uuid = _text(leaf_text(mapping.get("UUID")))
            identity = storage_mapping_id(mapping) if scsi else observed_uuid
            missing = []
            if not valid_client:
                missing.append("AssociatedLogicalPartition UUID")
            record = _mapping_record(mapping, vios_uuid, kind)
            if scsi:
                adapter = _resource({"Resource": mapping.get("ServerAdapter")})
                target = _resource({"Resource": mapping.get("TargetDevice")})
                devices = [
                    value for key, value in target.items() if not key.startswith("@")
                ]
                device = (
                    devices[0]
                    if len(devices) == 1 and isinstance(devices[0], dict)
                    else {}
                )
                for field, value, label in (
                    (
                        "server_adapter",
                        adapter.get("AdapterName"),
                        "ServerAdapter.AdapterName",
                    ),
                    (
                        "target_device",
                        device.get("TargetName"),
                        "TargetDevice.TargetName",
                    ),
                ):
                    name = _text(leaf_text(value))
                    record[field] = name or "unknown"
                    if not name:
                        missing.append(label)
                if identity is None and all(
                    record[field] != "unknown"
                    for field in ("server_adapter", "target_device")
                ):
                    missing.append("supported mapping identifier")
            elif identity is None:
                missing.append("mapping UUID")
            record.update(
                uuid=observed_uuid or "unknown",
                mapping_id=identity or "unknown",
                client_uuid=client or "unknown",
                missing_identity=", ".join(missing) or "none",
                relationship="target" if matches is True else "unknown",
                disposition="retained"
                if matches is True and (not scsi or not missing)
                else "unproven",
                backing_device=_evidence_backing_device(mapping),
                backing_storage="retained",
                reason="No mapping detach proven; backing storage is retained.",
            )
            evidence.append(record)
    return evidence


def _unavailable_evidence(vios_uuid: str, name: str, reason: str) -> dict[str, str]:
    return {
        "vios_uuid": vios_uuid,
        "vios_name": name,
        "type": "unknown",
        "mapping_id": "unknown",
        "uuid": "unknown",
        "backing_device": "unknown",
        "missing_identity": "storage inventory unavailable or incomplete",
        "disposition": "unproven",
        "relationship": "unknown",
        "backing_storage": "retained",
        "reason": reason,
    }


async def _inventory_storage_mappings(
    hmc: HMCClient, system_uuid: str, lpar_uuid: str, partition_id: int | None
) -> tuple[
    tuple[dict[str, str], ...],
    int,
    tuple[str, ...],
    tuple[_VScsiDetach, ...],
    tuple[str, ...],
    tuple[dict[str, str], ...],
]:
    storage_mappings: list[dict[str, str]] = []
    unresolved = 0
    source_warnings: list[str] = []
    evidence: list[dict[str, str]] = []

    targets: list[_VScsiDetach] = []
    errors: list[str] = []
    partition_id_text = str(partition_id) if partition_id is not None else None
    try:
        sources = await hmc.list_vios(system_uuid)
    except HMCError as exc:
        source_warnings.append(f"VIOS inventory could not be read: {exc}")
        evidence.append(
            _unavailable_evidence("unknown", "unknown", source_warnings[-1])
        )
        sources = []
    for vios in sources:
        vios_uuid = _text(vios.get("UUID"))
        vios_name = _text(_resource(vios).get("PartitionName")) or "unknown"
        evidence_name = (
            _text(leaf_text(_resource(vios).get("PartitionName"))) or "unknown"
        )
        if vios_uuid is None or not is_uuid(vios_uuid):
            source_warnings.append(
                "Storage blast radius may be incomplete: listed VIOS "
                f"{vios_name!r} has no UUID, so its storage mappings could not be "
                "inventoried."
            )
            evidence.append(
                _unavailable_evidence(
                    vios_uuid or "unknown", evidence_name, source_warnings[-1]
                )
            )
            continue
        try:
            detail = await hmc.get_vios_storage_detail(vios_uuid)
        except HMCError as exc:
            source_warnings.append(
                f"VIOS {vios_uuid!r} storage detail unavailable: {exc}"
            )
            evidence.append(
                _unavailable_evidence(vios_uuid, evidence_name, source_warnings[-1])
            )
            continue
        if detail is None:
            source_warnings.append(
                f"Storage blast radius may be incomplete: VIOS {vios_uuid!r} returned "
                "no storage detail, so its storage mappings could not be inventoried."
            )
            evidence.append(
                _unavailable_evidence(vios_uuid, evidence_name, source_warnings[-1])
            )
            continue
        detail_resource = detail.get("Resource")
        if not isinstance(detail_resource, dict):
            source_warnings.append(
                f"VIOS {vios_uuid!r} returned malformed storage detail."
            )
            evidence.append(
                _unavailable_evidence(vios_uuid, evidence_name, source_warnings[-1])
            )
            continue
        records, unresolved_count = collect_storage_records(
            detail_resource, vios_uuid, lpar_uuid, partition_id_text
        )
        storage_mappings.extend(records)
        unresolved += unresolved_count
        found, failures = _vscsi_detach_inventory(detail_resource, vios_uuid, lpar_uuid)
        observed = _mapping_evidence(
            detail_resource, vios_uuid, lpar_uuid, partition_id_text
        )
        evidence.extend(dict(row, vios_name=evidence_name) for row in observed)
        if failures and not observed:
            evidence.append(
                _unavailable_evidence(vios_uuid, evidence_name, "; ".join(failures))
            )
        targets.extend(found)
        errors.extend(failures)
    return (
        tuple(storage_mappings),
        unresolved,
        tuple(source_warnings),
        tuple(
            sorted(targets, key=lambda item: (item["vios_uuid"], item["mapping_id"]))
        ),
        tuple(source_warnings + errors),
        tuple(evidence),
    )


def _inventory_warnings(
    partition_name: str, unresolved: int, source_warnings: tuple[str, ...]
) -> tuple[str, ...]:
    warnings: list[str] = []
    if unresolved:
        warnings.append(
            "Storage blast radius may be incomplete: "
            f"{unresolved} mapping(s) lacked enough client identity to prove they belong "
            f"to LPAR {partition_name!r}."
        )
    warnings.extend(source_warnings)
    return tuple(warnings)


async def _inventory(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    ownership_override: bool,
) -> _Inventory:
    (
        system_uuid,
        system_name,
        lpar_uuid,
        lpar_name,
        owner,
    ) = await _resolve_inventory_identity(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )
    partition_name, partition_id, state = await _partition_snapshot(
        hmc, lpar_uuid, lpar_name
    )
    adapters = await _inventory_adapters(hmc, lpar_uuid)
    (
        storage_mappings,
        unresolved,
        source_warnings,
        detaches,
        errors,
        evidence,
    ) = await _inventory_storage_mappings(hmc, system_uuid, lpar_uuid, partition_id)

    return _Inventory(
        system_name=system_name,
        ownership_lpar_name=lpar_name,
        lpar_uuid=lpar_uuid,
        lpar_name=partition_name,
        partition_id=partition_id,
        state=state,
        owner=owner,
        adapters=adapters,
        storage_mappings=storage_mappings,
        unresolved_storage_mapping_count=unresolved,
        unavailable_storage_source_count=len(source_warnings),
        warnings=_inventory_warnings(partition_name, unresolved, source_warnings)
        + tuple(error for error in errors if error not in source_warnings),
        vscsi_detaches=detaches,
        vscsi_inventory_errors=errors,
        mapping_evidence=evidence,
    )


async def _power_off(
    hmc: HMCClient,
    inventory: _Inventory,
    *,
    immediate: bool,
    timeout_seconds: int,
    poll_interval: int,
) -> WorkflowStep:
    if inventory.state == "not activated":
        return WorkflowStep(
            "power_off",
            "ok",
            {"already_off": True, "state": "not activated"},
        )
    # ADR 0180 as amended by #1115: before the submit, because a submit that raises
    # may still have reached the HMC. The values are the ones the document below carries.
    audit.record_lpar_power_off(
        lpar=inventory.lpar_uuid,
        host=hmc.config.host,
        operation="shutdown",
        immediate=immediate,
        restart=False,
        agent_id=hmc.config.agent_id or "hmcpctl",
    )
    submitted_job = await hmc.submit_job(
        f"/rest/api/uom/LogicalPartition/{inventory.lpar_uuid}/do/PowerOff",
        power_off_lpar_job(immediate=immediate),
    )
    completed_job = await wait_for_submitted_job(
        hmc, submitted_job, True, timeout_seconds, poll_interval
    )
    submitted_id = job_identifier(submitted_job) if submitted_job is not None else None
    outcome = job_outcome(submitted_id or "", completed_job)
    if outcome.timed_out:
        return WorkflowStep(
            "power_off",
            "error",
            f"PowerOff for LPAR {inventory.lpar_name!r} timed out before reaching "
            "a successful terminal status.",
        )
    if outcome.status not in SUCCESSFUL_JOB_STATUSES:
        detail = outcome.error or "no error detail returned"
        return WorkflowStep(
            "power_off",
            "error",
            f"PowerOff for LPAR {inventory.lpar_name!r} did not complete successfully "
            f"(status {outcome.status!r}: {detail}).",
        )
    return WorkflowStep(
        "power_off",
        "ok",
        {
            "already_off": False,
            "job_id": outcome.job_id,
            "status": outcome.status,
        },
    )


async def _detach_storage_mappings(
    hmc: HMCClient, inventory: _Inventory
) -> WorkflowStep:
    detached: list[_VScsiDetach] = []
    for mapping in inventory.vscsi_detaches:
        try:
            await hmc.delete_storage_mapping(
                mapping["vios_uuid"], mapping["mapping_id"], inventory.lpar_uuid
            )
        except HMCError as exc:
            return WorkflowStep(
                "detach_storage_mappings",
                "error",
                {
                    "mappings": tuple(detached),
                    "error": str(exc),
                    "failed_mapping": mapping,
                },
            )
        detached.append(mapping)
    return WorkflowStep("detach_storage_mappings", "ok", {"mappings": tuple(detached)})


async def _detach_adapters(
    hmc: HMCClient,
    inventory: _Inventory,
    adapters: tuple[DecommissionAdapterRecord, ...],
) -> WorkflowStep:
    deleted: list[DecommissionAdapterRecord] = []
    for adapter in adapters:
        try:
            await hmc.delete_adapter(
                inventory.lpar_uuid,
                adapter["type"],
                adapter["uuid"],
            )
        except HMCError as exc:
            return WorkflowStep(
                "detach_adapters",
                "error",
                {
                    "adapters": tuple(deleted),
                    "error": str(exc),
                },
            )
        deleted.append(adapter)
    return WorkflowStep(
        "detach_adapters",
        "ok",
        {"adapters": adapters},
    )


async def _detach_state_error(
    hmc: HMCClient, inventory: _Inventory
) -> WorkflowStep | None:
    try:
        state = await hmc.get_quick_property(
            "LogicalPartition", inventory.lpar_uuid, "PartitionState"
        )
    except HMCError as exc:
        return WorkflowStep(
            "detach_adapters",
            "error",
            f"Could not verify LPAR {inventory.lpar_name!r} state before "
            f"detaching adapters: {exc}",
        )
    if state == "not activated":
        return None
    return WorkflowStep(
        "detach_adapters",
        "error",
        f"Cannot detach adapters from LPAR {inventory.lpar_name!r}: current state "
        f"is {state!r}; expected 'not activated'.",
    )


def _build_decommission_result(
    inventory: _Inventory,
    *,
    resource_deleted: bool,
    workflow_completed: bool,
    dry_run: bool,
    steps: tuple[WorkflowStep, ...],
) -> DecommissionResult:
    blast_radius = inventory.blast_radius()
    evidence = [dict(row) for row in inventory.mapping_evidence]
    mapping_step = next(
        (step for step in steps if step.step == "detach_storage_mappings"), None
    )
    outcome = (
        mapping_step.result
        if mapping_step and isinstance(mapping_step.result, dict)
        else {}
    )
    for row in evidence:
        identity = {"vios_uuid": row["vios_uuid"], "mapping_id": row["mapping_id"]}
        if (
            row["type"] != "VirtualSCSIMapping"
            or row["disposition"] == "unproven"
            or identity not in inventory.vscsi_detaches
        ):
            continue
        if identity in outcome.get("mappings", ()) and not dry_run:
            row.update(
                disposition="detached",
                reason="Mapping detach completed; backing storage retained.",
            )
        elif identity == outcome.get("failed_mapping"):
            row.update(
                disposition="failed",
                reason=f"Detach request failed; resulting mapping state is unproven: {outcome['error']}",
            )
        elif dry_run and not inventory.vscsi_inventory_errors:
            row.update(
                disposition="planned detach",
                reason="Dry-run only; no mutation performed.",
            )
        else:
            row.update(
                disposition="retained",
                reason="Detach not attempted; workflow blocked or stopped.",
            )
    blast_radius["mapping_evidence"] = tuple(evidence)
    return DecommissionResult(
        resource_deleted=resource_deleted,
        workflow_completed=workflow_completed,
        lpar_uuid=inventory.lpar_uuid,
        dry_run=dry_run,
        steps=steps,
        warnings=inventory.warnings,
        blast_radius=blast_radius,
    )


def _dry_run_steps(inventory: _Inventory) -> tuple[WorkflowStep, ...]:
    return (
        WorkflowStep("power_off", "dry_run", {"state": inventory.state}),
        WorkflowStep(
            "detach_storage_mappings", "dry_run", {"mappings": inventory.vscsi_detaches}
        ),
        WorkflowStep("detach_adapters", "dry_run", {"adapters": inventory.adapters}),
        WorkflowStep("delete_lpar", "dry_run", {"lpar_uuid": inventory.lpar_uuid}),
    )


def _incomplete_result(
    inventory: _Inventory, steps: list[WorkflowStep]
) -> DecommissionResult:
    return _build_decommission_result(
        inventory,
        resource_deleted=False,
        workflow_completed=False,
        dry_run=False,
        steps=tuple(steps),
    )


async def _power_step_with_errors(
    hmc: HMCClient,
    inventory: _Inventory,
    *,
    immediate: bool,
    timeout_seconds: int,
    poll_interval: int,
) -> WorkflowStep:
    try:
        return await _power_off(
            hmc,
            inventory,
            immediate=immediate,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        )
    except HMCError as exc:
        return WorkflowStep("power_off", "error", str(exc))


async def _delete_lpar_step(hmc: HMCClient, lpar_uuid: str) -> WorkflowStep:
    try:
        await hmc.delete_logical_partition(lpar_uuid)
    except HMCError as exc:
        return WorkflowStep("delete_lpar", "error", str(exc))
    return WorkflowStep("delete_lpar", "ok", {"lpar_uuid": lpar_uuid})


async def decommission_lpar(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    dry_run: bool = False,
    ownership_override: bool = False,
    immediate: bool = False,
    timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS,
    poll_interval: int = DEFAULT_JOB_POLL_INTERVAL,
) -> DecommissionResult:
    """Inventory, authorize, and optionally decommission one LPAR."""
    validate_wait_timing(True, timeout_seconds, poll_interval)
    inventory = await _inventory(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        ownership_override=ownership_override,
    )

    if dry_run:
        return _build_decommission_result(
            inventory,
            resource_deleted=False,
            workflow_completed=True,
            dry_run=True,
            steps=_dry_run_steps(inventory),
        )

    if inventory.vscsi_inventory_errors:
        return _incomplete_result(
            inventory,
            [
                WorkflowStep("power_off", "skipped"),
                WorkflowStep(
                    "detach_storage_mappings",
                    "error",
                    {
                        "mappings": (),
                        "errors": inventory.vscsi_inventory_errors,
                    },
                ),
                WorkflowStep("detach_adapters", "skipped"),
                WorkflowStep("delete_lpar", "skipped"),
            ],
        )

    steps: list[WorkflowStep] = []
    if inventory.state != "not activated":
        await authorize_decommission_lpar_ownership_snapshot(
            hmc,
            inventory.system_name,
            inventory.ownership_lpar_name,
            ownership_override=ownership_override,
        )
    power_step = await _power_step_with_errors(
        hmc,
        inventory,
        immediate=immediate,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )
    steps.append(power_step)
    if power_step.status != "ok":
        _skip_steps(steps, "detach_storage_mappings", "detach_adapters", "delete_lpar")
        return _incomplete_result(inventory, steps)

    await authorize_decommission_lpar_ownership_snapshot(
        hmc,
        inventory.system_name,
        inventory.ownership_lpar_name,
        ownership_override=ownership_override,
    )
    detach_state_error = await _detach_state_error(hmc, inventory)
    if detach_state_error is not None:
        steps.append(
            WorkflowStep("detach_storage_mappings", "error", detach_state_error.result)
        )
        _skip_steps(steps, "detach_adapters", "delete_lpar")
        return _incomplete_result(inventory, steps)
    mapping_step = await _detach_storage_mappings(hmc, inventory)
    steps.append(mapping_step)
    if mapping_step.status != "ok":
        _skip_steps(steps, "detach_adapters", "delete_lpar")
        return _incomplete_result(inventory, steps)
    detach_state_error = await _detach_state_error(hmc, inventory)
    if detach_state_error is not None:
        steps.append(detach_state_error)
        _skip_steps(steps, "delete_lpar")
        return _incomplete_result(inventory, steps)

    try:
        await authorize_decommission_lpar_ownership_snapshot(
            hmc,
            inventory.system_name,
            inventory.ownership_lpar_name,
            ownership_override=ownership_override,
        )
        adapters = await _inventory_adapters(hmc, inventory.lpar_uuid)
        authorized = {
            (item["type"], item["uuid"].lower()) for item in inventory.adapters
        }
        if any(
            (item["type"], item["uuid"].lower()) not in authorized for item in adapters
        ):
            raise ValueError(
                "Refreshed adapter inventory contains an unauthorized identity"
            )
    except (HMCError, ValueError, PermissionError) as exc:
        steps.append(
            WorkflowStep(
                "detach_adapters", "error", {"adapters": (), "error": str(exc)}
            )
        )
        _skip_steps(steps, "delete_lpar")
        return _incomplete_result(inventory, steps)
    detach_step = await _detach_adapters(hmc, inventory, adapters)
    steps.append(detach_step)
    if detach_step.status != "ok":
        steps.append(WorkflowStep("delete_lpar", "skipped"))
        return _incomplete_result(inventory, steps)

    delete_step = await _delete_lpar_step(hmc, inventory.lpar_uuid)
    steps.append(delete_step)
    if delete_step.status != "ok":
        return _incomplete_result(inventory, steps)

    return _build_decommission_result(
        inventory,
        resource_deleted=True,
        workflow_completed=True,
        dry_run=False,
        steps=tuple(steps),
    )
