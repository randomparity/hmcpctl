"""Registered finite VIOS AME source probe; never product maturity evidence."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import os
import re
import shlex
import sys
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from hmcpctl.config import load_profile, validate_agent_id
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.native_records import build_attribute_record, build_filter
from hmcpctl.documents.partition_profile import one, parse_profile
from hmcpctl.operations.vios.core import delete_vios
from hmcpctl.ssh.description_validation import validate_lpar_description
from hmcpctl.ssh.lpar import _lpar_resource_pairs, validate_caller_token
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.xmlutil import leaf_text

from .. import vios_source_support as support
from ..packets import (
    HEX32,
    HEX40,
    HEX64,
    MIB,
    CheckpointError,
    Held,
    _attest,
    _json,
    _keys,
    _private,
    _read_private,
    _save,
    _text,
    _uuid,
    digest,
    encode,
)

GROUP = "vios-ame-source"
SNAPSHOT_REST = 68
RESOURCES: dict[str, Any] = {
    "min_memory": 1024,
    "desired_memory": 2048,
    "max_memory": 4096,
    "dedicated": False,
    "min_procs": 0.1,
    "desired_procs": 0.5,
    "max_procs": 1.0,
    "min_vcpus": 1,
    "desired_vcpus": 1,
    "max_vcpus": 2,
    "uncapped": True,
}
LIMITS = {
    "preflight-initial": (70, 1, 4 * MIB, 170),
    "preflight-final": (70, 1, 4 * MIB, 170),
    "capture": (418, 12, 20 * MIB, 1130),
    "recover": (70, 1, 4 * MIB, 170),
}


SOURCE_PATHS = (
    "scripts/live_test/arms/__init__.py",
    "scripts/live_test/packets.py",
    "scripts/live_test/vios_source_support.py",
    "scripts/live_test/arms/vios_ame_source.py",
    "scripts/live_vios_ame_source.py",
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
    "src/hmcpctl/operations/partition_creation.py",
    "src/hmcpctl/resource_identity.py",
    "src/hmcpctl/ssh/lpar.py",
    "src/hmcpctl/ssh/commands.py",
    "src/hmcpctl/documents/native_records.py",
    "src/hmcpctl/ssh/description_validation.py",
    "src/hmcpctl/ssh/transport.py",
    "src/hmcpctl/xmlutil.py",
)


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
            "cases",
            "resources",
            "baseline_sha256",
            "window",
            "budgets",
            "grant_binding",
        },
    )
    if type(p["schema_version"]) is not int or p["schema_version"] != 1:
        raise Held("unsupported source packet version")
    _text(p["configured_profile"], 128)
    h = _keys(p["hmc_identity"], {"host", "port", "user", "verify_ssl"})
    _text(h["host"])
    _text(h["user"])
    if type(h["port"]) is not int or not 1 <= h["port"] <= 65535:
        raise Held("invalid HMC port")
    if type(h["verify_ssl"]) is not bool:
        raise Held("TLS verification binding must be boolean")
    s = _keys(p["system"], {"uuid", "name"})
    if _uuid(s["uuid"]) != s["uuid"]:
        raise Held("packet system UUID must be canonical")
    _text(s["name"], 128)
    for key in ("tested_commit", "tested_tree"):
        if not isinstance(p[key], str) or not HEX40.fullmatch(p[key]):
            raise Held("exact clean Git identity required")
    hashes = _keys(p["imported_source_sha256"], set(SOURCE_PATHS))
    if any(not isinstance(v, str) or not HEX64.fullmatch(v) for v in hashes.values()):
        raise Held("source hash invalid")
    if p["baseline_sha256"] is not None and (
        not isinstance(p["baseline_sha256"], str)
        or not HEX64.fullmatch(p["baseline_sha256"])
    ):
        raise Held("baseline digest invalid")
    if encode(p["resources"]) != encode(RESOURCES):
        raise Held("resource tuple is not the fixed diagnostic tuple")
    g = _keys(p["grant_binding"], {"agent_id", "created_date", "grant_id"})
    validate_agent_id(_text(g["agent_id"], 64))
    if (
        date.fromisoformat(_text(g["created_date"], 10)).isoformat()
        != g["created_date"]
    ):
        raise Held("ownership date invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", _text(g["grant_id"], 64)):
        raise Held("grant identity invalid")
    if not isinstance(p["cases"], list) or len(p["cases"]) != 2:
        raise Held("exactly two fixed cases required")
    for c, factor in zip(p["cases"], ("0", "1.10"), strict=True):
        _keys(c, {"name", "caller_nonce", "description", "mem_expansion"})
        if not isinstance(c["caller_nonce"], str) or not HEX32.fullmatch(
            c["caller_nonce"]
        ):
            raise Held("128-bit caller nonce required")
        validate_caller_token(c["caller_nonce"])
        if (
            c["name"] != "c07v-" + c["caller_nonce"][:24]
            or c["mem_expansion"] != factor
        ):
            raise Held("case name or factor differs")
        marker = (
            f"[hmcpctl owner:{g['agent_id']} created:{g['created_date']}] "
            f"[caller {c['caller_nonce']}]"
        )
        if c["description"] != marker:
            raise Held("exact initial marker required")
        validate_lpar_description(marker)
    if len({c["name"] for c in p["cases"]}) != 2:
        raise Held("case names collide")
    w = _keys(p["window"], {"directory", "not_before", "expires_at"})
    _text(w["directory"], 4096)
    if any(type(w[k]) is not int for k in ("not_before", "expires_at")):
        raise Held("window timestamps must be integral UTC seconds")
    if not 1640 <= w["expires_at"] - w["not_before"] <= 3600:
        raise Held("window must fit the bound without being open-ended")
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
        raise Held("reviewed finite stage budgets required")


def _reserve(budget: support.Budget, cases_left: int) -> None:
    # Normal remaining cases + one observational uncertainty snapshot + logout.
    wanted = (
        cases_left * 140 + 69,
        cases_left * 5 + 1,
        cases_left * 7 * MIB + 3 * MIB,
    )
    if any(budget.maximum[i] - budget.used[i] < wanted[i] for i in range(3)):
        raise Held("remaining normal/recovery/close transport reserve unavailable")
    if (
        min(budget.deadline - time.monotonic(), budget.window - time.time())
        < cases_left * 420 + 150
    ):
        raise Held("remaining normal/recovery/close time reserve unavailable")


class CountedHMC(support.CountedHMC):
    def __init__(self, config, budget: support.Budget):
        super().__init__(config, budget)
        self.delete_uuid: str | None = None
        self.delete_sent = False
        self.create_case: str | None = None
        self.created_names: set[str] = set()
        self.delete_count = 0
        self.packet_sha = ""
        self.grant_sha = ""
        self.exclusive = False
        self.deletion_approved = False

    async def _request(self, method: str, path: str, **kwargs):
        if method == "DELETE" and path != "/rest/api/web/Logon":
            if (
                self.delete_count >= 2
                or self.delete_sent
                or not self.delete_uuid
                or path != "/rest/api/uom/VirtualIOServer/" + self.delete_uuid
            ):
                raise Held("resource DELETE lacks once-only armed UUID")
            self.delete_sent = True
            self.delete_count += 1
            return await self._counted_request(method, path, **kwargs)
        return await super()._request(method, path, **kwargs)


async def _ssh(hmc: CountedHMC, p: dict, kind: str, case: dict | None = None) -> str:
    system = shlex.quote(p["system"]["name"])
    seconds = 20
    if kind in {"profile", "description"} and case is not None and case in p["cases"]:
        pairs = [("lpar_names", case["name"])]
        if kind == "profile":
            pairs.append(("profile_names", "default_profile"))
        query = build_filter(pairs)
        level = "prof" if kind == "profile" else "lpar"
        suffix = "" if kind == "profile" else " -F description"
        command = (
            f"lssyscfg -r {level} -m {system} --filter {shlex.quote(query)}{suffix}"
        )
    elif kind == "create" and case is not None and (case in p["cases"]):
        if (
            hmc.budget.stage != "capture"
            or hmc.create_case != case["name"]
            or case["name"] in hmc.created_names
            or (len(hmc.created_names) >= 2)
        ):
            raise Held("unarmed or repeated source creation")
        hmc.create_case = None
        hmc.created_names.add(case["name"])
        seconds = 60
        record = build_attribute_record(_record_pairs(case))
        command = f"mksyscfg -r lpar -m {system} -i {shlex.quote(record)}"
    else:
        raise Held("unknown finite source command")
    return await support._counted_ssh(hmc, command, kind, seconds)


def _record_pairs(case: dict) -> list[tuple[str, object]]:
    pairs = [
        ("name", case["name"]),
        ("lpar_env", "vioserver"),
        ("profile_name", "default_profile"),
    ]
    pairs.extend(_lpar_resource_pairs(LparResources(**RESOURCES), None))
    pairs.extend(
        [
            ("description", case["description"]),
            ("mem_mode", "ded"),
            ("mem_expansion", case["mem_expansion"]),
        ]
    )
    return pairs


def _record(case: dict) -> str:
    return build_attribute_record(_record_pairs(case))


def _admission(s: dict, p: dict, *, require_digest: bool) -> None:
    stable = s["stable"]
    if (
        len(stable["vios"]) > 8
        or sum(len(v["profiles"]) for v in stable["vios"].values()) > 32
    ):
        raise Held("baseline exceeds pre-existing VIOS/profile bound")
    names = [
        leaf_text(v["fields"]["PartitionName"]["value"])
        for v in stable["vios"].values()
    ]
    names.extend(
        leaf_text(v["PartitionName"]["value"]) for v in stable["regular"].values()
    )
    if any(_text(n, 128).startswith("c07v-") for n in names):
        raise Held("stranded diagnostic name requires read-only recovery")
    support._admit_capacity(stable["system"])
    if require_digest and digest(stable) != p["baseline_sha256"]:
        raise Held("approved baseline changed")


def _observed(s: dict, identity: str, case: dict) -> dict:
    saved = s["stable"]["vios"][identity]["profiles"]
    if len(saved) != 1:
        return {
            "outcome": "unavailable",
            "reason": "unique default profile unavailable",
        }
    pid, profile = next(iter(saved.items()))
    if profile["name"] != "default_profile":
        return {"outcome": "unavailable", "reason": "default profile unavailable"}
    root = parse_profile(s["profiles"][identity][pid])
    try:

        def scalar(name):
            node = one(root, "ProfileMemory/" + name)
            if len(node) or node.text is None:
                raise ValueError("non-scalar saved value")
            return node.text.strip()

        sharing = scalar("ActiveMemorySharingEnabled")
        enabled = scalar("ActiveMemoryExpansionEnabled")
        if sharing not in {"true", "false"} or enabled not in {"true", "false"}:
            raise ValueError("non-boolean saved value")
        factor = None
        if case["mem_expansion"] != "0":
            factor = Decimal(scalar("ExpansionFactor"))
            if not factor.is_finite():
                raise ValueError("nonfinite saved factor")
        matches = sharing == "false" and enabled == (
            "false" if factor is None else "true"
        )
        if factor is not None:
            matches = matches and factor == Decimal("1.10")
        return {
            "outcome": "observed-accepted" if matches else "mismatch",
            "sharing": sharing,
            "enabled": enabled,
            "factor": str(factor) if factor is not None else None,
        }
    except (ValueError, InvalidOperation):
        return {"outcome": "unavailable", "reason": "saved AME/mode fields unavailable"}


def _grant(
    hmc: CountedHMC,
    p: dict,
    raw_sha: str,
    supplied_sha: str,
    exclusive: bool,
    deletion: bool,
) -> None:
    validate_packet(p)
    support._current(p)
    if supplied_sha != raw_sha or exclusive is not True or deletion is not True:
        raise Held("current exact source/cleanup grant required")
    actual = {k: getattr(hmc.config, k) for k in ("host", "port", "user", "verify_ssl")}
    if (
        actual != p["hmc_identity"]
        or hmc.config.agent_id != p["grant_binding"]["agent_id"]
    ):
        raise Held("configured target/agent differs")
    _attest(p)


def _new_document(p: dict, raw_sha: str, stage: str) -> dict:
    return {
        "schema_version": 1,
        "source_only": True,
        "packet_sha256": raw_sha,
        "binding_sha256": _binding(p),
        "run": {
            "group": GROUP,
            "subtasks": [],
            "tested_commit": p["tested_commit"],
            "tested_tree": p["tested_tree"],
            "tree_clean": True,
            "partial": True,
            "stage": stage,
            "finished": None,
        },
        "phase": "admitted",
        "baseline_sha256": p["baseline_sha256"],
        "snapshots": [],
        "cases": [],
        "sessions": {"logon": "unknown", "logoff": "unknown"},
        "transport": {},
        "results": [],
    }


def _checkpoint(directory: Path, document: dict, phase: str, hmc: CountedHMC) -> None:
    document["phase"] = phase
    document["transport"] = {
        "used": list(hmc.budget.used),
        "calls": list(hmc.budget.calls),
    }
    _save(directory, document)


async def preflight(hmc: CountedHMC, p: dict, directory: Path) -> dict:
    document = _new_document(p, hmc.packet_sha, hmc.budget.stage)
    observed = await support.snapshot(hmc, p)
    _admission(observed, p, require_digest=p["baseline_sha256"] is not None)
    document["snapshots"].append(observed)
    document["observed_baseline_sha256"] = digest(observed["stable"])
    document["run"].update(partial=False, finished=int(time.time()))
    _checkpoint(directory, document, "preflight-complete", hmc)
    return document


async def _delete_owned(
    hmc: CountedHMC,
    p: dict,
    case: dict,
    record: dict,
    observed: dict,
    before: dict,
    directory: Path,
    document: dict,
) -> None:
    identity = support._candidate(observed, case, before)
    if record["native"] != "acknowledged" or record["deletion"] != "not-attempted":
        raise Held("cleanup requires acknowledged once-only creation")
    if support._foreign(observed["stable"], identity) != support._baseline_foreign(
        before
    ):
        raise Held("foreign protected configuration changed")
    marker = await _ssh(hmc, p, "description", case)
    if marker.splitlines() != [case["description"]]:
        raise Held("initial native marker unconfirmed")
    route = observed["route_ids"][identity]
    current_id, current = support._entry(
        await hmc.get_uom("VirtualIOServer", route), "VirtualIOServer"
    )
    if (
        current_id != identity
        or leaf_text(current.get("PartitionName")) != case["name"]
        or leaf_text(current.get("PartitionType")) != "Virtual IO Server"
        or leaf_text(current.get("PartitionState")) != "not activated"
    ):
        raise Held("latest independent identity/state differs")
    if (
        "Description" in current
        and leaf_text(current["Description"]) != case["description"]
    ):
        raise Held("latest REST marker differs")
    _grant(hmc, p, hmc.packet_sha, hmc.grant_sha, hmc.exclusive, hmc.deletion_approved)
    record.update(
        uuid=identity,
        deletion="pending",
        marker_verified=True,
        initial_marker_readback=marker,
        identity_readback={
            "UUID": route,
            "ResourceType": "VirtualIOServer",
            "Resource": current,
        },
    )
    _checkpoint(directory, document, "delete-pending", hmc)
    hmc.delete_uuid = route
    hmc.delete_sent = False
    try:
        await delete_vios(hmc, route, system_name_or_uuid=p["system"]["uuid"])
    finally:
        hmc.delete_uuid = None
    record["deletion"] = "submitted"
    _checkpoint(directory, document, "delete-submitted", hmc)


async def capture(
    hmc: CountedHMC,
    p: dict,
    directory: Path,
    *,
    grant_packet_sha256: str,
    exclusive_writer_window: bool,
    fresh_vios_delete_approved: bool,
) -> dict:
    hmc.grant_sha = grant_packet_sha256
    hmc.exclusive = exclusive_writer_window
    hmc.deletion_approved = fresh_vios_delete_approved
    _grant(hmc, p, hmc.packet_sha, hmc.grant_sha, hmc.exclusive, hmc.deletion_approved)
    document = _new_document(p, hmc.packet_sha, "capture")
    _checkpoint(directory, document, "capture-start", hmc)
    recovery_used = False
    try:
        baseline = await support.snapshot(hmc, p)
        _admission(baseline, p, require_digest=True)
        before = baseline["stable"]
        document["snapshots"].append(baseline)
        for index, case in enumerate(p["cases"]):
            _reserve(hmc.budget, 2 - index)
            support._admit_capacity(document["snapshots"][-1]["stable"]["system"])
            _grant(
                hmc,
                p,
                hmc.packet_sha,
                hmc.grant_sha,
                hmc.exclusive,
                hmc.deletion_approved,
            )
            record = {
                "name": case["name"],
                "record": _record(case),
                "record_sha256": hashlib.sha256(_record(case).encode()).hexdigest(),
                "native": "pending",
                "outcome": "unknown",
                "deletion": "not-attempted",
                "uuid": None,
                "marker_verified": False,
            }
            document["cases"].append(record)
            _checkpoint(directory, document, "create-pending", hmc)
            with hmc.budget.section("create", 0, 1, 65536, 60):
                _reserve(hmc.budget, 2 - index)
                hmc.create_case = case["name"]
                try:
                    record["native_output"] = await _ssh(hmc, p, "create", case)
                    record["native"] = "acknowledged"
                except HMCCLIError as error:
                    record["refusal_code"] = error.remote_refusal_code
                    record["native_error"] = str(error)[:16384]
                    record["native"] = (
                        "coded-refusal" if error.remote_refusal_code else "unknown"
                    )
            _checkpoint(directory, document, "create-returned", hmc)
            observed = await support.snapshot(hmc, p)
            document["snapshots"].append(observed)
            if record["native"] == "coded-refusal" and observed["stable"] == before:
                record["outcome"] = "observed-rejected"
                _checkpoint(directory, document, "case-reconciled", hmc)
                continue
            if record["native"] != "acknowledged":
                raise Held("native creation completion unknown; no adoption")
            identity = support._candidate(observed, case, before)
            record["observed"] = _observed(observed, identity, case)
            record["outcome"] = record["observed"]["outcome"]
            with hmc.budget.section("observe-delete", 4, 2, 3 * MIB - 65536, 120):
                record["native_profile"] = await _ssh(hmc, p, "profile", case)
                await _delete_owned(
                    hmc, p, case, record, observed, before, directory, document
                )
            restored = await support.snapshot(hmc, p)
            document["snapshots"].append(restored)
            if restored["stable"] != before:
                raise Held("independent cleanup/baseline unconfirmed")
            record["deletion"] = "confirmed-absent"
            _checkpoint(directory, document, "case-reconciled", hmc)
            if record["outcome"] != "observed-accepted":
                raise Held("saved observation incomplete; no further case")
        document["run"].update(partial=False, finished=int(time.time()))
        _checkpoint(directory, document, "captured-and-reconciled", hmc)
        return document
    except CheckpointError:
        raise
    except BaseException as error:
        document["run"]["partial"] = True
        document["failure"] = type(error).__name__
        document["failed_snapshot"] = list(hmc.failed_snapshot)
        _checkpoint(directory, document, "held-residual", hmc)
        # One reserved read-only snapshot. This cannot enable cleanup or another case.
        if not recovery_used:
            recovery_used = True
            try:
                document["snapshots"].append(await support.snapshot(hmc, p))
            except (Exception, asyncio.CancelledError) as recovery_error:  # noqa: BLE001 — retain any failed reconciliation as held
                document["reconciliation_failure"] = type(recovery_error).__name__
            _checkpoint(directory, document, "held-residual", hmc)
        raise


def _history_complete(p: dict, document: dict, packet_sha: str) -> bool:
    try:
        return _history_matches(p, document, packet_sha)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return False


def _history_matches(p: dict, document: dict, packet_sha: str) -> bool:
    if (
        type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("source_only") is not True
        or document.get("packet_sha256") != packet_sha
        or document.get("phase") != "captured-and-reconciled"
        or "failure" in document
        or document.get("baseline_sha256") != p["baseline_sha256"]
        or document.get("binding_sha256") != _binding(p)
    ):
        return False
    run = document.get("run")
    if (
        not isinstance(run, dict)
        or run.get("group") != GROUP
        or run.get("partial") is not False
        or run.get("tested_commit") != p["tested_commit"]
        or run.get("tested_tree") != p["tested_tree"]
        or run.get("tree_clean") is not True
        or run.get("subtasks") != []
    ):
        return False
    cases, snapshots = document.get("cases"), document.get("snapshots")
    if (
        not isinstance(cases, list)
        or len(cases) != 2
        or not isinstance(snapshots, list)
    ):
        return False
    if not 3 <= len(snapshots) <= 5:
        return False
    before = snapshots[0]["stable"]
    if digest(before) != p["baseline_sha256"]:
        return False
    cursor = 1
    for expected, actual in zip(p["cases"], cases, strict=True):
        if not isinstance(actual, dict) or actual.get("name") != expected["name"]:
            return False
        if (
            actual.get("record") != _record(expected)
            or actual.get("record_sha256")
            != hashlib.sha256(_record(expected).encode()).hexdigest()
        ):
            return False
        observed = snapshots[cursor]
        cursor += 1
        if actual.get("native") == "coded-refusal":
            if (
                actual.get("outcome") != "observed-rejected"
                or actual.get("deletion") != "not-attempted"
                or not re.fullmatch(
                    r"HSCL[A-Z0-9]{4}", str(actual.get("refusal_code", ""))
                )
                or observed["stable"] != before
            ):
                return False
            continue
        if (
            actual.get("native") != "acknowledged"
            or actual.get("outcome") != "observed-accepted"
            or actual.get("deletion") != "confirmed-absent"
            or actual.get("marker_verified") is not True
        ):
            return False
        identity = support._candidate(observed, expected, before)
        if (
            actual.get("uuid") != identity
            or actual.get("initial_marker_readback", "").splitlines()
            != [expected["description"]]
            or support._foreign(observed["stable"], identity)
            != support._baseline_foreign(before)
            or _observed(observed, identity, expected).get("outcome")
            != "observed-accepted"
        ):
            return False
        current_id, current = support._entry(
            actual.get("identity_readback"), "VirtualIOServer"
        )
        if (
            current_id != identity
            or leaf_text(current.get("PartitionName")) != expected["name"]
            or leaf_text(current.get("PartitionState")) != "not activated"
            or leaf_text(current.get("PartitionType")) != "Virtual IO Server"
            or (
                "Description" in current
                and leaf_text(current["Description"]) != expected["description"]
            )
        ):
            return False
        if snapshots[cursor]["stable"] != before:
            return False
        cursor += 1
    return cursor == len(snapshots)


def _session_closed(closeout: dict, packet_sha: object) -> bool:
    return (
        isinstance(packet_sha, str)
        and HEX64.fullmatch(packet_sha) is not None
        and closeout.get("source_only") is True
        and closeout.get("packet_sha256") == packet_sha
        and closeout.get("closed") is True
        and closeout.get("logon") == "acknowledged"
        and closeout.get("logoff") == "acknowledged"
        and closeout.get("http") == "closed"
    )


async def recover(hmc: CountedHMC, p: dict, document: dict, directory: Path) -> dict:
    result = _new_document(p, hmc.packet_sha, "recover")
    current = await support.snapshot(hmc, p)
    result["snapshots"].append(current)
    result["baseline_agrees_now"] = digest(current["stable"]) == p["baseline_sha256"]
    result["history_complete"] = _history_complete(p, document, hmc.packet_sha)
    try:
        closeout = _read_private(
            Path(p["window"]["directory"]) / "capture" / "session.json", MIB
        )
    except (OSError, ValueError, TypeError):
        closeout = {}
    result["capture_session_closed"] = _session_closed(closeout, hmc.packet_sha)
    result["clean"] = (
        result["baseline_agrees_now"]
        and result["history_complete"]
        and result["capture_session_closed"]
    )
    result["run"].update(partial=not result["clean"], finished=int(time.time()))
    _checkpoint(directory, result, "recovery-observed", hmc)
    return result


def _binding(p: dict) -> str:
    return digest({k: v for k, v in p.items() if k != "baseline_sha256"})


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
    if (
        type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or document.get("source_only") is not True
        or document.get("binding_sha256") != _binding(p)
        or document.get("phase") != "preflight-complete"
        or document.get("observed_baseline_sha256") != p["baseline_sha256"]
        or document.get("run", {}).get("partial") is not False
        or not _session_closed(session, document.get("packet_sha256"))
    ):
        raise Held("successful bound predecessor is unavailable")
    if stage == "capture" and document.get("packet_sha256") != packet_sha:
        raise Held("final preflight used a different packet")


async def _run_stage(p: dict, args, stage: str, started: float) -> int:
    budget = support.Budget(stage, p, LIMITS[stage])
    budget.started = started
    budget.deadline = started + LIMITS[stage][3]
    config = load_profile(p["configured_profile"])
    identity = {
        key: getattr(config, key) for key in ("host", "port", "user", "verify_ssl")
    }
    if (
        identity != p["hmc_identity"]
        or config.agent_id != p["grant_binding"]["agent_id"]
    ):
        raise Held("effective configured connection or agent differs")
    # Explicit port disables HMCClient's legacy retry. No alternate destination.
    config = config.model_copy(
        update={"port": config.port, "timeout": 20, "ssh_timeout": 20}
    )
    hmc = CountedHMC(config, budget)
    hmc.packet_sha = args.packet_sha256
    closeout = {
        "source_only": True,
        "packet_sha256": args.packet_sha256,
        "logon": "not-entered",
        "logoff": "not-entered",
        "http": "open",
        "closed": False,
    }
    code = 2
    try:
        support._current(p)
        _attest(p)
        remaining = budget.deadline - time.monotonic() - 30
        if remaining <= 20:
            raise Held("stage body/close reserve unavailable")
        async with asyncio.timeout(remaining):
            closeout["logon"] = "unknown"
            with budget.section("logon", 1, 0, MIB, 20):
                await hmc.logon()
            closeout["logon"] = "acknowledged"
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
            code = 0 if result["run"]["partial"] is False else 2
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as error:  # noqa: BLE001 — close own session after every body failure
        # Privacy boundary; preserve durable resource intent and failure category only.
        closeout["body_failure"] = type(error).__name__
        closeout["own_process_pid"] = getattr(error, "own_process_pid", None)
    finally:
        if hmc.is_logged_on:
            closeout["logoff"] = "unknown"
            try:
                with budget.section("logoff", 1, 0, MIB, 20):
                    await hmc.logoff()
                closeout["logoff"] = "acknowledged"
            except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — unknown closeout must remain durable
                closeout["logoff_failure"] = type(error).__name__
        else:
            closeout["logoff"] = "no-known-token"
        try:
            remaining = min(10, budget.deadline - time.monotonic())
            if remaining <= 0:
                raise Held("local close deadline exhausted")
            async with asyncio.timeout(remaining):
                await hmc._http.aclose()
            closeout["http"] = "closed"
        except (Exception, asyncio.CancelledError) as error:  # noqa: BLE001 — unknown closeout must remain durable
            closeout["http"] = "unknown"
            closeout["close_failure"] = type(error).__name__
        closeout["closed"] = (
            closeout["logon"] == "acknowledged"
            and closeout["logoff"] == "acknowledged"
            and closeout["http"] == "closed"
        )
        closeout["transport"] = {"used": list(budget.used), "calls": list(budget.calls)}
        _save(args.output, closeout, "session.json")
    return code if closeout["closed"] else 2


def _main(argv: list[str] | None, started: float) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded source-only VIOS AME diagnostic"
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
        raise Held("packet bytes exceed bound or digest changed")
    p = _json(raw)
    validate_packet(p)
    stage = args.mode
    if args.mode == "preflight":
        stage = (
            "preflight-initial" if p["baseline_sha256"] is None else "preflight-final"
        )
    elif p["baseline_sha256"] is None:
        raise Held("capture/recovery require the bound baseline digest")
    flags = (
        args.grant_packet_sha256,
        args.exclusive_writer_window,
        args.fresh_vios_delete_approved,
    )
    if args.mode == "capture":
        if flags != (args.packet_sha256, True, True) or args.results is not None:
            raise Held("capture requires exact current source and cleanup assertions")
    elif any(flags):
        raise Held("read-only stage refuses effect assertions")
    if args.mode == "recover":
        if args.results != Path(p["window"]["directory"]) / "capture" / "results.json":
            raise Held("recovery must read this window's exact capture journal")
    elif args.results is not None:
        raise Held("results input is recovery-only")
    support._current(p)
    _attest(p)
    _predecessor(p, stage, args.packet_sha256)
    support._receipt(p, stage, args.packet_sha256, args.output)
    # Library warnings may include connection identity; discard only console diagnostics.
    # The bounded source observations/errors remain in their private journals.
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
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:  # noqa: BLE001 — redact all console failure details
        own_pid = getattr(error, "own_process_pid", None)
        suffix = f"; own child PID {own_pid} unreaped" if type(own_pid) is int else ""
        print(
            f"SOURCE ONLY: stopped ({type(error).__name__}){suffix}; private recovery required",
            file=sys.stderr,
        )
        return 2
