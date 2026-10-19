"""System-profile membership and lifecycle; fixed routes and whole-member admission."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import Literal
from urllib.parse import urlsplit

import asyncssh
from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

from ...client.core import HMCClient
from ...errors import HMCError
from ...jobs import JobOutcome, validate_wait_timing
from ...resource_identity import is_uuid, resolve_system_uuid
from ...ssh.commands import parse_hmc_delimited_rows
from ...ssh.profiles import list_system_profile_records
from ...ssh.transport import run_hmc_command
from ...xmlutil import console_version, leaf_text, render_mtms

LIMIT = 5 * 1024 * 1024
CommandStatus = Literal["completed", "failed", "uncertain"]
Action = Literal["validate", "activate", "validate-and-activate"]


def _text(value: object) -> str:
    text = leaf_text(value)
    if text is None:
        return ""
    if not isinstance(text, str):
        raise TypeError("native system-profile field must be text")
    return text


def _name(value: str) -> str:
    if (
        len(value.encode()) > LIMIT
        or not value.strip()
        or any(c in value for c in '\x00\n\r,:"\\')
    ):
        raise ValueError(
            "nonblank, unambiguously encodable system-profile/member name required"
        )
    return value


class SystemProfilePair(BaseModel):
    """A contained partition/profile selector pair, never connection configuration."""

    model_config = ConfigDict(extra="forbid")
    partition: StrictStr
    profile: StrictStr

    @field_validator("partition", "profile")
    @classmethod
    def encodable(cls, value: str) -> str:
        return _name(value)


@dataclass(frozen=True)
class SystemProfileMember:
    resource_type: str
    partition_uuid: str
    partition_id: int
    partition_name: str
    profile_uuid: str
    profile_name: str


@dataclass(frozen=True)
class SystemProfileRecord:
    name: str
    members: tuple[SystemProfileMember, ...]


@dataclass(frozen=True)
class SystemProfileInventory:
    system_uuid: str
    records: tuple[SystemProfileRecord, ...] | None
    source: Literal["ssh-command"]
    job: JobOutcome | None
    command_status: CommandStatus
    error: str | None


@dataclass(frozen=True)
class MemberObservation:
    member: SystemProfileMember
    before_state: str | None
    after_state: str | None
    observed_profile_name: str | None
    observed_profile_uuid: str | None
    classification: Literal["changed", "unchanged", "unavailable"]
    error: str | None


@dataclass(frozen=True)
class SystemProfileOutcome:
    system_uuid: str
    name: str
    action: Literal[
        "create", "update", "delete", "validate", "activate", "validate-and-activate"
    ]
    backend: Literal["rest-job", "ssh-command"]
    state: CommandStatus
    job: JobOutcome | None
    return_code: str | None
    command_status: CommandStatus | None
    activation_execution: Literal["complete", "partial", "failed", "unknown"] | None
    postcondition: Literal["verified", "drifted", "unverified", "not-applicable"]
    members: tuple[MemberObservation, ...]
    warnings: tuple[str, ...]
    error: str | None


@dataclass
class _Phase:
    system_uuid: str
    cli_name: str
    entries: dict[str, dict]
    namespace: tuple[str, ...]


def _profile_parent(
    profile: dict, parent: str, system: str, hmc: HMCClient, kind: str
) -> None:
    href = profile["Resource"]["AssociatedPartition"]["link"]["@attrs"]["href"]
    if (
        not isinstance(href, str)
        or not href.isprintable()
        or any(c.isspace() for c in href)
    ):
        raise ValueError("native profile link contains invalid characters")
    u = urlsplit(href)
    paths = {
        f"/rest/api/uom/{kind}/{parent}",
        f"/rest/api/uom/ManagedSystem/{system}/{kind}/{parent}",
    }
    if (
        u.scheme != "https"
        or u.hostname != hmc.config.host
        or (u.port or 443) != hmc.config.port
        or u.username
        or u.password
        or u.query
        or u.fragment
        or u.path not in paths
    ):
        raise ValueError("contained profile parent/destination differs")


def _bounded(raw: str) -> str:
    if (
        not isinstance(raw, str)
        or len(raw.encode()) > LIMIT
        or "[stderr]" in raw.splitlines()
    ):
        raise ValueError(
            "system-profile output missing, oversized or diagnostic-bearing"
        )
    return raw


async def _observe(hmc: HMCClient, system: str) -> _Phase:
    entry = await hmc.get_managed_system(system)
    if not entry or entry.get("UUID") != system:
        raise ValueError("managed-system identity unavailable")
    cli = render_mtms(entry["Resource"])
    if cli is None:
        raise ValueError("unique current managed-system MTMS required")
    raw = _bounded(
        await run_hmc_command(
            hmc.config,
            f"lssyscfg -r sysprof -m {shlex.quote(cli)} -F name --header",
            include_stderr=True,
        )
    )
    names = (
        []
        if raw.strip() == "No results were found."
        else [_name(r["name"]) for r in parse_hmc_delimited_rows(raw, ("name",), ":")]
    )
    if len(set(names)) != len(names):
        raise ValueError("duplicate system-profile namespace")
    entries: dict[str, dict] = {}
    ids: set[int] = set()
    observed_bytes = 0
    for kind, feed in [
        ("LogicalPartition", await hmc.list_logical_partitions(system)),
        ("VirtualIOServer", await hmc.list_vios(system)),
    ]:
        observed_bytes += len(json.dumps(feed).encode())
        if observed_bytes > LIMIT:
            raise ValueError("member observations exceed system-profile bound")
        for e in feed:
            uuid = e["UUID"]
            r = e["Resource"]
            value = _text(r.get("PartitionID"))
            if (
                not is_uuid(uuid)
                or uuid in entries
                or not re.fullmatch("[1-9][0-9]*", value)
            ):
                raise ValueError("unique current partition UUID/positive ID required")
            ident = int(value)
            if ident in ids:
                raise ValueError("duplicate current partition ID")
            ids.add(ident)
            _name(_text(r.get("PartitionName")))
            profiles = await hmc.list_child(kind, uuid, "LogicalPartitionProfile")
            observed_bytes += len(json.dumps(profiles).encode())
            if observed_bytes > LIMIT:
                raise ValueError("contained profiles exceed system-profile bound")
            profile_names: set[str] = set()
            profile_ids: set[str] = set()
            for profile in profiles:
                pname = _name(_text(profile["Resource"].get("ProfileName")))
                pid = profile["UUID"]
                if not is_uuid(pid) or pname in profile_names or pid in profile_ids:
                    raise ValueError("unique contained profile identity required")
                _profile_parent(profile, uuid, system, hmc, kind)
                profile_names.add(pname)
                profile_ids.add(pid)
            entries[uuid] = {"kind": kind, "entry": e, "profiles": profiles}
    return _Phase(system, cli, entries, tuple(sorted(names)))


def _stable(phase: _Phase) -> tuple:
    members = {}
    for uuid, e in phase.entries.items():
        r = e["entry"]["Resource"]
        members[uuid] = (
            e["kind"],
            {
                k: deepcopy(r.get(k))
                for k in (
                    "PartitionID",
                    "PartitionName",
                    "PartitionState",
                    "Description",
                    "DefaultProfileName",
                    "LastActivatedProfile",
                    "AssociatedPartitionProfile",
                    "PartitionMemoryConfiguration",
                    "PartitionProcessorConfiguration",
                )
            },
            sorted(
                [
                    (
                        p["UUID"],
                        {k: v for k, v in p["Resource"].items() if k != "Metadata"},
                    )
                    for p in e["profiles"]
                ],
                key=lambda p: p[0],
            ),
        )
    return phase.system_uuid, phase.cli_name, phase.namespace, members


def _records(raw: str, phase: _Phase) -> tuple[SystemProfileRecord, ...]:
    raw = _bounded(raw)
    rows = (
        []
        if raw.strip() == "No results were found."
        else parse_hmc_delimited_rows(raw, ("name", "lpar_ids", "profile_names"), ":")
    )
    if sorted(r["name"] for r in rows) != list(phase.namespace):
        raise ValueError("whole inventory and independent namespace disagree")
    result = []
    for row in rows:
        name = _name(row["name"])
        ids, profiles = row["lpar_ids"].split(","), row["profile_names"].split(",")
        if not ids or len(ids) != len(profiles) or len(set(ids)) != len(ids):
            raise ValueError("system-profile pair cardinality/uniqueness differs")
        members = []
        for ident, profile in zip(ids, profiles, strict=True):
            if not re.fullmatch("[1-9][0-9]*", ident):
                raise ValueError("positive current partition ID required")
            candidates = [
                (u, e)
                for u, e in phase.entries.items()
                if _text(e["entry"]["Resource"]["PartitionID"]) == ident
            ]
            if len(candidates) != 1:
                raise ValueError("system-profile member is missing or ambiguous")
            uuid, e = candidates[0]
            matches = [
                p
                for p in e["profiles"]
                if _text(p["Resource"]["ProfileName"]) == _name(profile)
            ]
            if len(matches) != 1:
                raise ValueError("contained member profile is missing or ambiguous")
            members.append(
                SystemProfileMember(
                    e["kind"],
                    uuid,
                    int(ident),
                    _text(e["entry"]["Resource"]["PartitionName"]),
                    matches[0]["UUID"],
                    profile,
                )
            )
        result.append(SystemProfileRecord(name, tuple(members)))
    return tuple(result)


async def _inventory(hmc: HMCClient, system: str):
    before = await _observe(hmc, system)
    raw = await list_system_profile_records(hmc.config, before.cli_name)
    after = await _observe(hmc, system)
    if _stable(before) != _stable(after):
        raise ValueError("system-profile observation changed between fresh phases")
    return after, _records(raw, after)


def _command_failure(exc: BaseException) -> CommandStatus:
    cause = exc.__cause__
    return (
        "failed"
        if isinstance(cause, asyncssh.ProcessError)
        and cause.exit_status not in (None, 0)
        else "uncertain"
    )


async def list_system_profiles(
    hmc: HMCClient,
    system_name_or_uuid: str,
    *,
    name: str | None = None,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> SystemProfileInventory:
    """Read whole bound inventory before any requested-name lookup; never hide invalid rows."""
    validate_wait_timing(True, timeout_seconds, poll_interval)
    system = await resolve_system_uuid(hmc, system_name_or_uuid)
    try:
        async with asyncio.timeout(timeout_seconds):
            _, records = await _inventory(hmc, system)
    except (HMCError, ValueError, KeyError, TypeError, TimeoutError) as exc:
        return SystemProfileInventory(
            system, None, "ssh-command", None, _command_failure(exc), str(exc)[:2048]
        )
    try:
        _bounded(json.dumps([asdict(r) for r in records]))
    except ValueError as exc:
        return SystemProfileInventory(
            system, None, "ssh-command", None, "uncertain", str(exc)
        )
    if name is not None:
        records = tuple(r for r in records if r.name == name)
        if not records:
            raise ValueError(f"system profile {name!r} is missing")
    return SystemProfileInventory(
        system, records, "ssh-command", None, "completed", None
    )


def _resolve_pairs(phase: _Phase, pairs) -> tuple[SystemProfileMember, ...]:
    if not pairs:
        raise ValueError("nonempty member pairs required")
    result = []
    seen = set()
    for pair in pairs:
        pair = SystemProfilePair.model_validate(pair.model_dump())
        matches = [
            (u, e)
            for u, e in phase.entries.items()
            if pair.partition in (u, _text(e["entry"]["Resource"]["PartitionName"]))
        ]
        if len(matches) != 1 or matches[0][0] in seen:
            raise ValueError("unique contained partition selector required")
        uuid, entry = matches[0]
        profiles = [
            p
            for p in entry["profiles"]
            if pair.profile in (p["UUID"], _text(p["Resource"]["ProfileName"]))
        ]
        if len(profiles) != 1:
            raise ValueError("unique contained profile selector required")
        profile = profiles[0]
        r = entry["entry"]["Resource"]
        result.append(
            SystemProfileMember(
                entry["kind"],
                uuid,
                int(_text(r["PartitionID"])),
                _text(r["PartitionName"]),
                profile["UUID"],
                _text(profile["Resource"]["ProfileName"]),
            )
        )
        seen.add(uuid)
    return tuple(result)


async def _admit(
    hmc,
    system,
    name,
    *,
    action,
    members=None,
    new_name=None,
    exclusive_writer_window=False,
    ownership_override=False,
):
    from ..lpar.ownership import authorize_lpar_ownership_description

    _name(name)
    if action != "validate" and exclusive_writer_window is not True:
        raise ValueError("exclusive_writer_window=true required through reconciliation")
    if not isinstance(ownership_override, bool):
        raise TypeError("ownership_override must be boolean")
    if action in {"create", "update", "activate"}:
        console = await hmc.get_console_info()
        version = (
            console_version(console.get("Resource", {}))
            if isinstance(console, dict)
            else None
        )
        if version is None or version < (10, 3, 1060):
            raise ValueError(
                "system-profile REST jobs require proven HMC 10.3.1060 or later"
            )
    phase, records = await _inventory(hmc, system)
    old = next((r for r in records if r.name == name), None)
    if action == "create":
        if old:
            raise ValueError("system-profile name collision")
    elif old is None:
        raise ValueError("system profile is missing")
    if action == "update" and new_name is None and members is None:
        raise ValueError("update requires replacement members or new name")
    target = _name(new_name) if new_name is not None else name
    if target != name and target in phase.namespace:
        raise ValueError("system-profile rename collision")
    selected = (
        _resolve_pairs(phase, members)
        if members is not None
        else old.members
        if old
        else ()
    )
    if not selected:
        raise ValueError("nonempty member pairs required")
    union = {
        m.partition_uuid: m for m in (*(() if old is None else old.members), *selected)
    }
    for uuid, member in union.items():
        resource = phase.entries[uuid]["entry"]["Resource"]
        if "Description" not in resource:
            raise ValueError("authoritative member ownership metadata is missing")
        authorize_lpar_ownership_description(
            hmc,
            phase.cli_name,
            member.partition_name,
            _text(resource["Description"]),
            operation="lpar-mutation",
            ownership_override=ownership_override,
        )
    return phase, old, SystemProfileRecord(target, selected)


async def _refresh(hmc, before: _Phase):
    after, records = await _inventory(hmc, before.system_uuid)
    if _stable(before) != _stable(after):
        raise ValueError("system-profile admission changed before effect")
    return after, records


def _job_identity(hmc, job, expected=None):
    from ...jobs import canonical_job_path

    ident = job["Resource"]["JobID"]
    if (
        not isinstance(ident, str)
        or len(ident) > 4096
        or not re.fullmatch(r"[A-Za-z0-9_:-][A-Za-z0-9_.:-]*", ident)
    ):
        raise ValueError("stable native JobID required")
    if expected is not None and ident != expected:
        raise ValueError("submission and terminal JobID disagree")
    path = f"/rest/api/uom/jobs/{ident}"
    if job.get("link") is not None:
        link = job["link"]
        if (
            not isinstance(link, str)
            or not link.isprintable()
            or any(c.isspace() for c in link)
        ):
            raise ValueError("native job SELF link contains invalid characters")
        u = urlsplit(link)
        if (
            u.scheme not in ("", "https")
            or bool(u.netloc)
            and u.scheme != "https"
            or u.username
            or u.password
            or u.query
            or u.fragment
            or u.hostname is not None
            and u.hostname != hmc.config.host
            or u.scheme == "https"
            and (u.hostname != hmc.config.host or (u.port or 443) != hmc.config.port)
            or canonical_job_path(u.path) != path
        ):
            raise ValueError("native job SELF destination/identity differs")
    return ident


def _job_values(job):
    raw = job["Resource"].get("Results", {}).get("JobParameter", [])
    raw = [raw] if isinstance(raw, dict) else raw
    if not isinstance(raw, list):
        raise TypeError("native job results are malformed")
    values = {}
    for p in raw:
        key = _text(p["ParameterName"])
        if not key or key in values:
            raise ValueError("duplicate or missing native result parameter")
        values[key] = _text(p.get("ParameterValue"))
    _bounded(json.dumps(values))
    return values


def _job_facts(hmc, job, expected):
    _job_identity(hmc, job, expected)
    values = _job_values(job)
    if (
        job["Resource"].get("Status") != "COMPLETED_OK"
        or values.get("returnCode") != "0"
        or values.get("ErrorData")
        or values.get("errorData")
    ):
        raise ValueError("native job lacks definite strict success")
    return values


def _optional_profile(value):
    if (
        isinstance(value, dict)
        and "text" not in value
        and set(value) <= {"@attrs", "ksv", "kxe", "kb"}
    ):
        return ""
    return _text(value)


def _associated(hmc, resource, member):
    raw = resource.get("AssociatedPartitionProfile", {})
    href = raw.get("href") or raw.get("link", {}).get("@attrs", {}).get("href")
    if not href:
        return None
    if (
        not isinstance(href, str)
        or not href.isprintable()
        or any(c.isspace() for c in href)
    ):
        raise ValueError("native profile link contains invalid characters")
    u = urlsplit(href)
    ident = u.path.rsplit("/", 1)[-1]
    paths = {
        f"/rest/api/uom/LogicalPartitionProfile/{ident}",
        f"/rest/api/uom/{member.resource_type}/{member.partition_uuid}/LogicalPartitionProfile/{ident}",
    }
    if (
        u.scheme != "https"
        or u.hostname != hmc.config.host
        or (u.port or 443) != hmc.config.port
        or u.username
        or u.password
        or u.query
        or u.fragment
        or u.path not in paths
        or not is_uuid(ident)
    ):
        raise ValueError("current associated-profile destination is ambiguous")
    return ident


def _coherent_resources(fact):
    from decimal import Decimal

    r, profile = fact["resource"], fact["profile"]
    memory, cpu = (
        r["PartitionMemoryConfiguration"],
        r["PartitionProcessorConfiguration"],
    )
    pm, pc = profile["ProfileMemory"], profile["ProcessorAttributes"]
    current, desired = (
        cpu["CurrentSharedProcessorConfiguration"],
        pc["SharedProcessorConfiguration"],
    )
    units, expected = (
        Decimal(_text(current["CurrentProcessingUnits"])),
        Decimal(_text(desired["DesiredProcessingUnits"])),
    )
    return (
        memory["SharedMemoryEnabled"] == pm["ActiveMemorySharingEnabled"] == "false"
        and cpu["CurrentHasDedicatedProcessors"]
        == pc["HasDedicatedProcessors"]
        == "false"
        and cpu["CurrentSharingMode"] == pc["SharingMode"] == "capped"
        and int(_text(current["AllocatedVirtualProcessors"]))
        == int(_text(desired["DesiredVirtualProcessors"]))
        > 0
        and units.is_finite()
        and expected.is_finite()
        and units == expected > 0
        and int(_text(memory["CurrentMemory"])) == int(_text(pm["DesiredMemory"])) > 0
        and 0 < int(_text(memory["RuntimeMemory"])) <= int(_text(pm["DesiredMemory"]))
    )


def _new_witness(hmc, member, before, after):
    try:
        old, new = before["resource"], after["resource"]
        return (
            member.resource_type == "LogicalPartition"
            and _text(old["PartitionState"]).lower() == "not activated"
            and _optional_profile(old.get("LastActivatedProfile"))
            != member.profile_name
            and before["cli_profile"] != member.profile_name
            and _associated(hmc, old, member) != member.profile_uuid
            and _text(new["PartitionState"]).lower() in {"running", "open firmware"}
            and _optional_profile(new.get("LastActivatedProfile"))
            == after["cli_profile"]
            == member.profile_name
            and _associated(hmc, new, member) == member.profile_uuid
            and _coherent_resources(after)
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


async def _rest(hmc, system, operation, document, timeout, poll):
    from ...jobs import FAILED_JOB_STATUSES, job_outcome, wait_for_submitted_job

    outcome = None
    code = None
    try:
        _bounded(document)
        submission = await hmc.submit_job(
            f"/rest/api/uom/ManagedSystem/{system}/do/{operation}", document
        )
        if submission is None:
            raise ValueError(
                "submission returned no native job identity; reconcile before replay"
            )
        ident = _job_identity(
            hmc, {"Resource": {"JobID": submission["Resource"]["JobID"]}}
        )
        outcome = JobOutcome(
            ident, None, True, None, None, True, f"/rest/api/uom/jobs/{ident}"
        )
        _job_identity(hmc, submission, ident)
        outcome = replace(
            job_outcome(ident, submission),
            job=None,
            job_href=f"/rest/api/uom/jobs/{ident}",
        )
        terminal = await wait_for_submitted_job(hmc, submission, True, timeout, poll)
        _job_identity(hmc, terminal, ident)
        _bounded(json.dumps(terminal))
        outcome = replace(
            job_outcome(ident, terminal),
            job=None,
            job_href=f"/rest/api/uom/jobs/{ident}",
        )
        values = _job_values(terminal)
        code = values.get("returnCode")
        warnings = tuple(
            values[k][:2048] for k in ("result", "detailedStatus") if values.get(k)
        )
        try:
            _job_facts(hmc, terminal, ident)
        except ValueError as exc:
            state = "failed" if outcome.status in FAILED_JOB_STATUSES else "uncertain"
            return state, outcome, code, warnings, str(exc)
        return "completed", outcome, code, warnings, None
    except (
        HMCError,
        ValueError,
        TypeError,
        KeyError,
        TimeoutError,
        asyncio.CancelledError,
    ) as exc:
        return "uncertain", outcome, code, (), str(exc)[:2048]


def _metadata_result(
    system,
    name,
    action,
    state,
    job,
    code,
    post,
    error,
    warnings=(),
    command_status=None,
):
    return SystemProfileOutcome(
        system,
        name,
        action,
        "ssh-command" if action == "delete" else "rest-job",
        state,
        job,
        code,
        command_status if action == "delete" else None,
        None,
        post,
        (),
        warnings,
        error,
    )


async def _metadata(
    hmc,
    selector,
    name,
    action,
    *,
    members=None,
    new_name=None,
    exclusive_writer_window=False,
    ownership_override=False,
    timeout_seconds=300,
    poll_interval=5,
):
    from ...jobs import create_system_profile_job, update_system_profile_job
    from ...ssh.profiles import delete_system_profile_record

    validate_wait_timing(True, timeout_seconds, poll_interval)
    system = await resolve_system_uuid(hmc, selector)
    state, job, code, warnings, error = "uncertain", None, None, (), None
    attempted = False
    command_status = None
    try:
        async with asyncio.timeout(timeout_seconds):
            phase, _old, desired = await _admit(
                hmc,
                system,
                name,
                action=action,
                members=members,
                new_name=new_name,
                exclusive_writer_window=exclusive_writer_window,
                ownership_override=ownership_override,
            )
            _, initial = await _refresh(hmc, phase)
            attempted = True
            if action == "delete":
                command_status = "uncertain"
                try:
                    _bounded(
                        await delete_system_profile_record(
                            hmc.config, phase.cli_name, name
                        )
                    )
                    state = "completed"
                except (HMCError, ValueError, TimeoutError) as exc:
                    state, error = _command_failure(exc), str(exc)[:2048]
                command_status = state
            else:
                names = tuple(m.partition_name for m in desired.members)
                profiles = tuple(m.profile_name for m in desired.members)
                document = (
                    create_system_profile_job(name, names, profiles)
                    if action == "create"
                    else update_system_profile_job(
                        name,
                        new_name,
                        names if members is not None else None,
                        profiles if members is not None else None,
                    )
                )
                state, job, code, warnings, error = await _rest(
                    hmc,
                    system,
                    "CreateSystemProfile"
                    if action == "create"
                    else "UpdateSystemProfile",
                    document,
                    timeout_seconds,
                    poll_interval,
                )
            after, records = await _inventory(hmc, system)
            expected = {r.name: r for r in initial if r.name != name}
            if action != "delete":
                expected[desired.name] = desired
            exact = (
                {r.name: r for r in records} == expected
                and _stable(phase)[:2] == _stable(after)[:2]
                and _stable(phase)[3] == _stable(after)[3]
            )
            if state == "completed":
                return _metadata_result(
                    system,
                    desired.name,
                    action,
                    "completed" if exact else "uncertain",
                    job,
                    code,
                    "verified" if exact else "drifted",
                    error,
                    warnings,
                    command_status,
                )
    except (
        HMCError,
        ValueError,
        TypeError,
        KeyError,
        TimeoutError,
        asyncio.CancelledError,
    ) as exc:
        if not attempted:
            raise
        error = str(exc)[:2048] or "interrupted; reconcile known effects before replay"
        if state != "failed":
            state = "uncertain"
    return _metadata_result(
        system,
        name,
        action,
        state,
        job,
        code,
        "unverified",
        error,
        warnings,
        command_status,
    )


async def create_system_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    name: str,
    members: Sequence[SystemProfilePair],
    *,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> SystemProfileOutcome:
    """Create one bound system profile after every new member is authorized."""
    return await _metadata(
        hmc,
        system_name_or_uuid,
        name,
        "create",
        members=members,
        exclusive_writer_window=exclusive_writer_window,
        ownership_override=ownership_override,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )


async def update_system_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    name: str,
    *,
    new_name: str | None = None,
    members: Sequence[SystemProfilePair] | None = None,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> SystemProfileOutcome:
    """Rename, replace paired members, or both; authorize the old/new union."""
    return await _metadata(
        hmc,
        system_name_or_uuid,
        name,
        "update",
        members=members,
        new_name=new_name,
        exclusive_writer_window=exclusive_writer_window,
        ownership_override=ownership_override,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )


async def delete_system_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    name: str,
    *,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
) -> SystemProfileOutcome:
    """Delete through the fixed CLI route, never as an automatic REST fallback."""
    return await _metadata(
        hmc,
        system_name_or_uuid,
        name,
        "delete",
        exclusive_writer_window=exclusive_writer_window,
        ownership_override=ownership_override,
    )


async def _facts(hmc, phase, members):
    from ...ssh.commands import build_filter

    result = {}
    for member in members:
        entry = phase.entries[member.partition_uuid]
        resource = entry["entry"]["Resource"]
        command = (
            f"lssyscfg -r lpar -m {shlex.quote(phase.cli_name)} --filter "
            f"{shlex.quote(build_filter([('lpar_ids', str(member.partition_id))]))} "
            "-F name:lpar_id:curr_profile --header"
        )
        rows = parse_hmc_delimited_rows(
            _bounded(await run_hmc_command(hmc.config, command, include_stderr=True)),
            ("name", "lpar_id", "curr_profile"),
            ":",
        )
        if (
            len(rows) != 1
            or rows[0]["name"] != member.partition_name
            or rows[0]["lpar_id"] != str(member.partition_id)
        ):
            raise ValueError("independent activation fact has wrong member binding")
        actual = _associated(hmc, resource, member)
        profiles = [p for p in entry["profiles"] if p["UUID"] == actual]
        requested = next(
            p for p in entry["profiles"] if p["UUID"] == member.profile_uuid
        )
        result[member.partition_uuid] = {
            "resource": deepcopy(
                {k: v for k, v in resource.items() if k not in {"Metadata", "Uptime"}}
            ),
            "cli_profile": rows[0]["curr_profile"],
            "profile": deepcopy(profiles[0]["Resource"]) if len(profiles) == 1 else {},
            "profile_uuid": actual,
            "requested_profile": deepcopy(requested["Resource"]),
        }
    return result


def _contrary(hmc, member, fact):
    try:
        r = fact["resource"]
        name = _optional_profile(r.get("LastActivatedProfile"))
        actual = _associated(hmc, r, member)
        return (
            _text(r["PartitionState"]).lower() in {"running", "open firmware"}
            and name == fact["cli_profile"] == _text(fact["profile"]["ProfileName"])
            and name != member.profile_name
            and actual == fact["profile_uuid"] != member.profile_uuid
            and _coherent_resources(fact)
            and int(_text(r["PartitionMemoryConfiguration"]["CurrentMemory"]))
            != int(_text(fact["requested_profile"]["ProfileMemory"]["DesiredMemory"]))
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def _activation_execution(hmc, members, before, after, state):
    if any(
        m.partition_uuid not in before or m.partition_uuid not in after for m in members
    ):
        return "unknown"
    witnessed = [
        _new_witness(hmc, m, before[m.partition_uuid], after[m.partition_uuid])
        for m in members
    ]
    if state == "completed" and all(witnessed):
        return "complete"
    if (
        state == "failed"
        and any(witnessed)
        and any(_contrary(hmc, m, after[m.partition_uuid]) for m in members)
    ):
        return "partial"
    if (
        state == "failed"
        and not any(witnessed)
        and all(
            before[m.partition_uuid] == after[m.partition_uuid]
            and _text(after[m.partition_uuid]["resource"].get("PartitionState")).lower()
            in {"not activated", "running", "open firmware"}
            for m in members
        )
    ):
        return "failed"
    return "unknown"


def _activation_invariants(before, after, members):
    a, z = deepcopy(before), deepcopy(after)
    for phase in (a, z):
        for member in members:
            resource = phase.entries[member.partition_uuid]["entry"]["Resource"]
            for key in (
                "LastActivatedProfile",
                "AssociatedPartitionProfile",
                "PartitionState",
                "PartitionMemoryConfiguration",
                "PartitionProcessorConfiguration",
            ):
                resource.pop(key, None)
    return _stable(a) == _stable(z)


def _member_observations(hmc, members, before, after):
    observations = []
    for member in members:
        old, new = before.get(member.partition_uuid), after.get(member.partition_uuid)
        try:
            a = _text(old["resource"]["PartitionState"]) if old else None
            z = _text(new["resource"]["PartitionState"]) if new else None
            observations.append(
                MemberObservation(
                    member,
                    a,
                    z,
                    new["cli_profile"] if new else None,
                    _associated(hmc, new["resource"], member) if new else None,
                    "unavailable"
                    if a is None or z is None
                    else "unchanged"
                    if a == z
                    else "changed",
                    None,
                )
            )
        except (ValueError, TypeError, KeyError) as exc:
            observations.append(
                MemberObservation(
                    member, None, None, None, None, "unavailable", str(exc)[:2048]
                )
            )
    return tuple(observations)


async def activate_system_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    name: str,
    *,
    action: Action = "activate",
    continue_on_error: bool = False,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> SystemProfileOutcome:
    """Validate or activate once, reporting backend facts separately from bounded witnesses."""
    from ...jobs import activate_system_profile_job
    from ...ssh.profiles import validate_system_profile_record

    if action not in {"validate", "activate", "validate-and-activate"}:
        raise ValueError("unknown system-profile action")
    if not isinstance(continue_on_error, bool):
        raise TypeError("continue_on_error must be boolean")
    if action == "validate" and continue_on_error:
        raise ValueError("validation-only cannot continue activation")
    validate_wait_timing(True, timeout_seconds, poll_interval)
    system = await resolve_system_uuid(hmc, system_name_or_uuid)
    state, command_status, job, code, warnings, error = (
        "uncertain",
        None,
        None,
        None,
        (),
        None,
    )
    before, after, members, execution, post = (
        {},
        {},
        (),
        None if action == "validate" else "unknown",
        "unverified",
    )
    attempted = False
    try:
        async with asyncio.timeout(timeout_seconds):
            phase, record, _ = await _admit(
                hmc,
                system,
                name,
                action=action,
                exclusive_writer_window=exclusive_writer_window,
                ownership_override=ownership_override,
            )
            members = record.members
            before = await _facts(hmc, phase, members)
            await _refresh(hmc, phase)
            attempted = True
            if action == "activate":
                state, job, code, warnings, error = await _rest(
                    hmc,
                    system,
                    "ActivateSystemProfile",
                    activate_system_profile_job(name, continue_on_error),
                    timeout_seconds,
                    poll_interval,
                )
            else:
                command_status = "uncertain"
                try:
                    raw = await validate_system_profile_record(
                        hmc.config,
                        phase.cli_name,
                        name,
                        activate=action == "validate-and-activate",
                        continue_on_error=continue_on_error,
                    )
                    _bounded(raw)
                    state, warnings = "completed", (raw[:2048],) if raw else ()
                except (HMCError, ValueError, TimeoutError) as exc:
                    state, error = _command_failure(exc), str(exc)[:2048]
                command_status = state
            current, records = await _inventory(hmc, system)
            after = await _facts(hmc, current, members)
            await _refresh(hmc, current)
            if next(
                (r for r in records if r.name == name), None
            ) != record or not _activation_invariants(phase, current, members):
                raise ValueError(
                    "protected membership/profile/ownership state changed during activation"
                )
            if action == "validate":
                if (
                    state == "completed"
                    and before == after
                    and _stable(phase) == _stable(current)
                ):
                    post = "not-applicable"
                elif state == "completed":
                    state, post = "uncertain", "drifted"
            else:
                execution = _activation_execution(hmc, members, before, after, state)
                post = (
                    "verified"
                    if execution == "complete"
                    else "drifted"
                    if execution == "partial"
                    else "unverified"
                )
                if state == "completed" and execution != "complete":
                    state = "uncertain"
    except (
        HMCError,
        ValueError,
        TypeError,
        KeyError,
        TimeoutError,
        asyncio.CancelledError,
    ) as exc:
        if not attempted:
            raise
        error = str(exc)[:2048] or "interrupted; caller reconciliation required"
        if state != "failed":
            state = "uncertain"
        execution, post = None if action == "validate" else "unknown", "unverified"
    return SystemProfileOutcome(
        system,
        name,
        action,
        "rest-job" if action == "activate" else "ssh-command",
        state,
        job,
        code,
        command_status,
        execution,
        post,
        _member_observations(hmc, members, before, after),
        warnings,
        error,
    )
