"""Contained saved-profile lifecycle with verified recovery and honest outcomes."""

from __future__ import annotations

import asyncio
import json
import secrets
import shlex
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from ...client.client_profiles import (
    NativeProfile,
    PartitionKind,
    delete_native_profile,
    read_native_profile,
    require_fresh_profile,
    write_native_profile,
)
from ...client.core import HMCClient
from ...documents.partition_profile import (
    ProfileDifference,
    ProfilePatch,
    apply_profile_patch,
    clone_profile_document,
    compare_profiles,
    one,
    profile_name,
)
from ...errors import HMCError
from ...jobs import (
    JobOutcome,
    build_job_request,
    canonical_job_path,
    job_identifier,
    job_outcome,
    validate_wait_timing,
    wait_for_submitted_job,
)
from ...resource_identity import is_uuid, resolve_system_uuid, system_name_from_uuid
from ...ssh.transport import run_hmc_command
from ...xmlutil import console_version, leaf_text
from .ownership import authorize_lpar_ownership_description

Execution = Literal["completed", "rejected", "unknown"]
Requested = Literal["verified", "mismatched", "unverified"]
Preservation = Literal["verified", "drifted", "unverified", "not-applicable"]
LIMIT = 5 * 1024 * 1024


@dataclass(frozen=True)
class PartitionProfileRecord:
    uuid: str
    name: str
    setting_id: str
    etag: str | None
    fields: dict[str, Any]


@dataclass(frozen=True)
class PartitionProfileInventory:
    system_uuid: str
    partition_uuid: str
    partition_type: PartitionKind
    profiles: tuple[PartitionProfileRecord, ...]


@dataclass(frozen=True)
class RecoveryProfile:
    uuid: str | None
    name: str


@dataclass(frozen=True)
class PartitionProfileOutcome:
    action: Literal["update", "save", "default", "delete"]
    system_uuid: str
    partition_uuid: str
    partition_type: PartitionKind
    profile_name: str
    profile_uuid: str | None
    execution: Execution = "unknown"
    requested_change: Requested = "unverified"
    preservation: Preservation = "unverified"
    before_etag: str | None = None
    after_etag: str | None = None
    comparison_complete: bool = False
    details_truncated: bool = False
    differences: tuple[ProfileDifference, ...] = ()
    recovery_profile: RecoveryProfile | None = None
    recovery_verified: bool = False
    job: JobOutcome | None = None
    return_code: str | None = None
    diagnostics: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class _Target:
    system: str
    cli: str
    uuid: str
    name: str
    kind: PartitionKind
    resource: dict[str, Any]


def _text(value: object) -> str:
    value = leaf_text(value)
    if not isinstance(value, str):
        raise TypeError("partition/profile scalar is unavailable or malformed")
    return value


def _window(value: bool) -> None:
    if value is not True:
        raise PermissionError(
            "assert exclusive_writer_window=true after coordinating partition,profile and system-profile writers"
        )


async def _target(
    hmc: HMCClient,
    system: str,
    partition: str,
    kind: PartitionKind,
    mutate: bool = False,
    override: bool = False,
) -> _Target:
    if kind not in {"LogicalPartition", "VirtualIOServer"} or not isinstance(
        override, bool
    ):
        raise ValueError("valid partition_type and boolean ownership_override required")
    system = await resolve_system_uuid(hmc, system)
    cli = await system_name_from_uuid(hmc, system)
    if cli is None:
        raise ValueError("managed system has no unique CLI name")
    feed = await (
        hmc.list_logical_partitions(system)
        if kind == "LogicalPartition"
        else hmc.list_vios(system)
    )
    if not isinstance(feed, list) or len(json.dumps(feed).encode()) > LIMIT:
        raise ValueError("partition containment feed is malformed or oversized")
    matches = [
        e
        for e in feed
        if (is_uuid(partition) and e.get("UUID", "").casefold() == partition.casefold())
        or (
            not is_uuid(partition)
            and _text(e.get("Resource", {}).get("PartitionName")) == partition
        )
    ]
    if len(matches) != 1 or not is_uuid(matches[0].get("UUID", "")):
        raise ValueError("partition is missing,ambiguous or outside the managed system")
    uuid = matches[0]["UUID"]
    entry = await hmc.get_uom(kind, uuid)
    if (
        not isinstance(entry, dict)
        or entry.get("UUID") != uuid
        or not isinstance(entry.get("Resource"), dict)
    ):
        raise ValueError("contained partition readback is unavailable or foreign")
    resource = deepcopy(entry["Resource"])
    name = profile_name(_text(resource.get("PartitionName")))
    if mutate:
        description = _text(resource.get("Description"))
        authorize_lpar_ownership_description(
            hmc,
            cli,
            name,
            description,
            operation="lpar-mutation",
            ownership_override=override,
        )
        _partition_snapshot(resource)
    return _Target(system, cli, uuid, name, kind, resource)


def _partition_snapshot(resource: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        key: _text(resource.get(key))
        for key in (
            "PartitionName",
            "PartitionID",
            "Description",
            "DefaultProfileName",
            "LastActivatedProfile",
            "CurrentProfileSync",
        )
    }
    if any(
        not snapshot[k].strip()
        for k in (
            "PartitionName",
            "PartitionID",
            "DefaultProfileName",
            "LastActivatedProfile",
            "CurrentProfileSync",
        )
    ):
        raise ValueError("partition current/default/sync identity is unavailable")
    snapshot["AssociatedPartitionProfile"] = deepcopy(
        resource.get("AssociatedPartitionProfile")
    )
    return snapshot


async def _refresh(hmc: HMCClient, target: _Target, override: bool) -> _Target:
    current = await _target(
        hmc, target.system, target.uuid, target.kind, True, override
    )
    if _partition_snapshot(current.resource) != _partition_snapshot(target.resource):
        raise ValueError(
            "partition ownership/current/default/sync snapshot changed; refresh before retrying"
        )
    return current


async def _inventory(hmc: HMCClient, target: _Target) -> tuple[NativeProfile, ...]:
    feed = await hmc.list_child(target.kind, target.uuid, "LogicalPartitionProfile")
    if (
        not isinstance(feed, list)
        or len(feed) > 1024
        or len(json.dumps(feed).encode()) > LIMIT
    ):
        raise ValueError("profile inventory exceeds its complete bounded envelope")
    ids = [e.get("UUID") for e in feed]
    if any(not isinstance(v, str) or not is_uuid(v) for v in ids) or len(
        set(ids)
    ) != len(ids):
        raise ValueError("profile feed has missing/duplicate identities")
    records = []
    size = 0
    for uuid in cast(list[str], ids):
        record = await read_native_profile(
            hmc, target.kind, target.uuid, uuid, target.system
        )
        size += len(record.raw.encode())
        if size > hmc.config.max_response_bytes:
            raise ValueError("complete profile inventory exceeds response bound")
        records.append(record)
    if len({r.name for r in records}) != len(records):
        raise ValueError("profile names are ambiguous")
    return tuple(records)


def _choose(records: tuple[NativeProfile, ...], selector: str) -> NativeProfile:
    profile_name(selector)
    matches = [
        r
        for r in records
        if (
            r.uuid.casefold() == selector.casefold()
            if is_uuid(selector)
            else r.name == selector
        )
    ]
    if len(matches) != 1:
        raise ValueError(
            "profile selector is missing or ambiguous inside this partition"
        )
    return matches[0]


def _record(profile: NativeProfile) -> PartitionProfileRecord:
    return PartitionProfileRecord(
        profile.uuid, profile.name, profile.setting_id, profile.etag, profile.resource
    )


async def list_partition_profiles(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    *,
    partition_type: PartitionKind = "LogicalPartition",
) -> PartitionProfileInventory:
    """List complete bounded saved profiles, including contained names/opaque ETags."""
    target = await _target(hmc, system_name_or_uuid, lpar_name_or_uuid, partition_type)
    return PartitionProfileInventory(
        target.system,
        target.uuid,
        target.kind,
        tuple(_record(r) for r in await _inventory(hmc, target)),
    )


async def read_partition_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_selector: str,
    *,
    partition_type: PartitionKind = "LogicalPartition",
) -> PartitionProfileRecord:
    """Resolve one exact profile name/UUID within its owning partition."""
    target = await _target(hmc, system_name_or_uuid, lpar_name_or_uuid, partition_type)
    return _record(_choose(await _inventory(hmc, target), profile_selector))


def _outcome(action, target, profile: NativeProfile | None, name: str | None = None):
    actual_name = name if name is not None else profile.name if profile else None
    if actual_name is None:
        raise ValueError("profile outcome requires a selected name")
    return PartitionProfileOutcome(
        action,
        target.system,
        target.uuid,
        target.kind,
        actual_name,
        profile.uuid if profile else None,
        before_etag=profile.etag if profile else None,
    )


def _etag(profile: NativeProfile, expected: str | None) -> None:
    if not expected or expected != profile.etag:
        raise ValueError("expected_etag must match a fresh contained profile ETag")


def _same_identity(before: NativeProfile, after: NativeProfile) -> bool:
    return (
        before.uuid == after.uuid
        and before.setting_id == after.setting_id
        and all(
            one(before.element, path).text == one(after.element, path).text
            for path in (
                "Metadata/Atom/AtomID",
                "Metadata/Atom/AtomCreated",
                "ProfileType",
            )
        )
    )


async def update_partition_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_selector: str,
    expected_etag: str,
    patch: ProfilePatch,
    *,
    partition_type: PartitionKind = "LogicalPartition",
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
) -> PartitionProfileOutcome:
    """Typed whole-profile If-Match update; applied changes and preservation stay separate."""
    _window(exclusive_writer_window)
    target = await _target(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        partition_type,
        True,
        ownership_override,
    )
    records = await _inventory(hmc, target)
    before = _choose(records, profile_selector)
    _etag(before, expected_etag)
    expected = apply_profile_patch(before.element, patch)
    new_name = one(expected, "ProfileName").text
    if any(r.uuid != before.uuid and r.name == new_name for r in records):
        raise ValueError("profile rename collides with another contained profile")
    await _refresh(hmc, target, ownership_override)
    fresh = await read_native_profile(
        hmc, target.kind, target.uuid, before.uuid, target.system
    )
    require_fresh_profile(before, fresh)
    outcome = _outcome("update", target, before)
    try:
        status = await write_native_profile(
            hmc, target.kind, target.uuid, expected, before
        )
    except HMCError as error:
        return replace(
            outcome,
            execution="rejected" if error.status_code == 412 else "unknown",
            error=str(error)[:2048],
        )
    outcome = replace(outcome, execution="unknown" if status == 202 else "completed")
    try:
        after = await read_native_profile(
            hmc, target.kind, target.uuid, before.uuid, target.system
        )
        from ...documents.partition_profile import requested_profile_values

        requested = _same_identity(before, after) and requested_profile_values(
            expected, patch
        ) == requested_profile_values(after.element, patch)
        comparison = compare_profiles(expected, after.element)
        return replace(
            outcome,
            profile_name=after.name,
            after_etag=after.etag,
            requested_change="verified" if requested else "mismatched",
            preservation="verified"
            if comparison.equal
            else "drifted"
            if comparison.complete
            else "unverified",
            comparison_complete=comparison.complete,
            details_truncated=comparison.details_truncated,
            differences=comparison.differences,
            diagnostics=(
                "applied with preservation drift;requested change verified but full preservation not verified;reconcile before another write",
            )
            if outcome.execution == "completed"
            and requested
            and comparison.complete
            and not comparison.equal
            else (),
        )
    except (HMCError, ValueError, TypeError) as error:
        return replace(outcome, error=str(error)[:2048])


def _job_identity(
    hmc: HMCClient, entry: dict | None, expected: str | None = None
) -> str:
    if not isinstance(entry, dict):
        raise TypeError("profile job identity unavailable")
    ident = job_identifier(entry)
    if (
        not ident
        or len(ident) > 256
        or any(c.isspace() or c in "/?#%" for c in ident)
        or (expected and ident != expected)
    ):
        raise ValueError("profile job identity missing,malformed or changed")
    link = entry.get("link")
    if link:
        u = urlsplit(link)
        if (
            u.scheme
            and (
                u.scheme != "https"
                or u.hostname != hmc.config.host
                or (u.port or 443) != hmc.config.port
            )
            or u.username
            or u.password
            or u.query
            or u.fragment
            or canonical_job_path(u.path) != f"/rest/api/uom/jobs/{ident}"
        ):
            raise ValueError(
                "profile job polling link differs from submitted identity/HMC"
            )
    return ident


def _result_values(entry: dict | None) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise TypeError("profile job results unavailable")
    resource = entry.get("Resource")
    if not isinstance(resource, dict) or not isinstance(resource.get("Results"), dict):
        raise TypeError("profile job result structure is unavailable")
    parameters = resource["Results"].get("JobParameter")
    parameters = [parameters] if isinstance(parameters, dict) else parameters
    if not isinstance(parameters, list) or len(json.dumps(parameters).encode()) > LIMIT:
        raise ValueError("profile job result parameters malformed/oversized")
    values = {}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            raise TypeError("profile job parameter is malformed")
        name = _text(parameter.get("ParameterName"))
        if not name or name in values:
            raise ValueError("profile job result parameter duplicated/missing")
        value = parameter.get("ParameterValue")
        values[name] = "" if value is None else _text(value)
    if not all(k in values for k in ("returnCode", "result", "detailedStatus")):
        raise ValueError("profile job lacks CLI returnCode/result/detailedStatus")
    return values


async def _profile_job(
    hmc: HMCClient,
    target: _Target,
    operation: str,
    parameters: dict[str, str],
    outcome: PartitionProfileOutcome,
    timeout: int,
    poll: int,
) -> PartitionProfileOutcome:
    if operation not in {"ChangeDefaultProfileName", "SaveCurrentConfig"}:
        raise ValueError("unsupported partition-profile job operation")
    submitted_outcome = None
    try:
        submitted = await hmc.submit_job(
            f"/rest/api/uom/{target.kind}/{target.uuid}/do/{operation}",
            build_job_request(operation, target.kind, parameters),
        )
        ident = _job_identity(hmc, submitted)
        submitted_outcome = replace(job_outcome(ident, submitted), job=None)
        terminal = await wait_for_submitted_job(hmc, submitted, True, timeout, poll)
        _job_identity(hmc, terminal, ident)
        normalized = replace(job_outcome(ident, terminal), job=None)
        values = _result_values(terminal)
        terminal_resource = cast(dict[str, Any], terminal)["Resource"]
        diagnostics = tuple(
            values[k][:2048] for k in ("result", "detailedStatus") if values[k]
        )
        success = (
            normalized.status == "COMPLETED_OK"
            and values["returnCode"] == "0"
            and not values["detailedStatus"]
            and not any(
                values.get(k) or terminal_resource.get(k)
                for k in ("ErrorData", "errorData")
            )
        )
        return replace(
            outcome,
            execution="completed" if success else "unknown",
            job=normalized,
            return_code=values["returnCode"],
            diagnostics=diagnostics,
            error=None
            if success
            else "profile job did not establish definite CLI success; reconcile before retrying",
        )
    except (HMCError, ValueError, TypeError, KeyError, TimeoutError) as error:
        return replace(outcome, job=submitted_outcome, error=str(error)[:2048])


async def _job_version(hmc: HMCClient, timeout: int, poll: int) -> None:
    validate_wait_timing(True, timeout, poll)
    console = await hmc.get_console_info()
    version = (
        console_version(console.get("Resource", {}))
        if isinstance(console, dict)
        else None
    )
    if version is None or version < (10, 3, 1060):
        raise ValueError("partition profile jobs require proven HMC 10.3.1060 or later")


async def set_default_partition_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_selector: str,
    *,
    partition_type: PartitionKind = "LogicalPartition",
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> PartitionProfileOutcome:
    """Select one contained default profile with strict native job/readback facts."""
    _window(exclusive_writer_window)
    await _job_version(hmc, timeout_seconds, poll_interval)
    target = await _target(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        partition_type,
        True,
        ownership_override,
    )
    before = _choose(await _inventory(hmc, target), profile_selector)
    await _refresh(hmc, target, ownership_override)
    require_fresh_profile(
        before,
        await read_native_profile(
            hmc, target.kind, target.uuid, before.uuid, target.system
        ),
    )
    outcome = await _profile_job(
        hmc,
        target,
        "ChangeDefaultProfileName",
        {"DefaultProfileName": before.name},
        _outcome("default", target, before),
        timeout_seconds,
        poll_interval,
    )
    if outcome.execution != "completed":
        return outcome
    try:
        after = await _target(hmc, target.system, target.uuid, target.kind)
        record = await read_native_profile(
            hmc, target.kind, target.uuid, before.uuid, target.system
        )
        comparison = compare_profiles(before.element, record.element)
        return replace(
            outcome,
            requested_change="verified"
            if _text(after.resource.get("DefaultProfileName")) == before.name
            else "mismatched",
            preservation="verified"
            if comparison.equal
            else "drifted"
            if comparison.complete
            else "unverified",
            comparison_complete=comparison.complete,
            details_truncated=comparison.details_truncated,
            differences=comparison.differences,
            after_etag=record.etag,
        )
    except (HMCError, ValueError, TypeError) as error:
        return replace(outcome, error=str(error)[:2048])


async def _unchanged_original(
    hmc: HMCClient, target: _Target, before: NativeProfile, override: bool
) -> None:
    await _refresh(hmc, target, override)
    require_fresh_profile(
        before,
        await read_native_profile(
            hmc, target.kind, target.uuid, before.uuid, target.system
        ),
    )


async def _recovery(
    hmc: HMCClient,
    target: _Target,
    before: NativeProfile,
    records: tuple[NativeProfile, ...],
    selector: str | None,
    outcome: PartitionProfileOutcome,
    override: bool,
) -> PartitionProfileOutcome:
    if selector is not None:
        recovery = _choose(records, selector)
        if recovery.uuid == before.uuid:
            raise ValueError("recovery profile cannot be the overwrite destination")
        outcome = replace(
            outcome, recovery_profile=RecoveryProfile(recovery.uuid, recovery.name)
        )
        name = recovery.name
        document = None
    else:
        name = "hmcprec-" + secrets.token_hex(12)
        if any(r.name == name for r in records):
            raise ValueError("recovery name collided; nothing was written")
        document = clone_profile_document(before.element, name)
        await _unchanged_original(hmc, target, before, override)
        outcome = replace(outcome, recovery_profile=RecoveryProfile(None, name))
    try:
        if document is not None:
            status = await write_native_profile(hmc, target.kind, target.uuid, document)
            recovery = _choose(await _inventory(hmc, target), name)
            outcome = replace(
                outcome, recovery_profile=RecoveryProfile(recovery.uuid, name)
            )
            if status == 202:
                return replace(
                    outcome,
                    error="recovery copy accepted asynchronously;retain and reconcile before force-save",
                )
        comparison = compare_profiles(before.element, recovery.element, recovery=True)
        if recovery.name != name or not comparison.equal or not comparison.complete:
            return replace(
                outcome,
                error="recovery copy differs; overwrite not submitted;retain copy for reconciliation",
                differences=comparison.differences,
                comparison_complete=comparison.complete,
                details_truncated=comparison.details_truncated,
            )
        await _unchanged_original(hmc, target, before, override)
        fresh_copy = await read_native_profile(
            hmc, target.kind, target.uuid, recovery.uuid, target.system
        )
        require_fresh_profile(recovery, fresh_copy)
        return replace(
            outcome,
            recovery_verified=True,
            preservation="verified",
            comparison_complete=True,
        )
    except asyncio.CancelledError as error:
        error.add_note(
            "Do not retry blindly; recovery profile may be retained: " + name
        )
        raise
    except (HMCError, ValueError, TypeError) as error:
        return replace(
            outcome,
            error="recovery-copy outcome requires reconciliation: " + str(error)[:1800],
        )


async def save_current_partition_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    name: str,
    *,
    partition_type: PartitionKind = "LogicalPartition",
    overwrite: bool = False,
    expected_etag: str | None = None,
    recovery_profile_selector: str | None = None,
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
) -> PartitionProfileOutcome:
    """Save current configuration; force-save only after a retained on-HMC copy verifies."""
    _window(exclusive_writer_window)
    profile_name(name)
    if not isinstance(overwrite, bool):
        raise TypeError("overwrite must be boolean")
    await _job_version(hmc, timeout_seconds, poll_interval)
    target = await _target(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        partition_type,
        True,
        ownership_override,
    )
    records = await _inventory(hmc, target)
    before = next((r for r in records if r.name == name), None)
    if before is not None:
        if not overwrite:
            raise ValueError(
                "existing profile requires explicit overwrite=true and expected_etag"
            )
        _etag(before, expected_etag)
        if one(before.element, "ProfileType").text != "REG_LPAR_PROFILE_TYPE":
            raise ValueError(
                "full-system or unknown profile type cannot be force-saved"
            )
    elif expected_etag is not None or recovery_profile_selector is not None:
        raise ValueError("overwrite preconditions cannot target a nonexistent profile")
    outcome = _outcome("save", target, before, name)
    try:
        if before is not None:
            outcome = await _recovery(
                hmc,
                target,
                before,
                records,
                recovery_profile_selector,
                outcome,
                ownership_override,
            )
            if not outcome.recovery_verified:
                return outcome
            await _unchanged_original(hmc, target, before, ownership_override)
        else:
            await _refresh(hmc, target, ownership_override)
            if any(r.name == name for r in await _inventory(hmc, target)):
                raise ValueError(
                    "new profile name appeared before save; overwrite not submitted"
                )
        parameters = {"PartitionProfileName": name}
        if before is not None:
            parameters["force"] = "true"
        outcome = await _profile_job(
            hmc,
            target,
            "SaveCurrentConfig",
            parameters,
            outcome,
            timeout_seconds,
            poll_interval,
        )
        if outcome.execution != "completed":
            return outcome
        after = _choose(await _inventory(hmc, target), name)
        if before and not _same_identity(before, after):
            return replace(
                outcome,
                error="overwrite destination identity changed; retain verified recovery",
            )
        return replace(
            outcome,
            profile_uuid=after.uuid,
            after_etag=after.etag,
            requested_change="verified",
            preservation="verified" if outcome.recovery_verified else "not-applicable",
        )
    except asyncio.CancelledError as error:
        if outcome.recovery_profile:
            error.add_note(
                "Do not retry blindly; recovery profile retained: "
                + outcome.recovery_profile.name
            )
        raise
    except (HMCError, ValueError, TypeError) as error:
        return replace(outcome, error=str(error)[:2048])


async def _empty_system_profiles(hmc: HMCClient, target: _Target) -> None:
    raw = await run_hmc_command(
        hmc.config,
        f"lssyscfg -r sysprof -m {shlex.quote(target.cli)} -F name:lpar_ids:profile_names --header",
        include_stderr=True,
    )
    if (
        not isinstance(raw, str)
        or len(raw.encode()) > LIMIT
        or raw.strip() != "No results were found."
    ):
        raise ValueError(
            "delete requires the canonical empty unfiltered system-profile inventory; retain profile for operator-managed membership reconciliation"
        )


def _delete_eligible(
    hmc: HMCClient,
    target: _Target,
    profile: NativeProfile,
    records: tuple[NativeProfile, ...],
) -> None:
    resource = target.resource
    if (
        len(records) <= 1
        or profile.name
        in {
            _text(resource.get("DefaultProfileName")),
            _text(resource.get("LastActivatedProfile")),
        }
        or _text(resource.get("CurrentProfileSync")) not in {"Disabled", "Suspended"}
    ):
        raise ValueError(
            "profile is current,default,last or synchronized; deletion refused"
        )
    if one(profile.element, "ProfileType").text != "REG_LPAR_PROFILE_TYPE":
        raise ValueError("full-system/unknown profile type is retained")
    association = resource.get("AssociatedPartitionProfile")
    if association is not None:
        if not isinstance(association, dict):
            raise ValueError("associated active profile link is malformed")
        href = association.get("href")
        if "link" in association:
            links = association["link"]
            if not isinstance(links, dict) or href not in {None, links.get("href")}:
                raise ValueError("associated active profile link is ambiguous")
            href = links.get("href")
        if (
            not isinstance(href, str)
            or not href.isprintable()
            or any(c.isspace() for c in href)
        ):
            raise ValueError("associated active profile identity unavailable")
        u = urlsplit(href)
        prefixes = {
            f"/rest/api/uom/{target.kind}/{target.uuid}/LogicalPartitionProfile/",
            f"/rest/api/uom/ManagedSystem/{target.system}/{target.kind}/{target.uuid}/LogicalPartitionProfile/",
        }
        if (
            u.scheme != "https"
            or u.hostname != hmc.config.host
            or (u.port or 443) != hmc.config.port
            or u.username
            or u.password
            or u.query
            or u.fragment
            or not any(
                u.path.startswith(p) and is_uuid(u.path[len(p) :]) for p in prefixes
            )
        ):
            raise ValueError("associated active profile link is foreign or malformed")
        if u.path.rsplit("/", 1)[-1].casefold() == profile.uuid.casefold():
            raise ValueError("associated active profile is protected from deletion")


async def delete_partition_profile(
    hmc: HMCClient,
    system_name_or_uuid: str,
    lpar_name_or_uuid: str,
    profile_selector: str,
    expected_etag: str,
    *,
    partition_type: PartitionKind = "LogicalPartition",
    exclusive_writer_window: bool = False,
    ownership_override: bool = False,
) -> PartitionProfileOutcome:
    """Delete only an unprotected contained profile with empty system-profile membership."""
    _window(exclusive_writer_window)
    target = await _target(
        hmc,
        system_name_or_uuid,
        lpar_name_or_uuid,
        partition_type,
        True,
        ownership_override,
    )
    records = await _inventory(hmc, target)
    before = _choose(records, profile_selector)
    _etag(before, expected_etag)
    _delete_eligible(hmc, target, before, records)
    await _empty_system_profiles(hmc, target)
    await _refresh(hmc, target, ownership_override)
    fresh = await _inventory(hmc, target)
    if {r.uuid for r in records} != {r.uuid for r in fresh}:
        raise ValueError("profile namespace changed before delete")
    for original in records:
        require_fresh_profile(original, _choose(fresh, original.uuid))
    _delete_eligible(hmc, target, _choose(fresh, before.uuid), fresh)
    await _empty_system_profiles(hmc, target)
    outcome = _outcome("delete", target, before)
    try:
        status = await delete_native_profile(hmc, target.kind, target.uuid, before.uuid)
    except HMCError as error:
        return replace(outcome, error=str(error)[:2048])
    outcome = replace(
        outcome,
        execution="unknown" if status == 202 else "completed",
        error="delete accepted asynchronously;absence readback is provisional"
        if status == 202
        else None,
    )
    try:
        after = await _inventory(hmc, target)
        if {r.uuid for r in after} != {
            r.uuid for r in records if r.uuid != before.uuid
        }:
            return replace(
                outcome,
                requested_change="mismatched",
                error="delete readback differs; reconcile before retrying",
            )
        for record in after:
            require_fresh_profile(_choose(records, record.uuid), record)
        return replace(
            outcome,
            requested_change="verified",
            preservation="verified",
            comparison_complete=True,
        )
    except (HMCError, ValueError, TypeError) as error:
        return replace(outcome, error=str(error)[:2048])
