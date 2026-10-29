"""ST68: one finite boot/start case; retained evidence never restores effect authority."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import json
import shlex
import sys
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from hmcpctl.client.core import HMCClient
from hmcpctl.config import load_profile
from hmcpctl.documents.boot import select_boot_string, validate_raw_boot_string
from hmcpctl.documents.lpar_start import LparStartSettings
from hmcpctl.operations.lpar import boot_order, core, linux_roles, ownership
from hmcpctl.operations.vios import boot_order as vios_boot
from hmcpctl.operations.vios import core as vios_core
from hmcpctl.operations.vios.boot_order import _encode_boot_record
from hmcpctl.ssh import lpar as native
from hmcpctl.ssh import profiles
from hmcpctl.ssh.transport import run_hmc_command
from hmcpctl.xmlutil import leaf_text

from . import lpar_serial_adapter as serial
from .vios_ame_source import _json

GROUP, SUBTASK = "lpar-boot-start", 68
MIB, REST_CAP, SSH_CAP = 1024**2, 262144, 65536
REQUEST_CAP = 65536
LIMITS = {
    "preflight": (32, 4, 4 * MIB),
    "run": (64, 8, 8 * MIB),
    "recover": (32, 4, 4 * MIB),
}
CASES = (
    "create-omitted",
    "create-auto-0",
    "create-auto-1",
    "create-monitor-0",
    "create-monitor-1",
    *("create-mode-" + s for s in ("norm", "dd", "ds", "of", "sms")),
    "create-power-ids",
    "create-power-names",
    "boot-raw",
    "boot-devices",
)
RAW = "  /dev/test,a=b\"c'\\d  "
DEVICES = ["/dev/test-a", "/dev/test-b"]


def validate_packet(p):
    serial._exact(
        p,
        (
            *(
                "candidate",
                "expires_utc",
                "system_uuid",
                "partition_type",
                "name",
                "caller_token",
                "cleanup_authorized",
            ),
            "case",
            "controllers",
            "boot_target",
        ),
    )
    subset = {
        k: v for k, v in p.items() if k not in ("case", "controllers", "boot_target")
    }
    if not isinstance(p["name"], str) or not isinstance(p["caller_token"], str):
        raise TypeError("ST68 fixture name/token required")
    subset["name"] = p["name"].replace("hmcpctl-live-boot-", "hmcpctl-live-serial-", 1)
    subset["caller_token"] = p["caller_token"].replace("q1770-", "q1772-", 1)
    serial.validate_packet(subset)
    if str(uuid.UUID(p["system_uuid"])) != p["system_uuid"] or p["case"] not in CASES:
        raise ValueError("ST68 canonical system and finite case required")
    if not p["name"].startswith("hmcpctl-live-boot-") or not p[
        "caller_token"
    ].startswith("q1770-"):
        raise ValueError("ST68 reserved fixture name/token required")
    controllers = p["controllers"]
    power = p["case"].startswith("create-power-")
    if not isinstance(controllers, list) or len(controllers) not in (
        (1, 2) if power else (0,)
    ):
        raise ValueError("ST68 fixed controller count required")
    for c in controllers:
        identity(c)
    for field in ("uuid", "name", "partition_id"):
        if len({c[field] for c in controllers}) != len(controllers):
            raise ValueError("ST68 duplicate controller")
    if p["case"].startswith("boot-"):
        target = p["boot_target"]
        serial._exact(target, ("kind", "uuid", "name", "partition_id", "description"))
        identity({k: v for k, v in target.items() if k != "description"})
        if not isinstance(target["description"], str) or p["partition_type"] == "OS400":
            raise ValueError("ST68 restoring boot source/ownership unavailable")
    elif p["boot_target"] is not None:
        raise ValueError("ST68 creation cannot select a boot target")
    settings(p).validate_creation(p["partition_type"])


def identity(c):
    serial._exact(c, ("kind", "uuid", "name", "partition_id"))
    if (
        c["kind"] not in ("LogicalPartition", "VirtualIOServer")
        or not isinstance(c["uuid"], str)
        or str(uuid.UUID(c["uuid"])) != c["uuid"]
        or not isinstance(c["name"], str)
        or not c["name"]
        or not c["name"].isprintable()
        or type(c["partition_id"]) is not int
        or c["partition_id"] <= 0
    ):
        raise ValueError("ST68 exact contained identity required")


def settings(p):
    case = p["case"]
    if case.startswith("create-auto-"):
        return LparStartSettings(auto_start=case.endswith("1"))
    if case.startswith("create-monitor-"):
        return LparStartSettings(conn_monitoring=case.endswith("1"))
    if case.startswith("create-mode-"):
        return LparStartSettings(boot_mode=case.removeprefix("create-mode-"))
    if case.startswith("create-power-"):
        if case.endswith("ids"):
            return LparStartSettings(
                power_ctrl_lpar_ids=tuple(c["partition_id"] for c in p["controllers"])
            )
        return LparStartSettings(
            power_ctrl_lpar_names=tuple(c["name"] for c in p["controllers"])
        )
    return LparStartSettings()


def bounded_config(config):
    return config.model_copy(
        update={
            "max_response_bytes": min(config.max_response_bytes, REST_CAP),
            "port": config.port,
        }
    )


def binding(p, config, *, recovery=False):
    validate_packet(p)
    if serial.connection(config) != p["candidate"]["hmc_identity"]:
        raise ValueError("ST68 effective connection differs before authentication")
    serial.revision(p["candidate"])
    if (
        not recovery
        and not 0 < (serial._expiry(p) - datetime.now(UTC)).total_seconds() <= 1200
    ):
        raise ValueError("ST68 original window expired or exceeds1200s")


class Window(serial.Window):
    def check(self, config, invocation):
        if self.grant != self.digest or self.exclusive is not True:
            raise PermissionError(
                "ST68 separate current matching grant/exclusive assertion required"
            )
        if (
            self.packet["cleanup_authorized"] is not True
            or invocation != self.invocation
        ):
            raise PermissionError(
                "ST68 current invocation/conditional cleanup required"
            )
        binding(self.packet, config)


class Budget:
    def __init__(self, mode, p):
        self.maximum = LIMITS[mode]
        self.used = [0, 0, 0]
        self.started = time.monotonic()
        self.deadline = self.started + 300
        if mode == "run":
            self.deadline = min(
                self.deadline,
                self.started + serial._expiry(p).timestamp() - time.time() - 320,
            )
        self.expiry = serial._expiry(p).timestamp()
        self.recovery = mode == "recover"
        self.calls = []

    def reserve(self, rest, ssh, size, seconds, *, effect=False, close=False):
        for label, u, n, cap in zip(
            ("REST count", "SSH count", "byte"),
            self.used,
            (rest, ssh, size),
            self.maximum,
            strict=True,
        ):
            if u + n > cap:
                raise ValueError("ST68 " + label + " reserve exhausted")
        remaining = self.deadline - time.monotonic()
        if not self.recovery and not close:
            remaining = min(
                remaining, self.expiry - time.time() - (320 if effect else 0)
            )
        if remaining < seconds:
            raise ValueError(
                "ST68 original deadline/cleanup/recovery reserve unavailable"
            )

    def dispatch(self, kind, size, label, *, close=False):
        self.reserve(int(kind == 0), int(kind == 1), size, 0.001, close=close)
        self.used[kind] += 1
        self.used[2] += size
        self.calls.append({"kind": "REST" if kind == 0 else "SSH", "label": label})

    def timeout(self, maximum, *, close=False):
        remaining = self.deadline - time.monotonic()
        if not self.recovery and not close:
            remaining = min(remaining, self.expiry - time.time())
        if remaining <= 0:
            raise ValueError("ST68 absolute phase deadline expired")
        return min(maximum, remaining)


class CountedHMC(HMCClient):
    """ST68-local physical boundary; immutable pre-session response configuration."""

    def __init__(self, config, budget):
        if config.max_response_bytes > REST_CAP:
            raise ValueError(
                "ST68 response cap must be selected before session construction"
            )
        super().__init__(config)
        self.budget = budget
        self.window = None
        self.effect = None
        self.target = None
        self.sent = set()
        self.acknowledged = set()
        self.documents = {}
        self.login_sent = self.closed = self.logout_sent = False

    def mutation(self, token):
        if self.window is None or self.effect is None or token in self.sent:
            raise PermissionError("ST68 unarmed or repeated physical mutation")
        self.window.check(self.config, self.window.invocation)
        self.sent.add(token)

    async def _request(self, method, path, **kwargs):
        close = method == "DELETE" and path == "/rest/api/web/Logon"
        if close:
            if self.logout_sent:
                raise PermissionError("ST68 repeated session logoff refused")
            self.logout_sent = True
        if method == "PUT" and path == "/rest/api/web/Logon":
            if self.login_sent:
                raise PermissionError("ST68 second logon refused")
            self.login_sent = True
        elif not close and method != "GET":
            if (
                self.window is not None
                and self.effect == "create"
                and method == "PUT"
                and path
                in (
                    "/rest/api/uom/ManagedSystem/"
                    + self.window.packet["system_uuid"]
                    + "/LogicalPartition",
                    "/rest/api/uom/ManagedSystem/"
                    + self.window.packet["system_uuid"]
                    + "/VirtualIOServer",
                )
            ):
                self.mutation("create")
            elif (
                self.target
                and self.effect == "delete"
                and method == "DELETE"
                and path
                == "/rest/api/uom/" + self.target["kind"] + "/" + self.target["uuid"]
            ):
                self.mutation("delete")
            elif (
                self.target
                and self.target["kind"] == "LogicalPartition"
                and self.effect in ("test", "restore")
                and method == "POST"
                and path
                == "/rest/api/uom/LogicalPartition/"
                + self.target["uuid"]
                + "?group=Advanced"
            ):
                self.mutation(self.effect)
            else:
                raise PermissionError("ST68 REST writer outside exact target")
        content = kwargs.get("content", b"")
        request_size = len(content.encode() if isinstance(content, str) else content)
        if request_size > REQUEST_CAP:
            raise ValueError("ST68 fixed request-body byte admission exceeded")
        allowance = self.config.max_response_bytes + request_size
        if not close:
            self.budget.reserve(2, 0, allowance + self.config.max_response_bytes, 20)
        self.budget.dispatch(
            0, allowance, method + " " + path.split("?")[0], close=close
        )
        maximum = 20 if close or method not in ("GET", "PUT") else 2
        if self.effect == "create" and method == "PUT":
            maximum = 60
        async with asyncio.timeout(self.budget.timeout(maximum, close=close)):
            response = await super()._request(method, path, **kwargs)
        self.budget.used[2] -= self.config.max_response_bytes - len(response.content)
        if (
            not close
            and method not in ("GET",)
            and path != "/rest/api/web/Logon"
            and 200 <= response.status_code < 300
        ):
            self.acknowledged.add(self.effect)
        if method == "GET":
            complete_feed(path, response, self.config)
            self.documents[path.split("?")[0]] = response.text
        if close:
            self.closed = response.status_code in (200, 202, 204)
        return response

    async def ssh(self, config, command, **kwargs):
        if serial.connection(config) != serial.connection(self.config):
            raise ValueError("ST68 SSH connection differs")
        if len(command.encode()) > REQUEST_CAP:
            raise ValueError("ST68 fixed native command byte admission exceeded")
        words = shlex.split(command)
        if words and words[0] in ("mksyscfg", "chsyscfg"):
            if (
                len(words) != 7
                or words[1:4] != ["-r", "lpar", "-m"]
                or words[5] != "-i"
            ):
                raise PermissionError(
                    "ST68 native command is not the fixed record route"
                )
            cells = next(csv.reader([words[6]], strict=True))
            pairs = [cell.partition("=") for cell in cells]
            if any(not separator for _, separator, _ in pairs) or len(
                {k for k, _, _ in pairs}
            ) != len(pairs):
                raise PermissionError("ST68 ambiguous native record")
            values = {k: v for k, _, v in pairs}
            if self.effect in ("test", "restore"):
                if (
                    not self.target
                    or set(values) != {"lpar_id", "boot_string"}
                    or values["lpar_id"] != str(self.target["partition_id"])
                ):
                    raise PermissionError(
                        "ST68 native pending target differs from packet"
                    )
            elif (
                self.window is None or values.get("name") != self.window.packet["name"]
            ):
                raise PermissionError("ST68 native fixture name differs from packet")
            elif words[0] == "chsyscfg" and set(values) != {"name", "description"}:
                raise PermissionError(
                    "ST68 only the existing fixture stamp is allocated"
                )
        if not words or words[0] not in ("lssyscfg", "lscomgmt"):
            if words and words[0] == "mksyscfg" and self.effect == "create":
                self.mutation("create")
            elif (
                words
                and words[0] == "chsyscfg"
                and self.effect in ("create", "stamp", "test", "restore")
            ):
                self.mutation(
                    "stamp" if self.effect in ("create", "stamp") else self.effect
                )
            else:
                raise PermissionError("ST68 SSH writer outside exact effect")
        self.budget.reserve(
            1, 1, SSH_CAP + len(command.encode()) + self.config.max_response_bytes, 20
        )
        self.budget.dispatch(1, SSH_CAP + len(command.encode()), words[0])
        maximum = (
            2
            if words[0] in ("lssyscfg", "lscomgmt")
            else 60
            if words[0] == "mksyscfg"
            else 20
        )
        seconds = self.budget.timeout(maximum)
        limit = min(SSH_CAP, kwargs.pop("max_output_bytes", None) or SSH_CAP)
        async with asyncio.timeout(seconds):
            raw = await run_hmc_command(
                config.model_copy(update={"ssh_timeout": seconds}),
                command,
                max_output_bytes=limit,
                **kwargs,
            )
        if (
            not isinstance(raw, str)
            or len(raw.encode()) > limit
            or "[stderr]" in raw.splitlines()
        ):
            raise ValueError("ST68 exact bounded native response unavailable")
        self.budget.used[2] -= SSH_CAP - len(raw.encode())
        if words[0] not in ("lssyscfg", "lscomgmt"):
            self.acknowledged.add(
                "stamp"
                if self.effect in ("create", "stamp") and words[0] == "chsyscfg"
                else self.effect
            )
        return raw

    @contextlib.contextmanager
    def transports(self):
        # These are the fixed owners used by this one serial standalone process.
        owners: tuple[Any, ...] = (native, profiles, linux_roles, vios_boot)
        originals = [o.run_hmc_command for o in owners]
        try:
            for owner in owners:
                owner.run_hmc_command = self.ssh
            yield
        finally:
            for owner, original in zip(owners, originals, strict=True):
                owner.run_hmc_command = original


async def snapshot(hmc, p, *, created=False):
    """Preserve complete partition/profile resources, including unknown I/O and profile ETags."""
    from decimal import Decimal

    from hmcpctl.client.client_profiles import read_native_profile
    from hmcpctl.documents.partition_profile import _records

    system = await hmc.get_managed_system(p["system_uuid"])
    r = (system or {}).get("Resource", {})
    if (system or {}).get("UUID", "").lower() != p["system_uuid"] or leaf_text(
        r.get("State")
    ) != "operating":
        raise ValueError("ST68 selected operating system identity unavailable")
    mode = None
    if p["case"].startswith("create-"):
        mode = (
            await linux_roles.read_role_capabilities(
                hmc, p["system_uuid"], need_kvm=False, need_management=False
            )
        ).controller_mode
    selected = {
        k: r.get(k)
        for k in (
            "SystemName",
            "State",
            "IsClassicHMCManagement",
            "IsHMCPowerVMManagementController",
            "IsPowerVMManagementController",
            "AssociatedSystemMemoryConfiguration",
            "AssociatedSystemProcessorConfiguration",
            "AssociatedSystemCapabilities",
        )
    }
    if not isinstance(selected["SystemName"], str) or not selected["SystemName"]:
        raise ValueError("ST68 system command identity unavailable")
    rows = serial._feed(
        await hmc.list_child("ManagedSystem", p["system_uuid"], "LogicalPartition"),
        "LogicalPartition",
    )
    rows += serial._feed(
        await hmc.list_child("ManagedSystem", p["system_uuid"], "VirtualIOServer"),
        "VirtualIOServer",
    )
    if len(rows) > (5 if created else 4):
        raise ValueError("ST68 original partition cardinality exceeded")
    for key in ("uuid", "name", "partition_id"):
        if len({x[key] for x in rows}) != len(rows):
            raise ValueError("ST68 contained inventory collision")
    partitions, count = {}, 0
    for target in rows:
        entry = await hmc.get_uom(target["kind"], target["uuid"], group="Advanced")
        if serial._feed([entry], target["kind"]) != [target]:
            raise ValueError("ST68 feed/detail identity drift")
        resource = entry["Resource"]
        if any(
            not isinstance(resource.get(k), dict)
            for k in (
                "PartitionMemoryConfiguration",
                "PartitionProcessorConfiguration",
                "PartitionIOConfiguration",
            )
        ):
            raise ValueError("ST68 complete current resource containers unavailable")
        feed = await hmc.list_child(
            target["kind"], target["uuid"], "LogicalPartitionProfile"
        )
        if not isinstance(feed, list) or count + len(feed) > (9 if created else 8):
            raise ValueError("ST68 saved-profile cardinality exceeded")
        saved = {}
        for e in feed:
            raw_uid = e.get("UUID")
            if not isinstance(raw_uid, str):
                raise TypeError("ST68 profile identity unavailable")
            uid = str(uuid.UUID(raw_uid))
            if uid in saved:
                raise ValueError("ST68 profile identity ambiguous")
            profile = await read_native_profile(
                hmc, target["kind"], target["uuid"], raw_uid, p["system_uuid"]
            )
            if not profile.etag or profile.name in [v["name"] for v in saved.values()]:
                raise ValueError("ST68 full profile ETag/name unavailable")
            saved[uid] = {
                "name": profile.name,
                "etag": profile.etag,
                "resource": profile.resource,
                "semantic": _records(profile.element, False),
            }
        count += len(saved)
        partitions[target["uuid"]] = {
            "identity": {
                k: target[k] for k in ("kind", "uuid", "name", "partition_id")
            },
            "resource": resource,
            "semantic": current_records(hmc, target),
            "profiles": saved,
        }
    memory = r.get("AssociatedSystemMemoryConfiguration", {})
    processor = r.get("AssociatedSystemProcessorConfiguration", {})
    capacity = [
        Decimal(str(leaf_text(memory.get("CurrentAvailableSystemMemory")))),
        Decimal(str(leaf_text(processor.get("CurrentAvailableSystemProcessorUnits")))),
    ]
    if any(not v.is_finite() or v < 0 for v in capacity):
        raise ValueError("ST68 finite capacity unavailable")
    return {
        "system": selected,
        "controller_mode": mode,
        "partitions": dict(sorted(partitions.items())),
        "profiles": count,
    }


def foreign(s, uid):
    result = json.loads(json.dumps(s))
    removed = result["partitions"].pop(uid)
    result["profiles"] -= len(removed["profiles"])
    return result


def saved_resources(entry):
    """Configured tuple comes from the unique saved default; inactive current is observational."""
    from decimal import Decimal

    profiles_ = list(entry["profiles"].values())
    if len(profiles_) != 1 or profiles_[0]["name"] != "default_profile":
        raise ValueError("ST68 unique contained default profile required")
    r = profiles_[0]["resource"]
    memory, cpu = r.get("ProfileMemory", {}), r.get("ProcessorAttributes", {})
    dedicated = cpu.get("DedicatedProcessorConfiguration", {})
    if (
        leaf_text(cpu.get("HasDedicatedProcessors")) != "true"
        or leaf_text(cpu.get("SharingMode")) != "keep idle procs"
    ):
        raise ValueError("ST68 fixed dedicated saved processor mode differs")
    for fields, names, wanted in (
        (memory, ("MinimumMemory", "DesiredMemory", "MaximumMemory"), 1024),
        (dedicated, ("MinimumProcessors", "DesiredProcessors", "MaximumProcessors"), 1),
    ):
        for key in names:
            value = Decimal(str(leaf_text(fields.get(key))))
            if not value.is_finite() or value != wanted:
                raise ValueError("ST68 fixed saved resource tuple differs")
    io = r.get("IOConfigurationInstance")
    if not isinstance(io, dict) or leaf_text(io.get("MaximumVirtualIOSlots")) != "8":
        raise ValueError("ST68 fixed slots or complete I/O evidence unavailable")
    for key, value in io.items():
        if key not in ("Metadata", "MaximumVirtualIOSlots") and value not in (
            {},
            [],
            None,
            "",
            "false",
        ):
            raise ValueError("ST68 unexpected configured I/O; retain fixture")


def fixture(current, p, baseline):
    matches = [
        v for v in current["partitions"].values() if v["identity"]["name"] == p["name"]
    ]
    if len(matches) != 1:
        raise ValueError("ST68 fresh fixture identity unavailable")
    e = matches[0]
    uid = e["identity"]["uuid"]
    r = e["resource"]
    if (
        uid in baseline["partitions"]
        or foreign(current, uid) != baseline
        or e["identity"]["kind"]
        != (
            "VirtualIOServer"
            if p["partition_type"] == "Virtual IO Server"
            else "LogicalPartition"
        )
        or leaf_text(r.get("PartitionState")) != "not activated"
        or leaf_text(r.get("PartitionType")) != p["partition_type"]
        or leaf_text(r.get("DefaultProfileName")) != "default_profile"
    ):
        raise ValueError("ST68 own fresh inactive identity/foreign baseline differs")
    saved_resources(e)
    return e


def route_plan(p, baseline, cap):
    """Conservative fixed physical suffix; boundary tests must substantiate owner allowance."""
    snap = snapshot_calls(p, baseline)
    if p["case"].startswith("create-"):
        vios = p["partition_type"] == "Virtual IO Server"
        # One additional partition/default profile for fresh confirmation/deletion guards.
        owner = 40 + 16 * (len(p["controllers"]) - 1) if p["controllers"] else 24
        rest = (2 if vios else 1) * (snap + 3) + snap + owner + int(vios) + 2
        ssh = 7 if vios else 6
        seconds = 2 * (rest + ssh) + 112
    else:
        rest, ssh = (
            2 * snap + 40,
            6 if p["boot_target"]["kind"] == "VirtualIOServer" else 2,
        )
        seconds = 2 * (rest + ssh) + 54
    return rest, ssh, rest * cap + 2 * ssh * SSH_CAP + 3 * REQUEST_CAP, seconds


def admit(p, baseline, hmc):
    from decimal import Decimal

    for c in p["controllers"]:
        e = baseline["partitions"].get(c["uuid"])
        if not e or e["identity"] != c:
            raise ValueError("ST68 selected controller identity differs")
        description = leaf_text(e["resource"].get("Description"))
        if not isinstance(description, str):
            raise TypeError("ST68 controller ownership unavailable")
        ownership.authorize_lpar_ownership_description(
            hmc,
            baseline["system"]["SystemName"],
            c["name"],
            description,
            operation="lpar-mutation",
        )
    if p["case"].startswith("create-"):
        if any(
            e["identity"]["name"].startswith("hmcpctl-live-")
            for e in baseline["partitions"].values()
        ):
            raise ValueError("ST68 prior fixture residue; no takeover")
        s = baseline["system"]
        if (
            Decimal(
                str(
                    leaf_text(
                        s["AssociatedSystemMemoryConfiguration"].get(
                            "CurrentAvailableSystemMemory"
                        )
                    )
                )
            )
            < 1024
            or Decimal(
                str(
                    leaf_text(
                        s["AssociatedSystemProcessorConfiguration"].get(
                            "CurrentAvailableSystemProcessorUnits"
                        )
                    )
                )
            )
            < 1
        ):
            raise ValueError("ST68 fixed fixture capacity unavailable")
    else:
        target = p["boot_target"]
        e = baseline["partitions"].get(target["uuid"])
        if not e or e["identity"] != {
            k: target[k] for k in ("kind", "uuid", "name", "partition_id")
        }:
            raise ValueError("ST68 contained boot target differs")
        r = e["resource"]
        if (
            leaf_text(r.get("Description")) != target["description"]
            or leaf_text(r.get("PartitionType")) != p["partition_type"]
            or leaf_text(r.get("PartitionState")) != "not activated"
        ):
            raise ValueError("ST68 original owned inactive boot identity differs")
        if ownership.parse_lpar_ownership_owner(target["description"]) != (
            hmc.config.agent_id or "hmcpctl"
        ):
            raise ValueError(
                "ST68 existing boot fixture must already be positively owned"
            )
        ownership.authorize_lpar_ownership_description(
            hmc,
            baseline["system"]["SystemName"],
            target["name"],
            target["description"],
            operation="lpar-mutation",
        )


async def pending(hmc, p):
    target = p["boot_target"]
    if target["kind"] == "VirtualIOServer":
        result = await vios_boot.read_vios_boot_order(
            hmc, target["uuid"], system_name_or_uuid=p["system_uuid"]
        )
    else:
        result = await boot_order.read_lpar_boot_order(
            hmc, target["uuid"], system_name_or_uuid=p["system_uuid"]
        )
    text = result["pending_boot_string"]
    return text


def protected_boot(s, p):
    result = json.loads(json.dumps(s))
    boot = result["partitions"][p["boot_target"]["uuid"]]["resource"].get(
        "BootListInformation"
    )
    if p["boot_target"]["kind"] == "LogicalPartition" and isinstance(boot, dict):
        boot.pop("PendingBootString", None)
        from hmcpctl.documents.partition_profile import N

        key = (
            "/"
            + N
            + "LogicalPartition[1]/"
            + N
            + "BootListInformation[1]/"
            + N
            + "PendingBootString[1]"
        )
        result["partitions"][p["boot_target"]["uuid"]]["semantic"][key]["text"] = (
            "<pending-observation>"
        )
    return result


async def effect(hmc, p, journal, path, label, call):
    hmc.window.check(hmc.config, journal["invocation"])
    journal[label] = {"execution": "unknown"}
    checkpoint(path, journal)
    hmc.effect = label
    try:
        result = await call()
        journal[label]["execution"] = "acknowledged"
        checkpoint(path, journal)
        return result
    except BaseException as error:
        if label in hmc.acknowledged:
            journal[label]["execution"] = "acknowledged"
            try:
                checkpoint(path, journal)
            except BaseException as checkpoint_error:
                error.add_note(
                    "ST68 acknowledgment checkpoint also failed: "
                    + type(checkpoint_error).__name__
                )
                raise error from checkpoint_error
        raise
    finally:
        hmc.effect = None


async def stamp_fixture(hmc, p, journal, path, entry):
    marker = f"[hmcpctl owner:{hmc.config.agent_id or 'hmcpctl'} created:{date.today().isoformat()}] [caller {p['caller_token']}]"  # noqa: DTZ011 — existing operator-local ownership calendar
    r = entry["resource"]
    initial = leaf_text(r.get("Description"))
    if not isinstance(initial, str) or "[hmcpctl" in initial or "[caller" in initial:
        raise ValueError("ST68 initial VIOS description unavailable/foreign")
    stamp = {
        "identity": {"system_uuid": p["system_uuid"], **entry["identity"]},
        "created_date": date.today().isoformat(),  # noqa: DTZ011 — existing operator-local ownership calendar
        "expected_description": marker,
        "returned_description": None,
        "execution": "unknown",
        "verification": "unavailable",
        "observed_description": None,
    }
    journal["vios_stamp"] = stamp
    checkpoint(path, journal)
    hmc.window.check(hmc.config, journal["invocation"])
    hmc.effect = "stamp"
    try:
        returned = await native.stamp_lpar_ownership(
            hmc.config,
            journal["baseline"]["system"]["SystemName"],
            p["name"],
            agent_id=hmc.config.agent_id,
            caller_token=p["caller_token"],
        )
    finally:
        hmc.effect = None
    if not isinstance(returned, str):
        raise ValueError("ST68 VIOS stamp outcome unknown; retain residue")  # noqa: TRY004 — external helper uncertainty, not caller type
    stamp.update(execution="acknowledged", returned_description=returned)
    checkpoint(path, journal)
    fresh = await hmc.get_uom(
        "VirtualIOServer", entry["identity"]["uuid"], group="Advanced"
    )
    rr = (fresh or {}).get("Resource", {})
    observed = leaf_text(rr.get("Description"))
    stamp["observed_description"] = observed if isinstance(observed, str) else None
    checkpoint(path, journal)
    fresh_identity = serial._feed([fresh], "VirtualIOServer")[0]
    if (
        {k: fresh_identity[k] for k in entry["identity"]} != entry["identity"]
        or fresh_identity["state"] != "not activated"
        or fresh_identity["type"] != "Virtual IO Server"
        or not isinstance(observed, str)
    ):
        raise ValueError("ST68 independent marker observation unbound/unavailable")
    before_xml = description_records(entry["semantic"], entry["identity"]["kind"])
    after_xml = description_records(
        current_records(hmc, entry["identity"]), entry["identity"]["kind"]
    )
    if (
        before_xml != after_xml
        or {k: v for k, v in rr.items() if k != "Description"}
        != {k: v for k, v in r.items() if k != "Description"}
        or str((fresh or {}).get("UUID", "")).lower() != entry["identity"]["uuid"]
        or returned != marker
        or observed != marker
    ):
        stamp["verification"] = "mismatch"
        checkpoint(path, journal)
        raise ValueError("ST68 acknowledged stamp mismatch; retain residue")
    stamp["verification"] = "verified"
    checkpoint(path, journal)


async def execute(
    hmc, p, digest, preflight, path, window, packet_path, *, journal=None
):
    window.check(hmc.config, window.invocation)
    if (
        preflight.get("local_binding") != local_binding(p, hmc.config)
        or preflight.get("complete") is not True
        or preflight.get("session", {}).get("closed") is not True
        or preflight.get("session", {}).get("local_closed") is not True
        or preflight.get("packet_sha256") != digest
        or preflight.get("candidate") != p["candidate"]
        or preflight.get("response_cap") != hmc.config.max_response_bytes
        or not 0
        <= (
            datetime.now(UTC) - datetime.fromisoformat(preflight["captured_utc"])
        ).total_seconds()
        < 60
    ):
        raise ValueError("ST68 fresh exact packet/config preflight required")
    baseline = await snapshot(hmc, p)
    admit(p, baseline, hmc)
    if baseline != preflight["baseline"]:
        raise ValueError("ST68 original preflight baseline drift")
    hmc.budget.reserve(
        *route_plan(p, baseline, hmc.config.max_response_bytes), effect=True
    )
    initial = journal is None
    journal = (
        journal
        if journal is not None
        else {
            "group": GROUP,
            "subtask": SUBTASK,
            "mode": "run",
            "case": p["case"],
            "packet_path": str(packet_path),
            "packet_sha256": digest,
            "candidate": p["candidate"],
            "invocation": window.invocation,
            "baseline": baseline,
            "response_cap": hmc.config.max_response_bytes,
            "complete": False,
            "vios_stamp": None,
        }
    )
    journal["baseline"] = baseline
    journal["local_binding"] = local_binding(p, hmc.config)
    hmc.window = window
    checkpoint(path, journal, fresh=initial)
    if p["case"].startswith("create-"):

        async def create():
            if p["partition_type"] == "Virtual IO Server":
                return await vios_core.create_vios(
                    hmc,
                    p["system_uuid"],
                    p["name"],
                    resources=serial.RESOURCES,
                    max_virtual_slots=8,
                    start_settings=settings(p),
                )
            return await core.create_and_stamp_lpar(
                hmc,
                p["system_uuid"],
                core.LparCreation(
                    name=p["name"],
                    partition_type=p["partition_type"],
                    resources=serial.RESOURCES,
                    max_virtual_slots=8,
                    apply_profile=False,
                    caller_token=p["caller_token"],
                    stamp_policy="required",
                    start_settings=settings(p),
                ),
            )

        result = await effect(hmc, p, journal, path, "create", create)
        current = await snapshot(hmc, p, created=True)
        entry = fixture(current, p, baseline)
        journal["configuration"] = verify_start_snapshot(hmc, p, current, entry)
        journal["created_identity"] = entry["identity"]
        checkpoint(path, journal)
        if p["partition_type"] == "Virtual IO Server":
            rest = 2 * snapshot_calls(p, baseline) + 6
            hmc.budget.reserve(
                rest,
                3,
                rest * hmc.config.max_response_bytes + 6 * SSH_CAP,
                2 * (rest + 3) + 54,
                effect=True,
            )
            await stamp_fixture(hmc, p, journal, path, entry)
            current = await snapshot(hmc, p, created=True)
            newer = fixture(current, p, baseline)
            if newer["profiles"] != entry["profiles"] or {
                k: v for k, v in newer["resource"].items() if k != "Description"
            } != {k: v for k, v in entry["resource"].items() if k != "Description"}:
                raise ValueError("ST68 post-stamp protected fixture drift")
            entry = newer
        elif getattr(result, "ownership_stamped", False) is not True:
            raise ValueError("ST68 creation stamp unconfirmed")
        expected = f"[hmcpctl owner:{hmc.config.agent_id or 'hmcpctl'} created:{date.today().isoformat()}] [caller {p['caller_token']}]"  # noqa: DTZ011 — existing operator-local ownership calendar
        if leaf_text(entry["resource"].get("Description")) != expected:
            raise ValueError("ST68 fresh full marker differs")
        hmc.target = entry["identity"]
        rest = snapshot_calls(p, baseline) + 2
        hmc.budget.reserve(
            rest,
            1,
            rest * hmc.config.max_response_bytes + 2 * SSH_CAP,
            2 * (rest + 1) + 36,
            effect=True,
        )
        deleter = (
            hmc.delete_vios
            if entry["identity"]["kind"] == "VirtualIOServer"
            else hmc.delete_logical_partition
        )
        await effect(
            hmc, p, journal, path, "delete", lambda: deleter(entry["identity"]["uuid"])
        )
    else:
        original = await pending(hmc, p)
        validate_raw_boot_string(original)
        await admit_boot_payloads(hmc, p, baseline, original)
        journal["boot_baseline"] = original
        checkpoint(path, journal)
        setter = (
            vios_boot.set_vios_boot_order
            if p["boot_target"]["kind"] == "VirtualIOServer"
            else boot_order.set_lpar_boot_order
        )
        hmc.target = p["boot_target"]

        async def write_text(text=None):
            return await setter(
                hmc,
                hmc.target["uuid"],
                DEVICES if text is None else None,
                system_name_or_uuid=p["system_uuid"],
                **({"boot_string": text} if text is not None else {}),
            )

        text = RAW if p["case"] == "boot-raw" else select_boot_string(DEVICES, None)
        await effect(
            hmc,
            p,
            journal,
            path,
            "test",
            lambda: write_text(RAW if p["case"] == "boot-raw" else None),
        )
        observed = await snapshot(hmc, p)
        if (
            protected_boot(observed, p) != protected_boot(baseline, p)
            or await pending(hmc, p) != text
        ):
            raise ValueError(
                "ST68 known last value/protected baseline differs; no restore"
            )
        rest = 17 + snapshot_calls(p, baseline)
        hmc.budget.reserve(
            rest,
            2,
            rest * hmc.config.max_response_bytes + 4 * SSH_CAP + REQUEST_CAP,
            2 * (rest + 2) + 36,
            effect=True,
        )
        await effect(hmc, p, journal, path, "restore", lambda: write_text(original))
    if await snapshot(hmc, p) != baseline:
        raise ValueError("ST68 full original baseline not restored")
    journal["complete"] = True
    checkpoint(path, journal)
    return journal


def complete_feed(path, response, config):
    """Reuse established Atom envelope guards; ST68 alone supplies its smaller row caps."""
    from defusedxml import ElementTree as ET

    from hmcpctl.documents.partition_profile import ATOM

    from .vios_ame_source import _collection_entry, _collection_link

    parts = path.split("/")
    if parts[-1] not in (
        "LogicalPartition",
        "VirtualIOServer",
        "LogicalPartitionProfile",
    ):
        return
    if (
        any(k in response.headers for k in ("link", "content-range"))
        or response.status_code != 200
    ):
        raise ValueError("ST68 complete collection unavailable")
    root = ET.fromstring(response.content)
    atom = "{" + ATOM + "}"
    if root.tag != atom + "feed" or root.attrib or (root.text or "").strip():
        raise ValueError("ST68 one complete Atom feed required")
    entries = root.findall(atom + "entry")
    if len(entries) > (9 if parts[-1] == "LogicalPartitionProfile" else 5):
        raise ValueError("ST68 physical collection row cap exceeded")
    self_links = 0
    for child in root:
        if (child.tail or "").strip():
            raise ValueError("ST68 collection mixed text")
        if child.tag == atom + "entry":
            _collection_entry(child, parts[-1], config, path)
        elif child.tag == atom + "link":
            relation = child.get("rel", "").upper()
            if relation not in ("SELF", "MANAGEMENT_CONSOLE"):
                raise ValueError("ST68 unknown/paginated collection")
            _collection_link(
                child, config, path, console=relation == "MANAGEMENT_CONSOLE"
            )
            self_links += relation == "SELF"
        elif (
            child.tag not in {atom + k for k in ("id", "title", "updated", "generator")}
            or child.attrib
            or len(child)
        ):
            raise ValueError("ST68 collection completeness extension unavailable")
    if self_links != 1:
        raise ValueError("ST68 unique complete collection SELF required")


async def recover(hmc, p, document, observations=None):
    current = await snapshot(hmc, p, created=p["case"].startswith("create-"))
    boot = await pending(hmc, p) if p["case"].startswith("boot-") else None
    if observations is not None:
        observations.update(current=current, pending_boot_string=boot)
    required = (
        ("create", "delete") if p["case"].startswith("create-") else ("test", "restore")
    )
    if (
        document.get("group") != GROUP
        or document.get("subtask") != SUBTASK
        or document.get("candidate") != p["candidate"]
        or document.get("local_binding") != local_binding(p, hmc.config)
        or document.get("complete") is not True
        or document.get("response_cap") != hmc.config.max_response_bytes
        or any(document.get(k, {}).get("execution") != "acknowledged" for k in required)
        or document.get("session", {}).get("closed") is not True
        or document.get("session", {}).get("local_closed") is not True
    ):
        raise ValueError(
            "ST68 incomplete/unknown original history or session; retain residue"
        )
    transport = document.get("transport", {})
    counts, calls = transport.get("counts"), transport.get("calls")
    if (
        not isinstance(counts, list)
        or len(counts) != 3
        or any(
            type(v) is not int or not 0 <= v <= cap
            for v, cap in zip(counts, LIMITS["run"], strict=True)
        )
        or not isinstance(calls, list)
        or len(calls) != sum(counts[:2])
        or any(
            not isinstance(c, dict)
            or set(c) != {"kind", "label"}
            or c["kind"] not in ("REST", "SSH")
            or not isinstance(c["label"], str)
            for c in calls
        )
        or sum(c["kind"] == "REST" for c in calls) != counts[0]
        or not calls
        or calls[0] != {"kind": "REST", "label": "PUT /rest/api/web/Logon"}
        or calls[-1] != {"kind": "REST", "label": "DELETE /rest/api/web/Logon"}
        or document.get("error") is not None
    ):
        raise ValueError("ST68 incomplete physical/session history; never clean")
    if p["case"].startswith("create-"):
        target = document.get("created_identity")
        identity(target)
        if (
            target["name"] != p["name"]
            or target["uuid"] in document["baseline"]["partitions"]
        ):
            raise ValueError("ST68 fresh creation identity history differs")
    else:
        validate_raw_boot_string(document.get("boot_baseline"))
    if p["case"].startswith("create-") and p["partition_type"] == "Virtual IO Server":
        stamp = document.get("vios_stamp", {})
        if (
            stamp.get("execution") != "acknowledged"
            or stamp.get("verification") != "verified"
            or stamp.get("expected_description") != stamp.get("returned_description")
            or stamp.get("expected_description") != stamp.get("observed_description")
        ):
            raise ValueError("ST68 stamp history cannot qualify cleanup")
    if current != document.get("baseline"):
        raise ValueError("ST68 fresh full original baseline differs")
    if not p["case"].startswith("create-") and await pending(hmc, p) != document.get(
        "boot_baseline"
    ):
        raise ValueError("ST68 exact restored boot baseline unavailable")
    return current


async def phase(hmc, p, document, path, operation):
    """Keep original failures and independently observed owned-session closeout."""
    failure = None
    try:
        with hmc.transports():
            async with hmc:
                async with asyncio.timeout(
                    max(0.001, hmc.budget.deadline - time.monotonic() - 20)
                ):
                    await operation()
    except BaseException as error:  # noqa: BLE001 — preserve cancellation/failure with owned closeout
        failure = error
        document["error"] = type(error).__name__
    finally:
        document["session"] = {
            "opened": hmc.login_sent,
            "closed": hmc.closed,
            "local_closed": hmc._http.is_closed,
        }
        document["transport"] = {"counts": hmc.budget.used, "calls": hmc.budget.calls}
        try:
            checkpoint(path, document)
        except BaseException as checkpoint_error:
            if failure is not None:
                failure.add_note(
                    "ST68 final checkpoint also failed: "
                    + type(checkpoint_error).__name__
                )
                raise failure from checkpoint_error
            raise
    if failure is not None:
        raise failure
    if not hmc.closed or not hmc._http.is_closed:
        raise ValueError("ST68 owned session closure unobserved")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run", "recover"))
    for name in ("packet", "preflight", "output", "results-file", "results"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--packet-sha256")
    parser.add_argument("--grant-packet-sha256")
    parser.add_argument("--exclusive-writer-window", action="store_true")
    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:  # noqa: BLE001 — private boundary
        print(
            f"ST68 HELD ({type(error).__name__}); retain private intent and run read-only recovery.",
            file=sys.stderr,
        )
        return 2


def dispatch(args):
    if args.mode != "run" and (
        args.grant_packet_sha256 or args.exclusive_writer_window
    ):
        raise ValueError("ST68 read-only phase cannot accept effect assertions")
    original, original_sha256 = None, None
    if args.mode == "recover":
        with serial.private_path(args.results, maximum=8 * MIB).open("rb") as stream:
            original_raw = stream.read(8 * MIB + 1)
        if len(original_raw) > 8 * MIB:
            raise ValueError("ST68 original results exceed8MiB before JSON")
        original = _json(original_raw)
        original_sha256 = hashlib.sha256(original_raw).hexdigest()
    packet_path = Path(original["packet_path"]) if original is not None else args.packet
    digest = original["packet_sha256"] if original is not None else args.packet_sha256
    with serial.private_path(packet_path).open("rb") as stream:
        raw = stream.read(serial.LIMIT + 1)
    if len(raw) > serial.LIMIT:
        raise ValueError("ST68 packet exceeds1MiB before JSON")
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("ST68 exact raw packet digest differs")
    p = _json(raw)
    config = load_profile()
    binding(p, config, recovery=args.mode == "recover")
    config = bounded_config(config)
    metadata = local_binding(p, config)
    window = Window(p, digest, args.grant_packet_sha256, args.exclusive_writer_window)
    path = (
        args.results.with_name(args.results.name + ".recovery.json")
        if args.mode == "recover"
        else args.results_file
        if args.mode == "run"
        else args.output
    )
    serial.private_path(path, fresh=True)
    document = {
        "group": GROUP,
        "subtask": SUBTASK,
        "mode": args.mode,
        "case": p["case"],
        "source_only": False,
        "local_binding": metadata,
        "run": {
            "group": GROUP,
            "tested_commit": p["candidate"]["commit"],
            "tree_clean": True,
            "partial": True,
        },
        "candidate": p["candidate"],
        "packet_path": str(packet_path),
        "packet_sha256": digest,
        "response_cap": config.max_response_bytes,
        "complete": False,
        "invocation": window.invocation,
        "vios_stamp": None,
    }
    if args.mode == "run":
        window.check(config, window.invocation)
    checkpoint(path, document, fresh=True)
    hmc = CountedHMC(config, Budget(args.mode, p))

    async def operation():
        if args.mode == "preflight":
            baseline = await snapshot(hmc, p)
            admit(p, baseline, hmc)
            if p["case"].startswith("boot-"):
                document["boot_baseline"] = await pending(hmc, p)
                validate_raw_boot_string(document["boot_baseline"])
            document.update(
                baseline=baseline, captured_utc=datetime.now(UTC).isoformat()
            )
        elif args.mode == "run":
            preflight = serial.read(args.preflight, maximum=4 * MIB)
            await execute(
                hmc, p, digest, preflight, path, window, packet_path, journal=document
            )
        else:
            document["baseline"] = await recover(hmc, p, original, document)
            document["history_verified"] = True
            document["original_results_sha256"] = original_sha256
        document["complete"] = True

    asyncio.run(phase(hmc, p, document, path, operation))
    document["clean"] = args.mode == "recover"
    document["run"]["partial"] = not document["clean"]
    checkpoint(path, document)
    print(
        "ST68 observational phase complete"
        if args.mode != "run"
        else "ST68 bounded run complete; independent recovery required"
    )
    return 0


def verify_start_snapshot(hmc, p, current, entry):
    """Apply the approved finite matrix to the fresh complete snapshot, without new transport."""
    from hmcpctl.operations.lpar.start import (
        StartAdmission,
        StartController,
        _observed_bool,
        _power_set,
    )

    requested = settings(p)
    if not requested.requested():
        return {}
    saved = (
        None
        if current["controller_mode"]
        else next(iter(entry["profiles"].values()))["resource"]
    )
    modes = dict(
        zip(
            ("norm", "dd", "ds", "of", "sms"),
            (
                "Normal",
                "Diagnostic_With_Default_Boot_List",
                "Diagnostic_With_Stored_Boot_List",
                "Open_Firmware",
                "System_Management_Services",
            ),
            strict=True,
        )
    )
    observed = {}
    for attr, saved_key, current_key in (
        ("auto_start", "AutoStart", None),
        (
            "conn_monitoring",
            "ConnectionMonitoringEnabled",
            "IsConnectionMonitoringEnabled",
        ),
        ("boot_mode", "BootMode", "BootMode"),
    ):
        value = getattr(requested, attr)
        if value is None:
            continue
        planes = {"current": entry["resource"][current_key]} if current_key else {}
        if saved is not None:
            planes["saved"] = saved[saved_key]
        if not planes:
            raise ValueError("ST68 requested controller observation unavailable")
        for plane, actual in planes.items():
            equal = (
                leaf_text(actual) == modes[value]
                if attr == "boot_mode"
                else _observed_bool(actual) is value
            )
            if not equal:
                raise ValueError(
                    "ST68 fresh configured value mismatches requested field"
                )
            observed[attr + "." + plane] = actual
    if p["controllers"]:
        admitted = StartAdmission(
            p["system_uuid"],
            current["system"]["SystemName"],
            requested,
            tuple(StartController(**c) for c in p["controllers"]),
        )
        planes = {"current": entry["resource"]["PowerControlPartitions"]}
        if saved is not None:
            planes["saved"] = saved["PowerControlPartitions"]
        for plane, value in planes.items():
            if _power_set(hmc, value, admitted) != set(admitted.controllers):
                raise ValueError("ST68 fresh power-controller set differs")
            observed["power_control." + plane] = value
    return observed


async def admit_boot_payloads(hmc, p, baseline, original):
    """Prove both future bounded payloads before the test effect, using the actual serializer."""
    from copy import deepcopy

    from hmcpctl.client.client_lpars import ET, _edit_logical_partition

    requested = RAW if p["case"] == "boot-raw" else select_boot_string(DEVICES, None)
    if p["boot_target"]["kind"] == "LogicalPartition":
        root, _ = await hmc.read_logical_partition_document(
            p["boot_target"]["uuid"], require_etag=True
        )
        for text in (requested, original):
            clone = deepcopy(root)
            _edit_logical_partition(
                clone,
                {"BootListInformation/PendingBootString": text},
                "ST68 bounded pending text",
            )
            if len(ET.tostring(clone, encoding="unicode").encode()) > REQUEST_CAP:
                raise ValueError("ST68 exact test/restoration request body cannot fit")
    else:
        for text in (requested, original):
            record = _encode_boot_record(p["boot_target"]["partition_id"], text)
            command = f"chsyscfg -r lpar -m {shlex.quote(baseline['system']['SystemName'])} -i {shlex.quote(record)}"
            if len(command.encode()) > REQUEST_CAP:
                raise ValueError(
                    "ST68 exact test/restoration native command cannot fit"
                )


def snapshot_calls(p, baseline):
    return (
        (4 if p["case"].startswith("create-") else 3)
        + 2 * len(baseline["partitions"])
        + baseline["profiles"]
    )


def current_records(hmc, target):
    from defusedxml import ElementTree as ET

    from hmcpctl.documents.partition_profile import ATOM, N, _records

    path = "/rest/api/uom/" + target["kind"] + "/" + target["uuid"]
    root = ET.fromstring(hmc.documents[path])
    nodes = (
        [root]
        if root.tag == N + target["kind"]
        else root.findall("{" + ATOM + "}content/" + N + target["kind"])
    )
    if len(nodes) != 1:
        raise ValueError("ST68 one full correctly-namespaced current resource required")
    return _records(nodes[0], False)


def description_records(records, kind):
    from copy import deepcopy

    from hmcpctl.documents.partition_profile import N

    result = deepcopy(records)
    key = "/" + N + kind + "[1]/" + N + "Description[1]"
    if key not in result:
        raise ValueError("ST68 full description field unavailable")
    result[key]["text"] = "<description-observation>"
    return result


def local_binding(p, config):
    """Retain the existing source binding and selected runtime/trust policy, never credentials."""
    import inspect
    import os
    import platform

    trust = {
        k: getattr(config, k)
        for k in (
            "verify_ssl",
            "ssh_verify_host_key",
            "ca_bundle",
            "cert_fingerprint",
            "trust_system_store",
        )
    }
    trust.update({k: os.environ.get(k) for k in ("SSL_CERT_FILE", "SSL_CERT_DIR")})
    runtime = {
        "python": sys.version,
        "executable": sys.executable,
        "architecture": platform.machine(),
        "import": inspect.getfile(HMCClient),
    }
    values = {"source": p["candidate"], "runtime": runtime, "trust": trust}
    return {
        "metadata": values,
        "sha256": hashlib.sha256(
            json.dumps(values, sort_keys=True).encode()
        ).hexdigest(),
    }


def checkpoint(path, document, *, fresh=False):
    mode = document["mode"]
    if mode not in LIMITS:
        raise ValueError("ST68 fixed evidence phase required")
    serial.write(path, document, fresh=fresh, maximum=LIMITS[mode][2])
