"""Bounded calibration on two NEW owned fixtures, never product activation proof."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hmcpctl.client.core import HMCClient
from hmcpctl.config import load_config_document, load_profile
from hmcpctl.documents.lpar import LparResources
from hmcpctl.jobs import TERMINAL_JOB_STATUSES, build_job_request
from hmcpctl.operations.lpar.activation import submit_power_off
from hmcpctl.operations.lpar.ownership import (
    authorize_lpar_ownership_description,
    parse_lpar_ownership_caller_token,
)
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.commands import (
    build_attribute_record,
    build_filter,
    parse_hmc_delimited_rows,
)
from hmcpctl.ssh.lpar import create_lpar_via_cli, stamp_lpar_ownership
from hmcpctl.ssh.transport import run_hmc_command
from hmcpctl.xmlutil import render_mtms

from . import system_profile_source as source

GROUP = "system-profile-activation-source"
# Public integrity digest of the private immutable target packet, not a credential.
BINDING_SHA256 = "4c727c9ee2f4f9d0534b1023fdac5d3df50e5d1c40b9fa3f8307156eb492838c"  # pragma: allowlist secret
NAMES = tuple(f"hmcpctl-q1725-d647765d-{suffix}" for suffix in ("a", "b"))
BASE = ("q1725-base-a", "q1725-base-b")
WITNESS = ("q1725-witness-a", "q1725-witness-b")
SYSTEM_PROFILES = ("hmcpctl-q1725-d647765d-complete", "hmcpctl-q1725-d647765d-mixed")
PROFILE_PAIRS = (((0, WITNESS[0]), (1, WITNESS[1])), ((1, BASE[1]), (0, BASE[0])))
CREATE_PARAMETERS = tuple(
    {
        "systemProfileName": SYSTEM_PROFILES[index],
        "logicalPartitionNames": ",".join(NAMES[i] for i, _ in pairs),
        "logicalPartitionProfileNames": ",".join(p for _, p in pairs),
    }
    for index, pairs in enumerate(PROFILE_PAIRS)
)
# The complete named-arm vocabulary. Neither packet nor CLI accepts job parameters.
REQUESTS = {
    "create-complete": ("CreateSystemProfile", CREATE_PARAMETERS[0]),
    "create-mixed": ("CreateSystemProfile", CREATE_PARAMETERS[1]),
    "activate": (
        "ActivateSystemProfile",
        {
            "systemProfile": SYSTEM_PROFILES[0],
            "operation": "on",
            "test": "false",
            "continue": "false",
        },
    ),
    "negative-false": (
        "ActivateSystemProfile",
        {
            "systemProfile": SYSTEM_PROFILES[1],
            "operation": "on",
            "test": "false",
            "continue": "false",
        },
    ),
    "negative-true": (
        "ActivateSystemProfile",
        {
            "systemProfile": SYSTEM_PROFILES[1],
            "operation": "on",
            "test": "false",
            "continue": "true",
        },
    ),
}

PACKET_KEYS = {
    "binding_json",
    "grant",
    "owner",
    "caller_token",
    "negative_cases",
    "tested_commit",
    "tested_tree",
    "baseline_sha256",
}


def binding(packet: dict) -> dict:
    if not isinstance(packet, dict) or set(packet) != PACKET_KEYS:
        raise ValueError("calibration packet has an unapproved field set")
    raw = packet["binding_json"]
    if (
        not isinstance(raw, str)
        or hashlib.sha256(raw.encode()).hexdigest() != BINDING_SHA256
    ):
        raise ValueError(
            "calibration target binding differs from the restricted target"
        )
    b = json.loads(raw)
    if (
        packet["grant"] != "temporary-fixtures-20261019"
        or packet["caller_token"] != "q1725-d647765d"
        or type(packet["negative_cases"]) is not bool
        or not isinstance(packet["owner"], str)
        or not packet["owner"].strip()
        or any(
            not re.fullmatch(r"[0-9a-f]{40}", str(packet[k]))
            for k in ("tested_commit", "tested_tree")
        )
        or (
            packet["baseline_sha256"] is not None
            and not re.fullmatch(r"[0-9a-f]{64}", str(packet["baseline_sha256"]))
        )
        or not is_uuid(b.get("system_uuid", ""))
        or not _cli_name(b)
    ):
        raise ValueError(
            "calibration authority, revision or target identity is malformed"
        )
    return b


def _cli_name(b: dict) -> str:
    name = render_mtms(b["selected_inventory"]["Resource"])
    if name is None:
        raise ValueError("exact MTMS identity missing")
    return name


def configured(packet: dict):
    b = binding(packet)
    if any(k.upper() == "HMC_HOST" for k in os.environ):
        raise ValueError("ambient host override is prohibited before authentication")
    raw = load_config_document().data.get("profiles", {}).get(b["profile"], {})
    config = load_profile(b["profile"])
    if (
        (config.host, config.port) != (b["host"], b["port"])
        or (config.host, config.port, config.user)
        != (raw.get("host"), int(raw.get("port", 443)), raw.get("user"))
        or (config.agent_id or "hmcpctl") != packet["owner"]
    ):
        raise ValueError(
            "effective calibration context/owner differs before authentication"
        )
    return config.model_copy(
        update={"port": config.port, "timeout": 60, "ssh_timeout": 30}
    )


def resources() -> LparResources:
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


def profile_command(system: str, index: int) -> str:
    attributes = [
        ("name", WITNESS[index]),
        ("lpar_name", NAMES[index]),
        ("mem_mode", "ded"),
        ("min_mem", 512),
        ("desired_mem", 1024),
        ("max_mem", 1024),
        ("proc_mode", "shared"),
        ("min_procs", 1),
        ("desired_procs", 1),
        ("max_procs", 1),
        ("min_proc_units", 0.05),
        ("desired_proc_units", 0.05),
        ("max_proc_units", 0.05),
        ("sharing_mode", "cap"),
        ("boot_mode", "sms"),
    ]
    return f"mksyscfg -r prof -m {shlex.quote(system)} -i {shlex.quote(build_attribute_record(attributes))}"


def _text(value: Any) -> str:
    return str(value.get("text", "")) if isinstance(value, dict) else str(value or "")


def _source_packet(b: dict) -> dict:
    return {
        "context_host": b["host"],
        "context_port": b["port"],
        "system": {
            "system_uuid": b["system_uuid"],
            "command": "lssyscfg -r sysprof -m " + shlex.quote(_cli_name(b)),
        },
    }


async def _observe(hmc: HMCClient, packet: dict) -> dict:
    """Collect actual inventory even when recovery must report unsafe policy."""
    b = binding(packet)
    source._require_context(hmc.config, _source_packet(b))
    system = await hmc.get_uom("ManagedSystem", b["system_uuid"])
    r = (system or {}).get("Resource", {})
    original = b["selected_inventory"]["Resource"]
    if (
        (system or {}).get("UUID") != b["system_uuid"]
        or r.get("SystemName") != original["SystemName"]
        or render_mtms(r) != render_mtms(original)
        or _text(r.get("State")).casefold() != "operating"
    ):
        raise ValueError("restricted system identity/state is not proven")
    entries = await hmc.list_logical_partitions(b["system_uuid"])
    if (
        any(
            not is_uuid(e.get("UUID", ""))
            or not e.get("Resource", {}).get("PartitionName")
            for e in entries
        )
        or len({e["UUID"].casefold() for e in entries}) != len(entries)
        or len({e["Resource"]["PartitionName"] for e in entries}) != len(entries)
    ):
        raise ValueError("complete unique target partition inventory required")
    partitions = []
    for entry in entries:
        profiles = await hmc.list_child(
            "LogicalPartition", entry["UUID"], "LogicalPartitionProfile"
        )
        if (
            any(
                not is_uuid(p.get("UUID", ""))
                or not p.get("Resource", {}).get("ProfileName")
                for p in profiles
            )
            or len({p["UUID"].casefold() for p in profiles}) != len(profiles)
            or len({p["Resource"]["ProfileName"] for p in profiles}) != len(profiles)
        ):
            raise ValueError("complete unique contained profile inventory required")
        partitions.append(
            {
                "UUID": entry["UUID"],
                "Resource": source._stable(entry["Resource"]),
                "profiles": sorted(
                    [
                        {"UUID": p["UUID"], "Resource": source._stable(p["Resource"])}
                        for p in profiles
                    ],
                    key=lambda e: e["UUID"],
                ),
            }
        )
    ipl = r.get("AssociatedIPLConfiguration")
    return {
        "system_auto_off": ipl.get("PowerOffWhenLastLogicalPartitionIsShutdown")
        if isinstance(ipl, dict)
        else None,
        "partitions": sorted(partitions, key=lambda e: e["UUID"]),
        "namespace": await source.namespace(hmc, _source_packet(b)),
        "capacity": source._stable(
            {
                k: r.get(k)
                for k in (
                    "AssociatedSystemMemoryConfiguration",
                    "AssociatedSystemProcessorConfiguration",
                )
            }
        ),
    }


def _require_stay_on(observed: dict) -> None:
    if observed.get("system_auto_off") != "false":
        raise ValueError(
            "system auto-off policy is unsafe or unproven; no later effects/CLEAN"
        )


async def observe(hmc: HMCClient, packet: dict) -> dict:
    observed = await _observe(hmc, packet)
    _require_stay_on(observed)
    return observed


def _capacity(observed: dict) -> None:
    m = observed["capacity"]["AssociatedSystemMemoryConfiguration"]
    p = observed["capacity"]["AssociatedSystemProcessorConfiguration"]
    available, minimum = (
        float(p["CurrentAvailableSystemProcessorUnits"]),
        float(p["MinimumProcessorUnitsPerVirtualProcessor"]),
    )
    if (
        not all(math.isfinite(v) for v in (available, minimum))
        or minimum <= 0
        or int(m["CurrentLogicalMemoryBlockSize"]) <= 0
        or int(m["CurrentAvailableSystemMemory"]) < 2048
        or 512 % int(m["CurrentLogicalMemoryBlockSize"]) != 0
        or float(p["CurrentAvailableSystemProcessorUnits"]) < 0.10
        or float(p["MinimumProcessorUnitsPerVirtualProcessor"]) > 0.05
    ):
        raise ValueError(
            "documented capped fixture resources do not fit target capacity"
        )


async def preflight(hmc: HMCClient, packet: dict) -> dict:
    current = await observe(hmc, packet)
    if any(
        e["Resource"]["PartitionName"].casefold() in {n.casefold() for n in NAMES}
        for e in current["partitions"]
    ) or {n.casefold() for n in SYSTEM_PROFILES} & {
        n.casefold() for n in current["namespace"]
    }:
        raise ValueError("reserved fixture namespace collision; no effects")
    _capacity(current)
    if (
        packet["baseline_sha256"] is not None
        and _baseline_digest(current) != packet["baseline_sha256"]
    ):
        raise ValueError("pre-existing baseline differs from exact prepared packet")
    return current


def _baseline_digest(observed):
    return source.digest(
        {k: observed[k] for k in ("partitions", "namespace", "system_auto_off")}
    )


def _safe_profile(profile: dict, desired: int) -> None:
    r = profile["Resource"]
    m, p = r.get("ProfileMemory", {}), r.get("ProcessorAttributes", {})
    shared = p.get("SharedProcessorConfiguration", {})
    if (
        _text(r.get("AssignAllResources")).lower() != "false"
        or m.get("ActiveMemorySharingEnabled") != "false"
        or _text(r.get("AutoStart")) != "false"
        or m.get("ActiveMemoryExpansionEnabled") != "false"
        or tuple(int(m[k]) for k in ("MinimumMemory", "DesiredMemory", "MaximumMemory"))
        != (512, desired, 1024)
        or p.get("HasDedicatedProcessors") != "false"
        or p.get("SharingMode") != "capped"
        or any(
            float(shared[k]) != 0.05
            for k in (
                "MinimumProcessingUnits",
                "DesiredProcessingUnits",
                "MaximumProcessingUnits",
            )
        )
        or any(
            int(shared[k]) != 1
            for k in (
                "MinimumVirtualProcessors",
                "DesiredVirtualProcessors",
                "MaximumVirtualProcessors",
            )
        )
    ):
        raise ValueError("new profile exceeds the exact fixture resource envelope")
    io = source._stable(r.get("IOConfigurationInstance", {}))
    if any(v for k, v in io.items() if k != "MaximumVirtualIOSlots"):
        raise ValueError("new profile has unapproved IO/adapters")


def _profile_parent(profile, uuid, b):
    link = urlsplit(profile["Resource"].get("AssociatedPartition", {}).get("href", ""))
    if (
        link.path
        not in {
            f"/rest/api/uom/LogicalPartition/{uuid}",
            f"/rest/api/uom/ManagedSystem/{b['system_uuid']}/LogicalPartition/{uuid}",
        }
        or link.query
        or link.fragment
        or link.username
        or link.password
        or link.scheme not in {"", "https"}
        or link.hostname is not None
        and link.hostname != b["host"]
        or link.port is not None
        and link.port != b["port"]
    ):
        raise ValueError(
            "contained profile contradicts its current parent UUID/context"
        )


def _runtime_limits(resource):
    memory = resource.get("PartitionMemoryConfiguration", {})
    state = _text(resource.get("PartitionState")).lower()
    if state not in {"running", "not activated"}:
        raise ValueError("known fixture power state required; uncertainty STOP")
    running = state == "running"
    if running and any(key not in memory for key in ("CurrentMemory", "RuntimeMemory")):
        raise ValueError(
            "actual running fixture memory observations missing; uncertainty STOP"
        )
    for key in ("CurrentMemory", "RuntimeMemory"):
        if key in memory and not 0 <= int(_text(memory[key])) <= 1024:
            raise ValueError("actual fixture memory exceeds the fixed ceiling")
    if running:
        p = resource["PartitionProcessorConfiguration"]
        shared = p["CurrentSharedProcessorConfiguration"]
        if (
            memory.get("SharedMemoryEnabled") != "false"
            or p.get("CurrentHasDedicatedProcessors") != "false"
            or p.get("CurrentSharingMode") != "capped"
            or int(shared["AllocatedVirtualProcessors"]) != 1
            or not 0 < float(shared["CurrentProcessingUnits"]) <= 0.05
        ):
            raise ValueError("actual CPU/memory mode exceeds the fixed fixture ceiling")


def _owned(hmc: HMCClient, packet: dict, entry: dict, record: dict) -> None:
    r = entry["Resource"]
    desc = _text(r.get("Description"))
    b = binding(packet)
    owner = authorize_lpar_ownership_description(
        hmc, _cli_name(b), r["PartitionName"], desc, operation="lpar-mutation"
    )
    if (
        entry["UUID"] != record["uuid"]
        or r["PartitionName"] != record["name"]
        or owner != packet["owner"]
        or parse_lpar_ownership_caller_token(desc) != packet["caller_token"]
        or desc != record["description"]
        or r.get("PartitionType") != "AIX/Linux"
        or not re.fullmatch(r"[1-9][0-9]*", _text(r.get("PartitionID")))
        or _text(r.get("PartitionID")) != record["partition_id"]
    ):
        raise PermissionError(
            "known created UUID/name/owner/caller fingerprint required"
        )
    _runtime_limits(r)
    expected = record["profiles"]
    if source.digest(entry["profiles"]) != source.digest(expected):
        raise ValueError(
            "owned contained profiles changed since their recorded baseline"
        )
    for p in entry["profiles"]:
        name = p["Resource"]["ProfileName"]
        index = NAMES.index(record["name"])
        if name not in (BASE[index], WITNESS[index]):
            raise ValueError("foreign contained profile on a fixture")
        _profile_parent(p, entry["UUID"], b)
        _safe_profile(p, 512 if name == BASE[index] else 1024)


async def guard(hmc: HMCClient, packet: dict, document: dict) -> dict:
    current = await observe(hmc, packet)
    known = {r["uuid"]: r for r in document["fixtures"] if not r.get("deleted")}
    unrelated = [e for e in current["partitions"] if e["UUID"] not in known]
    if unrelated != document["before"]["partitions"]:
        raise ValueError("pre-existing members or unrelated namespace changed")
    if current["namespace"] != sorted(
        document["before"]["namespace"] + document["system_profiles"]
    ):
        raise ValueError("system-profile namespace changed")
    if len(current["partitions"]) != len(unrelated) + len(known):
        raise ValueError("created fixture identity is missing")
    for e in current["partitions"]:
        if e["UUID"] in known:
            _owned(hmc, packet, e, known[e["UUID"]])
    return current


def _intent(document: dict, directory: Path, stage: str, target: str) -> None:
    if document.get("pending"):
        raise ValueError("prior effect is uncertain; no subsequent dispatch")
    document["pending"] = {"stage": stage, "target": target}
    source.save(directory, document)


def _settled(document: dict, directory: Path) -> None:
    document["effects"].append(document.pop("pending"))
    source.save(directory, document)


async def _create_fixture(hmc, packet, document, directory, index):
    await guard(hmc, packet, document)
    b = binding(packet)
    system = _cli_name(b)
    _intent(document, directory, "create-fixture", NAMES[index])
    await create_lpar_via_cli(
        hmc.config,
        system,
        NAMES[index],
        resources=resources(),
        max_virtual_slots=2,
        profile_name=BASE[index],
    )
    current = await observe(hmc, packet)
    matches = [
        e
        for e in current["partitions"]
        if e["Resource"]["PartitionName"] == NAMES[index]
    ]
    if len(matches) != 1:
        raise ValueError("creation identity ambiguous; no stamping/retry/cleanup")
    entry = matches[0]
    if [p["Resource"]["ProfileName"] for p in entry["profiles"]] != [BASE[index]]:
        raise ValueError("created baseline profile is not exact")
    _profile_parent(entry["profiles"][0], entry["UUID"], b)
    _safe_profile(entry["profiles"][0], 512)
    _runtime_limits(entry["Resource"])
    partition_id = _text(entry["Resource"].get("PartitionID"))
    if not re.fullmatch(r"[1-9][0-9]*", partition_id):
        raise ValueError("created fixture lacks a positive contained PartitionID")
    record = {
        "uuid": entry["UUID"],
        "name": NAMES[index],
        "profiles": entry["profiles"],
        "partition_id": partition_id,
    }
    known_ids = {r["uuid"] for r in document["fixtures"]}
    if [
        e for e in current["partitions"] if e["UUID"] not in known_ids | {entry["UUID"]}
    ] != document["before"]["partitions"] or current["namespace"] != sorted(
        document["before"]["namespace"] + document["system_profiles"]
    ):
        raise ValueError("pre-existing state drifted; no initial stamp")
    for prior in document["fixtures"]:
        _owned(
            hmc,
            packet,
            next(e for e in current["partitions"] if e["UUID"] == prior["uuid"]),
            prior,
        )
    document["fixtures"].append(record)
    _settled(document, directory)
    # Only the known just-created UUID may receive its INITIAL ownership stamp.
    _intent(document, directory, "stamp-new-fixture", entry["UUID"])
    confirmed = await stamp_lpar_ownership(
        hmc.config,
        system,
        record["name"],
        agent_id=packet["owner"],
        caller_token=packet["caller_token"],
    )
    if confirmed is None:
        raise PermissionError(
            "initial ownership stamp unconfirmed; STOP all later writes"
        )
    fresh = await observe(hmc, packet)
    entry = next(e for e in fresh["partitions"] if e["UUID"] == record["uuid"])
    record["description"] = confirmed
    _owned(hmc, packet, entry, record)
    _settled(document, directory)
    await guard(hmc, packet, document)


async def _create_profile(hmc, packet, document, directory, index):
    await guard(hmc, packet, document)
    record = document["fixtures"][index]
    _intent(document, directory, "create-witness-profile", record["uuid"])
    await run_hmc_command(
        hmc.config, profile_command(_cli_name(binding(packet)), index)
    )
    fresh = await observe(hmc, packet)
    entry = next(e for e in fresh["partitions"] if e["UUID"] == record["uuid"])
    old = [p for p in entry["profiles"] if p["Resource"]["ProfileName"] == BASE[index]]
    new = [
        p for p in entry["profiles"] if p["Resource"]["ProfileName"] == WITNESS[index]
    ]
    if old != record["profiles"] or len(new) != 1 or len(entry["profiles"]) != 2:
        raise ValueError("witness profile creation or existing profile changed")
    _profile_parent(new[0], record["uuid"], binding(packet))
    _safe_profile(new[0], 1024)
    record["profiles"] = entry["profiles"]
    _settled(document, directory)
    await guard(hmc, packet, document)


def _values(record: dict) -> dict:
    raw = record["terminal"]["Resource"].get("Results", {}).get("JobParameter", [])
    raw = [raw] if isinstance(raw, dict) else raw
    if not isinstance(raw, list):
        raise ValueError("malformed terminal result parameters")  # noqa: TRY004 — untrusted native result shape
    values = {}
    for item in raw:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("ParameterName"), str)
            or item["ParameterName"] in values
        ):
            raise ValueError("duplicate/malformed terminal result parameter")
        values[item["ParameterName"]] = item.get("ParameterValue")
    return values


async def _finish_job(
    hmc, packet, document, directory, operation, submission, *, negative=False
):
    sp = _source_packet(binding(packet))
    record = {"operation": operation, "submission": submission}
    document["jobs"].append(record)
    source.save(directory, document)
    identity, href = source._identity(submission, sp)
    if any(
        j.get("id") == identity or j.get("href") == href for j in document["jobs"][:-1]
    ):
        raise ValueError("duplicate effective job handle; no poll/delete")
    record.update(id=identity, href=href)
    source.save(directory, document)
    record["terminal"] = await hmc.wait_for_job_entry(
        identity, timeout_seconds=120, poll_interval=2, job_href=href
    )
    source.save(directory, document)
    if source._identity(record["terminal"], sp) != (identity, href):
        raise ValueError("terminal job identity changed")
    status = record["terminal"]["Resource"].get("Status")
    if status not in TERMINAL_JOB_STATUSES:
        raise ValueError("job terminality unknown; retain coordination")
    record["values"] = _values(record)
    if status in source.SUCCESSFUL_JOB_STATUSES:
        source._successful_record(record, sp)
        await hmc.delete_job(identity, job_href=href)
        record["own_job_deleted"] = True
    elif not negative:
        raise ValueError("required stage failed; no subsequent effects")
    elif status not in {
        "FAILED_TO_START",
        "FAILED_BEFORE_COMPLETION",
        "COMPLETED_WITH_ERROR",
    }:
        raise ValueError("negative terminal outcome uncertain; no subsequent effects")
    # Failed negative jobs are retained, never silently erased or called success.
    _settled(document, directory)
    return record


async def _job(hmc, packet, document, directory, stage):
    if stage not in REQUESTS:
        raise ValueError("only fixed calibration request variants allowed")
    operation, parameters = REQUESTS[stage]
    negative = stage in {"negative-false", "negative-true"}
    await guard(hmc, packet, document)
    request = build_job_request(operation, "ManagedSystem", parameters)
    _intent(document, directory, operation, binding(packet)["system_uuid"])
    document["pending"]["request_xml"] = request
    source.save(directory, document)
    submitted = await hmc.submit_job(
        f"/rest/api/uom/ManagedSystem/{binding(packet)['system_uuid']}/do/{operation}",
        request,
    )
    return await _finish_job(
        hmc, packet, document, directory, operation, submitted, negative=negative
    )


def _inventory_rows(raw):
    if not isinstance(raw, str) or len(raw.encode()) > source.LIMIT:
        raise ValueError("unbounded system-profile inventory")
    if raw.strip() == "No results were found.":
        return []
    return parse_hmc_delimited_rows(raw, ("name", "lpar_ids", "profile_names"), ":")


def _check_inventory(rows, fresh, document, names):
    ids = [_text(e["Resource"].get("PartitionID")) for e in fresh["partitions"]]
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in ids) or len(
        set(ids)
    ) != len(ids):
        raise ValueError("unique current PartitionID binding required")
    if sorted(r["name"] for r in rows) != sorted(names) or fresh["namespace"] != sorted(
        names
    ):
        raise ValueError("whole system-profile namespace differs")
    if len({r["name"] for r in rows}) != len(rows):
        raise ValueError("duplicate system-profile row")
    for row in rows:
        index = SYSTEM_PROFILES.index(row["name"])
        expected = []
        for fixture_index, profile_name in PROFILE_PAIRS[index]:
            record = document["fixtures"][fixture_index]
            entry = next(e for e in fresh["partitions"] if e["UUID"] == record["uuid"])
            if entry["Resource"]["PartitionName"] != record["name"]:
                raise ValueError("current member name binding differs")
            profiles = [
                p
                for p in entry["profiles"]
                if p["Resource"]["ProfileName"] == profile_name
            ]
            recorded = [
                p
                for p in record["profiles"]
                if p["Resource"]["ProfileName"] == profile_name
            ]
            if len(profiles) != 1 or profiles != recorded:
                raise ValueError("contained profile binding differs")
            expected.append((_text(entry["Resource"]["PartitionID"]), profile_name))
        if (
            list(
                zip(
                    row["lpar_ids"].split(","),
                    row["profile_names"].split(","),
                    strict=True,
                )
            )
            != expected
        ):
            raise ValueError("scratch association/order changed; no subsequent effects")


async def _inventory(hmc, packet, document, directory):
    reads = document["inventory_reads"]
    if len(reads) >= 4:
        raise ValueError("finite inventory read budget exceeded")
    await guard(hmc, packet, document)
    command = (
        f"lssyscfg -r sysprof -m {shlex.quote(_cli_name(binding(packet)))} "
        "-F name:lpar_ids:profile_names --header"
    )
    raw = await run_hmc_command(hmc.config, command, include_stderr=True)
    rows = _inventory_rows(raw)
    fresh = await guard(hmc, packet, document)
    _check_inventory(rows, fresh, document, document["system_profiles"])
    reads.append(
        {"effect_count": len(document["effects"]), "raw": raw, "observed": fresh}
    )
    source.save(directory, document)
    return fresh


async def _system_profile(hmc, packet, document, directory, index):
    name = SYSTEM_PROFILES[index]
    members = PROFILE_PAIRS[index]
    await _job(
        hmc,
        packet,
        document,
        directory,
        "create-complete" if index == 0 else "create-mixed",
    )
    document["system_profiles"].append(name)
    document["system_profile_members"][name] = members
    source.save(directory, document)
    await _inventory(hmc, packet, document, directory)


async def facts(hmc, packet, document):
    current = await guard(hmc, packet, document)
    observed = []
    system = _cli_name(binding(packet))
    for rec in document["fixtures"]:
        e = next(e for e in current["partitions"] if e["UUID"] == rec["uuid"])
        command = f"lssyscfg -r lpar -m {shlex.quote(system)} --filter {shlex.quote(build_filter([('lpar_ids', _text(e['Resource']['PartitionID']))]))} -F name:lpar_id:curr_profile --header"
        raw = await run_hmc_command(hmc.config, command, include_stderr=True)
        if len(raw.encode()) > source.LIMIT:
            raise ValueError("independent CLI observation exceeds bound")
        rows = parse_hmc_delimited_rows(raw, ("name", "lpar_id", "curr_profile"), ":")
        if (
            len(rows) != 1
            or rows[0]["name"] != rec["name"]
            or rows[0]["lpar_id"] != _text(e["Resource"]["PartitionID"])
        ):
            raise ValueError("independent CLI profile fact has wrong member binding")
        observed.append(
            {"partition": e, "cli_current_profile": rows[0]["curr_profile"]}
        )
    return observed


def _validation_command(packet):
    return (
        f"chsysstate -m {shlex.quote(_cli_name(binding(packet)))} "
        f"-r sysprof -n {shlex.quote(SYSTEM_PROFILES[0])} --test"
    )


def _check_validation(packet, document, proof):
    if (
        proof.get("effect_count") != 7
        or proof.get("command") != _validation_command(packet)
        or type(proof.get("exit")) is not int
        or proof["exit"] != 0
        or not isinstance(proof.get("raw"), str)
        or len(proof["raw"].encode()) > source.LIMIT
        or "[stderr]" in proof["raw"].splitlines()
        or proof["before_facts"] != proof["after_facts"]
        or proof["before"] != proof["after"]
    ):
        raise ValueError(
            "fixed validation command proof is incomplete or contradictory"
        )
    for phase in ("before", "after"):
        observed = proof[phase]
        _check_inventory(
            _inventory_rows(document["inventory_reads"][0]["raw"]),
            observed,
            document,
            [SYSTEM_PROFILES[0]],
        )
        expected = [
            next(e for e in observed["partitions"] if e["UUID"] == r["uuid"])
            for r in document["fixtures"]
        ]
        facts = proof[phase + "_facts"]
        if (
            len(facts) != 2
            or [f["partition"] for f in facts] != expected
            or any(not isinstance(f["cli_current_profile"], str) for f in facts)
            or any(
                _text(e["Resource"].get("PartitionState")).lower() != "not activated"
                for e in expected
            )
        ):
            raise ValueError(
                "validation facts do not bind the inactive current fixtures"
            )


async def _validate(hmc, packet, document, directory):
    if document["validation_commands"]:
        raise ValueError("only one fixed validation command permitted")
    proof = {
        "effect_count": len(document["effects"]),
        "command": _validation_command(packet),
    }
    proof["before"] = await guard(hmc, packet, document)
    proof["before_facts"] = await facts(hmc, packet, document)
    if any(
        _text(f["partition"]["Resource"].get("PartitionState")).lower()
        != "not activated"
        for f in proof["before_facts"]
    ):
        raise ValueError("complete-path members must both be shut down")
    if [f["partition"] for f in proof["before_facts"]] != [
        next(e for e in proof["before"]["partitions"] if e["UUID"] == r["uuid"])
        for r in document["fixtures"]
    ]:
        raise ValueError("validation admission observations changed")
    _intent(document, directory, "validate-scratch-system-profile", SYSTEM_PROFILES[0])
    document["validation_commands"].append(proof)
    source.save(directory, document)
    proof["raw"] = await run_hmc_command(
        hmc.config, proof["command"], include_stderr=True
    )
    proof["exit"] = 0  # The existing transport raises on a nonzero native exit.
    source.save(directory, document)
    proof["after_facts"] = await facts(hmc, packet, document)
    proof["after"] = await guard(hmc, packet, document)
    _check_validation(packet, document, proof)
    _settled(document, directory)
    return proof["before_facts"]


def calibrated(before: list, after: list) -> bool:
    """Consistency of experimental facts, explicitly NOT an application classifier."""
    for index, (old, new) in enumerate(zip(before, after, strict=True)):
        a, z = old["partition"]["Resource"], new["partition"]["Resource"]
        profile = next(
            p
            for p in new["partition"]["profiles"]
            if p["Resource"]["ProfileName"] == WITNESS[index]
        )
        link = z.get("AssociatedPartitionProfile", {}).get("href", "")
        memory = z.get("PartitionMemoryConfiguration", {})
        if (
            _text(a.get("LastActivatedProfile")) == WITNESS[index]
            or _text(z.get("LastActivatedProfile")) != WITNESS[index]
            or new["cli_current_profile"] != WITNESS[index]
            or not link.endswith("/" + profile["UUID"])
            or _text(z.get("PartitionState")).lower() != "running"
            or _text(memory.get("CurrentMemory")) != "1024"
            or not 0 < int(_text(memory.get("RuntimeMemory")) or "0") <= 1024
        ):
            return False
    return len(before) == len(after) == 2


async def _stop(hmc, packet, document, directory, index):
    current = await guard(hmc, packet, document)
    record = document["fixtures"][index]
    e = next(e for e in current["partitions"] if e["UUID"] == record["uuid"])
    state = _text(e["Resource"].get("PartitionState")).lower()
    if state == "not activated":
        return
    if state != "running":
        raise ValueError("fixture power is not known terminal; no teardown writes")
    _intent(document, directory, "PowerOff", record["uuid"])
    submitted = await submit_power_off(
        hmc, record["uuid"], operation="shutdown", immediate=True, restart=False
    )
    await _finish_job(hmc, packet, document, directory, "PowerOff", submitted)
    fresh = await guard(hmc, packet, document)
    e = next(e for e in fresh["partitions"] if e["UUID"] == record["uuid"])
    if _text(e["Resource"].get("PartitionState")).lower() != "not activated":
        raise ValueError("new fixture did not reach known stopped state")


async def teardown(hmc, packet, document, directory):
    """Only known-owned, freshly checked fixtures; never an exception rollback."""
    b = binding(packet)
    for name in tuple(document["system_profiles"]):
        await _inventory(hmc, packet, document, directory)
        _intent(document, directory, "delete-scratch-system-profile", name)
        command = (
            f"rmsyscfg -r sysprof -m {shlex.quote(_cli_name(b))} -n {shlex.quote(name)}"
        )
        await run_hmc_command(hmc.config, command)
        document["system_profiles"].remove(name)
        del document["system_profile_members"][name]
        _settled(document, directory)
        await guard(hmc, packet, document)
    for i, record in enumerate(document["fixtures"]):
        await _stop(hmc, packet, document, directory, i)
        await guard(hmc, packet, document)
        _intent(document, directory, "delete-owned-fixture", record["uuid"])
        await hmc.delete_logical_partition(record["uuid"])
        record["deleted"] = True
        _settled(document, directory)
        await guard(hmc, packet, document)


async def capture(hmc, packet, directory):
    if packet["baseline_sha256"] is None:
        raise ValueError("capture requires the prepared independent baseline digest")
    before = await preflight(hmc, packet)
    document = {
        "packet_sha256": source.digest(packet),
        "before": before,
        "fixtures": [],
        "system_profiles": [],
        "system_profile_members": {},
        "jobs": [],
        "inventory_reads": [],
        "validation_commands": [],
        "effects": [],
        "run": {
            "group": GROUP,
            "partial": True,
            "tested_commit": packet["tested_commit"],
            "tree_clean": True,
            "subtasks": [],
            "finished": None,
        },
        "results": [],
    }
    source.save(directory, document)
    if await preflight(hmc, packet) != before:
        raise ValueError("preflight baseline changed before first effect")
    # This marker records the ONE actual capture attempt, not read-only preflight.
    fd = os.open(
        directory.parent / ("activation-attempt-" + source.digest(packet)),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    os.close(fd)
    try:
        for i in range(2):
            await _create_fixture(hmc, packet, document, directory, i)
            await _create_profile(hmc, packet, document, directory, i)
        await _system_profile(hmc, packet, document, directory, 0)
        initial = await _validate(hmc, packet, document, directory)
        await _job(hmc, packet, document, directory, "activate")
        after = await facts(hmc, packet, document)
        document["calibration"] = {
            "before": initial,
            "after": after,
            "consistent": calibrated(initial, after),
            "application_semantics": "UNPROVEN; calibration facts only",
        }
        source.save(directory, document)
        if not document["calibration"]["consistent"]:
            raise ValueError(
                "calibration incomplete or contradictory; uncertainty STOP before later effects"
            )
        if packet["negative_cases"]:
            await _negative_cases(hmc, packet, document, directory)
        document["teardown_before"] = await facts(hmc, packet, document)
        source.save(directory, document)
        if any(
            _text(e["partition"]["Resource"].get("PartitionState")).lower()
            not in {"running", "not activated"}
            for e in document["teardown_before"]
        ):
            raise ValueError("fixture power observation unknown; stop before teardown")
        await teardown(hmc, packet, document, directory)
        current = await observe(hmc, packet)
        if (
            current["partitions"] != before["partitions"]
            or current["namespace"] != before["namespace"]
        ):
            raise ValueError("final existing-member/namespace baseline differs")
        document["results"] = _result_rows(document)
        document["run"].update(partial=False, finished=datetime.now(UTC).isoformat())
        document["phase"] = "known-fixtures-removed"
        source.save(directory, document)
        return document
    except BaseException as error:
        document["failure"] = {
            "type": type(error).__name__,
            "detail": str(error)[:16384],
        }
        document["phase"] = "manual-reconciliation-required"
        with contextlib.suppress(OSError, ValueError):
            source.save(directory, document)
        raise


async def _negative_cases(hmc, packet, document, directory):
    await _stop(hmc, packet, document, directory, 0)
    await _system_profile(hmc, packet, document, directory, 1)
    before = await facts(hmc, packet, document)
    states = [
        _text(e["partition"]["Resource"].get("PartitionState")).lower() for e in before
    ]
    if states != ["not activated", "running"]:
        raise ValueError(
            "negative experiment lacks the exact known mixed fixture state"
        )
    document["negative_cases"] = []
    for continuation in (False, True):
        # N2 exists only if N1 independently left the EXACT same known baseline.
        if continuation and await facts(hmc, packet, document) != before:
            break
        record = await _job(
            hmc,
            packet,
            document,
            directory,
            "negative-true" if continuation else "negative-false",
        )
        observed = await facts(hmc, packet, document)
        document["negative_cases"].append(
            {
                "continue": continuation,
                "before": before,
                "after": observed,
                "job_id": record["id"],
                "partial_activation": "UNPROVEN; inspect attributable evidence",
            }
        )
        source.save(directory, document)


def _completed_effects(packet, document):
    fixtures = document["fixtures"]
    uuids = [r["uuid"] for r in fixtures]
    if (
        len(set(uuids)) != 2
        or any(not is_uuid(u) for u in uuids)
        or [r["name"] for r in fixtures] != list(NAMES)
        or any(
            u in {e["UUID"] for e in document["before"]["partitions"]} for u in uuids
        )
    ):
        raise ValueError("journal fixture identities are inconsistent")
    expected: list[tuple[str, str, str | None]] = []
    for i, uuid in enumerate(uuids):
        expected.extend(
            [
                ("create-fixture", NAMES[i], None),
                ("stamp-new-fixture", uuid, None),
                ("create-witness-profile", uuid, None),
            ]
        )
    system = binding(packet)["system_uuid"]

    def job(stage):
        operation, params = REQUESTS[stage]
        expected.append(
            (operation, system, build_job_request(operation, "ManagedSystem", params))
        )

    reads = []

    def create(index):
        job("create-complete" if index == 0 else "create-mixed")
        reads.append((len(expected), list(SYSTEM_PROFILES[: index + 1])))

    create(0)
    expected.append(("validate-scratch-system-profile", SYSTEM_PROFILES[0], None))
    job("activate")
    cases = document.get("negative_cases", [])
    consistency = calibrated(
        document["calibration"]["before"], document["calibration"]["after"]
    )
    if not consistency:
        raise ValueError("uncertain calibration cannot form a completed journal")
    if consistency is not document["calibration"]["consistent"]:
        raise ValueError("calibration facts contradict their recorded consistency")
    if bool(cases) != (packet["negative_cases"] and consistency) or len(cases) > 2:
        raise ValueError("negative case sequence contradicts the exact packet")
    if cases:
        expected.append(("PowerOff", uuids[0], None))
        create(1)
        for index, case in enumerate(cases):
            if type(case["continue"]) is not bool or case["continue"] != bool(index):
                raise ValueError("negative variants duplicated or reordered")
            job("negative-true" if index else "negative-false")
        if len(cases) == 2 and (
            cases[0]["before"] != cases[0]["after"]
            or cases[1]["before"] != cases[0]["before"]
        ):
            raise ValueError(
                "continuation experiment had no unchanged independent baseline"
            )
    remaining = list(SYSTEM_PROFILES[: 2 if cases else 1])
    for name in tuple(remaining):
        reads.append((len(expected), list(remaining)))
        remaining.remove(name)
        expected.append(("delete-scratch-system-profile", name, None))
    teardown = document["teardown_before"]
    if len(teardown) != 2:
        raise ValueError("complete pre-teardown facts missing")
    for uuid, fact in zip(uuids, teardown, strict=True):
        if fact["partition"]["UUID"] != uuid:
            raise ValueError("teardown target changed")
        state = _text(fact["partition"]["Resource"]["PartitionState"]).lower()
        if state == "running":
            expected.append(("PowerOff", uuid, None))
        elif state != "not activated":
            raise ValueError("teardown power was unknown")
        expected.append(("delete-owned-fixture", uuid, None))
    actual = [
        (e.get("stage"), e.get("target"), e.get("request_xml"))
        for e in document["effects"]
    ]
    proofs = document.get("inventory_reads", [])
    if [p.get("effect_count") for p in proofs] != [count for count, _ in reads]:
        raise ValueError("inventory read proofs missing, duplicated or reordered")
    for proof, (_, names) in zip(proofs, reads, strict=True):
        _check_inventory(
            _inventory_rows(proof["raw"]), proof["observed"], document, names
        )
    if actual != expected:
        raise ValueError("journal is not the complete finite effect sequence")
    operations = [
        stage
        for stage, _, _ in expected
        if stage
        in {
            "CreateSystemProfile",
            "ActivateSystemProfile",
            "PowerOff",
        }
    ]
    if [j.get("operation") for j in document["jobs"]] != operations:
        raise ValueError("journal jobs do not match all dispatched effects")
    negative_requests = {
        build_job_request(operation, "ManagedSystem", params)
        for stage, (operation, params) in REQUESTS.items()
        if stage in {"negative-false", "negative-true"}
    }
    job_effects = [
        e
        for e in expected
        if e[0]
        in {
            "CreateSystemProfile",
            "ActivateSystemProfile",
            "PowerOff",
        }
    ]
    negatives = [
        j
        for j, effect in zip(document["jobs"], job_effects, strict=True)
        if effect[2] in negative_requests
    ]
    if [j["id"] for j in negatives] != [case["job_id"] for case in cases]:
        raise ValueError(
            "negative job identities do not match their exact finite stages"
        )
    if len(negatives) != len(cases) or any(
        j["operation"] != "ActivateSystemProfile" for j in negatives
    ):
        raise ValueError("negative observations lost their own job identities")
    if document.get("results") != _result_rows(document):
        raise ValueError("calibration/native negative results were rewritten")
    return {j["id"] for j in negatives}


def _result_rows(document):
    rows = [
        {
            "subtask": "source-calibration",
            "tool": "live_system_profile_activation_source",
            "status": "PASS" if document["calibration"]["consistent"] else "FAIL",
        }
    ]
    for index, case in enumerate(document.get("negative_cases", []), 1):
        job = next(j for j in document["jobs"] if j["id"] == case["job_id"])
        rows.append(
            {
                "subtask": f"source-negative-{index}",
                "tool": "ActivateSystemProfile",
                "status": "PASS"
                if job["terminal"]["Resource"]["Status"]
                in source.SUCCESSFUL_JOB_STATUSES
                else "FAIL",
            }
        )
    return rows


async def recover(hmc, packet, document):
    """Observe even partial journals; CLEAN requires coherent completion AND baseline."""
    current = await _observe(hmc, packet)
    _require_stay_on(current)
    run = document.get("run", {})
    before = document.get("before", {})
    if (
        document.get("packet_sha256") != source.digest(packet)
        or document.get("phase") != "known-fixtures-removed"
        or document.get("pending")
        or "failure" in document
        or run.get("partial") is not False
        or run.get("group") != GROUP
        or run.get("tested_commit") != packet["tested_commit"]
        or run.get("tree_clean") is not True
        or not isinstance(run.get("finished"), str)
        or len(document.get("fixtures", [])) != 2
        or document.get("system_profiles") != []
        or document.get("system_profile_members") != {}
        or not all(r.get("deleted") is True for r in document["fixtures"])
    ):
        raise ValueError(
            "incomplete or contradictory calibration journal; fresh inventory retained, not CLEAN"
        )
    if datetime.fromisoformat(run["finished"]).tzinfo is None:
        raise ValueError("completion time has no timezone")
    negative_ids = _completed_effects(packet, document)
    validations = document.get("validation_commands", [])
    if len(validations) != 1:
        raise ValueError("exactly one validation command proof required")
    _check_validation(packet, document, validations[0])
    if document["calibration"]["before"] != validations[0]["before_facts"]:
        raise ValueError("validation baseline contradicts calibration baseline")
    observations = [proof["observed"] for proof in document["inventory_reads"]]
    observations.extend(validations[0][phase] for phase in ("before", "after"))
    for observed in observations:
        _require_stay_on(observed)
        known = {r["uuid"]: r for r in document["fixtures"]}
        if [e for e in observed["partitions"] if e["UUID"] not in known] != before[
            "partitions"
        ]:
            raise ValueError("inventory proof original baseline differs")
        if len(observed["partitions"]) != len(before["partitions"]) + 2:
            raise ValueError("inventory proof fixture identities missing")
        for entry in observed["partitions"]:
            if entry["UUID"] in known:
                _owned(hmc, packet, entry, known[entry["UUID"]])
    names = [e["Resource"]["PartitionName"] for e in before.get("partitions", [])]
    if (
        set(names) & set(NAMES)
        or set(before.get("namespace", [])) & set(SYSTEM_PROFILES)
        or _baseline_digest(before) != packet["baseline_sha256"]
        or current["partitions"] != before.get("partitions")
        or current["namespace"] != before.get("namespace")
    ):
        raise ValueError("fixture residue or pre-existing baseline mismatch; not CLEAN")
    sp = _source_packet(binding(packet))
    handles = []
    for record in document.get("jobs", []):
        pair = source._identity(record.get("submission"), sp)
        if pair != source._identity(record.get("terminal"), sp) or pair != (
            record.get("id"),
            record.get("href"),
        ):
            raise ValueError("journal job identity contradiction")
        status = record["terminal"]["Resource"].get("Status")
        if status not in source.SUCCESSFUL_JOB_STATUSES | {
            "FAILED_TO_START",
            "FAILED_BEFORE_COMPLETION",
            "COMPLETED_WITH_ERROR",
        }:
            raise ValueError("journal retains an uncertain job")
        if status in source.SUCCESSFUL_JOB_STATUSES:
            source._successful_record(record, sp)
            if record.get("own_job_deleted") is not True:
                raise ValueError("successful own job not cleaned")
        elif record["id"] not in negative_ids or record.get("own_job_deleted"):
            raise ValueError("required job failed or failed negative was deleted")
        handles.append(pair)
    if not handles or len(set(handles)) != len(handles):
        raise ValueError("journal jobs are missing or duplicated")
    return current


def _private_bytes(path: Path) -> bytes:
    mode = path.lstat().st_mode
    if (
        not stat.S_ISREG(mode)
        or stat.S_IMODE(mode) != 0o600
        or path.stat().st_size > source.LIMIT
    ):
        raise ValueError("private regular600 bounded input required")
    return path.read_bytes()


def _read(path: Path) -> dict:
    return json.loads(_private_bytes(path))


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "capture", "recover"))
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--packet-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--grant-packet-sha256")
    parser.add_argument("--exclusive-writer-window", action="store_true")
    args = parser.parse_args(argv)
    raw = _private_bytes(args.packet)
    packet = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != args.packet_sha256:
        raise ValueError("exact private effect packet changed")
    binding(packet)
    checkout = Path(__file__).resolve().parents[2]
    revision, clean = source._revision()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=checkout, text=True
    ).strip()
    if (revision, tree, clean) != (
        packet["tested_commit"],
        packet["tested_tree"],
        True,
    ):
        raise ValueError("exact clean deployed candidate is required")
    if args.mode == "capture" and (
        args.grant_packet_sha256 != args.packet_sha256
        or not args.exclusive_writer_window
    ):
        raise PermissionError(
            "exact prepared fixture grant and coordinated writer interval required"
        )
    if args.mode == "recover" and not args.results:
        raise ValueError("independent recovery requires retained results")
    output = args.output.resolve()
    if output == checkout or checkout in output.parents:
        raise ValueError("raw calibration output must be outside checkout")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)

    async def run():
        config = configured(
            packet
        )  # Destination/owner checked BEFORE client construction/auth.
        async with HMCClient(config) as hmc:
            async with asyncio.timeout(900 if args.mode == "capture" else 120):
                if args.mode == "preflight":
                    observed = await preflight(hmc, packet)
                    source.save(output, {"read_only": True, "observed": observed})
                elif args.mode == "recover":
                    # Observe first even when the retained journal is missing or unreadable.
                    current = await _observe(hmc, packet)
                    source.save(
                        output,
                        {
                            "read_only": True,
                            "observed": current,
                            "system_policy_safe": current.get("system_auto_off")
                            == "false",
                        },
                    )
                    document = _read(args.results)
                    await recover(hmc, packet, document)
                else:
                    document = await capture(hmc, packet, output)
                    if any(row["status"] == "FAIL" for row in document["results"]):
                        raise ValueError(
                            "native/calibration FAIL rows retained; baseline restoration does not make a passing run"
                        )

    try:
        with (
            output.joinpath("diagnostics.log").open("w") as private,
            contextlib.redirect_stdout(private),
            contextlib.redirect_stderr(private),
        ):
            os.chmod(output / "diagnostics.log", 0o600)
            asyncio.run(run())
    except BaseException as error:
        if not (output / "results.json").exists():
            source.save(
                output,
                {"cli_failure": type(error).__name__, "detail": str(error)[:16384]},
            )
        raise
    print(
        {
            "preflight": "READ-ONLY fixture preflight completed; no effect authority inferred",
            "capture": "Calibration facts captured; application semantics remain unproven; run independent recovery",
            "recover": "CLEAN: fresh fixture/member baseline; original run verdict unchanged",
        }[args.mode]
    )
    return 0


def main(argv=None):
    try:
        return _main(argv)
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:  # noqa: BLE001 — CLI privacy boundary
        print(
            f"CALIBRATION STOPPED ({type(error).__name__}): inspect private packet/journal and reconcile; no automatic retry.",
            file=sys.stderr,
        )
        return 2
