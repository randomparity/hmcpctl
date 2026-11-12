"""Counted transport and closed snapshots for finite VIOS source diagnostics."""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from hmcpctl.client.client_profiles import read_native_profile
from hmcpctl.client.core import HMCClient
from hmcpctl.documents.partition_profile import ATOM, _records, parse_profile
from hmcpctl.ssh.transport import run_hmc_command
from hmcpctl.xmlutil import leaf_text

from .packets import (
    MIB,
    Held,
    _collection_entry,
    _collection_link,
    _collection_media,
    _json,
    _private,
    _root,
    _text,
    _uuid,
    encode,
)

BASE_FIELDS = (
    "PartitionID",
    "PartitionName",
    "PartitionType",
    "PartitionState",
    "Description",
    "DefaultProfileName",
    "LastActivatedProfile",
    "AssociatedPartitionProfile",
    "PartitionMemoryConfiguration",
    "PartitionProcessorConfiguration",
    "PartitionProfiles",
)

IO_FIELDS = (
    "PartitionIOConfiguration",
    "ClientNetworkAdapters",
    "HostEthernetAdapterLogicalPorts",
    "SRIOVEthernetLogicalPorts",
    "SRIOVRoCELogicalPorts",
    "SharedEthernetAdapters",
    "TrunkAdapters",
    "VirtualNICBackingDevices",
    "PhysicalVolumes",
    "StoragePools",
    "MediaRepositories",
)

MAPPINGS = ("VirtualSCSIMappings", "VirtualFibreChannelMappings")


def _current(p: dict) -> None:
    if not p["window"]["not_before"] <= time.time() < p["window"]["expires_at"]:
        raise Held("root window is not current")


def _receipt(p: dict, stage: str, packet_sha: str, output: Path) -> None:
    window = Path(p["window"]["directory"])
    if window != window.resolve() or output != output.resolve():
        raise Held("canonical absolute window/output paths required")
    _private(window, directory=True)
    root = _root()
    if window == root or root in window.parents or output != window / stage:
        raise Held("exact external private stage destination required")
    if output.exists():
        raise Held("stage output already exists")
    fd = os.open(
        window / (stage + ".claim"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(packet_sha.encode())
        stream.flush()
        os.fsync(stream.fileno())
    output.mkdir(mode=0o700)
    fd = os.open(window, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Budget:
    def __init__(self, stage: str, p: dict, limits: tuple[int, int, int, int]):
        self.stage = stage
        self.maximum = limits
        self.used = [0, 0, 0]
        self.started = time.monotonic()
        self.deadline = self.started + self.maximum[3]
        self.window = p["window"]["expires_at"]
        self.block: dict | None = None
        self.calls: list[dict] = []

    @contextmanager
    def section(self, name: str, rest: int, ssh: int, byte_count: int, seconds: int):
        if self.block is not None:
            raise Held("nested budget sections are invalid")
        self.block = {
            "name": name,
            "limit": [rest, ssh, byte_count],
            "used": [0, 0, 0],
            "deadline": min(self.deadline, time.monotonic() + seconds),
        }
        try:
            yield
        finally:
            self.block = None

    def remaining(self, kind: int) -> int:
        if self.block is None:
            raise Held("transport outside a declared stage block")
        return min(
            self.maximum[kind] - self.used[kind],
            self.block["limit"][kind] - self.block["used"][kind],
        )

    def timeout(self, maximum: int, *, session_close: bool = False) -> float:
        if self.block is None:
            raise Held("transport outside budget section")
        left = min(self.deadline, self.block["deadline"]) - time.monotonic()
        if not session_close:
            left = min(left, self.window - time.time())
        if left <= 0:
            raise Held("physical deadline expired")
        return min(float(maximum), left)

    def dispatch(self, kind: int, label: str) -> None:
        if self.remaining(kind) < 1 or self.remaining(2) < 1:
            raise Held("physical request or byte budget exhausted")
        self.used[kind] += 1
        assert self.block is not None
        self.block["used"][kind] += 1
        self.calls.append({"kind": "REST" if kind == 0 else "SSH", "label": label})

    def charge(self, size: int) -> None:
        if size > self.remaining(2):
            raise Held("response exceeds remaining retained-byte allowance")
        self.used[2] += size
        assert self.block is not None
        self.block["used"][2] += size

    def refund(self, size: int) -> None:
        if self.block is None or size < 0 or size > self.block["used"][2]:
            raise Held("invalid byte allowance refund")
        self.used[2] -= size
        self.block["used"][2] -= size


def _collection_route(path: str) -> tuple[str, int] | None:
    bare = path.split("?", 1)[0].split("#", 1)[0]
    parts = bare.split("/")
    allowed = {
        ("ManagedSystem", "LogicalPartition"): 128,
        ("ManagedSystem", "VirtualIOServer"): 9,
        ("ManagedSystem", "SharedMemoryPool"): 4,
        ("VirtualIOServer", "LogicalPartitionProfile"): 33,
    }
    if bare == "/rest/api/uom/ManagementConsole":
        result = ("ManagementConsole", 1)
    elif (
        len(parts) == 7
        and parts[:4] == ["", "rest", "api", "uom"]
        and (parts[4], parts[6]) in allowed
    ):
        _uuid(parts[5])
        result = (parts[6], allowed[(parts[4], parts[6])])
    else:
        return None
    if path != bare:
        raise Held("collection queries/fragments are unsupported")
    return result


def _collection_response(path: str, response, config) -> None:
    route = _collection_route(path)
    if route is None:
        return
    kind, limit = route
    if "link" in response.headers or "content-range" in response.headers:
        raise Held("collection continuation/range headers are unsupported")
    length = response.headers.get("content-length")
    if length is not None and (
        not length.isascii()
        or not length.isdecimal()
        or int(length) != len(response.content)
    ):
        raise Held("collection declared length contradicts its complete body")
    if response.status_code == 204:
        if (
            kind != "SharedMemoryPool"
            or response.content
            or response.headers.get("content-type") not in {None, "text/plain"}
        ):
            raise Held("unestablished or contradictory empty-collection sentinel")
        return
    if response.status_code != 200 or not response.content.strip():
        raise Held("collection is unavailable or HTTP200 body is empty")
    _collection_media(response.headers.get("content-type"), kind)
    try:
        root = DET.fromstring(response.content)
    except (DET.ParseError, DefusedXmlException) as error:
        raise Held("collection XML is incomplete or unsafe") from error
    atom = "{" + ATOM + "}"
    if root.tag != atom + "feed" or root.attrib or (root.text or "").strip():
        raise Held("one complete Atom collection envelope required")
    entries = root.findall(atom + "entry")
    if len(entries) > limit:
        raise Held("physical collection row bound exceeded")
    scalars = {atom + name for name in ("id", "title", "updated", "generator")}
    seen, self_links = set(), 0
    for child in root:
        if (child.tail or "").strip():
            raise Held("collection has mixed envelope text")
        if child.tag == atom + "entry":
            _collection_entry(child, kind, config, path)
        elif child.tag in scalars:
            if child.tag in seen or child.attrib or len(child):
                raise Held("collection metadata scalar is ambiguous")
            seen.add(child.tag)
        elif child.tag == atom + "link":
            relation = child.get("rel", "").upper()
            if relation not in {"SELF", "MANAGEMENT_CONSOLE"}:
                raise Held("collection continuation/unknown relation")
            _collection_link(
                child, config, path, console=relation == "MANAGEMENT_CONSOLE"
            )
            self_links += relation == "SELF"
        else:
            raise Held("collection extension/completeness indicator is unsupported")
    if self_links != 1:
        raise Held("one exact collection SELF link required")


class CountedHMC(HMCClient):
    def __init__(self, config, budget: Budget):
        super().__init__(config)
        self.budget = budget
        self.login_sent = False
        self.logout_sent = False
        self.snapshot_responses: list[dict] | None = None
        self.failed_snapshot: list[dict] = []

    async def _request(self, method: str, path: str, **kwargs):
        close = method == "DELETE" and path == "/rest/api/web/Logon"
        if method == "PUT" and path == "/rest/api/web/Logon":
            if self.login_sent:
                raise Held("second logon refused")
            self.login_sent = True
        elif close:
            if self.logout_sent:
                raise Held("second logout refused")
            self.logout_sent = True
        elif method != "GET":
            raise Held("unallocated REST mutation refused")
        return await self._counted_request(method, path, **kwargs)

    async def _counted_request(self, method: str, path: str, **kwargs):
        close = method == "DELETE" and path == "/rest/api/web/Logon"
        seconds = self.budget.timeout(20, session_close=close)
        self.budget.dispatch(0, method + " " + path.split("?")[0])
        original = self.config
        cap = min(MIB, self.budget.remaining(2))
        self.budget.charge(cap)
        self.config = original.model_copy(
            update={"max_response_bytes": cap, "timeout": seconds}
        )
        try:
            async with asyncio.timeout(seconds):
                response = await super()._request(method, path, **kwargs)
            self.budget.refund(cap - len(response.content))
            if method == "GET" and self.snapshot_responses is not None:
                self.snapshot_responses.append(
                    {
                        "path": path,
                        "status": response.status_code,
                        "etag": response.headers.get("ETag"),
                        "body": response.content.decode("utf-8"),
                        "content_type": response.headers.get("content-type"),
                        "content_length": response.headers.get("content-length"),
                        "link": response.headers.get("link"),
                        "content_range": response.headers.get("content-range"),
                    }
                )
            if method == "GET":
                _collection_response(path, response, self.config)
            return response
        finally:
            self.config = original


async def _counted_ssh(hmc: CountedHMC, command: str, kind: str, maximum: int) -> str:
    seconds = hmc.budget.timeout(maximum)
    cap = min(65536, hmc.budget.remaining(2))
    if cap <= 10:
        raise Held("native output allowance exhausted")
    hmc.budget.dispatch(1, kind)
    hmc.budget.charge(cap)
    async with asyncio.timeout(seconds):
        result = await run_hmc_command(
            hmc.config.model_copy(update={"ssh_timeout": seconds}),
            command,
            include_stderr=True,
            max_output_bytes=cap - 10,
        )
    hmc.budget.refund(cap - len(result.encode()))
    return result


async def _read_mode(hmc: CountedHMC, p: dict) -> str:
    system = shlex.quote(p["system"]["name"])
    command = f"lscomgmt -m {system} -F is_controller"
    return await _counted_ssh(hmc, command, "mode", 20)


def _projection(resource: dict, fields: tuple[str, ...]) -> dict:
    return {
        key: {"present": key in resource, "value": resource.get(key)} for key in fields
    }


def _entry(entry: object, kind: str) -> tuple[str, dict]:
    if not isinstance(entry, dict) or entry.get("ResourceType") != kind:
        raise Held("resource kind is unavailable or differs")
    identity = _uuid(entry.get("UUID"))
    resource = entry.get("Resource")
    if not isinstance(resource, dict):
        raise Held("resource body is unavailable")
    return identity, resource


def _inventory(feed: object, kind: str, limit: int) -> dict[str, dict]:
    if not isinstance(feed, list) or len(feed) > limit:
        raise Held("complete inventory exceeds its row bound")
    rows = {}
    names = set()
    ids = set()
    for entry in feed:
        identity, resource = _entry(entry, kind)
        name = _text(leaf_text(resource.get("PartitionName")), 128)
        partition_id = _text(leaf_text(resource.get("PartitionID")), 16)
        if not partition_id.isdecimal() or int(partition_id) < 1:
            raise Held("positive partition ID unavailable")
        _text(leaf_text(resource.get("PartitionState")), 128)
        partition_type = _text(leaf_text(resource.get("PartitionType")), 64)
        if kind == "VirtualIOServer" and partition_type != "Virtual IO Server":
            raise Held("VIOS type differs")
        if identity in rows or name in names or int(partition_id) in ids:
            raise Held("partition identity is ambiguous")
        rows[identity] = entry
        names.add(name)
        ids.add(int(partition_id))
    return rows


def _numbers(resource: dict, container: str, fields: tuple[str, ...]) -> dict:
    section = resource.get(container)
    if not isinstance(section, dict):
        raise Held("capacity container unavailable")
    result = {}
    for key in fields:
        raw = _text(leaf_text(section.get(key)), 64)
        try:
            number = Decimal(raw)
        except InvalidOperation as error:
            raise Held("capacity is not numeric") from error
        if not number.is_finite() or number < 0:
            raise Held("capacity is nonfinite or negative")
        result[key] = str(number)
    return result


def _system(entry: object, p: dict) -> dict:
    identity, resource = _entry(entry, "ManagedSystem")
    if (
        identity != p["system"]["uuid"]
        or leaf_text(resource.get("SystemName")) != p["system"]["name"]
    ):
        raise Held("selected system identity differs")
    if leaf_text(resource.get("State")) != "operating":
        raise Held("selected system is not operating")
    flags = tuple(
        leaf_text(resource.get(k))
        for k in (
            "IsClassicHMCManagement",
            "IsHMCPowerVMManagementController",
            "IsPowerVMManagementController",
        )
    )
    if flags != ("true", "false", "false"):
        raise Held("classic system authority unavailable")
    caps = resource.get("AssociatedSystemCapabilities")
    if not isinstance(caps, dict) or any(
        leaf_text(caps.get(k)) != "true"
        for k in (
            "VirtualIOServerCapable",
            "ActiveMemoryExpansionCapable",
        )
    ):
        raise Held("VIOS/AME capabilities unavailable")
    memory = _numbers(
        resource,
        "AssociatedSystemMemoryConfiguration",
        (
            "MemoryRegionSize",
            "ConfigurableSystemMemory",
            "CurrentAvailableSystemMemory",
        ),
    )
    cpu = _numbers(
        resource,
        "AssociatedSystemProcessorConfiguration",
        (
            "ConfigurableSystemProcessorUnits",
            "CurrentAvailableSystemProcessorUnits",
            "MinimumProcessorUnitsPerVirtualProcessor",
        ),
    )
    if Decimal(memory["CurrentAvailableSystemMemory"]) > Decimal(
        memory["ConfigurableSystemMemory"]
    ):
        raise Held("memory capacity contradicts total")
    if Decimal(cpu["CurrentAvailableSystemProcessorUnits"]) > Decimal(
        cpu["ConfigurableSystemProcessorUnits"]
    ):
        raise Held("CPU capacity contradicts total")
    return {
        "uuid": identity,
        "name": leaf_text(resource["SystemName"]),
        "mode": list(flags),
        "firmware": _text(leaf_text(resource.get("SystemFirmware")), 256),
        "capabilities": _projection(
            caps, ("VirtualIOServerCapable", "ActiveMemoryExpansionCapable")
        ),
        "memory": memory,
        "cpu": cpu,
    }


def _admit_capacity(system: dict) -> None:
    m, c = system["memory"], system["cpu"]
    region = Decimal(m["MemoryRegionSize"])
    if region <= 0 or region != region.to_integral_value():
        raise Held("memory region is not a positive integer")
    if any(Decimal(v) % region for v in (1024, 2048, 4096)):
        raise Held("fixed memory tuple is not region-aligned")
    if (
        Decimal(m["ConfigurableSystemMemory"]) < 4096
        or Decimal(m["CurrentAvailableSystemMemory"]) < 2048
    ):
        raise Held("fixed memory tuple exceeds observed capacity")
    if Decimal(c["ConfigurableSystemProcessorUnits"]) < 1 or Decimal(
        c["CurrentAvailableSystemProcessorUnits"]
    ) < Decimal("0.5"):
        raise Held("fixed processor tuple exceeds observed capacity")
    if not 0 < Decimal(c["MinimumProcessorUnitsPerVirtualProcessor"]) <= Decimal("0.1"):
        raise Held("fixed processor tuple is below the advertised minimum")


def _profile_semantic(raw: str) -> dict:
    # parse_profile applies existing XML identity/duplicate/depth safety first.
    parse_profile(raw)
    root = DET.fromstring(raw)
    if root.tag == "{" + ATOM + "}entry":
        publication = root.findall("{" + ATOM + "}published")
        if len(publication) > 1 or any(len(n) for n in publication):
            raise Held("outer publication scalar ambiguous")
        for node in publication:
            node.text = "<outer-publication>"
    return _records(root, False)


def _profile_links(resource: dict, hmc: CountedHMC, parent: str) -> set[str]:
    from urllib.parse import urlsplit

    if "PartitionProfiles" not in resource:
        raise Held("profile association inventory unavailable")
    section = resource["PartitionProfiles"]
    if section == "":
        return set()
    if not isinstance(section, dict):
        raise Held("profile association inventory malformed")
    links = section.get("link", [])
    if isinstance(links, dict):
        links = [links]
    if not isinstance(links, list) or len(links) > 33:
        raise Held("profile association bound exceeded")
    identities = set()
    for link in links:
        if not isinstance(link, dict) or not isinstance(link.get("href"), str):
            raise Held("profile association link malformed")
        u = urlsplit(link["href"])
        bits = u.path.split("/")
        if (
            u.scheme != "https"
            or u.hostname != hmc.config.host
            or (u.port or 443) != hmc.config.port
            or u.username
            or u.password
            or u.query
            or u.fragment
            or len(bits) != 8
            or bits[1:4] != ["rest", "api", "uom"]
            or bits[4] != "VirtualIOServer"
            or _uuid(bits[5]) != parent
            or bits[6] != "LogicalPartitionProfile"
        ):
            raise Held("foreign profile link")
        identity = _uuid(bits[7])
        if identity in identities:
            raise Held("duplicate profile association")
        identities.add(identity)
    return identities


def _pool_rows(feed: object) -> dict:
    if not isinstance(feed, list) or len(feed) > 4:
        raise Held("memory pool inventory bound exceeded")
    rows = {}
    ids = set()
    for entry in feed:
        identity, resource = _entry(entry, "SharedMemoryPool")
        pool_id = _text(leaf_text(resource.get("PoolID")), 16)
        if not pool_id.isdecimal() or int(pool_id) in ids or identity in rows:
            raise Held("pool identity ambiguous")
        if any(
            k not in resource
            for k in ("PagingServicePartitionOne", "PagingServicePartitionTwo")
        ):
            raise Held("existing pool provider associations unavailable")
        rows[identity] = resource
        ids.add(int(pool_id))
    return rows


def _closing(rows: dict) -> dict:
    return {
        key: _projection(
            entry["Resource"],
            (
                "PartitionID",
                "PartitionName",
                "PartitionType",
                "PartitionState",
                "PartitionProfiles",
            ),
        )
        for key, entry in rows.items()
    }


async def snapshot(hmc: CountedHMC, p: dict) -> dict:
    hmc.snapshot_responses = []
    try:
        return await _snapshot(hmc, p)
    except BaseException:
        hmc.failed_snapshot = list(hmc.snapshot_responses)
        raise
    finally:
        hmc.snapshot_responses = None


async def _snapshot(hmc: CountedHMC, p: dict) -> dict:
    system_uuid = p["system"]["uuid"]
    with hmc.budget.section("snapshot", 68, 1, 2 * MIB, 120):
        console = await hmc.get_console_info()
        _, console_resource = _entry(console, "ManagementConsole")
        version = console_resource.get("VersionInfo")
        if not isinstance(version, dict) or not version:
            raise Held("HMC version evidence unavailable")
        system = _system(await hmc.get_uom("ManagedSystem", system_uuid), p)
        if (await _read_mode(hmc, p)).splitlines() != ["null"]:
            raise Held("native classic authority unavailable")
        regular = _inventory(
            await hmc.list_child("ManagedSystem", system_uuid, "LogicalPartition"),
            "LogicalPartition",
            128,
        )
        vios = _inventory(
            await hmc.list_child("ManagedSystem", system_uuid, "VirtualIOServer"),
            "VirtualIOServer",
            9,
        )
        if set(regular) & set(vios):
            raise Held("partition inventories overlap")
        names = [
            leaf_text(r["Resource"]["PartitionName"])
            for r in [*regular.values(), *vios.values()]
        ]
        ids = [
            int(_text(leaf_text(r["Resource"].get("PartitionID")), 16))
            for r in [*regular.values(), *vios.values()]
        ]
        if len(set(names)) != len(names) or len(set(ids)) != len(ids):
            raise Held("cross-inventory identity collision")
        pools = _pool_rows(
            await hmc.list_child("ManagedSystem", system_uuid, "SharedMemoryPool")
        )
        stable = {
            "system": system,
            "console": version,
            "regular": {},
            "vios": {},
            "pools": pools,
        }
        profiles = {}
        route_ids = {}
        for identity, entry in regular.items():
            for field in (
                "PartitionMemoryConfiguration",
                "PartitionProcessorConfiguration",
            ):
                if not isinstance(entry["Resource"].get(field), dict):
                    raise Held("regular partition resource baseline unavailable")
            stable["regular"][identity] = _projection(entry["Resource"], BASE_FIELDS)
        total_profiles = 0
        for identity, entry in vios.items():
            route = entry["UUID"]
            route_ids[identity] = route
            observed_id, resource = _entry(
                await hmc.get_uom("VirtualIOServer", route), "VirtualIOServer"
            )
            mapped_id, mapped = _entry(
                await hmc.get_vios_storage_detail(route), "VirtualIOServer"
            )
            if observed_id != identity or mapped_id != identity:
                raise Held("VIOS detail identity differs")
            if _closing({identity: {"Resource": resource}}) != _closing(
                {identity: entry}
            ):
                raise Held("VIOS feed/detail identity differs")
            for field in (
                "PartitionMemoryConfiguration",
                "PartitionProcessorConfiguration",
                "PartitionIOConfiguration",
            ):
                if not isinstance(resource.get(field), dict):
                    raise Held("VIOS resource baseline unavailable")
            if any(k not in mapped for k in MAPPINGS):
                raise Held("explicit mapping groups unavailable")
            fields = _projection(resource, BASE_FIELDS + IO_FIELDS)
            fields["grouped_mappings"] = _projection(mapped, MAPPINGS)
            feed = await hmc.list_child(
                "VirtualIOServer", route, "LogicalPartitionProfile"
            )
            if not isinstance(feed, list) or total_profiles + len(feed) > 33:
                raise Held("complete saved-profile bound exceeded")
            saved = {}
            raw = {}
            profile_names = set()
            for item in feed:
                profile_id, _ = _entry(item, "LogicalPartitionProfile")
                if profile_id in saved:
                    raise Held("duplicate profile identity")
                profile = await read_native_profile(
                    hmc, "VirtualIOServer", route, item["UUID"], system_uuid
                )
                if not profile.etag or profile.name in profile_names:
                    raise Held("profile ETag/name unavailable or ambiguous")
                saved[profile_id] = {
                    "name": profile.name,
                    "setting_id": profile.setting_id,
                    "etag": profile.etag,
                    "semantic": _profile_semantic(profile.raw),
                }
                raw[profile_id] = profile.raw
                profile_names.add(profile.name)
            if set(saved) != _profile_links(resource, hmc, identity):
                raise Held("contained profile inventory and associations differ")
            total_profiles += len(saved)
            stable["vios"][identity] = {"fields": fields, "profiles": saved}
            profiles[identity] = raw
        regular_end = _inventory(
            await hmc.list_child("ManagedSystem", system_uuid, "LogicalPartition"),
            "LogicalPartition",
            128,
        )
        vios_end = _inventory(
            await hmc.list_child("ManagedSystem", system_uuid, "VirtualIOServer"),
            "VirtualIOServer",
            9,
        )
        pools_end = _pool_rows(
            await hmc.list_child("ManagedSystem", system_uuid, "SharedMemoryPool")
        )
        if (
            _closing(regular_end) != _closing(regular)
            or _closing(vios_end) != _closing(vios)
            or pools_end != pools
        ):
            raise Held("inventory changed during snapshot")
        result = {
            "stable": stable,
            "profiles": profiles,
            "route_ids": route_ids,
            "raw_rest": list(hmc.snapshot_responses or []),
        }
        if len(encode(result)) > 2 * MIB:
            raise Held("protected snapshot serialization bound exceeded")
        return result


def _foreign(stable: dict, identity: str) -> dict:
    copy = _json(encode(stable))
    if identity not in copy["vios"]:
        raise Held("own UUID is not contained")
    del copy["vios"][identity]
    for key, field in (
        ("memory", "CurrentAvailableSystemMemory"),
        ("cpu", "CurrentAvailableSystemProcessorUnits"),
    ):
        del copy["system"][key][field]
    return copy


def _baseline_foreign(stable: dict) -> dict:
    copy = _json(encode(stable))
    for key, field in (
        ("memory", "CurrentAvailableSystemMemory"),
        ("cpu", "CurrentAvailableSystemProcessorUnits"),
    ):
        del copy["system"][key][field]
    return copy


def _candidate(s: dict, case: dict, before: dict) -> str:
    matches = [
        identity
        for identity, v in s["stable"]["vios"].items()
        if leaf_text(v["fields"]["PartitionName"]["value"]) == case["name"]
    ]
    if len(matches) != 1 or matches[0] in before["vios"]:
        raise Held("one new contained VIOS UUID required")
    identity = matches[0]
    fields = s["stable"]["vios"][identity]["fields"]
    if (
        leaf_text(fields["PartitionState"]["value"]) != "not activated"
        or leaf_text(fields["PartitionType"]["value"]) != "Virtual IO Server"
    ):
        raise Held("fresh VIOS is not inactive")
    if (
        fields["Description"]["present"]
        and leaf_text(fields["Description"]["value"]) != case["description"]
    ):
        raise Held("REST initial marker differs")
    return identity
