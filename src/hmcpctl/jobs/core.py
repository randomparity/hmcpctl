"""HMC job outcome normalization and polling helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse, urlsplit, urlunsplit

from ..errors import HMCError

DEFAULT_JOB_TIMEOUT_SECONDS = 300
DEFAULT_JOB_POLL_INTERVAL = 5
# The documented job statuses (docs/refs/hmc-rest-api-p10/016-job-status.md);
# NOT_STARTED and RUNNING are the two non-terminal ones. Bare COMPLETED is the
# success status the ListManagementConsoleUpdates and ListStorageMediaDevices
# job pages document instead of COMPLETED_OK. A status outside this set is not
# terminal, so the wait keeps polling to its deadline (#1202).
TERMINAL_JOB_STATUSES = frozenset(
    {
        "CANCELED_BEFORE_START",
        "CANCELED_WHILE_RUNNING",
        "COMPLETED",
        "COMPLETED_OK",
        "COMPLETED_WITH_ERROR",
        "COMPLETED_WITH_WARNINGS",
        "FAILED_BEFORE_COMPLETION",
        "FAILED_BEFORE_COMPLETION_RETRY",
        "FAILED_TO_START",
    }
)
SUCCESSFUL_JOB_STATUSES = frozenset({"COMPLETED", "COMPLETED_OK"})
FAILED_JOB_STATUSES = TERMINAL_JOB_STATUSES - SUCCESSFUL_JOB_STATUSES

# The SELF link a V10R3 HMC puts on every *read* of a job:
# `/rest/api/uom/jobs/{JobID}/{uuid}` or `/rest/api/templates/jobs/{JobID}/{uuid}`,
# whose trailing UUID changes on every read and which the HMC itself refuses as a
# request URL (HTTP 400 REST000B, live capture at 2281afd2, issue #1160). The
# submission's own SELF link is the one-segment `/rest/api/{uom,templates}/jobs/{JobID}`
# this reduces to. Only a UUID-shaped trailing segment matches; anything else is left
# for the job-path guard.
_READ_SELF_LINK = re.compile(
    r"(/rest/api/(?:uom|templates)/jobs/[^/?#]+)/[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}"
)


@dataclass(frozen=True)
class JobOutcome:
    """Normalized result of polling an HMC job."""

    job_id: str
    status: str | None
    timed_out: bool
    error: str | None
    job: dict[str, Any] | None
    found: bool
    job_href: str | None


class JobWaitClient(Protocol):
    async def wait_for_job_entry(
        self,
        job_id: str,
        timeout_seconds: int,
        poll_interval: int,
        *,
        job_href: str | None = None,
    ) -> dict[str, Any] | None: ...


def validate_wait_timing(wait: bool, timeout_seconds: int, poll_interval: int) -> None:
    if not wait:
        return
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be greater than or equal to 0")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than 0")


def canonical_job_path(path: str) -> str:
    """Reduce a read-side job SELF-link path to the stable JobID path it extends."""
    read_link = _READ_SELF_LINK.fullmatch(path)
    return read_link.group(1) if read_link else path


def job_identifier(job: dict[str, Any]) -> str | None:
    """Return the identifier the documented global jobs path resolves.

    ``Resource.JobID`` comes first. It is the one identifier stable from
    submission through every read: a V10R3 HMC gives the submission entry and
    the read entries different Atom UUIDs, and answers an entry UUID on
    ``/rest/api/uom/jobs/{id}`` with HTTP 406 to a web+xml Accept and 400 REST000E
    to ``*/*`` (issue #1160). The entry UUID,
    then the SELF link, are fallbacks for a response that carries no JobID; a
    read-side link yields its JobID segment, not its per-read last one.
    """
    resource = job.get("Resource")
    resource_id = resource.get("JobID") if isinstance(resource, dict) else None
    for candidate in (resource_id, job.get("UUID")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    link = job.get("link")
    if not isinstance(link, str) or not link.strip():
        return None
    path = canonical_job_path(urlparse(link.strip()).path.rstrip("/"))
    return path.rsplit("/", 1)[-1] or None


def _job_href(job: dict[str, Any] | None) -> str | None:
    """Return the entry's SELF link in the form worth persisting and polling.

    A read-side link is reduced to its JobID path, so a handle persisted from
    any read is the same stable link the submission carried. A relative link —
    the HMC's malformed ``nulljobs/{JobID}`` — addresses nothing the client will
    request, so it is not a handle at all.
    """
    link = (job or {}).get("link")
    if not isinstance(link, str) or not link.strip():
        return None
    if not urlsplit(link.strip()).path.startswith("/"):
        return None
    return canonical_job_href(link.strip())


def canonical_job_href(link: str) -> str:
    """Return *link* with a read-side job path reduced; any other link unchanged."""
    parts = urlsplit(link)
    path = canonical_job_path(parts.path)
    return link if path == parts.path else urlunsplit(parts._replace(path=path))


def _result_message(resource: dict[str, Any]) -> str | None:
    results = resource.get("Results")
    parameters = results.get("JobParameter", []) if isinstance(results, dict) else []
    if isinstance(parameters, dict):
        parameters = [parameters]
    messages = {}
    for parameter in parameters if isinstance(parameters, list) else []:
        name = parameter.get("ParameterName") if isinstance(parameter, dict) else None
        value = parameter.get("ParameterValue") if isinstance(parameter, dict) else None
        if (
            name in {"result", "detailedStatus", "ErrorData"}
            and isinstance(value, str)
            and value.strip()
            and name not in messages
        ):
            messages[name] = value.strip()
    return next(
        (
            messages[name]
            for name in ("ErrorData", "detailedStatus", "result")
            if name in messages
        ),
        None,
    )


def job_outcome(requested_id: str, job: dict[str, Any] | None) -> JobOutcome:
    resource = _job_resource(job)
    status_value = resource.get("Status")
    status = status_value.strip() if isinstance(status_value, str) else None
    return JobOutcome(
        (job_identifier(job) if job else None) or requested_id.strip(),
        status,
        status not in TERMINAL_JOB_STATUSES,
        _job_error(status, resource),
        job,
        job is not None,
        _job_href(job),
    )


def _job_resource(job: dict[str, Any] | None) -> dict[str, Any]:
    """Return the mapping-shaped HMC job resource, or an empty mapping."""
    resource = (job or {}).get("Resource")
    return resource if isinstance(resource, dict) else {}


def _job_error(status: str | None, resource: dict[str, Any]) -> str | None:
    """Select the actionable diagnostic for one terminal failed job status."""
    if status not in FAILED_JOB_STATUSES:
        return None
    return _result_message(resource) or f"Job ended with status {status}"


def vios_stdout(job: dict[str, Any] | None) -> str | None:
    resource = (job or {}).get("Resource")
    results = resource.get("Results") if isinstance(resource, dict) else None
    parameters = results.get("JobParameter", []) if isinstance(results, dict) else []
    if isinstance(parameters, dict):
        parameters = [parameters]
    for parameter in parameters if isinstance(parameters, list) else []:
        value = parameter.get("ParameterValue") if isinstance(parameter, dict) else None
        if (
            isinstance(parameter, dict)
            and parameter.get("ParameterName") == "stdOut"
            and isinstance(value, str)
            and value.strip()
        ):
            return value.strip()
    return None


async def wait_for_submitted_job(
    client: JobWaitClient,
    job: dict[str, Any] | None,
    wait: bool,
    timeout_seconds: int,
    poll_interval: int,
) -> dict[str, Any] | None:
    if not wait:
        return job
    validate_wait_timing(wait, timeout_seconds, poll_interval)
    if job is None:
        raise HMCError(
            "Cannot wait for the submitted HMC job: the submission returned no job resource"
        )
    identifier = job_identifier(job)
    if identifier is None:
        raise HMCError(
            "Cannot wait for the submitted HMC job: the response contained no usable JobID, UUID, or polling link"
        )
    return await client.wait_for_job_entry(
        identifier, timeout_seconds, poll_interval, job_href=_job_href(job)
    )
