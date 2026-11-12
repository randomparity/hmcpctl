"""Contract tests for the supported cross-process job-polling operations (ADR 0093).

The operations exist so a consumer can persist a job identifier in one process and
poll it from another. Every test here therefore passes plain strings — never a job
object, a live coroutine, or a client instance carried over from submission.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
import pytest
from conftest import live_response, make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.jobs import (
    JobOutcome,
    job_identifier,
    job_outcome,
    wait_for_submitted_job,
)
from hmcpctl.operations.jobs import (
    get_job,
    wait_for_job,
)

# Every job body here is the captured V10R3 read of a finished PowerOn job
# (#1161), with only its Status and, where a test needs one, its SELF links
# replaced. _SELF_HREF is a stored `jobs/{JobID}` link whose path differs from
# the global path built from _JOB_ID, so the tests can tell which one was read
# (the per-operation link form issue #95 accepted is refused since #1202).
_COMPLETED_PATH, _COMPLETED = live_response("rest-job-completed-ok")
_JOB_ID = _COMPLETED_PATH.rsplit("/", 1)[-1]
_GLOBAL_PATH = _COMPLETED_PATH
_SELF_HREF = "/rest/api/uom/jobs/1787837921299"
_SUBMIT_PATH, _SUBMITTED = live_response("rest-poweron-submit")
_CAPTURED_LINKS = re.compile(r'    <link rel="SELF" href="[^"]*"/>\n')


def _job_entry(status: str, *, self_href: str | None = None) -> str:
    """The captured job read with *status*, and *self_href* as its only SELF link."""
    body = _COMPLETED.text.replace(">COMPLETED_OK<", f">{status}<")
    if self_href is not None:
        body = _CAPTURED_LINKS.sub("", body).replace(
            '    <link rel="MANAGEMENT_CONSOLE"',
            f'    <link rel="SELF" href="{self_href}"/>\n'
            '    <link rel="MANAGEMENT_CONSOLE"',
        )
    return body


def _no_such_job() -> httpx.Response:
    """The captured V10R3 answer for a job the HMC does not have (404 REST0005)."""
    return live_response("rest-job-not-found")[1]


@pytest.mark.asyncio
async def test_wait_for_job_polls_an_identifier_persisted_across_a_process_restart(
    mock_hmc,
) -> None:
    """The critical acceptance path: only strings survive from submission to poll.

    The submitting client is closed before the polling client is constructed, and
    the only thing crossing between them is a JSON round trip — the stand-in for the
    database a restarted worker reads its handle back from.
    """
    mock_hmc.put(_SUBMIT_PATH).mock(return_value=_SUBMITTED)
    poll = mock_hmc.get(_GLOBAL_PATH).mock(return_value=_COMPLETED)

    async with HMCClient(make_config()) as submitting:
        submitted = await submitting.submit_job(_SUBMIT_PATH, "<JobRequest/>")
    # The entry UUID is not a handle: V10R3 refuses it on jobs/{id} (#1160).
    assert submitted["UUID"] != job_identifier(submitted)
    stored = json.dumps(
        {"job_id": job_identifier(submitted), "job_href": submitted["link"]}
    )

    handle = json.loads(stored)
    assert isinstance(handle["job_id"], str) and isinstance(handle["job_href"], str)

    async with HMCClient(make_config()) as polling:
        outcome = await wait_for_job(
            polling,
            handle["job_id"],
            job_href=handle["job_href"],
            timeout_seconds=30,
            poll_interval=1,
        )

    assert poll.called
    assert isinstance(outcome, JobOutcome)
    assert outcome.found is True
    assert outcome.status == "COMPLETED_OK"
    assert outcome.timed_out is False
    assert outcome.error is None
    assert outcome.job_id == _JOB_ID


@pytest.mark.asyncio
async def test_wait_for_job_returns_a_terminal_job_after_one_poll(mock_hmc) -> None:
    """An already-finished job returns its outcome instead of blocking."""
    route = mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(200, text=_job_entry("COMPLETED_OK"))
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(
            hmc, _JOB_ID, timeout_seconds=300, poll_interval=60
        )

    assert route.call_count == 1
    assert outcome.status == "COMPLETED_OK"
    assert outcome.timed_out is False
    assert outcome.found is True


@pytest.mark.asyncio
async def test_get_job_reports_a_reaped_identifier_as_not_found(mock_hmc) -> None:
    """A 404 becomes a documented outcome, not an opaque transport error."""
    mock_hmc.get(_GLOBAL_PATH).mock(return_value=_no_such_job())

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _JOB_ID)

    assert outcome.found is False
    assert outcome.status is None
    assert outcome.job is None
    assert outcome.job_id == _JOB_ID


@pytest.mark.asyncio
async def test_get_job_reports_an_empty_job_response_as_not_found(
    mock_hmc, caplog
) -> None:
    """An HMC that answers with no job entry is the same observation as a 404.

    It is also the same destructive signal, so it is as loud: an HMC answering
    the jobs path with no content reports every job gone, forever.
    """
    mock_hmc.get(_GLOBAL_PATH).mock(return_value=httpx.Response(204))

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            outcome = await get_job(hmc, _JOB_ID)

    assert outcome.found is False
    assert any(
        record.levelno == logging.WARNING and _JOB_ID in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_get_job_does_not_hand_back_a_link_on_a_missing_job(mock_hmc) -> None:
    """A found=False outcome carries no handle: nothing resolved to persist."""
    mock_hmc.get(_SELF_HREF).mock(return_value=_no_such_job())
    mock_hmc.get(_GLOBAL_PATH).mock(return_value=_no_such_job())

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)

    assert (outcome.found, outcome.job_href) == (False, None)


@pytest.mark.asyncio
async def test_get_job_distinguishes_a_running_job_from_a_gone_one(mock_hmc) -> None:
    """The distinction a restarted worker needs: still running versus gone."""
    running_route = mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(200, text=_job_entry("RUNNING"))
    )

    async with HMCClient(make_config()) as hmc:
        running = await get_job(hmc, _JOB_ID)

    assert running_route.called
    assert (running.found, running.status) == (True, "RUNNING")
    assert running.timed_out is True


@pytest.mark.asyncio
async def test_wait_for_job_confirms_a_disappearance_then_stops(mock_hmc) -> None:
    """A vanished job ends the wait after one confirming read, not at the deadline."""
    route = mock_hmc.get(_GLOBAL_PATH).mock(
        side_effect=[
            httpx.Response(200, text=_job_entry("RUNNING")),
            _no_such_job(),
            _no_such_job(),
        ]
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(
            hmc, _JOB_ID, timeout_seconds=3600, poll_interval=1
        )

    assert route.call_count == 3
    assert outcome.found is False


@pytest.mark.asyncio
async def test_wait_for_job_does_not_report_a_momentary_404_as_a_vanished_job(
    mock_hmc,
) -> None:
    """One 404 is the only failure here that returns instead of raising.

    A proxy reload or a failover must not be handed to a consumer as "your
    90-minute install is gone", because the documented re-call recovery cannot
    undo an answer the caller has already acted on.
    """
    route = mock_hmc.get(_GLOBAL_PATH).mock(
        side_effect=[
            httpx.Response(200, text=_job_entry("RUNNING")),
            _no_such_job(),
            httpx.Response(200, text=_job_entry("COMPLETED_OK")),
        ]
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(
            hmc, _JOB_ID, timeout_seconds=3600, poll_interval=1
        )

    assert route.call_count == 3
    assert (outcome.found, outcome.status) == (True, "COMPLETED_OK")


@pytest.mark.asyncio
async def test_wait_for_job_reports_a_first_read_miss_immediately(mock_hmc) -> None:
    """With no earlier observation to contradict, one missing read is the answer."""
    route = mock_hmc.get(_GLOBAL_PATH).mock(return_value=_no_such_job())

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(
            hmc, _JOB_ID, timeout_seconds=3600, poll_interval=1
        )

    assert route.call_count == 1
    assert outcome.found is False


@pytest.mark.asyncio
async def test_wait_for_job_reports_a_still_running_job_at_the_deadline(
    mock_hmc,
) -> None:
    """``timeout_seconds=0`` performs exactly one poll and reports the timeout."""
    route = mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(200, text=_job_entry("RUNNING"))
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(hmc, _JOB_ID, timeout_seconds=0, poll_interval=5)

    assert route.call_count == 1
    assert outcome.found is True
    assert outcome.timed_out is True
    assert outcome.status == "RUNNING"


@pytest.mark.asyncio
async def test_get_job_propagates_a_transport_failure_that_is_not_a_missing_job(
    mock_hmc,
) -> None:
    """Only 404 is translated; every other HMC failure still raises."""
    mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(500, text="Internal error")
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError):
            await get_job(hmc, _JOB_ID)


@pytest.mark.asyncio
async def test_get_job_uses_the_persisted_self_link_when_supplied(mock_hmc) -> None:
    """A persisted SELF link addresses the job on firmware that cannot resolve the UUID."""
    href_route = mock_hmc.get(_SELF_HREF).mock(
        return_value=httpx.Response(200, text=_job_entry("COMPLETED_OK"))
    )
    global_route = mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=live_response("rest-job-entry-uuid-refused")[1]
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)

    assert href_route.called
    assert not global_route.called
    assert outcome.job_href == _SELF_HREF


@pytest.mark.asyncio
async def test_get_job_echoes_the_handle_needed_to_poll_again(mock_hmc) -> None:
    """The outcome carries both persistable strings, so the handle round-trips."""
    mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(
            200, text=_job_entry("RUNNING", self_href=_SELF_HREF)
        )
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _JOB_ID)

    assert (outcome.job_id, outcome.job_href) == (_JOB_ID, _SELF_HREF)


@pytest.mark.asyncio
async def test_get_job_keeps_the_link_the_caller_polled_with(mock_hmc) -> None:
    """A stored handle does not rotate to an untried link the response advertises."""
    other_link = "/rest/api/uom/jobs/1787837921298"
    mock_hmc.get(_SELF_HREF).mock(
        return_value=httpx.Response(
            200, text=_job_entry("RUNNING", self_href=other_link)
        )
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)

    assert outcome.job_href == _SELF_HREF


@pytest.mark.asyncio
async def test_get_job_warns_with_the_discarded_detail_when_a_job_is_missing(
    mock_hmc, caplog
) -> None:
    """The one place an error becomes an ordinary value leaves a loud record.

    A deployment whose job path 404s answers ``found=False`` for every job, and a
    consumer acts on that signal, so it is not an INFO-level event.
    """
    mock_hmc.get(_GLOBAL_PATH).mock(return_value=_no_such_job())

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            assert (await get_job(hmc, _JOB_ID)).found is False

    assert any(
        record.levelno == logging.WARNING
        and _JOB_ID in record.getMessage()
        and "REST0005 No such Job" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_get_job_confirms_a_stale_link_against_the_global_path(
    mock_hmc, caplog
) -> None:
    """A SELF link can stop resolving while the job is fine; do not call that gone."""
    stale = mock_hmc.get(_SELF_HREF).mock(return_value=_no_such_job())
    fallback = mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(
            200, text=_job_entry("RUNNING", self_href=_SELF_HREF)
        )
    )

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            outcome = await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)

    assert stale.called and fallback.called
    assert (outcome.found, outcome.status) == (True, "RUNNING")
    assert outcome.job_href is None, "a link proved stale is not handed back"
    assert any("no longer resolves" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_job_drops_a_stale_link_with_an_equivalent_absolute_spelling(
    mock_hmc,
) -> None:
    """A retired resource stays retired when the response makes its link absolute."""
    mock_hmc.get(_SELF_HREF).mock(return_value=_no_such_job())
    absolute_self_href = f"https://hmc.test:443{_SELF_HREF}"
    mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(
            200, text=_job_entry("RUNNING", self_href=absolute_self_href)
        )
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)

    assert (outcome.found, outcome.job_href) == (True, None)


@pytest.mark.asyncio
async def test_wait_for_job_drops_a_stale_link_after_confirming_it_once(
    mock_hmc, caplog
) -> None:
    """The confirming read and its warning happen once, not on every poll."""
    stale = mock_hmc.get(_SELF_HREF).mock(return_value=_no_such_job())
    fallback = mock_hmc.get(_GLOBAL_PATH).mock(
        side_effect=[
            httpx.Response(200, text=_job_entry("RUNNING", self_href=_SELF_HREF)),
            httpx.Response(200, text=_job_entry("COMPLETED_OK", self_href=_SELF_HREF)),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            outcome = await wait_for_job(
                hmc,
                _JOB_ID,
                job_href=_SELF_HREF,
                timeout_seconds=3600,
                poll_interval=1,
            )

    assert stale.call_count == 1, "the stale link must not be retried every poll"
    assert fallback.call_count == 2
    assert outcome.status == "COMPLETED_OK"
    assert outcome.job_href is None, (
        "a link retired earlier in the wait must not come back on a later poll"
    )
    assert (
        len([r for r in caplog.records if "no longer resolves" in r.getMessage()]) == 1
    )


# The envelopes a V10R3 HMC returns for an LPAR power job (live capture at
# 2281afd2, issue #1160). The submission entry's SELF link is the one-segment
# `/rest/api/uom/jobs/{JobID}`. Every read of that path returns a *different*
# Atom `<id>` from the submission's, a malformed `nulljobs/{JobID}` SELF link,
# and a SELF link `/rest/api/uom/jobs/{JobID}/{uuid}` whose trailing UUID changes
# on every read. The JobID is the only identifier stable across all of them; the
# global path answers an entry UUID with HTTP 406, and the two-segment link with
# HTTP 400 REST000B.
_REAL_JOB_ID = "1787837921264"
_SUBMIT_ENTRY_UUID = "82e81d12-0000-4000-8000-000000000001"
_READ_ENTRY_UUID = "2dd9cdd8-0000-4000-8000-000000000002"
_REAL_JOB_PATH = f"/rest/api/uom/jobs/{_REAL_JOB_ID}"
_REAL_JOB_HREF = f"https://hmc.example.test{_REAL_JOB_PATH}"
_REAL_POWER_ON = "/rest/api/uom/LogicalPartition/lpar-uuid/do/PowerOn"


def _real_job_entry(status: str, entry_uuid: str, *links: str) -> str:
    link_elements = "".join(f'  <link rel="SELF" href="{link}"/>\n' for link in links)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<entry xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <id>{entry_uuid}</id>\n"
        "  <title>JobResponse</title>\n"
        f"{link_elements}"
        '  <content type="application/vnd.ibm.powervm.web+xml; type=JobResponse">\n'
        '    <JobResponse:JobResponse xmlns:JobResponse="http://www.ibm.com/xmlns/'
        'systems/power/firmware/web/mc/2012_10/" xmlns="http://www.ibm.com/xmlns/'
        'systems/power/firmware/web/mc/2012_10/" schemaVersion="V1_0">\n'
        '      <RequestURL href="LogicalPartition/lpar-uuid/do/PowerOn" rel="via"/>\n'
        "      <TargetUuid>lpar-uuid</TargetUuid>\n"
        f"      <JobID>{_REAL_JOB_ID}</JobID>\n"
        f"      <Status>{status}</Status>\n"
        "    </JobResponse:JobResponse>\n"
        "  </content>\n"
        "</entry>\n"
    )


def _submission_entry() -> str:
    return _real_job_entry("NOT_STARTED", _SUBMIT_ENTRY_UUID, _REAL_JOB_HREF)


def _read_entry(status: str, read_uuid: str) -> str:
    return _real_job_entry(
        status,
        _READ_ENTRY_UUID,
        f"nulljobs/{_REAL_JOB_ID}",
        f"{_REAL_JOB_HREF}/{read_uuid}",
    )


def _read_link(read_uuid: str) -> str:
    return f"{_REAL_JOB_HREF}/{read_uuid}"


def _refuse_what_the_hmc_refuses(mock_hmc) -> None:
    for entry_uuid in (_SUBMIT_ENTRY_UUID, _READ_ENTRY_UUID):
        mock_hmc.get(f"/rest/api/uom/jobs/{entry_uuid}").mock(
            return_value=httpx.Response(406, text="Console Internal Error")
        )
    mock_hmc.get(url__regex=rf"{_REAL_JOB_PATH}/[^/]+$").mock(
        return_value=httpx.Response(400, text="REST000B The URL is not valid.")
    )


@pytest.mark.asyncio
async def test_a_submitted_real_job_hands_out_a_handle_every_later_read_resolves(
    mock_hmc, caplog
) -> None:
    """Submit, wait, persist, restart, poll: every read goes through the JobID path."""
    _refuse_what_the_hmc_refuses(mock_hmc)
    mock_hmc.put(_REAL_POWER_ON).mock(
        return_value=httpx.Response(200, text=_submission_entry())
    )
    by_job_id = mock_hmc.get(_REAL_JOB_PATH).mock(
        side_effect=[
            httpx.Response(
                200, text=_read_entry("RUNNING", "ae9d19a9-0000-4000-8000-00000000000a")
            ),
            httpx.Response(
                200,
                text=_read_entry(
                    "COMPLETED_OK", "25fed53a-0000-4000-8000-00000000000b"
                ),
            ),
            httpx.Response(
                200,
                text=_read_entry(
                    "COMPLETED_OK", "25fed53a-0000-4000-8000-00000000000c"
                ),
            ),
        ]
    )

    async with HMCClient(make_config()) as submitting:
        submitted = await submitting.submit_job(_REAL_POWER_ON, "<JobRequest/>")
        waited = await wait_for_submitted_job(submitting, submitted, True, 30, 1)
        outcome = job_outcome(job_identifier(submitted) or "", waited)
    handle = json.loads(
        json.dumps({"job_id": outcome.job_id, "job_href": outcome.job_href})
    )

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as polling:
            polled = await get_job(
                polling, handle["job_id"], job_href=handle["job_href"]
            )

    assert handle == {"job_id": _REAL_JOB_ID, "job_href": _REAL_JOB_HREF}
    assert by_job_id.call_count == 3
    assert (polled.found, polled.status, polled.job_id) == (
        True,
        "COMPLETED_OK",
        _REAL_JOB_ID,
    )
    assert polled.job_href == _REAL_JOB_HREF
    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_read_reports_the_stable_job_href_not_the_per_read_link(
    mock_hmc,
) -> None:
    """Persisting from any read stores one link, not one that changes every poll."""
    _refuse_what_the_hmc_refuses(mock_hmc)
    mock_hmc.get(_REAL_JOB_PATH).mock(
        return_value=httpx.Response(
            200,
            text=_read_entry("COMPLETED_OK", "65680cb7-0000-4000-8000-00000000000d"),
        )
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(hmc, _REAL_JOB_ID, timeout_seconds=0)

    assert (outcome.found, outcome.status, outcome.timed_out) == (
        True,
        "COMPLETED_OK",
        False,
    )
    assert (outcome.job_id, outcome.job_href) == (_REAL_JOB_ID, _REAL_JOB_HREF)


@pytest.mark.asyncio
async def test_get_job_resolves_a_supplied_per_read_link_and_echoes_it_stable(
    mock_hmc,
) -> None:
    """A link persisted from a read before this fix resolves; the echo is stable."""
    _refuse_what_the_hmc_refuses(mock_hmc)
    stored_link = _read_link("65680cb7-0000-4000-8000-00000000000e")
    mock_hmc.get(_REAL_JOB_PATH).mock(
        return_value=httpx.Response(
            200,
            text=_read_entry("COMPLETED_OK", "65680cb7-0000-4000-8000-00000000000f"),
        )
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _REAL_JOB_ID, job_href=stored_link)

    assert (outcome.found, outcome.job_id) == (True, _REAL_JOB_ID)
    assert outcome.job_href == _REAL_JOB_HREF


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_uuid", [_SUBMIT_ENTRY_UUID, _READ_ENTRY_UUID])
async def test_get_job_explains_a_legacy_entry_uuid_handle_paired_with_its_link(
    mock_hmc, caplog, stored_uuid
) -> None:
    """An entry UUID stored by an earlier release reads the right job, and says why."""
    _refuse_what_the_hmc_refuses(mock_hmc)
    mock_hmc.get(_REAL_JOB_PATH).mock(
        return_value=httpx.Response(
            200,
            text=_read_entry("COMPLETED_OK", "65680cb7-0000-4000-8000-000000000010"),
        )
    )

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            outcome = await get_job(hmc, stored_uuid, job_href=_REAL_JOB_HREF)

    assert outcome.job_id == _REAL_JOB_ID
    [warning] = [
        r.getMessage() for r in caplog.records if "returned job" in r.getMessage()
    ]
    assert stored_uuid in warning and _REAL_JOB_ID in warning
    assert "entry's UUID" in warning


# --------------------------------------------------------------------------- #
# Captured V10R3 refusals through the production job paths (#1161; ADR 0093's
# #1174 amendment: only a 404 means missing, and a 400 REST000E propagates).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_captured_failed_activation_is_a_terminal_error(mock_hmc) -> None:
    """A COMPLETED_WITH_ERROR read carries the HSCL refusal as the outcome's error."""
    path, response = live_response("rest-job-completed-with-error")
    mock_hmc.get(path).mock(return_value=response)

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, path.rsplit("/", 1)[-1])

    assert (outcome.found, outcome.timed_out) == (True, False)
    assert outcome.job_id == path.rsplit("/", 1)[-1]
    assert outcome.status == "COMPLETED_WITH_ERROR"
    assert outcome.error is not None and outcome.error.startswith("HSCL3681 ")
    assert outcome.job_href == f"https://hmc.test:443{path}"


@pytest.mark.asyncio
async def test_captured_no_such_job_reads_as_found_false(mock_hmc) -> None:
    path, response = live_response("rest-job-not-found")
    route = mock_hmc.get(path).mock(return_value=response)

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(
            hmc, path.rsplit("/", 1)[-1], timeout_seconds=300, poll_interval=5
        )

    assert (outcome.found, outcome.job, outcome.status) == (False, None, None)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_captured_entry_uuid_refusal_is_an_error_not_a_missing_job(
    mock_hmc,
) -> None:
    """The HMC refuses an entry UUID's URL form for a live job; that is not absence."""
    path, response = live_response("rest-job-entry-uuid-refused")
    mock_hmc.get(path).mock(return_value=response)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as excinfo:
            await get_job(hmc, path.rsplit("/", 1)[-1])

    assert excinfo.value.status_code == 400
    # The HMC's own words reach the caller; it says the URL is invalid, and
    # nothing about licences or PTF levels (#1202).
    assert "REST000E Unrecognized root REST type of jobs." in str(excinfo.value)
    assert excinfo.value.body == HMCError("sanitized", 400, response.text).body
    assert "REST000E" in excinfo.value.body
    assert "PTF" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_captured_entry_uuid_refusal_on_the_confirming_read_propagates(
    mock_hmc,
) -> None:
    """A stale link's 404 is not confirmed by a REST000E on the global read (#1174)."""
    path, refused = live_response("rest-job-entry-uuid-refused")
    _, missing = live_response("rest-job-not-found")
    mock_hmc.get(_SELF_HREF).mock(return_value=missing)
    global_route = mock_hmc.get(path).mock(return_value=refused)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as excinfo:
            await get_job(hmc, path.rsplit("/", 1)[-1], job_href=_SELF_HREF)

    assert global_route.called
    assert excinfo.value.status_code == 400
