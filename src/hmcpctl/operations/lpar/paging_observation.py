"""Independent configured and runtime evidence for one paging-provider switch."""

from __future__ import annotations

import csv
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from urllib.parse import urlsplit

from ...client.client_profiles import PartitionKind, read_native_profile
from ...documents.common import UOM_NS
from ...documents.partition_profile import one
from ...errors import HMCError
from ...resource_identity import is_uuid
from ...ssh import memory as native
from ...xmlutil import leaf_text
from .creation_memory import (
    PagingProvider,
    _detail,
    _identity,
    _inventory,
    _provider_uuid,
    _read,
)

if TYPE_CHECKING:
    from ...client.core import HMCClient

_LIMIT = 5 * 1024 * 1024
T = TypeVar("T")


@dataclass(frozen=True)
class PartitionIdentity:
    uuid: str
    name: str
    partition_id: int
    kind: PartitionKind


@dataclass(frozen=True)
class ConfiguredPair:
    primary: PagingProvider
    secondary: PagingProvider
    plane: Literal["current-configuration", "associated-profile"]
    profile_uuid: str | None
    profile_name: str | None
    setting_id: str | None
    etag: str | None


@dataclass(frozen=True)
class PagingObservation:
    system_uuid: str
    system_name: str
    target: PartitionIdentity
    pool_uuid: str | None
    pool_id: int | None
    providers: tuple[PagingProvider, ...]
    configured: ConfiguredPair | None
    paths: tuple[tuple[str, str], ...]
    unique_device_id: str | None
    uom_redundant: bool | None
    native_redundant: bool | None
    current_uom: PagingProvider | None
    current_native: PagingProvider | None
    state: str | None
    shared: bool | None
    provider_dedicated: tuple[bool | None, ...]
    states: tuple[tuple[str, str | None], ...]
    unavailable: tuple[str, ...]
    invalid: tuple[str, ...]


def _text(value: object) -> str:
    if isinstance(value, dict) and set(value) - {"text", "@attrs"}:
        raise ValueError("Paging scalar is not a single leaf")
    value = leaf_text(value)
    if not isinstance(value, str) or not value or len(value.encode()) > 65536:
        raise ValueError("Paging scalar is missing or malformed")
    return value


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("Paging container is missing or ambiguous")
    return value


def _integer(value: object, minimum: int = 1) -> int:
    value = _text(value)
    if not value.isascii() or not value.isdecimal() or len(value) > 4096:
        raise ValueError("Paging integer is malformed")
    number = int(value)
    if number < minimum:
        raise ValueError("Paging integer is outside its domain")
    return number


def _boolean(value: object) -> bool:
    value = _text(value)
    if value not in {"true", "false", "1", "0"}:
        raise ValueError("Paging boolean is malformed")
    return value in {"true", "1"}


def _contained_link(
    hmc: HMCClient, system: str, value: object, paths: tuple[str, ...]
) -> str:
    value = _mapping(value)
    if "link" in value:
        if set(value) - {"link", "@attrs"}:
            raise ValueError("Paging association is ambiguous")
        value = _mapping(value["link"])
    href = value.get("href")
    if (
        not isinstance(href, str)
        or not href.isprintable()
        or any(c.isspace() for c in href)
    ):
        raise ValueError("Paging association is malformed")
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
        raise ValueError("Paging association is foreign")
    parts = tuple(
        part.casefold() if is_uuid(part) else part for part in url.path.split("/")
    )
    for prefix in paths:
        expected = tuple(
            part.casefold() if is_uuid(part) else part
            for part in prefix.split("/")[:-1]
        )
        if parts[:-1] == expected and is_uuid(parts[-1]):
            return parts[-1].lower()
    raise ValueError("Paging association has the wrong contained route")


def _provider(providers: tuple[PagingProvider, ...], uuid: str) -> PagingProvider:
    matches = [p for p in providers if p.uuid == uuid.lower()]
    if len(matches) != 1:
        raise ValueError("Paging provider is outside the contained inventory")
    return matches[0]


def _rows(body: str) -> list[dict[str, str]]:
    if not isinstance(body, str) or len(body) > _LIMIT or len(body.encode()) > _LIMIT:
        raise ValueError("Paging native evidence exceeds its bound")
    if body.strip() == "No results were found.":
        return []
    rows = []
    try:
        for line in body.splitlines():
            if not line.strip():
                continue
            row = {}
            for pair in next(csv.reader([line], strict=True)):
                key, sep, value = pair.partition("=")
                if not sep or not key.strip() or key.strip() in row:
                    raise ValueError("Paging native keys are missing or duplicate")
                row[key.strip()] = value.strip()
            rows.append(row)
    except csv.Error:
        raise ValueError("Paging native record is malformed") from None
    return rows


def _assignment(
    rows: list[dict[str, str]], target: PartitionIdentity
) -> dict[str, str]:
    matches = []
    for row in rows:
        name = _text(row.get("lpar_name"))
        number = row.get("lpar_id")
        if number == "none":
            continue
        identity = _integer(number)
        if name == target.name or identity == target.partition_id:
            if name != target.name or identity != target.partition_id:
                raise ValueError("Paging native consumer identities disagree")
            matches.append(row)
    if len(matches) != 1:
        raise ValueError("Paging requires one unambiguous native device assignment")
    return matches[0]


async def _configured(
    hmc: HMCClient,
    system: str,
    target: PartitionIdentity,
    resource: dict[str, Any],
    providers: tuple[PagingProvider, ...],
) -> ConfiguredPair:
    memory = _mapping(resource.get("PartitionMemoryConfiguration"))
    keys = ("PrimaryPagingServicePartition", "SecondaryPagingServicePartition")
    metadata: tuple[Any, ...] = ("current-configuration", None, None, None, None)
    if not any(key in memory for key in keys):
        prefixes = (
            f"/rest/api/uom/{target.kind}/{target.uuid}/LogicalPartitionProfile/",
            f"/rest/api/uom/ManagedSystem/{system}/{target.kind}/{target.uuid}/LogicalPartitionProfile/",
        )
        uuid = _contained_link(
            hmc, system, resource.get("AssociatedPartitionProfile"), prefixes
        )
        profile = await read_native_profile(hmc, target.kind, target.uuid, uuid, system)
        raw_memory = one(profile.element, "ProfileMemory")
        if any(
            not node.tag.startswith("{" + UOM_NS + "}") for node in raw_memory.iter()
        ):
            raise ValueError("Paging configured memory has foreign namespaces")
        memory = _mapping(profile.resource.get("ProfileMemory"))
        metadata = (
            "associated-profile",
            profile.uuid,
            profile.name,
            profile.setting_id,
            profile.etag,
        )
    pair = [
        _provider(providers, _provider_uuid(hmc, system, memory.get(key)))
        for key in keys
    ]
    if pair[0] == pair[1]:
        raise ValueError("Paging configured primary and secondary must differ")
    return ConfiguredPair(pair[0], pair[1], *metadata)


async def _pool(
    hmc: HMCClient, system: str, inventory: tuple[dict[str, Any], ...]
) -> tuple[dict[str, Any], tuple[PagingProvider, ...], tuple[bool, ...]]:
    rows = await _read(
        hmc,
        f"ManagedSystem/{system}/SharedMemoryPool",
        "SharedMemoryPool",
        {"system_uuid": system},
    )
    if len(rows) != 1:
        raise ValueError("Paging requires exactly one identified pool")
    pool = rows[0]
    resource = _mapping(pool["Resource"])
    providers, dedicated = [], []
    for key in ("PagingServicePartitionOne", "PagingServicePartitionTwo"):
        uuid = _provider_uuid(hmc, system, resource.get(key))
        matches = [
            e
            for e in inventory
            if e["UUID"] == uuid and e["ResourceType"] == "VirtualIOServer"
        ]
        if len(matches) != 1:
            raise ValueError("Paging provider is not contained")
        expected = _identity(matches[0], "VirtualIOServer")
        detail = await _detail(hmc, "VirtualIOServer", uuid)
        if _identity(detail, "VirtualIOServer") != expected:
            raise ValueError("Paging provider detail identity differs")
        fields = _mapping(detail["Resource"])
        if _text(fields.get("PartitionType")) != "Virtual IO Server":
            raise ValueError("Paging provider type differs")
        providers.append(expected)
        dedicated.append(
            not _boolean(
                _mapping(fields.get("PartitionMemoryConfiguration")).get(
                    "SharedMemoryEnabled"
                )
            )
        )
    if providers[0] == providers[1]:
        raise ValueError("Paging providers must differ")
    ordered = sorted(
        zip(providers, dedicated, strict=True), key=lambda item: item[0].uuid
    )
    return pool, tuple(p for p, _ in ordered), tuple(d for _, d in ordered)


def _device(
    hmc: HMCClient, system: str, resource: dict[str, Any], target: PartitionIdentity
) -> dict[str, Any]:
    devices = _mapping(resource.get("PagingDevices")).get("ReservedStorageDevice")
    if isinstance(devices, dict):
        devices = [devices]
    if not isinstance(devices, list):
        raise TypeError("Paging actual device assignments are unavailable")
    matches = []
    prefixes = (
        f"/rest/api/uom/{target.kind}/",
        f"/rest/api/uom/ManagedSystem/{system}/{target.kind}/",
    )
    for raw in devices:
        device = _mapping(raw)
        uuid = _contained_link(
            hmc, system, device.get("AssociatedLogicalPartition"), prefixes
        )
        if uuid == target.uuid.lower():
            matches.append(device)
    if len(matches) != 1:
        raise ValueError("Paging requires one actual consumer-associated device")
    return matches[0]


def _native_paths(
    row: dict[str, str], providers: tuple[PagingProvider, ...]
) -> tuple[tuple[str, str], ...]:
    paths = []
    for prefix, device in (
        ("", "device_name"),
        ("redundant_", "redundant_device_name"),
    ):
        matches = [
            p
            for p in providers
            if p.partition_id == _integer(row.get(prefix + "paging_vios_id"))
            and p.name == _text(row.get(prefix + "paging_vios_name"))
        ]
        if len(matches) != 1:
            raise ValueError("Paging native provider identity is unavailable")
        paths.append((matches[0].uuid, _text(row.get(device))))
    if len({p for p, _ in paths}) != 2:
        raise ValueError("Paging native paths require distinct providers")
    return tuple(sorted(paths))


def _native_current(
    rows: list[dict[str, str]],
    target: PartitionIdentity,
    providers: tuple[PagingProvider, ...],
) -> PagingProvider:
    matches = [row for row in rows if _text(row.get("lpar_name")) == target.name]
    if len(matches) != 1:
        raise ValueError("Paging native current identity is ambiguous")
    row = matches[0]
    if "lpar_id" in row and _integer(row["lpar_id"]) != target.partition_id:
        raise ValueError("Paging native current consumer differs")
    found = [p for p in providers if p.name == _text(row.get("curr_paging_vios_name"))]
    if len(found) != 1:
        raise ValueError("Paging native current provider is unavailable")
    return found[0]


async def observe_paging(
    hmc: HMCClient, system_uuid: str, system_name: str, target: PartitionIdentity
) -> PagingObservation:
    """Retain independent readable planes; absence cannot repair another plane."""
    if not math.isfinite(hmc.config.ssh_timeout) or hmc.config.ssh_timeout <= 0:
        raise ValueError("Paging requires a finite positive ssh_timeout")
    system_uuid = system_uuid.lower()
    unavailable: list[str] = []
    invalid: list[str] = []

    async def load(label: str, call: Awaitable[T]) -> T | None:
        try:
            return await call
        except (HMCError, ValueError, TypeError):
            unavailable.append(label)
            return None

    def fact(label: str, call: Callable[[], T]) -> T | None:
        try:
            return call()
        except (ValueError, TypeError, KeyError):
            unavailable.append(label)
            return None

    system = await load("system", _detail(hmc, "ManagedSystem", system_uuid))
    if system is not None:
        name = fact("system-name", lambda: _text(system["Resource"].get("SystemName")))
        ams = fact(
            "ams",
            lambda: _boolean(
                _mapping(system["Resource"].get("AssociatedSystemCapabilities")).get(
                    "ActiveMemorySharingCapable"
                )
            ),
        )
        if name is not None and name != system_name:
            invalid.append("system-name-changed")
        if ams is False:
            invalid.append("ams-disabled")
    inventory = await load("contained-inventory", _inventory(hmc, system_uuid))
    known_providers = tuple(
        _identity(entry, "VirtualIOServer")
        for entry in inventory or ()
        if entry["ResourceType"] == "VirtualIOServer"
    )
    detail = await load("consumer-detail", _detail(hmc, target.kind, target.uuid))
    resource = _mapping(detail["Resource"]) if detail else {}
    identity = fact("consumer-identity", lambda: _identity(detail, target.kind))
    if identity is not None and identity != PagingProvider(
        target.uuid.lower(), target.name, target.partition_id
    ):
        invalid.append("consumer-identity-changed")
    if inventory is not None and not any(
        e["UUID"] == target.uuid.lower() and e["ResourceType"] == target.kind
        for e in inventory
    ):
        invalid.append("consumer-not-contained")
    environment = fact("consumer-type", lambda: _text(resource.get("PartitionType")))
    allowed = (
        {"Virtual IO Server"}
        if target.kind == "VirtualIOServer"
        else {"AIX/Linux", "OS400"}
    )
    if environment is not None and environment not in allowed:
        invalid.append("consumer-kind-type-disagree")
    state = fact("consumer-state", lambda: _text(resource.get("PartitionState")))
    shared = fact(
        "consumer-shared",
        lambda: _boolean(
            _mapping(resource.get("PartitionMemoryConfiguration")).get(
                "SharedMemoryEnabled"
            )
        ),
    )
    if state is not None and state != "running":
        invalid.append("consumer-not-running")
    if shared is False:
        invalid.append("consumer-not-shared")
    if "IsOperationInProgress" in resource:
        progress = fact(
            "operation-in-progress", lambda: _boolean(resource["IsOperationInProgress"])
        )
        if progress is True:
            invalid.append("operation-in-progress")
    pool_data = await load("pool-providers", _pool(hmc, system_uuid, inventory or ()))
    pool, providers, dedicated = pool_data or ({}, (), ())
    if any(value is False for value in dedicated):
        invalid.append("provider-not-dedicated")
    pool_id = fact("pool-id", lambda: _integer(pool["Resource"].get("PoolID"), 0))
    configured = await load(
        "configured-pair",
        _configured(hmc, system_uuid, target, resource, known_providers),
    )
    device = fact(
        "assigned-device", lambda: _device(hmc, system_uuid, pool["Resource"], target)
    )
    uom_redundant = (
        fact("uom-redundancy", lambda: _boolean(device.get("IsRedundant")))
        if device
        else None
    )
    if uom_redundant is False:
        invalid.append("uom-not-redundant")
    current_uom = fact(
        "uom-current",
        lambda: _provider(
            known_providers,
            _provider_uuid(
                hmc,
                system_uuid,
                _mapping(resource.get("PartitionMemoryConfiguration")).get(
                    "CurrentPagingServicePartition"
                ),
            ),
        ),
    )
    current_body = await load(
        "native-current", native.read_lpar_paging_memory(hmc.config, system_name)
    )
    device_body = await load(
        "native-devices", native.read_lpar_paging_devices(hmc.config, system_name)
    )
    current_native = (
        fact(
            "native-current",
            lambda: _native_current(_rows(current_body), target, known_providers),
        )
        if current_body is not None
        else None
    )
    assignment = (
        fact("native-assignment", lambda: _assignment(_rows(device_body), target))
        if device_body is not None
        else None
    )
    paths = (
        fact("native-paths", lambda: _native_paths(assignment, known_providers))
        if assignment is not None
        else None
    )
    native_redundant = (
        fact("native-redundancy", lambda: _boolean(assignment.get("is_redundant")))
        if assignment is not None
        else None
    )
    if native_redundant is False:
        invalid.append("native-not-redundant")
    if device and paths:
        names = fact(
            "uom-device-names",
            lambda: sorted(
                (
                    _text(device.get("DeviceName")),
                    _text(device.get("RedundantDeviceName")),
                )
            ),
        )
        if names is not None and names != sorted(name for _, name in paths):
            invalid.append("device-names-disagree")
    if paths and {uuid for uuid, _ in paths} != {p.uuid for p in providers}:
        invalid.append("runtime-pool-pair-disagree")
    if any(p is not None and p not in providers for p in (current_uom, current_native)):
        invalid.append("current-outside-pair")
    if current_native and current_uom and current_native != current_uom:
        invalid.append("current-readers-disagree")
    if configured and {configured.primary, configured.secondary} != set(providers):
        invalid.append("configured-runtime-pair-disagree")
    udid = (
        fact("unique-device-id", lambda: _text(device["UniqueDeviceID"]))
        if device and "UniqueDeviceID" in device
        else None
    )
    states = []
    for label, fields, keys in (
        ("uom", device or {}, ("DeviceState", "RedundantDeviceState")),
        ("native", assignment or {}, ("state", "redundant_state")),
    ):
        for key in keys:
            states.append(
                (
                    label + "." + key,
                    fact("service-state", lambda value=fields[key]: _text(value))
                    if key in fields
                    else None,
                )
            )
    return PagingObservation(
        system_uuid,
        system_name,
        PartitionIdentity(
            identity.uuid, identity.name, identity.partition_id, target.kind
        )
        if identity
        else target,
        pool.get("UUID"),
        pool_id,
        providers,
        configured,
        paths or (),
        udid,
        uom_redundant,
        native_redundant,
        current_uom,
        current_native,
        state,
        shared,
        dedicated,
        tuple(states),
        tuple(dict.fromkeys(unavailable)),
        tuple(invalid),
    )
