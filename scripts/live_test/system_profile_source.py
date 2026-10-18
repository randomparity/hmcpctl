"""Exact-target metadata-only system-profile source capture; never a product API."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hmcpctl.client.core import HMCClient
from hmcpctl.config import load_profile
from hmcpctl.jobs import SUCCESSFUL_JOB_STATUSES, build_job_request, canonical_job_path
from hmcpctl.resource_identity import is_uuid
from hmcpctl.ssh.transport import run_hmc_command

GROUP = "system-profile-source"
LIMIT = 5 * 1024 * 1024
OPERATIONS = ("CreateSystemProfile", "GetSystemProfiles", "DeleteSystemProfile")
JOBS = frozenset(OPERATIONS)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_packet(packet: dict) -> None:
    members = packet.get("members")
    if not isinstance(members, list) or len(members) != 2:
        raise ValueError("source capture requires exactly two reference members")
    if not re.fullmatch(
        r"hmcpctl-source-q1725-[0-9a-f]{8}", packet.get("scratch_system_profile", "")
    ):
        raise ValueError("source capture requires the reserved scratch name")
    if not is_uuid(packet.get("system", {}).get("system_uuid", "")):
        raise ValueError("source system identity is invalid")
    for member in members:
        for key in ("partition_uuid", "profile_uuid"):
            if not is_uuid(member.get(key, "")):
                raise ValueError("source reference identity is invalid")
        for key in ("partition_name", "profile_name"):
            value = member.get(key)
            if (
                not isinstance(value, str)
                or not value.strip()
                or any(c in value for c in ',="\r\n')
                or not value.isprintable()
            ):
                raise ValueError("source reference name contains record structure")
        if member.get("partition_type") != "AIX/Linux":
            raise ValueError("source reference must be a regular AIX/Linux partition")
    if (
        len({m["partition_uuid"].casefold() for m in members}) != 2
        or len({m["profile_name"] for m in members}) != 2
        or len({m["partition_name"] for m in members}) != 2
    ):
        raise ValueError(
            "source references require distinct partitions and profile names"
        )


def _system_name(packet: dict) -> str:
    tokens = shlex.split(packet["system"]["command"])
    if len(tokens) != 5 or tokens[:4] != ["lssyscfg", "-r", "sysprof", "-m"]:
        raise ValueError("source packet has no exact recorded system name")
    return tokens[-1]


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k != "Metadata"}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


async def namespace(hmc: HMCClient, packet: dict) -> list[str]:
    command = (
        f"lssyscfg -r sysprof -m {shlex.quote(_system_name(packet))} -F name --header"
    )
    raw = await run_hmc_command(hmc.config, command)
    if len(raw.encode()) > LIMIT:
        raise ValueError("source namespace exceeds capture bound")
    if raw.strip() == "No results were found.":
        return []
    try:
        rows = list(csv.reader(io.StringIO(raw), strict=True))
    except csv.Error as error:
        raise ValueError("source namespace is malformed") from error
    if (
        not rows
        or rows[0] != ["name"]
        or any(len(r) != 1 or not r[0].strip() for r in rows[1:])
    ):
        raise ValueError("source namespace has no exact name-only header/rows")
    names = [r[0] for r in rows[1:]]
    if len(names) != len(set(names)):
        raise ValueError("source namespace has duplicate names")
    return sorted(names)


def _require_context(config, packet: dict) -> None:
    if (config.host, config.port) != (
        packet["context_host"],
        packet.get("context_port", 443),
    ):
        raise ValueError("source connection differs from the pinned context")


async def snapshot(hmc: HMCClient, packet: dict) -> dict:
    """Read exact member/profile identities and namespace without any job."""
    validate_packet(packet)
    _require_context(hmc.config, packet)
    system_uuid = packet["system"]["system_uuid"]
    system = await hmc.get_managed_system(system_uuid)
    if not system or system.get("Resource", {}).get("SystemName") != _system_name(
        packet
    ):
        raise ValueError("source managed-system binding changed")
    partitions = await hmc.list_logical_partitions(system_uuid)
    observed = []
    for member in packet["members"]:
        matches = [
            p
            for p in partitions
            if str(p.get("UUID", "")).casefold() == member["partition_uuid"].casefold()
        ]
        named = [
            p
            for p in partitions
            if (p.get("Resource") or {}).get("PartitionName")
            == member["partition_name"]
        ]
        if len(matches) != 1 or len(named) != 1:
            raise ValueError("source partition is not uniquely contained")
        resource = matches[0].get("Resource") or {}
        expected = {
            "PartitionName": member["partition_name"],
            "PartitionState": member["partition_state"],
            "PartitionType": member["partition_type"],
            "Description": member["ownership"].get("description"),
        }
        if any(resource.get(k) != v for k, v in expected.items()):
            raise ValueError("source member name, power, type or ownership changed")
        profiles = await hmc.list_child(
            "LogicalPartition", member["partition_uuid"], "LogicalPartitionProfile"
        )
        if (
            any(
                not is_uuid(p.get("UUID", ""))
                or not isinstance(p.get("Resource"), dict)
                or not isinstance(p["Resource"].get("ProfileName"), str)
                or not p["Resource"]["ProfileName"]
                for p in profiles
            )
            or len({p["UUID"].casefold() for p in profiles}) != len(profiles)
            or len({p["Resource"]["ProfileName"] for p in profiles}) != len(profiles)
        ):
            raise ValueError("complete unambiguous member profile baseline is required")
        contained = [
            p
            for p in profiles
            if str(p.get("UUID", "")).casefold() == member["profile_uuid"].casefold()
            and (p.get("Resource") or {}).get("ProfileName") == member["profile_name"]
        ]
        if len(contained) != 1:
            raise ValueError(
                "source profile is not uniquely contained in its partition"
            )
        observed.append(
            {
                "partition": {
                    **expected,
                    "DefaultProfileName": resource.get("DefaultProfileName"),
                    "AssociatedPartitionProfile": _stable(
                        resource.get("AssociatedPartitionProfile")
                    ),
                },
                "profiles": sorted(
                    [
                        {"UUID": p.get("UUID"), "Resource": _stable(p.get("Resource"))}
                        for p in profiles
                    ],
                    key=lambda p: str(p["UUID"]),
                ),
            }
        )
    return {"members": observed, "namespace": await namespace(hmc, packet)}


def save(directory: Path, document: dict) -> None:
    data = json.dumps(document, indent=2).encode()
    if len(data) > LIMIT:
        raise ValueError("source journal exceeds five MiB; retain existing evidence")
    fd, name = tempfile.mkstemp(prefix=".source-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, directory / "results.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _identity(job: object, packet: dict) -> tuple[str, str]:
    if not isinstance(job, dict) or not isinstance(job.get("Resource"), dict):
        raise ValueError("source job resource is malformed")  # noqa: TRY004 — malformed external HMC data, not a caller type error
    resource = job["Resource"]
    identity = resource.get("JobID")
    if (
        not isinstance(identity, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]+", identity)
        or set(identity) == {"."}
    ):
        raise ValueError("source job lacks an unambiguous stable JobID")
    path = f"/rest/api/uom/jobs/{identity}"
    link = job.get("link")
    if link is not None:
        if (
            not isinstance(link, str)
            or not link.isprintable()
            or any(c.isspace() for c in link)
        ):
            raise ValueError("source job SELF link is malformed")
        parts = urlsplit(link)
        if (
            parts.query
            or parts.fragment
            or parts.username
            or parts.password
            or parts.scheme not in {"", "https"}
            or parts.hostname
            and parts.hostname.casefold() != packet["context_host"].casefold()
            or parts.port is not None
            and parts.port != packet.get("context_port", 443)
            or canonical_job_path(parts.path) != path
        ):
            raise ValueError(
                "source job SELF link contradicts its stable JobID/context/namespace"
            )
    return identity, path


def _successful_record(record: dict, packet: dict) -> tuple[str, str]:
    identity, path = _identity(record.get("submission"), packet)
    if (record.get("id"), record.get("href")) != (identity, path) or _identity(
        record.get("terminal"), packet
    ) != (identity, path):
        raise ValueError("source submission and terminal job identities disagree")
    resource = record["terminal"]["Resource"]
    raw = resource.get("Results", {}).get("JobParameter", [])
    raw = [raw] if isinstance(raw, dict) else raw
    if not isinstance(raw, list) or any(not isinstance(p, dict) for p in raw):
        raise ValueError("source job results are malformed")
    values = {}
    for parameter in raw:
        key = parameter.get("ParameterName")
        if not isinstance(key, str) or key in values:
            raise ValueError("source job has malformed or duplicate result parameters")
        values[key] = parameter.get("ParameterValue")
    if (
        resource.get("Status") not in SUCCESSFUL_JOB_STATUSES
        or values.get("returnCode") != "0"
        or values.get("ErrorData")
    ):
        raise ValueError(
            "source job lacks definite terminal success; manual reconciliation required"
        )
    if record["operation"] == "GetSystemProfiles" and (
        not isinstance(values.get("result"), str) or not values["result"].strip()
    ):
        raise ValueError(
            "source Get has no nonempty raw result; retain evidence for reconciliation"
        )
    return identity, path


async def _job(
    hmc: HMCClient,
    packet: dict,
    operation: str,
    parameters: dict,
    document: dict,
    directory: Path,
) -> None:
    if operation not in JOBS:
        raise ValueError("source arm permits only its three named jobs")
    request = build_job_request(operation, "ManagedSystem", parameters)
    document["pending_request"] = {
        "operation": operation,
        "sha256": hashlib.sha256(request.encode()).hexdigest(),
        "xml": request,
    }
    document["phase"] = "submitting-" + operation
    save(directory, document)
    job = await hmc.submit_job(
        f"/rest/api/uom/ManagedSystem/{packet['system']['system_uuid']}/do/{operation}",
        request,
    )
    record = {"operation": operation, "submission": job}
    document["jobs"].append(record)
    save(directory, document)
    identity, path = _identity(job, packet)
    if any(
        j.get("id") == identity or j.get("href") == path for j in document["jobs"][:-1]
    ):
        raise ValueError("source job reuses a prior effective handle")
    record.update(id=identity, href=path)
    save(directory, document)
    terminal = await hmc.wait_for_job_entry(
        identity, timeout_seconds=40, poll_interval=2, job_href=path
    )
    record["terminal"] = terminal
    save(directory, document)
    _successful_record(record, packet)
    await hmc.delete_job(identity, job_href=path)
    record["own_job_deleted"] = True
    save(directory, document)


def _revision() -> tuple[str, bool]:
    return (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            cwd=Path(__file__).resolve().parents[2],
        ).strip(),
        not bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                text=True,
                cwd=Path(__file__).resolve().parents[2],
            )
        ),
    )


async def capture(
    hmc: HMCClient,
    packet: dict,
    directory: Path,
    *,
    grant_packet_sha256: str,
    references_approved: bool,
    exclusive_writer_window: bool,
    raw_packet_sha256: str | None = None,
) -> dict:
    """Operator assertions bind the exact source request; they are not credentials."""
    validate_packet(packet)
    if (
        grant_packet_sha256 != digest(packet)
        or references_approved is not True
        or exclusive_writer_window is not True
    ):
        raise ValueError(
            "exact final source grant, reference-target approval and coordinated window required"
        )
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    revision = await asyncio.to_thread(_revision)
    document = {
        "packet_sha256": digest(packet),
        "input_packet_sha256": raw_packet_sha256,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "run": {
            "group": GROUP,
            "partial": True,
            "tested_commit": revision[0],
            "tree_clean": revision[1],
            "subtasks": [],
            "finished": None,
        },
        "jobs": [],
        "results": [],
    }
    save(directory, document)
    try:
        before = await snapshot(hmc, packet)
        document["before"] = before
        save(directory, document)
        scratch = packet["scratch_system_profile"]
        if (
            scratch.casefold() in {name.casefold() for name in before["namespace"]}
            or await snapshot(hmc, packet) != before
        ):
            raise ValueError("source scratch collision or baseline drift; no create")
        await _job(
            hmc,
            packet,
            "CreateSystemProfile",
            {
                "systemProfileName": scratch,
                "logicalPartitionNames": ",".join(
                    m["partition_name"] for m in packet["members"]
                ),
                "logicalPartitionProfileNames": ",".join(
                    m["profile_name"] for m in packet["members"]
                ),
            },
            document,
            directory,
        )
        await _job(hmc, packet, "GetSystemProfiles", {}, document, directory)
        current = await snapshot(hmc, packet)
        if current != {**before, "namespace": sorted(before["namespace"] + [scratch])}:
            raise ValueError(
                "source scratch identity, namespace or member drift; no delete"
            )
        await _job(
            hmc,
            packet,
            "DeleteSystemProfile",
            {"systemProfileName": scratch},
            document,
            directory,
        )
        if await snapshot(hmc, packet) != before:
            raise ValueError(
                "source cleanup or member baseline differs; manual reconciliation required"
            )
        document["run"].update(partial=False, finished=datetime.now(UTC).isoformat())
        document["phase"] = "captured-and-reconciled"
        save(directory, document)
        return document
    except BaseException as error:
        document["run"]["partial"] = True
        document["phase"] = "manual-reconciliation-required"
        document["failure"] = {
            "type": type(error).__name__,
            "detail": str(error)[:16384],
        }
        try:
            save(directory, document)
        except (OSError, ValueError):
            pass  # The last atomic journal remains the uncertainty/reconciliation anchor.
        raise


def _require_completed_journal(packet: dict, document: dict) -> None:
    run = document.get("run", {})
    before = document.get("before", {})
    jobs = document.get("jobs")
    if (
        document.get("packet_sha256") != digest(packet)
        or "failure" in document
        or document.get("phase") != "captured-and-reconciled"
        or run.get("partial") is not False
        or run.get("group") != GROUP
        or not isinstance(run.get("finished"), str)
        or not isinstance(before, dict)
        or set(before) != {"namespace", "members"}
        or not isinstance(jobs, list)
        or len(jobs) != len(OPERATIONS)
    ):
        raise ValueError("incomplete source journal; manual reconciliation required")
    if datetime.fromisoformat(run["finished"]).tzinfo is None:
        raise ValueError("source journal lacks an attributable completion time")
    names = before["namespace"]
    if (
        not isinstance(names, list)
        or any(not isinstance(n, str) or not n for n in names)
        or len(names) != len(set(names))
        or packet["scratch_system_profile"].casefold() in {n.casefold() for n in names}
    ):
        raise ValueError("source baseline contains scratch or malformed namespace")
    handles = []
    for record, operation in zip(jobs, OPERATIONS, strict=True):
        if (
            not isinstance(record, dict)
            or record.get("operation") != operation
            or record.get("own_job_deleted") is not True
        ):
            raise ValueError(
                "source journal has an unfinished or wrong own-job sequence"
            )
        handles.append(_successful_record(record, packet))
    if len(set(handles)) != len(handles):
        raise ValueError("source journal repeats an effective job handle")


async def recover(hmc: HMCClient, packet: dict, document: dict) -> str:
    """Independent observational witness: no submission or job deletion is reachable."""
    _require_completed_journal(packet, document)
    current = await snapshot(hmc, packet)
    if (
        packet["scratch_system_profile"].casefold()
        in {n.casefold() for n in current["namespace"]}
        or current != document["before"]
    ):
        raise ValueError("source namespace/member baseline is unreadable or differs")
    return "clean"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "capture", "recover"))
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--packet-sha256", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--grant-packet-sha256")
    parser.add_argument("--references-approved", action="store_true")
    parser.add_argument("--exclusive-writer-window", action="store_true")
    args = parser.parse_args(argv)
    raw = args.packet.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.packet_sha256:
        raise ValueError("private source packet digest changed")
    packet = json.loads(raw)
    validate_packet(packet)
    if args.mode == "capture" and (
        not args.output
        or args.grant_packet_sha256 != args.packet_sha256
        or not args.references_approved
        or not args.exclusive_writer_window
    ):
        raise ValueError(
            "capture needs output and the exact operator-approved source packet assertions"
        )
    if args.output:
        checkout = Path(__file__).resolve().parents[2]
        output = args.output.resolve()
        if output == checkout or checkout in output.parents:
            raise ValueError("raw source captures must be outside the checkout")
    if args.mode == "recover" and not args.results:
        raise ValueError("recovery requires the retained private results journal")

    async def run():
        config = load_profile(packet["configured_profile"]).model_copy(
            update={"timeout": 20, "ssh_timeout": 15}
        )
        _require_context(config, packet)
        config = config.model_copy(update={"port": config.port})
        async with HMCClient(config) as hmc:
            async with asyncio.timeout(180):
                if args.mode == "preflight":
                    observed = await snapshot(hmc, packet)
                    if packet["scratch_system_profile"].casefold() in {
                        name.casefold() for name in observed["namespace"]
                    }:
                        raise ValueError(
                            "scratch collision; source capture would not start"
                        )
                elif args.mode == "recover":
                    await recover(hmc, packet, json.loads(args.results.read_text()))
                else:
                    await capture(
                        hmc,
                        packet,
                        args.output,
                        grant_packet_sha256=digest(packet),
                        references_approved=args.references_approved,
                        exclusive_writer_window=args.exclusive_writer_window,
                        raw_packet_sha256=args.packet_sha256,
                    )

    with (
        tempfile.TemporaryFile(mode="w+") as private_diagnostics,
        contextlib.redirect_stdout(private_diagnostics),
        contextlib.redirect_stderr(private_diagnostics),
    ):
        asyncio.run(run())
    print(
        {
            "preflight": "READ-ONLY PREFLIGHT: exact references contained; scratch absent; no execution grant inferred",
            "recover": "CLEAN: independent source namespace/member witness",
            "capture": "Source capture finished; run independent read-only recovery",
        }[args.mode]
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:  # noqa: BLE001 — CLI privacy boundary: native errors must not escape
        print(
            f"SOURCE STOPPED ({type(error).__name__}): check the exact private packet/configuration and retained journal; reconcile before retry.",
            file=sys.stderr,
        )
        return 2
