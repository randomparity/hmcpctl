"""One bounded, opt-in experiment on inactive disposable saved profiles.

This is contract evidence, not a product profile API or a recovery-copy admission gate.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import os
import re
import subprocess
from pathlib import Path
from time import monotonic
from typing import Any
from xml.etree import ElementTree as ET

from defusedxml.ElementTree import fromstring

from hmcpctl.client.core import HMCClient
from hmcpctl.operations.systems import profiles as domain
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.transport import run_hmc_command

SUBTASK = 45
GROUP = "partition-profile-contract"
PREFIX = "hmcpctl-live-prof-"
UOM = "{http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/}"
ATOM = "{http://www.w3.org/2005/Atom}"
ANNOTATIONS = {"kb", "kxe", "ksv", "schemaVersion"}
# Finite sparse shape observed on V10R3 SP1060; populated adapters are not admitted here.
NESTED = frozenset(
    [
        "ActiveMemoryExpansionEnabled",
        "ActiveMemorySharingEnabled",
        "AffinityGroupID",
        "AssignAllResources",
        "AutoStart",
        "BootMode",
        "ConnectionMonitoringEnabled",
        "DesiredEntitledMemory",
        "DesiredHugePageCount",
        "DesiredMemory",
        "DesiredPhysicalPageTableRatio",
        "DesiredProcessingUnits",
        "DesiredProcessorCompatibilityMode",
        "DesiredVirtualProcessors",
        "ExpansionFactor",
        "HardwarePageTableRatio",
        "HasDedicatedProcessors",
        "IOConfigurationInstance",
        "MaximumHugePageCount",
        "MaximumMemory",
        "MaximumProcessingUnits",
        "MaximumVirtualIOSlots",
        "MaximumVirtualProcessors",
        "MemoryWeight",
        "MinimumHugePageCount",
        "MinimumMemory",
        "MinimumProcessingUnits",
        "MinimumVirtualProcessors",
        "ProcessorAttributes",
        "ProfileMemory",
        "ProfileName",
        "RedundantErrorPathReportingEnabled",
        "SharedProcessorConfiguration",
        "SharedProcessorPoolID",
        "SharedProcessorPoolName",
        "SharingMode",
        "UncappedWeight",
        "VirtualOpticonnectPool",
    ]
)
OPTIONAL = {
    "IOConfigurationInstance/VirtualOpticonnectPool": "false",
    "ProcessorAttributes/SharedProcessorConfiguration/UncappedWeight": "0",
    "ProfileMemory/DesiredEntitledMemory": "AUTO",
    "ProfileMemory/DesiredHugePageCount": "0",
    "ProfileMemory/ManualEntitledModeEnabled": "false",
    "ProfileMemory/MaximumHugePageCount": "0",
    "ProfileMemory/MemoryWeight": "0",
    "ProfileMemory/MinimumHugePageCount": "0",
}
EFFECTS = ("create", "stamp", "normalize", "copy-a", "copy-b", "patch-b", "delete")
MEDIA = "application/vnd.ibm.powervm.uom+xml; type=LogicalPartitionProfile"


def serialize(node):
    return ET.tostring(node, encoding="unicode")


def profile(xml):
    root = fromstring(xml)
    found = list(root.iter(UOM + "LogicalPartitionProfile"))
    if len(found) != 1:
        raise ValueError("one exact UOM profile is required")
    result = found[0]
    names = [n.tag for n in result]
    if len(names) != len(set(names)):
        raise ValueError("duplicate profile fields")
    return result


def scalar(root, name):
    nodes = root.findall(UOM + name)
    if len(nodes) != 1 or list(nodes[0]) or not (nodes[0].text or "").strip():
        raise ValueError("one nonempty scalar is required: " + name)
    return nodes[0]


def canonical(node, *, configuration=False, root=True):
    children = []
    for child in node:
        if configuration:
            if root and child.tag in {
                UOM + x for x in ("Metadata", "ProfileName", "SettingID")
            }:
                continue
            if child.tag == UOM + "Metadata" and all(
                n.tag in {UOM + "Metadata", UOM + "Atom"}
                and not (n.text or "").strip()
                and not n.attrib
                for n in child.iter()
            ):
                continue
        children.append(canonical(child, configuration=configuration, root=False))
    attributes = tuple(
        sorted(
            (key, value)
            for key, value in node.attrib.items()
            if not configuration or key not in ANNOTATIONS
        )
    )
    return node.tag, attributes, (node.text or "").strip(), tuple(children)


def differences(before, after, path=""):
    """Complete structural deltas; callers never use a truncated diagnostic as proof."""
    old, new = canonical(before), canonical(after)
    if old == new:
        return []
    if old[0] != new[0]:
        return [{"path": path, "before": old, "after": new}]
    here = path + "/" + old[0]
    result = []
    if old[1:3] != new[1:3]:
        result.append({"path": here, "before": old[1:3], "after": new[1:3]})
    count = max(len(before), len(after))
    for index in range(count):
        a = before[index] if index < len(before) else None
        b = after[index] if index < len(after) else None
        if a is None or b is None:
            result.append(
                {
                    "path": here + f"[{index}]",
                    "before": None if a is None else canonical(a),
                    "after": None if b is None else canonical(b),
                }
            )
        else:
            result.extend(differences(a, b, here + f"[{index}]"))
    return result


def compare_profiles(before, after):
    left, right = profile(before), profile(after)
    deltas = {}
    for name in ("ProfileName", "SettingID", "Metadata"):
        a, b = left.find(UOM + name), right.find(UOM + name)
        old = None if a is None else canonical(a)
        new = None if b is None else canonical(b)
        if old != new:
            deltas[name] = {"before": old, "after": new}
    return {
        "strict_equal": canonical(left) == canonical(right),
        "configuration_equal": canonical(left, configuration=True)
        == canonical(right, configuration=True),
        "root_identity_deltas": deltas,
        "recovery_admitted": False,
        "differences": differences(left, right),
        "differences_complete": True,
    }


def clone_payload(xml, name):
    root = copy.deepcopy(profile(xml))
    scalar(root, "ProfileName").text = name
    for child in list(root):
        if child.tag in {
            UOM + x
            for x in ("Metadata", "AssociatedPartition", "ProfileType", "SettingID")
        }:
            root.remove(child)
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag == UOM + "Metadata":
                if any(
                    n.tag not in {UOM + "Metadata", UOM + "Atom"}
                    or n.attrib
                    or (n.text or "").strip()
                    for n in child.iter()
                ):
                    raise ValueError("nonempty nested metadata shape is not admitted")
                parent.remove(child)
            elif (
                child.tag == UOM + "ManualEntitledModeEnabled"
                and child.get("kb") == "ROR"
            ):
                parent.remove(child)
    for node in root.iter():
        if node is root:
            continue
        if not node.tag.startswith(UOM) or node.tag[len(UOM) :] not in NESTED:
            raise ValueError("unknown or adapter-bearing profile shape")
        if node.get("kb") not in {None, "CUD", "CUR", "COD"}:
            raise ValueError("unproved createability in sparse shape")
    return serialize(root)


def memory_payload(xml, old, new):
    root = copy.deepcopy(profile(xml))
    memory = root.find(UOM + "ProfileMemory")
    if memory is None or not 512 <= new <= 1024:
        raise ValueError("fixed memory envelope is required")
    desired = scalar(memory, "DesiredMemory")
    if (desired.text, desired.get("kb")) != (str(old), "CUD") or (
        scalar(memory, "MinimumMemory").text,
        scalar(memory, "MaximumMemory").text,
    ) != ("512", "1024"):
        raise ValueError("fresh exact CUD memory/bounds required")
    desired.text = str(new)
    return serialize(root)


def normalization_permitted(expected, actual):
    """Only newly present, previously observed optional values may seed the copy experiment."""
    left, right = profile(expected), copy.deepcopy(profile(actual))
    for path, value in OPTIONAL.items():
        expanded = "/".join(UOM + part for part in path.split("/"))
        old, new = left.find(expanded), right.find(expanded)
        if old is None and new is not None:
            if list(new) or new.text != value:
                return False
            parent_path = "/".join(UOM + part for part in path.split("/")[:-1])
            parent = right.find(parent_path)
            if parent is None:
                return False
            parent.remove(new)
    return (
        scalar(left, "SettingID").text == scalar(right, "SettingID").text
        and scalar(left, "ProfileName").text == scalar(right, "ProfileName").text
        and canonical(left, configuration=True) == canonical(right, configuration=True)
    )


def save(path, value):
    payload = json.dumps(value, sort_keys=True).encode()
    if len(payload) > 32 * 1024 * 1024:
        raise ValueError("private journal exceeds 32MiB")
    temporary = path.with_suffix(".pending")
    with temporary.open("xb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class Journal:
    def __init__(self, path, data=None):
        self.path = path
        self.data: dict[str, Any] = (
            data
            if data is not None
            else {"pending": None, "attempted": [], "completed": []}
        )

    def write(self):
        save(self.path, self.data)

    @property
    def cleanup_permitted(self):
        return self.data.get("pending") is None and not self.data.get(
            "uncertain", False
        )

    def intent(self, stage):
        if not self.cleanup_permitted:
            raise ValueError("pending or uncertain effect prevents further writes")
        if stage not in EFFECTS or stage in self.data["attempted"]:
            raise ValueError("effect replay is prohibited")
        self.data["attempted"].append(stage)
        self.data["pending"] = stage
        self.write()

    def settle(self, stage):
        if self.data["pending"] != stage:
            raise ValueError("effect settlement does not match intent")
        self.data["completed"].append(stage)
        self.data["pending"] = None
        self.write()


class Budget:
    def __init__(self, limit, used=0):
        self.limit, self.used = limit, used

    def take(self, *, logoff=False):
        if self.used >= self.limit - (0 if logoff else 1):
            raise ValueError("transport budget exhausted; logout slot reserved")
        self.used += 1


def private_file(path):
    path = Path(path)
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
        or path.stat().st_mode & 0o077
    ):
        raise ValueError("existing private regular file required")
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ValueError("private parent directory required")
    return path.resolve()


def configured(config):
    path = private_file(config.partition_profile_contract_packet)
    raw = path.read_bytes()
    if (
        len(raw) > 65536
        or hashlib.sha256(raw).hexdigest() != config.partition_profile_contract_sha256
    ):
        raise ValueError("exact approved packet digest required")
    packet = json.loads(raw)
    required = {
        "authorized",
        "host",
        "port",
        "user",
        "agent_id",
        "system_uuid",
        "system_name",
        "mtms",
        "expected_build",
        "tested_commit",
        "tested_tree",
        "fixture",
        "base",
        "copies",
        "caller",
        "window_dir",
    }
    if set(packet) != required or packet["authorized"] is not True:
        raise ValueError("exact authorized experiment packet required")
    if not is_uuid(packet["system_uuid"]) or not all(
        re.fullmatch(r"[0-9a-f]{40}", str(packet[k]))
        for k in ("tested_commit", "tested_tree")
    ):
        raise ValueError("exact system and revision identities required")
    if (
        packet["expected_build"] != "2408210051"
        or type(packet["port"]) is not int
        or not 0 < packet["port"] < 65536
    ):
        raise ValueError("the approved V10R3 build/port binding is required")
    if not re.fullmatch(
        PREFIX + r"[0-9a-f]{8}", str(packet["fixture"])
    ) or not re.fullmatch(r"q1724-[0-9a-f]{8}", str(packet["caller"])):
        raise ValueError("fresh experiment fixture and caller names required")
    names = [packet["base"], *packet["copies"]]
    if (
        len(names) != 3
        or len(set(names)) != 3
        or not all(re.fullmatch(r"q1724-[a-z0-9-]{1,40}", str(n)) for n in names)
    ):
        raise ValueError("exactly three distinct bounded profile names required")
    directory = Path(packet["window_dir"])
    root = Path(__file__).resolve().parents[2]
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or directory.stat().st_uid != os.getuid()
        or directory.stat().st_mode & 0o077
        or root == directory.resolve()
        or root in directory.resolve().parents
    ):
        raise ValueError("private output directory outside checkout required")
    if config.system_name != packet["system_name"]:
        raise ValueError("configured managed system differs from approved packet")
    return packet


def verify_revision(packet):
    root = Path(__file__).resolve().parents[2]

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True
        ).strip()

    if (
        git("rev-parse", "HEAD") != packet["tested_commit"]
        or git("rev-parse", "HEAD^{tree}") != packet["tested_tree"]
        or git("status", "--porcelain", "--untracked-files=all")
    ):
        raise ValueError("deployed clean commit/tree differs from approved packet")
    if (
        Path(inspect.getfile(HMCClient)).resolve()
        != root / "src/hmcpctl/client/core.py"
    ):
        raise ValueError("imported package is not the verified deployed source")


def pinned(hmc_config, packet):
    if (
        hmc_config.host,
        hmc_config.port,
        hmc_config.user,
        hmc_config.agent_id or "hmcpctl",
    ) != (packet["host"], packet["port"], packet["user"], packet["agent_id"]):
        raise ValueError(
            "effective HMC destination/account/owner differs before authentication"
        )
    return hmc_config.model_copy(
        update={
            "port": packet["port"],
            "timeout": 15,
            "ssh_timeout": 15,
            "max_response_bytes": 8 * 1024 * 1024,
        }
    )


class Native(HMCClient):
    def __init__(self, config, packet, budget, directory):
        super().__init__(pinned(config, packet))
        self.packet, self.budget, self.directory = packet, budget, directory
        self.deadline = float("inf")

    async def _request(self, method, path, **kwargs):
        self.budget.take(logoff=method == "DELETE" and path == "/rest/api/web/Logon")
        number = self.budget.used
        auth = path == "/rest/api/web/Logon"
        row = {"method": method, "path": path}
        if not auth:
            save(
                self.directory / f"request-{number}.json",
                {
                    **row,
                    "body": kwargs.get("content"),
                    "headers": {
                        k: v
                        for k, v in kwargs.get("headers", {}).items()
                        if k.lower() in {"accept", "content-type", "if-match"}
                    },
                },
            )
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise ValueError("absolute native request deadline expired")
        started = monotonic()
        try:
            async with asyncio.timeout(min(30, remaining)):
                response = await super()._request(method, path, **kwargs)
        except BaseException as error:
            if not auth:
                finished = monotonic()
                classes, seen = [], set()
                cause = error
                while cause is not None and len(classes) < 8 and id(cause) not in seen:
                    seen.add(id(cause))
                    classes.append(type(cause).__name__[:128])
                    cause = cause.__cause__ or (
                        None if cause.__suppress_context__ else cause.__context__
                    )
                try:
                    save(
                        self.directory / f"failure-{number}.json",
                        {
                            **row,
                            "elapsed_ms": max(0, int((finished - started) * 1000)),
                            "exception_classes": classes,
                            "deadline_remaining_ms": max(
                                0, int((self.deadline - finished) * 1000)
                            )
                            if math.isfinite(self.deadline)
                            else None,
                            "transport_count": number,
                        },
                    )
                except (OSError, ValueError) as diagnostic_error:
                    error.add_note(
                        "Private transport diagnostic could not be saved ("
                        + type(diagnostic_error).__name__
                        + ")."
                    )
            raise
        if not auth:
            # HMC error bodies can echo authentication/cookie headers; never retain them.
            save(
                self.directory / f"response-{number}.json",
                {
                    **row,
                    "status": response.status_code,
                    "headers": {
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() in {"etag", "content-type", "x-hmc-schema-version"}
                    },
                    "sha256": hashlib.sha256(response.content).hexdigest(),
                    "body": response.text
                    if 200 <= response.status_code < 300
                    else None,
                },
            )
        return response

    async def command(self, command):
        self.budget.take()
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise ValueError("absolute native SSH deadline expired")
        result = await asyncio.wait_for(
            run_hmc_command(self.config, command), min(15, remaining)
        )
        if len(result.encode()) > 8 * 1024 * 1024:
            raise ValueError("SSH response exceeds 8MiB")
        return result

    async def snapshot(self):
        system = await self.get_managed_system(self.packet["system_uuid"])
        if system is None or system["UUID"] != self.packet["system_uuid"]:
            raise ValueError("exact operating managed system required")
        resource = system["Resource"]
        if (
            resource.get("SystemName") != self.packet["system_name"]
            or domain.render_mtms(resource) != self.packet["mtms"]
            or domain._text(resource.get("State")).lower() != "operating"
        ):
            raise ValueError("managed system identity/state differs")
        # _observe's one SSH namespace read is reserved before dispatch. An early
        # failure can overcount, never undercount, this upper transport bound.
        self.budget.take()
        phase = await domain._observe(self, self.packet["system_uuid"])
        return {
            "partitions": sorted(
                [
                    {
                        "UUID": u,
                        "kind": e["kind"],
                        "Resource": e["entry"]["Resource"],
                        "profiles": sorted(e["profiles"], key=lambda p: p["UUID"]),
                    }
                    for u, e in phase.entries.items()
                ],
                key=lambda e: e["UUID"],
            ),
            "namespace": list(phase.namespace),
            "system_auto_off": resource.get("AssociatedIPLConfiguration", {}).get(
                "PowerOffWhenLastLogicalPartitionIsShutdown"
            ),
            "capacity": {
                k: resource.get(k)
                for k in (
                    "AssociatedSystemMemoryConfiguration",
                    "AssociatedSystemProcessorConfiguration",
                )
            },
        }

    def profile_path(self, partition_uuid, profile_uuid=None):
        if (
            not is_uuid(partition_uuid)
            or profile_uuid is not None
            and not is_uuid(profile_uuid)
        ):
            raise ValueError("contained UUIDs required")
        path = f"/rest/api/uom/ManagedSystem/{self.packet['system_uuid']}/LogicalPartition/{partition_uuid}/LogicalPartitionProfile"
        return path if profile_uuid is None else path + "/" + profile_uuid

    async def read_profile(self, partition_uuid, profile_uuid):
        response = await self._request(
            "GET",
            self.profile_path(partition_uuid, profile_uuid),
            headers=self._uom_headers("LogicalPartitionProfile", False),
        )
        if response.status_code != 200 or not response.headers.get("ETag"):
            raise ValueError("full profile GET200 with ETag required")
        root = profile(response.text)
        parent = root.find(UOM + "AssociatedPartition")
        if (
            parent is None
            or scalar(root, "ProfileType").text != "REG_LPAR_PROFILE_TYPE"
        ):
            raise ValueError("contained regular profile required")
        domain._profile_parent(
            {
                "UUID": profile_uuid,
                "Resource": {"AssociatedPartition": dict(parent.attrib)},
            },
            partition_uuid,
            self.packet["system_uuid"],
            self,
            "LogicalPartition",
        )
        identities = root.findall(".//" + UOM + "AtomID")
        if not identities or any(
            (n.text or "").lower() != profile_uuid.lower() for n in identities
        ):
            raise ValueError("profile metadata UUID differs")
        scalar(root, "SettingID")
        return {"xml": response.text, "etag": response.headers["ETag"]}


async def exercise(client, state):
    """Dispatch only the explicitly selected finite experiment; no maturity assertion."""
    del client
    if state.group != GROUP or state.hmc_config is None:
        state.skip(
            SUBTASK, "profile contract experiment", "requires its own named group"
        )
        return
    from .profile_contract_run import execute

    try:
        if state.checkpoint is None:
            raise ValueError(
                "durable runner recovery checkpoint required before effects"
            )
        state.checkpoint()
        result = await execute(state.hmc_config, state.config)
    except Exception as error:  # noqa: BLE001 — journal retains private details; runner reports a safe failure
        state.record(
            SUBTASK,
            "profile contract experiment",
            "FAIL",
            {"error_type": type(error).__name__, "recovery_required": True},
        )
        return
    state.record(
        SUBTASK,
        "profile contract experiment",
        "PASS" if result["status"] == "CONTRACT_OBSERVED" else "FAIL",
        {
            key: result.get(key)
            for key in (
                "status",
                "deleted",
                "copies",
                "patches",
                "arm_transports_upper_bound",
            )
        },
    )


async def recover_document(hmc_config, document):
    from .config import LiveTestConfig
    from .profile_contract_run import recover

    run = document.get("run", {})
    if (
        run.get("group") != GROUP
        or run.get("subtasks") != [SUBTASK]
        or run.get("tree_clean") is not True
    ):
        raise ValueError("exact clean experiment dispatch provenance required")
    config = LiveTestConfig(**document["config"])
    packet = configured(config)
    if run.get("tested_commit") != packet["tested_commit"] or document.get(
        "hmc", {}
    ) != {
        "host": hmc_config.host,
        "port": hmc_config.port,
        "user": hmc_config.user,
        "verify_ssl": hmc_config.verify_ssl,
    }:
        raise ValueError("recovery document destination/revision differs")
    return await recover(hmc_config, config)
