"""System-profile membership and lifecycle; fixed routes and whole-member admission."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from copy import deepcopy
from dataclasses import dataclass
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
from ...xmlutil import leaf_text, render_mtms

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
    if not value.strip() or any(c in value for c in '\x00\n\r,:"\\'):
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
