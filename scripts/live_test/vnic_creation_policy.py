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
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import hmcpctl
from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig, load_profile
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
from hmcpctl.ssh.profiles import get_lpar_description
from hmcpctl.ssh.sriov import list_sriov_configured_logical_port_rows
from hmcpctl.ssh.vnic import (
    build_vnic_add_attributes,
    list_vnic_backing_rows,
    list_vnic_rows,
)

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


def _source(packet: dict) -> bool:
    return type(packet.get("version")) is int and packet["version"] == 2


def validate_packet(packet: dict) -> None:
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
    def __init__(self, hmc, packet, stage, document, output):
        self.hmc, self.packet, self.document, self.output = (
            hmc,
            packet,
            document,
            output,
        )
        self.rest_limit, self.ssh_limit, seconds = (
            SOURCE_LIMITS if _source(packet) else LIMITS
        )[stage]
        self.dispatched: set[str] = set()
        self.remove_reads: set[str] = set()
        self.deadline = time.monotonic() + seconds
        self.counts = {"rest": 0, "ssh": 0}
        self.armed: tuple[str, str] | None = None
        self.spent: set[str] = set()
        self.logged_on = self.logged_off = False

    def take(self, kind: str, *, logout: bool = False) -> float:
        reserve = 15 if _source(self.packet) and not logout else 0
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
    ):
        yield


def _plain(value):
    return json.loads(json.dumps(value, default=str))


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
    description = await get_lpar_description(hmc.config, system_name, lpar_name)
    if (
        description != resource.get("Description")
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
            "description": resource["Description"],
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
            if (
                client is not hmc
                or system != identity["system"]
                or lpar != identity["lpar"]
                or description != identity["description"]
            ):
                raise ValueError("source fresh removal ownership drifted")
            evidence["description"] = description
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


async def _stage(hmc, packet, stage, output=None, journal=None):
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
    label = "vnic-update-source SOURCE ONLY" if _source(packet) else GROUP
    print(f"{label}: {result['status']}")
    return 0 if result["status"] in ("READY", "CLEAN", "SKIP") else 2
