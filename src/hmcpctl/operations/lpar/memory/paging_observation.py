"""Independent configured and runtime evidence for one paging-provider switch."""

from __future__ import annotations

import csv
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeVar
from urllib.parse import urlsplit

from hmcpctl.client.client_profiles import PartitionKind, read_native_profile
from hmcpctl.documents.common import UOM_NS
from hmcpctl.documents.partition_profile import one
from hmcpctl.errors import HMCError, HMCTLSFingerprintError
from hmcpctl.operations.lpar.memory.creation import (
    PagingProvider,
    _detail,
    _identity,
    _inventory,
    _provider_uuid,
    _read,
)
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh import memory as native
from hmcpctl.xmlutil import leaf_text

if TYPE_CHECKING:
    from hmcpctl.client.core import HMCClient

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


async def _pool(hmc: HMCClient, system: str) -> dict[str, Any]:
    rows = await _read(
        hmc,
        f"ManagedSystem/{system}/SharedMemoryPool",
        "SharedMemoryPool",
        {"system_uuid": system},
    )
    if len(rows) != 1:
        raise ValueError("Paging requires exactly one identified pool")
    return rows[0]


async def _provider_dedicated(hmc: HMCClient, provider: PagingProvider) -> bool:
    detail = await _detail(hmc, "VirtualIOServer", provider.uuid)
    if _identity(detail, "VirtualIOServer") != provider:
        raise ValueError("Paging provider detail identity differs")
    fields = _mapping(detail["Resource"])
    if _text(fields.get("PartitionType")) != "Virtual IO Server":
        raise ValueError("Paging provider type differs")
    return not _boolean(
        _mapping(fields.get("PartitionMemoryConfiguration")).get("SharedMemoryEnabled")
    )


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


def _reconcile(
    observation: PagingObservation, device_names: list[str] | None
) -> PagingObservation:
    """Compare independent planes without changing their collected evidence."""
    invalid = list(observation.invalid)
    if device_names is not None and device_names != sorted(
        name for _, name in observation.paths
    ):
        invalid.append("device-names-disagree")

    if (
        len(set(observation.providers)) == 2
        and observation.paths
        and {uuid for uuid, _ in observation.paths}
        != {p.uuid for p in observation.providers}
    ):
        invalid.append("runtime-pool-pair-disagree")

    if len(set(observation.providers)) == 2 and any(
        p is not None and p not in observation.providers
        for p in (observation.current_uom, observation.current_native)
    ):
        invalid.append("current-outside-pair")

    if (
        observation.current_native
        and observation.current_uom
        and observation.current_native != observation.current_uom
    ):
        invalid.append("current-readers-disagree")

    if (
        len(set(observation.providers)) == 2
        and observation.configured
        and {observation.configured.primary, observation.configured.secondary}
        != set(observation.providers)
    ):
        invalid.append("configured-runtime-pair-disagree")
    return replace(observation, invalid=tuple(invalid))


@dataclass
class _PagingDiagnostics:
    unavailable: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)

    async def load(self, label: str, call: Awaitable[T]) -> T | None:
        try:
            return await call
        except HMCTLSFingerprintError:
            raise
        except (HMCError, ValueError, TypeError):
            self.unavailable.append(label)
            return None

    def fact(self, label: str, call: Callable[[], T]) -> T | None:
        try:
            return call()
        except (ValueError, TypeError, KeyError):
            self.unavailable.append(label)
            return None


class _PoolFacts(NamedTuple):
    entry: dict[str, Any]
    id: int | None
    providers: tuple[PagingProvider, ...]
    dedicated: tuple[bool | None, ...]


async def _observe_pool(
    hmc: HMCClient,
    system_uuid: str,
    known_providers: tuple[PagingProvider, ...],
    diagnostics: _PagingDiagnostics,
) -> _PoolFacts:
    pool = await diagnostics.load("pool", _pool(hmc, system_uuid)) or {}
    observed_providers = []
    for key in ("PagingServicePartitionOne", "PagingServicePartitionTwo"):
        provider = diagnostics.fact(
            "pool-providers",
            lambda key=key: _provider(
                known_providers,
                _provider_uuid(
                    hmc, system_uuid, _mapping(pool.get("Resource")).get(key)
                ),
            ),
        )
        if provider is not None:
            dedicated = await diagnostics.load(
                "provider-detail", _provider_dedicated(hmc, provider)
            )
            observed_providers.append((provider, dedicated))
    ordered = sorted(observed_providers, key=lambda item: item[0].uuid)
    providers = tuple(provider for provider, _ in ordered)
    dedicated = tuple(value for _, value in ordered)
    if len(providers) == 2 and providers[0] == providers[1]:
        diagnostics.invalid.append("pool-providers-not-distinct")
    if any(value is False for value in dedicated):
        diagnostics.invalid.append("provider-not-dedicated")
    pool_id = diagnostics.fact(
        "pool-id", lambda: _integer(pool["Resource"].get("PoolID"), 0)
    )
    return _PoolFacts(pool, pool_id, providers, dedicated)


class _NativeFacts(NamedTuple):
    current: PagingProvider | None
    assignment: dict[str, str] | None
    paths: tuple[tuple[str, str], ...] | None
    redundant: bool | None


async def _observe_native(
    hmc: HMCClient,
    system_name: str,
    target: PartitionIdentity,
    known_providers: tuple[PagingProvider, ...],
    diagnostics: _PagingDiagnostics,
) -> _NativeFacts:
    current_body = await diagnostics.load(
        "native-current", native.read_lpar_paging_memory(hmc.config, system_name)
    )
    device_body = await diagnostics.load(
        "native-devices", native.read_lpar_paging_devices(hmc.config, system_name)
    )
    current_native = (
        diagnostics.fact(
            "native-current",
            lambda: _native_current(_rows(current_body), target, known_providers),
        )
        if current_body is not None
        else None
    )
    assignment = (
        diagnostics.fact(
            "native-assignment", lambda: _assignment(_rows(device_body), target)
        )
        if device_body is not None
        else None
    )
    paths = (
        diagnostics.fact(
            "native-paths", lambda: _native_paths(assignment, known_providers)
        )
        if assignment is not None
        else None
    )
    native_redundant = (
        diagnostics.fact(
            "native-redundancy", lambda: _boolean(assignment.get("is_redundant"))
        )
        if assignment is not None
        else None
    )
    if native_redundant is False:
        diagnostics.invalid.append("native-not-redundant")
    return _NativeFacts(current_native, assignment, paths, native_redundant)


async def observe_paging(
    hmc: HMCClient, system_uuid: str, system_name: str, target: PartitionIdentity
) -> PagingObservation:
    """Retain independent readable planes; absence cannot repair another plane."""
    if not math.isfinite(hmc.config.ssh_timeout) or hmc.config.ssh_timeout <= 0:
        raise ValueError("Paging requires a finite positive ssh_timeout")
    system_uuid = system_uuid.lower()
    diagnostics = _PagingDiagnostics()

    system = await diagnostics.load(
        "system", _detail(hmc, "ManagedSystem", system_uuid)
    )
    if system is not None:
        name = diagnostics.fact(
            "system-name", lambda: _text(system["Resource"].get("SystemName"))
        )
        ams = diagnostics.fact(
            "ams",
            lambda: _boolean(
                _mapping(system["Resource"].get("AssociatedSystemCapabilities")).get(
                    "ActiveMemorySharingCapable"
                )
            ),
        )
        if name is not None and name != system_name:
            diagnostics.invalid.append("system-name-changed")
        if ams is False:
            diagnostics.invalid.append("ams-disabled")
    inventory = await diagnostics.load(
        "contained-inventory", _inventory(hmc, system_uuid)
    )
    known_providers = tuple(
        _identity(entry, "VirtualIOServer")
        for entry in inventory or ()
        if entry["ResourceType"] == "VirtualIOServer"
    )
    contained = inventory is not None and any(
        entry["UUID"] == target.uuid.lower() and entry["ResourceType"] == target.kind
        for entry in inventory
    )
    detail = (
        await diagnostics.load(
            "consumer-detail", _detail(hmc, target.kind, target.uuid)
        )
        if contained
        else None
    )
    if not contained:
        diagnostics.unavailable.append("consumer-detail")
    resource = _mapping(detail["Resource"]) if detail else {}
    identity = diagnostics.fact(
        "consumer-identity", lambda: _identity(detail, target.kind)
    )
    if identity is not None and identity != PagingProvider(
        target.uuid.lower(), target.name, target.partition_id
    ):
        diagnostics.invalid.append("consumer-identity-changed")
    if inventory is not None and not contained:
        diagnostics.invalid.append("consumer-not-contained")
    environment = diagnostics.fact(
        "consumer-type", lambda: _text(resource.get("PartitionType"))
    )
    allowed = (
        {"Virtual IO Server"}
        if target.kind == "VirtualIOServer"
        else {"AIX/Linux", "OS400"}
    )
    if environment is not None and environment not in allowed:
        diagnostics.invalid.append("consumer-kind-type-disagree")
    state = diagnostics.fact(
        "consumer-state", lambda: _text(resource.get("PartitionState"))
    )
    shared = diagnostics.fact(
        "consumer-shared",
        lambda: _boolean(
            _mapping(resource.get("PartitionMemoryConfiguration")).get(
                "SharedMemoryEnabled"
            )
        ),
    )
    if state is not None and state != "running":
        diagnostics.invalid.append("consumer-not-running")
    if shared is False:
        diagnostics.invalid.append("consumer-not-shared")
    if "IsOperationInProgress" in resource:
        progress = diagnostics.fact(
            "operation-in-progress", lambda: _boolean(resource["IsOperationInProgress"])
        )
        if progress is True:
            diagnostics.invalid.append("operation-in-progress")
    pool = await _observe_pool(hmc, system_uuid, known_providers, diagnostics)
    configured = await diagnostics.load(
        "configured-pair",
        _configured(hmc, system_uuid, target, resource, known_providers),
    )
    device = diagnostics.fact(
        "assigned-device",
        lambda: _device(hmc, system_uuid, pool.entry["Resource"], target),
    )
    uom_redundant = (
        diagnostics.fact("uom-redundancy", lambda: _boolean(device.get("IsRedundant")))
        if device
        else None
    )
    if uom_redundant is False:
        diagnostics.invalid.append("uom-not-redundant")
    current_uom = diagnostics.fact(
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
    native_facts = await _observe_native(
        hmc, system_name, target, known_providers, diagnostics
    )
    device_names = None
    if device and native_facts.paths:
        device_names = diagnostics.fact(
            "uom-device-names",
            lambda: sorted(
                (
                    _text(device.get("DeviceName")),
                    _text(device.get("RedundantDeviceName")),
                )
            ),
        )
    udid = (
        diagnostics.fact("unique-device-id", lambda: _text(device["UniqueDeviceID"]))
        if device and "UniqueDeviceID" in device
        else None
    )
    states = []
    for label, fields, keys in (
        ("uom", device or {}, ("DeviceState", "RedundantDeviceState")),
        ("native", native_facts.assignment or {}, ("state", "redundant_state")),
    ):
        for key in keys:
            states.append(
                (
                    label + "." + key,
                    diagnostics.fact(
                        "service-state", lambda value=fields[key]: _text(value)
                    )
                    if key in fields
                    else None,
                )
            )
    observation = PagingObservation(
        system_uuid=system_uuid,
        system_name=system_name,
        target=PartitionIdentity(
            identity.uuid, identity.name, identity.partition_id, target.kind
        )
        if identity
        else target,
        pool_uuid=pool.entry.get("UUID"),
        pool_id=pool.id,
        providers=pool.providers,
        configured=configured,
        paths=native_facts.paths or (),
        unique_device_id=udid,
        uom_redundant=uom_redundant,
        native_redundant=native_facts.redundant,
        current_uom=current_uom,
        current_native=native_facts.current,
        state=state,
        shared=shared,
        provider_dedicated=pool.dedicated,
        states=tuple(states),
        unavailable=tuple(dict.fromkeys(diagnostics.unavailable)),
        invalid=tuple(diagnostics.invalid),
    )
    return _reconcile(observation, device_names)
