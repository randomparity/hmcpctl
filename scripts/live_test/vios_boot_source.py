"""Finite source-only VIOS pending-boot qualification; no product maturity claim."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import io
import os
import shlex
import sys
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from hmcpctl.config import load_profile, validate_agent_id
from hmcpctl.operations.vios.core import delete_vios
from hmcpctl.ssh.commands import build_attribute_record, build_filter
from hmcpctl.ssh.description_validation import validate_lpar_description
from hmcpctl.ssh.lpar import validate_caller_token
from hmcpctl.ssh.transport import HMCCLIError, run_hmc_command
from hmcpctl.xmlutil import leaf_text

from . import vios_ame_source as ame

GROUP = "vios-boot-source"
MIB = ame.MIB
VALUES = (
    "c09-plain",
    "c09,comma",
    "c09=equals",
    "c09'single",
    'c09"double',
    "c09\\slash",
    "c09 space",
    "c09,='\"\\ combined",
)
ENCODING_ID = "csv-minimal-two-attribute-v1"
LIMITS = {
    "preflight-initial": (70, 1, 4 * MIB, 200),
    "preflight-final": (70, 1, 4 * MIB, 200),
    "capture": (830, 40, 42 * MIB, 2600),
    "recover": (70, 1, 4 * MIB, 200),
}
SOURCE_PATHS = (
    "scripts/live_test/vios_ame_source.py",
    "scripts/live_test_preflight.py",
    "scripts/live_test_recovery.py",
    "scripts/live_test_evidence.py",
    "src/hmcpctl/config.py",
    "src/hmcpctl/client/core.py",
    "src/hmcpctl/client/client_systems.py",
    "src/hmcpctl/client/client_profiles.py",
    "src/hmcpctl/client/client_resolution.py",
    "src/hmcpctl/documents/partition_profile.py",
    "src/hmcpctl/documents/lpar.py",
    "src/hmcpctl/operations/vios/core.py",
    "src/hmcpctl/resource_identity.py",
    "src/hmcpctl/ssh/lpar.py",
    "src/hmcpctl/ssh/commands.py",
    "src/hmcpctl/ssh/description_validation.py",
    "src/hmcpctl/ssh/transport.py",
    "src/hmcpctl/xmlutil.py",
    "scripts/live_test/vios_boot_source.py",
    "scripts/live_vios_boot_source.py",
)
Held = ame.Held
CheckpointError = ame.CheckpointError
encode = ame.encode
digest = ame.digest
_json = ame._json
_keys = ame._keys
_text = ame._text
_uuid = ame._uuid
_private = ame._private
_attest = ame._attest
_read_private = ame._read_private


def validate_packet(p: dict) -> None:
    _keys(
        p,
        {
            "schema_version",
            "configured_profile",
            "hmc_identity",
            "system",
            "tested_commit",
            "tested_tree",
            "imported_source_sha256",
            "fixture",
            "resources",
            "encoding_id",
            "values",
            "baseline_sha256",
            "window",
            "budgets",
            "grant_binding",
        },
    )
    if type(p["schema_version"]) is not int or p["schema_version"] != 1:
        raise Held("unsupported packet version")
    _text(p["configured_profile"], 128)
    h = _keys(p["hmc_identity"], {"host", "port", "user", "verify_ssl"})
    _text(h["host"])
    _text(h["user"])
    if (
        type(h["port"]) is not int
        or not 1 <= h["port"] <= 65535
        or type(h["verify_ssl"]) is not bool
    ):
        raise Held("strict endpoint types required")
    s = _keys(p["system"], {"uuid", "name"})
    if _uuid(s["uuid"]) != s["uuid"]:
        raise Held("canonical system UUID required")
    _text(s["name"], 128)
    for key in ("tested_commit", "tested_tree"):
        if not isinstance(p[key], str) or not ame.HEX40.fullmatch(p[key]):
            raise Held("exact Git identity required")
    hashes = _keys(p["imported_source_sha256"], set(SOURCE_PATHS))
    if any(
        not isinstance(v, str) or not ame.HEX64.fullmatch(v) for v in hashes.values()
    ):
        raise Held("source hash invalid")
    baseline = p["baseline_sha256"]
    if baseline is not None and (
        not isinstance(baseline, str) or not ame.HEX64.fullmatch(baseline)
    ):
        raise Held("baseline digest invalid")
    if encode(p["resources"]) != encode(ame.RESOURCES):
        raise Held("fixed resources required")
    if p["encoding_id"] != ENCODING_ID or p["values"] != list(VALUES):
        raise Held("fixed candidate and ordered literals required")
    g = _keys(p["grant_binding"], {"agent_id", "created_date", "grant_id"})
    validate_agent_id(_text(g["agent_id"], 64))
    if (
        date.fromisoformat(_text(g["created_date"], 10)).isoformat()
        != g["created_date"]
    ):
        raise Held("canonical ownership date required")
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", _text(g["grant_id"], 64)):
        raise Held("grant identity invalid")
    f = _keys(p["fixture"], {"name", "caller_nonce", "description"})
    nonce = f["caller_nonce"]
    if not isinstance(nonce, str) or not ame.HEX32.fullmatch(nonce):
        raise Held("128-bit caller nonce required")
    validate_caller_token(nonce)
    marker = (
        f"[hmcpctl owner:{g['agent_id']} created:{g['created_date']}] [caller {nonce}]"
    )
    if f["name"] != "c09v-" + nonce[:24] or f["description"] != marker:
        raise Held("exact fresh fixture name and marker required")
    validate_lpar_description(marker)
    w = _keys(p["window"], {"directory", "not_before", "expires_at"})
    _text(w["directory"], 4096)
    path = Path(w["directory"])
    if not path.is_absolute() or path.resolve() != path:
        raise Held("canonical absolute window required")
    if any(type(w[k]) is not int for k in ("not_before", "expires_at")):
        raise Held("integral UTC bounds required")
    if not 3200 <= w["expires_at"] - w["not_before"] <= 7200:
        raise Held("bounded window required")
    expected = {
        "stages": {k: list(v) for k, v in LIMITS.items()},
        "rows": {
            "regular": 128,
            "vios": 8,
            "profiles": 32,
            "pools": 4,
            "with_own_vios": 9,
            "with_own_profiles": 33,
        },
    }
    if encode(p["budgets"]) != encode(expected):
        raise Held("fixed strict finite budgets required")


def encode_boot_record(partition_id: int, case_index: int) -> str:
    if type(partition_id) is not int or partition_id <= 0:
        raise Held("positive REST partition ID required")
    if type(case_index) is not int or not 0 <= case_index < 8:
        raise Held("fixed case index required")
    out = io.StringIO(newline="")
    csv.writer(
        out,
        delimiter=",",
        quotechar='"',
        quoting=csv.QUOTE_MINIMAL,
        doublequote=True,
        escapechar=None,
        lineterminator="\n",
    ).writerow([f"lpar_id={partition_id}", f"boot_string={VALUES[case_index]}"])
    return out.getvalue()[:-1]


def parse_native_header(raw: str, identity: dict) -> dict[str, str]:
    _keys(identity, {"system_uuid", "vios_uuid", "name", "partition_id", "description"})
    _uuid(identity["system_uuid"])
    _uuid(identity["vios_uuid"])
    encode_boot_record(identity["partition_id"], 0)
    if (
        not isinstance(raw, str)
        or len(raw.encode()) > 65536
        or "\ufffd" in raw
        or "[stderr]" in raw
        or any((ord(c) < 32 and c not in "\r\n") or ord(c) == 127 for c in raw)
    ):
        raise Held("native header contains invalid or oversized output")
    try:
        rows = list(csv.reader(io.StringIO(raw, newline=""), strict=True))
    except csv.Error as error:
        raise Held("malformed native CSV") from error
    required = {
        "name",
        "lpar_id",
        "lpar_env",
        "uuid",
        "description",
        "pend_boot_string",
    }
    if (
        len(rows) != 2
        or not rows[0]
        or any(not h for h in rows[0])
        or len(set(rows[0])) != len(rows[0])
        or len(rows[0]) != len(rows[1])
        or not required.issubset(rows[0])
    ):
        raise Held("complete unique header and one data row required")
    if any(ord(c) < 32 or ord(c) == 127 for row in rows for cell in row for c in cell):
        raise Held("native CSV cell contains controls")
    cells = dict(zip(rows[0], rows[1], strict=True))
    expected = {
        "name": identity["name"],
        "lpar_id": str(identity["partition_id"]),
        "lpar_env": "vioserver",
        "uuid": identity["vios_uuid"],
        "description": identity["description"],
    }
    if any(cells[k] != v for k, v in expected.items()):
        raise Held("native identity or marker differs")
    return cells


def _save(directory: Path, document: dict, name_out: str = "results.json") -> None:
    limit = MIB if name_out == "session.json" else 32 * MIB
    if len(encode(document)) > limit:
        raise CheckpointError("private checkpoint bound exceeded")
    ame._save(directory, document, name_out)


class BootSourceBudget(ame.Budget):
    def __init__(self, stage: str, packet: dict):
        self.stage = stage
        self.maximum = LIMITS[stage]
        self.used = [0, 0, 0]
        self.started = time.monotonic()
        self.deadline = self.started + self.maximum[3]
        self.window = packet["window"]["expires_at"]
        self.block = None
        self.calls = []

    # This private subclass is consumed by snapshot only; its finite reserve policy is distinct.
    def reserve(self, cursor: str, case_index: int = 0) -> None:  # ty: ignore[invalid-method-override]
        boot = cursor in {"before-boot-observations", "before-boot-write"}
        if (
            self.stage != "capture"
            or type(case_index) is not int
            or (not boot and case_index != 0)
            or (boot and not 0 <= case_index < 8)
        ):
            raise Held("invalid finite reserve cursor")
        # Remaining fixed blocks: REST, SSH, retained bytes, seconds, attestations.
        snapshot = (68, 1, 2 * MIB, 120, 0)
        detail = (1, 0, MIB, 20, 0)
        native = (0, 1, 65536, 20, 0)
        write = (0, 1, 65536, 20, 1)
        cleanup = (4, 1, 4 * MIB + 65536, 100, 1)
        close = (1, 0, MIB, 30, 0)
        uncertainty = (68, 2, 2 * MIB + 65536, 140, 0)
        case = [detail, native, write, native, snapshot]
        after = [cleanup, snapshot, uncertainty, close]
        if cursor == "before-create":
            blocks = [(0, 1, 65536, 60, 1), snapshot, native] + case * 8 + after
        elif boot:
            blocks = (
                (case if cursor == "before-boot-observations" else case[2:])
                + case * (7 - case_index)
                + after
            )
        elif cursor == "before-cleanup-observations":
            blocks = after
        elif cursor == "before-delete":
            blocks = [(1, 0, MIB, 20, 1), snapshot, uncertainty, close]
        else:
            raise Held("unknown finite reserve cursor")
        wanted = [sum(b[i] for b in blocks) for i in range(5)]
        if any(self.maximum[i] - self.used[i] < wanted[i] for i in range(3)):
            raise Held("remaining suffix/uncertainty/close reserve unavailable")
        left = min(self.deadline - time.monotonic(), self.window - time.time())
        if left < wanted[3] + 18 * wanted[4]:
            raise Held("remaining suffix/attestation/close time unavailable")


class BootSourceHMC(ame.CountedHMC):
    budget: BootSourceBudget

    def __init__(self, config, budget: BootSourceBudget):
        super().__init__(config, budget)
        self.packet: dict | None = None
        self.document: dict | None = None
        self.held = False
        self.own_process_pid: int | None = None
        self.uncertainty_native_safe = True
        self.create_sent = False
        self.boot_sent = 0
        self.native_slot: str | None = None
        self.native_consumed: set[str] = set()

    async def _request(self, method: str, path: str, **kwargs):
        if method == "DELETE" and path != "/rest/api/web/Logon":
            if (
                self.budget.stage != "capture"
                or self.held
                or self.document is None
                or self.document["phase"] != "delete-intent"
                or self.delete_count >= 1
                or self.delete_sent
                or not self.delete_uuid
                or path != "/rest/api/uom/VirtualIOServer/" + self.delete_uuid
            ):
                raise Held("resource DELETE lacks one exact current intent")
            assert self.packet is not None
            _authority(self, self.packet, "before-delete")
        return await super()._request(method, path, **kwargs)


def _authority(hmc: BootSourceHMC, p: dict, cursor: str, index: int = 0) -> None:
    validate_packet(p)
    ame._current(p)
    if (
        hmc.held
        or hmc.budget.stage != "capture"
        or hmc.grant_sha != hmc.packet_sha
        or hmc.exclusive is not True
        or hmc.deletion_approved is not True
    ):
        raise Held("current exact source/cleanup authority required")
    actual = {k: getattr(hmc.config, k) for k in ("host", "port", "user", "verify_ssl")}
    if (
        actual != p["hmc_identity"]
        or hmc.config.agent_id != p["grant_binding"]["agent_id"]
    ):
        raise Held("configured connection or agent differs")
    hmc.budget.reserve(cursor, index)
    _attest(p)
    hmc.budget.reserve(cursor, index)


async def _native(
    hmc: BootSourceHMC, p: dict, kind: str, ident: dict | None = None, index: int = 0
) -> str:
    system = shlex.quote(p["system"]["name"])
    seconds = 20
    if kind == "create":
        if (
            hmc.document is None
            or hmc.document["phase"] != "create-intent"
            or hmc.create_sent
        ):
            raise Held("create token unavailable")
        _authority(hmc, p, "before-create")
        hmc.create_sent = True
        record = build_attribute_record(
            ame._record_pairs({**p["fixture"], "mem_expansion": "0"})
        )
        command = f"mksyscfg -r lpar -m {system} -i {shlex.quote(record)}"
        seconds = 60
    elif kind == "boot" and ident is not None:
        record = encode_boot_record(ident["partition_id"], index)
        if (
            hmc.document is None
            or hmc.document["phase"] != "boot-intent"
            or hmc.boot_sent != index
            or hmc.document["cases"][-1]["record"] != record
        ):
            raise Held("ordered boot token unavailable")
        _authority(hmc, p, "before-boot-write", index)
        hmc.boot_sent += 1
        command = f"chsyscfg -r lpar -m {system} -i {shlex.quote(record)}"
    elif kind == "header" and ident is not None:
        encode_boot_record(ident["partition_id"], 0)
        if not hmc.native_slot or hmc.native_slot in hmc.native_consumed:
            raise Held("native observation slot unavailable")
        hmc.native_consumed.add(hmc.native_slot)
        hmc.native_slot = None
        query = build_filter([("lpar_ids", str(ident["partition_id"]))])
        command = (
            f"lssyscfg -r lpar -m {system} --filter {shlex.quote(query)} -F --header"
        )
    else:
        raise Held("unknown finite native command")
    seconds = hmc.budget.timeout(seconds)
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
    size = len(result.encode())
    if size > cap:
        raise Held("native response exceeds bound")
    hmc.budget.refund(cap - size)
    return result


def _binding(p: dict) -> str:
    return digest({k: v for k, v in p.items() if k != "baseline_sha256"})


def _new_document(p: dict, packet_sha: str) -> dict:
    return {
        "schema_version": 1,
        "source_only": True,
        "run": {
            "group": GROUP,
            "subtasks": [],
            "tested_commit": p["tested_commit"],
            "tested_tree": p["tested_tree"],
            "tree_clean": True,
            "imported_source_sha256": p["imported_source_sha256"],
            "started": int(time.time()),
            "finished": None,
            "partial": True,
        },
        "packet_sha256": packet_sha,
        "binding_sha256": _binding(p),
        "baseline_sha256": p["baseline_sha256"],
        "observed_baseline_sha256": None,
        "encoding_id": ENCODING_ID,
        "fixture": {**p["fixture"], "vios_uuid": None, "partition_id": None},
        "phase": "baseline-observed",
        "create": {
            "native_output": None,
            "execution": "not-attempted",
            "record_sha256": None,
            "identity": None,
            "observation": {
                "outcome": "unobserved",
                "baseline_snapshot_index": None,
                "owned_snapshot_index": None,
                "native_baseline": None,
            },
        },
        "cases": [],
        "deletion": {
            "execution": "not-attempted",
            "record_sha256": None,
            "identity": None,
            "observation": {
                "outcome": "unobserved",
                "predelete_detail": None,
                "predelete_native": None,
                "restored_snapshot_index": None,
            },
        },
        "snapshots": [],
        "failure": None,
        "transport": {"used": [0, 0, 0], "calls": []},
        "recovery": None,
    }


def _checkpoint(
    directory: Path, document: dict, phase: str, hmc: BootSourceHMC
) -> None:
    document["phase"] = phase
    document["transport"] = {
        "used": list(hmc.budget.used),
        "calls": list(hmc.budget.calls),
    }
    _save(directory, document)


def _admission(s: dict, p: dict, *, require_digest: bool) -> None:
    stable = s["stable"]
    if (
        len(stable["vios"]) > 8
        or sum(len(v["profiles"]) for v in stable["vios"].values()) > 32
    ):
        raise Held("pre-existing VIOS/profile bound exceeded")
    fields = [v["fields"] for v in stable["vios"].values()] + list(
        stable["regular"].values()
    )
    names = [_text(leaf_text(v["PartitionName"]["value"]), 128) for v in fields]
    if any(n.startswith("c09v-") for n in names):
        raise Held("stranded source fixture requires root disposition")
    ame._admit_capacity(stable["system"])
    if require_digest and digest(stable) != p["baseline_sha256"]:
        raise Held("approved B0 baseline differs")


def _resource_fields(fields: dict) -> None:
    memory = fields["PartitionMemoryConfiguration"]["value"]
    cpu = fields["PartitionProcessorConfiguration"]["value"]
    if not isinstance(memory, dict) or not isinstance(cpu, dict):
        raise Held("resource containers unavailable")
    shared = cpu.get("SharedProcessorConfiguration")
    if not isinstance(shared, dict):
        raise Held("shared processor evidence unavailable")
    checks = [
        (memory, k, str(v), True)
        for k, v in zip(
            ("MinimumMemory", "DesiredMemory", "MaximumMemory"),
            (1024, 2048, 4096),
            strict=True,
        )
    ]
    checks.extend(
        (shared, k, v, integer)
        for k, v, integer in (
            ("MinimumProcessingUnits", "0.1", False),
            ("DesiredProcessingUnits", "0.5", False),
            ("MaximumProcessingUnits", "1.0", False),
            ("MinimumVirtualProcessors", "1", True),
            ("DesiredVirtualProcessors", "1", True),
            ("MaximumVirtualProcessors", "2", True),
        )
    )
    for container, key, wanted, integer in checks:
        try:
            value = Decimal(_text(leaf_text(container.get(key)), 64))
        except InvalidOperation as error:
            raise Held("resource evidence is not numeric") from error
        if (
            not value.is_finite()
            or value != Decimal(wanted)
            or (integer and value != value.to_integral_value())
        ):
            raise Held("fixed resource tuple differs")
    if (
        leaf_text(cpu.get("HasDedicatedProcessors")) != "false"
        or leaf_text(cpu.get("SharingMode")) != "uncapped"
    ):
        raise Held("shared uncapped processor evidence required")


def _owned(s: dict, p: dict, before: dict) -> dict:
    case = {**p["fixture"], "mem_expansion": "0"}
    ident = ame._candidate(s, case, before)
    fields = s["stable"]["vios"][ident]["fields"]
    raw = _text(leaf_text(fields["PartitionID"]["value"]), 16)
    if not raw.isdecimal() or str(int(raw)) != raw or int(raw) <= 0:
        raise Held("canonical positive REST partition ID required")
    _resource_fields(fields)
    if ame._observed(s, ident, case).get("outcome") != "observed-accepted":
        raise Held("dedicated AME0 default profile unconfirmed")
    if ame._foreign(s["stable"], ident) != ame._baseline_foreign(before):
        raise Held("foreign protected B0 configuration differs")
    return {
        "system_uuid": p["system"]["uuid"],
        "vios_uuid": ident,
        "name": p["fixture"]["name"],
        "partition_id": int(raw),
        "description": p["fixture"]["description"],
    }


def _detail_identity(detail: dict, ident: dict, b1: dict) -> None:
    observed, resource = ame._entry(detail, "VirtualIOServer")
    if observed != ident["vios_uuid"]:
        raise Held("fresh REST UUID differs")
    expected = {
        "PartitionName": ident["name"],
        "PartitionID": str(ident["partition_id"]),
        "PartitionType": "Virtual IO Server",
        "PartitionState": "not activated",
    }
    if any(leaf_text(resource.get(k)) != v for k, v in expected.items()):
        raise Held("fresh REST identity/type/inactivity differs")
    if (
        "Description" in resource
        and leaf_text(resource["Description"]) != ident["description"]
    ):
        raise Held("fresh REST marker differs")
    projection = ame._projection(resource, ame.BASE_FIELDS + ame.IO_FIELDS)
    saved = b1["vios"][ident["vios_uuid"]]["fields"]
    if any(projection[k] != saved[k] for k in projection):
        raise Held("fresh REST protected detail differs")


def _witness(raw: str, ident: dict) -> dict:
    return {"raw": raw, "cells": parse_native_header(raw, ident)}


def _native_equal(witness: dict, n1: dict, pending: str) -> None:
    # Reparse the complete raw header to retain order even after canonical JSON serialization.
    a = next(csv.reader(io.StringIO(witness["raw"], newline=""), strict=True))
    b = next(csv.reader(io.StringIO(n1["raw"], newline=""), strict=True))
    cells = witness["cells"]
    base = n1["cells"]
    if a != b or {k: v for k, v in cells.items() if k != "pend_boot_string"} != {
        k: v for k, v in base.items() if k != "pend_boot_string"
    }:
        raise Held("native N1 nonpending fields or header order differ")
    if cells["pend_boot_string"] != pending:
        raise Held("pending literal differs")


async def _header(hmc: BootSourceHMC, p: dict, ident: dict, slot: str) -> dict:
    hmc.native_slot = slot
    with hmc.budget.section("native-" + slot, 0, 1, 65536, 20):
        try:
            return _witness(await _native(hmc, p, "header", ident), ident)
        except (ValueError, HMCCLIError):
            hmc.uncertainty_native_safe = False
            raise


async def _detail(hmc: BootSourceHMC, ident: dict, b1: dict) -> dict:
    with hmc.budget.section("fresh-detail", 1, 0, MIB, 20):
        detail = await hmc.get_uom("VirtualIOServer", ident["vios_uuid"])
    try:
        if detail is None:
            raise Held("fresh REST detail unavailable")
        _detail_identity(detail, ident, b1)
    except ValueError:
        hmc.uncertainty_native_safe = False
        raise
    return detail


async def _cleanup(
    hmc: BootSourceHMC,
    p: dict,
    document: dict,
    directory: Path,
    ident: dict,
    b0: dict,
    b1: dict,
    n1: dict,
    pending: str,
) -> None:
    hmc.budget.reserve("before-cleanup-observations")
    marker = await _header(hmc, p, ident, "final")
    _native_equal(marker, n1, pending)
    detail = await _detail(hmc, ident, b1)
    deletion = document["deletion"]
    deletion.update(execution="intent", identity=ident)
    deletion["observation"].update(predelete_detail=detail, predelete_native=marker)
    _checkpoint(directory, document, "delete-intent", hmc)
    hmc.delete_uuid = ident["vios_uuid"]
    try:
        with hmc.budget.section("scoped-delete", 3, 0, 3 * MIB, 60):
            await delete_vios(
                hmc, ident["vios_uuid"], system_name_or_uuid=p["system"]["uuid"]
            )
    except BaseException:
        if hmc.delete_sent:
            deletion["execution"] = "unknown"
        raise
    finally:
        hmc.delete_uuid = None
    deletion["execution"] = "acknowledged"
    _checkpoint(directory, document, "delete-intent", hmc)
    restored = await ame.snapshot(hmc, p)
    document["snapshots"].append(restored)
    if restored["stable"] != b0 or ident["vios_uuid"] in restored["stable"]["vios"]:
        raise Held("original B0 and absence not restored")
    deletion["observation"].update(
        outcome="exact", restored_snapshot_index=len(document["snapshots"]) - 1
    )
    _checkpoint(directory, document, "deleted-observed", hmc)


async def capture(
    hmc: BootSourceHMC,
    packet: dict,
    directory: Path,
    *,
    grant_packet_sha256: str,
    exclusive_writer_window: bool,
    fresh_vios_delete_approved: bool,
) -> dict:
    p = packet
    hmc.packet = p
    hmc.grant_sha = grant_packet_sha256
    hmc.exclusive = exclusive_writer_window
    hmc.deletion_approved = fresh_vios_delete_approved
    _predecessor(p, "capture", hmc.packet_sha)
    document = _new_document(p, hmc.packet_sha)
    hmc.document = document
    ident = None
    try:
        observed = await ame.snapshot(hmc, p)
        _admission(observed, p, require_digest=True)
        document["snapshots"].append(observed)
        b0 = observed["stable"]
        document["observed_baseline_sha256"] = digest(b0)
        creation = document["create"]
        creation["observation"]["baseline_snapshot_index"] = 0
        record = build_attribute_record(
            ame._record_pairs({**p["fixture"], "mem_expansion": "0"})
        )
        creation.update(
            execution="intent",
            record_sha256=hashlib.sha256(record.encode()).hexdigest(),
        )
        _checkpoint(directory, document, "create-intent", hmc)
        try:
            with hmc.budget.section("create", 0, 1, 65536, 60):
                creation["native_output"] = await _native(hmc, p, "create")
        except HMCCLIError as error:
            creation["execution"] = (
                "rejected" if error.remote_refusal_code else "unknown"
            )
            if error.remote_refusal_code:
                unchanged = await ame.snapshot(hmc, p)
                document["snapshots"].append(unchanged)
                if unchanged["stable"] != b0:
                    raise Held("refused create changed B0") from error
                creation["observation"]["outcome"] = "rejected"
            raise
        except BaseException:
            if hmc.create_sent:
                creation["execution"] = "unknown"
            raise
        creation["execution"] = "acknowledged"
        _checkpoint(directory, document, "create-intent", hmc)
        if creation["native_output"]:
            raise Held("nonempty successful create output")
        created = await ame.snapshot(hmc, p)
        document["snapshots"].append(created)
        ident = _owned(created, p, b0)
        b1 = created["stable"]
        n1 = await _header(hmc, p, ident, "baseline")
        creation["identity"] = ident
        creation["observation"].update(
            outcome="exact", owned_snapshot_index=1, native_baseline=n1
        )
        document["fixture"].update(
            vios_uuid=ident["vios_uuid"], partition_id=ident["partition_id"]
        )
        _checkpoint(directory, document, "created-observed", hmc)
        pending = n1["cells"]["pend_boot_string"]
        refused = False
        for index, literal in enumerate(VALUES):
            hmc.budget.reserve("before-boot-observations", index)
            detail = await _detail(hmc, ident, b1)
            before = await _header(hmc, p, ident, f"pre-{index}")
            _native_equal(before, n1, pending)
            record = encode_boot_record(ident["partition_id"], index)
            case = {
                "index": index,
                "literal_sha256": hashlib.sha256(literal.encode()).hexdigest(),
                "record": record,
                "record_sha256": hashlib.sha256(record.encode()).hexdigest(),
                "execution": "intent",
                "native_output": None,
                "native_readback": {"detail": detail, "before": before, "after": None},
                "readback_sha256": None,
                "snapshot_index": None,
                "observation": "unobserved",
            }
            document["cases"].append(case)
            _checkpoint(directory, document, "boot-intent", hmc)
            try:
                with hmc.budget.section("boot", 0, 1, 65536, 20):
                    case["native_output"] = await _native(hmc, p, "boot", ident, index)
            except HMCCLIError as error:
                case["execution"] = (
                    "rejected" if error.remote_refusal_code else "unknown"
                )
                if not error.remote_refusal_code:
                    raise
                refused = True
            except BaseException:
                if hmc.boot_sent > index:
                    case["execution"] = "unknown"
                raise
            else:
                case["execution"] = "acknowledged"
            _checkpoint(directory, document, "boot-intent", hmc)
            if case["native_output"]:
                raise Held("nonempty successful boot output")
            after = await _header(hmc, p, ident, f"post-{index}")
            case["native_readback"]["after"] = after
            case["readback_sha256"] = digest(case["native_readback"])
            observed = await ame.snapshot(hmc, p)
            document["snapshots"].append(observed)
            case["snapshot_index"] = len(document["snapshots"]) - 1
            _native_equal(after, n1, pending if refused else literal)
            if observed["stable"] != b1:
                raise Held("full B1 protected snapshot differs")
            case["observation"] = "rejected" if refused else "exact"
            _checkpoint(directory, document, "boot-observed", hmc)
            if refused:
                break
            pending = literal
        await _cleanup(hmc, p, document, directory, ident, b0, b1, n1, pending)
        document["run"].update(partial=refused, finished=int(time.time()))
        if refused:
            document["failure"] = {"category": "refusal", "phase": "boot-observed"}
        _checkpoint(directory, document, "deleted-observed", hmc)
    except CheckpointError:
        hmc.held = True
        raise
    except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — hold all subsequent resource effects
        hmc.held = True
        hmc.own_process_pid = getattr(error, "own_process_pid", None)
        phase = document["phase"]
        category = (
            "refusal"
            if isinstance(error, HMCCLIError) and error.remote_refusal_code
            else "observation"
        )
        document["failure"] = {"category": category, "phase": phase}
        document["run"].update(partial=True, finished=int(time.time()))
        _checkpoint(directory, document, "held", hmc)
        # This observation can describe residue but cannot clear the held latch.
        try:
            uncertain = await ame.snapshot(hmc, p)
            document["snapshots"].append(uncertain)
            _checkpoint(directory, document, "held", hmc)
            if (
                ident is not None
                and hmc.uncertainty_native_safe
                and _owned(uncertain, p, b0) == ident
            ):
                await _header(hmc, p, ident, "uncertainty")
        except CheckpointError:
            raise
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — retain held original and any own-child residue
            hmc.own_process_pid = (
                getattr(error, "own_process_pid", None) or hmc.own_process_pid
            )
            return document
        _checkpoint(directory, document, "held", hmc)
    return document


def _transport_valid(transport: object, stage: str) -> bool:
    if not isinstance(transport, dict) or set(transport) != {"used", "calls"}:
        return False
    used, calls = transport["used"], transport["calls"]
    if (
        not isinstance(used, list)
        or len(used) != 3
        or any(
            type(n) is not int or not 0 <= n <= LIMITS[stage][i]
            for i, n in enumerate(used)
        )
        or not isinstance(calls, list)
    ):
        return False
    for call in calls:
        if (
            not isinstance(call, dict)
            or set(call) != {"kind", "label"}
            or call["kind"] not in ("REST", "SSH")
            or not isinstance(call["label"], str)
        ):
            return False
    return (
        sum(c["kind"] == "REST" for c in calls) == used[0]
        and sum(c["kind"] == "SSH" for c in calls) == used[1]
    )


def _capture_trace(transport: object, identity: str) -> bool:
    if not isinstance(transport, dict) or not _transport_valid(transport, "capture"):
        return False
    calls = transport["calls"]
    if not calls or calls[0] != {"kind": "REST", "label": "PUT /rest/api/web/Logon"}:
        return False
    effects = []
    for index, call in enumerate(calls):
        label = call["label"]
        if call["kind"] == "SSH":
            if label not in {"mode", "header", "create", "boot"}:
                return False
            if label in {"create", "boot"}:
                effects.append(label)
        elif label == "DELETE /rest/api/uom/VirtualIOServer/" + identity:
            effects.append("delete")
        elif not (
            label.startswith("GET /rest/api/")
            or (index == 0 and label == "PUT /rest/api/web/Logon")
        ):
            return False
    # Positional boot calls bind the already re-derived ordered case records.
    return effects == ["create", *(["boot"] * 8), "delete"]


def _session_extends_capture(close: dict, transport: object) -> bool:
    if not isinstance(transport, dict) or not _transport_valid(transport, "capture"):
        return False
    after = close["transport"]
    return (
        after["calls"]
        == transport["calls"]
        + [{"kind": "REST", "label": "DELETE /rest/api/web/Logon"}]
        and after["used"][:2] == [transport["used"][0] + 1, transport["used"][1]]
        and 0 <= after["used"][2] - transport["used"][2] <= MIB
    )


def _session_closed(closeout: dict, packet_sha: object, stage: str) -> bool:
    return (
        isinstance(closeout, dict)
        and set(closeout)
        == {
            "schema_version",
            "source_only",
            "packet_sha256",
            "stage",
            "logon",
            "logoff",
            "http",
            "own_process_pid",
            "transport",
            "closed",
        }
        and type(closeout["schema_version"]) is int
        and closeout["schema_version"] == 1
        and closeout["source_only"] is True
        and closeout["packet_sha256"] == packet_sha
        and isinstance(packet_sha, str)
        and ame.HEX64.fullmatch(packet_sha) is not None
        and closeout["stage"] == stage
        and closeout["closed"] is True
        and closeout["logon"] == "acknowledged"
        and closeout["logoff"] == "acknowledged"
        and closeout["http"] == "closed"
        and closeout["own_process_pid"] is None
        and stage in LIMITS
        and _transport_valid(closeout["transport"], stage)
        and sum(
            c["label"] == "PUT /rest/api/web/Logon"
            for c in closeout["transport"]["calls"]
        )
        == 1
        and sum(
            c["label"] == "DELETE /rest/api/web/Logon"
            for c in closeout["transport"]["calls"]
        )
        == 1
    )


def _predecessor(p: dict, stage: str, packet_sha: str) -> None:
    previous = {
        "preflight-final": "preflight-initial",
        "capture": "preflight-final",
    }.get(stage)
    if previous is None:
        return
    directory = Path(p["window"]["directory"]) / previous
    document = _read_private(directory / "results.json", 32 * MIB)
    session = _read_private(directory / "session.json", MIB)
    run = document.get("run", {})
    expected_run = _new_document(p, packet_sha)["run"]
    if (
        not isinstance(run, dict)
        or any(
            run.get(k) != expected_run[k]
            for k in (
                "group",
                "subtasks",
                "tested_commit",
                "tested_tree",
                "tree_clean",
                "imported_source_sha256",
            )
        )
        or run.get("tree_clean") is not True
        or any(type(run.get(k)) is not int for k in ("started", "finished"))
        or run["finished"] < run["started"]
    ):
        raise Held("predecessor source identity or completion differs")
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or document.get("source_only") is not True
        or document.get("binding_sha256") != _binding(p)
        or document.get("phase") != "baseline-observed"
        or document.get("run", {}).get("partial") is not False
        or document.get("observed_baseline_sha256") != p["baseline_sha256"]
        or len(document.get("snapshots", [])) != 1
        or digest(document["snapshots"][0]["stable"]) != p["baseline_sha256"]
        or not _session_closed(session, document.get("packet_sha256"), previous)
    ):
        raise Held("complete bound predecessor and closed session required")
    if stage == "capture" and document["packet_sha256"] != packet_sha:
        raise Held("final preflight packet differs")


def _check_witness(value: dict, ident: dict) -> dict:
    _keys(value, {"raw", "cells"})
    if parse_native_header(value["raw"], ident) != value["cells"]:
        raise Held("native witness digest differs")
    return value


def _history_complete(p: dict, document: dict, packet_sha: str) -> bool:
    try:
        return _history_matches(p, document, packet_sha)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, csv.Error):
        return False


def _history_matches(p: dict, d: dict, packet_sha: str) -> bool:
    template = _new_document(p, packet_sha)
    _keys(d, set(template))
    for key in (
        "schema_version",
        "source_only",
        "packet_sha256",
        "binding_sha256",
        "baseline_sha256",
        "encoding_id",
    ):
        if type(d[key]) is not type(template[key]) or d[key] != template[key]:
            return False
    if (
        d["failure"] is not None
        or d["phase"] != "deleted-observed"
        or d["recovery"] is not None
    ):
        return False
    run = _keys(d["run"], set(template["run"]))
    for key in (
        "group",
        "subtasks",
        "tested_commit",
        "tested_tree",
        "tree_clean",
        "imported_source_sha256",
    ):
        if run[key] != template["run"][key]:
            return False
    if run["partial"] is not False or run["tree_clean"] is not True:
        return False
    if (
        any(type(run[k]) is not int for k in ("started", "finished"))
        or run["finished"] < run["started"]
    ):
        return False
    if not _transport_valid(d["transport"], "capture"):
        return False
    snapshots = d["snapshots"]
    if not isinstance(snapshots, list) or len(snapshots) != 11:
        return False
    for s in snapshots:
        _keys(s, {"stable", "profiles", "route_ids", "raw_rest"})
    b0 = snapshots[0]["stable"]
    b1 = snapshots[1]["stable"]
    if (
        digest(b0) != p["baseline_sha256"]
        or d["observed_baseline_sha256"] != p["baseline_sha256"]
    ):
        return False
    _admission(snapshots[0], p, require_digest=True)
    ident = _owned(snapshots[1], p, b0)
    if not _capture_trace(d["transport"], ident["vios_uuid"]):
        return False
    if encode(d["fixture"]) != encode(
        {
            **p["fixture"],
            "vios_uuid": ident["vios_uuid"],
            "partition_id": ident["partition_id"],
        }
    ):
        return False
    creation = _keys(d["create"], set(template["create"]))
    record = build_attribute_record(
        ame._record_pairs({**p["fixture"], "mem_expansion": "0"})
    )
    if (
        creation["execution"] != "acknowledged"
        or creation["native_output"] != ""
        or encode(creation["identity"]) != encode(ident)
        or creation["record_sha256"] != hashlib.sha256(record.encode()).hexdigest()
    ):
        return False
    co = _keys(creation["observation"], set(template["create"]["observation"]))
    if (
        co["outcome"] != "exact"
        or type(co["baseline_snapshot_index"]) is not int
        or co["baseline_snapshot_index"] != 0
        or type(co["owned_snapshot_index"]) is not int
        or co["owned_snapshot_index"] != 1
    ):
        return False
    n1 = _check_witness(co["native_baseline"], ident)
    pending = n1["cells"]["pend_boot_string"]
    if not isinstance(d["cases"], list) or len(d["cases"]) != 8:
        return False
    for i, case in enumerate(d["cases"]):
        _keys(
            case,
            {
                "index",
                "literal_sha256",
                "record",
                "record_sha256",
                "execution",
                "native_readback",
                "native_output",
                "readback_sha256",
                "snapshot_index",
                "observation",
            },
        )
        record = encode_boot_record(ident["partition_id"], i)
        if (
            type(case["index"]) is not int
            or case["index"] != i
            or case["record"] != record
            or case["literal_sha256"] != hashlib.sha256(VALUES[i].encode()).hexdigest()
            or case["record_sha256"] != hashlib.sha256(record.encode()).hexdigest()
            or case["execution"] != "acknowledged"
            or case["native_output"] != ""
            or case["observation"] != "exact"
            or type(case["snapshot_index"]) is not int
            or case["snapshot_index"] != i + 2
        ):
            return False
        readback = _keys(case["native_readback"], {"detail", "before", "after"})
        if digest(readback) != case["readback_sha256"]:
            return False
        _detail_identity(readback["detail"], ident, b1)
        _native_equal(_check_witness(readback["before"], ident), n1, pending)
        _native_equal(_check_witness(readback["after"], ident), n1, VALUES[i])
        if snapshots[i + 2]["stable"] != b1:
            return False
        pending = VALUES[i]
    deletion = _keys(d["deletion"], set(template["deletion"]))
    if (
        deletion["execution"] != "acknowledged"
        or encode(deletion["identity"]) != encode(ident)
        or deletion["record_sha256"] is not None
    ):
        return False
    do = _keys(deletion["observation"], set(template["deletion"]["observation"]))
    if (
        do["outcome"] != "exact"
        or type(do["restored_snapshot_index"]) is not int
        or do["restored_snapshot_index"] != 10
    ):
        return False
    _detail_identity(do["predelete_detail"], ident, b1)
    _native_equal(_check_witness(do["predelete_native"], ident), n1, pending)
    return snapshots[10]["stable"] == b0 and ident["vios_uuid"] not in b0["vios"]


async def preflight(hmc: BootSourceHMC, packet: dict, directory: Path) -> dict:
    p = packet
    d = _new_document(p, hmc.packet_sha)
    observed = await ame.snapshot(hmc, p)
    _admission(observed, p, require_digest=p["baseline_sha256"] is not None)
    d["snapshots"].append(observed)
    d["observed_baseline_sha256"] = digest(observed["stable"])
    d["run"].update(partial=False, finished=int(time.time()))
    _checkpoint(directory, d, "baseline-observed", hmc)
    return d


async def recover(
    hmc: BootSourceHMC, packet: dict, document: dict, directory: Path
) -> dict:
    p = packet
    d = _new_document(p, hmc.packet_sha)
    current = await ame.snapshot(hmc, p)
    d["snapshots"].append(current)
    try:
        close = _read_private(
            Path(p["window"]["directory"]) / "capture" / "session.json", MIB
        )
    except (OSError, ValueError, TypeError):
        close = {}
    names = [
        leaf_text(v["fields"]["PartitionName"]["value"])
        for v in current["stable"]["vios"].values()
    ]
    baseline = digest(current["stable"]) == p["baseline_sha256"]
    history = _history_complete(p, document, hmc.packet_sha)
    # A renamed owned UUID is residue too. Without proved identity only complete B0 proves absence.
    absent = p["fixture"]["name"] not in names and (
        document["fixture"]["vios_uuid"] not in current["stable"]["vios"]
        if history
        else baseline
    )
    closed = _session_closed(
        close, hmc.packet_sha, "capture"
    ) and _session_extends_capture(close, document.get("transport"))
    d["recovery"] = {
        "history_complete": history,
        "baseline_agrees_now": baseline,
        "fixture_absent_now": absent,
        "capture_session_closed": closed,
        "clean": history and baseline and absent and closed,
    }
    d["run"].update(partial=not d["recovery"]["clean"], finished=int(time.time()))
    _checkpoint(directory, d, "recovery-observed", hmc)
    return d


async def _run_stage(p: dict, args, stage: str, started: float) -> int:
    budget = BootSourceBudget(stage, p)
    budget.started = started
    budget.deadline = started + LIMITS[stage][3]
    config = load_profile(p["configured_profile"])
    actual = {k: getattr(config, k) for k in ("host", "port", "user", "verify_ssl")}
    if actual != p["hmc_identity"] or config.agent_id != p["grant_binding"]["agent_id"]:
        raise Held("effective configured endpoint or agent differs")
    config = config.model_copy(
        update={"port": config.port, "timeout": 20, "ssh_timeout": 20}
    )
    hmc = BootSourceHMC(config, budget)
    hmc.packet = p
    hmc.packet_sha = args.packet_sha256
    close = {
        "schema_version": 1,
        "source_only": True,
        "packet_sha256": hmc.packet_sha,
        "stage": stage,
        "logon": "not-entered",
        "logoff": "not-entered",
        "http": "open",
        "own_process_pid": None,
        "transport": {"used": [0, 0, 0], "calls": []},
        "closed": False,
    }
    code = 2
    checkpoint_failed = False

    def checkpoint():
        nonlocal checkpoint_failed
        if checkpoint_failed:
            raise CheckpointError("session checkpoint previously failed")
        close["transport"] = {"used": list(budget.used), "calls": list(budget.calls)}
        try:
            _save(args.output, close, "session.json")
        except CheckpointError:
            checkpoint_failed = True
            raise

    try:
        ame._current(p)
        remaining = (
            min(budget.deadline - time.monotonic(), budget.window - time.time()) - 30
        )
        if remaining <= 20:
            raise Held("stage body/close reserve unavailable")
        async with asyncio.timeout(remaining):
            close["logon"] = "unknown"
            checkpoint()
            with budget.section("logon", 1, 0, MIB, 20):
                await hmc.logon()
            close["logon"] = "acknowledged"
            checkpoint()
            if stage.startswith("preflight"):
                result = await preflight(hmc, p, args.output)
            elif stage == "capture":
                result = await capture(
                    hmc,
                    p,
                    args.output,
                    grant_packet_sha256=args.grant_packet_sha256,
                    exclusive_writer_window=args.exclusive_writer_window,
                    fresh_vios_delete_approved=args.fresh_vios_delete_approved,
                )
            else:
                try:
                    history = _read_private(args.results, 32 * MIB)
                except (OSError, ValueError, TypeError):
                    history = {}
                result = await recover(hmc, p, history, args.output)
            close["own_process_pid"] = hmc.own_process_pid
            code = 0 if result["run"]["partial"] is False else 2
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as error:  # noqa: BLE001 — always close only this stage's session
        code = 2
        close["own_process_pid"] = getattr(error, "own_process_pid", None)
    finally:
        if hmc.is_logged_on:
            close["logoff"] = "unknown"
            try:
                if not checkpoint_failed:
                    checkpoint()
                with budget.section("logoff", 1, 0, MIB, 20):
                    await hmc.logoff()
                close["logoff"] = "acknowledged"
                if not checkpoint_failed:
                    checkpoint()
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — retain unknown own logout
                code = 2
                close["own_process_pid"] = (
                    getattr(error, "own_process_pid", None) or close["own_process_pid"]
                )
        else:
            close["logoff"] = "no-known-token"
        try:
            remaining = min(10, budget.deadline - time.monotonic())
            if remaining <= 0:
                raise Held("local close deadline expired")
            async with asyncio.timeout(remaining):
                await hmc._http.aclose()
            close["http"] = "closed"
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 — local close never proves remote cancellation
            close["http"] = "unknown"
            code = 2
        close["closed"] = (
            close["logon"] == "acknowledged"
            and close["logoff"] == "acknowledged"
            and close["http"] == "closed"
            and close["own_process_pid"] is None
        )
        if not checkpoint_failed:
            checkpoint()
    return code if close["closed"] and not checkpoint_failed else 2


def _main(argv: list[str] | None, started: float) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded source-only VIOS boot diagnostic"
    )
    parser.add_argument("mode", choices=("preflight", "capture", "recover"))
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--packet-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--grant-packet-sha256")
    parser.add_argument("--exclusive-writer-window", action="store_true")
    parser.add_argument("--fresh-vios-delete-approved", action="store_true")
    args = parser.parse_args(argv)
    _private(args.packet, directory=False)
    with args.packet.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != args.packet_sha256:
        raise Held("raw packet digest or byte bound differs")
    p = _json(raw)
    validate_packet(p)
    stage = args.mode
    if stage == "preflight":
        stage = (
            "preflight-initial" if p["baseline_sha256"] is None else "preflight-final"
        )
    elif p["baseline_sha256"] is None:
        raise Held("bound baseline required")
    flags = (
        args.grant_packet_sha256,
        args.exclusive_writer_window,
        args.fresh_vios_delete_approved,
    )
    if args.mode == "capture":
        if flags != (args.packet_sha256, True, True) or args.results is not None:
            raise Held("exact current capture/cleanup assertions required")
    elif any(flags):
        raise Held("read-only phases refuse effect assertions")
    if args.mode == "recover":
        if args.results != Path(p["window"]["directory"]) / "capture" / "results.json":
            raise Held("exact capture journal path required")
    elif args.results is not None:
        raise Held("results input is recovery-only")
    window = Path(p["window"]["directory"])
    if window == ame._root() or ame._root() in window.parents:
        raise Held("window must be outside this source checkout")
    if len(window.parts) > 256:
        raise Held("window ancestor depth exceeds bound")
    if any((ancestor / ".git").exists() for ancestor in (window, *window.parents)):
        raise Held("window must be outside source checkouts")
    ame._current(p)
    _attest(p)
    _predecessor(p, stage, args.packet_sha256)
    ame._receipt(p, stage, args.packet_sha256, args.output)
    with (
        open(os.devnull, "w") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        code = asyncio.run(_run_stage(p, args, stage, started))
    print(
        "SOURCE ONLY: complete observations"
        if code == 0
        else "SOURCE ONLY: held; inspect private evidence"
    )
    return code


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    try:
        return _main(argv, started)
    except (Exception, KeyboardInterrupt, asyncio.CancelledError):  # noqa: BLE001 — private input/provider output must not reach console
        print("SOURCE ONLY: stopped; private recovery required", file=sys.stderr)
        return 2
