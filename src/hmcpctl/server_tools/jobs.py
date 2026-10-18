"""MCP tools for HMC asynchronous job inspection and waiting."""

from __future__ import annotations

from .._app import with_client
from ..jobs import JobOutcome
from ..operations import jobs as operations_jobs
from ..tool_registry import tool_module

tool, register_tools, tool_security = tool_module()


# Not exhaustive: `job_href` is a caller-supplied URI whose path replaces the
# `job_id` selector outright — `client.get_job_entry` fetches `urlparse(job_href).path`
# and never looks at `job_id`. A `targets` table would therefore authorize one
# job identity while the server reads another, so ADR 0039 grants this tool only
# under `targets = "all-targets"`. ADR 0036 already noted that a job identifier
# is minted by the HMC at runtime and so cannot usefully appear in an allowlist.
@tool(
    effect="read",
    operation="job.get",
    target_kind="job",
    exhaustive_targets=False,
)
def hmc_get_job(
    job_id: str,
    job_href: str | None = None,
    profile: str | None = None,
) -> JobOutcome:
    """Inspect one HMC job and return its normalized outcome and original entry.

    Returns the normalized JobOutcome with the original HMC entry in ``job``.
    Read ``found`` first: a missing entry returns ``found`` false and ``job`` null,
    with null status and ``timed_out`` true; that timeout carries no information.
    Every non-missing HMC failure still raises. Absence on this single read is
    unconfirmed, and does not prove whether the work happened. Re-read by
    ``job_id`` alone before acting on absence (ADR 0093, refined by ADR 0239).

    An empty identifier, a bare dot, or one carrying a path, query, fragment,
    percent, or interior whitespace character is rejected outright rather than
    reported as a missing job; surrounding whitespace is trimmed. That check runs
    even when ``job_href`` is supplied.

    A ``job_href`` that stops resolving is re-read against the global jobs path
    before the job is called gone. Persist the returned ``job_href``: a stale
    link is retired, even if the original entry in ``job`` still advertises it.
    A null handle on a found job means to drop the stored link. An echoed link
    is validated only by its requested path; its host, query and fragment remain
    unattested.

    A supplied link decides **which job is read**, so a mispaired handle returns
    the other job. Compare the response-derived ``job_id`` with the requested
    identifier; never overwrite a stored identifier on a mismatch. An earlier
    release's entry UUID can differ legitimately from the response JobID.

    Persist the JobID and the stable ``/rest/api/uom/jobs/{JobID}`` ``job_href`` that
    submitting tools return. The Atom entry UUID is not a usable ``job_id`` on V10R3:
    the global jobs path refuses it (HTTP 406 to this client's web+xml Accept). A UUID stored by an earlier
    release reads only through its ``job_href``.

    Args:
        job_id: JobID returned when the job was submitted.
        job_href: Optional ``/rest/api/uom/jobs/{JobID}`` submission SELF link; needed
            only when job_id is an entry UUID stored by an earlier release.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    async def operation(hmc):
        return await operations_jobs.get_job(hmc, job_id, job_href=job_href)

    return with_client(operation, profile=profile)


# Not exhaustive: `job_href` is a caller-supplied URI whose path replaces the
# `job_id` selector outright — `client.get_job_entry` fetches `urlparse(job_href).path`
# and never looks at `job_id`. A `targets` table would therefore authorize one
# job identity while the server reads another, so ADR 0039 grants this tool only
# under `targets = "all-targets"`. ADR 0036 already noted that a job identifier
# is minted by the HMC at runtime and so cannot usefully appear in an allowlist.
@tool(
    effect="read",
    operation="job.wait",
    target_kind="job",
    exhaustive_targets=False,
)
def hmc_wait_for_job(
    job_id: str,
    timeout_seconds: int = 300,
    poll_interval: int = 5,
    job_href: str | None = None,
    profile: str | None = None,
) -> JobOutcome:
    """Poll a job and return its normalized status, timeout, and error outcome.

    Polling stops at CANCELED_BEFORE_START, CANCELED_WHILE_RUNNING, COMPLETED,
    COMPLETED_OK, COMPLETED_WITH_ERROR, COMPLETED_WITH_WARNINGS,
    FAILED_BEFORE_COMPLETION, FAILED_BEFORE_COMPLETION_RETRY, or
    FAILED_TO_START. If the timeout expires first, the last observed job is
    returned with ``timed_out`` set to true.

    **Read ``found`` before ``timed_out``.** A job the HMC no longer has — reaped,
    deleted, or never present — returns ``found`` false immediately rather than
    raising, with a null ``status`` and ``timed_out`` true. On a ``found`` false
    outcome ``timed_out`` carries no information and never means "still running";
    only ``found`` true with ``timed_out`` true does, and a null ``status`` there
    means the entry carried no readable Status. Every HMC failure other than a
    missing job still raises ``HMCError``.

    ``found`` false is unconfirmed on the first poll: only a job that vanishes
    after this wait has seen it alive is re-read before being reported gone, so
    one momentary or empty response at the start ends the wait immediately. It is
    also not proof the work did or did not happen — confirm that against the
    affected resource, not the job record. Two shapes are not a reaped job at all:
    ``found`` false for *every* identifier is a deployment whose jobs path is
    absent, and ``found`` false for one job polled with a ``job_href`` can be a
    link that stopped resolving while the job runs. Re-read by ``job_id`` alone
    before acting on absence (ADR 0093).

    ``timeout_seconds`` is a soft bound: an owed confirming re-read waits a full
    ``poll_interval`` when that interval fits within the timeout, even if the
    deadline is nearer. A larger interval is capped at ``timeout_seconds``, so the
    poll schedule extends by at most one interval and at most one timeout. Time
    awaiting HMC reads remains subject to the client's separate HTTP timeout.
    ``timeout_seconds=0`` still performs one poll and owes no confirming read.

    ``job_href`` on the result is the link to persist for the next call — the one
    you passed when it resolved, otherwise the successful read's own link, null
    when nothing resolved. If you passed a link and get ``found`` true with a null
    ``job_href``, that link was retired; drop it. An echoed link is your cleaned
    input whose parsed path was validated and requested. Host, query, and fragment
    remain unattested, so do not dereference it as something the HMC attested.

    A supplied ``job_href`` decides **which job is read**: its path is fetched
    directly and checked only for addressing a job resource, so a mispaired handle
    returns the *other* job. Compare the returned ``job_id`` against the
    identifier you passed.

    An empty identifier, a bare dot, or one carrying a path, query, fragment,
    percent, or interior whitespace character is rejected outright rather than
    reported as ``found`` false; surrounding whitespace is trimmed. That check
    runs even when ``job_href`` is supplied.

    The same two fields appear on the outcomes returned by the submit-and-wait
    tools (the migrate, remote-restart and power tools), where they describe a
    *submission* — there ``found`` false means "this submission returned no job
    entry", not "the HMC no longer has this job".

    Args:
        job_id: JobID returned when the job was submitted.
        timeout_seconds: Maximum polling duration in seconds; zero performs one poll.
        poll_interval: Seconds between polls; must be greater than zero.
        job_href: Optional ``/rest/api/uom/jobs/{JobID}`` submission SELF link; needed
            only when job_id is an entry UUID stored by an earlier release.
        profile: Optional configured HMC profile name; uses the default when omitted.
    """

    return with_client(
        lambda hmc: operations_jobs.wait_for_job(
            hmc,
            job_id,
            job_href=job_href,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
        ),
        profile=profile,
    )
