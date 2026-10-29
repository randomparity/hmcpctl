"""ST63 standalone serial creation; retained intent is never effect authority."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import inspect
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from hmcpctl.client.core import HMCClient
from hmcpctl.config import load_profile
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_serial import VirtualSerialAdapter
from hmcpctl.operations.lpar.linux_roles import read_role_capabilities
from hmcpctl.operations.lpar.ownership import (
    parse_lpar_ownership_caller_token,
    parse_lpar_ownership_owner,
)
from hmcpctl.operations.lpar.serial import admit_serial_creation, confirm_serial_created
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.commands import build_filter
from hmcpctl.ssh.lpar import create_lpar_via_cli, stamp_lpar_ownership
from hmcpctl.ssh.transport import run_hmc_command
from hmcpctl.xmlutil import leaf_text, render_mtms

GROUP = "lpar-serial-adapter"
SUBTASK = 63
ROOT = Path(__file__).resolve().parents[2]
LIMIT = 1024 * 1024
FIELDS = (
    "lpar_name",
    "lpar_id",
    "name",
    "max_virtual_slots",
    "virtual_serial_adapters",
    "virtual_eth_adapters",
    "virtual_scsi_adapters",
    "virtual_fc_adapters",
    "vnic_adapters",
)
RESOURCES = LparResources(
    min_memory=1024,
    desired_memory=1024,
    max_memory=1024,
    dedicated=True,
    min_procs=1,
    desired_procs=1,
    max_procs=1,
    sharing_mode="keep_idle_procs",
)


def _exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("ST63 exact object fields required")


def validate_packet(packet):
    _exact(
        packet,
        (
            "candidate",
            "expires_utc",
            "system_uuid",
            "partition_type",
            "name",
            "caller_token",
            "cleanup_authorized",
        ),
    )
    candidate = packet["candidate"]
    _exact(candidate, ("commit", "tree", "hmc_identity"))
    if any(
        not isinstance(candidate[k], str)
        or re.fullmatch(r"[0-9a-f]{40}", candidate[k]) is None
        for k in ("commit", "tree")
    ):
        raise ValueError("ST63 exact candidate commit/tree required")
    identity = candidate["hmc_identity"]
    _exact(identity, ("host", "port", "user", "verify_ssl", "agent_id"))
    if (
        any(
            not isinstance(identity[k], str) or not identity[k].strip()
            for k in ("host", "user", "agent_id")
        )
        or type(identity["port"]) is not int
        or not 1 <= identity["port"] <= 65535
        or type(identity["verify_ssl"]) is not bool
        or type(packet["cleanup_authorized"]) is not bool
        or not isinstance(packet["system_uuid"], str)
        or not is_uuid(packet["system_uuid"])
        or packet["partition_type"] not in ("AIX/Linux", "OS400", "Virtual IO Server")
        or not isinstance(packet["name"], str)
        or re.fullmatch(r"hmcpctl-live-serial-[0-9a-f]{8}", packet["name"]) is None
        or not isinstance(packet["caller_token"], str)
        or re.fullmatch(r"q1772-[0-9a-f]{8}", packet["caller_token"]) is None
    ):
        raise ValueError("ST63 strict target, identity and cleanup fields required")
    _expiry(packet)


def _expiry(packet):
    value = packet["expires_utc"]
    if not isinstance(value, str):
        raise TypeError("ST63 UTC expiry required")
    expiry = datetime.fromisoformat(value)
    offset = expiry.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("ST63 UTC expiry required")
    return expiry


def connection(config):
    return {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "verify_ssl": config.verify_ssl,
        "agent_id": config.agent_id or "hmcpctl",
    }


def revision(candidate):
    def git(*args):
        return subprocess.check_output(
            ["git", "--no-pager", *args], cwd=ROOT, text=True
        ).strip()

    if (
        git("rev-parse", "HEAD") != candidate["commit"]
        or git("rev-parse", "HEAD^{tree}") != candidate["tree"]
        or git("status", "--porcelain", "--untracked-files=all")
        or Path(inspect.getfile(HMCClient)).resolve()
        != ROOT / "src/hmcpctl/client/core.py"
    ):
        raise ValueError("ST63 clean deployed candidate/import differs")


def binding(packet, config):
    validate_packet(packet)
    if connection(config) != packet["candidate"]["hmc_identity"]:
        raise ValueError("ST63 effective HMC identity differs before authentication")
    revision(packet["candidate"])
    if datetime.now(UTC) >= _expiry(packet):
        raise ValueError("ST63 current packet has expired")


def private_path(path, *, fresh=False, maximum=LIMIT):
    if type(maximum) is not int or maximum not in (LIMIT, 4 * LIMIT, 8 * LIMIT):
        raise ValueError("fixed 1/4/8MiB private-file ceiling required")
    path = Path(path).absolute()
    resolved = path.resolve()
    parent = path.parent.lstat()
    if (
        path.is_symlink()
        or path.parent.is_symlink()
        or resolved == ROOT
        or ROOT in resolved.parents
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_mode & 0o077
    ):
        raise ValueError("ST63 private operator-owned path outside checkout required")
    if fresh:
        if path.exists():
            raise ValueError("ST63 fresh output cannot replace prior intent")
    else:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise ValueError("ST63 bounded private regular file required")
    return path


def read(path, *, maximum=LIMIT):
    with private_path(path, maximum=maximum).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("private input exceeds fixed byte bound")
    return json.loads(raw)


def load_packet(path, digest):
    raw = private_path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("ST63 packet digest differs")
    packet = json.loads(raw)
    validate_packet(packet)
    return packet


def write(path, document, *, fresh=False, maximum=LIMIT):
    raw = json.dumps(document, sort_keys=True, indent=2).encode()
    if type(maximum) is not int or maximum not in (LIMIT, 4 * LIMIT, 8 * LIMIT):
        raise ValueError("fixed 1/4/8MiB private-file ceiling required")
    if len(raw) > maximum:
        raise ValueError("ST63 retained evidence exceeds bound")
    path = private_path(path, fresh=fresh, maximum=maximum)
    if fresh:
        with path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".st63-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def adapters(packet):
    return (
        VirtualSerialAdapter(
            2,
            "server",
            False,
            supports_hmc=0,
            remote_lpar_name=packet["name"],
            remote_slot=3,
        ),
        VirtualSerialAdapter(
            3, "client", False, remote_lpar_name=packet["name"], remote_slot=2
        ),
    )


def kind(packet):
    return (
        "VirtualIOServer"
        if packet["partition_type"] == "Virtual IO Server"
        else "LogicalPartition"
    )


async def _command(hmc, command):
    raw = await run_hmc_command(
        hmc.config, command, include_stderr=True, max_output_bytes=LIMIT
    )
    if (
        not isinstance(raw, str)
        or len(raw.encode()) > LIMIT
        or "[stderr]" in raw.splitlines()
    ):
        raise ValueError("ST63 bounded independent native observation unavailable")
    return raw


def _feed(rows, resource_kind):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("ST63 partition feed unavailable")
    result = []
    for entry in rows:
        r = entry.get("Resource", {})
        identity = entry.get("UUID", "")
        native = leaf_text(r.get("PartitionID"))
        if (
            not is_uuid(identity)
            or entry.get("ResourceType") != resource_kind
            or not isinstance(r.get("PartitionName"), str)
            or not r["PartitionName"]
            or not isinstance(native, str)
            or re.fullmatch(r"[0-9]+", native) is None
        ):
            raise ValueError("ST63 feed identity unavailable")
        result.append(
            {
                "uuid": identity.lower(),
                "kind": resource_kind,
                "name": r["PartitionName"],
                "partition_id": int(native),
                "type": r.get("PartitionType"),
                "state": r.get("PartitionState"),
                "description": leaf_text(r.get("Description")),
            }
        )
    return result


def saved_profiles(raw, partitions):
    """Retain every native profile attribute; compare records independently of ordering."""
    try:
        rows = list(csv.reader(io.StringIO(raw), strict=True))
    except csv.Error as error:
        raise ValueError("ST63 full saved-profile inventory malformed") from error
    records, identities = [], set()
    for row in rows:
        record = {}
        for field in row:
            key, separator, value = field.partition("=")
            if not separator or not key or key in record:
                raise ValueError("ST63 full saved-profile attribute missing/duplicate")
            record[key] = value
        identity = (record.get("lpar_name"), record.get("name"))
        if not all(identity) or identity in identities:
            raise ValueError("ST63 saved-profile identity missing/duplicate")
        identities.add(identity)
        records.append(record)
    if {e["name"] for e in partitions} != {r["lpar_name"] for r in records}:
        raise ValueError("ST63 complete contained saved profiles required")
    return sorted(records, key=lambda r: (r["lpar_name"], r["name"]))


async def snapshot(hmc, packet):
    system = await hmc.get_managed_system(packet["system_uuid"])
    r = (system or {}).get("Resource", {})
    system_name = render_mtms(r)
    if (
        str((system or {}).get("UUID", "")).lower() != packet["system_uuid"].lower()
        or not system_name
        or leaf_text(r.get("State")) != "operating"
    ):
        raise ValueError("ST63 selected operating system identity unavailable")
    caps = await read_role_capabilities(
        hmc, packet["system_uuid"], need_kvm=False, need_management=False
    )
    if caps.controller_mode:
        raise ValueError("ST63 classic saved-profile observation unavailable")
    rows = _feed(
        await hmc.list_logical_partitions(packet["system_uuid"]), "LogicalPartition"
    )
    rows += _feed(await hmc.list_vios(packet["system_uuid"]), "VirtualIOServer")
    for key in ("uuid", "name", "partition_id"):
        if len({e[key] for e in rows}) != len(rows):
            raise ValueError("ST63 duplicate feed identity")
    profiles = await _command(hmc, f"lssyscfg -r prof -m {shlex.quote(system_name)}")
    if profiles.strip() == "No results were found.":
        profiles = ""
    if rows and not profiles.strip():
        raise ValueError("ST63 complete saved profiles unavailable")
    memory = r.get("AssociatedSystemMemoryConfiguration", {})
    processor = r.get("AssociatedSystemProcessorConfiguration", {})
    capacity = {
        "memory": leaf_text(memory.get("CurrentAvailableSystemMemory")),
        "processors": leaf_text(processor.get("CurrentAvailableSystemProcessorUnits")),
    }
    # Decimal avoids silently accepting nonfinite native values.
    from decimal import Decimal

    values = [Decimal(str(capacity[k])) for k in ("memory", "processors")]
    if any(not v.is_finite() or v < 0 for v in values):
        raise ValueError("ST63 available resources unavailable")
    return {
        "system": system_name,
        "partitions": sorted(rows, key=lambda e: e["uuid"]),
        "profiles": saved_profiles(profiles, rows),
        "capacity": capacity,
    }


def admission(snapshot_, packet):
    from decimal import Decimal

    if (
        any(e["name"].startswith("hmcpctl-live-") for e in snapshot_["partitions"])
        or Decimal(snapshot_["capacity"]["memory"]) < 1024
        or Decimal(snapshot_["capacity"]["processors"]) < 1
    ):
        raise ValueError("ST63 residue or insufficient one-fixture capacity")


def pair_oracle(raw, name, native):
    """Literal CSV projection, independent of product serial parsers and pair logic."""
    if not isinstance(raw, str) or len(raw.encode()) > LIMIT:
        raise ValueError("ST63 pair observation unavailable")
    try:
        rows = list(csv.reader(io.StringIO(raw), strict=True))
    except csv.Error as error:
        raise ValueError("ST63 malformed CSV pair observation") from error
    if len(rows) != 2 or rows[0] != list(FIELDS) or len(rows[1]) != len(FIELDS):
        raise ValueError("ST63 exact pair header and single row required")
    row = dict(zip(FIELDS, rows[1], strict=True))
    if [row[k] for k in FIELDS[:4]] != [
        name,
        str(native),
        "default_profile",
        "8",
    ] or any(row[k] not in ("", "none") for k in FIELDS[5:]):
        raise ValueError("ST63 profile identity/maximum/other-family mismatch")
    records = {}
    for text in row["virtual_serial_adapters"].split(","):
        parts = text.split("/")
        if len(parts) != 7 or parts[0] in records:
            raise ValueError("ST63 duplicate or malformed serial tuple")
        records[parts[0]] = parts
    for slot in ("0", "1"):
        if slot in records:
            fields = records.pop(slot)
            if fields != [slot, "server", "1", "any", "", "any", "1"]:
                raise ValueError("ST63 unexpected implicit console tuple")
    if set(records) != {"2", "3"}:
        raise ValueError("ST63 exact custom reciprocal pair required")
    for slot, role, hmc_value, remote in (
        ("2", "server", "0", "3"),
        ("3", "client", "", "2"),
    ):
        f = records[slot]
        if (
            f[1:3] != [role, hmc_value]
            or f[3] != str(native)
            or f[4] not in ("", name)
            or f[5:] != [remote, "0"]
        ):
            raise ValueError("ST63 independent reciprocal pair mismatch")


async def fixture(hmc, packet, baseline, identity):
    current = await snapshot(hmc, packet)
    matching = [e for e in current["partitions"] if e["uuid"] == identity.lower()]
    if (
        len(matching) != 1
        or identity.lower() in {e["uuid"] for e in baseline["partitions"]}
        or matching[0]["name"] != packet["name"]
        or matching[0]["kind"] != kind(packet)
        or matching[0]["type"] != packet["partition_type"]
        or matching[0]["state"] != "not activated"
        or [e for e in current["partitions"] if e["uuid"] != identity.lower()]
        != baseline["partitions"]
        or [p for p in current["profiles"] if p["lpar_name"] != packet["name"]]
        != baseline["profiles"]
        or [p["name"] for p in current["profiles"] if p["lpar_name"] == packet["name"]]
        != ["default_profile"]
    ):
        raise ValueError("ST63 exact fresh contained inactive fixture unconfirmed")
    entry = await hmc.get_uom(kind(packet), identity)
    r = (entry or {}).get("Resource", {})
    if (
        str((entry or {}).get("UUID", "")).lower() != identity.lower()
        or r.get("PartitionName") != packet["name"]
        or leaf_text(r.get("PartitionID")) != str(matching[0]["partition_id"])
        or r.get("PartitionType") != packet["partition_type"]
        or r.get("PartitionState") != "not activated"
    ):
        raise ValueError("ST63 correct-kind fresh identity differs")
    memory = r.get("PartitionMemoryConfiguration", {})
    processors = r.get("PartitionProcessorConfiguration", {})
    dedicated = processors.get("DedicatedProcessorConfiguration", {})
    if (
        any(
            leaf_text(memory.get(k)) != "1024"
            for k in ("MinimumMemory", "DesiredMemory", "MaximumMemory")
        )
        or leaf_text(processors.get("HasDedicatedProcessors")) != "true"
        or any(
            leaf_text(dedicated.get(k)) != "1"
            for k in ("MinimumProcessors", "DesiredProcessors", "MaximumProcessors")
        )
    ):
        raise ValueError("ST63 one GiB/one dedicated CPU fixture differs")
    return entry


async def verify_fixture(hmc, packet, baseline, identity):
    entry = await fixture(hmc, packet, baseline, identity)
    r = entry["Resource"]
    description = leaf_text(r.get("Description"))
    if not isinstance(description, str):
        raise TypeError("ST63 fixture description unavailable")
    if kind(packet) == "LogicalPartition" and (
        parse_lpar_ownership_owner(description) != connection(hmc.config)["agent_id"]
        or parse_lpar_ownership_caller_token(description) != packet["caller_token"]
    ):
        raise ValueError("ST63 current LPAR owner/caller differs")
    filters = build_filter(
        [("lpar_names", packet["name"]), ("profile_names", "default_profile")]
    )
    command = (
        f"lssyscfg -r prof -m {shlex.quote(baseline['system'])} "
        f"--filter {shlex.quote(filters)} -F {','.join(FIELDS)} --header"
    )
    native = leaf_text(r["PartitionID"])
    if not isinstance(native, str):
        raise TypeError("ST63 fixture native ID unavailable")
    pair_oracle(await _command(hmc, command), packet["name"], int(native))
    await fixture(hmc, packet, baseline, identity)


class Window:
    """One in-memory invocation; journals are outputs and never restored authority."""

    def __init__(self, packet, digest, grant, exclusive):
        self.packet, self.digest = packet, digest
        self.grant, self.exclusive = grant, exclusive
        self.invocation = uuid.uuid4().hex

    def check(self, config, invocation):
        if self.grant != self.digest:
            raise PermissionError(
                "ST63 separate matching current packet grant required"
            )
        if self.exclusive is not True:
            raise PermissionError("ST63 current exclusive writer assertion required")
        if self.packet["cleanup_authorized"] is not True:
            raise PermissionError(
                "ST63 conditional cleanup authorization required before create"
            )
        if invocation != self.invocation:
            raise PermissionError("ST63 restored invocation cannot authorize effects")
        binding(self.packet, config)


async def execute(hmc, packet, digest, preflight, path, window, packet_path):
    window.check(hmc.config, window.invocation)
    if (
        preflight.get("packet_sha256") != digest
        or preflight.get("candidate") != packet["candidate"]
        or not 0
        <= (
            datetime.now(UTC) - datetime.fromisoformat(preflight["captured_utc"])
        ).total_seconds()
        < 60
    ):
        raise ValueError("ST63 fresh candidate/packet-bound preflight required")
    baseline = await snapshot(hmc, packet)
    admission(baseline, packet)
    if baseline != preflight["baseline"]:
        raise ValueError("ST63 baseline changed before create")
    serial = adapters(packet)
    admitted = await admit_serial_creation(
        hmc, packet["system_uuid"], packet["name"], None, 8, serial, kind(packet)
    )
    journal = {
        "group": GROUP,
        "subtask": SUBTASK,
        "packet_path": str(packet_path),
        "packet_sha256": digest,
        "candidate": packet["candidate"],
        "grant_packet_sha256": window.grant,
        "invocation": window.invocation,
        "baseline": baseline,
        "phase": "create-pending",
        "created_uuid": None,
        "complete": False,
        "delete_attempted": False,
    }
    window.check(hmc.config, journal["invocation"])
    write(path, journal, fresh=True)
    window.check(hmc.config, journal["invocation"])
    await create_lpar_via_cli(
        hmc.config,
        baseline["system"],
        packet["name"],
        packet["partition_type"],
        RESOURCES,
        max_virtual_slots=8,
        virtual_serial_adapters=serial,
    )
    # Only an acknowledged call can reach fresh identity reconciliation. A raised/lost
    # response leaves create-pending and never reaches stamp, delete or a replay.
    rows = (
        await hmc.list_vios(packet["system_uuid"])
        if kind(packet) == "VirtualIOServer"
        else await hmc.list_logical_partitions(packet["system_uuid"])
    )
    matches = [
        e for e in rows if e.get("Resource", {}).get("PartitionName") == packet["name"]
    ]
    if len(matches) != 1:
        raise ValueError("ST63 acknowledged create lacks unique fresh identity")
    entry = await confirm_serial_created(hmc, admitted, matches[0])
    identity = entry["UUID"]
    await fixture(hmc, packet, baseline, identity)
    journal.update(created_uuid=identity, phase="created-confirmed")
    write(path, journal)
    if kind(packet) == "LogicalPartition":
        window.check(hmc.config, journal["invocation"])
        journal["phase"] = "stamp-pending"
        write(path, journal)
        window.check(hmc.config, journal["invocation"])
        stamped = await stamp_lpar_ownership(
            hmc.config,
            baseline["system"],
            packet["name"],
            agent_id=hmc.config.agent_id,
            caller_token=packet["caller_token"],
        )
        if stamped is None:
            raise ValueError("ST63 stamp outcome unconfirmed; stop and retain")
    await verify_fixture(hmc, packet, baseline, identity)
    journal["phase"] = "verified"
    write(path, journal)
    # Verify again before the single UUID deletion. A changed identity or pair stops all effects.
    await verify_fixture(hmc, packet, baseline, identity)
    window.check(hmc.config, journal["invocation"])
    journal.update(phase="delete-pending", delete_attempted=True)
    write(path, journal)
    window.check(hmc.config, journal["invocation"])
    if kind(packet) == "VirtualIOServer":
        await hmc.delete_vios(identity)
    else:
        await hmc.delete_logical_partition(identity)
    if await snapshot(hmc, packet) != baseline:
        raise ValueError("ST63 post-delete baseline differs")
    journal.update(phase="complete", complete=True)
    write(path, journal)
    return journal


async def recover(hmc, packet, document):
    # No authority reconstruction or mutating owner is reachable from recovery.
    current = await snapshot(hmc, packet)
    if (
        document.get("group") != GROUP
        or document.get("subtask") != SUBTASK
        or document.get("candidate") != packet["candidate"]
        or document.get("complete") is not True
        or document.get("phase") != "complete"
        or document.get("delete_attempted") is not True
        or not is_uuid(document.get("created_uuid", ""))
        or current != document.get("baseline")
    ):
        raise ValueError(
            "ST63 incomplete intent or changed baseline; held for operator reconciliation"
        )
    return current


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "run", "recover"))
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--packet-sha256")
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--results-file", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--grant-packet-sha256")
    parser.add_argument("--exclusive-writer-window", action="store_true")
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:  # noqa: BLE001 — private CLI boundary
        print(
            f"ST63 STOPPED ({type(error).__name__}); inspect retained private intent and run read-only recovery.",
            file=sys.stderr,
        )
        return 2


def _dispatch(args):
    if args.mode != "run" and (
        args.grant_packet_sha256 or args.exclusive_writer_window
    ):
        raise ValueError("ST63 read-only phases accept no effect assertions")
    document = read(args.results) if args.mode == "recover" else None
    packet_path = Path(document["packet_path"]) if document is not None else args.packet
    digest = document["packet_sha256"] if document is not None else args.packet_sha256
    packet = load_packet(packet_path, digest)
    config = load_profile()
    if args.mode == "recover":
        if connection(config) != packet["candidate"]["hmc_identity"]:
            raise ValueError("ST63 recovery HMC identity differs before authentication")
        revision(packet["candidate"])
    else:
        binding(packet, config)
    window = Window(
        packet, digest, args.grant_packet_sha256, args.exclusive_writer_window
    )
    preflight = None
    if args.mode == "run":
        window.check(config, window.invocation)
        private_path(args.results_file, fresh=True)
        preflight = read(args.preflight)
    if args.mode == "preflight":
        private_path(args.output, fresh=True)

    async def run():
        async with HMCClient(config) as hmc:
            async with asyncio.timeout(300):
                if args.mode == "preflight":
                    baseline = await snapshot(hmc, packet)
                    admission(baseline, packet)
                    write(
                        args.output,
                        {
                            "packet_sha256": digest,
                            "candidate": packet["candidate"],
                            "captured_utc": datetime.now(UTC).isoformat(),
                            "baseline": baseline,
                        },
                        fresh=True,
                    )
                elif args.mode == "recover":
                    await recover(hmc, packet, document)
                else:
                    await execute(
                        hmc,
                        packet,
                        digest,
                        preflight,
                        args.results_file,
                        window,
                        packet_path,
                    )

    asyncio.run(run())
    print(
        "ST63 observational phase completed"
        if args.mode != "run"
        else "ST63 finite sequence complete; read-only recovery required"
    )
    return 0
