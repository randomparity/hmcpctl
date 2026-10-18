"""Bounded managed-system backup inventories and explicit artifact deletion."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Literal

from ...client.core import HMCClient
from ...errors import HMCError
from ...jobs import (
    SUCCESSFUL_JOB_STATUSES,
    JobOutcome,
    build_job_request,
    job_identifier,
    job_outcome,
    wait_for_submitted_job,
)
from ...resource_identity import resolve_system_uuid

_MAX_RESULT_BYTES = 1_048_576
_MAX_ARTIFACTS = 1000
_MAX_IDENTITY_BYTES = 4096
_MAX_DETAIL = 2048


@dataclass(frozen=True)
class BackupProfileArtifact:
    file_name: str
    backup_time: str


@dataclass(frozen=True)
class BackupProfileInventory:
    system_uuid: str
    job: JobOutcome
    artifacts: tuple[BackupProfileArtifact, ...] | None


@dataclass(frozen=True)
class BackupProfileDeletion:
    system_uuid: str
    file_name: str
    job: JobOutcome
    postcondition: Literal["absent", "present", "unknown", "not-checked"]
    postcondition_error: str | None = None


def _detail(text: str) -> str:
    suffix = " [truncated]"
    return (
        text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - len(suffix)] + suffix
    )


def _project(outcome: JobOutcome) -> JobOutcome:
    for value in (outcome.job_id, outcome.job_href, outcome.status):
        if value is not None and len(value.encode("utf-8")) > _MAX_IDENTITY_BYTES:
            raise HMCError(
                "Backup artifact job identity exceeds 4096 bytes; effects are unknown; do not retry the mutation"
            )
    return replace(
        outcome, job=None, error=_detail(outcome.error) if outcome.error else None
    )


def _validate_file_name(file_name: str) -> None:
    if (
        not isinstance(file_name, str)
        or not file_name
        or file_name != file_name.strip()
        or "/" in file_name
        or "\\" in file_name
        or not file_name.strip(".")
        or file_name.startswith("-")
        or any(ord(c) < 32 or ord(c) == 127 for c in file_name)
    ):
        raise ValueError(
            "file_name must be an unpadded HMC backup name, without paths, dot-only names, leading dash or control characters"
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _parse_inventory(entry: dict[str, Any] | None) -> tuple[BackupProfileArtifact, ...]:
    resource = (entry or {}).get("Resource") or {}
    results = resource.get("Results")
    parameters = results.get("JobParameter", []) if isinstance(results, dict) else []
    parameters = [parameters] if isinstance(parameters, dict) else parameters
    matches = (
        [
            p.get("ParameterValue")
            for p in parameters
            if isinstance(p, dict) and p.get("ParameterName") == "result"
        ]
        if isinstance(parameters, list)
        else []
    )
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise HMCError(
            "Backup inventory requires one lowercase result parameter; deletion was not admitted"
        )
    text = matches[0]
    if len(text.encode("utf-8")) > _MAX_RESULT_BYTES:
        raise HMCError(
            "Backup inventory exceeds 1 MiB; no truncated inventory can admit deletion"
        )
    try:
        rows = json.loads(text, object_pairs_hook=_unique_object)
        if not isinstance(rows, list) or len(rows) > _MAX_ARTIFACTS:
            raise ValueError("expected at most 1000 inventory entries")
        artifacts = []
        names: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("BackupTime"), str):
                raise TypeError("expected BackupFileName and BackupTime strings")
            name = row.get("BackupFileName")
            _validate_file_name(name)
            if name in names:
                raise ValueError("duplicate backup name")
            names.add(name)
            artifacts.append(BackupProfileArtifact(name, row["BackupTime"]))
    except (ValueError, TypeError, RecursionError) as exc:
        raise HMCError(
            "Backup inventory is malformed or ambiguous; inspect the HMC inventory before deleting"
        ) from exc
    return tuple(artifacts)


async def list_backup_profile_artifacts(
    hmc: HMCClient, system_name_or_uuid: str
) -> BackupProfileInventory:
    """List a complete bounded inventory; failed/warning jobs have no inventory."""
    if not isinstance(system_name_or_uuid, str) or not system_name_or_uuid.strip():
        raise ValueError("system_name_or_uuid must identify one managed system")
    system_uuid = await resolve_system_uuid(hmc, system_name_or_uuid)
    submitted = await hmc.submit_job(
        f"/rest/api/uom/ManagedSystem/{system_uuid}/do/ListBackupProfileData",
        build_job_request("ListBackupProfileData", "ManagedSystem"),
    )
    terminal = await wait_for_submitted_job(hmc, submitted, True, 300, 5)
    outcome = job_outcome(
        job_identifier(submitted or {}) or "ListBackupProfileData", terminal
    )
    artifacts = (
        _parse_inventory(terminal)
        if outcome.status in SUCCESSFUL_JOB_STATUSES
        else None
    )
    return BackupProfileInventory(system_uuid, _project(outcome), artifacts)


async def delete_backup_profile_artifact(
    hmc: HMCClient, system_name_or_uuid: str, file_name: str
) -> BackupProfileDeletion:
    """Delete exactly one listed backup, then observe absence without retrying."""
    _validate_file_name(file_name)
    before = await list_backup_profile_artifacts(hmc, system_name_or_uuid)
    if before.artifacts is None:
        raise HMCError(
            "Backup deletion requires a complete successful pre-list; inspect the inventory job before retrying"
        )
    if file_name not in {artifact.file_name for artifact in before.artifacts}:
        raise HMCError(
            "Requested backup file is missing from the selected managed-system inventory; no deletion submitted"
        )
    system_uuid = before.system_uuid
    submitted = await hmc.submit_job(
        f"/rest/api/uom/ManagedSystem/{system_uuid}/do/DeleteBackupProfileData",
        build_job_request(
            "DeleteBackupProfileData", "ManagedSystem", {"file": file_name}
        ),
    )
    terminal = await wait_for_submitted_job(hmc, submitted, True, 300, 5)
    outcome = _project(
        job_outcome(
            job_identifier(submitted or {}) or "DeleteBackupProfileData", terminal
        )
    )
    if outcome.status not in SUCCESSFUL_JOB_STATUSES:
        return BackupProfileDeletion(system_uuid, file_name, outcome, "not-checked")
    try:
        after = await list_backup_profile_artifacts(hmc, system_uuid)
        if after.artifacts is None:
            return BackupProfileDeletion(
                system_uuid,
                file_name,
                outcome,
                "unknown",
                _detail(
                    after.job.error
                    or "Inventory readback did not complete successfully"
                ),
            )
    except (HMCError, ValueError) as exc:
        return BackupProfileDeletion(
            system_uuid, file_name, outcome, "unknown", _detail(str(exc))
        )
    present = file_name in {artifact.file_name for artifact in after.artifacts}
    return BackupProfileDeletion(
        system_uuid, file_name, outcome, "present" if present else "absent"
    )
