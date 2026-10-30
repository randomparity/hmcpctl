"""ST69: one retained client Ethernet change, followed by independent read-only recovery."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hmcpctl.client.core import HMCClient
from hmcpctl.config import load_profile
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.ownership import authorize_lpar_mutation
from hmcpctl.operations.virtualization import adapters
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh import ethernet, profiles
from hmcpctl.ssh.commands import build_attribute_record, build_filter
from hmcpctl.xmlutil import console_version

from . import lpar_serial_adapter as files

GROUP = "client-ethernet-update"
MIB = 1024 * 1024
REST_CAP = 256 * 1024
SSH_CAP = REQUEST_CAP = 65536
LIMITS = {
    "preflight": (24, 3, 4 * MIB, 300),
    "run": (24, 3, 8 * MIB, 600),
    "recover": (24, 2, 4 * MIB, 300),
}
CASES: dict[str, dict[str, Any]] = {
    "tagging-enable": {"tagged": True},
    "tagging-disable": {"tagged": False},
    "vlan-replace": {
        "tagged": True,
        "addl_vlan_ids": [101, 102],
        "vlan_action": "replace",
    },
    "vlan-add": {"tagged": True, "addl_vlan_ids": [101], "vlan_action": "add"},
    "vlan-remove": {"addl_vlan_ids": [101], "vlan_action": "remove"},
    "vlan-empty": {"addl_vlan_ids": [], "vlan_action": "replace"},
    "qos-set": {"qos_priority": 3},
    "qos-disable": {"qos_priority": "none"},
    "vsi-set": {"vsi_manager_id": 1, "vsi_type_id": 2, "vsi_type_version": 3},
    "vsi-remove": {"remove_vsi_profile": True},
}
SESSION_KEYS = {
    "logon_sent",
    "logon_acknowledged",
    "logoff_sent",
    "logoff_acknowledged",
    "http_closed",
}
JOURNAL_KEYS = {
    "schema_version",
    "group",
    "packet_sha256",
    "binding",
    "phase",
    "baseline",
    "intent",
    "submission",
    "observations",
    "result",
    "budget",
    "session",
    "errors",
    "complete",
    "preflight_sha256",
    "original_results_sha256",
}
OUTCOMES = {
    "retained_verified_change",
    "observed_drift",
    "unconfirmed",
    "no_write_verified",
}


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("ST69 exact object fields required")


def sha(value, length=64):
    if (
        not isinstance(value, str)
        or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None
    ):
        raise ValueError("ST69 exact digest required")
    return value


def expiry(value):
    if not isinstance(value, str):
        raise TypeError("ST69 UTC expiry required")
    moment = datetime.fromisoformat(value)
    offset = moment.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("ST69 UTC expiry required")
    return moment.timestamp()


def validate_packet(packet: object) -> dict:
    exact(
        packet,
        (
            "schema_version",
            "candidate",
            "hmc_uuid",
            "system_uuid",
            "lpar_uuid",
            "adapter_uuid",
            "case",
            "expires_utc",
            "retained_change_authorized",
        ),
    )
    packet = cast(dict[str, Any], packet)
    exact(packet["candidate"], ("commit", "tree"))
    for field in ("commit", "tree"):
        sha(packet["candidate"][field], 40)
    if (
        type(packet["schema_version"]) is not int
        or packet["schema_version"] != 1
        or packet["retained_change_authorized"] is not True
    ):
        raise ValueError(
            "ST69 version1 and explicit retained-change permission required"
        )
    if not isinstance(packet["case"], str) or packet["case"] not in CASES:
        raise ValueError("ST69 fixed case required")
    result = deepcopy(packet)
    for field in ("hmc_uuid", "system_uuid", "lpar_uuid", "adapter_uuid"):
        if not isinstance(result[field], str) or not is_uuid(result[field]):
            raise ValueError("ST69 UUID identity required")
        result[field] = result[field].casefold()
    if expiry(result["expires_utc"]) <= time.time():
        raise ValueError("ST69 packet expired")
    return result


def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("ST69 duplicate JSON key")
        value[key] = item
    return value


def checked_path(path, *, fresh=False, maximum=4 * MIB):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise ValueError("ST69 canonical absolute private path required")
    return files.private_path(path, fresh=fresh, maximum=maximum)


def read_record(path, maximum=4 * MIB):
    path = checked_path(path, maximum=maximum)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("ST69 private record exceeds byte bound")

    def refuse_nonfinite(_value):
        raise ValueError("ST69 nonfinite JSON refused")

    document = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=no_duplicates,
        parse_constant=refuse_nonfinite,
    )
    return document, hashlib.sha256(raw).hexdigest()


def read_private(path, maximum=4 * MIB):
    return read_record(path, maximum)[0]


def load_packet(path, digest):
    sha(digest)
    packet, observed_digest = read_record(path, MIB)
    if observed_digest != digest:
        raise ValueError("ST69 packet digest differs")
    return validate_packet(packet)


def checkpoint(path, document, *, fresh=False):
    checked_path(path, fresh=fresh)
    files.write(path, document, fresh=fresh, maximum=4 * MIB)


class Budget:
    """Logical cap reservations persist after failed/partial transport; not wire bytes."""

    def __init__(self, phase, expires_utc):
        self.phase = phase
        self.maximum = LIMITS[phase]
        self.used = [0, 0, 0]
        self.started = time.monotonic()
        self.deadline = self.started + self.maximum[3]
        self.expires_utc = expires_utc
        self.expiry = expiry(expires_utc)

    def reserve(self, rest, ssh, size, seconds=0, *, close=False):
        if any(
            u + n > cap
            for u, n, cap in zip(
                self.used, (rest, ssh, size), self.maximum[:3], strict=True
            )
        ):
            raise ValueError("ST69 physical call/logical byte reserve exhausted")
        remaining = self.deadline - time.monotonic()
        if self.phase != "recover" and not close:
            remaining = min(remaining, self.expiry - time.time())
        if remaining < seconds or remaining <= 0:
            raise ValueError("ST69 original deadline reserve unavailable")

    def dispatch(self, kind, size, *, close=False):
        self.reserve(int(kind == 0), int(kind == 1), size, close=close)
        self.used[kind] += 1
        self.used[2] += size

    def admit_effect(self):
        # Post-observation + own logout within phase; independent recovery within packet window.
        self.reserve(9, 1, 9 * REST_CAP + SSH_CAP + REQUEST_CAP, 100)
        if self.expiry - time.time() < 400:
            raise ValueError("ST69 independent recovery time reserve unavailable")

    def record(self):
        return {
            "maximum": list(self.maximum),
            "used": self.used.copy(),
            "deadline": self.deadline,
            "expires_utc": self.expires_utc,
        }


class CountedHMC(HMCClient):
    def __init__(self, config, budget):
        if config.max_response_bytes != REST_CAP:
            raise ValueError("ST69 REST cap must be configured before construction")
        super().__init__(config)
        self._legacy_port_fallback = False
        self.budget = budget
        self.session = dict.fromkeys(SESSION_KEYS, False)
        self.allowed_write = None
        self.allowed_read = None
        self.write_sent = False
        self.native = ethernet.run_hmc_command

    async def _request(self, method, path, **kwargs):
        login = method == "PUT" and path == "/rest/api/web/Logon"
        logout = method == "DELETE" and path == "/rest/api/web/Logon"
        if method != "GET" and not (login or logout):
            raise PermissionError("ST69 REST writes forbidden")
        if login or logout:
            flag = "logon_sent" if login else "logoff_sent"
            if self.session[flag]:
                raise PermissionError("ST69 repeated session action refused")
        body = kwargs.get("content", b"")
        size = len(body.encode() if isinstance(body, str) else body)
        if size > REQUEST_CAP:
            raise ValueError("ST69 request body exceeds bound")
        if not logout:
            self.budget.reserve(2, 0, 2 * REST_CAP + size, 20)
        self.budget.dispatch(0, REST_CAP + size, close=logout)
        if login or logout:
            self.session[flag] = True
        async with asyncio.timeout(min(20, self.budget.deadline - time.monotonic())):
            response = await super()._request(method, path, **kwargs)
        if logout and response.status_code in (200, 202, 204):
            self.session["logoff_acknowledged"] = True
        return response

    async def native_read(self, config, command, **kwargs):
        if self.allowed_read is None or shlex.split(command) != self.allowed_read:
            raise PermissionError("ST69 native read outside bound target")
        return await self.native_call(config, command)

    async def native_write(self, config, command, **kwargs):
        if (
            self.allowed_write is None
            or self.write_sent
            or shlex.split(command) != self.allowed_write
        ):
            raise PermissionError("ST69 unarmed/repeated/different native write")
        self.budget.admit_effect()
        self.write_sent = True
        return await self.native_call(config, command)

    async def native_call(self, config, command):
        if (
            files.connection(config) != files.connection(self.config)
            or len(command.encode()) > REQUEST_CAP
        ):
            raise ValueError("ST69 native identity/command bound differs")
        self.budget.reserve(1, 1, REST_CAP + SSH_CAP + len(command.encode()), 40)
        self.budget.dispatch(1, SSH_CAP + len(command.encode()))
        async with asyncio.timeout(
            min(config.ssh_timeout, self.budget.deadline - time.monotonic() - 20)
        ):
            return await self.native(config, command, max_output_bytes=SSH_CAP)


async def snapshot(hmc, packet, *, admitted=True):
    consoles = await hmc.list_uom("ManagementConsole")
    if not isinstance(consoles, list) or len(consoles) != 1:
        raise ValueError("ST69 unique console identity required")
    console = consoles[0]
    if (
        not isinstance(console, dict)
        or str(console.get("UUID", "")).casefold() != packet["hmc_uuid"]
        or not isinstance(console.get("Resource"), dict)
    ):
        raise ValueError("ST69 console identity differs")
    version_tuple = console_version(console["Resource"])
    version = "V{}R{}M{}".format(*version_tuple) if version_tuple else None
    if not isinstance(version, str) or not version.strip():
        raise ValueError("ST69 console version unavailable")
    system_name = await adapters._network_update_system(hmc, packet["system_uuid"])
    parent = await adapters._network_update_parent(
        hmc, packet["system_uuid"], packet["lpar_uuid"]
    )
    entries = deepcopy(await hmc._network_adapter_creation_feed(packet["lpar_uuid"]))
    inventory = adapters._network_update_inventory(entries)
    if admitted and (len(inventory) > 8 or packet["adapter_uuid"] not in inventory):
        raise ValueError("ST69 bounded contained adapter inventory required")
    target = inventory.get(packet["adapter_uuid"])
    slot = (
        adapters._network_integer(target.get("VirtualSlotNumber"), 0, 65535)
        if target is not None
        else None
    )
    return {
        "hmc_uuid": packet["hmc_uuid"],
        "hmc_version": version,
        "system_name": system_name,
        "partition_name": parent[0],
        "partition_id": parent[1],
        "state": "running",
        "sync": asdict(parent[2]),
        "slot": slot,
        "adapters": entries,
    }


def configured(snapshot_value, packet):
    inventory = adapters._network_update_inventory(snapshot_value["adapters"])
    return adapters._network_update_expectation(
        inventory[packet["adapter_uuid"]], CASES[packet["case"]]
    )


def same_snapshot(before, after):
    return {k: v for k, v in before.items() if k != "adapters"} == {
        k: v for k, v in after.items() if k != "adapters"
    } and adapters._network_update_inventory_projection(
        before["adapters"]
    ) == adapters._network_update_inventory_projection(after["adapters"])


def binding(packet, config, baseline):
    return {
        "candidate": packet["candidate"],
        "hmc_uuid": packet["hmc_uuid"],
        "hmc_version": baseline["hmc_version"],
        "system_uuid": packet["system_uuid"],
        "lpar_uuid": packet["lpar_uuid"],
        "adapter_uuid": packet["adapter_uuid"],
        "system_name": baseline["system_name"],
        "partition_id": baseline["partition_id"],
        "slot": baseline["slot"],
        "connection": files.connection(config),
    }


def new_document(packet, digest, phase, budget):
    return {
        "schema_version": 1,
        "group": GROUP,
        "packet_sha256": digest,
        "binding": None,
        "phase": phase,
        "baseline": None,
        "intent": None,
        "submission": None,
        "observations": dict.fromkeys(("prewrite", "postwrite", "recovery")),
        "result": None,
        "budget": budget.record(),
        "session": dict.fromkeys(SESSION_KEYS, False),
        "errors": [],
        "complete": False,
        "preflight_sha256": None,
        "original_results_sha256": None,
    }


def closed(document):
    return document["session"] == dict.fromkeys(SESSION_KEYS, True)


def validate_document(document, phase=None):
    exact(document, JOURNAL_KEYS)
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or document["group"] != GROUP
        or document["phase"] not in LIMITS
    ):
        raise ValueError("ST69 journal version/group/phase differs")
    if phase is not None and document["phase"] != phase:
        raise ValueError("ST69 wrong predecessor phase")
    sha(document["packet_sha256"])
    exact(document["session"], SESSION_KEYS)
    if (
        any(type(value) is not bool for value in document["session"].values())
        or type(document["complete"]) is not bool
    ):
        raise ValueError("ST69 explicit session/completion proof required")
    exact(document["observations"], ("prewrite", "postwrite", "recovery"))
    if (
        document["result"] not in OUTCOMES | {None}
        or not isinstance(document["errors"], list)
        or any(
            not isinstance(error, str) or len(error) > 256
            for error in document["errors"]
        )
    ):
        raise ValueError("ST69 invalid bounded result/errors")
    exact(document["budget"], ("maximum", "used", "deadline", "expires_utc"))
    budget = document["budget"]
    if (
        budget["maximum"] != list(LIMITS[document["phase"]])
        or not isinstance(budget["used"], list)
        or len(budget["used"]) != 3
        or any(
            type(u) is not int or not 0 <= u <= cap
            for u, cap in zip(budget["used"], budget["maximum"], strict=False)
        )
        or type(budget["deadline"]) not in (float, int)
        or not math.isfinite(budget["deadline"])
    ):
        raise ValueError("ST69 invalid phase budget proof")
    expiry(budget["expires_utc"])
    if document["complete"] and (
        not closed(document)
        or document["errors"]
        or document["binding"] is None
        or document["baseline"] is None
    ):
        raise ValueError("ST69 contradictory complete record")
    validate_history(document)
    return document


def validate_history(document):
    bound = document["binding"]
    if bound is None:
        if document["complete"]:
            raise ValueError("ST69 complete binding required")
        return
    exact(
        bound,
        (
            "candidate",
            "hmc_uuid",
            "hmc_version",
            "system_uuid",
            "lpar_uuid",
            "adapter_uuid",
            "system_name",
            "partition_id",
            "slot",
            "connection",
        ),
    )
    exact(bound["candidate"], ("commit", "tree"))
    for value in bound["candidate"].values():
        sha(value, 40)
    for key in ("hmc_uuid", "system_uuid", "lpar_uuid", "adapter_uuid"):
        if not isinstance(bound[key], str) or not is_uuid(bound[key]):
            raise ValueError("ST69 retained UUID binding invalid")
    if (
        not isinstance(bound["hmc_version"], str)
        or re.fullmatch(r"V[0-9]+R[0-9]+M[0-9]+", bound["hmc_version"]) is None
    ):
        raise ValueError("ST69 retained console version invalid")
    exact(bound["connection"], ("host", "port", "user", "verify_ssl", "agent_id"))
    identity = bound["connection"]
    if (
        type(identity["port"]) is not int
        or not 1 <= identity["port"] <= 65535
        or type(identity["verify_ssl"]) is not bool
        or any(
            not isinstance(identity[key], str) or not identity[key].strip()
            for key in ("host", "user", "agent_id")
        )
    ):
        raise ValueError("ST69 retained connection invalid")
    snapshots = {"baseline": document["baseline"], **document["observations"]}
    for label, snapshot_value in snapshots.items():
        if snapshot_value is not None:
            validate_snapshot(
                snapshot_value, bound, admitted=label in ("baseline", "prewrite")
            )
    intent = document["intent"]
    if intent is not None:
        exact(
            intent,
            ("system_uuid", "lpar_uuid", "adapter_uuid", "slot", "case", "attributes"),
        )
        if intent["case"] not in CASES or any(
            intent[key] != bound[key]
            for key in ("system_uuid", "lpar_uuid", "adapter_uuid", "slot")
        ):
            raise ValueError("ST69 retained intent target differs")
        packet = original_packet(document)
        _, attributes = configured(document["baseline"], packet)
        if intent["attributes"] != [list(pair) for pair in attributes]:
            raise ValueError("ST69 retained intent is not fixed-case attributes")
    if document["phase"] == "preflight":
        if any(
            document[key] is not None
            for key in (
                "intent",
                "submission",
                "preflight_sha256",
                "original_results_sha256",
                "result",
            )
        ) or any(value is not None for value in document["observations"].values()):
            raise ValueError("ST69 preflight cannot contain run/recovery claims")
    else:
        sha(document["preflight_sha256"])
        if (
            document["phase"] == "run"
            and document["original_results_sha256"] is not None
        ):
            raise ValueError("ST69 run cannot claim recovery lineage")
        if document["phase"] == "recover":
            sha(document["original_results_sha256"])
        if document["complete"] and (
            intent is None
            or document["submission"] is None
            or document["observations"]["prewrite"] is None
        ):
            raise ValueError("ST69 complete run history required")
        if (
            document["phase"] == "run"
            and document["complete"]
            and document["observations"]["postwrite"] is None
        ):
            raise ValueError("ST69 complete postwrite observation required")
    if document["submission"] is not None:
        if intent is None:
            raise ValueError("ST69 submission requires original fixed-case intent")
        expected, attributes = configured(
            document["baseline"], original_packet(document)
        )
        validate_submission(
            document["submission"],
            bound,
            expected,
            no_write_allowed=intent["case"] == "vlan-empty" and not attributes,
        )
    if document["result"] in ("retained_verified_change", "no_write_verified"):
        observed = document["observations"][
            "recovery" if document["phase"] == "recover" else "postwrite"
        ]
        if (
            not document["complete"]
            or observed is None
            or outcome(document, original_packet(document), observed)
            != document["result"]
        ):
            raise ValueError("ST69 qualifying result contradicts observations/history")


def validate_snapshot(value, bound, *, admitted):
    exact(
        value,
        (
            "hmc_uuid",
            "hmc_version",
            "system_name",
            "partition_name",
            "partition_id",
            "state",
            "sync",
            "slot",
            "adapters",
        ),
    )
    if (
        value["hmc_uuid"] != bound["hmc_uuid"]
        or not isinstance(value["hmc_version"], str)
        or re.fullmatch(r"V[0-9]+R[0-9]+M[0-9]+", value["hmc_version"]) is None
        or value["state"] != "running"
    ):
        raise ValueError("ST69 snapshot console/state identity differs")
    if (
        any(
            not isinstance(value[key], str)
            or not value[key].strip()
            or any(not c.isprintable() for c in value[key])
            for key in ("system_name", "partition_name")
        )
        or type(value["partition_id"]) is not int
        or value["partition_id"] <= 0
    ):
        raise ValueError("ST69 snapshot native identities invalid")
    exact(value["sync"], ("current_profile_sync", "lives_in", "profile_name"))
    if value["sync"]["lives_in"] != "unknown":
        raise ValueError("ST69 cannot claim saved-profile propagation")
    inventory = adapters._network_update_inventory(value["adapters"])
    target = inventory.get(bound["adapter_uuid"])
    if admitted and (len(inventory) > 8 or target is None):
        raise ValueError("ST69 admitted adapter inventory invalid")
    if target is None:
        if value["slot"] is not None:
            raise ValueError("ST69 absent observed target requires null slot")
    elif (
        type(value["slot"]) is not int
        or adapters._network_integer(target.get("VirtualSlotNumber"), 0, 65535)
        != value["slot"]
    ):
        raise ValueError("ST69 observed adapter slot invalid")
    # Admission stays exact; later observations must retain readable drift as evidence.
    if admitted and any(
        value[key] != bound[key]
        for key in ("hmc_version", "system_name", "partition_id", "slot")
    ):
        raise ValueError("ST69 snapshot native tuple differs")


def validate_submission(value, bound, expected, *, no_write_allowed):
    exact(
        value,
        (
            "system_uuid",
            "lpar_uuid",
            "adapter_uuid",
            "slot_number",
            "mutation_dispatched",
            "submission",
            "reason",
            "refusal_code",
            "verification",
            "preservation",
            "change_location",
            "errors",
        ),
    )
    if (
        any(
            value[key] != bound[key]
            for key in ("system_uuid", "lpar_uuid", "adapter_uuid")
        )
        or value["slot_number"] != bound["slot"]
        or type(value["mutation_dispatched"]) is not bool
    ):
        raise ValueError("ST69 submission identity differs")
    if value["submission"] not in (
        "completed",
        "rejected",
        "unknown",
        "not_dispatched",
    ) or value["reason"] not in (None, "already_satisfied"):
        raise ValueError("ST69 submission state invalid")
    no_write = value["submission"] == "not_dispatched"
    if no_write and not no_write_allowed:
        raise ValueError("ST69 no-write requires already-empty VLAN-only intent")
    if no_write != (value["mutation_dispatched"] is False) or no_write != (
        value["reason"] == "already_satisfied"
    ):
        raise ValueError("ST69 contradictory submission state")
    exact(
        value["verification"],
        ("status", "expected", "observed", "mismatched_fields", "unavailable_fields"),
    )
    exact(value["preservation"], ("status", "scope", "changed_adapter_uuids"))
    if (
        value["verification"]["status"] not in ("verified", "mismatched", "unavailable")
        or value["preservation"]["status"]
        not in ("unchanged", "drifted", "unavailable")
        or not isinstance(value["errors"], list)
    ):
        raise ValueError("ST69 submission observation enums invalid")
    witness = value["verification"]
    exact(witness["expected"], expected)
    if witness["expected"] != expected or any(
        type(witness["expected"][key]) is not type(wanted)
        for key, wanted in expected.items()
    ):
        raise ValueError("ST69 submission expected fields differ from fixed case")
    if any(
        isinstance(item, list) and any(type(member) is not int for member in item)
        for item in witness["expected"].values()
    ):
        raise ValueError("ST69 expected VLAN members must be integers")
    observed = witness["observed"]
    if not isinstance(observed, dict) or not set(observed) <= set(expected):
        raise ValueError("ST69 observed fields differ from selected closure")
    for name, observed_value in observed.items():
        validate_observed_value(name, observed_value)
    missing = [
        name
        for name in expected
        if observed.get(name) is None or name == "remove_vsi_profile"
    ]
    mismatched = [
        name
        for name in expected
        if name not in missing and observed[name] != expected[name]
    ]
    status = "mismatched" if mismatched else "unavailable" if missing else "verified"
    if (
        witness["status"] != status
        or witness["mismatched_fields"] != mismatched
        or witness["unavailable_fields"] != missing
    ):
        raise ValueError("ST69 verification status contradicts its witnesses")
    preservation = value["preservation"]
    changes = preservation["changed_adapter_uuids"]
    if (
        preservation["scope"]
        != "parsed_adapter_resources_except_metadata_and_selected_fields"
        or not isinstance(changes, list)
        or any(
            not isinstance(identity, str) or not is_uuid(identity)
            for identity in changes
        )
        or len({identity.casefold() for identity in changes}) != len(changes)
    ):
        raise ValueError("ST69 preservation witness invalid")
    if (preservation["status"] == "drifted") != bool(changes):
        raise ValueError("ST69 preservation status contradicts changed identities")
    if any(not isinstance(error, str) or len(error) > 256 for error in value["errors"]):
        raise ValueError("ST69 bounded local submission errors required")


def validate_observed_value(name, value):
    if value is None:
        return
    if name == "tagged":
        valid = type(value) is bool
    elif name == "addl_vlan_ids":
        valid = (
            isinstance(value, list)
            and len(value) <= 4094
            and all(type(member) is int and 1 <= member <= 4094 for member in value)
            and len(set(value)) == len(value)
        )
    elif name == "qos_priority":
        valid = value == "none" or (type(value) is int and 0 <= value <= 7)
    elif name == "remove_vsi_profile":
        exact(value, ("vsi_manager_id", "vsi_type_id", "vsi_type_version"))
        valid = all(
            member is None or (type(member) is int and -(2**31) <= member < 2**31)
            for member in value.values()
        )
    else:
        valid = type(value) is int and -(2**31) <= value < 2**31
    if not valid:
        raise ValueError("ST69 observed field type/range invalid")


def original_packet(document):
    bound, intent = document["binding"], document["intent"]
    if not isinstance(bound, dict) or not isinstance(intent, dict):
        raise TypeError("ST69 original armed binding unavailable")
    return {
        "schema_version": 1,
        "candidate": bound["candidate"],
        **{
            key: bound[key]
            for key in ("hmc_uuid", "system_uuid", "lpar_uuid", "adapter_uuid")
        },
        "case": intent["case"],
        "expires_utc": document["budget"]["expires_utc"],
        "retained_change_authorized": True,
    }


def outcome(document, packet, current):
    if document["intent"] is None or document["baseline"] is None or current is None:
        return "unconfirmed"
    expected, _ = configured(document["baseline"], packet)
    verification, preservation = adapters._network_update_observation(
        document["baseline"]["adapters"],
        current["adapters"],
        packet["adapter_uuid"],
        expected,
    )
    protected = (
        "hmc_uuid",
        "hmc_version",
        "system_name",
        "partition_name",
        "partition_id",
        "state",
        "sync",
        "slot",
    )
    if (
        any(current[key] != document["baseline"][key] for key in protected)
        or preservation.status == "drifted"
        or verification.status == "mismatched"
    ):
        return "observed_drift"
    submitted = document["submission"]
    if submitted is not None and (
        submitted["preservation"]["status"] == "drifted"
        or submitted["verification"]["status"] == "mismatched"
    ):
        return "observed_drift"
    if submitted is None or not document["complete"] or not closed(document):
        return "unconfirmed"
    if (
        submitted["errors"]
        or submitted["verification"]["status"] == "unavailable"
        or submitted["preservation"]["status"] == "unavailable"
    ):
        return "unconfirmed"
    if verification.status != "verified" or preservation.status != "unchanged":
        return "unconfirmed"
    if (
        submitted["submission"] == "completed"
        and submitted["mutation_dispatched"] is True
    ):
        return "retained_verified_change"
    if (
        submitted["submission"] == "not_dispatched"
        and submitted["mutation_dispatched"] is False
        and submitted["reason"] == "already_satisfied"
    ):
        return "no_write_verified"
    return "unconfirmed"


async def phase(packet, digest, mode, output_path, action):
    # All local admission and fresh output checks precede credentials/session construction.
    checked_path(output_path, fresh=True)
    files.revision(packet["candidate"])
    config = load_profile(PROFILE).model_copy(update={"max_response_bytes": REST_CAP})
    budget = Budget(mode, packet["expires_utc"])
    document = new_document(packet, digest, mode, budget)
    checkpoint(output_path, document, fresh=True)
    hmc = CountedHMC(config, budget)
    try:
        await hmc.logon()
        hmc.session["logon_acknowledged"] = True
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(ethernet, "run_hmc_command", hmc.native_write)
            )
            stack.enter_context(
                patch.object(profiles, "run_hmc_command", hmc.native_read)
            )
            await action(hmc, document)
    except (
        ValueError,
        TypeError,
        KeyError,
        HMCError,
        PermissionError,
        OSError,
        TimeoutError,
    ):
        document["errors"].append(
            "ST69 phase stopped; inspect retained intent and run read-only recovery"
        )
        document["result"] = "unconfirmed"
    finally:
        hmc.allowed_write = None
        try:
            await hmc.logoff()
        except (HMCError, OSError, TimeoutError, ValueError):
            document["errors"].append("ST69 own logoff not acknowledged")
        try:
            await hmc._http.aclose()
            hmc.session["http_closed"] = hmc._http.is_closed
        finally:
            document["session"] = hmc.session.copy()
            document["budget"] = budget.record()
            if not closed(document):
                document["errors"].append("ST69 session closeout unconfirmed")
            history_complete = mode == "preflight" or (
                document["intent"] is not None
                and document["submission"] is not None
                and document["observations"]["prewrite"] is not None
                and document["observations"][
                    "postwrite" if mode == "run" else "recovery"
                ]
                is not None
            )
            document["complete"] = (
                not document["errors"]
                and document["baseline"] is not None
                and history_complete
            )
            if document["complete"] and mode == "run":
                document["result"] = outcome(
                    document, packet, document["observations"]["postwrite"]
                )
            checkpoint(output_path, document)
    return document


PROFILE = None


def read_command(snapshot_value):
    return [
        "lssyscfg",
        "-r",
        "lpar",
        "-m",
        snapshot_value["system_name"],
        "--filter",
        build_filter([("lpar_names", snapshot_value["partition_name"])]),
        "-F",
        "description",
    ]


async def preflight(packet, digest, output_path):
    packet = validate_packet(packet)
    sha(digest)

    async def action(hmc, document):
        baseline = await snapshot(hmc, packet)
        configured(baseline, packet)
        hmc.allowed_read = read_command(baseline)
        await authorize_lpar_mutation(
            hmc, baseline["system_name"], baseline["partition_name"]
        )
        document["binding"] = binding(packet, hmc.config, baseline)
        document["baseline"] = baseline

    return await phase(packet, digest, "preflight", output_path, action)


async def execute(packet, digest, preflight_path, output_path, grant, exclusive):
    packet = validate_packet(packet)
    if grant != sha(digest) or exclusive is not True:
        raise ValueError(
            "ST69 separate current packet grant/writer acknowledgement required"
        )
    predecessor, preflight_hash = read_record(preflight_path)
    validate_document(predecessor, "preflight")
    if not predecessor["complete"] or predecessor["packet_sha256"] != digest:
        raise ValueError("ST69 complete matching preflight required")

    async def action(hmc, document):
        document["preflight_sha256"] = preflight_hash
        baseline = await snapshot(hmc, packet)
        bound = binding(packet, hmc.config, baseline)
        if bound != predecessor["binding"] or not same_snapshot(
            predecessor["baseline"], baseline
        ):
            raise ValueError("ST69 preflight binding/baseline drift")
        _expected, attributes = configured(baseline, packet)
        document["binding"], document["baseline"] = bound, baseline
        document["observations"]["prewrite"] = baseline
        hmc.allowed_read = read_command(baseline)
        record = (
            build_attribute_record(
                attributes,
                quoted=[
                    name for name, _ in attributes if name.startswith("addl_vlan_ids")
                ],
                surface="ST69 fixed record",
            )
            if attributes
            else None
        )
        document["intent"] = {
            "system_uuid": packet["system_uuid"],
            "lpar_uuid": packet["lpar_uuid"],
            "adapter_uuid": packet["adapter_uuid"],
            "slot": baseline["slot"],
            "case": packet["case"],
            "attributes": [list(pair) for pair in attributes],
        }
        checkpoint(output_path, document)
        hmc.budget.admit_effect()
        # Revalidate current authority after potentially slow checkpoint I/O, before arming.
        validate_packet(packet)
        hmc.allowed_write = (
            [
                "chhwres",
                "-r",
                "virtualio",
                "--rsubtype",
                "eth",
                "-o",
                "s",
                "-m",
                baseline["system_name"],
                "--id",
                str(baseline["partition_id"]),
                "-s",
                str(baseline["slot"]),
                "-a",
                record,
            ]
            if attributes
            else None
        )
        result = await adapters.update_network_adapter(
            hmc,
            packet["lpar_uuid"],
            packet["adapter_uuid"],
            system_name_or_uuid=packet["system_uuid"],
            exclusive_writer_window=True,
            **CASES[packet["case"]],
        )
        hmc.allowed_write = None
        document["submission"] = asdict(result)
        checkpoint(output_path, document)
        document["observations"]["postwrite"] = await snapshot(
            hmc, packet, admitted=False
        )

    return await phase(packet, digest, "run", output_path, action)


async def recover(run_path, output_path):
    original, original_hash = read_record(run_path)
    validate_document(original, "run")
    packet = original_packet(original)
    # Expiry limits effects, not a later read-only investigation of retained intent.
    exact(packet["candidate"], ("commit", "tree"))

    async def action(hmc, document):
        document.update(
            binding=deepcopy(original["binding"]),
            baseline=deepcopy(original["baseline"]),
            intent=deepcopy(original["intent"]),
            submission=deepcopy(original["submission"]),
            preflight_sha256=original["preflight_sha256"],
            original_results_sha256=original_hash,
        )
        if files.connection(hmc.config) != original["binding"]["connection"]:
            raise ValueError("ST69 recovery connection differs")
        current = await snapshot(hmc, packet, admitted=False)
        document["observations"] = deepcopy(original["observations"])
        document["observations"]["recovery"] = current
        document["result"] = outcome(original, packet, current)

    return await phase(
        packet, original["packet_sha256"], "recover", output_path, action
    )


def supervise(arguments, seconds):
    """Own one process group from spawn through reap; interrupted workers prove no closeout."""
    process = None
    previous = signal.getsignal(signal.SIGTERM)
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})

    def stop(_signum, _frame):
        raise InterruptedError("ST69 supervisor interrupted")

    signal.signal(signal.SIGTERM, stop)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from live_test.ethernet_update import worker; import sys; raise SystemExit(worker(sys.argv[1:]))",
                *arguments,
            ],
            cwd=files.ROOT,
            env={**os.environ, "PYTHONPATH": str(files.ROOT / "scripts")},
            start_new_session=True,
        )
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        return process.wait(timeout=max(0.001, seconds - (time.monotonic() - started)))
    except (subprocess.TimeoutExpired, InterruptedError, KeyboardInterrupt):
        return 2
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        try:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        finally:
            signal.signal(signal.SIGTERM, previous)
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    subs = root.add_subparsers(dest="phase", required=True)
    for mode in LIMITS:
        command = subs.add_parser(mode)
        if mode == "recover":
            command.add_argument("--results", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--packet", type=Path, required=True)
            command.add_argument("--packet-sha256", required=True)
            command.add_argument(
                "--output" if mode == "preflight" else "--out",
                dest="output",
                type=Path,
                required=True,
            )
        command.add_argument("--profile")
        if mode == "run":
            command.add_argument("--preflight", type=Path, required=True)
            command.add_argument("--grant-packet-sha256", required=True)
            command.add_argument(
                "--exclusive-writer-window", action="store_true", required=True
            )
    return root


def worker(argv):
    global PROFILE
    args = parser().parse_args(argv)
    PROFILE = args.profile
    try:
        if args.phase == "recover":
            document = asyncio.run(recover(args.results, args.output))
        else:
            packet = load_packet(args.packet, args.packet_sha256)
            if args.phase == "preflight":
                document = asyncio.run(
                    preflight(packet, args.packet_sha256, args.output)
                )
            else:
                document = asyncio.run(
                    execute(
                        packet,
                        args.packet_sha256,
                        args.preflight,
                        args.output,
                        args.grant_packet_sha256,
                        args.exclusive_writer_window,
                    )
                )
        return (
            0
            if document["complete"]
            and (
                args.phase == "preflight"
                or document["result"]
                in ("retained_verified_change", "no_write_verified")
            )
            else 2
        )
    except (ValueError, TypeError, KeyError, OSError, HMCError):
        print(
            "ST69 refused or unconfirmed; inspect private evidence and use read-only recovery",
            file=sys.stderr,
        )
        return 2


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(arguments)
    return supervise(arguments, LIMITS[args.phase][3])
