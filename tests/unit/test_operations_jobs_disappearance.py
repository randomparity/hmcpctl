"""Job disappearance and confirmation behavior tests."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from conftest import live_response, make_config

from hmcpctl.client.core import HMCClient
from hmcpctl.errors import HMCError
from hmcpctl.operations.jobs import get_job, wait_for_job

_JOB_ID = "job-uuid-999"
_GLOBAL_PATH = f"/rest/api/uom/jobs/{_JOB_ID}"
# A handle an earlier release stored: an entry-UUID-shaped job_id with the
# submission's `jobs/{JobID}` SELF link (#1202 refuses per-operation links).
_SELF_HREF = "/rest/api/uom/jobs/1787837921266"
_SUBMIT_PATH = "/rest/api/uom/LogicalPartition/lpar-uuid/do/PowerOn"


def _job_entry(status: str, *, self_href: str | None = None) -> str:
    """One Atom job entry as the HMC returns it, optionally carrying its SELF link."""
    link = f'  <link rel="SELF" href="{self_href}"/>\n' if self_href else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<entry xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <id>urn:uuid:{_JOB_ID}</id>\n"
        "  <title>Job:PowerOn</title>\n"
        f"{link}"
        '  <content type="application/vnd.ibm.powervm.uom+xml">\n'
        '    <Job xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/'
        'mc/2012_10/">\n'
        f"      <JobID>{_JOB_ID}</JobID>\n"
        f"      <Status>{status}</Status>\n"
        "    </Job>\n"
        "  </content>\n"
        "</entry>\n"
    )


def _no_such_job() -> httpx.Response:
    """The captured V10R3 answer for a job the HMC does not have (404 REST0005)."""
    return live_response("rest-job-not-found")[1]


@pytest.mark.asyncio
async def test_get_job_reports_not_found_when_neither_path_has_the_job(
    mock_hmc,
) -> None:
    """Neither the persisted link nor the global path has it: the job is gone."""
    mock_hmc.get(_SELF_HREF).mock(return_value=_no_such_job())
    mock_hmc.get(_GLOBAL_PATH).mock(return_value=_no_such_job())

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)

    assert outcome.found is False


@pytest.mark.asyncio
async def test_get_job_warns_when_the_hmc_answers_about_a_different_job(
    mock_hmc, caplog
) -> None:
    """A mispaired handle reads another job; the outcome names the job read."""
    other_entry = _job_entry("COMPLETED_OK").replace(_JOB_ID, "some-other-job")
    mock_hmc.get(_SELF_HREF).mock(return_value=httpx.Response(200, text=other_entry))

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            outcome = await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)

    assert outcome.job_id == "some-other-job"
    assert any(
        record.levelno == logging.WARNING
        and "some-other-job" in record.getMessage()
        and _JOB_ID in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_get_job_does_not_warn_when_the_identifier_matches(
    mock_hmc, caplog
) -> None:
    """The mismatch warning must not fire on the ordinary path."""
    mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(200, text=_job_entry("RUNNING"))
    )

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            assert (await get_job(hmc, _JOB_ID)).job_id == _JOB_ID

    assert caplog.records == []


@pytest.mark.asyncio
async def test_get_job_does_not_warn_about_a_relabelled_job_without_a_link(
    mock_hmc, caplog
) -> None:
    """Without a link the path is built from the identifier, so no job was substituted.

    The HMC reporting its other identifier is a relabel, not a mispaired handle.
    """
    relabelled = _job_entry("RUNNING").replace(
        f"<JobID>{_JOB_ID}</JobID>", "<JobID>the-jobid-form</JobID>"
    )
    mock_hmc.get(_GLOBAL_PATH).mock(return_value=httpx.Response(200, text=relabelled))

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            assert (await get_job(hmc, _JOB_ID)).job_id == "the-jobid-form"

    assert caplog.records == []


@pytest.mark.asyncio
async def test_wait_for_job_warns_about_a_substituted_job_once_not_per_poll(
    mock_hmc, caplog
) -> None:
    """An hour of five-second polls must not emit hundreds of identical warnings."""
    other = _job_entry("RUNNING").replace(_JOB_ID, "some-other-job")
    finished = _job_entry("COMPLETED_OK").replace(_JOB_ID, "some-other-job")
    mock_hmc.get(_SELF_HREF).mock(
        side_effect=[
            httpx.Response(200, text=other),
            httpx.Response(200, text=finished),
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

    assert outcome.job_id == "some-other-job"
    assert len([r for r in caplog.records if "returned job" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_wait_for_job_logs_the_last_status_when_a_job_vanishes_mid_wait(
    mock_hmc, caplog
) -> None:
    """'Ran, then disappeared' is different evidence from 'never resolved'."""
    mock_hmc.get(_GLOBAL_PATH).mock(
        side_effect=[
            httpx.Response(200, text=_job_entry("RUNNING")),
            _no_such_job(),
            _no_such_job(),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            outcome = await wait_for_job(
                hmc, _JOB_ID, timeout_seconds=3600, poll_interval=1
            )

    assert outcome.found is False
    assert any(
        "disappeared during the wait" in r.getMessage() and "RUNNING" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupted",
    ["abc/def", "abc?x=1", "abc#frag", "abc%2e%2e", "abc def", "a\x00b", "a\x1fb"],
)
async def test_job_operations_reject_a_corrupted_persisted_identifier(
    mock_hmc, corrupted
) -> None:
    """A mangled stored handle fails loudly instead of reading as a reaped job."""
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="not an HMC job identifier"):
            await get_job(hmc, corrupted)
        with pytest.raises(ValueError, match="not an HMC job identifier"):
            await wait_for_job(hmc, corrupted)

    assert not [
        call
        for call in mock_hmc.calls
        if call.request.url.path.startswith("/rest/api/uom/")
    ]


@pytest.mark.asyncio
async def test_get_job_reports_a_blank_job_href_as_no_link(mock_hmc) -> None:
    """A blank link falls back to the global path and is not echoed as usable."""
    global_route = mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(200, text=_job_entry("RUNNING"))
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _JOB_ID, job_href="   ")

    assert global_route.called
    assert outcome.job_href is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [get_job, wait_for_job])
@pytest.mark.parametrize("control", ["\t", "\r", "\n"])
@pytest.mark.parametrize("position", ["leading", "embedded", "trailing"])
async def test_job_operations_reject_parser_deleted_job_href_controls(
    mock_hmc, operation, control: str, position: str
) -> None:
    """The validated and echoed link must have exactly the same spelling."""
    parts = {
        "leading": (control, _SELF_HREF),
        "embedded": (_SELF_HREF[:10], f"{control}{_SELF_HREF[10:]}"),
        "trailing": (_SELF_HREF, control),
    }
    job_href = "".join(parts[position])

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            ValueError, match="job_href must not contain TAB, CR, or LF"
        ):
            await operation(hmc, _JOB_ID, job_href=job_href)

    assert not [
        call
        for call in mock_hmc.calls
        if call.request.url.path.startswith("/rest/api/uom/")
    ]


@pytest.mark.asyncio
async def test_get_job_refuses_a_job_href_that_addresses_another_resource(
    mock_hmc,
) -> None:
    """The client's job-path guard still bounds a persisted link (ADR 0039)."""
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="job_href refused"):
            await get_job(hmc, _JOB_ID, job_href="/rest/api/uom/HmcUser/root")


@pytest.mark.asyncio
async def test_cancelling_the_waiter_leaves_the_session_usable(mock_hmc) -> None:
    """Cancellation unwinds nothing: no session mutation, and the client still works."""
    poll = mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(200, text=_job_entry("RUNNING"))
    )
    logoff = mock_hmc.delete("/rest/api/web/Logon")

    async with HMCClient(make_config()) as hmc:
        waiter = asyncio.create_task(
            wait_for_job(hmc, _JOB_ID, timeout_seconds=3600, poll_interval=600)
        )
        try:
            async with asyncio.timeout(5):
                while not poll.called:
                    if waiter.done():
                        await waiter
                        pytest.fail("job waiter finished before its first poll")
                    await asyncio.sleep(0.01)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

        assert hmc.is_logged_on
        assert not logoff.called
        assert (await get_job(hmc, _JOB_ID)).status == "RUNNING"


@pytest.mark.asyncio
@pytest.mark.parametrize("job_id", ["", "   "])
async def test_job_operations_reject_an_empty_identifier(mock_hmc, job_id) -> None:
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="job_id"):
            await get_job(hmc, job_id)
        with pytest.raises(ValueError, match="job_id"):
            await wait_for_job(hmc, job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout_seconds", "poll_interval", "message"),
    [(-1, 5, "timeout_seconds"), (10, 0, "poll_interval"), (10, -1, "poll_interval")],
)
async def test_wait_for_job_rejects_invalid_polling_settings(
    mock_hmc, timeout_seconds, poll_interval, message
) -> None:
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match=message):
            await wait_for_job(
                hmc,
                _JOB_ID,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )


@pytest.mark.asyncio
async def test_wait_for_job_confirms_a_disappearance_that_lands_on_the_deadline(
    mock_hmc,
) -> None:
    """The confirming read is owed even when the deadline arrives first.

    Every bounded wait ends at its deadline, and the documented usage chops a
    multi-hour install into many bounded waits, so skipping the confirmation
    there would put the hole where it fires routinely.
    """
    route = mock_hmc.get(_GLOBAL_PATH).mock(
        side_effect=[
            httpx.Response(200, text=_job_entry("RUNNING")),
            _no_such_job(),
            httpx.Response(200, text=_job_entry("COMPLETED_OK")),
        ]
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(hmc, _JOB_ID, timeout_seconds=1, poll_interval=1)

    assert route.call_count == 3
    assert (outcome.found, outcome.status) == (True, "COMPLETED_OK")


@pytest.mark.asyncio
async def test_wait_for_job_does_not_compress_the_confirmation_interval(
    monkeypatch, mock_hmc
) -> None:
    now = 0.0
    read_times: list[float] = []
    responses = [
        httpx.Response(200, text=_job_entry("RUNNING")),
        _no_such_job(),
        _no_such_job(),
    ]

    def respond(_: httpx.Request) -> httpx.Response:
        read_times.append(now)
        return responses.pop(0)

    mock_hmc.get(_GLOBAL_PATH).mock(side_effect=respond)
    loop = MagicMock()
    loop.time.side_effect = lambda: now

    async def advance(delay: float) -> None:
        nonlocal now
        now += delay

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=advance))

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(hmc, _JOB_ID, timeout_seconds=3, poll_interval=2)

    assert outcome.found is False
    assert read_times == [0.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_wait_for_job_caps_an_oversized_confirmation_interval(
    monkeypatch, mock_hmc
) -> None:
    now = 0.0
    read_times: list[float] = []
    responses = [
        httpx.Response(200, text=_job_entry("RUNNING")),
        _no_such_job(),
        _no_such_job(),
    ]

    def respond(_: httpx.Request) -> httpx.Response:
        read_times.append(now)
        return responses.pop(0)

    mock_hmc.get(_GLOBAL_PATH).mock(side_effect=respond)
    loop = MagicMock()
    loop.time.side_effect = lambda: now

    async def advance(delay: float) -> None:
        nonlocal now
        now += delay

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=advance))

    async with HMCClient(make_config()) as hmc:
        outcome = await wait_for_job(hmc, _JOB_ID, timeout_seconds=2, poll_interval=5)

    assert outcome.found is False
    assert read_times == [0.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_wait_for_job_warns_about_a_substituted_job_on_the_first_poll(
    mock_hmc, caplog
) -> None:
    """A wait on the wrong job must not stay silent until it ends.

    Cancellation and a non-404 ``HMCError`` both leave the loop without reaching
    its final return, so a warning deferred to loop exit can never be emitted.
    """
    other = _job_entry("RUNNING").replace(_JOB_ID, "some-other-job")
    mock_hmc.get(_SELF_HREF).mock(return_value=httpx.Response(200, text=other))

    with caplog.at_level(logging.WARNING, logger="hmcpctl.operations.jobs"):
        async with HMCClient(make_config()) as hmc:
            waiter = asyncio.create_task(
                wait_for_job(
                    hmc,
                    _JOB_ID,
                    job_href=_SELF_HREF,
                    timeout_seconds=3600,
                    poll_interval=600,
                )
            )
            try:
                async with asyncio.timeout(5):
                    while not any(
                        "returned job" in r.getMessage() for r in caplog.records
                    ):
                        if waiter.done():
                            await waiter
                            pytest.fail(
                                "job waiter finished before its mismatch warning"
                            )
                        await asyncio.sleep(0.01)
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

    assert len([r for r in caplog.records if "returned job" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_get_job_does_not_report_a_degraded_hmc_as_a_vanished_job(
    mock_hmc,
) -> None:
    """The confirming read second-sources absence, not failure.

    ``HMCTransportError`` subclasses ``HMCError``, so a base-class catch here
    would turn a socket reset into the one answer a consumer acts on
    destructively.
    """
    mock_hmc.get(_SELF_HREF).mock(return_value=_no_such_job())
    mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(503, text="Service unavailable")
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError):
            await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)


def _http_error_response(request_uri: str, message: str) -> str:
    """An HMC error body carrying the message text captured live on V10R3 (#1161)."""
    return (
        '<HttpErrorResponse xmlns="http://www.ibm.com/xmlns/systems/power'
        '/firmware/web/mc/2012_10/">'
        f"<RequestURI>{request_uri}</RequestURI>"
        f"<Message>{message}</Message>"
        "</HttpErrorResponse>"
    )


#: #1161 P4: V10R3's answer to a job URL whose form it does not accept.
_INVALID_JOB_URL = _http_error_response(
    _GLOBAL_PATH,
    "REST000B The URL presented to the Management Console REST Web Services is "
    "not valid.REST000E Unrecognized root REST type of jobs.",
)
#: #1161 P5, P11: V10R3's answer to a well-formed job URL whose job is gone.
_NO_SUCH_JOB = _http_error_response(_SELF_HREF, "REST0005 No such Job")


@pytest.mark.asyncio
async def test_get_job_does_not_report_an_invalid_job_url_as_absence(mock_hmc) -> None:
    """A confirming read refused as an invalid URL (400 REST000E) raises (#1174).

    On V10R3 only 404 REST0005 means a job is gone. REST000E says the request's
    URL form is invalid, which is no evidence about the job, so reporting it as
    found=False would tell a worker a live job is gone.
    """
    mock_hmc.get(_SELF_HREF).mock(return_value=httpx.Response(404, text=_NO_SUCH_JOB))
    mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(400, text=_INVALID_JOB_URL)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)

    assert raised.value.status_code == 400
    assert "REST000E" in str(raised.value)


@pytest.mark.asyncio
async def test_wait_for_job_does_not_report_an_invalid_job_url_as_absence(
    mock_hmc,
) -> None:
    """The primary read keeps the same rule: 400 REST000E propagates (#1174)."""
    mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(400, text=_INVALID_JOB_URL)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await wait_for_job(hmc, _JOB_ID, timeout_seconds=0)

    assert raised.value.status_code == 400


@pytest.mark.asyncio
async def test_get_job_reports_no_such_job_on_both_paths_as_absence(mock_hmc) -> None:
    """404 REST0005 on the link and on the global path is found=False (#1161 P5)."""
    mock_hmc.get(_SELF_HREF).mock(return_value=httpx.Response(404, text=_NO_SUCH_JOB))
    mock_hmc.get(_GLOBAL_PATH).mock(
        return_value=httpx.Response(
            404, text=_http_error_response(_GLOBAL_PATH, "REST0005 No such Job")
        )
    )

    async with HMCClient(make_config()) as hmc:
        outcome = await get_job(hmc, _JOB_ID, job_href=_SELF_HREF)

    assert outcome.found is False
