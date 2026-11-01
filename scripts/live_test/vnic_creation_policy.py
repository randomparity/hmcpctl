"""Bounded, separately invoked vNIC creation policy procedure on an owned LPAR."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shlex
import sys
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import hmcpctl
from hmcpctl.client import core as client_core
from hmcpctl.client.client_profiles import PartitionKind, read_native_profile
from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig, load_profile
from hmcpctl.documents.lpar import LparResources, build_lpar_document
from hmcpctl.documents.partition_profile import _records
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar import core as lpar_core
from hmcpctl.operations.lpar import ownership
from hmcpctl.operations.lpar.ownership import (
    authorize_lpar_ownership_description,
    parse_lpar_ownership_caller_token,
    parse_lpar_ownership_owner,
)
from hmcpctl.operations.virtualization import vnic
from hmcpctl.operations.virtualization.pcie import (
    SriovLogicalPortCapabilityError,
    require_capacity_granularity,
)
from hmcpctl.operations.virtualization.vnic_policy import (
    VnicPolicy,
    creation_policy,
    observed_policy,
    policy_attributes,
    verify_policy,
)
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh import transport
from hmcpctl.ssh.commands import build_attribute_record
from hmcpctl.ssh.lpar import _lpar_resource_pairs
from hmcpctl.ssh.profiles import get_lpar_description
from hmcpctl.ssh.sriov import list_sriov_configured_logical_port_rows
from hmcpctl.ssh.vnic import (
    build_vnic_add_attributes,
    list_vnic_backing_rows,
    list_vnic_rows,
)
from hmcpctl.xmlutil import leaf_text

from .system_profile_source import _revision, digest, save
from .vios_ame_source import _json, _private, _read_private

GROUP = "vnic-creation-policy"
MIB = 1024 * 1024
LIMITS = {
    "preflight": (24, 24, 120),
    "capture": (48, 48, 300),
    "recover": (24, 24, 120),
}


def _keys(value: object, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("vNIC packet has missing or unknown fields")


def _selector(packet: dict) -> vnic.VnicBackingSelector:
    value = packet["selector"]
    return vnic._validate_vnic_backing_selector(
        vnic.VnicBackingSelector(
            value["vios_name"],
            value["vios_lpar_id"],
            value["adapter_id"],
            value["physical_port_id"],
            Decimal(value["capacity_percent"]),
        )
    )


def _validate_creation_packet(packet: dict) -> None:
    _keys(
        packet,
        {
            "version",
            "configured_profile",
            "hmc_identity",
            "expected_commit",
            "system_uuid",
            "lpar_uuid",
            "expected_lpar_id",
            "owner_token",
            "selector",
            "port_vlan_id",
            "policy",
            "baseline_sha256",
            "expires_at",
            "exclusive_writer_window",
            "allow_one_create",
            "allow_captured_slot_cleanup",
        },
    )
    if type(packet["version"]) is not int or packet["version"] != 1:
        raise ValueError("vNIC packet requires version 1")
    for name in ("configured_profile", "owner_token"):
        if (
            not isinstance(packet[name], str)
            or not packet[name]
            or not packet[name].isprintable()
        ):
            raise ValueError("vNIC profile and owner token require printable text")
    _keys(packet["hmc_identity"], {"host", "port"})
    destination = packet["hmc_identity"]
    if not isinstance(destination["host"], str) or not destination["host"]:
        raise ValueError("vNIC packet requires an HMC host")
    if type(destination["port"]) is not int or not 1 <= destination["port"] <= 65535:
        raise ValueError("vNIC packet requires a valid HMC port")
    for name in ("system_uuid", "lpar_uuid"):
        if not isinstance(packet[name], str) or not is_uuid(packet[name]):
            raise ValueError("vNIC packet requires exact system and LPAR UUIDs")
    for name in ("expected_lpar_id", "expires_at"):
        if type(packet[name]) is not int or packet[name] <= 0:
            raise ValueError(
                "vNIC packet requires positive numeric identity and expiry"
            )
    for name, length in (("expected_commit", 40), ("baseline_sha256", 64)):
        value = packet[name]
        if name == "baseline_sha256" and value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(
            f"[0-9a-f]{{{length}}}", value
        ):
            raise ValueError("vNIC packet requires exact commit and baseline digests")
    for name in (
        "exclusive_writer_window",
        "allow_one_create",
        "allow_captured_slot_cleanup",
    ):
        if packet[name] is not True:
            raise ValueError(
                "vNIC packet requires the explicit bounded writer acknowledgements"
            )
    _keys(
        packet["selector"],
        {
            "vios_name",
            "vios_lpar_id",
            "adapter_id",
            "physical_port_id",
            "capacity_percent",
        },
    )
    if any(not isinstance(value, str) for value in packet["selector"].values()):
        raise ValueError(
            "vNIC selector members require strings, including decimal capacity"
        )
    selector = _selector(packet)
    if (
        type(packet["port_vlan_id"]) is not int
        or not 0 <= packet["port_vlan_id"] <= 4094
    ):
        raise ValueError("vNIC port VLAN must be an integer between 0 and 4094")
    policy = packet["policy"]
    if not isinstance(policy, dict) or not set(policy) <= {
        item.name for item in fields(VnicPolicy)
    }:
        raise ValueError("vNIC packet policy contains unknown fields")
    build_vnic_add_attributes(
        vnic._add_payload(selector),
        packet["port_vlan_id"],
        policy_attributes(creation_policy(**policy)),
    )


_SOURCE_CASES = {
    "vlan-replace-all": ("allowed_vlan_ids", "=", "all"),
    "vlan-replace-none": ("allowed_vlan_ids", "=", "none"),
    "vlan-remove-last": ("allowed_vlan_ids", "-", "none"),
    "mac-replace-all": ("allowed_os_mac_addrs", "=", "all"),
    "mac-replace-none": ("allowed_os_mac_addrs", "=", "none"),
    "mac-remove-last": ("allowed_os_mac_addrs", "-", "none"),
}
_SOURCE_EXTRA = {"purpose", "case", "expected_lpar_state", "allow_one_update"}
_SOURCE_POLICY = {
    "pvid_priority",
    "auto_priority_failover",
    "allowed_vlan_ids",
    "allowed_os_mac_addrs",
}


_FIXTURE_FIELDS = {
    "version",
    "purpose",
    "action",
    "configured_profile",
    "hmc_identity",
    "expected_commit",
    "system_uuid",
    "fixture_name",
    "caller_token",
    "baseline_sha256",
    "expires_at",
    "exclusive_writer_window",
    "allow_fixture_effect",
    "prepare_journal",
    "source_declaration",
    "source_evidence",
}
FIXTURE_LIMITS = {
    "preflight": (64, 40, 120),
    "prepare": (96, 24, 300),
    "cleanup": (128, 64, 300),
    "recover": (32, 24, 120),
}
FIXTURE_SUFFIX = {
    "create-rest": (64, 16),
    "create-native": (60, 14),
    "stamp": (32, 8),
    "delete": (24, 4),
}


def _fixture(packet):
    return (
        isinstance(packet, dict)
        and type(packet.get("version")) is int
        and packet["version"] == 3
    )


def _validate_fixture(packet):
    _keys(packet, _FIXTURE_FIELDS)
    if packet["purpose"] != "vnic-source-fixture" or packet["action"] not in (
        "prepare",
        "cleanup",
    ):
        raise ValueError("fixture requires its fixed purpose and explicit action")
    for key in ("configured_profile", "fixture_name", "caller_token"):
        if (
            not isinstance(packet[key], str)
            or not packet[key]
            or not packet[key].isprintable()
        ):
            raise ValueError("fixture identity requires printable strings")
    match = re.fullmatch(r"hmcpctl-vnic-fixture-([0-9a-f]{16})", packet["fixture_name"])
    if match is None or packet["caller_token"] != "q1828-fixture-" + match[1]:
        raise ValueError("fixture name and caller nonce differ")
    _keys(packet["hmc_identity"], {"host", "port"})
    host, port = packet["hmc_identity"]["host"], packet["hmc_identity"]["port"]
    if (
        not isinstance(host, str)
        or not host
        or not host.isprintable()
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise ValueError("fixture destination is invalid")
    if not isinstance(packet["system_uuid"], str) or not is_uuid(packet["system_uuid"]):
        raise ValueError("fixture requires the exact system UUID")
    for key, length in (("expected_commit", 40), ("baseline_sha256", 64)):
        value = packet[key]
        if key == "baseline_sha256" and value is None:
            continue
        if (
            not isinstance(value, str)
            or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None
        ):
            raise ValueError("fixture candidate or baseline digest is invalid")
    if type(packet["expires_at"]) is not int or packet["expires_at"] <= 0:
        raise ValueError("fixture expiry must be a positive integer")
    if (
        packet["exclusive_writer_window"] is not True
        or packet["allow_fixture_effect"] is not True
    ):
        raise ValueError("fixture requires current bounded writer acknowledgements")
    records = packet["source_evidence"]
    if not isinstance(records, list) or len(records) > 6:
        raise ValueError("fixture source evidence exceeds six records")
    if packet["source_declaration"] == "none-invoked":
        if records:
            raise ValueError("no-source declaration contradicts known records")
    elif packet["source_declaration"] != "complete-set" or not records:
        raise ValueError("fixture requires an explicit source invocation declaration")
    paths = []
    for record in records:
        _keys(record, {"packet", "capture", "recovery"})
        for value in record.values():
            if not isinstance(value, str) or not value or not value.isprintable():
                raise ValueError("fixture evidence paths must be printable strings")
            paths.append(value)
    if len(set(paths)) != len(paths):
        raise ValueError("fixture source evidence repeats an input")
    original = packet["prepare_journal"]
    if packet["action"] == "prepare":
        if (
            original is not None
            or records
            or packet["source_declaration"] != "none-invoked"
        ):
            raise ValueError("fixture preparation cannot adopt prior work")
    elif not isinstance(original, str) or not original or not original.isprintable():
        raise ValueError("fixture cleanup requires original preparation")


def _fixture_resources():
    return LparResources(
        min_memory=512,
        desired_memory=512,
        max_memory=1024,
        dedicated=False,
        min_procs=0.05,
        desired_procs=0.05,
        max_procs=0.05,
        min_vcpus=1,
        desired_vcpus=1,
        max_vcpus=1,
        uncapped=False,
    )


def _scalar_text(value):
    if isinstance(value, dict):
        attrs = value.get("@attrs")
        if (
            set(value) != {"@attrs", "text"}
            or not isinstance(attrs, dict)
            or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in attrs.items()
            )
        ):
            raise ValueError("vNIC observed scalar leaf is malformed")
        value = leaf_text(value)
    if not isinstance(value, str):
        raise TypeError("vNIC observed scalar text is unavailable")
    return value


def _fixture_number(value):
    result = Decimal(_scalar_text(value))
    if not result.is_finite() or result < 0:
        raise ValueError("fixture numeric observation is unavailable")
    return result


def _fixture_capacity(system):
    memory = system["AssociatedSystemMemoryConfiguration"]
    cpu = system["AssociatedSystemProcessorConfiguration"]
    block = _fixture_number(memory["CurrentLogicalMemoryBlockSize"])
    minimum = _fixture_number(cpu["MinimumProcessorUnitsPerVirtualProcessor"])
    if (
        block <= 0
        or 512 % block
        or 1024 % block
        or not 0 < minimum <= Decimal("0.05")
        or _fixture_number(memory["CurrentAvailableSystemMemory"]) < 1024
        or _fixture_number(cpu["CurrentAvailableSystemProcessorUnits"])
        < Decimal("0.05")
    ):
        raise ValueError("fixed fixture resources are not freshly eligible")


async def _fixture_snapshot(hmc, packet):
    system = await hmc.get_managed_system(packet["system_uuid"])
    r = system["Resource"]
    if (
        system.get("UUID", "").casefold() != packet["system_uuid"].casefold()
        or _scalar_text(r.get("State")) != "operating"
    ):
        raise ValueError("fixture operating system identity unavailable")
    system_name = _scalar_text(r.get("SystemName"))
    if not isinstance(system_name, str) or not system_name:
        raise ValueError("fixture system command name unavailable")
    rows: list[tuple[PartitionKind, dict]] = []
    kinds: tuple[PartitionKind, ...] = ("LogicalPartition", "VirtualIOServer")
    for kind in kinds:
        for entry in await hmc.list_child("ManagedSystem", packet["system_uuid"], kind):
            rows.append((kind, entry))
    if len(rows) > 5:
        raise ValueError("fixture partition observation exceeds five entries")
    observed, profile_count, names, numbers = {}, 0, set(), set()
    for kind, entry in rows:
        uid, resource = entry["UUID"], entry["Resource"]
        name, number = (
            _scalar_text(resource.get("PartitionName")),
            _scalar_text(resource.get("PartitionID")),
        )
        if (
            not isinstance(uid, str)
            or not is_uuid(uid)
            or uid.casefold() in observed
            or not isinstance(name, str)
            or not name
            or name in names
            or number in numbers
            or not isinstance(number, str)
            or re.fullmatch(r"[1-9][0-9]*", number) is None
        ):
            raise ValueError("fixture contained identities are ambiguous")
        names.add(name)
        numbers.add(number)
        detail = await hmc.get_uom(kind, uid, group="Advanced")
        full = detail["Resource"]
        if (
            detail["UUID"].casefold() != uid.casefold()
            or _scalar_text(full.get("PartitionName")) != name
            or _scalar_text(full.get("PartitionID")) != number
            or any(
                not isinstance(full.get(k), dict)
                for k in (
                    "PartitionMemoryConfiguration",
                    "PartitionProcessorConfiguration",
                    "PartitionIOConfiguration",
                )
            )
        ):
            raise ValueError("fixture feed/detail or full resource observation differs")
        saved = {}
        feed = await hmc.list_child(kind, uid, "LogicalPartitionProfile")
        if not isinstance(feed, list) or profile_count + len(feed) > 9:
            raise ValueError("fixture profile observation exceeds nine entries")
        for item in feed:
            profile = await read_native_profile(
                hmc, kind, uid, item["UUID"], packet["system_uuid"]
            )
            if (
                profile.uuid.casefold() in saved
                or not profile.etag
                or profile.name in [p["name"] for p in saved.values()]
            ):
                raise ValueError("fixture saved profile identity/ETag unavailable")
            saved[profile.uuid.casefold()] = {
                "name": profile.name,
                "setting_id": profile.setting_id,
                "etag": profile.etag,
                "resource": profile.resource,
                "semantic": _plain(_records(profile.element, False)),
            }
        profile_count += len(saved)
        full = deepcopy(full)
        full.pop("Uptime", None)
        observed[uid.casefold()] = {
            "kind": kind,
            "name": name,
            "id": number,
            "resource": full,
            "profiles": saved,
        }
    return _plain(
        {
            "system": {
                "uuid": packet["system_uuid"].casefold(),
                "name": system_name,
                "memory": r["AssociatedSystemMemoryConfiguration"],
                "cpu": r["AssociatedSystemProcessorConfiguration"],
            },
            "partitions": dict(sorted(observed.items())),
            "profiles": profile_count,
            "backings": sorted(
                await list_vnic_backing_rows(hmc.config, system_name),
                key=lambda row: (row["adapter_id"], row["logical_port_id"]),
            ),
        }
    )


def _fixture_foreign(snapshot, uid=None):
    value = deepcopy(snapshot)
    if uid is not None:
        target = value["partitions"].pop(uid)
        value["profiles"] -= len(target["profiles"])
    value["system"]["memory"].pop("CurrentAvailableSystemMemory", None)
    value["system"]["cpu"].pop("CurrentAvailableSystemProcessorUnits", None)
    return value


def _fixture_target(packet, current, baseline):
    matches = [
        (uid, item)
        for uid, item in current["partitions"].items()
        if item["name"] == packet["fixture_name"]
    ]
    if len(matches) != 1:
        raise ValueError("fixture identity is not unique")
    uid, target = matches[0]
    r = target["resource"]
    if (
        uid in baseline["partitions"]
        or target["kind"] != "LogicalPartition"
        or _scalar_text(r.get("PartitionType")) != "AIX/Linux"
        or _scalar_text(r.get("PartitionState")) != "not activated"
        or _scalar_text(r.get("DefaultProfileName")) != "default_profile"
        or _fixture_foreign(current, uid) != _fixture_foreign(baseline)
    ):
        raise ValueError(
            "fixture fresh inactive identity or foreign preservation differs"
        )
    profiles = list(target["profiles"].values())
    if len(profiles) != 1 or profiles[0]["name"] != "default_profile":
        raise ValueError("fixture requires one contained saved default")
    profile = profiles[0]["resource"]
    memory, cpu = profile["ProfileMemory"], profile["ProcessorAttributes"]
    shared = cpu["SharedProcessorConfiguration"]
    if (
        _scalar_text(cpu.get("HasDedicatedProcessors")) != "false"
        or _scalar_text(cpu.get("SharingMode")) != "capped"
    ):
        raise ValueError("fixture configured shared/capped mode differs")
    for mapping, field_names, values in (
        (memory, ("MinimumMemory", "DesiredMemory", "MaximumMemory"), (512, 512, 1024)),
        (
            shared,
            (
                "MinimumProcessingUnits",
                "DesiredProcessingUnits",
                "MaximumProcessingUnits",
            ),
            ("0.05",) * 3,
        ),
        (
            shared,
            (
                "MinimumVirtualProcessors",
                "DesiredVirtualProcessors",
                "MaximumVirtualProcessors",
            ),
            (1,) * 3,
        ),
    ):
        if any(
            _fixture_number(mapping.get(k)) != Decimal(str(v))
            for k, v in zip(field_names, values, strict=True)
        ):
            raise ValueError("fixture saved resource tuple differs")
    io = profile["IOConfigurationInstance"]
    if _scalar_text(io.get("MaximumVirtualIOSlots")) != "8" or any(
        value not in ({}, [], None, "", "false")
        for key, value in io.items()
        if key not in ("Metadata", "MaximumVirtualIOSlots")
    ):
        raise ValueError("fixture configured I/O exceeds its envelope")
    return {"uuid": uid, **target}


def _source(packet: dict) -> bool:
    return type(packet.get("version")) is int and packet["version"] == 2


def validate_packet(packet: dict) -> None:
    if _fixture(packet):
        _validate_fixture(packet)
        return
    if not isinstance(packet, dict) or not _source(packet):
        _validate_creation_packet(packet)
        return
    if not _SOURCE_EXTRA <= set(packet):
        raise ValueError("source packet is missing required fields")
    if packet["purpose"] != "update-mode-source" or not isinstance(packet["case"], str):
        raise ValueError("source packet requires one fixed purpose and case")
    if packet["case"] not in _SOURCE_CASES or packet["allow_one_update"] is not True:
        raise ValueError("source packet requires a case and one-update acknowledgement")
    state = packet["expected_lpar_state"]
    if not isinstance(state, str) or not state or not state.isprintable():
        raise ValueError("source packet requires an exact printable state")
    base = {key: value for key, value in packet.items() if key not in _SOURCE_EXTRA}
    base["version"] = 1
    _validate_creation_packet(base)
    if not packet["hmc_identity"]["host"].isprintable():
        raise ValueError("source packet host must be printable")
    _keys(packet["policy"], _SOURCE_POLICY)
    policy = creation_policy(**packet["policy"])
    if policy.pvid_priority is None or policy.auto_priority_failover is None:
        raise ValueError("source baseline requires explicit scalar policies")
    for name in ("allowed_vlan_ids", "allowed_os_mac_addrs"):
        members = getattr(policy, name)
        if not isinstance(members, tuple) or len(members) != 1:
            raise ValueError("source baseline requires finite singleton lists")


def _case(packet: dict) -> tuple[str, str, str, str]:
    field, operation, oracle = _SOURCE_CASES[packet["case"]]
    value = oracle
    if operation == "-":
        value = str(getattr(creation_policy(**packet["policy"]), field)[0])
    return field, field + ("-" if operation == "-" else ""), value, oracle


def _source_row(current: dict, slot: str) -> dict:
    rows = [row for row in current["vnics"] if row["slot_num"] == slot]
    if len(rows) != 1:
        raise ValueError("source captured slot is missing or ambiguous")
    return rows[0]


def _source_complete(row: dict) -> None:
    raw = row.get("port_vlan_id")
    if (
        not isinstance(raw, str)
        or not re.fullmatch(r"[0-9]+", raw)
        or not 0 <= int(raw) <= 4094
    ):
        raise ValueError("source PVID is unavailable")
    policy = observed_policy(row)
    if any(getattr(policy, item.name) is None for item in fields(VnicPolicy)):
        raise ValueError("source policy observation is incomplete")


def _context(hmc: HMCClient, packet: dict, stage: str) -> None:
    validate_packet(packet)
    if (hmc.config.host, hmc.config.port) != (
        packet["hmc_identity"]["host"],
        packet["hmc_identity"]["port"],
    ):
        raise ValueError("vNIC configured destination differs from packet")
    if (
        hmc.config.max_response_bytes != MIB
        or hmc._legacy_port_fallback
        or hmc._session_token is not None
    ):
        raise ValueError(
            "vNIC client requires the pinned port and one MiB response bound"
        )
    revision, clean = _revision()
    root = Path(__file__).resolve().parents[2]
    if revision != packet["expected_commit"] or not clean:
        raise ValueError("vNIC procedure requires the exact clean candidate commit")
    if Path(hmcpctl.__file__).resolve() != root / "src/hmcpctl/__init__.py":
        raise ValueError("vNIC imported package is not the candidate checkout")
    if stage != "recover" and time.time() >= packet["expires_at"]:
        raise ValueError("vNIC writer window expired")


SOURCE_LIMITS = {
    "preflight": (24, 24, 120),
    "capture": (48, 56, 300),
    "recover": (24, 24, 120),
}
SOURCE_SUFFIX = {"create": (24, 26), "update": (17, 18), "remove": (7, 6)}


class _Budget:
    def __init__(
        self, hmc, packet, stage, document, output, *, limits=None, reserve_seconds=None
    ):
        self.hmc, self.packet, self.document, self.output = (
            hmc,
            packet,
            document,
            output,
        )
        self.rest_limit, self.ssh_limit, seconds = (
            limits or (SOURCE_LIMITS if _source(packet) else LIMITS)
        )[stage]
        self.reserve_seconds = (
            (15 if _source(packet) else 0)
            if reserve_seconds is None
            else reserve_seconds
        )
        self.dispatched: set[str] = set()
        self.remove_reads: set[str] = set()
        self.deadline = time.monotonic() + seconds
        self.counts = {"rest": 0, "ssh": 0}
        self.armed: tuple[str, str] | None = None
        self.spent: set[str] = set()
        self.logged_on = self.logged_off = False

    def private_warning(self, message, *args, **kwargs):
        self.document.setdefault("warnings", []).append(
            message % args if args else message
        )
        if self.output is not None:
            save(self.output, self.document)

    def take(self, kind: str, *, logout: bool = False) -> float:
        reserve = self.reserve_seconds if not logout else 0
        remaining = self.deadline - reserve - time.monotonic()
        limit = self.rest_limit - (not logout) if kind == "rest" else self.ssh_limit
        if remaining <= 0 or self.counts[kind] >= limit:
            raise ValueError("vNIC procedure transport count or deadline exhausted")
        self.counts[kind] += 1
        return remaining

    def _source_reserve(self, kind: str) -> None:
        rest, ssh = SOURCE_SUFFIX[kind]
        if (
            self.rest_limit - self.counts["rest"] < rest
            or self.ssh_limit - self.counts["ssh"] < ssh
            or self.deadline - 15 - time.monotonic() < 15
        ):
            raise ValueError("source effect lacks observation/logout reserves")

    def arm(self, kind: str, command: str) -> None:
        if self.armed or kind in self.spent:
            raise ValueError("vNIC mutation allowance is already used")
        self.armed = kind, command

    def before_write(self, command: str) -> str:
        if (
            self.armed is None
            or command != self.armed[1]
            or time.time() >= self.packet["expires_at"]
        ):
            raise ValueError(
                "vNIC mutation does not match its unexpired exact allowance"
            )
        kind, _ = self.armed
        if _source(self.packet):
            self._source_reserve(kind)
            if kind == "remove" and self.remove_reads != {
                "vnics",
                "backings",
                "target",
                "members",
                "description",
            }:
                raise ValueError("source removal lacks qualified fresh observations")
        self.armed = None
        self.spent.add(kind)
        if _source(self.packet):
            self.document.setdefault("effect_commands", []).append(
                {"kind": kind, "command": command}
            )
        self.document["intents"].append(kind)
        save(self.output, self.document)
        if time.time() >= self.packet["expires_at"]:
            raise ValueError(
                "vNIC mutation allowance expired during intent persistence"
            )
        if _source(self.packet):
            self._source_reserve(kind)
        return kind


@contextmanager
def _bounded(budget: _Budget):
    request = HMCClient._request
    ssh = transport._run_hmc_command

    async def counted_request(self, method, path, **kwargs):
        if self is not budget.hmc or not (
            method == "GET"
            or (path == "/rest/api/web/Logon" and method in ("PUT", "DELETE"))
        ):
            raise ValueError("vNIC procedure refuses an unrelated REST effect")
        logout = method == "DELETE"
        if method == "PUT" and budget.logged_on:
            raise ValueError("vNIC procedure permits only one logon")
        async with asyncio.timeout(budget.take("rest", logout=logout)):
            result = await request(self, method, path, **kwargs)
        if path == "/rest/api/web/Logon":
            if method == "PUT":
                budget.logged_on = result.status_code == 200
            elif logout:
                budget.logged_off = result.status_code in (200, 202, 204)
        return result

    async def counted_ssh(
        config: HMCConfig,
        cmd: str,
        include_stderr: bool,
        *,
        shown: str,
        max_output_bytes: int | None = None,
    ) -> str:
        if config is not budget.hmc.config:
            raise ValueError("vNIC procedure refuses an unrelated SSH connection")
        remaining = budget.take("ssh")
        tokens = shlex.split(cmd)
        if not tokens or tokens[0] not in ("lshmc", "lssyscfg", "lshwres"):
            kind = budget.before_write(cmd)
            reserve = 15 if _source(budget.packet) else 0
            remaining = budget.deadline - reserve - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    "vNIC procedure deadline exhausted before SSH dispatch"
                )
            budget.dispatched.add(kind)
        async with asyncio.timeout(remaining):
            return await ssh(
                config, cmd, include_stderr, shown=shown, max_output_bytes=MIB
            )

    with (
        patch.object(HMCClient, "_request", counted_request),
        patch.object(transport, "_run_hmc_command", counted_ssh),
        patch.object(client_core._logger, "warning", budget.private_warning),
    ):
        yield


def _fixture_commands(packet, system_name, description):
    create_record = build_attribute_record(
        [
            ("name", packet["fixture_name"]),
            ("lpar_env", "aixlinux"),
            ("profile_name", "default_profile"),
            *_lpar_resource_pairs(_fixture_resources(), 8),
        ]
    )
    create = f"mksyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(create_record)}"
    stamp_record = build_attribute_record(
        [("name", packet["fixture_name"]), ("description", description)]
    )
    stamp = (
        f"chsyscfg -r lpar -m {shlex.quote(system_name)} -i {shlex.quote(stamp_record)}"
    )
    return {"create-native": create, "stamp": stamp}


async def _fixture_held(hmc, packet, baseline):
    current = await _fixture_snapshot(hmc, packet)
    target = _fixture_target(packet, current, baseline)
    native = await get_lpar_description(
        hmc.config, current["system"]["name"], packet["fixture_name"]
    )
    description = _description_text(native, native=True)
    if description != _description_text(target["resource"].get("Description")):
        raise ValueError("fixture native/REST description differs")
    rows = await list_vnic_rows(
        hmc.config, current["system"]["name"], packet["fixture_name"]
    )
    if rows:
        raise ValueError(
            "fixture vNIC baseline is not empty; enclosing deletion disarmed"
        )
    return {
        "snapshot": current,
        "target": target,
        "description": description,
        "native_description_raw": native,
        "vnics": rows,
    }


def _fixture_owned(hmc, packet, held):
    description = held["description"]
    authorize_lpar_ownership_description(
        hmc,
        held["snapshot"]["system"]["name"],
        packet["fixture_name"],
        description,
        operation="lpar-mutation",
    )
    if parse_lpar_ownership_caller_token(description) != packet["caller_token"]:
        raise ValueError("fixture caller token differs")


class _FixtureBudget(_Budget):
    def __init__(self, hmc, packet, stage, document, output):
        super().__init__(
            hmc,
            packet,
            stage,
            document,
            output,
            limits=FIXTURE_LIMITS,
            reserve_seconds=15,
        )
        self.stage = stage
        self.qualified = None
        self.captured: dict | None = None
        self.description = f"[hmcpctl owner:{hmc.config.agent_id or 'hmcpctl'} created:{date.today().isoformat()}] [caller {packet['caller_token']}]"  # noqa: DTZ011 - match the existing ADR0011 stamp owner's local calendar date
        self.creation = build_lpar_document(
            name=packet["fixture_name"],
            partition_type="AIX/Linux",
            resources=_fixture_resources(),
            max_virtual_slots=8,
        )

    def signature(self, kind):
        system = self.document["baseline"]["system"]["name"]
        if kind in ("create-native", "stamp"):
            return _fixture_commands(self.packet, system, self.description)[kind]
        if kind == "create-rest":
            return {
                "method": "PUT",
                "path": f"/rest/api/uom/ManagedSystem/{self.packet['system_uuid']}/LogicalPartition",
                "body_sha256": hashlib.sha256(self.creation.encode()).hexdigest(),
            }
        if self.captured is None:
            raise ValueError("fixture deletion requires a captured identity")
        return {
            "method": "DELETE",
            "path": f"/rest/api/uom/LogicalPartition/{self.captured['uuid']}",
        }

    def reserve(self, kind):
        rest, ssh = FIXTURE_SUFFIX[kind]
        if (
            self.rest_limit - self.counts["rest"] < rest
            or self.ssh_limit - self.counts["ssh"] < ssh
            or self.deadline - 15 - time.monotonic() < 15
            or time.time() >= self.packet["expires_at"]
        ):
            raise ValueError("fixture effect lacks fresh observation/logout reserve")

    def intent(self, kind, signature):
        if signature != self.signature(kind) or kind in self.spent:
            raise ValueError("fixture effect differs from its once-only allowance")
        previous = [
            (item["kind"], item["settlement"]) for item in self.document["effects"]
        ]
        allowed = {
            "create-rest": self.stage == "prepare" and not previous,
            "create-native": self.stage == "prepare"
            and previous == [("create-rest", "refused")],
            "stamp": self.stage == "prepare"
            and self.captured is not None
            and previous
            in (
                [("create-rest", "acknowledged")],
                [("create-rest", "refused"), ("create-native", "acknowledged")],
            ),
            "delete": self.stage == "cleanup"
            and self.qualified is not None
            and not previous,
        }
        if not allowed[kind]:
            raise ValueError("fixture effect order or current authority differs")
        self.reserve(kind)
        self.spent.add(kind)
        self.document["effects"].append(
            {"kind": kind, "signature": signature, "settlement": "unknown"}
        )
        save(self.output, self.document)
        self.reserve(kind)

    def settle(self, kind, result):
        if self.document["effects"][-1]["kind"] != kind:
            raise ValueError("fixture effect settlement order differs")
        self.document["effects"][-1]["settlement"] = result
        save(self.output, self.document)


@contextmanager
def _fixture_bounded(budget):
    request, ssh = HMCClient._request, transport._run_hmc_command
    stamp_owner = lpar_core.stamp_created_lpar_ownership

    async def guarded_stamp(hmc, system_uuid, fallback, created, caller_token=None):
        if (
            hmc is not budget.hmc
            or system_uuid != budget.packet["system_uuid"]
            or caller_token != budget.packet["caller_token"]
        ):
            raise ValueError("fixture stamp owner context differs")
        held = await _fixture_held(hmc, budget.packet, budget.document["baseline"])
        if created.get("UUID", "").casefold() != held["target"]["uuid"]:
            raise ValueError("fixture returned and independently contained UUID differ")
        budget.captured = held["target"]
        budget.document["before_stamp"] = held
        save(budget.output, budget.document)
        return await stamp_owner(
            hmc, system_uuid, fallback, created, caller_token=caller_token
        )

    async def counted_request(hmc, method, path, **kwargs):
        if hmc is not budget.hmc:
            raise ValueError("fixture refuses an unrelated REST client")
        own_session = path == "/rest/api/web/Logon" and method in ("PUT", "DELETE")
        kind = None
        if method != "GET" and not own_session:
            kind = (
                "create-rest"
                if method == "PUT"
                else "delete"
                if method == "DELETE"
                else None
            )
            if kind is None:
                raise ValueError("fixture refuses an unrelated REST effect")
            if kind == "delete":
                if budget.qualified is None:
                    raise ValueError("fixture deletion is not currently qualified")
                fresh = await _fixture_held(
                    hmc, budget.packet, budget.document["original"]["baseline"]
                )
                _fixture_owned(hmc, budget.packet, fresh)
                if fresh != budget.qualified:
                    raise ValueError("fixture changed at physical deletion boundary")
                budget.document["delete_observation"] = fresh
            signature = {"method": method, "path": path}
            if method == "PUT":
                body = kwargs.get("content")
                if not isinstance(body, str):
                    raise ValueError(
                        "fixture create body is not the exact text document"
                    )
                signature["body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
        if own_session and method == "PUT" and budget.logged_on:
            raise ValueError("fixture allows one own logon")
        remaining = budget.take("rest", logout=own_session and method == "DELETE")
        if kind is not None:
            budget.intent(kind, signature)
            remaining = budget.deadline - 15 - time.monotonic()
        if remaining <= 0:
            raise ValueError("fixture dispatch deadline expired after intent")
        async with asyncio.timeout(remaining):
            response = await request(hmc, method, path, **kwargs)
        if own_session:
            if method == "PUT":
                budget.logged_on = response.status_code == 200
            else:
                budget.logged_off = response.status_code in (200, 202, 204)
        elif kind is not None:
            refused = kind == "create-rest" and lpar_core._rest_create_refused(
                HMCError(
                    "fixture REST creation refusal", response.status_code, response.text
                )
            )
            budget.settle(
                kind,
                "refused"
                if refused
                else "acknowledged"
                if response.status_code in (200, 201, 202, 204)
                else "unknown",
            )
        return response

    async def counted_ssh(
        config, command, include_stderr, *, shown, max_output_bytes=None
    ):
        if config is not budget.hmc.config:
            raise ValueError("fixture refuses an unrelated SSH connection")
        tokens = shlex.split(command)
        kind = None
        if not tokens or tokens[0] not in ("lshmc", "lssyscfg", "lshwres"):
            kind = {"mksyscfg": "create-native", "chsyscfg": "stamp"}.get(
                tokens[0] if tokens else ""
            )
            if kind is None:
                raise ValueError("fixture refuses an unrelated native effect")
        remaining = budget.take("ssh")
        if kind is not None:
            budget.intent(kind, command)
            remaining = budget.deadline - 15 - time.monotonic()
        if remaining <= 0:
            raise ValueError("fixture dispatch deadline expired after intent")
        async with asyncio.timeout(remaining):
            result = await ssh(
                config, command, include_stderr, shown=shown, max_output_bytes=MIB
            )
        if kind is not None:
            budget.settle(kind, "acknowledged")
        return result

    with (
        patch.object(HMCClient, "_request", counted_request),
        patch.object(transport, "_run_hmc_command", counted_ssh),
        patch.object(client_core._logger, "warning", budget.private_warning),
        patch.object(lpar_core, "stamp_created_lpar_ownership", guarded_stamp),
        patch.object(lpar_core._logger, "warning", budget.private_warning),
        patch.object(ownership._logger, "warning", budget.private_warning),
    ):
        yield


def _plain(value):
    return json.loads(json.dumps(value, default=str))


def _description_text(value, *, native=False):
    if not native:
        value = _scalar_text(value)
    if not isinstance(value, str):
        raise TypeError("vNIC Description text is unavailable")
    if native:
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    if value and not value.isprintable():
        raise ValueError("vNIC Description contains control or multiple records")
    return value


async def snapshot(
    hmc: HMCClient, packet: dict, *, source_result: dict | None = None
) -> dict:
    """Fresh containment, owner token and independent native inventory projections."""
    system = await hmc.get_managed_system(packet["system_uuid"])
    target = await hmc.get_logical_partition(packet["lpar_uuid"])
    members = await hmc.list_logical_partitions(packet["system_uuid"])
    matches = [
        item
        for item in members
        if str(item.get("UUID", "")).casefold() == packet["lpar_uuid"].casefold()
    ]
    if (
        not system
        or not target
        or str(system.get("UUID", "")).casefold() != packet["system_uuid"].casefold()
        or str(target.get("UUID", "")).casefold() != packet["lpar_uuid"].casefold()
        or len(matches) != 1
        or matches[0].get("Resource") != target.get("Resource")
    ):
        raise ValueError("vNIC target is not uniquely and consistently contained")
    resource = target["Resource"]
    system_name = system["Resource"]["SystemName"]
    lpar_name = resource["PartitionName"]
    if str(resource.get("PartitionID")) != str(packet["expected_lpar_id"]):
        raise ValueError("vNIC numeric partition identity changed")
    description = _description_text(
        await get_lpar_description(hmc.config, system_name, lpar_name), native=True
    )
    if (
        description != _description_text(resource.get("Description"))
        or parse_lpar_ownership_owner(description) != (hmc.config.agent_id or "hmcpctl")
        or parse_lpar_ownership_caller_token(description) != packet["owner_token"]
    ):
        raise ValueError("vNIC owned fixture description or caller token changed")
    identity = {
        "system": system_name,
        "lpar": lpar_name,
        "id": packet["expected_lpar_id"],
        "description": description,
    }
    if _source(packet):
        if resource.get("PartitionState") != packet["expected_lpar_state"]:
            raise ValueError("source fixture state differs or is unavailable")
        identity["state"] = resource["PartitionState"]
    rows = await list_vnic_rows(hmc.config, system_name, lpar_name)
    if source_result is not None:
        row = _source_row({"vnics": rows}, source_result["captured_slot"])
        if row["lpar_name"] != lpar_name or row["lpar_id"] != str(
            packet["expected_lpar_id"]
        ):
            raise ValueError("source selected-row identity differs")
        selected_field, _, _, oracle = _case(packet)
        source_result["selected_after"] = dict(row)
        available = getattr(observed_policy(row), selected_field) is not None
        source_result["policy_verification"] = (
            "unavailable"
            if not available
            else "verified"
            if row.get(selected_field) == oracle
            else "mismatched"
        )
    backings = await list_vnic_backing_rows(hmc.config, system_name)
    direct = await list_sriov_configured_logical_port_rows(
        hmc.config, system_name, packet["selector"]["adapter_id"]
    )
    parsed = vnic._parse_vnic_snapshots(rows)
    if len({item.slot_num for item in parsed}) != len(parsed) or any(
        item.lpar_name != lpar_name or item.lpar_id != str(packet["expected_lpar_id"])
        for item in parsed
    ):
        raise ValueError("vNIC native slot identity is ambiguous")
    vnic._reconciled_logical_ports(
        direct, tuple(vnic._parse_backing_snapshot(row) for row in backings)
    )
    return {
        "identity": identity,
        "vnics": sorted(rows, key=lambda row: row["slot_num"]),
        "backings": sorted(
            backings, key=lambda row: (row["adapter_id"], row["logical_port_id"])
        ),
        "direct": sorted(
            direct, key=lambda row: (row["adapter_id"], row["logical_port_id"])
        ),
    }


async def _eligible(hmc, packet):
    selector = _selector(packet)
    context = await vnic._preflight_add(
        hmc, packet["system_uuid"], packet["lpar_uuid"], selector, False
    )
    require_capacity_granularity(selector.capacity_percent, context.granularity)
    if context.used_capacity + selector.capacity_percent > 100:
        raise ValueError("vNIC fixture lacks requested free capacity")
    if vnic._matching_vnics(context.vnics, selector, packet["port_vlan_id"]):
        raise ValueError(
            "vNIC fixture already has a matching slot; creation proof unavailable"
        )


def _cleanup_candidate(packet, baseline, current, created):
    slot = created["slot_num"]
    selected = [row for row in current["vnics"] if row["slot_num"] == slot]
    if len(selected) != 1 or current["identity"] != baseline["identity"]:
        raise ValueError("vNIC captured slot or owned identity changed")
    observed = vnic._parse_vnic_snapshots(selected)[0]
    if _plain(asdict(observed)) != created["vnic_after"][0]:
        raise ValueError("vNIC captured slot has been replaced or changed")
    verification = verify_policy(creation_policy(**packet["policy"]), observed.policy)
    if (
        verification.status not in ("verified", "not_requested")
        or len(observed.backing_devices) != 1
    ):
        raise ValueError("vNIC cleanup lacks selected policy or one-backing evidence")
    backing = observed.backing_devices[0]
    matches = [
        row
        for row in current["backings"]
        if (row["adapter_id"], row["logical_port_id"])
        == (backing.adapter_id, backing.logical_port_id)
    ]
    if (
        len(matches) != 1
        or _plain(asdict(vnic._parse_backing_snapshot(matches[0])))
        != created["backing_after"][0]
    ):
        raise ValueError("vNIC captured backing identity changed")
    foreign = {
        **current,
        "vnics": [row for row in current["vnics"] if row not in selected],
        "backings": [row for row in current["backings"] if row not in matches],
        "direct": [
            row
            for row in current["direct"]
            if (row["adapter_id"], row["logical_port_id"])
            != (backing.adapter_id, backing.logical_port_id)
        ],
    }
    if foreign != baseline:
        raise ValueError("vNIC unrelated baseline inventory changed")
    return slot


async def _capture(hmc, packet, document, output, budget):
    before = document["baseline"] = await snapshot(hmc, packet)
    if packet["baseline_sha256"] is None or digest(before) != packet["baseline_sha256"]:
        raise ValueError("vNIC supplied baseline differs from fresh inventory")
    await _eligible(hmc, packet)
    identity = before["identity"]
    prefix = f"chhwres -r virtualio --rsubtype vnic -o {{operation}} -m {shlex.quote(identity['system'])} -p {shlex.quote(identity['lpar'])}"
    payload = build_vnic_add_attributes(
        vnic._add_payload(_selector(packet)),
        packet["port_vlan_id"],
        policy_attributes(creation_policy(**packet["policy"])),
    )
    budget.arm("create", f"{prefix.format(operation='a')} -a {shlex.quote(payload)}")
    added = await vnic.add_vnic(
        hmc,
        packet["lpar_uuid"],
        _selector(packet),
        packet["port_vlan_id"],
        system_name_or_uuid=packet["system_uuid"],
        **packet["policy"],
    )
    document["create"] = _plain(asdict(added))
    save(output, document)
    selected = bool(policy_attributes(creation_policy(**packet["policy"])))
    expected = "verified" if selected else "not_requested"
    if (
        not added.mutation_dispatched
        or added.changed is not True
        or added.errors
        or len(added.vnic_after) != 1
        or len(added.backing_after) != 1
        or added.policy_verification.status != expected
    ):
        raise ValueError(
            "vNIC creation is not completely verified; no automatic cleanup"
        )
    document["optional_policy_verified"] = selected
    current = document["cleanup_snapshot"] = await snapshot(hmc, packet)
    slot = _cleanup_candidate(packet, before, current, document["create"])
    budget.arm("remove", prefix.format(operation="r") + " -s " + shlex.quote(slot))
    removed = await vnic.remove_vnic(
        hmc, packet["lpar_uuid"], slot, system_name_or_uuid=packet["system_uuid"]
    )
    document["remove"] = _plain(asdict(removed))
    save(output, document)
    after = document["after"] = await snapshot(hmc, packet)
    if (
        not removed.mutation_dispatched
        or removed.changed is not True
        or removed.errors
        or after != before
    ):
        raise ValueError("vNIC removal did not restore the captured baseline")
    document["status"] = "CLEAN"


def _history_complete(packet, journal, current) -> bool:
    if not isinstance(journal, dict) or not (
        journal.get("group") == GROUP
        and journal.get("stage") == "capture"
        and journal.get("packet_sha256") == digest(packet)
        and journal.get("expected_commit") == packet["expected_commit"]
        and journal.get("complete") is True
        and journal.get("status") == "CLEAN"
        and journal.get("intents") == ["create", "remove"]
        and journal.get("baseline") == current == journal.get("after")
        and digest(current) == packet["baseline_sha256"]
    ):
        return False
    for name, operation in (("create", "add"), ("remove", "remove")):
        result = journal.get(name)
        if not isinstance(result, dict) or not (
            result.get("operation") == operation
            and result.get("mutation_dispatched") is True
            and result.get("changed") is True
            and result.get("errors") == []
            and result.get("vnic_after_read_succeeded") is True
            and result.get("backing_after_read_succeeded") is True
        ):
            return False
    created = journal["create"]
    selected = creation_policy(**packet["policy"])
    observed = creation_policy(**created["vnic_after"][0]["policy"])
    verification = verify_policy(selected, observed)
    if (
        verification.status not in ("verified", "not_requested")
        or created.get("policy_verification") != _plain(asdict(verification))
        or journal["remove"].get("slot_num") != created.get("slot_num")
    ):
        return False
    _cleanup_candidate(packet, current, journal["cleanup_snapshot"], created)
    return True


def _source_command(packet, identity, slot) -> str:
    _, attribute, value, _ = _case(packet)
    record = build_attribute_record([(attribute, value)], surface="`chhwres -a`")
    return (
        "chhwres -r virtualio --rsubtype vnic -o s"
        f" -m {shlex.quote(identity['system'])} -p {shlex.quote(identity['lpar'])}"
        f" -s {shlex.quote(slot)} -a {shlex.quote(record)}"
    )


def _source_compare(packet, before, after, slot) -> tuple[str, str]:
    field, _, _, oracle = _case(packet)
    old, new = _source_row(before, slot), _source_row(after, slot)
    available = getattr(observed_policy(new), field) is not None
    policy = (
        "unavailable"
        if not available
        else "verified"
        if new.get(field) == oracle
        else "mismatched"
    )
    preserved = deepcopy(after)
    _source_row(preserved, slot)[field] = old[field]
    return policy, "unchanged" if preserved == before else "drifted"


def _source_cleanup(packet, baseline, before, after, created) -> str:
    slot = created["slot_num"]
    if _source_compare(packet, before, after, slot) != ("verified", "unchanged"):
        raise ValueError("source selected policy or preservation is unqualified")
    field, _, _, oracle = _case(packet)
    expected_packet, expected_create = deepcopy(packet), deepcopy(created)
    expected_packet["policy"][field] = oracle
    expected_create["vnic_after"] = [
        _plain(asdict(vnic._parse_vnic_snapshots([_source_row(after, slot)])[0]))
    ]
    return _cleanup_candidate(expected_packet, baseline, after, expected_create)


@contextmanager
def _source_remove_guard(qualified, budget):
    read_vnics, read_backings = vnic.list_vnic_rows, vnic.list_vnic_backing_rows
    identity = qualified["identity"]
    hmc, packet = budget.hmc, budget.packet
    read_target, read_members = hmc.get_logical_partition, hmc.list_logical_partitions
    read_description = ownership._read_lpar_description_for_authorization
    budget.remove_reads.clear()
    evidence = budget.document["removal_observations"] = {}

    def fixture(entry):
        resource = entry["Resource"]
        if str(resource["PartitionID"]) != str(identity["id"]):
            raise ValueError("source fresh removal numeric identity drifted")
        value = {
            "lpar": resource["PartitionName"],
            "id": identity["id"],
            "state": resource["PartitionState"],
            "description": _description_text(resource["Description"]),
        }
        expected = {key: identity[key] for key in value}
        if (
            str(entry["UUID"]).casefold() != packet["lpar_uuid"].casefold()
            or value != expected
        ):
            raise ValueError("source fresh removal fixture identity drifted")
        return value

    async def checked_target(uuid):
        entry = await read_target(uuid)
        if "remove" not in budget.dispatched:
            if uuid != packet["lpar_uuid"]:
                raise ValueError("source removal target selector differs")
            evidence["target"] = fixture(entry)
            budget.remove_reads.add("target")
        return entry

    async def checked_members(uuid):
        entries = await read_members(uuid)
        if "remove" not in budget.dispatched:
            if uuid != packet["system_uuid"]:
                raise ValueError("source removal containment selector differs")
            selected = [
                entry
                for entry in entries
                if str(entry.get("UUID", "")).casefold()
                == packet["lpar_uuid"].casefold()
            ]
            if len(selected) != 1:
                raise ValueError("source removal containment is ambiguous")
            evidence["members"] = fixture(selected[0])
            budget.remove_reads.add("members")
        return entries

    async def checked_description(client, system, lpar, **kwargs):
        description = await read_description(client, system, lpar, **kwargs)
        if "remove" not in budget.dispatched:
            semantic = _description_text(description, native=True)
            if (
                client is not hmc
                or system != identity["system"]
                or lpar != identity["lpar"]
                or semantic != identity["description"]
            ):
                raise ValueError("source fresh removal ownership drifted")
            evidence["description"] = semantic
            budget.remove_reads.add("description")
        return description

    async def checked_vnics(config, system, lpar):
        rows = await read_vnics(config, system, lpar)
        if "remove" not in budget.dispatched:
            if (
                config is not budget.hmc.config
                or system != identity["system"]
                or lpar != identity["lpar"]
                or sorted(rows, key=lambda row: row["slot_num"]) != qualified["vnics"]
            ):
                raise ValueError("source fresh removal vNIC inventory drifted")
            evidence["vnics"] = sorted(rows, key=lambda row: row["slot_num"])
            budget.remove_reads.add("vnics")
        return rows

    async def checked_backings(config, system):
        rows = await read_backings(config, system)
        if "remove" not in budget.dispatched:
            if (
                config is not budget.hmc.config
                or system != identity["system"]
                or sorted(
                    rows, key=lambda row: (row["adapter_id"], row["logical_port_id"])
                )
                != qualified["backings"]
            ):
                raise ValueError("source fresh removal backing inventory drifted")
            evidence["backings"] = sorted(
                rows, key=lambda row: (row["adapter_id"], row["logical_port_id"])
            )
            budget.remove_reads.add("backings")
        return rows

    try:
        with (
            patch.object(hmc, "get_logical_partition", checked_target),
            patch.object(hmc, "list_logical_partitions", checked_members),
            patch.object(
                ownership,
                "_read_lpar_description_for_authorization",
                checked_description,
            ),
            patch.object(vnic, "list_vnic_rows", checked_vnics),
            patch.object(vnic, "list_vnic_backing_rows", checked_backings),
        ):
            yield
    finally:
        budget.remove_reads.clear()


def _source_remove_before(qualified, slot, removed) -> bool:
    observed = vnic._parse_vnic_snapshots([_source_row(qualified, slot)])[0]
    if len(observed.backing_devices) != 1:
        return False
    embedded = observed.backing_devices[0]
    matches = [
        row
        for row in qualified["backings"]
        if (row["adapter_id"], row["logical_port_id"])
        == (embedded.adapter_id, embedded.logical_port_id)
    ]
    if len(matches) != 1:
        return False
    backing = vnic._parse_backing_snapshot(matches[0])
    selector = vnic.VnicBackingSelector(
        backing.vios_name,
        backing.vios_lpar_id,
        backing.adapter_id,
        backing.physical_port_id,
        backing.desired_capacity_percent,
    )
    return (
        removed.get("slot_num") == slot
        and removed.get("vnic_before") == [_plain(asdict(observed))]
        and removed.get("backing_before") == [_plain(asdict(backing))]
        and removed.get("selector") == _plain(asdict(selector))
    )


async def _capture_source(hmc, packet, document, output, budget):
    before = document["baseline"] = await snapshot(hmc, packet)
    if packet["baseline_sha256"] is None or digest(before) != packet["baseline_sha256"]:
        raise ValueError("source supplied baseline differs from fresh inventory")
    await _eligible(hmc, packet)
    identity = before["identity"]
    prefix = (
        "chhwres -r virtualio --rsubtype vnic -o {operation}"
        f" -m {shlex.quote(identity['system'])} -p {shlex.quote(identity['lpar'])}"
    )
    payload = build_vnic_add_attributes(
        vnic._add_payload(_selector(packet)),
        packet["port_vlan_id"],
        policy_attributes(creation_policy(**packet["policy"])),
    )
    budget.arm("create", f"{prefix.format(operation='a')} -a {shlex.quote(payload)}")
    added = await vnic.add_vnic(
        hmc,
        packet["lpar_uuid"],
        _selector(packet),
        packet["port_vlan_id"],
        system_name_or_uuid=packet["system_uuid"],
        **packet["policy"],
    )
    created = document["create"] = _plain(asdict(added))
    save(output, document)
    if (
        not added.mutation_dispatched
        or added.changed is not True
        or added.errors
        or len(added.vnic_after) != 1
        or len(added.backing_after) != 1
        or added.policy_verification.status != "verified"
    ):
        raise ValueError("source creation is incomplete; cleanup disarmed")
    b1 = await snapshot(hmc, packet)
    slot = _cleanup_candidate(packet, before, b1, created)
    row = _source_row(b1, slot)
    _source_complete(row)
    parsed = vnic._parse_vnic_snapshots([row])[0]
    if len(parsed.backing_devices) != 1 or not parsed.backing_devices[0].is_active:
        raise ValueError("source requires one active captured backing")
    if parsed.backing_devices[0].status != "Operational":
        raise ValueError("source requires Operational captured backing")
    authorize_lpar_ownership_description(
        hmc,
        identity["system"],
        identity["lpar"],
        b1["identity"]["description"],
        operation="lpar-mutation",
    )
    field, attribute, value, oracle = _case(packet)
    source = document["source"] = {
        "captured_slot": slot,
        "before_update": b1,
        "native_record": build_attribute_record(
            [(attribute, value)], surface="`chhwres -a`"
        ),
        "expected": {"field": field, "value": oracle},
        "submission": "not_dispatched",
        "remote_refusal_code": None,
        "after_update": None,
        "selected_after": None,
        "policy_verification": "unavailable",
        "preservation": "unavailable",
        "qualification": "not_attempted",
    }
    command = _source_command(packet, identity, slot)
    budget.arm("update", command)
    try:
        await transport.run_hmc_command(hmc.config, command)
        source["submission"] = "completed"
    except Exception as error:  # noqa: BLE001 - every dispatched failure is private uncertainty, never retried
        source["submission"] = (
            "unknown" if "update" in budget.dispatched else "not_dispatched"
        )
        if (
            source["submission"] == "unknown"
            and isinstance(error, transport.HMCCLIError)
            and error.remote_nonzero_completed
            and error.remote_refusal_code
        ):
            source["submission"] = "rejected"
            source["remote_refusal_code"] = error.remote_refusal_code
        source["error"] = type(error).__name__
    save(output, document)
    try:
        b2 = source["after_update"] = await snapshot(hmc, packet, source_result=source)
        source["policy_verification"], source["preservation"] = _source_compare(
            packet, b1, b2, slot
        )
    except Exception as error:  # noqa: BLE001 - incomplete observation cannot authorize cleanup
        source["observation_error"] = type(error).__name__
    if source["submission"] == "completed" and (
        source["policy_verification"],
        source["preservation"],
    ) == ("verified", "unchanged"):
        source["qualification"] = "qualified"
    elif source["submission"] == "rejected" and source["after_update"] == b1:
        source["qualification"] = "refused"
    elif source["submission"] != "not_dispatched":
        source["qualification"] = "unresolved"
    save(output, document)
    if source["qualification"] != "qualified":
        raise ValueError("source case is not qualified; cleanup disarmed")
    slot = _source_cleanup(packet, before, b1, source["after_update"], created)
    document["cleanup_snapshot"] = source["after_update"]
    budget.arm("remove", prefix.format(operation="r") + " -s " + shlex.quote(slot))
    with _source_remove_guard(source["after_update"], budget):
        removed = await vnic.remove_vnic(
            hmc, packet["lpar_uuid"], slot, system_name_or_uuid=packet["system_uuid"]
        )
    document["remove"] = _plain(asdict(removed))
    save(output, document)
    if not _source_remove_before(source["after_update"], slot, document["remove"]):
        raise ValueError("source removal before-state contradicts qualified object")
    after = document["after"] = await snapshot(hmc, packet)
    if (
        not removed.mutation_dispatched
        or removed.changed is not True
        or removed.errors
        or after != before
    ):
        raise ValueError("source removal did not restore captured baseline")
    document["status"] = "CLEAN"


def _source_history(packet, journal, current) -> bool:
    if not isinstance(journal, dict) or not (
        journal.get("group") == GROUP
        and journal.get("stage") == "capture"
        and journal.get("version") == 2
        and journal.get("purpose") == "update-mode-source"
        and journal.get("evidence_class") == "source-only"
        and journal.get("case") == packet["case"]
        and journal.get("packet_sha256") == digest(packet)
        and journal.get("expected_commit") == packet["expected_commit"]
        and journal.get("complete") is True
        and journal.get("status") == "CLEAN"
        and journal.get("intents") == ["create", "update", "remove"]
        and journal.get("session") == {"logged_on": True, "logged_off": True}
        and journal.get("baseline") == current == journal.get("after")
        and digest(current) == packet["baseline_sha256"]
    ):
        return False
    created, removed, source = journal["create"], journal["remove"], journal["source"]
    slot = created["slot_num"]
    for result, operation in ((created, "add"), (removed, "remove")):
        if not (
            result.get("operation") == operation
            and result.get("mutation_dispatched") is True
            and result.get("changed") is True
            and result.get("errors") == []
            and result.get("vnic_after_read_succeeded") is True
            and result.get("backing_after_read_succeeded") is True
            and result.get("slot_num") == slot
        ):
            return False
    if len(created["vnic_after"]) != 1 or len(created["backing_after"]) != 1:
        return False
    selected = creation_policy(**packet["policy"])
    observed = creation_policy(**created["vnic_after"][0]["policy"])
    if created.get("policy_verification") != _plain(
        asdict(verify_policy(selected, observed))
    ):
        return False
    if removed.get("vnic_after") != [] or removed.get("backing_after") != []:
        return False
    b1, b2 = source["before_update"], source["after_update"]
    identity = current["identity"]
    prefix = (
        "chhwres -r virtualio --rsubtype vnic -o {operation}"
        f" -m {shlex.quote(identity['system'])} -p {shlex.quote(identity['lpar'])}"
    )
    payload = build_vnic_add_attributes(
        vnic._add_payload(_selector(packet)),
        packet["port_vlan_id"],
        policy_attributes(selected),
    )
    commands = [
        {
            "kind": "create",
            "command": f"{prefix.format(operation='a')} -a {shlex.quote(payload)}",
        },
        {"kind": "update", "command": _source_command(packet, identity, slot)},
        {
            "kind": "remove",
            "command": prefix.format(operation="r") + " -s " + shlex.quote(slot),
        },
    ]
    if journal.get("effect_commands") != commands:
        return False
    fixture = {
        key: b2["identity"][key] for key in ("lpar", "id", "state", "description")
    }
    expected_observations = {
        "target": fixture,
        "members": fixture,
        "description": fixture["description"],
        "vnics": b2["vnics"],
        "backings": b2["backings"],
    }
    if journal.get("removal_observations") != expected_observations:
        return False
    if _cleanup_candidate(packet, current, b1, created) != slot:
        return False
    _source_complete(_source_row(b1, slot))
    field, attribute, value, oracle = _case(packet)
    expected_record = build_attribute_record(
        [(attribute, value)], surface="`chhwres -a`"
    )
    if not (
        source.get("captured_slot") == slot
        and source.get("native_record") == expected_record
        and source.get("expected") == {"field": field, "value": oracle}
        and source.get("submission") == "completed"
        and source.get("remote_refusal_code") is None
        and source.get("qualification") == "qualified"
        and source.get("policy_verification") == "verified"
        and source.get("preservation") == "unchanged"
        and journal.get("cleanup_snapshot") == b2
        and source.get("selected_after") == _source_row(b2, slot)
        and _source_remove_before(b2, slot, removed)
        and _source_compare(packet, b1, b2, slot) == ("verified", "unchanged")
    ):
        return False
    return _source_cleanup(packet, current, b1, b2, created) == slot


def _fixture_read(path, limit):
    target = Path(path)
    _private(target, directory=False)
    with target.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("fixture evidence input exceeds its byte bound")
    return _json(raw), {"path": str(target), "sha256": hashlib.sha256(raw).hexdigest()}


def _fixture_closed(document, packet, stage, status):
    limits = FIXTURE_LIMITS[stage]
    counts = document.get("counts")
    return (
        type(document.get("version")) is int
        and document.get("version") == 3
        and document.get("purpose") == "vnic-source-fixture"
        and document.get("group") == GROUP
        and document.get("stage") == stage
        and document.get("packet_sha256") == digest(packet)
        and document.get("packet") == packet
        and document.get("complete") is True
        and document.get("status") == status
        and document.get("session") == {"logged_on": True, "logged_off": True}
        and isinstance(counts, dict)
        and set(counts) == {"rest", "ssh"}
        and all(
            type(counts[k]) is int and 0 <= counts[k] <= limit
            for k, limit in zip(("rest", "ssh"), limits[:2], strict=True)
        )
    )


def _fixture_prepared(hmc, packet, original):
    p = original["packet"]
    _validate_fixture(p)
    if p["action"] != "prepare" or not _fixture_closed(
        original, p, "prepare", "PREPARED"
    ):
        raise ValueError("fixture original preparation is incomplete")
    if any(
        p[k] != packet[k]
        for k in (
            "expected_commit",
            "configured_profile",
            "hmc_identity",
            "system_uuid",
            "fixture_name",
            "caller_token",
        )
    ):
        raise ValueError("fixture original preparation binding differs")
    baseline, held, before = (
        original["baseline"],
        original["retained"],
        original["before_stamp"],
    )
    if p["baseline_sha256"] != digest(baseline):
        raise ValueError("fixture original baseline digest differs")
    target = _fixture_target(p, held["snapshot"], baseline)
    if (
        held["target"] != target
        or held["vnics"] != []
        or original["returned"]
        != {"resource_created": True, "ownership_stamped": True, "uuid": target["uuid"]}
    ):
        raise ValueError(
            "fixture original retained identity or creation result differs"
        )
    _fixture_owned(hmc, p, held)
    if held["description"] != _description_text(
        target["resource"].get("Description")
    ) or held["description"] != _description_text(
        held["native_description_raw"], native=True
    ):
        raise ValueError("fixture retained description contradicts current resource")
    prior = _fixture_target(p, before["snapshot"], baseline)
    if (
        before["target"] != prior
        or before["vnics"] != []
        or prior["uuid"] != target["uuid"]
    ):
        raise ValueError("fixture original pre-stamp identity differs")
    expected_prior = deepcopy(prior)
    expected_prior["resource"]["Description"] = target["resource"]["Description"]
    if expected_prior != target:
        raise ValueError("fixture changed beyond ownership during stamping")
    commands = _fixture_commands(p, baseline["system"]["name"], held["description"])
    body = build_lpar_document(
        name=p["fixture_name"],
        partition_type="AIX/Linux",
        resources=_fixture_resources(),
        max_virtual_slots=8,
    )
    commands["create-rest"] = {
        "method": "PUT",
        "path": f"/rest/api/uom/ManagedSystem/{p['system_uuid']}/LogicalPartition",
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }
    effects = original["effects"]
    order = [(e["kind"], e["settlement"]) for e in effects]
    if order not in (
        [("create-rest", "acknowledged"), ("stamp", "acknowledged")],
        [
            ("create-rest", "refused"),
            ("create-native", "acknowledged"),
            ("stamp", "acknowledged"),
        ],
    ):
        raise ValueError("fixture original effect history is unconfirmed")
    if any(e["signature"] != commands[e["kind"]] for e in effects):
        raise ValueError("fixture original effect signature differs")
    return held


async def _fixture_inputs(hmc, packet, *, source_baselines=None):
    original, binding = _fixture_read(packet["prepare_journal"], 5 * MIB)
    _fixture_prepared(hmc, packet, original)
    sources, bindings = [], {"prepare": binding, "sources": []}
    for index, paths in enumerate(packet["source_evidence"]):
        source_packet, packet_binding = _fixture_read(paths["packet"], MIB)
        capture_record, capture_binding = _fixture_read(paths["capture"], 5 * MIB)
        recovery_record, recovery_binding = _fixture_read(paths["recovery"], 5 * MIB)
        if not _source(source_packet):
            raise ValueError("fixture source evidence is not version 2")
        validate_packet(source_packet)
        captured = original["retained"]["target"]
        expected = {
            "expected_commit": packet["expected_commit"],
            "system_uuid": packet["system_uuid"],
            "lpar_uuid": captured["uuid"],
            "expected_lpar_id": int(captured["id"]),
            "owner_token": packet["caller_token"],
            "expected_lpar_state": "not activated",
            "configured_profile": packet["configured_profile"],
            "hmc_identity": packet["hmc_identity"],
        }
        if any(source_packet[k] != value for k, value in expected.items()):
            raise ValueError("fixture source evidence targets a different context")
        current = (
            await snapshot(hmc, source_packet)
            if source_baselines is None
            else source_baselines[index]
        )
        if not _source_history(source_packet, capture_record, current):
            raise ValueError(
                "fixture source capture is not independently qualified and restored"
            )
        if not (
            recovery_record.get("group") == GROUP
            and recovery_record.get("stage") == "recover"
            and recovery_record.get("version") == 2
            and recovery_record.get("purpose") == "update-mode-source"
            and recovery_record.get("evidence_class") == "source-only"
            and recovery_record.get("case") == source_packet["case"]
            and recovery_record.get("packet_sha256") == digest(source_packet)
            and recovery_record.get("expected_commit") == packet["expected_commit"]
            and recovery_record.get("complete") is True
            and recovery_record.get("status") == "CLEAN"
            and recovery_record.get("intents") == []
            and recovery_record.get("baseline") == current
            and recovery_record.get("session")
            == {"logged_on": True, "logged_off": True}
        ):
            raise ValueError("fixture source recovery is missing or unconfirmed")
        sources.append(current)
        bindings["sources"].append(
            {
                "packet": packet_binding,
                "capture": capture_binding,
                "recovery": recovery_binding,
            }
        )
    if source_baselines is not None and len(source_baselines) != len(sources):
        raise ValueError("fixture declared source histories differ")
    return original, sources, bindings


def _fixture_absent(packet, current):
    if (
        len(current["partitions"]) > 4
        or current["profiles"] > 8
        or any(
            item["name"] == packet["fixture_name"]
            or parse_lpar_ownership_caller_token(
                _scalar_text(item["resource"].get("Description", ""))
            )
            == packet["caller_token"]
            for item in current["partitions"].values()
        )
    ):
        raise ValueError(
            "fixture name/caller already exists or original inventory exceeds bounds"
        )


async def _fixture_recover(hmc, packet, document, journal):
    stage = journal["stage"]
    status = {"preflight": "READY", "prepare": "PREPARED", "cleanup": "CLEAN"}.get(
        stage
    )
    if (
        stage not in ("preflight", "prepare", "cleanup")
        or journal.get("group") != GROUP
        or type(journal.get("version")) is not int
        or journal.get("version") != 3
        or journal.get("purpose") != "vnic-source-fixture"
        or journal.get("packet_sha256") != digest(packet)
        or journal.get("packet") != packet
        or (stage in ("prepare", "cleanup") and stage != packet["action"])
    ):
        raise ValueError("fixture original stage binding is unavailable")
    if not _fixture_closed(journal, packet, stage, status):
        document["recovery_observation_attempted"] = True
        document["observation"] = await _fixture_snapshot(hmc, packet)
        raise ValueError(
            "fixture failed history remains uncertain after independent observation"
        )
    if stage == "prepare":
        expected = _fixture_prepared(hmc, packet, journal)
        current = await _fixture_held(hmc, packet, journal["baseline"])
        _fixture_owned(hmc, packet, current)
        if current != expected:
            raise ValueError("retained fixture differs during outside recovery")
        document.update(status="RETAINED", observation=current)
        return
    if packet["action"] == "cleanup":
        original, _sources, bindings = await _fixture_inputs(
            hmc, packet, source_baselines=journal["source_baselines"]
        )
        if original != journal["original"] or bindings != journal["input_bindings"]:
            raise ValueError("fixture original evidence bytes changed")
    if stage == "preflight":
        if journal["effects"]:
            raise ValueError("fixture preflight history contains effects")
        current = (
            await _fixture_snapshot(hmc, packet)
            if packet["action"] == "prepare"
            else await _fixture_held(hmc, packet, original["baseline"])
        )
        if current != journal["observation"]:
            raise ValueError("fixture preflight baseline changed during recovery")
        document.update(status="UNCHANGED", observation=current)
        return
    target = original["retained"]["target"]
    if journal["effects"] != [
        {
            "kind": "delete",
            "signature": {
                "method": "DELETE",
                "path": f"/rest/api/uom/LogicalPartition/{target['uuid']}",
            },
            "settlement": "acknowledged",
        }
    ]:
        raise ValueError("fixture cleanup effect history is incomplete")
    if (
        journal["delete_observation"] != journal["qualified"]
        or journal["qualified"] != original["retained"]
    ):
        raise ValueError("fixture cleanup protected-state history differs")
    current = await _fixture_snapshot(hmc, packet)
    _fixture_absent(packet, current)
    if current != original["baseline"] or journal["after"] != current:
        raise ValueError("fixture cleanup baseline is not independently restored")
    document.update(status="CLEAN", observation=current)


async def _fixture_run(hmc, packet, stage, output=None, journal=None):
    _context(hmc, packet, stage)
    action = packet["action"] if stage == "capture" else stage
    document = {
        "group": GROUP,
        "version": 3,
        "purpose": "vnic-source-fixture",
        "stage": action,
        "packet": packet,
        "packet_sha256": digest(packet),
        "complete": False,
        "status": "MANUAL_RECOVERY",
        "effects": [],
    }
    if output is not None:
        _private(output.parent, directory=True)
        output.mkdir(mode=0o700)
        _private(output, directory=True)
        save(output, document)
    budget = _FixtureBudget(hmc, packet, action, document, output)
    try:
        with _fixture_bounded(budget):
            async with hmc:
                if stage == "recover":
                    await _fixture_recover(hmc, packet, document, journal)
                else:
                    if packet["action"] == "prepare":
                        current = await _fixture_snapshot(hmc, packet)
                        _fixture_absent(packet, current)
                        _fixture_capacity(
                            {
                                "AssociatedSystemMemoryConfiguration": current[
                                    "system"
                                ]["memory"],
                                "AssociatedSystemProcessorConfiguration": current[
                                    "system"
                                ]["cpu"],
                            }
                        )
                    else:
                        original, sources, bindings = await _fixture_inputs(hmc, packet)
                        document.update(
                            original=original,
                            source_baselines=sources,
                            input_bindings=bindings,
                        )
                        current = await _fixture_held(hmc, packet, original["baseline"])
                        _fixture_owned(hmc, packet, current)
                        if current != original["retained"]:
                            raise ValueError(
                                "fixture differs from its retained empty baseline"
                            )
                    document["observation"] = current
                    if stage == "preflight":
                        if packet["baseline_sha256"] not in (None, digest(current)):
                            raise ValueError(
                                "fixture preflight supplied baseline differs"
                            )
                        document.update(status="READY", baseline_sha256=digest(current))
                    else:
                        if packet["baseline_sha256"] != digest(current):
                            raise ValueError(
                                "fixture capture requires its fresh bound baseline"
                            )
                        if action == "prepare":
                            document["baseline"] = current
                            result = await lpar_core.create_and_stamp_lpar(
                                hmc,
                                packet["system_uuid"],
                                lpar_core.LparCreation(
                                    packet["fixture_name"],
                                    "AIX/Linux",
                                    _fixture_resources(),
                                    max_virtual_slots=8,
                                    caller_token=packet["caller_token"],
                                    stamp_policy="required",
                                    apply_profile=False,
                                ),
                            )
                            if result.lpar is None:
                                raise ValueError(
                                    "fixture creation returned no identity"
                                )
                            document["returned"] = {
                                "resource_created": result.resource_created,
                                "ownership_stamped": result.ownership_stamped,
                                "uuid": result.lpar["UUID"].casefold(),
                            }
                            held = await _fixture_held(hmc, packet, current)
                            _fixture_owned(hmc, packet, held)
                            document["retained"] = held
                            document["status"] = "PREPARED"
                        else:
                            document["baseline"] = original["baseline"]
                            budget.captured = original["retained"]["target"]
                            budget.qualified = document["qualified"] = current
                            removed = await lpar_core.delete_lpar(
                                hmc,
                                budget.captured["uuid"],
                                system_name_or_uuid=packet["system_uuid"],
                                ownership_override=False,
                            )
                            if removed.casefold() != budget.captured["uuid"]:
                                raise ValueError(
                                    "fixture delete returned a different UUID"
                                )
                            after = document["after"] = await _fixture_snapshot(
                                hmc, packet
                            )
                            _fixture_absent(packet, after)
                            if after != original["baseline"]:
                                raise ValueError(
                                    "fixture deletion did not restore its original baseline"
                                )
                            document["status"] = "CLEAN"
        if not budget.logged_on or not budget.logged_off:
            raise ValueError("fixture own session closure is unconfirmed")
        document["complete"] = True
        if action == "prepare":
            document.update(
                counts=budget.counts,
                session={
                    "logged_on": budget.logged_on,
                    "logged_off": budget.logged_off,
                },
            )
            _fixture_prepared(hmc, packet, document)
    except Exception as error:  # noqa: BLE001 - persist uncertain stage without retry or cleanup authority
        document.update(
            status="MANUAL_RECOVERY",
            complete=False,
            error=type(error).__name__,
            reason=str(error),
        )
    document.update(
        counts=budget.counts,
        session={"logged_on": budget.logged_on, "logged_off": budget.logged_off},
    )
    if output is not None:
        save(output, document)
    return document


async def _stage(hmc, packet, stage, output=None, journal=None):
    if _fixture(packet):
        return await _fixture_run(hmc, packet, stage, output, journal)
    _context(hmc, packet, stage)
    document = {
        "group": GROUP,
        "stage": stage,
        "packet_sha256": digest(packet),
        "expected_commit": packet["expected_commit"],
        "complete": False,
        "status": "MANUAL_RECOVERY",
        "intents": [],
        "optional_policy_verified": False,
    }
    if _source(packet):
        document.update(
            version=2,
            purpose="update-mode-source",
            evidence_class="source-only",
            case=packet["case"],
        )
        document.pop("optional_policy_verified")
    if output is not None:
        _private(output.parent, directory=True)
        output.mkdir(mode=0o700)
        _private(output, directory=True)
        save(output, document)
    budget = _Budget(hmc, packet, stage, document, output)
    try:
        with _bounded(budget):
            async with hmc:
                if stage == "capture":
                    await (_capture_source if _source(packet) else _capture)(
                        hmc, packet, document, output, budget
                    )
                else:
                    current = document["baseline"] = await snapshot(hmc, packet)
                    if stage == "preflight":
                        await _eligible(hmc, packet)
                        document.update(status="READY", baseline_sha256=digest(current))
                        if packet["baseline_sha256"] not in (None, digest(current)):
                            raise ValueError("vNIC preflight baseline differs")
                    elif (_source_history if _source(packet) else _history_complete)(
                        packet, journal, current
                    ):
                        document["status"] = "CLEAN"
        if not budget.logged_on or not budget.logged_off:
            raise ValueError("vNIC procedure session closure is uncertain")
        document["complete"] = True
    except vnic.VnicPartialError as error:
        document.update(status="MANUAL_RECOVERY", partial=_plain(asdict(error.result)))
    except SriovLogicalPortCapabilityError:
        document["status"] = "SKIP" if stage == "preflight" else "MANUAL_RECOVERY"
    except Exception as error:  # noqa: BLE001 - persist uncertainty, never retry or clean up on failure
        document.update(
            status="MANUAL_RECOVERY", error=type(error).__name__, reason=str(error)
        )
    if _source(packet):
        document["session"] = {
            "logged_on": budget.logged_on,
            "logged_off": budget.logged_off,
        }
        if not document["complete"] and document["status"] != "SKIP":
            document["status"] = "MANUAL_RECOVERY"
    document["counts"] = budget.counts
    if output is not None:
        save(output, document)
    return document


async def preflight(hmc: HMCClient, packet: dict, output: Path) -> dict:
    return await _stage(hmc, packet, "preflight", output)


async def capture(hmc: HMCClient, packet: dict, output: Path) -> dict:
    return await _stage(hmc, packet, "capture", output)


async def recover(hmc: HMCClient, packet: dict, journal: dict) -> dict:
    return await _stage(hmc, packet, "recover", journal=journal)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=tuple(LIMITS))
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--packet-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--results", type=Path)
    args = parser.parse_args(argv)
    try:
        _private(args.packet, directory=False)
        with args.packet.open("rb") as stream:
            raw = stream.read(MIB + 1)
        if len(raw) > MIB or hashlib.sha256(raw).hexdigest() != args.packet_sha256:
            raise ValueError("vNIC packet byte bound or digest differs")
        packet = _json(raw)
        validate_packet(packet)
        config = load_profile(packet["configured_profile"])
        config = config.model_copy(
            update={"port": config.port, "max_response_bytes": MIB}
        )
        if (args.stage == "recover") != (args.results is not None):
            raise ValueError("vNIC recovery alone requires original results")
        journal = _read_private(args.results, 5 * MIB) if args.results else None
        result = asyncio.run(
            _stage(HMCClient(config), packet, args.stage, args.output, journal)
        )
    except Exception as error:  # noqa: BLE001 - operator output never includes private packet or HMC data
        print(f"vNIC procedure held: {type(error).__name__}", file=sys.stderr)
        return 2
    label = (
        "vnic-fixture PREREQUISITE ONLY"
        if _fixture(packet)
        else "vnic-update-source SOURCE ONLY"
        if _source(packet)
        else GROUP
    )
    print(f"{label}: {result['status']}")
    accepted = (
        ("READY", "CLEAN", "SKIP", "PREPARED", "RETAINED", "UNCHANGED")
        if _fixture(packet)
        else ("READY", "CLEAN", "SKIP")
    )
    return 0 if result["status"] in accepted else 2
