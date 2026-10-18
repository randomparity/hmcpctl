"""Capture an independent read-only live-window baseline, or compare two private snapshots."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

from defusedxml import ElementTree as ET
from live_test.storage_lifecycle import volume_listing

from hmcpctl.client.core import HMCClient
from hmcpctl.config import build_config
from hmcpctl.resource_identity import is_uuid, resolve_vios_uuid
from hmcpctl.ssh.commands import parse_hmc_result_rows
from hmcpctl.ssh.io_inventory import list_dedicated_pcie_slot_rows
from hmcpctl.ssh.profiles import parse_profile_io_slots, read_profile_io_slot_rows
from hmcpctl.ssh.sriov import (
    list_sriov_adapter_rows,
    list_sriov_configured_logical_port_rows,
    list_sriov_physical_port_rows,
)
from hmcpctl.ssh.transport import run_hmc_command
from hmcpctl.xmlutil import ATOM_NS, parse_feed

# Each table has exact fields and a structural identity; row order is immaterial.
TABLES = {
    "system": ("name,type_model,serial_num", "name"),
    "partitions": ("lpar_id,name,lpar_env,state", "lpar_id"),
    "slots": ("drc_index,description,lpar_name", "drc_index"),
    "profiles": ("lpar_name,name,io_slots", "lpar_name,name"),
    "sriov_adapters": (
        "adapter_id,slot_id,config_state,functional_state,phys_loc,phys_ports,logical_ports,adapter_max_logical_ports,sriov_status",
        "slot_id",
    ),
    "sriov_physical": (
        "adapter_id,phys_port_id,phys_port_type,phys_port_loc,state,config_logical_ports,phys_port_max_logical_ports,curr_eth_logical_ports,min_eth_capacity_granularity",
        "adapter_id,phys_port_id",
    ),
    "sriov_logical": (
        "config_id,lpar_name,lpar_id,lpar_state,adapter_id,logical_port_id,logical_port_type,phys_port_id,functional_state,capacity,max_capacity",
        "adapter_id,logical_port_id",
    ),
    "vios": ("id,name,uuid", "id"),
    "vios_mappings": (
        "vios_id,server,slot,remote_id,remote_slot,target,client,backing",
        "vios_id,server,target",
    ),
    "vios_groups": ("vios_id,uuid,name", "vios_id,uuid"),
    "vios_volumes": (
        "vios_id,group,name,type,lps,pps,pvs,state,mount",
        "vios_id,group,name",
    ),
}


def validate_backing(storage, target):
    """A present backing needs one known resource and its local identity."""
    if storage == {} and isinstance(target, dict):
        kinds = {key for key in target if not key.startswith("@")}
        if not kinds or kinds == {"VirtualOpticalTargetDevice"}:
            return
        raise ValueError("VIOS disk target has no backing")
    fields = {
        "VirtualDisk": "DiskName",
        "PhysicalVolume": "VolumeName",
        "LogicalUnit": "UnitName",
        "VirtualOpticalMedia": "MediaName",
    }
    if not isinstance(storage, dict):
        raise ValueError("VIOS mapping has malformed backing")  # noqa: TRY004 — external document
    variants = [key for key in storage if not key.startswith("@") and key != "Metadata"]
    if len(variants) != 1 or variants[0] not in fields:
        raise ValueError("VIOS mapping has unknown or missing backing variant")
    resource = storage[variants[0]]
    name = resource.get(fields[variants[0]]) if isinstance(resource, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("VIOS mapping backing has no identity")


def validate(document):
    """Reject partial documents before order-only canonicalization."""
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "scope", "tables"}
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise ValueError("snapshot version or top-level fields are invalid")
    scope, tables = document["scope"], document["tables"]
    if (
        not isinstance(scope, dict)
        or set(scope) != {"host", "system", "vios_ids"}
        or not all(
            isinstance(scope[k], str) and scope[k].strip() for k in ("host", "system")
        )
    ):
        raise ValueError("snapshot scope is incomplete")
    ids = scope["vios_ids"]
    if (
        not isinstance(ids, list)
        or any(type(i) is not int or i <= 0 for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("VIOS IDs must be distinct positive integers")
    if not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise ValueError("snapshot tables are incomplete")
    result = _normalize_tables(tables)
    _validate_relationships(scope, result)
    return {"version": 1, "scope": dict(scope, vios_ids=sorted(ids)), "tables": result}


def _normalize_tables(tables):
    """Validate each row and canonicalize table order without changing the input."""
    result = {}
    for name, (fields, keys) in TABLES.items():
        rows = tables[name]
        if not isinstance(rows, list):
            raise ValueError(f"{name}: expected rows")  # noqa: TRY004 — invalid external document
        seen, normalized = set(), []
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != set(fields.split(","))
                or any(not isinstance(v, str) for v in row.values())
            ):
                raise ValueError(f"{name}: incomplete row")
            identity = tuple(row[k] for k in keys.split(","))
            if (
                any(not row[k] for k in keys.split(",") if k != "target")
                or identity in seen
            ):
                raise ValueError(f"{name}: missing or duplicate identity")
            optional = {"description", "lpar_name"} if name == "slots" else {"target"}
            if any(
                not value.strip() for key, value in row.items() if key not in optional
            ):
                raise ValueError(f"{name}: empty required value")
            seen.add(identity)
            row = dict(row)
            if name == "profiles":
                parse_profile_io_slots(row["io_slots"])
                row["io_slots"] = ",".join(sorted(row["io_slots"].split(",")))
            if name == "vios_mappings":
                backing = json.loads(row["backing"], object_pairs_hook=_unique_object)
                if not isinstance(backing, dict) or set(backing) != {
                    "storage",
                    "target",
                }:
                    raise ValueError("VIOS mapping backing document is malformed")
                validate_backing(backing["storage"], backing["target"])
            normalized.append(row)
        result[name] = sorted(
            normalized, key=lambda row: tuple(row[k] for k in keys.split(","))
        )
    return result


def _validate_relationships(scope, result):
    """Reject incomplete or out-of-scope relationships between normalized tables."""
    ids = scope["vios_ids"]
    if (
        len(result["system"]) != 1
        or result["system"][0]["name"] != scope["system"]
        or any(not v for v in result["system"][0].values())
    ):
        raise ValueError("system identity is incomplete or does not match scope")
    if {row["id"] for row in result["vios"]} != {str(i) for i in ids}:
        raise ValueError("selected VIOS coverage is incomplete")
    adapters = {
        row["adapter_id"]: row
        for row in result["sriov_adapters"]
        if not (row["config_state"] == "dedicated" and row["adapter_id"] == "null")
    }
    for adapter_id, adapter in adapters.items():
        ports = [
            row for row in result["sriov_physical"] if row["adapter_id"] == adapter_id
        ]
        if not adapter["phys_ports"].isdigit() or len(ports) != int(
            adapter["phys_ports"]
        ):
            raise ValueError("SR-IOV physical-port coverage is incomplete")
    if any(row["adapter_id"] not in adapters for row in result["sriov_physical"]):
        raise ValueError("SR-IOV physical-port coverage contains an unknown adapter")
    physical = {
        (row["adapter_id"], row["phys_port_id"]): row
        for row in result["sriov_physical"]
    }
    if any(
        (row["adapter_id"], row["phys_port_id"]) not in physical
        or row["logical_port_type"] != "eth"
        for row in result["sriov_logical"]
    ):
        raise ValueError(
            "SR-IOV logical-port coverage contains an unknown physical port"
        )
    for identity, port in physical.items():
        logical = [
            row
            for row in result["sriov_logical"]
            if (row["adapter_id"], row["phys_port_id"]) == identity
        ]
        count = port["curr_eth_logical_ports"]
        if not count.isdigit() or len(logical) != int(count):
            raise ValueError("SR-IOV logical-port coverage is incomplete")
    partitions = {row["lpar_id"]: row for row in result["partitions"]}
    if any(not re.fullmatch(r"[0-9]+", key) for key in partitions):
        raise ValueError("partition ID is malformed")
    for row in result["vios"]:
        partition = partitions.get(row["id"], {})
        if (
            partition.get("name") != row["name"]
            or partition.get("lpar_env") != "vioserver"
            or not is_uuid(row["uuid"])
        ):
            raise ValueError("VIOS identity does not match its partition")
    groups = {(row["vios_id"], row["uuid"]) for row in result["vios_groups"]}
    if any(
        (row["vios_id"], row["group"]) not in groups for row in result["vios_volumes"]
    ):
        raise ValueError("volume has no captured group")
    for name in ("vios_mappings", "vios_groups", "vios_volumes"):
        if any(row["vios_id"] not in {str(i) for i in ids} for row in result[name]):
            raise ValueError("VIOS row lies outside the selected scope")


async def read_resources(hmc, path, kind):
    """Do not let permissive list projections turn absent evidence into []."""
    raw = await hmc._get(path, kind, include_schema_version=False)
    if not raw.strip():
        raise ValueError("REST read has no body")
    root = ET.fromstring(raw)
    if root.tag not in {f"{{{ATOM_NS}}}feed", f"{{{ATOM_NS}}}entry"}:
        raise ValueError("REST read has no Atom envelope")
    if root.tag == f"{{{ATOM_NS}}}feed":
        metadata = {
            "entry",
            "id",
            "title",
            "updated",
            "published",
            "author",
            "contributor",
            "link",
            "category",
            "generator",
            "subtitle",
            "rights",
        }
        if (root.text or "").strip() or any(
            child.tag not in {f"{{{ATOM_NS}}}{name}" for name in metadata}
            or child.get("rel") in {"next", "previous", "prev"}
            for child in root
        ):
            raise ValueError("REST feed is malformed or incomplete")
    entries = parse_feed(raw)
    if kind == "VirtualIOServer" and len(entries) != 1:
        raise ValueError("VIOS read has no unique resource")
    if any(
        entry.get("ResourceType") != kind or not isinstance(entry.get("Resource"), dict)
        for entry in entries
    ):
        raise ValueError("REST read has incomplete resources")
    return entries


def mapping_rows(resource, vios_id):
    collection = resource.get("VirtualSCSIMappings")
    if not isinstance(collection, dict):
        raise ValueError("VIOS read has no mapping container")  # noqa: TRY004 — invalid external document
    if set(collection) - {"@attrs", "Metadata", "VirtualSCSIMapping"}:
        raise ValueError("VIOS mapping container has unknown fields")
    mappings = collection.get("VirtualSCSIMapping", [])
    if isinstance(mappings, dict):
        mappings = [mappings]
    if not isinstance(mappings, list):
        raise ValueError("VIOS mapping collection is malformed")  # noqa: TRY004 — invalid external document
    rows = []
    for mapping in mappings:
        server = mapping.get("ServerAdapter") if isinstance(mapping, dict) else None
        required = (
            "AdapterName",
            "VirtualSlotNumber",
            "RemoteLogicalPartitionID",
            "RemoteSlotNumber",
        )
        if not isinstance(server, dict) or any(
            not isinstance(server.get(k), str) or not server[k] for k in required
        ):
            raise ValueError("VIOS mapping has no server identity")
        target = mapping.get("TargetDevice", {})
        targets = (
            [v for k, v in target.items() if not k.startswith("@")]
            if isinstance(target, dict)
            else [None]
        )
        if "TargetDevice" in mapping and (
            len(targets) != 1
            or not isinstance(targets[0], dict)
            or not targets[0].get("TargetName")
        ):
            raise ValueError("VIOS mapping has malformed target")
        backing = mapping.get("Storage", {})
        if "Storage" in mapping and not backing:
            raise ValueError("VIOS mapping has empty present backing")
        validate_backing(backing, target)
        client = mapping.get("AssociatedLogicalPartition", {})
        if not isinstance(client, dict):
            raise ValueError("VIOS mapping has malformed client")  # noqa: TRY004 — invalid external document
        # Preserve the full backing and client structure (including LUN/UUID identity).
        rows.append(
            {
                "vios_id": vios_id,
                "server": server["AdapterName"],
                "slot": server["VirtualSlotNumber"],
                "remote_id": server["RemoteLogicalPartitionID"],
                "remote_slot": server["RemoteSlotNumber"],
                "target": targets[0]["TargetName"] if targets else "",
                "client": json.dumps(client, sort_keys=True),
                "backing": json.dumps(
                    {"storage": backing, "target": target}, sort_keys=True
                ),
            }
        )
    return rows


def volume_rows(raw, group):
    lines = [line.split() for line in raw.splitlines() if line.strip()]
    if (
        len(lines) < 2
        or lines[0] != [f"{group}:"]
        or lines[1]
        != ["LV", "NAME", "TYPE", "LPs", "PPs", "PVs", "LV", "STATE", "MOUNT", "POINT"]
    ):
        raise ValueError("volume read has no expected group/header")
    rows = []
    for values in lines[2:]:
        if len(values) < 7 or any(
            not re.fullmatch(r"[0-9]+", value) for value in values[2:5]
        ):
            raise ValueError("volume read has malformed allocation fields")
        rows.append(
            dict(
                zip(
                    ("name", "type", "lps", "pps", "pvs", "state", "mount"),
                    [*values[:6], " ".join(values[6:])],
                    strict=True,
                )
            )
        )
    return rows


async def capture_vios(config, system, partitions, tables):
    if (
        system != system.strip()
        or is_uuid(system)
        or any(is_uuid(partition["name"]) for partition in partitions)
    ):
        raise ValueError("UUID-shaped names cannot safely select VIOS reads")
    async with HMCClient(config) as hmc:
        for partition in partitions:
            id_ = partition["lpar_id"]
            uuid = await resolve_vios_uuid(
                hmc, partition["name"], system_name_or_uuid=system
            )
            if not is_uuid(uuid):
                raise ValueError("VIOS UUID is malformed")
            tables["vios"].append({"id": id_, "name": partition["name"], "uuid": uuid})
            base = f"/rest/api/uom/VirtualIOServer/{uuid}"
            detail = await read_resources(
                hmc, base + "?group=ViosSCSIMapping", "VirtualIOServer"
            )
            tables["vios_mappings"].extend(mapping_rows(detail[0]["Resource"], id_))
            for entry in await read_resources(
                hmc, base + "/VolumeGroup", "VolumeGroup"
            ):
                group, group_uuid = (
                    entry["Resource"].get("GroupName"),
                    entry.get("UUID"),
                )
                if (
                    not isinstance(group, str)
                    or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", group)
                    or not isinstance(group_uuid, str)
                    or not is_uuid(group_uuid)
                ):
                    raise ValueError("volume group identity is malformed")
                tables["vios_groups"].append(
                    {"vios_id": id_, "uuid": group_uuid, "name": group}
                )
                command = volume_listing(system, int(id_), group)
                rows = volume_rows(await run_hmc_command(config, command), group)
                tables["vios_volumes"].extend(
                    dict(row, vios_id=id_, group=group_uuid) for row in rows
                )


async def capture(config, system, vios_ids):
    if (
        not isinstance(system, str)
        or not system.strip()
        or system != system.strip()
        or any(ord(c) < 32 for c in system)
    ):
        raise ValueError("system must be a nonblank printable name")
    if any(type(i) is not int or i <= 0 for i in vios_ids) or len(set(vios_ids)) != len(
        vios_ids
    ):
        raise ValueError("VIOS IDs must be distinct positive integers")
    tables = {name: [] for name in TABLES}
    for name, resource in (("system", "sys"), ("partitions", "lpar")):
        fields = TABLES[name][0].split(",")
        command = f"lssyscfg -r {resource} -m {shlex.quote(system)} -F {','.join(fields)} --header"
        tables[name] = parse_hmc_result_rows(
            await run_hmc_command(config, command), fields, name
        )
    selected = [
        row
        for row in tables["partitions"]
        if row["lpar_id"] in {str(i) for i in vios_ids}
    ]
    if len(selected) != len(vios_ids) or any(
        row["lpar_env"] != "vioserver" for row in selected
    ):
        raise ValueError("selected VIOS IDs are absent or not VIOS partitions")
    tables["slots"] = await list_dedicated_pcie_slot_rows(config, system)
    tables["profiles"] = await read_profile_io_slot_rows(config, system)
    tables["sriov_adapters"] = await list_sriov_adapter_rows(config, system)
    for adapter in tables["sriov_adapters"]:
        if adapter["config_state"] == "dedicated" and adapter["adapter_id"] == "null":
            continue
        tables["sriov_physical"].extend(
            await list_sriov_physical_port_rows(config, system, adapter["adapter_id"])
        )
        tables["sriov_logical"].extend(
            await list_sriov_configured_logical_port_rows(
                config, system, adapter["adapter_id"]
            )
        )
    if selected:
        await capture_vios(config, system, selected, tables)
    return validate(
        {
            "version": 1,
            "scope": {"host": config.host, "system": system, "vios_ids": vios_ids},
            "tables": tables,
        }
    )


def write_snapshot(path, document):
    data = json.dumps(validate(document), indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".baseline-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
        os.link(
            temporary, path
        )  # Exclusive publication also refuses symlink destinations.
    finally:
        Path(temporary).unlink()


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON keys")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("capture")
    read.add_argument("--system", required=True)
    read.add_argument("--output", type=Path, required=True)
    read.add_argument("--profile")
    read.add_argument("--vios-id", type=int, action="append", default=[])
    read.add_argument("--dry-run", action="store_true")
    compare = commands.add_parser("compare")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            before, after = (
                validate(json.loads(path.read_text(), object_pairs_hook=_unique_object))
                for path in (args.before, args.after)
            )
            changed = (["scope"] if before["scope"] != after["scope"] else []) + [
                name
                for name in TABLES
                if before["tables"][name] != after["tables"][name]
            ]
            print(
                "DRIFT: " + ", ".join(changed)
                if changed
                else "MATCH: independent baselines agree"
            )
            return int(bool(changed))
        if args.dry_run:
            print(
                "Read-only baseline: system, partitions, slot owners, profile io_slots, SR-IOV; "
                + (
                    "selected VIOS mappings and group volumes"
                    if args.vios_id
                    else "no VIOS access"
                )
            )
            return 0
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output exists; choose a new private snapshot path")
        document = asyncio.run(
            capture(build_config(profile=args.profile), args.system, args.vios_id)
        )
        write_snapshot(args.output, document)
        print("CAPTURED: private independent baseline")
        return 0
    except Exception:  # noqa: BLE001 — raw transport errors contain private remote identifiers
        print(
            "ERROR: baseline unreadable; check selectors, credentials, read access, input schema and a new output path. No comparison succeeded.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
