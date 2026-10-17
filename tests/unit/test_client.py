"""Tests for HMCClient against a mocked HMC (respx)."""

import asyncio
import inspect
import json
import logging
import threading
import traceback
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from conftest import (
    COMPLETED_JOB_ENTRY,
    JOB_ENTRY,
    JOB_ID,
    LOGON_RESPONSE,
    RUNNING_JOB_ENTRY,
    live_fixture,
    live_response,
    make_config,
)
from defusedxml import ElementTree as DET

from hmcpctl.audit import sink as audit_sink
from hmcpctl.client import core as client_core
from hmcpctl.client.core import (
    HMCClient,
    HMCVerifySSLDefaultWarning,
    TLSVerificationDisabledWarning,
)
from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError, HMCTransportError
from hmcpctl.jobs import build_job_request
from hmcpctl.xmlutil import localname

BASE = "https://hmc.test"


@pytest.mark.parametrize("host", ["hmc.test\rX-Injected: yes", "[not-an-ip]"])
def test_constructor_invalid_url_is_non_retryable_hmc_error(host):
    config = make_config(host=host)
    with pytest.raises(HMCError) as exc_info:
        HMCClient(config)

    error = exc_info.value
    assert not isinstance(error, HMCTransportError)
    assert isinstance(error.__cause__, httpx.InvalidURL)
    assert host not in str(error)
    assert "\r" not in str(error)
    assert "\n" not in str(error)


def test_constructor_does_not_translate_other_exception_families(monkeypatch):
    failure = ValueError("unrelated construction failure")

    def fail_new_client(self, port):
        raise failure

    monkeypatch.setattr(HMCClient, "_new_http_client", fail_new_client)
    with pytest.raises(ValueError) as exc_info:
        HMCClient(make_config())

    assert exc_info.value is failure


@pytest.mark.asyncio
async def test_implicit_port_falls_back_to_12443_after_logon_transport_failure():
    with respx.mock(assert_all_called=False) as router:
        primary = router.put(url__regex=r"https://hmc\.test/rest/api/web/Logon").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        legacy = router.put(
            url__regex=r"https://hmc\.test:12443/rest/api/web/Logon"
        ).mock(return_value=httpx.Response(200, text=LOGON_RESPONSE))
        metrics = router.get(
            url__regex=(
                r"https://hmc\.test:12443/rest/api/pcm/ProcessedMetrics/"
                r"ManagedSystem_sys_2\.json"
            )
        ).mock(return_value=httpx.Response(200, json={"systemUtil": {}}))
        logoff = router.delete(
            url__regex=r"https://hmc\.test:12443/rest/api/web/Logon"
        ).mock(return_value=httpx.Response(204))

        async with HMCClient(make_config(verify_ssl=True)) as client:
            assert client.is_logged_on
            await client.fetch_json(
                "/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json"
            )

    assert primary.call_count == 1
    assert legacy.call_count == 1
    assert metrics.call_count == 1
    assert logoff.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [443, 12443])
async def test_explicit_port_transport_failure_is_hard_failure(port):
    with respx.mock(assert_all_called=False) as router:
        selected_url = (
            r"https://hmc\.test/rest/api/web/Logon"
            if port == 443
            else rf"https://hmc\.test:{port}/rest/api/web/Logon"
        )
        selected = router.put(url__regex=selected_url).mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        legacy = None
        if port == 443:
            legacy = router.put(
                url__regex=r"https://hmc\.test:12443/rest/api/web/Logon"
            ).mock(side_effect=AssertionError("explicit ports must not fall back"))

        client = HMCClient(make_config(port=port, verify_ssl=True))
        with pytest.raises(HMCTransportError):
            await client.logon()
        await client._http.aclose()

    assert selected.call_count == 1
    if legacy is not None:
        assert legacy.call_count == 0


@pytest.mark.asyncio
async def test_existing_session_token_prevents_fallback_on_repeated_logon():
    with respx.mock(assert_all_called=False) as router:
        primary = router.put(url__regex=r"https://hmc\.test/rest/api/web/Logon").mock(
            side_effect=[
                httpx.Response(200, text=LOGON_RESPONSE),
                httpx.ConnectError("connection refused"),
            ]
        )
        legacy = router.put(
            url__regex=r"https://hmc\.test:12443/rest/api/web/Logon"
        ).mock(side_effect=AssertionError("an existing session must not fall back"))

        client = HMCClient(make_config(verify_ssl=True))
        token = await client.logon()
        with pytest.raises(HMCTransportError):
            await client.logon()
        await client._http.aclose()

    assert client._session_token == token
    assert primary.call_count == 2
    assert legacy.call_count == 0


@pytest.mark.asyncio
async def test_close_failure_aborts_fallback_before_legacy_client_is_created():
    client = HMCClient(make_config(verify_ssl=True))
    client._request = AsyncMock(side_effect=HMCTransportError("primary failed"))
    client._http.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
    client._new_http_client = MagicMock()

    with pytest.raises(RuntimeError, match="close failed"):
        await client.logon()

    client._new_http_client.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_while_closing_aborts_fallback():
    close_started = asyncio.Event()

    async def suspended_close():
        close_started.set()
        await asyncio.Event().wait()

    client = HMCClient(make_config(verify_ssl=True))
    client._request = AsyncMock(side_effect=HMCTransportError("primary failed"))
    client._http.aclose = AsyncMock(side_effect=suspended_close)
    client._new_http_client = MagicMock()

    logon = asyncio.create_task(client.logon())
    await asyncio.wait_for(close_started.wait(), timeout=5)
    logon.cancel()

    with pytest.raises(asyncio.CancelledError):
        await logon
    client._new_http_client.assert_not_called()


@pytest.mark.asyncio
async def test_rest_transport_failure_uses_hmc_error_hierarchy(mock_hmc):
    request = mock_hmc.put("/rest/api/web/Logon").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(HMCTransportError, match=r"PUT /rest/api/web/Logon") as exc_info:
        async with HMCClient(make_config()):
            pass

    assert request.called
    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


@pytest.mark.asyncio
async def test_rest_timeout_names_configured_timeout_and_guidance(mock_hmc):
    mock_hmc.put("/rest/api/web/Logon").mock(side_effect=httpx.ConnectTimeout(""))

    with pytest.raises(HMCTransportError) as exc_info:
        async with HMCClient(make_config(timeout=12.5, verify_ssl=True)):
            pass

    message = str(exc_info.value)
    assert "timed out" in message
    assert "12.5s" in message
    assert "HMC_TIMEOUT" in message


LPAR_FEED = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:11111111-1111-1111-1111-111111111111</id>
    <title>LogicalPartition</title>
    <link rel="SELF" href="{BASE}/rest/api/uom/LogicalPartition/11111111-1111-1111-1111-111111111111"/>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>lpar1</PartitionName>
        <PartitionState>running</PartitionState>
      </LogicalPartition>
    </content>
  </entry>
  <entry>
    <id>urn:uuid:22222222-2222-2222-2222-222222222222</id>
    <title>LogicalPartition</title>
    <link rel="SELF" href="{BASE}/rest/api/uom/LogicalPartition/22222222-2222-2222-2222-222222222222"/>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <PartitionName>lpar2</PartitionName>
        <PartitionState>not activated</PartitionState>
      </LogicalPartition>
    </content>
  </entry>
</feed>
"""


def _managed_system_feed(uuid: str, name: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:{uuid}</id>
    <title>ManagedSystem:{name}</title>
    <link rel="SELF" href="{BASE}/rest/api/uom/ManagedSystem/{uuid}"/>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <ManagedSystem xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <SystemName>{name}</SystemName>
      </ManagedSystem>
    </content>
  </entry>
</feed>
"""


# The HMC answers a quick property with a JSON string, quotes included (#1161, P16).
QUICK_STATE = live_fixture("rest-quick-partition-state")["body"]


@pytest.mark.asyncio
async def test_logon_logoff(mock_hmc):
    async with HMCClient(make_config()) as hmc:
        assert hmc.is_logged_on
        assert hmc._session_token == "test-session-token-123"
    assert not hmc.is_logged_on


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 202, 204])
async def test_logoff_accepts_success_statuses(mock_hmc, status):
    mock_hmc.delete("/rest/api/web/Logon").mock(return_value=httpx.Response(status))
    client = HMCClient(make_config())
    await client.logon()
    assert client.is_logged_on

    await client.logoff()

    assert not client.is_logged_on
    assert "X-API-Session" not in client._http.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404, 500])
async def test_logoff_rejects_unexpected_status_and_clears_state(mock_hmc, status):
    mock_hmc.delete("/rest/api/web/Logon").mock(
        return_value=httpx.Response(status, text="<Message>nope</Message>")
    )
    client = HMCClient(make_config())
    await client.logon()

    with pytest.raises(HMCError) as raised:
        await client.logoff()

    assert raised.value.status_code == status
    assert not client.is_logged_on
    assert "X-API-Session" not in client._http.headers


@pytest.mark.asyncio
async def test_logoff_transport_failure_is_distinct_and_clears_state(mock_hmc):
    mock_hmc.delete("/rest/api/web/Logon").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    client = HMCClient(make_config())
    await client.logon()

    with pytest.raises(HMCTransportError):
        await client.logoff()

    assert not client.is_logged_on
    assert client._session_token is None
    # The dead token is gone from the shared header store, so no subsequent
    # request can carry it.
    assert "X-API-Session" not in client._http.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_error", "logoff_error", "close_error", "expected_error"),
    [
        (None, None, None, None),
        (ValueError("operation failed"), None, None, ValueError),
        (None, RuntimeError("logoff failed"), None, RuntimeError),
        (None, None, OSError("close failed"), OSError),
        (None, HMCTransportError("logoff dropped"), None, None),
        (None, HMCTransportError("logoff dropped"), OSError("close failed"), OSError),
        (
            ValueError("operation failed"),
            RuntimeError("logoff failed"),
            OSError("close failed"),
            ValueError,
        ),
    ],
)
async def test_context_exit_preserves_primary_error_and_always_closes(
    body_error, logoff_error, close_error, expected_error
):
    client = HMCClient(make_config())
    client.logoff = AsyncMock(side_effect=logoff_error)
    client._http.aclose = AsyncMock(side_effect=close_error)

    async def exercise_context():
        try:
            if body_error is not None:
                raise body_error
        except BaseException as exc:
            await client.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await client.__aexit__(None, None, None)

    if expected_error is None:
        await exercise_context()
    else:
        with pytest.raises(expected_error) as raised:
            await exercise_context()
        if body_error is not None:
            assert raised.value is body_error

    client.logoff.assert_awaited_once()
    client._http.aclose.assert_awaited_once()


def _logoff_warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.name == "hmcpctl.client.core" and record.levelno == logging.WARNING
    ]


@pytest.mark.asyncio
async def test_context_exit_logs_logoff_transport_failure_after_clean_body(
    mock_hmc, caplog
):
    mock_hmc.delete("/rest/api/web/Logon").mock(
        side_effect=httpx.RemoteProtocolError(
            "Server disconnected without sending a response."
        )
    )
    client = HMCClient(make_config())

    with caplog.at_level(logging.WARNING, logger="hmcpctl.client.core"):
        async with client:
            pass

    [record] = _logoff_warnings(caplog)
    message = record.getMessage()
    assert "hmc.test" in message
    assert "may persist" in message
    assert "Server disconnected" in message
    assert client._http.is_closed
    assert not client.is_logged_on


@pytest.mark.asyncio
async def test_context_exit_raises_logoff_rejection_after_clean_body(mock_hmc, caplog):
    mock_hmc.delete("/rest/api/web/Logon").mock(
        return_value=httpx.Response(500, text="<Message>nope</Message>")
    )
    client = HMCClient(make_config())

    with (
        caplog.at_level(logging.WARNING, logger="hmcpctl.client.core"),
        pytest.raises(HMCError) as raised,
    ):
        async with client:
            pass

    assert not isinstance(raised.value, HMCTransportError)
    assert raised.value.status_code == 500
    assert _logoff_warnings(caplog) == []
    assert client._http.is_closed


@pytest.mark.asyncio
async def test_context_exit_notes_logoff_transport_failure_when_body_raised(
    mock_hmc, caplog
):
    mock_hmc.delete("/rest/api/web/Logon").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    client = HMCClient(make_config())
    body_error = ValueError("operation failed")

    with (
        caplog.at_level(logging.WARNING, logger="hmcpctl.client.core"),
        pytest.raises(ValueError) as raised,
    ):
        async with client:
            raise body_error

    assert raised.value is body_error
    assert any(
        "HMC session cleanup also failed" in note and "HMCTransportError" in note
        for note in raised.value.__notes__
    )
    assert _logoff_warnings(caplog) == []
    assert client._http.is_closed


@pytest.mark.asyncio
async def test_logon_with_test_config_is_silent_by_default(mock_hmc):
    """The shared mock-client factory enables TLS verification by default."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with HMCClient(make_config()):
            pass

    assert not [warning for warning in caught if "verification" in str(warning.message)]


@pytest.mark.asyncio
async def test_logon_warns_when_verify_ssl_disabled(mock_hmc):
    """Logon with verify_ssl=False emits an explicit MITM warning."""
    with pytest.warns(
        TLSVerificationDisabledWarning,
        match="certificate verification is disabled",
    ):
        async with HMCClient(make_config(verify_ssl=False)):
            pass


@pytest.mark.asyncio
async def test_tls_warning_is_emitted_once_per_host_and_setting_source(
    monkeypatch,
):
    monkeypatch.setattr(client_core, "_reported_tls_warning_keys", set())

    async def logon(client):
        client._logon_once = AsyncMock(return_value="token")
        await client.logon()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await logon(HMCClient(make_config(host="first.test", verify_ssl=False)))
        await logon(HMCClient(make_config(host="first.test", verify_ssl=False)))
        await logon(HMCClient(make_config(host="second.test", verify_ssl=False)))
        await logon(
            HMCClient(
                HMCConfig.from_mapping(
                    {
                        "host": "first.test",
                        "user": "hscroot",
                        "password": "abc123",  # pragma: allowlist secret
                    }
                )
            )
        )

    tls_warnings = [
        warning
        for warning in caught
        if warning.category is TLSVerificationDisabledWarning
    ]
    assert len(tls_warnings) == 3


@pytest.mark.asyncio
async def test_tls_warning_key_retains_the_construction_time_source(monkeypatch):
    monkeypatch.setattr(client_core, "_reported_tls_warning_keys", set())
    monkeypatch.setenv("HMC_VERIFY_SSL", "false")
    environment_client = HMCClient(
        HMCConfig(
            host="hmc.test",
            user="hscroot",
            password="abc123",  # pragma: allowlist secret
        )
    )

    monkeypatch.setenv("HMC_VERIFY_SSL", "true")
    explicit_client = HMCClient(make_config(host="hmc.test", verify_ssl=False))
    for client in (environment_client, explicit_client):
        client._logon_once = AsyncMock(return_value="token")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", TLSVerificationDisabledWarning)
        await environment_client.logon()
        await explicit_client.logon()

    await environment_client._http.aclose()
    await explicit_client._http.aclose()
    tls_warnings = [
        warning
        for warning in caught
        if warning.category is TLSVerificationDisabledWarning
    ]
    assert len(tls_warnings) == 2


@pytest.mark.asyncio
async def test_tls_warning_promoted_to_error_does_not_consume_the_key(monkeypatch):
    monkeypatch.setattr(client_core, "_reported_tls_warning_keys", set())
    client = HMCClient(make_config(verify_ssl=False))
    client._logon_once = AsyncMock(return_value="token")

    with warnings.catch_warnings():
        warnings.simplefilter("error", TLSVerificationDisabledWarning)
        with pytest.raises(TLSVerificationDisabledWarning):
            await client.logon()

    with pytest.warns(TLSVerificationDisabledWarning):
        await client.logon()


def test_concurrent_logons_emit_one_tls_warning(monkeypatch):
    monkeypatch.setattr(client_core, "_reported_tls_warning_keys", set())
    first_warning_entered = threading.Event()
    second_warning_entered = threading.Event()
    warning_calls = 0
    calls_lock = threading.Lock()

    def slow_warning(*args, **kwargs):
        nonlocal warning_calls
        with calls_lock:
            warning_calls += 1
            call_number = warning_calls
        if call_number == 1:
            first_warning_entered.set()
            second_warning_entered.wait(timeout=0.2)
        else:
            second_warning_entered.set()

    async def logon():
        client = HMCClient(make_config(verify_ssl=False))
        client._logon_once = AsyncMock(return_value="token")
        await client.logon()
        await client._http.aclose()

    monkeypatch.setattr(client_core.warnings, "warn", slow_warning)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(asyncio.run, logon()) for _ in range(2)]
        for future in futures:
            future.result()

    assert first_warning_entered.is_set()
    assert warning_calls == 1


@pytest.mark.asyncio
async def test_logon_silent_when_verify_ssl_enabled(mock_hmc):
    """Logon with verify_ssl=True emits no verification warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        async with HMCClient(make_config(verify_ssl=True)):
            pass
    assert not [w for w in caught if "verification" in str(w.message)]


def test_startup_warning_fires_when_verify_ssl_defaults_to_false(monkeypatch):
    """Field-default verify_ssl=false emits the pre-1.0 startup warning."""
    monkeypatch.delenv("HMC_VERIFY_SSL", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", HMCVerifySSLDefaultWarning)
        HMCClient(
            HMCConfig(
                host="hmc.test",
                user="hscroot",
                password="abc123",  # pragma: allowlist secret
            )
        )
    assert any(
        warning.category is HMCVerifySSLDefaultWarning and "1.0" in str(warning.message)
        for warning in caught
    )


def test_startup_warning_silent_when_verify_ssl_explicitly_false():
    """Explicit verify_ssl=false does not emit the startup warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", HMCVerifySSLDefaultWarning)
        HMCClient(make_config(verify_ssl=False))
    assert not [
        warning for warning in caught if warning.category is HMCVerifySSLDefaultWarning
    ]


def test_startup_warning_silent_when_verify_ssl_true():
    """verify_ssl=true does not emit the startup warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", HMCVerifySSLDefaultWarning)
        HMCClient(make_config(verify_ssl=True))
    assert not [
        warning for warning in caught if warning.category is HMCVerifySSLDefaultWarning
    ]


@pytest.mark.asyncio
async def test_logon_failure(mock_hmc):
    mock_hmc.put("/rest/api/web/Logon").mock(return_value=httpx.Response(401))
    client = HMCClient(make_config())
    with pytest.raises(HMCError) as exc_info:
        async with client:
            pass
    assert exc_info.value.status_code == 401
    assert client._http.is_closed


@pytest.mark.asyncio
async def test_session_entry_preserves_logon_failure_when_close_also_fails():
    client = HMCClient(make_config())
    logon_error = HMCError("bad credentials", 401)
    client.logon = AsyncMock(side_effect=logon_error)
    client._http.aclose = AsyncMock(side_effect=RuntimeError("close failed"))

    with pytest.raises(HMCError) as exc_info:
        async with client:
            pass

    assert exc_info.value is logon_error
    assert "session cleanup failed: close failed" in exc_info.value.__notes__


def _capture_audit() -> list[dict]:
    """Collect parsed audit records. Logger isolation is conftest's autouse fixture."""
    events: list[dict] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            events.append(json.loads(record.getMessage()))

    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    logger.addHandler(_Collect())
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return events


@pytest.mark.asyncio
async def test_tls_disabled_audit_event_is_once_per_construction_not_per_request(
    mock_hmc,
):
    """#379. N constructions emit N records; M requests on one client add none.

    Per-request would flood the sink; per-process would miss a later client built
    with different settings. The record is the audit stream's answer to "were our
    HMC credentials ever sent over an unverified channel", so it must exist once
    per client and never scale with traffic.
    """
    mock_hmc.get("/rest/api/hmc").mock(return_value=httpx.Response(200, text="<feed/>"))
    caught = _capture_audit()

    for _ in range(3):
        async with HMCClient(make_config(verify_ssl=False)) as client:
            for _ in range(5):
                await client._get("/rest/api/hmc")

    assert len(caught) == 3
    assert {record["event"] for record in caught} == {"tls-verification-disabled"}


@pytest.mark.asyncio
async def test_no_tls_audit_event_when_verification_enabled(mock_hmc):
    """#379. A verified connection produces no record — the event marks a gap."""
    caught = _capture_audit()
    async with HMCClient(make_config(verify_ssl=True)):
        pass
    assert caught == []


@pytest.mark.parametrize(
    ("environ_value", "kwargs", "expected_source"),
    [
        # The conftest autouse fixture sets HMC_VERIFY_SSL=true; an explicit
        # False argument overrides it, and pydantic-settings' source priority
        # makes that the record's answer even when the two agree.
        (None, {"verify_ssl": False}, "explicit-argument"),
        ("true", {"verify_ssl": False}, "explicit-argument"),
        # No explicit argument: the environment supplied the value pydantic
        # folded into the constructor kwargs.
        ("false", {}, "environment:HMC_VERIFY_SSL"),
        # Neither: the field default, which is the case an operator upgrading
        # with no configuration at all is in.
        (None, {}, "field-default"),
    ],
)
def test_tls_audit_record_names_where_the_setting_came_from(
    monkeypatch, environ_value, kwargs, expected_source
):
    """#379. `source` says which knob to turn, per the acceptance criteria."""
    if environ_value is None:
        monkeypatch.delenv("HMC_VERIFY_SSL", raising=False)
    else:
        monkeypatch.setenv("HMC_VERIFY_SSL", environ_value)

    config_kwargs = {
        # make_config() forces verify_ssl=True; these cases need the real
        # default to flow through unset.
        "host": "hmc.test",
        "user": "hscroot",
        "password": "abc123",  # pragma: allowlist secret
        "_env_file": None,
    }
    config_kwargs.update(kwargs)
    caught = _capture_audit()
    # Field-default cases deliberately exercise ADR 0224's startup warning too.
    expectation = (
        pytest.warns(HMCVerifySSLDefaultWarning)
        if expected_source == "field-default"
        else nullcontext()
    )
    with expectation:
        HMCClient(HMCConfig(**config_kwargs))

    assert len(caught) == 1
    assert caught[0]["source"] == expected_source
    assert caught[0]["host"] == "hmc.test"
    # No credential material in the record — construction-time state only.
    assert "abc123" not in json.dumps(caught[0])


@pytest.mark.parametrize("env_name", ["hmc_verify_ssl", "Hmc_Verify_Ssl"])
def test_tls_audit_record_names_the_environment_for_a_case_variant_export(
    monkeypatch, env_name
):
    """#531. pydantic-settings folded the variant in, so the record must name it.

    The vocabulary keeps the canonical spelling — it names the knob, not the
    operator's spelling of it — but reading only that spelling would report
    ``explicit-argument`` for a value nothing in the call supplied.
    """
    monkeypatch.delenv("HMC_VERIFY_SSL", raising=False)
    monkeypatch.setenv(env_name, "false")
    caught = _capture_audit()

    HMCClient(
        HMCConfig(
            host="hmc.test",
            user="hscroot",
            password="abc123",  # pragma: allowlist secret
        )
    )

    assert len(caught) == 1
    assert caught[0]["source"] == "environment:HMC_VERIFY_SSL"


@pytest.mark.parametrize(
    ("kwargs", "expected_source"),
    [
        # from_mapping restores model_fields_set to the keys the caller
        # supplied, so an omitted verify_ssl is absent from it. Naming
        # HMC_VERIFY_SSL here would point the operator at a variable that has
        # no effect on this connection — and, set to "true" against an
        # effective False, at one that contradicts the value in the same record.
        ({}, "field-default"),
        ({"verify_ssl": False}, "explicit-argument"),
    ],
)
def test_tls_audit_record_ignores_the_environment_for_an_isolated_config(
    monkeypatch, kwargs, expected_source
):
    """ADR 0096: a config the environment cannot reach must not cite it."""
    monkeypatch.setenv("HMC_VERIFY_SSL", "true")
    caught = _capture_audit()

    # Field-default cases deliberately exercise ADR 0224's startup warning too.
    expectation = (
        pytest.warns(HMCVerifySSLDefaultWarning)
        if expected_source == "field-default"
        else nullcontext()
    )
    with expectation:
        HMCClient(
            HMCConfig.from_mapping(
                {
                    "host": "hmc.test",
                    "user": "hscroot",
                    "password": "abc123",  # pragma: allowlist secret
                    **kwargs,
                }
            )
        )

    assert len(caught) == 1
    assert caught[0]["source"] == expected_source


@pytest.mark.asyncio
async def test_logon_body_carries_escaped_credentials_to_the_transport(mock_hmc):
    """The reported defect, proved at the wire rather than at the builder (#284).

    The credentials come from ``HMCConfig``, so nothing on the argument
    boundary sees them; this is the only place that proves what is sent.
    """
    user = "a</UserID><UserID>root"
    metacharacters = "R&D <a> \"b\" 'c'"

    async with HMCClient(make_config(user=user, password=metacharacters)):
        pass

    logon = next(
        call
        for call in mock_hmc.calls
        if call.request.method == "PUT"
        and call.request.url.path == "/rest/api/web/Logon"
    )
    parsed = DET.fromstring(logon.request.content)
    assert [el.text for el in parsed.iter() if localname(el.tag) == "UserID"] == [user]
    assert [el.text for el in parsed.iter() if localname(el.tag) == "Password"] == [
        metacharacters
    ]


@pytest.mark.asyncio
async def test_logon_refuses_a_password_xml_cannot_carry_before_sending(mock_hmc):
    """Rejected at the encoding boundary, and the message quotes no credential."""
    unrepresentable = "unleakable\x00"
    client = HMCClient(make_config(password=unrepresentable))
    try:
        with pytest.raises(ValueError, match=r"U\+0000") as raised:
            await client.logon()
    finally:
        await client._http.aclose()

    assert "unleakable" not in str(raised.value)
    assert not mock_hmc.calls


@pytest.mark.asyncio
async def test_logon_failure_never_quotes_the_credentials(mock_hmc):
    """The defect lives in the credential path, so the fix must not leak it."""
    leaky = "unleakable&<value>"
    mock_hmc.put("/rest/api/web/Logon").mock(return_value=httpx.Response(401))

    with pytest.raises(HMCError) as raised:
        async with HMCClient(make_config(password=leaky)):
            pass

    reported = "\n".join(
        (str(raised.value), repr(raised.value), *getattr(raised.value, "__notes__", ()))
    )
    # The fragment rather than the whole value: an escaped or partially
    # rendered leak would slip past a check for the exact string.
    assert "unleakable" not in reported


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("connection refused"), httpx.ConnectTimeout("")],
    ids=["transport", "timeout"],
)
async def test_a_failed_logon_traceback_carries_no_credential(failure, mock_hmc):
    """The body holds the credential, so no diagnostic about sending it may.

    Formatted over the whole chain rather than the raised message alone:
    ``_request`` re-raises ``from`` the httpx error, whose request object holds
    the rendered body, and a rendered traceback is where that would surface.
    """
    leaky = "unleakable&<value>"
    mock_hmc.put("/rest/api/web/Logon").mock(side_effect=failure)

    with pytest.raises(HMCTransportError) as raised:
        async with HMCClient(make_config(password=leaky)):
            pass

    rendered = "".join(
        traceback.format_exception(
            type(raised.value), raised.value, raised.value.__traceback__
        )
    )
    assert "unleakable" not in rendered


@pytest.mark.asyncio
async def test_list_lpars_for_system(mock_hmc):
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{_PARENT_UUID}/LogicalPartition").mock(
        return_value=httpx.Response(200, text=LPAR_FEED)
    )
    async with HMCClient(make_config()) as hmc:
        lpars = await hmc.list_logical_partitions(_PARENT_UUID)
    assert len(lpars) == 2


@pytest.mark.asyncio
async def test_quick_property(mock_hmc):
    mock_hmc.get(
        "/rest/api/uom/LogicalPartition/33333333-3333-3333-3333-333333333333/quick/PartitionState"
    ).mock(return_value=httpx.Response(200, text=QUICK_STATE))
    async with HMCClient(make_config()) as hmc:
        state = await hmc.get_quick_property(
            "LogicalPartition", "33333333-3333-3333-3333-333333333333", "PartitionState"
        )
    assert QUICK_STATE == '"open firmware"'
    assert state == "open firmware"


@pytest.mark.asyncio
async def test_find_partition_by_name(mock_hmc):
    single = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>urn:uuid:22222222-2222-2222-2222-222222222222</id>
      <content type="application/vnd.ibm.powervm.uom+xml">
        <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
          <PartitionName>lpar2</PartitionName>
        </LogicalPartition>
      </content>
    </entry></feed>"""
    mock_hmc.get("/rest/api/uom/LogicalPartition/search/(PartitionName==lpar2)").mock(
        return_value=httpx.Response(200, text=single)
    )
    async with HMCClient(make_config()) as hmc:
        found = await hmc.find_partition_by_name("lpar2")
    assert found is not None
    assert found["Resource"]["PartitionName"] == "lpar2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("property_name", "property_value", "encoded_expression"),
    [
        (
            "Partition Name",
            "web server & db%#1+café",
            "Partition%20Name==web%20server%20%26%20db%25%231%2Bcaf%C3%A9",
        ),
        ("State", "running", "State==running"),
    ],
)
async def test_search_uom_encodes_only_interpolated_grammar_components(
    mock_hmc, property_name, property_value, encoded_expression
):
    path = f"/rest/api/uom/LogicalPartition/search/({encoded_expression})"
    route = mock_hmc.get(path).mock(return_value=httpx.Response(204))

    async with HMCClient(make_config()) as hmc:
        assert (
            await hmc.search_uom("LogicalPartition", property_name, property_value)
            == []
        )

    assert route.calls.last.request.url.raw_path.decode() == path


@pytest.mark.asyncio
async def test_search_uom_error_names_encoded_request_path(mock_hmc):
    path = "/rest/api/uom/LogicalPartition/search/(PartitionName==web%20server)"
    mock_hmc.get(path).mock(return_value=httpx.Response(500, text="failed"))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="PartitionName==web%20server"):
            await hmc.search_uom("LogicalPartition", "PartitionName", "web server")


@pytest.mark.asyncio
async def test_submit_power_on_job(mock_hmc):
    route = mock_hmc.put(
        "/rest/api/uom/LogicalPartition/33333333-3333-3333-3333-333333333333/do/PowerOn"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    async with HMCClient(make_config()) as hmc:
        job = await hmc.submit_job(
            "/rest/api/uom/LogicalPartition/33333333-3333-3333-3333-333333333333/do/PowerOn",
            build_job_request("PowerOn", "LogicalPartition"),
        )
    assert route.called
    request = route.calls.last.request
    assert b"PowerOn" in request.content
    assert b"LogicalPartition" in request.content
    assert job is not None
    assert job["Resource"]["Status"] == "NOT_STARTED"


@pytest.mark.asyncio
async def test_job_request_xml():
    xml = build_job_request("PowerOff", "LogicalPartition", {"immediate": "true"})
    assert "<OperationName" in xml and "PowerOff" in xml
    assert "<GroupName" in xml and "LogicalPartition" in xml
    assert "immediate" in xml
    assert "true" in xml


@pytest.mark.asyncio
async def test_missing_credentials():
    config = make_config(host="", user="", password="")
    with pytest.raises(ValueError, match="Missing HMC configuration"):
        HMCClient(config)


@pytest.mark.asyncio
async def test_http_error_raises(mock_hmc):
    mock_hmc.get("/rest/api/uom/ManagedSystem").mock(
        return_value=httpx.Response(500, text="<m><Message>boom</Message></m>")
    )
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await hmc.list_uom("ManagedSystem")
    assert exc_info.value.status_code == 500
    assert "boom" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_managed_system_falls_back_via_quick_all(mock_hmc):
    uuid = "11111111-1111-1111-1111-111111111111"
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{uuid}").mock(
        return_value=httpx.Response(
            500,
            text="Nested path contains null property, "
            "currentProperty=Uuid nestedPath=VirtualPersistentMemoryVolume/Uuid/Value/Value",
        )
    )
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(200, json=[{"UUID": uuid, "SystemName": "sys1"}])
    )
    mock_hmc.get("/rest/api/uom/ManagedSystem/search/(SystemName==sys1)").mock(
        return_value=httpx.Response(200, text=_managed_system_feed(uuid, "sys1"))
    )
    async with HMCClient(make_config()) as hmc:
        entry = await hmc.get_managed_system(uuid)
    assert entry is not None
    assert entry["UUID"] == uuid
    assert entry["Resource"]["SystemName"] == "sys1"


@pytest.mark.asyncio
async def test_get_managed_system_fallback_matches_quick_all_uuid_in_any_case(
    mock_hmc,
):
    """The captured V10R3 quick/All prints system UUIDs in lower case (#1202)."""
    uuid = "00000003-abcd-4ef0-8abc-000000000003"
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{uuid.upper()}").mock(
        return_value=httpx.Response(
            500,
            text="Nested path contains null property, "
            "currentProperty=Uuid nestedPath=VirtualPersistentMemoryVolume/Uuid/Value/Value",
        )
    )
    path, quick_all = live_response("rest-ms-quick-all")
    mock_hmc.get(path).mock(return_value=quick_all)
    mock_hmc.get("/rest/api/uom/ManagedSystem/search/(SystemName==sys-R1)").mock(
        return_value=httpx.Response(200, text=_managed_system_feed(uuid, "sys-R1"))
    )
    async with HMCClient(make_config()) as hmc:
        entry = await hmc.get_managed_system(uuid.upper())
    assert entry is not None
    assert entry["Resource"]["SystemName"] == "sys-R1"


@pytest.mark.parametrize(
    ("quick_all_response", "expect_quick_all_cause"),
    [
        (httpx.Response(200, json=[]), False),
        (httpx.Response(500, text="boom"), True),
    ],
    ids=["miss", "quick-all-fails"],
)
@pytest.mark.asyncio
async def test_get_managed_system_fallback_failure_raises_actionable_error(
    mock_hmc, quick_all_response, expect_quick_all_cause
):
    uuid = "11111111-1111-1111-1111-111111111111"
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{uuid}").mock(
        return_value=httpx.Response(500, text="Nested path contains null property")
    )
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=quick_all_response
    )
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="could not be resolved") as exc_info:
            await hmc.get_managed_system(uuid)
    assert exc_info.value.status_code == 500
    message = str(exc_info.value)
    assert "X-HMC-Schema-Version pin (HMC_SCHEMA_VERSION)" in message
    assert "unset HMC_SCHEMA_VERSION" in message
    assert "firmware" not in message
    if expect_quick_all_cause:
        assert "quick/All failed" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
async def test_get_managed_system_http_error_raises(mock_hmc):
    uuid = "11111111-1111-1111-1111-111111111111"
    mock_hmc.get(f"/rest/api/uom/ManagedSystem/{uuid}").mock(
        return_value=httpx.Response(500, text="<m><Message>boom</Message></m>")
    )
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await hmc.get_managed_system(uuid)
    assert exc_info.value.status_code == 500
    assert "boom" in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_managed_system_does_not_catch_runtime_errors(mock_hmc):
    async with HMCClient(make_config()) as hmc:
        hmc.get_uom = AsyncMock(side_effect=RuntimeError("parser invariant failed"))
        with pytest.raises(RuntimeError, match="parser invariant failed"):
            await hmc.get_managed_system("sys-uuid-1")


@pytest.mark.asyncio
async def test_quick_all_system_names_maps_uuid_to_name(mock_hmc):
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"UUID": "sys-uuid-1", "SystemName": "sys1", "State": "operating"},
                {"UUID": "sys-uuid-2", "SystemName": "sys2", "State": "operating"},
                {"State": "operating"},
            ],
        )
    )
    async with HMCClient(make_config()) as hmc:
        names = await hmc._quick_all_system_names()
    assert names == {"sys-uuid-1": "sys1", "sys-uuid-2": "sys2"}


@pytest.mark.asyncio
async def test_quick_all_system_names_raises_on_non_200(mock_hmc):
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(500, text="boom")
    )
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="quick/All failed"):
            await hmc._quick_all_system_names()


@pytest.mark.asyncio
async def test_quick_all_system_names_recursion_error_raises_hmc_error(
    mock_hmc, monkeypatch
):
    """A deeply nested body raises RecursionError, not a ValueError subclass,

    so it needs its own clause to reach HMCError instead of escaping the guard.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(200, text="[]")
    )

    def raise_recursion_error(_response):
        raise RecursionError

    monkeypatch.setattr(httpx.Response, "json", raise_recursion_error)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await hmc._quick_all_system_names()

    message = str(exc_info.value)
    assert "quick/All returned invalid JSON: document nesting is too deep" in message
    assert isinstance(exc_info.value.__cause__, RecursionError)


CREATED_LPAR = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:new-33333333-3333-3333-3333-333333333333</id>
  <title>LogicalPartition</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <LogicalPartition xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <PartitionName>newlpar</PartitionName>
      <PartitionState>not activated</PartitionState>
    </LogicalPartition>
  </content>
</entry>
"""


@pytest.mark.asyncio
async def test_create_logical_partition(mock_hmc):
    route = mock_hmc.put(
        f"/rest/api/uom/ManagedSystem/{_PARENT_UUID}/LogicalPartition"
    ).mock(return_value=httpx.Response(201, text=CREATED_LPAR))
    from hmcpctl.documents.lpar import LparResources, build_lpar_document

    xml = build_lpar_document(
        name="newlpar",
        resources=LparResources(
            min_memory=256,
            desired_memory=4096,
            max_memory=8192,
            desired_vcpus=1,
            max_vcpus=2,
        ),
    )
    async with HMCClient(make_config()) as hmc:
        created = await hmc.create_logical_partition(_PARENT_UUID, xml)
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "newlpar" in body and "4096" in body
    assert created is not None
    assert created["Resource"]["PartitionName"] == "newlpar"


@pytest.mark.asyncio
async def test_delete_logical_partition(mock_hmc):
    route = mock_hmc.delete(
        "/rest/api/uom/LogicalPartition/33333333-3333-3333-3333-333333333333"
    ).mock(return_value=httpx.Response(204))
    async with HMCClient(make_config()) as hmc:
        await hmc.delete_logical_partition("33333333-3333-3333-3333-333333333333")
    assert route.called


ADAPTER_ENTRY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:44444444-4444-4444-4444-444444444444</id>
  <title>ClientNetworkAdapter</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <ClientNetworkAdapter xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <PortVLANID>100</PortVLANID>
      <VirtualSlotNumber>9</VirtualSlotNumber>
    </ClientNetworkAdapter>
  </content>
</entry>
"""


@pytest.mark.asyncio
async def test_add_network_adapter(mock_hmc):
    route = mock_hmc.put(
        "/rest/api/uom/LogicalPartition/33333333-3333-3333-3333-333333333333/ClientNetworkAdapter"
    ).mock(return_value=httpx.Response(201, text=ADAPTER_ENTRY))
    async with HMCClient(make_config()) as hmc:
        adapter = await hmc.add_network_adapter(
            "33333333-3333-3333-3333-333333333333", port_vlan_id=100, slot_number=9
        )
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "ClientNetworkAdapter" in body and ">100<" in body
    assert adapter is not None
    assert adapter["Resource"]["PortVLANID"] == "100"


@pytest.mark.asyncio
async def test_add_vscsi_adapter(mock_hmc):
    route = mock_hmc.put(
        "/rest/api/uom/LogicalPartition/33333333-3333-3333-3333-333333333333/VirtualSCSIClientAdapter"
    ).mock(return_value=httpx.Response(201, text=ADAPTER_ENTRY))
    async with HMCClient(make_config()) as hmc:
        await hmc.add_vscsi_adapter(
            "33333333-3333-3333-3333-333333333333", vios_partition_id=1, vios_slot=5
        )
    body = route.calls.last.request.content.decode()
    assert "VirtualSCSIClientAdapter" in body
    assert "RemoteLogicalPartitionID" in body and "RemoteSlotNumber" in body


@pytest.mark.asyncio
async def test_add_vfc_adapter(mock_hmc):
    route = mock_hmc.put(
        "/rest/api/uom/LogicalPartition/33333333-3333-3333-3333-333333333333/VirtualFibreChannelClientAdapter"
    ).mock(return_value=httpx.Response(201, text=ADAPTER_ENTRY))
    async with HMCClient(make_config()) as hmc:
        await hmc.add_vfc_adapter(
            "33333333-3333-3333-3333-333333333333", vios_partition_id=1, vios_slot=6
        )
    body = route.calls.last.request.content.decode()
    assert "VirtualFibreChannelClientAdapter" in body
    assert "ConnectingPartitionID" in body and "ConnectingVirtualSlotNumber" in body


@pytest.mark.asyncio
async def test_list_adapters(mock_hmc):
    mock_hmc.get(
        "/rest/api/uom/LogicalPartition/33333333-3333-3333-3333-333333333333/ClientNetworkAdapter"
    ).mock(return_value=httpx.Response(200, text=ADAPTER_ENTRY))
    async with HMCClient(make_config()) as hmc:
        adapters = await hmc.list_child(
            "LogicalPartition",
            "33333333-3333-3333-3333-333333333333",
            "ClientNetworkAdapter",
        )
    assert len(adapters) == 1
    assert adapters[0]["ResourceType"] == "ClientNetworkAdapter"


@pytest.mark.asyncio
async def test_delete_adapter(mock_hmc):
    route = mock_hmc.delete(
        "/rest/api/uom/LogicalPartition/33333333-3333-3333-3333-333333333333/ClientNetworkAdapter/44444444-4444-4444-4444-444444444444"
    ).mock(return_value=httpx.Response(204))
    async with HMCClient(make_config()) as hmc:
        await hmc.delete_child(
            "LogicalPartition",
            "33333333-3333-3333-3333-333333333333",
            "ClientNetworkAdapter",
            "44444444-4444-4444-4444-444444444444",
        )
    assert route.called


VG_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:22222222-2222-2222-2222-222222222221</id>
    <title>VolumeGroup:vg_1</title>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <GroupName>vg_1</GroupName>
        <GroupCapacity>102400</GroupCapacity>
        <FreeSpace>51200</FreeSpace>
      </VolumeGroup>
    </content>
  </entry>
</feed>
"""

VG_ENTRY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:22222222-2222-2222-2222-222222222221</id>
  <title>VolumeGroup:vg_1</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <VolumeGroup xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <GroupName>vg_1</GroupName>
    </VolumeGroup>
  </content>
</entry>
"""

VIOS_ENTRY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
  <id>urn:uuid:11111111-1111-1111-1111-111111111111-1</id>
  <title>VirtualIOServer:vios1</title>
  <content type="application/vnd.ibm.powervm.uom+xml">
    <VirtualIOServer xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
      <PartitionName>vios1</PartitionName>
    </VirtualIOServer>
  </content>
</entry>
"""


@pytest.mark.asyncio
async def test_list_volume_groups(mock_hmc):
    mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup"
    ).mock(return_value=httpx.Response(200, text=VG_FEED))
    async with HMCClient(make_config()) as hmc:
        vgs = await hmc.list_volume_groups("11111111-1111-1111-1111-111111111111")
    assert len(vgs) == 1
    assert vgs[0]["Resource"]["GroupName"] == "vg_1"
    assert vgs[0]["Resource"]["FreeSpace"] == "51200"


@pytest.mark.asyncio
async def test_create_volume_group(mock_hmc):
    mock_hmc.get(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup"
    ).mock(return_value=httpx.Response(200, text=VG_FEED))
    route = mock_hmc.put(
        "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111/VolumeGroup"
    ).mock(return_value=httpx.Response(201, text=VG_ENTRY))
    async with HMCClient(make_config()) as hmc:
        vg = await hmc.create_volume_group(
            "11111111-1111-1111-1111-111111111111", "vg_1", ["hdisk10"]
        )
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "vg_1" in body and "hdisk10" in body
    assert vg is not None


@pytest.mark.asyncio
async def test_map_storage_to_lpar(mock_hmc):
    system_uuid = "22222222-2222-2222-2222-222222222222"
    path = "/rest/api/uom/VirtualIOServer/11111111-1111-1111-1111-111111111111?group=ViosSCSIMapping"
    vios = (
        '<VirtualIOServer xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">'
        "<Metadata><Atom><AtomID>11111111-1111-1111-1111-111111111111</AtomID></Atom></Metadata>"
        '<PartitionUUID kb="ROO">11111111-1111-1111-1111-111111111111</PartitionUUID>'
        f'<AssociatedManagedSystem kb="CUD" kxe="false" rel="related" '
        f'href="https://hmc.example.invalid:12443/rest/api/uom/ManagedSystem/{system_uuid}"/>'
        "<VirtualSCSIMappings/></VirtualIOServer>"
    )
    mock_hmc.get(path).mock(
        return_value=httpx.Response(200, text=vios, headers={"ETag": "e1"})
    )
    route = mock_hmc.post(path).mock(return_value=httpx.Response(200, text=VIOS_ENTRY))
    async with HMCClient(make_config()) as hmc:
        result = await hmc.map_storage_to_lpar(
            "11111111-1111-1111-1111-111111111111",
            "VirtualDisk",
            "lv_boot",
            _PARENT_UUID,
        )
    assert result is not None and result["Resource"]["PartitionName"] == "vios1"
    body = route.calls.last.request.content.decode()
    assert "VirtualSCSIMapping" in body
    assert "lv_boot" in body
    assert f"ManagedSystem/{system_uuid}/LogicalPartition/{_PARENT_UUID}" in body


CLUSTER_FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:uuid:cluster-uuid-1</id>
    <title>Cluster:cluster1</title>
    <content type="application/vnd.ibm.powervm.uom+xml">
      <Cluster xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
        <ClusterName>cluster1</ClusterName>
      </Cluster>
    </content>
  </entry>
</feed>
"""


@pytest.mark.asyncio
async def test_list_clusters(mock_hmc):
    mock_hmc.get("/rest/api/uom/Cluster").mock(
        return_value=httpx.Response(200, text=CLUSTER_FEED)
    )
    async with HMCClient(make_config()) as hmc:
        clusters = await hmc.list_clusters()
    assert len(clusters) == 1
    assert clusters[0]["Resource"]["ClusterName"] == "cluster1"


@pytest.mark.asyncio
async def test_create_logical_unit(mock_hmc):
    route = mock_hmc.put(
        f"/rest/api/uom/Cluster/{_PARENT_UUID}/do/CreateLogicalUnit"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    async with HMCClient(make_config()) as hmc:
        job = await hmc.create_logical_unit(_PARENT_UUID, "newLU", 18)
    body = route.calls.last.request.content.decode()
    assert "CreateLogicalUnit" in body and "newLU" in body and ">18<" in body
    assert job is not None and job["Resource"]["JobID"] == JOB_ID


@pytest.mark.asyncio
async def test_delete_logical_unit(mock_hmc):
    route = mock_hmc.put(
        f"/rest/api/uom/Cluster/{_PARENT_UUID}/do/DeleteLogicalUnit"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))
    async with HMCClient(make_config()) as hmc:
        await hmc.delete_logical_unit(_PARENT_UUID, "udid-9")
    body = route.calls.last.request.content.decode()
    assert "DeleteLogicalUnit" in body and "udid-9" in body


PCM_PREFS_XML = """<?xml version="1.0"?>
<ManagementConsolePcmPreference xmlns="http://www.ibm.com/xmlns/systems/power/firmware/pcm/mc/2012_10/">
  <LongTermMonitorEnabled>true</LongTermMonitorEnabled>
  <AggregationEnabled>false</AggregationEnabled>
</ManagementConsolePcmPreference>
"""

PCM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>ManagedSystem ProcessedMetrics</title>
    <updated>2026-08-07T12:00:30Z</updated>
    <link rel="SELF" href="/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json" type="application/json"/>
  </entry>
</feed>
"""


@pytest.mark.asyncio
async def test_get_pcm_preferences(mock_hmc):
    mock_hmc.get("/rest/api/pcm/ManagedSystem/sys-uuid/preferences").mock(
        return_value=httpx.Response(200, text=PCM_PREFS_XML)
    )
    async with HMCClient(make_config()) as hmc:
        prefs = await hmc.get_pcm_preferences("ManagedSystem", "sys-uuid")
    assert prefs["LongTermMonitorEnabled"] is True
    assert prefs["AggregationEnabled"] is False


@pytest.mark.asyncio
async def test_set_pcm_preferences(mock_hmc):
    """The update reads the document first and posts it back changed (#634)."""
    read = mock_hmc.get("/rest/api/pcm/ManagedSystem/sys-uuid/preferences").mock(
        return_value=live_response("rest-pcm-preferences")[1]
    )
    route = mock_hmc.post("/rest/api/pcm/ManagedSystem/sys-uuid/preferences").mock(
        return_value=httpx.Response(200, text=PCM_PREFS_XML)
    )
    async with HMCClient(make_config()) as hmc:
        prefs = await hmc.set_pcm_preferences(
            "ManagedSystem", "sys-uuid", LongTermMonitorEnabled=True
        )
    assert read.calls[0].request.headers["accept"] == "*/*"
    request = route.calls.last.request
    assert request.headers["content-type"] == "application/xml"
    body = request.content.decode()
    assert "<SystemName" in body
    assert ">true</LongTermMonitorEnabled>" in body
    assert ">false</AggregationEnabled>" in body
    assert prefs["LongTermMonitorEnabled"] is True
    assert prefs["AggregationEnabled"] is False


@pytest.mark.asyncio
async def test_get_processed_metrics_links(mock_hmc):
    route = mock_hmc.get("/rest/api/pcm/ManagedSystem/sys-uuid/ProcessedMetrics").mock(
        return_value=httpx.Response(200, text=PCM_FEED)
    )
    async with HMCClient(make_config()) as hmc:
        links = await hmc.get_processed_metric_links(
            "ManagedSystem", "sys-uuid", "2026-08-07T11:00:00Z", no_of_samples=5
        )
    assert len(links) == 1
    assert links[0]["link"].endswith("_2.json")
    # StartTS/NoOfSamples were sent as query params on the metrics GET.
    req = route.calls.last.request
    assert "StartTS=" in str(req.url) and "NoOfSamples=5" in str(req.url)


@pytest.mark.asyncio
async def test_fetch_json(mock_hmc):
    mock_hmc.get("/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json").mock(
        return_value=httpx.Response(200, json={"systemUtil": {"utilization": 0.5}})
    )
    async with HMCClient(make_config()) as hmc:
        data = await hmc.fetch_json(
            "/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json"
        )
    assert data["systemUtil"]["utilization"] == 0.5


@pytest.mark.asyncio
async def test_fetch_json_invalid_body_raises_contextual_hmc_error(
    mock_hmc, monkeypatch
):
    path = "/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json"
    mock_hmc.get(path).mock(return_value=httpx.Response(200, text="not json"))
    parse_error = ValueError("x" * 500 + "excluded detail")

    def raise_parse_error(_response):
        raise parse_error

    monkeypatch.setattr(httpx.Response, "json", raise_parse_error)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await hmc.fetch_json(path)

    message = str(exc_info.value)
    assert f"GET {BASE}{path} returned invalid JSON" in message
    assert "x" * 500 in message
    assert "excluded detail" not in message
    assert exc_info.value.__cause__ is parse_error


@pytest.mark.asyncio
async def test_fetch_json_recursion_error_tags_context(mock_hmc, monkeypatch):
    """A deeply nested body raises RecursionError, not a ValueError subclass,

    so it needs its own clause to reach HMCError instead of escaping the guard.
    """
    path = "/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json"
    mock_hmc.get(path).mock(return_value=httpx.Response(200, text="{}"))

    def raise_recursion_error(_response):
        raise RecursionError

    monkeypatch.setattr(httpx.Response, "json", raise_recursion_error)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await hmc.fetch_json(path)

    message = str(exc_info.value)
    assert (
        f"GET {BASE}{path} returned invalid JSON: document nesting is too deep"
        in message
    )
    assert isinstance(exc_info.value.__cause__, RecursionError)


@pytest.mark.asyncio
async def test_fetch_json_rejects_non_object_document(mock_hmc):
    """PCM documents must be JSON objects, not arbitrary JSON values."""
    path = "/rest/api/pcm/ProcessedMetrics/system.json"
    mock_hmc.get(path).mock(return_value=httpx.Response(200, json=[{"metric": 1}]))

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="JSON list; expected an object"):
            await hmc.fetch_json(path)


@pytest.mark.asyncio
async def test_fetch_json_404_raises(mock_hmc):
    """fetch_json raises HMCError on 404 like every other client method."""
    mock_hmc.get("/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json").mock(
        return_value=httpx.Response(404, text="<error>expired</error>")
    )
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await hmc.fetch_json(
                "/rest/api/pcm/ProcessedMetrics/ManagedSystem_sys_2.json"
            )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_parse_failure_names_failing_call(mock_hmc):
    """A 200 with malformed XML surfaces as HMCError naming the failing path."""
    mock_hmc.get("/rest/api/uom/ManagedSystem").mock(
        return_value=httpx.Response(200, text="<feed><entry>")
    )
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await hmc.list_uom("ManagedSystem")
    assert "Failed to parse /rest/api/uom/ManagedSystem response" in str(exc_info.value)


@pytest.mark.asyncio
async def test_pcm_parse_failure_names_failing_call(mock_hmc):
    """A 200 with malformed PCM preferences XML surfaces as HMCError."""
    mock_hmc.get("/rest/api/pcm/ManagedSystem/sys-uuid/preferences").mock(
        return_value=httpx.Response(
            200, text="<ManagementConsolePcmPreference><unclosed>"
        )
    )
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await hmc.get_pcm_preferences("ManagedSystem", "sys-uuid")
    assert (
        "Failed to parse /rest/api/pcm/ManagedSystem/sys-uuid/preferences response"
        in str(exc_info.value)
    )


@pytest.mark.asyncio
async def test_raw_get_returns_body_and_headers(mock_hmc):
    """raw_get() returns a (body, headers) tuple so callers can inspect response headers."""
    mock_hmc.get("/rest/api/uom/VirtualSwitch").mock(
        return_value=httpx.Response(
            200,
            text="<feed/>",
            headers={"X-HMC-Schema-Version": "V1_0"},
        )
    )
    async with HMCClient(make_config()) as hmc:
        body, headers = await hmc.raw_get("/rest/api/uom/VirtualSwitch")
    assert body == "<feed/>"
    assert headers.get("x-hmc-schema-version") == "V1_0"


@pytest.mark.asyncio
async def test_raw_get_204_returns_empty_body_and_headers(mock_hmc):
    """raw_get() handles 204 No Content correctly."""
    mock_hmc.get("/rest/api/uom/empty").mock(
        return_value=httpx.Response(204, headers={"X-HMC-Schema-Version": "V1_0"})
    )
    async with HMCClient(make_config()) as hmc:
        body, headers = await hmc.raw_get("/rest/api/uom/empty")
    assert body == ""
    assert "x-hmc-schema-version" in headers


@pytest.mark.asyncio
async def test_uom_headers_sends_schema_version_when_configured(mock_hmc):
    """_uom_headers() includes X-HMC-Schema-Version when schema_version is set."""
    route = mock_hmc.get("/rest/api/uom/LogicalPartition").mock(
        return_value=httpx.Response(
            200,
            text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>",
        )
    )
    async with HMCClient(make_config(schema_version="V1_0")) as hmc:
        await hmc.list_uom("LogicalPartition")
    sent_headers = route.calls.last.request.headers
    assert sent_headers.get("x-hmc-schema-version") == "V1_0"


@pytest.mark.asyncio
async def test_uom_headers_omits_schema_version_when_not_configured(mock_hmc):
    """_uom_headers() does not include X-HMC-Schema-Version when schema_version is empty (default)."""
    route = mock_hmc.get("/rest/api/uom/LogicalPartition").mock(
        return_value=httpx.Response(
            200,
            text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>",
        )
    )
    async with HMCClient(make_config()) as hmc:
        await hmc.list_uom("LogicalPartition")
    sent_headers = route.calls.last.request.headers
    assert "x-hmc-schema-version" not in sent_headers


@pytest.mark.asyncio
async def test_uom_post_sends_schema_version_when_configured(mock_hmc):
    """_post() includes X-HMC-Schema-Version when schema_version is set."""
    route = mock_hmc.post("/rest/api/uom/LogicalPartition").mock(
        return_value=httpx.Response(
            201,
            text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>",
        )
    )
    async with HMCClient(make_config(schema_version="V1_0")) as hmc:
        await hmc._post("/rest/api/uom/LogicalPartition", b"<xml/>")
    sent_headers = route.calls.last.request.headers
    assert sent_headers.get("x-hmc-schema-version") == "V1_0"


@pytest.mark.asyncio
async def test_uom_put_sends_schema_version_when_configured(mock_hmc):
    """_put() includes X-HMC-Schema-Version when schema_version is set."""
    route = mock_hmc.put("/rest/api/uom/LogicalPartition/uuid1").mock(
        return_value=httpx.Response(
            200, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>"
        )
    )
    async with HMCClient(make_config(schema_version="V1_0")) as hmc:
        await hmc._put("/rest/api/uom/LogicalPartition/uuid1", b"<xml/>")
    sent_headers = route.calls.last.request.headers
    assert sent_headers.get("x-hmc-schema-version") == "V1_0"


@pytest.mark.asyncio
async def test_uom_delete_sends_schema_version_when_configured(mock_hmc):
    """_delete() includes X-HMC-Schema-Version when schema_version is set."""
    route = mock_hmc.delete("/rest/api/uom/LogicalPartition/uuid1").mock(
        return_value=httpx.Response(204)
    )
    async with HMCClient(make_config(schema_version="V1_0")) as hmc:
        await hmc._delete("/rest/api/uom/LogicalPartition/uuid1")
    sent_headers = route.calls.last.request.headers
    assert sent_headers.get("x-hmc-schema-version") == "V1_0"


@pytest.mark.asyncio
async def test_uom_post_omits_schema_version_when_not_configured(mock_hmc):
    """_post() does not include X-HMC-Schema-Version when schema_version is empty."""
    route = mock_hmc.post("/rest/api/uom/LogicalPartition").mock(
        return_value=httpx.Response(
            201, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>"
        )
    )
    async with HMCClient(make_config()) as hmc:
        await hmc._post("/rest/api/uom/LogicalPartition", b"<xml/>")
    assert "x-hmc-schema-version" not in route.calls.last.request.headers


@pytest.mark.asyncio
async def test_uom_put_omits_schema_version_when_not_configured(mock_hmc):
    """_put() does not include X-HMC-Schema-Version when schema_version is empty."""
    route = mock_hmc.put("/rest/api/uom/LogicalPartition/uuid1").mock(
        return_value=httpx.Response(
            200, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>"
        )
    )
    async with HMCClient(make_config()) as hmc:
        await hmc._put("/rest/api/uom/LogicalPartition/uuid1", b"<xml/>")
    assert "x-hmc-schema-version" not in route.calls.last.request.headers


@pytest.mark.asyncio
async def test_uom_delete_omits_schema_version_when_not_configured(mock_hmc):
    """_delete() does not include X-HMC-Schema-Version when schema_version is empty."""
    route = mock_hmc.delete("/rest/api/uom/LogicalPartition/uuid1").mock(
        return_value=httpx.Response(204)
    )
    async with HMCClient(make_config()) as hmc:
        await hmc._delete("/rest/api/uom/LogicalPartition/uuid1")
    assert "x-hmc-schema-version" not in route.calls.last.request.headers


# ---------------------------------------------------------------------- #
# get_job / wait_for_job — SELF-link-based polling (issue #95)
# ---------------------------------------------------------------------- #

# A handle an earlier release stored: the job entry's UUID as job_id, paired
# with the submission's `jobs/{JobID}` SELF link. V10R3 refuses the UUID on the
# global path (captured), so the link decides the read.
_JOB_HREF = f"/rest/api/uom/jobs/{JOB_ID}"
_LEGACY_PATH, _LEGACY_REFUSED = live_response("rest-job-entry-uuid-refused")
_LEGACY_ID = _LEGACY_PATH.rsplit("/", 1)[-1]


@pytest.mark.asyncio
async def test_get_job_uses_href_when_provided(mock_hmc):
    """get_job(uuid, job_href=...) GETs the exact href, not /rest/api/uom/Job/{uuid}."""
    href_route = mock_hmc.get(_JOB_HREF).mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )
    global_route = mock_hmc.get(_LEGACY_PATH).mock(return_value=_LEGACY_REFUSED)
    async with HMCClient(make_config()) as hmc:
        result = await hmc.get_job_entry(_LEGACY_ID, job_href=_JOB_HREF)
    assert href_route.called
    assert not global_route.called
    assert result is not None
    assert result["Resource"]["Status"] == "RUNNING"


@pytest.mark.asyncio
async def test_get_job_falls_back_to_global_path_when_no_href(mock_hmc):
    """get_job(uuid) without job_href uses the documented global jobs path."""
    route = mock_hmc.get("/rest/api/uom/jobs/1787837921266").mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )
    async with HMCClient(make_config()) as hmc:
        result = await hmc.get_job_entry("1787837921266")
    assert route.called
    assert result is not None


@pytest.mark.asyncio
async def test_get_job_global_path_propagates_http_error(mock_hmc):
    mock_hmc.get("/rest/api/uom/jobs/1787837921266").mock(
        return_value=live_response("rest-job-not-found")[1]
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            HMCError, match="GET /rest/api/uom/jobs/1787837921266 failed"
        ):
            await hmc.get_job_entry("1787837921266")


@pytest.mark.asyncio
async def test_delete_job_uses_documented_global_path(mock_hmc):
    route = mock_hmc.delete("/rest/api/uom/jobs/1787837921266").mock(
        return_value=httpx.Response(204)
    )

    async with HMCClient(make_config()) as hmc:
        await hmc.delete_job("1787837921266")

    assert route.called


@pytest.mark.asyncio
async def test_delete_job_prefers_self_href(mock_hmc):
    route = mock_hmc.delete(_JOB_HREF).mock(return_value=httpx.Response(204))

    async with HMCClient(make_config()) as hmc:
        await hmc.delete_job(_LEGACY_ID, job_href=_JOB_HREF)

    assert route.called


@pytest.mark.asyncio
async def test_delete_job_propagates_http_error(mock_hmc):
    mock_hmc.delete("/rest/api/uom/jobs/1787837921266").mock(
        return_value=httpx.Response(500, text="Delete failed")
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            HMCError, match="DELETE /rest/api/uom/jobs/1787837921266 failed"
        ):
            await hmc.delete_job("1787837921266")


# ---------------------------------------------------------------------------
# Both job methods build one path and refuse it the same way (ADR 0149)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_job_entry", "delete_job"])
@pytest.mark.parametrize(
    "job_id",
    [
        "",
        "a/b",
        "a%2Fb",
        # `?` and `#` are both `[^/]`, so the identifier segment admitted them:
        # httpx turned the first into a query and dropped the second, which made
        # `delete_job("j#f")` delete job `j` (issue #825).
        "j?x=1",
        "j#f",
        "j%3Fx=1",
        "j%23f",
    ],
)
async def test_a_job_id_that_leaves_the_job_path_is_refused(mock_hmc, method, job_id):
    """`get_job_entry` sent every one of these; `delete_job` sent the last four."""
    sent = mock_hmc.route(method__in=("GET", "DELETE")).mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match=r"^job_id refused: it does not address"):
            await getattr(hmc, method)(job_id)

    assert not sent.called, "a refused job path must not reach the wire"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_job_entry", "delete_job"])
async def test_a_non_job_href_is_refused_naming_job_href(mock_hmc, method):
    """The refusal names the argument the path came from, not always `job_href`."""
    sent = mock_hmc.route(method__in=("GET", "DELETE")).mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match=r"^job_href refused: it does not address"):
            await getattr(hmc, method)(
                "1787837921266", job_href="/rest/api/uom/HmcUser/root"
            )

    assert not sent.called, "a refused job path must not reach the wire"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_job_entry", "delete_job"])
@pytest.mark.parametrize(
    "path",
    [
        "/rest/api/uom/jobs/j%2D1",
        "/rest/api/uom/jobs/1787837921266%2D1",
    ],
)
async def test_job_methods_preserve_non_structural_encoding(mock_hmc, method, path):
    route = mock_hmc.route(
        url=f"https://hmc.test{path}", method__in=("GET", "DELETE")
    ).mock(return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY))

    async with HMCClient(make_config()) as hmc:
        result = await getattr(hmc, method)(
            "response-job-id", job_href=f"https://hmc.test{path}?group=None#self"
        )

    assert route.called
    assert route.calls.last.request.url.raw_path.decode() == path
    if method == "get_job_entry":
        assert result["Resource"]["Status"] == "RUNNING"
    else:
        assert result is None


# The SELF link a V10R3 HMC renders on every read of a job: the global jobs path,
# the JobID, and a UUID that changes on every read of the same job. The HMC
# refuses the link itself with HTTP 400 REST000B (live capture at 2281afd2, #1160).
_READ_UUID = "65680cb7-0000-4000-8000-000000000002"
_HMC_SELF_LINK = f"https://hmc.test/rest/api/uom/jobs/1787837921263/{_READ_UUID}"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_job_entry", "delete_job"])
async def test_job_methods_address_the_hmc_self_link_by_its_job_id(mock_hmc, method):
    """The per-read segment addresses nothing stable; the JobID path resolves."""
    by_job_id = mock_hmc.route(
        url="https://hmc.test/rest/api/uom/jobs/1787837921263",
        method__in=("GET", "DELETE"),
    ).mock(return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY))
    by_read_uuid = mock_hmc.route(
        url=_HMC_SELF_LINK, method__in=("GET", "DELETE")
    ).mock(return_value=httpx.Response(400, text="REST000B The URL is not valid."))

    async with HMCClient(make_config()) as hmc:
        await getattr(hmc, method)("1787837921263", job_href=_HMC_SELF_LINK)

    assert by_job_id.called
    assert not by_read_uuid.called


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_job_entry", "delete_job"])
@pytest.mark.parametrize(
    "path",
    [
        # Only a UUID-shaped trailing segment is dropped; anything else stays in
        # the path and is refused whole.
        "/rest/api/uom/jobs/1787837921263/HmcUser",
        "/rest/api/uom/jobs/1787837921263/..",
        # Dropping the per-read segment must not let an unsafe JobID segment
        # through: the refusal runs on the path that would be requested.
        f"/rest/api/uom/jobs/..%2F..%2Fweb%2FHmcUser/{_READ_UUID}",
        f"/rest/api/uom/jobs/j%3Fx=1/{_READ_UUID}",
        f"/rest/api/web/jobs/1787837921263/{_READ_UUID}",
    ],
)
async def test_a_self_link_lookalike_is_refused(mock_hmc, method, path):
    sent = mock_hmc.route(method__in=("GET", "DELETE")).mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match=r"^job_href refused: it does not address"):
            await getattr(hmc, method)("1787837921263", job_href=path)

    assert not sent.called, "a refused job path must not reach the wire"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_job_entry", "delete_job"])
async def test_a_job_id_in_the_self_link_shape_is_still_refused(mock_hmc, method):
    """The SELF-link allowance is for job_href only; job_id stays one segment."""
    sent = mock_hmc.route(method__in=("GET", "DELETE")).mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match=r"^job_id refused: it does not address"):
            await getattr(hmc, method)(f"1787837921263/{_READ_UUID}")

    assert not sent.called, "a refused job path must not reach the wire"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_job_entry", "delete_job"])
async def test_job_methods_require_literal_collection_spelling(mock_hmc, method):
    sent = mock_hmc.route(method__in=("GET", "DELETE")).mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match=r"^job_href refused:"):
            await getattr(hmc, method)("j-1", job_href="/rest/api/uom/job%73/j-1")

    assert not sent.called


@pytest.mark.asyncio
async def test_wait_for_job_uses_href_when_provided(mock_hmc):
    """wait_for_job passes job_href to get_job so polling uses the SELF link."""
    href_route = mock_hmc.get(_JOB_HREF).mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    global_route = mock_hmc.get(_LEGACY_PATH).mock(return_value=_LEGACY_REFUSED)
    async with HMCClient(make_config()) as hmc:
        result = await hmc.wait_for_job_entry(
            _LEGACY_ID, timeout_seconds=5, poll_interval=1, job_href=_JOB_HREF
        )
    assert href_route.called
    assert not global_route.called
    assert result is not None
    assert result["Resource"]["Status"] == "COMPLETED_OK"


@pytest.mark.asyncio
async def test_wait_for_job_caps_sleep_and_does_not_poll_after_deadline(
    monkeypatch, mock_hmc
):
    now = 10.0
    loop = AsyncMock()
    loop.time = lambda: now
    get_job = AsyncMock(return_value={"Resource": {"Status": "RUNNING"}})

    async def advance(delay):
        nonlocal now
        now += delay

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: loop)
    sleep = AsyncMock(side_effect=advance)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async with HMCClient(make_config()) as hmc:
        hmc.get_job_entry = get_job
        result = await hmc.wait_for_job_entry(
            "job-1", timeout_seconds=2, poll_interval=5, job_href="/jobs/job-1"
        )

    assert result == {"Resource": {"Status": "RUNNING"}}
    sleep.assert_awaited_once_with(2.0)
    get_job.assert_awaited_once_with("job-1", job_href="/jobs/job-1")


@pytest.mark.asyncio
async def test_wait_for_job_timeout_zero_still_polls_once(monkeypatch, mock_hmc):
    get_job = AsyncMock(return_value={"Resource": {"Status": "RUNNING"}})
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async with HMCClient(make_config()) as hmc:
        hmc.get_job_entry = get_job
        await hmc.wait_for_job_entry("job-1", timeout_seconds=0)

    get_job.assert_awaited_once_with("job-1", job_href=None)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "COMPLETED_WITH_WARNINGS",
        "FAILED_TO_START",
        "FAILED_BEFORE_COMPLETION",
        "FAILED_BEFORE_COMPLETION_RETRY",
        "CANCELED_BEFORE_START",
        "CANCELED_WHILE_RUNNING",
    ],
)
async def test_wait_for_job_stops_at_documented_terminal_status(
    monkeypatch, mock_hmc, status
):
    get_job = AsyncMock(return_value={"Resource": {"Status": status}})
    sleep = AsyncMock(side_effect=AssertionError(f"{status} was not terminal"))
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async with HMCClient(make_config(verify_ssl=True)) as hmc:
        hmc.get_job_entry = get_job
        result = await hmc.wait_for_job_entry("job-1", timeout_seconds=30)

    assert result == {"Resource": {"Status": status}}
    get_job.assert_awaited_once_with("job-1", job_href=None)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_job_tolerates_empty_resource(monkeypatch, mock_hmc):
    get_job = AsyncMock(return_value={"Resource": ""})
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async with HMCClient(make_config(verify_ssl=True)) as hmc:
        hmc.get_job_entry = get_job
        result = await hmc.wait_for_job_entry("job-1", timeout_seconds=0)

    assert result == {"Resource": ""}
    get_job.assert_awaited_once_with("job-1", job_href=None)
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timeout_seconds", "poll_interval", "message"),
    [
        (-1, 5, "timeout_seconds"),
        (5, -1, "poll_interval"),
        (5, 0, "poll_interval"),
    ],
)
async def test_wait_for_job_rejects_invalid_timing_values(
    mock_hmc, timeout_seconds, poll_interval, message
):
    async with HMCClient(make_config()) as hmc:
        hmc.get_job_entry = AsyncMock()
        with pytest.raises(ValueError, match=message):
            await hmc.wait_for_job_entry(
                "job-1",
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )
        hmc.get_job_entry.assert_not_awaited()


# The captured web+xml JobResponse read (#1161).
_JOB_WEB_HREF = "/rest/api/uom/jobs/1778083847656"


@pytest.mark.asyncio
async def test_get_job_with_href_uses_web_xml_accept(mock_hmc):
    """get_job(uuid, job_href=...) sends Accept: web+xml, not uom+xml."""
    # The route matches any GET on that path; we verify the Accept header sent
    route = mock_hmc.get(_JOB_WEB_HREF).mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    async with HMCClient(make_config()) as hmc:
        result = await hmc.get_job_entry("1787837921266", job_href=_JOB_WEB_HREF)
    assert route.called
    sent_accept = route.calls.last.request.headers.get("accept", "")
    assert "powervm.web+xml" in sent_accept, (
        f"Expected web+xml Accept, got: {sent_accept}"
    )
    assert result is not None
    assert result["Resource"]["Status"] == "COMPLETED_OK"


@pytest.mark.asyncio
async def test_wait_for_job_recognises_completed_ok(mock_hmc):
    """wait_for_job treats COMPLETED_OK as a terminal state (web+xml JobResponse)."""
    mock_hmc.get(_JOB_WEB_HREF).mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    async with HMCClient(make_config()) as hmc:
        result = await hmc.wait_for_job_entry(
            "1778083847656", timeout_seconds=5, poll_interval=1, job_href=_JOB_WEB_HREF
        )
    assert result is not None
    assert result["Resource"]["Status"] == "COMPLETED_OK"


# Captured from an HMC reporting X-HMC-Schema-Version V1_17_0 on its regular uom
# feeds (#787), trimmed from 57 operations to two. Only the host, the UUIDs and
# the SELF href were shortened; element names, attributes and nesting are as the
# firmware sends them.
#
# The endpoint answers with a bare <entry>, not a <feed>, and the entry holds one
# OperationSet carrying every operation the type defines -- not one entry per
# operation. The two operations below differ deliberately: PowerOn has several
# parameters and a single result, ResetConnection omits <AllPossibleParameters>
# altogether. That asymmetry is what
# test_list_operations_collapses_single_child_elements pins.
OPERATIONS_ENTRY = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
    <id>11111111-1111-1111-1111-111111111111</id>
    <title>OperationSet</title>
    <published>2026-09-14T23:11:30.597Z</published>
    <link rel="SELF" href="https://hmc.example.com/rest/api/uom/ManagedSystem/operations"/>
    <content type="application/vnd.ibm.powervm.web+xml; type=OperationSet">
        <OperationSet:OperationSet
            xmlns:OperationSet="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"
            xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"
            schemaVersion="V1_0">
            <Metadata>
                <Atom/>
            </Metadata>
            <SetName kb="ROR" kxe="false">ManagedSystem</SetName>
            <DefinedOperations kxe="false" kb="ROO" schemaVersion="V1_0">
                <Metadata>
                    <Atom/>
                </Metadata>
                <Operation schemaVersion="V1_0">
                    <Metadata>
                        <Atom/>
                    </Metadata>
                    <OperationName kb="ROR" kxe="false">PowerOn</OperationName>
                    <GroupName kxe="false" kb="ROR">ManagedSystem</GroupName>
                    <ProgressType kb="ROR" kxe="false">LINEAR</ProgressType>
                    <AllPossibleParameters kb="ROO" kxe="false" schemaVersion="V1_0">
                        <Metadata>
                            <Atom/>
                        </Metadata>
                        <OperationParameter schemaVersion="V1_0">
                            <Metadata>
                                <Atom/>
                            </Metadata>
                            <ParameterName kb="ROR" kxe="false">operation</ParameterName>
                        </OperationParameter>
                        <OperationParameter schemaVersion="V1_0">
                            <Metadata>
                                <Atom/>
                            </Metadata>
                            <ParameterName kb="ROR" kxe="false">keylock</ParameterName>
                        </OperationParameter>
                    </AllPossibleParameters>
                    <AllPossibleResults kb="ROO" kxe="false" schemaVersion="V1_0">
                        <Metadata>
                            <Atom/>
                        </Metadata>
                        <OperationParameter schemaVersion="V1_0">
                            <Metadata>
                                <Atom/>
                            </Metadata>
                            <ParameterName kb="ROR" kxe="false">ErrorData</ParameterName>
                        </OperationParameter>
                    </AllPossibleResults>
                </Operation>
                <Operation schemaVersion="V1_0">
                    <Metadata>
                        <Atom/>
                    </Metadata>
                    <OperationName kb="ROR" kxe="false">ResetConnection</OperationName>
                    <GroupName kxe="false" kb="ROR">ManagedSystem</GroupName>
                    <ProgressType kb="ROR" kxe="false">LINEAR</ProgressType>
                    <AllPossibleResults kb="ROO" kxe="false" schemaVersion="V1_0">
                        <Metadata>
                            <Atom/>
                        </Metadata>
                        <OperationParameter schemaVersion="V1_0">
                            <Metadata>
                                <Atom/>
                            </Metadata>
                            <ParameterName kb="ROR" kxe="false">returnCode</ParameterName>
                        </OperationParameter>
                        <OperationParameter schemaVersion="V1_0">
                            <Metadata>
                                <Atom/>
                            </Metadata>
                            <ParameterName kb="ROR" kxe="false">result</ParameterName>
                        </OperationParameter>
                    </AllPossibleResults>
                </Operation>
            </DefinedOperations>
        </OperationSet:OperationSet>
    </content>
</entry>
"""

# The child anchor, same envelope with SetName naming the child type. Trimmed to
# the one DISCRETE operation, which carries the <AllDiscreteStates> block the
# LINEAR operations above have no equivalent of -- and, being alone under
# <DefinedOperations>, parses to a bare dict rather than a list.
OPERATIONS_ENTRY_CHILD = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<entry xmlns="http://www.w3.org/2005/Atom">
    <id>22222222-2222-2222-2222-222222222222</id>
    <title>OperationSet</title>
    <published>2026-09-14T23:11:31.297Z</published>
    <link rel="SELF" href="https://hmc.example.com/rest/api/uom/LogicalPartition/operations"/>
    <content type="application/vnd.ibm.powervm.web+xml; type=OperationSet">
        <OperationSet:OperationSet
            xmlns:OperationSet="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"
            xmlns="http://www.ibm.com/xmlns/systems/power/firmware/web/mc/2012_10/"
            schemaVersion="V1_0">
            <Metadata>
                <Atom/>
            </Metadata>
            <SetName kb="ROR" kxe="false">LogicalPartition</SetName>
            <DefinedOperations kxe="false" kb="ROO" schemaVersion="V1_0">
                <Metadata>
                    <Atom/>
                </Metadata>
                <Operation schemaVersion="V1_0">
                    <Metadata>
                        <Atom/>
                    </Metadata>
                    <OperationName kb="ROR" kxe="false">ApplyProfile</OperationName>
                    <GroupName kxe="false" kb="ROR">LogicalPartition</GroupName>
                    <ProgressType kb="ROR" kxe="false">DISCRETE</ProgressType>
                    <AllPossibleResults kb="ROO" kxe="false" schemaVersion="V1_0">
                        <Metadata>
                            <Atom/>
                        </Metadata>
                        <OperationParameter schemaVersion="V1_0">
                            <Metadata>
                                <Atom/>
                            </Metadata>
                            <ParameterName kb="ROR" kxe="false">returnCode</ParameterName>
                        </OperationParameter>
                        <OperationParameter schemaVersion="V1_0">
                            <Metadata>
                                <Atom/>
                            </Metadata>
                            <ParameterName kb="ROR" kxe="false">result</ParameterName>
                        </OperationParameter>
                    </AllPossibleResults>
                    <AllDiscreteStates kb="ROO" kxe="false" schemaVersion="V1_0">
                        <Metadata>
                            <Atom/>
                        </Metadata>
                        <NLSStaticMessage schemaVersion="V1_0">
                            <Metadata>
                                <Atom/>
                            </Metadata>
                            <UntranslatedMessage kxe="false" kb="ROR">ApplyProfile not yet started</UntranslatedMessage>
                            <MessageKey kb="ROR" kxe="false">NOT STARTED</MessageKey>
                        </NLSStaticMessage>
                        <NLSStaticMessage schemaVersion="V1_0">
                            <Metadata>
                                <Atom/>
                            </Metadata>
                            <UntranslatedMessage kxe="false" kb="ROR">ApplyProfile Completed</UntranslatedMessage>
                            <MessageKey kb="ROR" kxe="false">APPLYPROFILE COMPLETED</MessageKey>
                        </NLSStaticMessage>
                    </AllDiscreteStates>
                </Operation>
            </DefinedOperations>
        </OperationSet:OperationSet>
    </content>
</entry>
"""

_PARENT_UUID = "44444444-4444-4444-4444-444444444444"


def _operations(entry):
    """The OperationSet's operations, normalised to a list.

    Every caller of ``list_operations`` needs this, which is the point of
    test_list_operations_collapses_single_child_elements.
    """
    defined = entry["Resource"]["DefinedOperations"]["Operation"]
    return [defined] if isinstance(defined, dict) else defined


@pytest.mark.asyncio
async def test_list_operations_reads_the_root_anchor(mock_hmc):
    path = "/rest/api/uom/ManagedSystem/operations"
    route = mock_hmc.get(path).mock(
        return_value=httpx.Response(200, text=OPERATIONS_ENTRY)
    )

    async with HMCClient(make_config()) as hmc:
        entries, _ = await hmc.list_operations("ManagedSystem")

    assert route.calls.last.request.url.path == path
    # Pins against a *wrong* Accept only. httpx's own default is already "*/*",
    # so this assertion would still hold if the method stopped sending the
    # header at all; what it catches is a typed uom Accept, which is the failure
    # this endpoint is actually at risk of -- the content element is in the
    # web/mc namespace, so a uom media type is the wrong guess, not a stricter one.
    assert route.calls.last.request.headers["Accept"] == "*/*"
    # One OperationSet, not one entry per operation. A caller that reads this
    # result as a list of operations is reading the wrong level.
    assert len(entries) == 1
    assert entries[0]["ResourceType"] == "OperationSet"
    assert entries[0]["Resource"]["SetName"] == "ManagedSystem"
    assert [op["OperationName"] for op in _operations(entries[0])] == [
        "PowerOn",
        "ResetConnection",
    ]


@pytest.mark.asyncio
async def test_list_operations_reads_the_child_anchor(mock_hmc):
    path = f"/rest/api/uom/ManagedSystem/{_PARENT_UUID}/LogicalPartition/operations"
    route = mock_hmc.get(path).mock(
        return_value=httpx.Response(200, text=OPERATIONS_ENTRY_CHILD)
    )

    async with HMCClient(make_config()) as hmc:
        entries, _ = await hmc.list_operations(
            "LogicalPartition",
            parent_type="ManagedSystem",
            parent_uuid=_PARENT_UUID,
        )

    assert route.calls.last.request.url.path == path
    # Same envelope as the root anchor; SetName is what names the child type.
    assert entries[0]["ResourceType"] == "OperationSet"
    assert entries[0]["Resource"]["SetName"] == "LogicalPartition"
    assert [op["OperationName"] for op in _operations(entries[0])] == ["ApplyProfile"]


@pytest.mark.asyncio
async def test_list_operations_collapses_single_child_elements(mock_hmc):
    """Repeated elements parse to a list, or to a bare dict when there is one.

    ``element_to_dict`` keys children by tag, so an element's arity in the
    returned structure depends on how many siblings the HMC happened to send --
    not on the schema. Iterating such a value without normalising walks *dict
    keys* when the count is one, silently and without raising. This is inherited
    behaviour shared with every other read in this client, but ``/operations``
    is where it bites hardest: consumers want to walk the operations, their
    parameters and their results, and all three collapse.

    Both directions are asserted from one fixture pair so the contrast cannot
    drift apart. ``_operations`` above is the normalisation callers need.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/operations").mock(
        return_value=httpx.Response(200, text=OPERATIONS_ENTRY)
    )
    mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{_PARENT_UUID}/LogicalPartition/operations"
    ).mock(return_value=httpx.Response(200, text=OPERATIONS_ENTRY_CHILD))

    async with HMCClient(make_config()) as hmc:
        root, _ = await hmc.list_operations("ManagedSystem")
        child, _ = await hmc.list_operations(
            "LogicalPartition",
            parent_type="ManagedSystem",
            parent_uuid=_PARENT_UUID,
        )

    # Two operations under the root anchor, one under the child anchor.
    assert isinstance(root[0]["Resource"]["DefinedOperations"]["Operation"], list)
    assert isinstance(child[0]["Resource"]["DefinedOperations"]["Operation"], dict)

    # And again one level down, on the same field name within one response:
    # PowerOn declares a single result, ResetConnection declares two.
    power_on, reset_connection = _operations(root[0])
    assert isinstance(power_on["AllPossibleResults"]["OperationParameter"], dict)
    assert isinstance(
        reset_connection["AllPossibleResults"]["OperationParameter"], list
    )

    # An operation that takes no parameters omits the element rather than
    # sending it empty, so callers must use .get() and not index it.
    assert "AllPossibleParameters" not in reset_connection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"X-HMC-Schema-Version": "V1_0"}, "V1_0"), ({}, None)],
)
async def test_list_operations_returns_the_response_schema_version(
    mock_hmc, headers, expected
):
    mock_hmc.get("/rest/api/uom/ManagedSystem/operations").mock(
        return_value=httpx.Response(200, text=OPERATIONS_ENTRY, headers=headers)
    )

    async with HMCClient(make_config()) as hmc:
        _, schema_version = await hmc.list_operations("ManagedSystem")

    assert schema_version == expected


@pytest.mark.asyncio
async def test_list_operations_unknown_type_raises_hmc_error_with_status(mock_hmc):
    """An unknown type is rejected at the URL, with 400, not 404.

    Status and body are a live capture (V1_17_0, #797). The firmware validates
    the type name before reaching any handler that would look up operations, so
    it answers 400 ``INVALID_URL`` naming the type it did not recognise. The
    status is asserted because this method's only error contract is to surface
    whatever the HMC returned -- a mock inventing 404 would let a regression
    that swallowed the real status still pass.
    """
    mock_hmc.get("/rest/api/uom/NoSuchType/operations").mock(
        return_value=httpx.Response(
            400,
            text=(
                '<HttpErrorResponse xmlns="http://www.ibm.com/xmlns/systems/power'
                '/firmware/web/mc/2012_10/">'
                "<HTTPStatus>400</HTTPStatus>"
                "<RequestURI>/rest/api/uom/NoSuchType/operations</RequestURI>"
                "<ReasonCode>INVALID_URL</ReasonCode>"
                "<Message>REST000B The URL presented to the Management Console REST "
                "Web Services is not valid.REST000E Unrecognized root REST type of "
                "NoSuchType.</Message>"
                "</HttpErrorResponse>"
            ),
        )
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await hmc.list_operations("NoSuchType")

    assert raised.value.status_code == 400
    assert "Unrecognized root REST type of NoSuchType" in str(raised.value)


@pytest.mark.asyncio
async def test_create_child_sends_write_headers_without_schema_version(mock_hmc):
    """The adapter PUT sends Accept */*, a typed Content-Type, and no schema header (ADR 0178)."""
    lpar = "00000000-0000-4000-8000-000000000001"
    path = f"/rest/api/uom/LogicalPartition/{lpar}/VirtualSCSIClientAdapter"
    route = mock_hmc.put(path).mock(return_value=httpx.Response(200, text=""))

    async with HMCClient(make_config(schema_version="V1_0")) as hmc:
        await hmc.create_child(
            "LogicalPartition", lpar, "VirtualSCSIClientAdapter", "<x/>"
        )

    request = route.calls.last.request
    assert request.headers["Accept"] == "*/*"
    assert request.headers["Content-Type"] == (
        "application/vnd.ibm.powervm.uom+xml; type=VirtualSCSIClientAdapter"
    )
    assert "X-HMC-Schema-Version" not in request.headers


@pytest.mark.asyncio
async def test_create_child_400_surfaces_hmc_schema_message(mock_hmc):
    """A 400 REST0001 schema rejection reports the HMC's message, not a generic failure (#961).

    The body uses the HttpErrorResponse shape of the INVALID_URL test above. The message text
    is the one #961 recorded; whether V10R3 puts the schema detail in <Message> is unobserved.
    """
    lpar = "00000000-0000-4000-8000-000000000001"
    path = f"/rest/api/uom/LogicalPartition/{lpar}/VirtualSCSIClientAdapter"
    mock_hmc.put(path).mock(
        return_value=httpx.Response(
            400,
            text=(
                '<HttpErrorResponse xmlns="http://www.ibm.com/xmlns/systems/power'
                '/firmware/web/mc/2012_10/">'
                "<HTTPStatus>400</HTTPStatus>"
                f"<RequestURI>{path}</RequestURI>"
                "<ReasonCode>Unknown internal error.</ReasonCode>"
                "<Message>REST0001 Failed to unmarshal input payload. Value 'CUD' is not "
                "facet-valid with respect to enumeration '[ROR]'.</Message>"
                "</HttpErrorResponse>"
            ),
        )
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await hmc.create_child(
                "LogicalPartition", lpar, "VirtualSCSIClientAdapter", "<x/>"
            )

    assert raised.value.status_code == 400
    assert "enumeration '[ROR]'" in str(raised.value)
    assert "<Message>" not in str(raised.value)


# The captured V10R3 answers to a LogicalPartition create, read and miss (#1161).
_CAPTURED_SYSTEM_UUID = "0000000e-abcd-4ef0-8abc-00000000000e"


@pytest.mark.asyncio
async def test_create_logical_partition_parses_the_captured_created_entry(mock_hmc):
    path, response = live_response("rest-lpar-create-ok")
    mock_hmc.put(path).mock(return_value=response)

    async with HMCClient(make_config()) as hmc:
        entry = await hmc.create_logical_partition(_CAPTURED_SYSTEM_UUID, "<x/>")

    assert entry is not None
    assert entry["UUID"] == "00000010-ABCD-4EF0-8ABC-000000000010"
    assert entry["Resource"]["PartitionName"] == "sys-R1-pcie-e1164a"
    assert entry["Resource"]["PartitionState"] == "not activated"


@pytest.mark.asyncio
async def test_create_logical_partition_surfaces_the_captured_schema_refusal(mock_hmc):
    """V10R3 puts the REST0001 unmarshal detail in the atom-wrapped <Message>."""
    path, response = live_response("rest-lpar-create-refused")
    mock_hmc.put(path).mock(return_value=response)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await hmc.create_logical_partition(_CAPTURED_SYSTEM_UUID, "<x/>")

    assert raised.value.status_code == 400
    assert "REST0001" in raised.value.body
    assert "must appear on element 'PartitionProcessorConfiguration'" in str(
        raised.value
    )


@pytest.mark.asyncio
async def test_get_logical_partition_parses_the_captured_entry(mock_hmc):
    path, response = live_response("rest-lpar-entry")
    mock_hmc.get(path).mock(return_value=response)

    async with HMCClient(make_config()) as hmc:
        entry = await hmc.get_logical_partition(path.rsplit("/", 1)[-1])

    assert entry is not None
    assert entry["UUID"] == path.rsplit("/", 1)[-1]
    assert entry["Resource"]["PartitionName"] == "sys-R1-pcie-w1161b"
    assert entry["Resource"]["PartitionID"] == "2"


@pytest.mark.asyncio
async def test_get_logical_partition_raises_the_captured_not_found(mock_hmc):
    path, response = live_response("rest-lpar-not-found")
    mock_hmc.get(path).mock(return_value=response)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await hmc.get_logical_partition(path.rsplit("/", 1)[-1])

    assert raised.value.status_code == 404
    assert "REST029B" in raised.value.body


@pytest.mark.asyncio
async def test_list_operations_204_returns_no_entries(mock_hmc):
    mock_hmc.get("/rest/api/uom/ManagedSystem/operations").mock(
        return_value=httpx.Response(204, headers={"X-HMC-Schema-Version": "V1_0"})
    )

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_operations("ManagedSystem") == ([], "V1_0")


@pytest.mark.asyncio
async def test_list_operations_empty_200_body_raises_hmc_error(mock_hmc):
    """An empty 200 body is a malformed feed, not an empty result.

    This method carries no empty-body guard, unlike its sibling uom reads: they
    need one because ``_get`` collapses 204 to "" and cannot tell the two apart,
    while this method returns on 204 before parsing. That makes the guard
    unreachable here, so an empty 200 reaches ``_parse_feed`` and is reported.
    Without this case the decision lives only in a source comment.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/operations").mock(
        return_value=httpx.Response(200, text="")
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="Failed to parse"):
            await hmc.list_operations("ManagedSystem")


@pytest.mark.asyncio
async def test_list_operations_rejects_a_non_uuid_parent(mock_hmc):
    """Refused before transport: the request is never attempted."""
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="parent_uuid must be a UUID"):
            await hmc.list_operations(
                "LogicalPartition",
                parent_type="ManagedSystem",
                parent_uuid="not-a-uuid",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"parent_type": "ManagedSystem"},
        {"parent_uuid": _PARENT_UUID},
    ],
)
async def test_list_operations_requires_both_parent_arguments(mock_hmc, kwargs):
    """Refused before transport: the request is never attempted."""
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="must be given together"):
            await hmc.list_operations("LogicalPartition", **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        # Root anchor: resource_type is the only interpolated segment.
        (("../web/Logon",), {}),
        # Child anchor: parent_type is interpolated too, and is refused on the
        # same guard. Both anchors are covered because each interpolates a
        # different argument into the path.
        (
            ("LogicalPartition",),
            {"parent_type": "../../web", "parent_uuid": _PARENT_UUID},
        ),
        (
            ("../web/Logon",),
            {"parent_type": "ManagedSystem", "parent_uuid": _PARENT_UUID},
        ),
    ],
)
async def test_list_operations_rejects_a_dot_segment_type(mock_hmc, args, kwargs):
    """Refused before transport: the request is never attempted.

    ``ValueError``, not ``HMCError``: a ``..`` type fails the resource-type
    grammar, so the boundary check fires before the path ever reaches the waist
    guard (ADR 0143). The property this test exists for is unchanged -- nothing
    is built -- and ``_reject_dot_segments`` still owns a ``..`` *instance*
    segment, which no boundary check sees.
    """
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="must be an HMC resource type name"):
            await hmc.list_operations(*args, **kwargs)


# ---------------------------------------------------------------------------
# list_quick_properties (#788) -- the /quick discovery anchors.
#
# These bodies follow the structural capture taken against FW950/P10 and
# recorded on PR #800. The document root is <entry>, not <feed>; the collection
# sits under <content>; and each <QuickProperty> carries <Metadata><Atom>,
# <RESTElement>, <Nickname> and <Description>. The nicknames below are that
# run's verbatim output. Descriptions are placeholders except where a test
# needs the captured text, which it quotes inline and says so.
#
# The sibling that matters is <Description>: every QuickProperty has one, and
# LogicalPartition defines a property whose *name* is also "Description", so a
# parse that read the wrong element would still return plausible strings.
# test_list_quick_properties_returns_nicknames_not_descriptions pins it.
#
# The lowercase /quick/all anchor has no tests because it does not exist:
# FW950 answered 400 on both the root and the child form, so the method offers
# no way to build that path (ADR 0140).
_MANAGED_SYSTEM_NICKNAMES = [
    "ProcessorThrottling",
    "BMCVersion",
    "Description",
    "ConfigurableSystemMemory",
    "SystemFirmware",
    "SystemType",
    "IsNotPowerVMManagementController",
    "PermanentSystemProcessors",
]
_LOGICAL_PARTITION_NICKNAMES = [
    "ProgressState",
    "Description",
    "MemoryMode",
    "MigrationState",
    "PowerManagementMode",
    "OperatingSystemVersion",
    "PartitionID",
    "IsVirtualServiceAttentionLEDOn",
]


def _quick_property_entry(rest_element: str, *properties: str | tuple[str, str]) -> str:
    """The <entry> FW950 returns for a /quick anchor, as captured on PR #800.

    Each entry in *properties* is a nickname, or a (nickname, description) pair
    when the test cares about the Description sibling.
    """
    body = ""
    for item in properties:
        name, description = (
            item if isinstance(item, tuple) else (item, f"About {item}.")
        )
        body += (
            "<QuickProperty>"
            "<Metadata><Atom/></Metadata>"
            f"<RESTElement>{rest_element}</RESTElement>"
            f"<Nickname>{name}</Nickname>"
            f"<Description>{description}</Description>"
            "</QuickProperty>"
        )
    return (
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        "<id>00000000-0000-0000-0000-000000000000</id>"
        "<title>QuickPropertyCollection</title>"
        "<author><name>IBM Power Systems Management Console</name></author>"
        "<content>"
        '<QuickProperty_Collection xmlns="http://www.ibm.com/xmlns/systems/power'
        '/firmware/uom/mc/2012_10/">'
        "<Metadata><Atom/></Metadata>"
        f"{body}"
        "</QuickProperty_Collection>"
        "</content></entry>"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_type", "kwargs", "path", "expected"),
    [
        (
            "ManagedSystem",
            {},
            "/rest/api/uom/ManagedSystem/quick",
            _MANAGED_SYSTEM_NICKNAMES,
        ),
        (
            "LogicalPartition",
            {"parent_type": "ManagedSystem", "parent_uuid": _PARENT_UUID},
            f"/rest/api/uom/ManagedSystem/{_PARENT_UUID}/LogicalPartition/quick",
            _LOGICAL_PARTITION_NICKNAMES,
        ),
    ],
)
async def test_list_quick_properties_reads_the_live_anchors(
    mock_hmc, resource_type, kwargs, path, expected
):
    """The two anchors the live run found working: root and child, bare /quick."""
    route = mock_hmc.get(path).mock(
        return_value=httpx.Response(
            200, text=_quick_property_entry(resource_type, *expected)
        )
    )

    async with HMCClient(make_config()) as hmc:
        names, _ = await hmc.list_quick_properties(resource_type, **kwargs)

    assert route.calls.last.request.url.path == path
    # Pins against a *wrong* Accept only. httpx's own default is already "*/*",
    # so this would still hold if the method stopped sending the header; what
    # it catches is a typed uom Accept, which quick/ endpoints answer with 406
    # -- the constraint get_quick_property records beside its own path.
    assert route.calls.last.request.headers["Accept"] == "*/*"
    assert names == expected


def test_list_quick_properties_has_no_all_properties_argument():
    """The lowercase /quick/all anchor answered 400 live, so it is not offered.

    Pinned as a contract rather than left implicit: the argument shipped in the
    first draft of this branch, and re-adding it would silently restore a call
    that cannot work on any level yet observed.
    """
    parameters = inspect.signature(HMCClient.list_quick_properties).parameters
    assert "all_properties" not in parameters
    assert list(parameters) == ["self", "resource_type", "parent_type", "parent_uuid"]


@pytest.mark.asyncio
async def test_list_quick_properties_returns_a_single_name_as_a_one_element_list(
    mock_hmc,
):
    """A type defining one quick property yields a one-element list.

    This is why the parse does not go through _parse_feed: element_to_dict
    collapses a repeated element to a bare value when the HMC sends exactly one,
    the hazard ADR 0139 recorded for OperationSet. Reading Nickname elements
    directly has no such arity dependence.

    VirtualNetwork is the real single-property case: the live run on PR #800
    probed for one and found this type defines exactly NetworkName, and
    confirmed the method returns ["NetworkName"] against it.
    """
    path = f"/rest/api/uom/ManagedSystem/{_PARENT_UUID}/VirtualNetwork/quick"
    mock_hmc.get(path).mock(
        return_value=httpx.Response(
            200,
            text=_quick_property_entry(
                "VirtualNetwork",
                ("NetworkName", "The name of the Virtual Network."),
            ),
        )
    )

    async with HMCClient(make_config()) as hmc:
        names, _ = await hmc.list_quick_properties(
            "VirtualNetwork", parent_type="ManagedSystem", parent_uuid=_PARENT_UUID
        )

    assert names == ["NetworkName"]


@pytest.mark.asyncio
async def test_list_quick_properties_returns_nicknames_not_descriptions(mock_hmc):
    """Nickname is the name; the Description sibling must not leak into it.

    Both elements hold prose-looking text, and LogicalPartition defines a
    property whose name is itself "Description", so a parse reading the wrong
    element still returns plausible strings. The pairs below are the live
    capture's verbatim text for that type's first two properties.
    """
    path = f"/rest/api/uom/ManagedSystem/{_PARENT_UUID}/LogicalPartition/quick"
    mock_hmc.get(path).mock(
        return_value=httpx.Response(
            200,
            text=_quick_property_entry(
                "LogicalPartition",
                (
                    "ProgressState",
                    "The progress state of the partition's hibernation operation.",
                ),
                ("Description", "The description of the partition."),
            ),
        )
    )

    async with HMCClient(make_config()) as hmc:
        names, _ = await hmc.list_quick_properties(
            "LogicalPartition", parent_type="ManagedSystem", parent_uuid=_PARENT_UUID
        )

    assert names == ["ProgressState", "Description"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        # Collection at the document root, with no Atom envelope.
        (
            '<QuickProperty_Collection xmlns="http://www.ibm.com/xmlns/systems'
            '/power/firmware/uom/mc/2012_10/">'
            "<QuickProperty><Nickname>State</Nickname></QuickProperty>"
            "</QuickProperty_Collection>"
        ),
        # Wrapped one level deeper than the feed the live run described.
        (
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>'
            "<Wrapper><QuickProperty_Collection>"
            "<QuickProperty><Nickname>State</Nickname></QuickProperty>"
            "</QuickProperty_Collection></Wrapper>"
            "</content></entry></feed>"
        ),
    ],
)
async def test_list_quick_properties_reads_names_at_any_depth(mock_hmc, body):
    """The parse does not depend on where the collection sits.

    The nesting is no longer an open question -- the capture on PR #800 settled
    it -- but the first draft of this branch guessed a <feed> root and the real
    one is <entry>, so the depth independence that absorbed that error is worth
    keeping. This fails if someone later tightens the parse to a fixed element
    path, which would make the next such surprise a bug instead of a non-event.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(200, text=body)
    )

    async with HMCClient(make_config()) as hmc:
        names, _ = await hmc.list_quick_properties("ManagedSystem")

    assert names == ["State"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-HMC-Schema-Version": "V1_0"}, "V1_0"),
        # FW950 echoes the request's X-Audit-Memento into this header, so the
        # value is returned verbatim rather than validated as a level (ADR 0139).
        ({"X-HMC-Schema-Version": "hmcpctl"}, "hmcpctl"),
        ({}, None),
    ],
)
async def test_list_quick_properties_returns_the_response_schema_version(
    mock_hmc, headers, expected
):
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(
            200,
            text=_quick_property_entry("ManagedSystem", "State"),
            headers=headers,
        )
    )

    async with HMCClient(make_config()) as hmc:
        _, schema_version = await hmc.list_quick_properties("ManagedSystem")

    assert schema_version == expected


@pytest.mark.asyncio
async def test_list_quick_properties_204_returns_an_unknown_answer(mock_hmc):
    """204 is an empty answer, not an error -- and not a fact about the type.

    A 204 carries no container, so nothing in it says the type defines no quick
    properties; it says only that the HMC sent no body. ADR 0144 returns None
    there, which the cache reads as "do not validate".
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(204, headers={"X-HMC-Schema-Version": "V1_0"})
    )

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_quick_properties("ManagedSystem") == (None, "V1_0")


@pytest.mark.asyncio
async def test_list_quick_properties_drops_an_empty_nickname(mock_hmc):
    """An empty Nickname is dropped rather than returned as an empty name."""
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>'
                "<QuickProperty_Collection>"
                "<QuickProperty><Nickname>State</Nickname></QuickProperty>"
                "<QuickProperty><Nickname></Nickname></QuickProperty>"
                "<QuickProperty><Nickname>SystemName</Nickname></QuickProperty>"
                "</QuickProperty_Collection>"
                "</content></entry></feed>"
            ),
        )
    )

    async with HMCClient(make_config()) as hmc:
        names, _ = await hmc.list_quick_properties("ManagedSystem")

    assert names == ["State", "SystemName"]


@pytest.mark.asyncio
async def test_list_quick_properties_200_without_the_container_raises(mock_hmc):
    """A 200 carrying no QuickProperty_Collection raises rather than returning [].

    The HMC is known to answer 200 with an HttpErrorResponse feed. Without the
    container element that body is indistinguishable from a type defining no
    quick properties, so returning [] would report an error as an answer.
    """
    body = (
        '<HttpErrorResponse xmlns="http://www.ibm.com/xmlns/systems/power'
        '/firmware/web/mc/2012_10/">'
        "<HTTPStatus>200</HTTPStatus><ReasonCode>INVALID_URL</ReasonCode>"
        "</HttpErrorResponse>"
    )
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(200, text=body)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            HMCError, match="returned no QuickProperty_Collection element"
        ) as raised:
            await hmc.list_quick_properties("ManagedSystem")

    assert raised.value.status_code == 200
    assert raised.value.body == body


@pytest.mark.asyncio
async def test_list_quick_properties_empty_set_returns_no_names(mock_hmc):
    """A type defining no quick properties is an answer, not an error.

    The body is a well-formed collection holding no property at all. The
    container is what separates it from the HttpErrorResponse feed above. The
    empty list is a fact about the type, not an unknown answer (ADR 0144): the
    two tests below hold the shapes that are not.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(
            200,
            text=_quick_property_entry("ManagedSystem"),
            headers={"X-HMC-Schema-Version": "V1_0"},
        )
    )

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_quick_properties("ManagedSystem") == ([], "V1_0")


@pytest.mark.asyncio
async def test_list_quick_properties_all_empty_names_return_an_unknown_answer(mock_hmc):
    """Elements present, every text empty: properties this parse cannot name.

    That is the parse-artefact shape rather than the captured one -- a container
    holding QuickProperty elements whose names this parse does not read -- so it
    is unknown, like a 204, and not the empty fact above (ADR 0144).

    Pins the `if n` filter, which nothing else observes. find_all_text strips,
    so both elements arrive as "": without the filter the comprehension yields
    ["", ""], a *non-empty* positive set holding only "", which rejects every
    real name for the client's lifetime. The whitespace element pins the
    stripping too: a second way to reach the same empty name.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(
            200,
            text=_quick_property_entry("ManagedSystem", ("", ""), ("   ", "")),
            headers={"X-HMC-Schema-Version": "V1_0"},
        )
    )

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_quick_properties("ManagedSystem") == (None, "V1_0")


@pytest.mark.asyncio
async def test_list_quick_properties_unnamed_items_return_an_unknown_answer(
    mock_hmc,
):
    """Items present, no Nickname element at all: properties, still unnamed.

    A level keeping the collection and spelling the name element differently is
    in the same logical condition as the test above -- the container holds
    properties this parse cannot name -- so it reads as unknown too, and the
    QuickProperty element is the evidence for it (ADR 0144). Reading it as the
    empty fact would cache an empty positive set and refuse every name for the
    client's lifetime.

    The body is inline rather than from _quick_property_entry: that helper
    always emits a Nickname inside each QuickProperty, which is the element
    this shape lacks, and widening it would reach every test using it.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<entry xmlns="http://www.w3.org/2005/Atom"><content>'
                '<QuickProperty_Collection xmlns="http://www.ibm.com/xmlns'
                '/systems/power/firmware/uom/mc/2012_10/">'
                "<Metadata><Atom/></Metadata>"
                "<QuickProperty>"
                "<RESTElement>ManagedSystem</RESTElement>"
                "<Description>A property this parse cannot name.</Description>"
                "</QuickProperty>"
                "</QuickProperty_Collection></content></entry>"
            ),
            headers={"X-HMC-Schema-Version": "V1_0"},
        )
    )

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_quick_properties("ManagedSystem") == (None, "V1_0")


@pytest.mark.asyncio
async def test_list_quick_properties_malformed_xml_raises_hmc_error(mock_hmc):
    """A truncated body raises HMCError naming the call, not a ParseError.

    The tagging wrapper in client_parse owns that conversion; this pins that
    this method goes through it rather than calling the raw parser.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(200, text="<feed><entry>")
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError, match="GET /rest/api/uom/ManagedSystem/quick"):
            await hmc.list_quick_properties("ManagedSystem")


@pytest.mark.asyncio
async def test_list_quick_properties_unknown_type_raises_hmc_error_with_status(
    mock_hmc,
):
    """An unknown type is rejected at the URL, with 400, not 404.

    The status and body shape are modelled on the live ``/operations`` capture
    taken against V1_17_0 on PR #797, with the path changed to ``/quick``; it
    is not itself a ``/quick`` capture. The status is asserted because this
    method's only contract for a non-200 is to surface what the HMC returned,
    so a mock inventing 404 would let a regression that swallowed the real
    status still pass.
    """
    mock_hmc.get("/rest/api/uom/NoSuchType/quick").mock(
        return_value=httpx.Response(
            400,
            text=(
                '<HttpErrorResponse xmlns="http://www.ibm.com/xmlns/systems/power'
                '/firmware/web/mc/2012_10/">'
                "<HTTPStatus>400</HTTPStatus>"
                "<RequestURI>/rest/api/uom/NoSuchType/quick</RequestURI>"
                "<ReasonCode>INVALID_URL</ReasonCode>"
                "<Message>REST000B The URL presented to the Management Console REST "
                "Web Services is not valid.REST000E Unrecognized root REST type of "
                "NoSuchType.</Message>"
                "</HttpErrorResponse>"
            ),
        )
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as raised:
            await hmc.list_quick_properties("NoSuchType")

    assert raised.value.status_code == 400
    assert "Unrecognized root REST type of NoSuchType" in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "kwargs", "error", "match"),
    [
        (
            ("LogicalPartition",),
            {"parent_type": "ManagedSystem", "parent_uuid": "not-a-uuid"},
            ValueError,
            "parent_uuid must be a UUID",
        ),
        (
            ("LogicalPartition",),
            {"parent_type": "ManagedSystem"},
            ValueError,
            "must be given together",
        ),
        (
            ("LogicalPartition",),
            {"parent_uuid": _PARENT_UUID},
            ValueError,
            "must be given together",
        ),
        # Root anchor: resource_type is the only interpolated segment. A ".."
        # type fails the resource-type grammar, so this is refused at the
        # boundary with ValueError rather than at the waist (ADR 0143).
        (("../web/Logon",), {}, ValueError, "must be an HMC resource type name"),
        # Child anchor: parent_type is interpolated too, and is refused by the
        # same predicate. Each anchor interpolates a different argument.
        (
            ("LogicalPartition",),
            {"parent_type": "../../web", "parent_uuid": _PARENT_UUID},
            ValueError,
            "must be an HMC resource type name",
        ),
    ],
)
async def test_list_quick_properties_refuses_bad_arguments(
    mock_hmc, args, kwargs, error, match
):
    """Refused before transport: no request for a quick anchor is recorded.

    The router pre-mocks the logon and logoff the client context manager
    performs, so the assertion is scoped to the paths this method builds
    rather than to the router being untouched.
    """
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(error, match=match):
            await hmc.list_quick_properties(*args, **kwargs)

    assert not [call for call in mock_hmc.calls if "quick" in call.request.url.path]


# get_quick_property validation (#799) -- ADR 0141: opt-in, cached per client.

_VALIDATION_UUID = "3f2b1c9d-4e5a-4b6c-8d7e-9f0a1b2c3d4e"
_VALIDATION_TYPE = "ManagedSystem"
_VALIDATION_DISCOVERY = f"/rest/api/uom/{_VALIDATION_TYPE}/quick"


def _mock_validation_routes(router, *, discovery=200, value="running"):
    """Mock the discovery anchor plus a defined and an undefined value read.

    *discovery* is a status code, an exception to raise as a transport failure,
    or one of two named empty 200 bodies. 200 answers with the captured names;
    204 answers empty; ``"empty-set"`` answers with the container and no
    Nickname element, the shape of a type defining none; ``"empty-elements"``
    answers with Nickname elements whose texts are all empty, which is the
    parse-artefact shape rather than a captured one.
    """
    if isinstance(discovery, Exception):
        route_kwargs = {"side_effect": discovery}
    elif discovery == "empty-set":
        route_kwargs = {
            "return_value": httpx.Response(
                200, text=_quick_property_entry(_VALIDATION_TYPE)
            )
        }
    elif discovery == "empty-elements":
        route_kwargs = {
            "return_value": httpx.Response(
                200,
                text=_quick_property_entry(_VALIDATION_TYPE, ("", ""), ("   ", "")),
            )
        }
    elif discovery == 200:
        body = _quick_property_entry(_VALIDATION_TYPE, *_MANAGED_SYSTEM_NICKNAMES)
        route_kwargs = {"return_value": httpx.Response(200, text=body)}
    elif discovery == 204:
        route_kwargs = {"return_value": httpx.Response(204)}
    else:
        route_kwargs = {"return_value": httpx.Response(discovery, text="<error/>")}
    discovery_route = router.get(_VALIDATION_DISCOVERY).mock(**route_kwargs)
    instance = f"/rest/api/uom/{_VALIDATION_TYPE}/{_VALIDATION_UUID}/quick"
    defined = router.get(f"{instance}/SystemType").mock(
        return_value=httpx.Response(200, text=value)
    )
    undefined = router.get(f"{instance}/NoSuchProperty").mock(
        return_value=httpx.Response(200, text="unreachable-when-validating")
    )
    return discovery_route, defined, undefined


@pytest.mark.asyncio
async def test_get_quick_property_validate_refuses_an_undefined_name(mock_hmc):
    """An undefined name raises before the value request is built.

    The refusal must cost no round trip, so the value route records no call.
    """
    discovery, _, undefined = _mock_validation_routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError) as excinfo:
            await hmc.get_quick_property(
                _VALIDATION_TYPE, _VALIDATION_UUID, "NoSuchProperty", validate=True
            )

    assert not undefined.called
    assert discovery.call_count == 1
    message = str(excinfo.value)
    assert "NoSuchProperty" in message
    assert _VALIDATION_TYPE in message
    # The defined names are the actionable half: a caller who misspelled one
    # needs to see the spelling that would have worked.
    assert "SystemType" in message


@pytest.mark.asyncio
async def test_get_quick_property_validate_refuses_a_type_defining_nothing(mock_hmc):
    """A type defining no quick properties refuses every name, locally.

    The container-present empty answer is a fact about the type (ADR 0144), so
    it is cached as an empty positive set and SystemType -- a name this type
    really does define at the captured levels -- is refused here too. The value
    read is mocked and asserted unused: the refusal costs no round trip.

    The message says the type defines none rather than rendering an empty set,
    which _summarize_names would print as a bare ".".
    """
    _, defined, _ = _mock_validation_routes(mock_hmc, discovery="empty-set")

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError) as exc_info:
            await hmc.get_quick_property(
                _VALIDATION_TYPE, _VALIDATION_UUID, "SystemType", validate=True
            )

    assert str(exc_info.value).endswith("The type defines none at all.")
    assert ": ." not in str(exc_info.value)
    assert defined.call_count == 0


@pytest.mark.asyncio
async def test_get_quick_property_validate_caps_the_names_it_enumerates(mock_hmc):
    """The refusal message is a diagnostic, not an amplifier.

    Twin of ``test_search_uom_validate_caps_the_names_it_enumerates``: the set
    rendered here is whatever the discovery read returned, bounded through the
    same ``_summarize_names`` helper (#808) rather than a raw join.
    """
    many = [f"Prop{i:04d}" for i in range(500)]
    mock_hmc.get(_VALIDATION_DISCOVERY).mock(
        return_value=httpx.Response(
            200, text=_quick_property_entry(_VALIDATION_TYPE, *many)
        )
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError) as exc_info:
            await hmc.get_quick_property(
                _VALIDATION_TYPE, _VALIDATION_UUID, "NoSuchProperty", validate=True
            )

    message = str(exc_info.value)
    assert "Prop0000" in message
    assert "Prop0019" in message
    assert "Prop0020" not in message
    assert "and 480 more." in message
    # A single trailing period, not the doubled "..": _summarize_names already
    # appends its own, so the message must not append a second literal ".".
    assert not message.endswith("..")
    assert len(message) < 500


@pytest.mark.asyncio
async def test_get_quick_property_validate_allows_a_defined_name(mock_hmc):
    """A name the type defines is read exactly as it is without validation."""
    discovery, defined, _ = _mock_validation_routes(mock_hmc, value="Fixed")

    async with HMCClient(make_config()) as hmc:
        value = await hmc.get_quick_property(
            _VALIDATION_TYPE, _VALIDATION_UUID, "SystemType", validate=True
        )

    assert value == "Fixed"
    assert defined.call_count == 1
    assert discovery.call_count == 1


@pytest.mark.asyncio
async def test_get_quick_property_validate_reads_the_names_once_per_type(mock_hmc):
    """The cost bound ADR 0141 states: one discovery read per type per client."""
    discovery, defined, _ = _mock_validation_routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        for _ in range(2):
            await hmc.get_quick_property(
                _VALIDATION_TYPE, _VALIDATION_UUID, "SystemType", validate=True
            )

    assert defined.call_count == 2
    assert discovery.call_count == 1


@pytest.mark.asyncio
async def test_get_quick_property_validate_rereads_for_a_new_client(mock_hmc):
    """Cache lifetime is the client's: a fresh HMCClient reads the names again."""
    discovery, _, _ = _mock_validation_routes(mock_hmc)

    for _ in range(2):
        async with HMCClient(make_config()) as hmc:
            await hmc.get_quick_property(
                _VALIDATION_TYPE, _VALIDATION_UUID, "SystemType", validate=True
            )

    assert discovery.call_count == 2


@pytest.mark.parametrize(
    "discovery",
    [500, 400, httpx.ConnectError("refused"), 204, "empty-elements"],
    ids=[
        "server-error",
        "unknown-type",
        "transport-failure",
        "empty-204",
        "empty-elements",
    ],
)
@pytest.mark.asyncio
async def test_get_quick_property_validate_degrades_and_caches_the_failure(
    mock_hmc, discovery
):
    """A discovery read yielding no names leaves get_quick_property working.

    #799's fourth criterion, over the five ways the read can yield nothing.
    ADR 0139 records three V1_20_0 HMCs answering 500 at the sibling
    /operations anchor and ADR 0140 a 400 for NetworkBridge at the root
    anchor; a 204 and a container whose Nickname elements are all empty both
    return (None, version) rather than raising. The all-empty body is the one
    that would fail *closed* rather than open if the parse read it as
    authoritative, since an empty positive set rejects every name. The second
    call pins that the answer is cached: retrying per call is the extra request
    ADR 0141 promises not to make.
    """
    discovery_route, _, undefined = _mock_validation_routes(
        mock_hmc, discovery=discovery
    )

    async with HMCClient(make_config()) as hmc:
        for _ in range(2):
            value = await hmc.get_quick_property(
                _VALIDATION_TYPE, _VALIDATION_UUID, "NoSuchProperty", validate=True
            )

    assert value == "unreachable-when-validating"
    assert discovery_route.call_count == 1
    assert undefined.call_count == 2


@pytest.mark.asyncio
async def test_get_quick_property_defaults_to_no_validation(mock_hmc):
    """The default is unchanged behaviour: no discovery read, name sent as given."""
    discovery, _, undefined = _mock_validation_routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        value = await hmc.get_quick_property(
            _VALIDATION_TYPE, _VALIDATION_UUID, "NoSuchProperty"
        )

    assert value == "unreachable-when-validating"
    assert undefined.call_count == 1
    assert discovery.call_count == 0


def test_get_quick_property_validate_is_keyword_only_and_defaults_false():
    """Positional or default-True is the facade movement ADR 0141 declined."""
    parameter = inspect.signature(HMCClient.get_quick_property).parameters["validate"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False


@pytest.mark.asyncio
async def test_get_quick_property_validate_reads_the_names_once_under_concurrency(
    mock_hmc,
):
    """Concurrent validated calls share one discovery read, not one each.

    ADR 0141 and #799's second criterion state the bound without a sequential
    qualifier -- at most one extra request per resource type per client
    session. Without a lock every task in a gather misses the cache before the
    first read returns, so the bound holds only for sequential callers.
    """
    discovery, defined, _ = _mock_validation_routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        await asyncio.gather(
            *(
                hmc.get_quick_property(
                    _VALIDATION_TYPE, _VALIDATION_UUID, "SystemType", validate=True
                )
                for _ in range(5)
            )
        )

    assert defined.call_count == 5
    assert discovery.call_count == 1


@pytest.mark.asyncio
async def test_get_quick_property_validate_caches_per_resource_type(mock_hmc):
    """The cache is keyed by resource type, not a single slot.

    Without this, a one-entry cache passes every other test here: they all use
    one type, so a second type would silently reuse the first type's names and
    reject its own.
    """
    system_route = mock_hmc.get("/rest/api/uom/ManagedSystem/quick").mock(
        return_value=httpx.Response(
            200, text=_quick_property_entry("ManagedSystem", *_MANAGED_SYSTEM_NICKNAMES)
        )
    )
    lpar_route = mock_hmc.get("/rest/api/uom/LogicalPartition/quick").mock(
        return_value=httpx.Response(
            200,
            text=_quick_property_entry(
                "LogicalPartition", *_LOGICAL_PARTITION_NICKNAMES
            ),
        )
    )
    mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{_VALIDATION_UUID}/quick/SystemType"
    ).mock(return_value=httpx.Response(200, text="system-value"))
    mock_hmc.get(
        f"/rest/api/uom/LogicalPartition/{_VALIDATION_UUID}/quick/PartitionID"
    ).mock(return_value=httpx.Response(200, text="7"))

    async with HMCClient(make_config()) as hmc:
        system = await hmc.get_quick_property(
            "ManagedSystem", _VALIDATION_UUID, "SystemType", validate=True
        )
        # PartitionID is defined by LogicalPartition and not by ManagedSystem,
        # so a single-slot cache rejects it here.
        lpar = await hmc.get_quick_property(
            "LogicalPartition", _VALIDATION_UUID, "PartitionID", validate=True
        )

    assert system == "system-value"
    assert lpar == "7"
    assert system_route.call_count == 1
    assert lpar_route.call_count == 1


# list_search_parameters (#789) -- the /search discovery anchors.
#
# THE SHAPE BELOW IS RECONSTRUCTED FROM A LIVE CAPTURE, not invented and not a
# verbatim body. Two rounds ran against Power HMCs at V1_17_0 and V1_20_0 and
# are recorded on PR #807. Round 1 reported the element tree, per-path counts,
# namespaces and text lengths but no text values, because the raw bodies carry
# instance data; round 2 added the element texts that are schema strings. These
# facts are live, and nothing outside this list is:
#
#   * the root element <entry> and both namespace URIs;
#   * the nesting content > SearchParameterSet > SearchParameters >
#     SearchParameter > ParameterName;
#   * the ElementName, Comparator and XPath siblings and where they sit;
#   * the parameter names, which are schema property names;
#   * the Comparator text -- one string, on every parameter of every type
#     captured -- and the XPath form, a schema path ending in /Value;
#   * six of the eleven types captured answering 200 with a SearchParameterSet
#     and no SearchParameters child at all.
#
# Constructed here, among what the blanket above excludes: the Atom scaffolding the helper
# emits (<id>, <title>, <author>, <Metadata>), which no capture round reported
# because the parse does not read it; and the XPath texts in
# test_list_search_parameters_reads_the_named_element_not_its_siblings, which
# are invented on purpose -- they end in a name-like segment rather than
# /Value, precisely so a parse reading XPath instead of ParameterName returns a
# plausible wrong set and fails.
#
# This replaces an earlier inference that read <Nickname> from a
# <SearchParameter_Collection>, mirroring the /quick anchor. The capture found
# both halves wrong. ADR 0142 records what that cost and what the ground was.
#
# There is no child-anchored fixture because no captured level serves a child
# anchor. V1_17_0 and V1_20_0 both answer /rest/api/uom/{P}/{UUID}/{C}/search
# with 400 INVALID_URL; at V1_17_0 that holds for both LogicalPartition and
# VirtualIOServer, under a parent whose plain child feed and /quick anchor both
# answered 200 in the same session.
_CAPTURED_COMPARATOR = "Regular Expression or String Match"

# Names as captured. ManagedSystem and LogicalPartition are the two the tests
# drive; VirtualIOServer returned the same four as LogicalPartition, and
# SharedStoragePool two.
_MANAGED_SYSTEM_SEARCH_PARAMETERS = [
    "MachineType",
    "Model",
    "SerialNumber",
    "State",
    "SystemName",
]
_LOGICAL_PARTITION_SEARCH_PARAMETERS = [
    "PartitionID",
    "PartitionName",
    "PartitionState",
    "PartitionType",
]
# Captured: types answering the anchor with an empty set. Not an edge case --
# six of the eleven types captured do this, so the container branch below is
# the common path, not a corner. VirtualSwitch, VirtualNetwork, NetworkBridge,
# LogicalUnit and SharedProcessorPool answer the same way.
_EMPTY_SET_TYPE = "ManagementConsole"
# Captured: the single-parameter type. ADR 0139's element_to_dict collapse is
# reachable through it, which is why the parse reads _find_all_text.
_SINGLE_PARAMETER_TYPE = "Cluster"
_SINGLE_PARAMETER_NAMES = ["ClusterName"]


# What the HMC is known to answer with a 200 instead of an error status: a feed
# carrying an HttpErrorResponse and no names at all. The capture's own error
# bodies came back at 400 and 500 rather than 200, so this remains the ADR 0139
# hazard rather than a captured one -- but it is what makes the container test
# necessary, because a type defining nothing is now known to be real.
_HTTP_ERROR_RESPONSE_FEED = (
    '<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>'
    "<HttpErrorResponse><Message>Internal error</Message>"
    "</HttpErrorResponse></content></entry></feed>"
)


def _search_parameter_entry(
    element_name: str, *parameters: str | tuple[str, str]
) -> str:
    """An <entry> in the shape the /search anchor was captured answering.

    Each entry in *parameters* is a name, or a (name, xpath) pair when the test
    cares about the XPath sibling. Passing no parameter produces the empty set
    ManagementConsole was captured returning: a SearchParameterSet with no
    SearchParameters child, which is a type defining none rather than an error.
    """
    body = ""
    for item in parameters:
        name, xpath = (
            item if isinstance(item, tuple) else (item, f"{element_name}/{item}/Value")
        )
        body += (
            "<SearchParameter>"
            "<Metadata><Atom/></Metadata>"
            f"<ParameterName>{name}</ParameterName>"
            f"<Comparator>{_CAPTURED_COMPARATOR}</Comparator>"
            f"<XPath>{xpath}</XPath>"
            "</SearchParameter>"
        )
    if body:
        body = (
            f"<SearchParameters><Metadata><Atom/></Metadata>{body}</SearchParameters>"
        )
    return (
        '<entry xmlns="http://www.w3.org/2005/Atom">'
        "<id>00000000-0000-0000-0000-000000000000</id>"
        "<title>SearchParameterSet</title>"
        "<author><name>IBM Power Systems Management Console</name></author>"
        "<content>"
        '<SearchParameterSet xmlns="http://www.ibm.com/xmlns/systems/power'
        '/firmware/web/mc/2012_10/">'
        "<Metadata><Atom/></Metadata>"
        f"<ElementName>{element_name}</ElementName>"
        f"{body}"
        "</SearchParameterSet>"
        "</content></entry>"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_type", "expected"),
    [
        ("ManagedSystem", _MANAGED_SYSTEM_SEARCH_PARAMETERS),
        ("LogicalPartition", _LOGICAL_PARTITION_SEARCH_PARAMETERS),
    ],
)
async def test_list_search_parameters_reads_the_root_anchor(
    mock_hmc, resource_type, expected
):
    """The root anchor, at the path the corpus documents and the capture served.

    There is no child-anchored row because no captured level serves one: see the
    block comment above and ADR 0142. Both of these types were captured
    answering the root anchor, with exactly these names.
    """
    path = f"/rest/api/uom/{resource_type}/search"
    route = mock_hmc.get(path).mock(
        return_value=httpx.Response(
            200, text=_search_parameter_entry(resource_type, *expected)
        )
    )

    async with HMCClient(make_config()) as hmc:
        names, _ = await hmc.list_search_parameters(resource_type)

    assert route.calls.last.request.url.path == path
    assert names == expected
    # Pinned to the exact value, not merely "not a typed uom Accept". The
    # captured content type is application/atom+xml, but only two Accept values
    # were ever probed, so */* remains the one that cannot fail negotiation on
    # an unmeasured level -- and an assertion that only excludes one wrong
    # family would pass for every other wrong value.
    assert route.calls.last.request.headers["Accept"] == "*/*"


@pytest.mark.asyncio
async def test_list_search_parameters_refuses_a_dot_segment_resource_type(mock_hmc):
    """resource_type is the only interpolated segment, and it is guarded.

    Refused before transport: no request for a search anchor is recorded. The
    router pre-mocks the logon and logoff the client context manager performs,
    so the assertion is scoped to the paths this method builds rather than to
    the router being untouched.

    ``ValueError``, not ``HMCError``: a ".." type fails the resource-type
    grammar, so the boundary check fires before the waist guard (ADR 0143).
    """
    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="must be an HMC resource type name"):
            await hmc.list_search_parameters("../web/Logon")

    assert not [call for call in mock_hmc.calls if "search" in call.request.url.path]


@pytest.mark.asyncio
async def test_list_search_parameters_reads_the_named_element_not_its_siblings(
    mock_hmc,
):
    """The names come from ParameterName, not from a plausible neighbour.

    Every SearchParameter carries Comparator and XPath, and the set carries
    ElementName; XPath ends in a string that would pass for a parameter name,
    and ElementName holds the type. A parse reading any of them returns a
    plausible wrong set rather than failing, which is the defect class
    docs/solutions/2026-09-14-fixtures-invented-for-an-endpoint-never-spoken.md
    records -- and is what the capture caught the shipped <Nickname> parse
    doing in reverse. The fixture carries the real siblings so it can be caught
    here instead.
    """
    body = _search_parameter_entry(
        "LogicalPartition",
        ("PartitionName", "LogicalPartition/PartitionLabel"),
        ("PartitionID", "LogicalPartition/PartitionIndex"),
        ("PartitionState", "LogicalPartition/PartitionStatus"),
    )
    mock_hmc.get("/rest/api/uom/LogicalPartition/search").mock(
        return_value=httpx.Response(200, text=body)
    )

    async with HMCClient(make_config()) as hmc:
        names, _ = await hmc.list_search_parameters("LogicalPartition")

    assert names == ["PartitionName", "PartitionID", "PartitionState"]
    assert not [n for n in names if "/" in n], "an XPath text reached the names"
    # ElementName holds the type once per response, so a parse reading it
    # returns the type instead of its properties.
    assert "LogicalPartition" not in names


@pytest.mark.asyncio
async def test_list_search_parameters_returns_a_single_name_as_a_one_element_list(
    mock_hmc,
):
    """One defined parameter is a list of one, not a bare string.

    element_to_dict keys children by tag and promotes to a list only on the
    second sibling, so a _parse_feed-based parse collapses the single case
    (ADR 0139). Reading element texts directly is what avoids it. The type and
    name here are the captured ones: Cluster really does define exactly one, so
    this is a reachable body rather than a constructed edge case.
    """
    mock_hmc.get(f"/rest/api/uom/{_SINGLE_PARAMETER_TYPE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_parameter_entry(
                _SINGLE_PARAMETER_TYPE, *_SINGLE_PARAMETER_NAMES
            ),
        )
    )

    async with HMCClient(make_config()) as hmc:
        names, _ = await hmc.list_search_parameters(_SINGLE_PARAMETER_TYPE)

    assert names == _SINGLE_PARAMETER_NAMES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-HMC-Schema-Version": "V1_0"}, "V1_0"),
        ({}, None),
    ],
)
async def test_list_search_parameters_returns_the_response_schema_version(
    mock_hmc, headers, expected
):
    """The header is returned verbatim, and its absence is None.

    Verbatim because it is not guaranteed to hold a version: ADR 0139 and
    ADR 0140 both record the HMC echoing X-Audit-Memento into it.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/search").mock(
        return_value=httpx.Response(
            200,
            headers=headers,
            text=_search_parameter_entry(
                "ManagedSystem", *_MANAGED_SYSTEM_SEARCH_PARAMETERS
            ),
        )
    )

    async with HMCClient(make_config()) as hmc:
        _, schema_version = await hmc.list_search_parameters("ManagedSystem")

    assert schema_version == expected


@pytest.mark.asyncio
async def test_list_search_parameters_204_returns_an_unknown_answer(mock_hmc):
    """204 is an empty answer, not an error -- and not a fact about the type.

    A 204 carries no container, so nothing in it says the type defines no search
    parameters; it says only that the HMC sent no body. ADR 0144 returns None
    there, which the cache reads as "do not validate".
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/search").mock(
        return_value=httpx.Response(204, headers={"X-HMC-Schema-Version": "V1_0"})
    )

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_search_parameters("ManagedSystem") == (None, "V1_0")


@pytest.mark.asyncio
async def test_list_search_parameters_200_without_the_container_raises(mock_hmc):
    """A 200 carrying no SearchParameterSet raises rather than returning [].

    The HMC is known to answer 200 with an HttpErrorResponse feed. Without the
    container element that body is indistinguishable from a type defining no
    search parameters, so returning [] would report an error as an answer.
    """
    mock_hmc.get("/rest/api/uom/ManagedSystem/search").mock(
        return_value=httpx.Response(200, text=_HTTP_ERROR_RESPONSE_FEED)
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(
            HMCError, match="returned no SearchParameterSet element"
        ) as exc_info:
            await hmc.list_search_parameters("ManagedSystem")

    assert exc_info.value.status_code == 200


@pytest.mark.asyncio
async def test_list_search_parameters_empty_set_returns_no_names(mock_hmc):
    """A type defining no search parameters is an answer, not an error.

    This is the captured ManagementConsole answer: 200 with a
    SearchParameterSet carrying no SearchParameters child at all. The container
    is what separates it from the HttpErrorResponse feed above. The empty list
    is a fact about the type, not an unknown answer (ADR 0144): the two tests
    below hold the shapes that are not.
    """
    mock_hmc.get(f"/rest/api/uom/{_EMPTY_SET_TYPE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_parameter_entry(_EMPTY_SET_TYPE),
            headers={"X-HMC-Schema-Version": "V1_0"},
        )
    )

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_search_parameters(_EMPTY_SET_TYPE) == ([], "V1_0")


@pytest.mark.asyncio
async def test_list_search_parameters_all_empty_names_return_an_unknown_answer(
    mock_hmc,
):
    """Elements present, every text empty: parameters this parse cannot name.

    That is the parse-artefact shape rather than the captured one -- a container
    holding SearchParameters whose names this parse does not read -- so it is
    unknown, like a 204, and not the empty fact above (ADR 0144).

    Pins the `if n` filter, which nothing else observes. find_all_text strips,
    so both elements arrive as "": without the filter the comprehension yields
    ["", ""], a *non-empty* positive set holding only "", which rejects every
    real name for the client's lifetime. The whitespace element pins the
    stripping too: a second way to reach the same empty name.
    """
    mock_hmc.get(f"/rest/api/uom/{_EMPTY_SET_TYPE}/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_parameter_entry(_EMPTY_SET_TYPE, "", "   "),
            headers={"X-HMC-Schema-Version": "V1_0"},
        )
    )

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_search_parameters(_EMPTY_SET_TYPE) == (None, "V1_0")


@pytest.mark.asyncio
async def test_list_search_parameters_unnamed_items_return_an_unknown_answer(
    mock_hmc,
):
    """Items present, no ParameterName element: parameters, still unnamed.

    A level keeping the set and spelling the name element differently is in the
    same logical condition as the test above -- the container holds parameters
    this parse cannot name -- so it reads as unknown too, and the
    SearchParameter element is the evidence for it (ADR 0144). Reading it as
    the empty fact would cache an empty positive set and refuse every name for
    the client's lifetime.

    The body is inline rather than from _search_parameter_entry: that helper
    always emits a ParameterName inside each SearchParameter, which is the
    element this shape lacks, and widening it would reach every test using it.
    """
    mock_hmc.get(f"/rest/api/uom/{_EMPTY_SET_TYPE}/search").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<entry xmlns="http://www.w3.org/2005/Atom"><content>'
                '<SearchParameterSet xmlns="http://www.ibm.com/xmlns/systems'
                '/power/firmware/web/mc/2012_10/">'
                "<Metadata><Atom/></Metadata>"
                f"<ElementName>{_EMPTY_SET_TYPE}</ElementName>"
                "<SearchParameters>"
                "<SearchParameter>"
                f"<Comparator>{_CAPTURED_COMPARATOR}</Comparator>"
                f"<XPath>{_EMPTY_SET_TYPE}/Unnamed/Value</XPath>"
                "</SearchParameter>"
                "</SearchParameters>"
                "</SearchParameterSet></content></entry>"
            ),
            headers={"X-HMC-Schema-Version": "V1_0"},
        )
    )

    async with HMCClient(make_config()) as hmc:
        assert await hmc.list_search_parameters(_EMPTY_SET_TYPE) == (None, "V1_0")


@pytest.mark.asyncio
async def test_list_search_parameters_unknown_type_raises_hmc_error_with_status(
    mock_hmc,
):
    """An unrecognised type surfaces HMCError carrying the HMC's status.

    400 INVALID_URL rather than 404: ADR 0139 records the firmware validating
    the type name at the URL before reaching a handler.
    """
    mock_hmc.get("/rest/api/uom/NoSuchType/search").mock(
        return_value=httpx.Response(
            400,
            text="<HttpErrorResponse><Message>REST000E: Unknown resource type"
            "</Message></HttpErrorResponse>",
        )
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(HMCError) as exc_info:
            await hmc.list_search_parameters("NoSuchType")

    assert exc_info.value.status_code == 400
    assert "REST000E" in (exc_info.value.body or "")


# search_uom validation (#789) -- ADR 0142: opt-in, cached per client.
#
# The discovery bodies here come from the same _search_parameter_entry helper
# as the block above, so they carry the same reconstructed-from-capture
# provenance; read that block's head for which facts are live and which are
# constructed. What these tests prove is the transport, the cache, the
# degradation rule and the opt-in default, none of which depends on the parse
# being right. They do not prove the parse.
_SEARCH_VALIDATION_TYPE = "LogicalPartition"
_SEARCH_DISCOVERY = f"/rest/api/uom/{_SEARCH_VALIDATION_TYPE}/search"
_SEARCH_DEFINED = f"{_SEARCH_DISCOVERY}/(PartitionName==web)"
_SEARCH_UNDEFINED = f"{_SEARCH_DISCOVERY}/(NoSuchProperty==web)"
_SEARCH_RESULT_FEED = (
    '<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>'
    "<LogicalPartition><PartitionName>web</PartitionName></LogicalPartition>"
    "</content></entry></feed>"
)


def _mock_search_validation_routes(router, *, discovery=200):
    """Mock the discovery anchor plus a defined and an undefined instance search.

    *discovery* is a status code, an exception to raise as a transport failure,
    or one of two named empty 200 bodies. 200 answers with the captured names;
    204 answers empty; ``"empty-set"`` answers with the container and no
    ParameterName element, the captured shape of a type defining none;
    ``"empty-elements"`` answers with ParameterName elements whose texts are all
    empty, which is the parse-artefact shape rather than a captured one.
    """
    if isinstance(discovery, Exception):
        route_kwargs = {"side_effect": discovery}
    elif discovery == "empty-set":
        route_kwargs = {
            "return_value": httpx.Response(
                200, text=_search_parameter_entry(_SEARCH_VALIDATION_TYPE)
            )
        }
    elif discovery == "empty-elements":
        route_kwargs = {
            "return_value": httpx.Response(
                200, text=_search_parameter_entry(_SEARCH_VALIDATION_TYPE, "", "   ")
            )
        }
    elif discovery == 200:
        body = _search_parameter_entry(
            _SEARCH_VALIDATION_TYPE, *_LOGICAL_PARTITION_SEARCH_PARAMETERS
        )
        route_kwargs = {"return_value": httpx.Response(200, text=body)}
    elif discovery == 204:
        route_kwargs = {"return_value": httpx.Response(204)}
    else:
        route_kwargs = {"return_value": httpx.Response(discovery, text="<error/>")}
    discovery_route = router.get(_SEARCH_DISCOVERY).mock(**route_kwargs)
    defined = router.get(_SEARCH_DEFINED).mock(
        return_value=httpx.Response(200, text=_SEARCH_RESULT_FEED)
    )
    undefined = router.get(_SEARCH_UNDEFINED).mock(
        return_value=httpx.Response(200, text=_SEARCH_RESULT_FEED)
    )
    return discovery_route, defined, undefined


@pytest.mark.asyncio
async def test_search_uom_validate_refuses_an_undefined_property(mock_hmc):
    """An undefined property raises before the instance search is built.

    The undefined route is mocked and asserted unused: the contract is that
    nothing reaches the transport, not merely that the call fails.
    """
    _, _, undefined = _mock_search_validation_routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError, match="defines no search parameter named"):
            await hmc.search_uom(
                _SEARCH_VALIDATION_TYPE, "NoSuchProperty", "web", validate=True
            )

    assert undefined.call_count == 0


@pytest.mark.asyncio
async def test_search_uom_validate_refuses_a_type_defining_nothing(mock_hmc):
    """A type defining no search parameters refuses every name, locally.

    The container-present empty answer is a fact about the type (ADR 0144), so
    it is cached as an empty positive set and PartitionName -- a name this type
    really does define at the captured levels -- is refused here too. The
    instance search is mocked and asserted unused: the round trip this
    pre-flight exists to save is captured as an HTTP 500.

    The message says the type defines none rather than rendering an empty set,
    which _summarize_names would print as a bare ".".
    """
    _, defined, _ = _mock_search_validation_routes(mock_hmc, discovery="empty-set")

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError) as exc_info:
            await hmc.search_uom(
                _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
            )

    assert str(exc_info.value).endswith("The type defines none at all.")
    assert ": ." not in str(exc_info.value)
    assert defined.call_count == 0


@pytest.mark.asyncio
async def test_search_uom_validate_allows_a_defined_property(mock_hmc):
    """A defined property passes the check and the search runs normally."""
    _, defined, _ = _mock_search_validation_routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        results = await hmc.search_uom(
            _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
        )

    assert defined.call_count == 1
    assert results[0]["Resource"]["PartitionName"] == "web"


@pytest.mark.asyncio
async def test_search_uom_validate_reads_the_names_once_per_type(mock_hmc):
    """Three validated calls, one discovery request."""
    discovery, _, _ = _mock_search_validation_routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        for _ in range(3):
            await hmc.search_uom(
                _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
            )

    assert discovery.call_count == 1


@pytest.mark.asyncio
async def test_search_uom_validate_reads_the_names_once_under_concurrency(mock_hmc):
    """The bound holds for concurrent callers, not only sequential ones.

    Asserted on call_count after gather, never on timing: without the lock's
    re-check the count is five, and that is a deterministic observation.
    """
    discovery, _, _ = _mock_search_validation_routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        await asyncio.gather(
            *(
                hmc.search_uom(
                    _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
                )
                for _ in range(5)
            )
        )

    assert discovery.call_count == 1


@pytest.mark.asyncio
async def test_search_uom_validate_caches_per_resource_type(mock_hmc):
    """The cache is keyed by resource type, not a single slot.

    Without this, a one-entry cache passes every other test here: they all use
    one type, so a second type would reuse the first type's names and reject
    its own. SystemName is defined by ManagedSystem and not by
    LogicalPartition, so a single-slot cache rejects it.
    """
    lpar_discovery, _, _ = _mock_search_validation_routes(mock_hmc)
    system_discovery = mock_hmc.get("/rest/api/uom/ManagedSystem/search").mock(
        return_value=httpx.Response(
            200,
            text=_search_parameter_entry(
                "ManagedSystem", *_MANAGED_SYSTEM_SEARCH_PARAMETERS
            ),
        )
    )
    mock_hmc.get("/rest/api/uom/ManagedSystem/search/(SystemName==prod)").mock(
        return_value=httpx.Response(204)
    )

    async with HMCClient(make_config()) as hmc:
        await hmc.search_uom(
            _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
        )
        await hmc.search_uom("ManagedSystem", "SystemName", "prod", validate=True)

    assert lpar_discovery.call_count == 1
    assert system_discovery.call_count == 1


@pytest.mark.asyncio
async def test_search_uom_validate_rereads_for_a_new_client(mock_hmc):
    """The cache is per client; nothing is shared between two of them."""
    discovery, _, _ = _mock_search_validation_routes(mock_hmc)

    for _ in range(2):
        async with HMCClient(make_config()) as hmc:
            await hmc.search_uom(
                _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
            )

    assert discovery.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "discovery",
    [500, 400, httpx.ConnectError("connection refused"), 204, "empty-elements"],
    ids=["500", "400", "transport", "204", "empty-elements"],
)
async def test_search_uom_validate_degrades_and_caches_the_failure(mock_hmc, discovery):
    """A read yielding no names validates nothing, and is cached, not retried.

    All five ways it can happen: a 5xx and a 4xx raise HMCError, a connection
    failure raises HMCTransportError which subclasses it, and a 204 and a
    container whose ParameterName elements are all empty both return
    (None, version) without raising. Each degrades to today's unvalidated
    behaviour -- so the search runs, including for a property the type does not
    define -- and each is cached, so the second call issues no second read.
    """
    discovery_route, _, undefined = _mock_search_validation_routes(
        mock_hmc, discovery=discovery
    )

    async with HMCClient(make_config()) as hmc:
        first = await hmc.search_uom(
            _SEARCH_VALIDATION_TYPE, "NoSuchProperty", "web", validate=True
        )
        second = await hmc.search_uom(
            _SEARCH_VALIDATION_TYPE, "NoSuchProperty", "web", validate=True
        )

    assert first[0]["Resource"]["PartitionName"] == "web"
    assert second == first
    assert undefined.call_count == 2
    assert discovery_route.call_count == 1


@pytest.mark.asyncio
async def test_search_uom_defaults_to_no_validation(mock_hmc):
    """Omitting validate makes no discovery request and sends the search."""
    discovery, _, undefined = _mock_search_validation_routes(mock_hmc)

    async with HMCClient(make_config()) as hmc:
        await hmc.search_uom(_SEARCH_VALIDATION_TYPE, "NoSuchProperty", "web")

    assert discovery.call_count == 0
    assert undefined.call_count == 1


def test_search_uom_validate_is_keyword_only_and_defaults_false():
    """validate cannot be passed positionally, and is off unless asked for."""
    validate = inspect.signature(HMCClient.search_uom).parameters["validate"]

    assert validate.kind is inspect.Parameter.KEYWORD_ONLY
    assert validate.default is False


@pytest.mark.asyncio
async def test_search_uom_validate_caches_nothing_when_the_read_is_cancelled(mock_hmc):
    """A cancelled discovery read caches nothing and is retried.

    This is the one carve-out in the cost bound, and it holds only because
    asyncio.CancelledError is a BaseException that the helper's `except
    HMCError` does not catch. A later `except Exception` -- or an explicit
    CancelledError handler added for "robustness" -- would silently cache a
    negative entry and disable validation for the type on a caller's timeout,
    which is the opposite of what the failure model promises. Nothing else
    pins it.

    A timeout is deliberately not this case: _request converts
    httpx.TimeoutException to HMCTransportError, which the helper does catch
    and cache. That path is covered by the degradation test above.
    """
    started = asyncio.Event()
    attempts = 0

    async def slow_discovery(request):
        # respx increments call_count only when a response is returned, and
        # these attempts are cancelled in flight -- so count them here.
        nonlocal attempts
        attempts += 1
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable: the call is cancelled first")

    mock_hmc.get(_SEARCH_DISCOVERY).mock(side_effect=slow_discovery)
    mock_hmc.get(_SEARCH_DEFINED).mock(
        return_value=httpx.Response(200, text=_SEARCH_RESULT_FEED)
    )

    async with HMCClient(make_config()) as hmc:
        task = asyncio.create_task(
            hmc.search_uom(
                _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert hmc._search_parameter_names == {}
        assert not hmc._search_parameter_names_lock.locked()

        # The next validated call re-reads rather than inheriting a cached
        # negative entry; it reaches the same stalled route, so cancel it too
        # and assert on the attempt count.
        retry = asyncio.create_task(
            hmc.search_uom(
                _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
            )
        )
        started.clear()
        await started.wait()
        retry.cancel()
        with pytest.raises(asyncio.CancelledError):
            await retry

    assert attempts == 2


@pytest.mark.asyncio
async def test_search_uom_validate_caps_the_names_it_enumerates(mock_hmc):
    """The refusal message is a diagnostic, not an amplifier.

    The set rendered here is whatever the discovery read returned. The parse is
    captured at two levels (ADR 0142), so this is no longer about a wrong
    guess; it is about the levels nobody has measured. One answering the
    query-less anchor with an instance feed would fill the set with per-instance
    data bounded only by HMC_MAX_RESPONSE_BYTES, so an uncapped join builds a
    message the size of the response -- and one made of operator instance names.
    """
    many = [f"Param{i:04d}" for i in range(500)]
    mock_hmc.get(_SEARCH_DISCOVERY).mock(
        return_value=httpx.Response(
            200, text=_search_parameter_entry(_SEARCH_VALIDATION_TYPE, *many)
        )
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError) as exc_info:
            await hmc.search_uom(
                _SEARCH_VALIDATION_TYPE, "NoSuchProperty", "web", validate=True
            )

    message = str(exc_info.value)
    assert "Param0000" in message
    assert "Param0019" in message
    assert "Param0020" not in message
    assert "and 480 more." in message
    # The whole set joined would run past 5,000 characters; the count cap keeps
    # the message bounded by _MAX_REPORTED_NAMES rather than by the set size.
    assert len(message) < 500


@pytest.mark.asyncio
async def test_search_uom_validate_truncates_a_single_oversized_name(mock_hmc):
    """The count cap is not a byte bound, so the length cap carries this case.

    One name is never twenty-one, so capping the count alone never engages
    here: a single element carrying a whole response body renders in full. The
    element's text is bounded only by HMC_MAX_RESPONSE_BYTES (32 MiB by
    default), so without a per-name truncation this message is the size of the
    response. A name this long is already evidence the parse is wrong, which is
    the premise ADR 0142 records.

    The sibling test above cannot discriminate this: its 500 names are 9
    characters each, so a byte budget and a count budget behave identically
    against it.
    """
    oversized = "N" * 100_000
    mock_hmc.get(_SEARCH_DISCOVERY).mock(
        return_value=httpx.Response(
            200, text=_search_parameter_entry(_SEARCH_VALIDATION_TYPE, oversized)
        )
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError) as exc_info:
            await hmc.search_uom(
                _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
            )

    message = str(exc_info.value)
    assert "..." in message
    assert len(message) < 1_000
    assert oversized not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [False, True])
async def test_search_uom_validate_truncates_at_the_length_cap(mock_hmc, over):
    """The cap is pinned where it sits, not merely as "a cap exists".

    The 100,000-character name above discriminates only whether truncation
    happens at all -- raising the constant to 4,096 still kills it. These two
    rows sit either side of the boundary, so an off-by-one in the comparison or
    in the slice fails one of them. The constant is read rather than written as
    a literal: the boundary is the cap's, wherever it is set.
    """
    cap = client_core._MAX_REPORTED_NAME_LENGTH
    name = "N" * (cap + 1 if over else cap)
    mock_hmc.get(_SEARCH_DISCOVERY).mock(
        return_value=httpx.Response(
            200, text=_search_parameter_entry(_SEARCH_VALIDATION_TYPE, name)
        )
    )

    async with HMCClient(make_config()) as hmc:
        with pytest.raises(ValueError) as exc_info:
            await hmc.search_uom(
                _SEARCH_VALIDATION_TYPE, "PartitionName", "web", validate=True
            )

    message = str(exc_info.value)
    if over:
        assert f"{'N' * cap}..." in message
        assert name not in message
    else:
        assert name in message
        assert "..." not in message


# ---------------------------------------------------------------------------
# ADR 0222: trust_system_store — OS certificate store opt-in
# ---------------------------------------------------------------------------
#
# _new_http_client passes verify=<SSLContext|bool> to httpx.AsyncClient.
# We stub out AsyncClient to capture that argument before httpx reads
# SSL_CERT_FILE (which it does at __init__ time, not lazily).


class _CapturingAsyncClient:
    """Stub for httpx.AsyncClient that records the verify= kwarg."""

    captured: list

    def __init__(self, **kwargs):
        type(self).captured.append(kwargs.get("verify"))
        self.base_url = kwargs.get("base_url", httpx.URL("https://hmc.test:443"))

    async def aclose(self):
        pass


def _patch_async_client(monkeypatch):
    """Install a capturing stub and return the list it writes to."""
    _CapturingAsyncClient.captured = []
    monkeypatch.setattr("hmcpctl.client.core.httpx.AsyncClient", _CapturingAsyncClient)
    return _CapturingAsyncClient.captured


def test_trust_system_store_on_with_verify_ssl_uses_ssl_context(monkeypatch):
    """verify_ssl=True + trust_system_store=True (default) passes an SSLContext."""
    import ssl

    import truststore

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    captured = _patch_async_client(monkeypatch)
    HMCClient(make_config(verify_ssl=True, trust_system_store=True))
    assert len(captured) >= 1
    assert isinstance(captured[0], ssl.SSLContext)
    assert isinstance(captured[0], truststore.SSLContext)


def test_trust_system_store_off_with_verify_ssl_does_not_use_ssl_context(monkeypatch):
    """verify_ssl=True + trust_system_store=False passes True (certifi roots)."""
    import truststore

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    captured = _patch_async_client(monkeypatch)
    HMCClient(make_config(verify_ssl=True, trust_system_store=False))
    assert len(captured) >= 1
    assert not isinstance(captured[0], truststore.SSLContext)


def test_trust_system_store_ssl_cert_file_wins_over_system_store(monkeypatch):
    """When SSL_CERT_FILE is set, trust_system_store is bypassed."""
    import truststore

    monkeypatch.setenv("SSL_CERT_FILE", "/some/bundle.pem")
    captured = _patch_async_client(monkeypatch)
    HMCClient(make_config(verify_ssl=True, trust_system_store=True))
    assert len(captured) >= 1
    assert not isinstance(captured[0], truststore.SSLContext)


def test_trust_system_store_ssl_cert_dir_wins_over_system_store(monkeypatch):
    """When SSL_CERT_DIR is set, trust_system_store is bypassed."""
    import truststore

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", "/some/ca-dir")
    captured = _patch_async_client(monkeypatch)
    HMCClient(make_config(verify_ssl=True, trust_system_store=True))
    assert len(captured) >= 1
    assert not isinstance(captured[0], truststore.SSLContext)


def test_trust_system_store_ignored_when_verify_ssl_false(monkeypatch):
    """trust_system_store has no effect when verify_ssl=False."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    captured = _patch_async_client(monkeypatch)
    HMCClient(make_config(verify_ssl=False, trust_system_store=True))
    # When verify_ssl=False, verify stays False (a bool), not an SSLContext.
    assert len(captured) >= 1
    assert captured[0] is False


def test_tls_audit_record_still_names_verify_ssl_source_not_trust_system_store(
    monkeypatch,
):
    """ADR 0222: trust_system_store changes the context, not the audit source."""
    monkeypatch.delenv("HMC_VERIFY_SSL", raising=False)
    monkeypatch.setenv("HMC_VERIFY_SSL", "false")
    caught = _capture_audit()

    HMCClient(
        HMCConfig(
            host="hmc.test",
            user="hscroot",
            password="abc123",  # pragma: allowlist secret
        )
    )

    assert len(caught) == 1
    assert caught[0]["source"] == "environment:HMC_VERIFY_SSL"


# ---------------------------------------------------------------------------
# ADR 0223: ca_bundle — first-class CA-path setting
# ---------------------------------------------------------------------------
#
# _new_http_client passes verify=<path|SSLContext|bool> to httpx.AsyncClient.
# We reuse _patch_async_client / _CapturingAsyncClient from ADR 0222's section.


def test_ca_bundle_set_passes_path_as_verify(monkeypatch):
    """ca_bundle set and verify_ssl=True passes the path string to AsyncClient."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    captured = _patch_async_client(monkeypatch)
    HMCClient(make_config(verify_ssl=True, ca_bundle="/path/to/hmc-ca.pem"))
    assert len(captured) >= 1
    assert captured[0] == "/path/to/hmc-ca.pem"


def test_ca_bundle_wins_over_ssl_cert_file(monkeypatch):
    """When ca_bundle is set, SSL_CERT_FILE is ignored for verify=."""
    monkeypatch.setenv("SSL_CERT_FILE", "/some/other/bundle.pem")
    captured = _patch_async_client(monkeypatch)
    HMCClient(make_config(verify_ssl=True, ca_bundle="/hmc-specific.pem"))
    assert len(captured) >= 1
    # Must be the ca_bundle path, not a bool or SSLContext
    assert captured[0] == "/hmc-specific.pem"


def test_ca_bundle_wins_over_trust_system_store(monkeypatch):
    """When ca_bundle is set, trust_system_store is bypassed (no SSLContext)."""
    import ssl

    import truststore

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    captured = _patch_async_client(monkeypatch)
    HMCClient(
        make_config(
            verify_ssl=True,
            trust_system_store=True,
            ca_bundle="/hmc-specific.pem",
        )
    )
    assert len(captured) >= 1
    # Must be the path string, not a truststore.SSLContext
    assert not isinstance(captured[0], ssl.SSLContext)
    assert not isinstance(captured[0], truststore.SSLContext)
    assert captured[0] == "/hmc-specific.pem"


def test_ca_bundle_ignored_when_verify_ssl_false(monkeypatch):
    """ca_bundle has no effect when verify_ssl=False."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    captured = _patch_async_client(monkeypatch)
    HMCClient(make_config(verify_ssl=False, ca_bundle="/hmc-specific.pem"))
    assert len(captured) >= 1
    # verify_ssl=False → verify=False regardless of ca_bundle
    assert captured[0] is False


def test_ca_bundle_none_falls_through_to_trust_system_store(monkeypatch):
    """When ca_bundle is None, the ADR 0222 trust_system_store path is used."""
    import ssl

    import truststore

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    captured = _patch_async_client(monkeypatch)
    HMCClient(make_config(verify_ssl=True, trust_system_store=True, ca_bundle=None))
    assert len(captured) >= 1
    assert isinstance(captured[0], ssl.SSLContext)
    assert isinstance(captured[0], truststore.SSLContext)


def test_tls_audit_record_still_names_verify_ssl_source_not_ca_bundle(
    monkeypatch,
):
    """ADR 0223: ca_bundle changes the trust source, not the audit verify_ssl source."""
    monkeypatch.delenv("HMC_VERIFY_SSL", raising=False)
    monkeypatch.setenv("HMC_VERIFY_SSL", "false")
    caught = _capture_audit()

    HMCClient(
        HMCConfig(
            host="hmc.test",
            user="hscroot",
            password="abc123",  # pragma: allowlist secret
            ca_bundle="/hmc-specific.pem",
        )
    )

    assert len(caught) == 1
    assert caught[0]["source"] == "environment:HMC_VERIFY_SSL"
