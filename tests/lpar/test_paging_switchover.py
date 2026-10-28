"""One attempt with actual observation/ownership logic and independent readback."""

import asyncio
import importlib.util
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncssh
import pytest
from lpar.test_paging_observation import (
    PRIMARY,
    SECONDARY,
    SYSTEM,
    TARGET,
    observe,
    paging_boundary,  # noqa: F401 - pytest discovers imported fixtures
)

from hmcpctl.operations.lpar import ownership
from hmcpctl.ssh import memory as native
from hmcpctl.ssh import transport
from hmcpctl.ssh.transport import HMCCLIError, _process_error


def subject():
    name = "hmcpctl.operations.lpar.paging"
    assert importlib.util.find_spec(name), "one-attempt owner is missing"
    return __import__(name, fromlist=["switch_lpar_paging_vios"])


def native_error(status=1, signal=None):
    return _process_error(
        "chhwres",
        asyncssh.ProcessError(
            env={},
            command="chhwres",
            subsystem=None,
            exit_status=status,
            exit_signal=signal,
            returncode=status,
            stdout="",
            stderr="HSCL1234 private-body",
        ),
    )


@pytest.fixture
def attempt(request, monkeypatch):
    b = request.getfixturevalue("paging_boundary")
    b.denied = None
    b.error = None
    b.change = True

    async def description(config, system, name, **kwargs):
        if b.denied == name:
            return "[hmcpctl owner:other created:2026-01-01]"
        return ""

    b.description = AsyncMock(side_effect=description)
    monkeypatch.setattr(ownership, "get_lpar_description", b.description)

    async def submit(*args, **kwargs):
        if b.change:
            before = f"/VirtualIOServer/{SECONDARY}"
            # Only the current link changes; configured primary/secondary stay exact.
            old = f'<CurrentPagingServicePartition><link href="https://hmc.test/rest/api/uom{before}"/></CurrentPagingServicePartition>'
            b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
                old, old.replace(SECONDARY, PRIMARY)
            )
            b.current.return_value = b.current.return_value.replace(
                "paging-b", "paging-a"
            )
        if b.error:
            raise b.error
        return "native-ack"

    b.submit = AsyncMock(side_effect=submit)
    monkeypatch.setattr(native, "run_hmc_command", b.submit)
    return b


async def run(b, **kwargs):
    return await subject().switch_lpar_paging_vios(
        b.hmc,
        TARGET.upper(),
        system_name_or_uuid=SYSTEM,
        expected_current_paging_vios_name_or_uuid=SECONDARY.upper(),
        exclusive_writer_window=True,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,submission",
    [
        (None, "acknowledged"),
        (native_error(), "remote-error"),
        (HMCCLIError("private-body"), "unknown"),
    ],
)
async def test_all_submission_axes_expected_other_complete(attempt, error, submission):
    b = attempt
    b.error = error
    result = await run(b)
    assert result.outcome == "completed" and result.workflow_completed is True
    assert result.observation == "expected" and result.preservation == "verified"
    assert result.submission == submission
    assert (
        result.expected_current.uuid == SECONDARY
        and result.expected_other.uuid == PRIMARY
    )
    assert result.after.current_native.uuid == result.after.current_uom.uuid == PRIMARY
    assert "private-body" not in repr(result)
    b.submit.assert_awaited_once_with(
        b.hmc.config, "chhwres -r mem -m system -o so --id 9", max_output_bytes=65536
    )
    assert [c.args[2] for c in b.description.await_args_list] == [
        "consumer",
        "paging-a",
        "paging-b",
    ] * 2
    assert all(
        c.kwargs == {"max_output_bytes": 5 * 1024 * 1024}
        for c in b.description.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,outcome",
    [
        (None, "unverified"),
        (native_error(), "rejected"),
        (HMCCLIError("unknown"), "unverified"),
    ],
)
async def test_unchanged_outcome_no_retry(attempt, error, outcome):
    attempt.change = False
    attempt.error = error
    result = await run(attempt)
    assert result.outcome == outcome and result.observation == "unchanged"
    assert result.workflow_completed is False
    attempt.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["consumer", "paging-a", "paging-b"])
async def test_each_target_denied_with_supported_remedy(attempt, name, caplog):
    attempt.denied = name
    with pytest.raises(PermissionError) as error:
        await run(attempt)
    assert "resolve ownership" in str(error.value)
    assert "ownership_override=true" not in str(error.value)
    assert error.value.__suppress_context__ is True
    assert "ownership_denied" in caplog.text or "ownership-denied" in caplog.text
    attempt.submit.assert_not_called()


@pytest.mark.asyncio
async def test_precheck_remedy_is_not_submission(attempt):
    attempt.description.side_effect = HMCCLIError(
        "private-body; retry ownership_override=true"
    )
    with pytest.raises(HMCCLIError, match="readable ownership metadata") as error:
        await run(attempt)
    assert "private-body" not in str(
        error.value
    ) and "ownership_override=true" not in str(error.value)
    assert error.value.__suppress_context__ is True
    attempt.submit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
async def test_invalid_timeout_before_any_guard_read(attempt, timeout):
    attempt.hmc.config.ssh_timeout = timeout
    with pytest.raises(ValueError, match="finite positive"):
        await run(attempt)
    attempt.description.assert_not_called()
    attempt.hmc._get.assert_not_called()
    attempt.submit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [False, 1, "true", None])
async def test_window_required_before_io(attempt, window):
    with pytest.raises((ValueError, TypeError)):
        await subject().switch_lpar_paging_vios(
            attempt.hmc,
            TARGET,
            system_name_or_uuid=SYSTEM,
            expected_current_paging_vios_name_or_uuid=SECONDARY,
            exclusive_writer_window=window,
        )
    attempt.hmc._get.assert_not_called()
    attempt.submit.assert_not_called()


@pytest.mark.asyncio
async def test_stale_expected_refuses(attempt):
    with pytest.raises(ValueError):
        await subject().switch_lpar_paging_vios(
            attempt.hmc,
            TARGET,
            system_name_or_uuid=SYSTEM,
            expected_current_paging_vios_name_or_uuid=PRIMARY,
            exclusive_writer_window=True,
        )
    attempt.submit.assert_not_called()


@pytest.mark.asyncio
async def test_final_current_drift_refuses_before_switch(attempt):
    async def description(*args, **kwargs):
        if attempt.description.await_count == 3:
            old = f'<CurrentPagingServicePartition><link href="https://hmc.test/rest/api/uom/VirtualIOServer/{SECONDARY}"/></CurrentPagingServicePartition>'
            attempt.rows["LogicalPartition"] = attempt.rows["LogicalPartition"].replace(
                old, old.replace(SECONDARY, PRIMARY)
            )
            attempt.current.return_value = attempt.current.return_value.replace(
                "paging-b", "paging-a"
            )
        return ""

    attempt.description.side_effect = description
    with pytest.raises(ValueError, match="changed before"):
        await run(attempt)
    attempt.submit.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_timeout_admission_before_each_guard_read(attempt):
    async def description(*args, **kwargs):
        attempt.hmc.config.ssh_timeout = float("inf")
        return ""

    attempt.description.side_effect = description
    with pytest.raises(ValueError, match="finite positive"):
        await run(attempt)
    attempt.description.assert_awaited_once()
    attempt.submit.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["lpar", "system", "provider"])
@pytest.mark.parametrize("bad", ["", " ", None])
async def test_blank_selector_admission_before_io(attempt, target, bad):
    args = {
        "lpar_name_or_uuid": TARGET,
        "system_name_or_uuid": SYSTEM,
        "expected_current_paging_vios_name_or_uuid": SECONDARY,
        "exclusive_writer_window": True,
    }
    args[
        {
            "lpar": "lpar_name_or_uuid",
            "system": "system_name_or_uuid",
            "provider": "expected_current_paging_vios_name_or_uuid",
        }[target]
    ] = bad
    with pytest.raises(ValueError):
        await subject().switch_lpar_paging_vios(attempt.hmc, **args)
    attempt.hmc._get.assert_not_called()
    attempt.description.assert_not_called()
    attempt.submit.assert_not_called()


@pytest.mark.asyncio
async def test_drift_and_unavailable_preserves_both(attempt):
    mod = subject()
    before = await observe(attempt)
    after = replace(
        before,
        paths=((PRIMARY, "changed-device"), (SECONDARY, "disk-b")),
        current_uom=None,
        unavailable=("uom-current",),
    )
    result = mod.reduce_paging_attempt(
        SYSTEM,
        before,
        after,
        before.current_uom,
        before.configured.primary,
        "unknown",
        None,
        (),
    )
    assert result.outcome == "partial" and result.preservation == "drifted"
    assert result.workflow_completed is False
    assert "uom-current" in result.diagnostics and "paths" in result.diagnostics
    assert result.after is after


@pytest.mark.asyncio
async def test_optional_etag_and_orientation_are_not_drift(attempt):
    mod = subject()
    before = await observe(attempt)
    after = replace(
        before,
        paths=tuple(reversed(before.paths)),
        configured=replace(before.configured, etag="new"),
        states=(),
    )
    assert mod.compare_paging(before, after) == ("verified", ())


@pytest.mark.asyncio
async def test_changed_configured_plane_is_unavailable(attempt):
    before = await observe(attempt)
    after = replace(
        before,
        configured=replace(
            before.configured,
            plane="associated-profile",
            profile_uuid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            profile_name="associated",
            setting_id="setting",
        ),
    )
    assert subject().compare_paging(before, after) == (
        "unavailable",
        ("configured.plane",),
    )


@pytest.mark.asyncio
async def test_partial_provider_plane_is_total(attempt):
    before = await observe(attempt)
    after = replace(before, providers=(), unavailable=("pool-providers",))
    result = subject().reduce_paging_attempt(
        SYSTEM,
        before,
        after,
        before.current_uom,
        before.configured.primary,
        "acknowledged",
        None,
        (),
    )
    assert result.outcome == "unverified" and result.workflow_completed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,signal,expected",
    [
        (1, None, "remote-error"),
        (0, None, "unknown"),
        (-1, None, "unknown"),
        (True, None, "unknown"),
        (None, None, "unknown"),
        (1, ("TERM", False, "", ""), "unknown"),
    ],
)
async def test_submission_provenance(attempt, status, signal, expected):
    attempt.error = native_error(status, signal)
    result = await run(attempt)
    assert result.submission == expected and result.workflow_completed is True


@pytest.mark.asyncio
async def test_declared_secret_reconstruction_keeps_completion(attempt, monkeypatch):
    attempt.hmc.config.user = "fixture-user"
    attempt.hmc.config.password = (
        "fixture-password"  # pragma: allowlist secret - offline fixture
    )
    connection = MagicMock()
    connection.run = AsyncMock(
        side_effect=asyncssh.ProcessError(
            env={},
            command="diagnostic",
            subsystem=None,
            exit_status=1,
            exit_signal=None,
            returncode=1,
            stdout="",
            stderr="HSCL1234 fixture-password",
        )
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(transport.asyncssh, "connect", MagicMock(return_value=context))
    with pytest.raises(HMCCLIError) as caught:
        await transport.run_hmc_command(
            attempt.hmc.config, "diagnostic", secrets=("fixture-password",)
        )
    assert caught.value.remote_nonzero_completed is True
    assert "fixture-password" not in str(caught.value)
    attempt.error = caught.value
    result = await run(attempt)
    assert result.submission == "remote-error" and result.outcome == "completed"
    assert result.remote_refusal_code == "HSCL1234"
    attempt.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_finite_silent_peer_no_retry(attempt, monkeypatch):
    attempt.hmc.config.user = "fixture-user"
    attempt.hmc.config.password = (
        "fixture-password"  # pragma: allowlist secret - offline fixture
    )
    attempt.hmc.config.ssh_timeout = 0.02
    pending = asyncio.Event()

    async def read(_):
        await pending.wait()

    stream = SimpleNamespace(read=read)
    process = SimpleNamespace(stdout=stream, stderr=stream, close=MagicMock())
    connection = MagicMock()
    connection.create_process = AsyncMock(return_value=process)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=connection)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(transport.asyncssh, "connect", MagicMock(return_value=context))
    monkeypatch.setattr(native, "run_hmc_command", transport.run_hmc_command)
    result = await asyncio.wait_for(run(attempt), timeout=2)
    assert result.submission == "unknown" and result.outcome == "unverified"
    assert result.observation == "unchanged" and result.workflow_completed is False
    connection.create_process.assert_awaited_once()
    assert attempt.current.await_count == 3
    connection.abort.assert_called_once()


@pytest.mark.asyncio
async def test_uncoded_remote_nonzero_is_not_unknown(attempt):
    attempt.error = _process_error(
        "chhwres",
        asyncssh.ProcessError(
            env={},
            command="chhwres",
            subsystem=None,
            exit_status=1,
            exit_signal=None,
            returncode=1,
            stdout="uncoded refusal",
            stderr="",
        ),
    )
    attempt.change = False
    result = await run(attempt)
    assert result.submission == "remote-error" and result.outcome == "rejected"
    assert result.remote_refusal_code is None and result.workflow_completed is False
    attempt.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_acknowledgment_without_uom_after_proof_is_unverified(attempt):
    submit = attempt.submit.side_effect

    async def missing_after(*args, **kwargs):
        result = await submit(*args, **kwargs)
        attempt.rows["LogicalPartition"] = attempt.rows["LogicalPartition"].replace(
            "CurrentPagingServicePartition", "UnrelatedCurrent"
        )
        return result

    attempt.submit.side_effect = missing_after
    result = await run(attempt)
    assert result.submission == "acknowledged" and result.outcome == "unverified"
    assert (
        result.observation == "unavailable"
        and result.after.current_native.uuid == PRIMARY
    )
    assert result.after.current_uom is None and "uom-current" in result.diagnostics
    attempt.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["pool", "provider"])
@pytest.mark.parametrize("drift", [False, True])
async def test_unavailable_post_reader_is_not_invented_drift(attempt, missing, drift):
    from hmcpctl.errors import HMCError

    b = attempt
    b.change = False
    b.error = HMCCLIError("unknown completion")
    get = b.hmc._get.side_effect

    async def unavailable(path, *args, **kwargs):
        if b.submit.await_count:
            if drift:
                b.rows["LogicalPartition"] = b.rows["LogicalPartition"].replace(
                    "<PartitionState>running", "<PartitionState>not activated"
                )
            if (
                path.endswith("/SharedMemoryPool")
                if missing == "pool"
                else path.lower().endswith(PRIMARY)
            ):
                raise HMCError("read unavailable")
        return await get(path, *args, **kwargs)

    b.hmc._get.side_effect = unavailable
    result = await run(b)
    assert result.submission == "unknown" and result.observation == "unchanged"
    assert result.preservation == ("drifted" if drift else "unavailable")
    assert result.outcome == ("partial" if drift else "unverified")
    assert result.after.unavailable and result.workflow_completed is False
    assert set(result.after.invalid) == ({"consumer-not-running"} if drift else set())
    b.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_readable_device_drift_survives_provider_detail_failure(attempt):
    from hmcpctl.errors import HMCError

    b = attempt
    b.change = False
    b.error = HMCCLIError("unknown completion")
    get = b.hmc._get.side_effect

    async def unavailable(path, *args, **kwargs):
        if b.submit.await_count:
            b.rows["SharedMemoryPool"] = b.rows["SharedMemoryPool"].replace(
                "device-one", "device-two"
            )
            if path.lower().endswith(PRIMARY):
                raise HMCError("provider detail unavailable")
        return await get(path, *args, **kwargs)

    b.hmc._get.side_effect = unavailable
    result = await run(b)
    assert result.preservation == "drifted" and result.outcome == "partial"
    assert result.after.unique_device_id == "device-two"
    assert "unique_device_id" in result.diagnostics and result.after.unavailable
    assert result.after.invalid == ()
    b.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["pool", "provider"])
async def test_missing_before_reader_still_prevents_submission(attempt, missing):
    from hmcpctl.errors import HMCError

    b = attempt
    get = b.hmc._get.side_effect

    async def unavailable(path, *args, **kwargs):
        if (
            path.endswith("/SharedMemoryPool")
            if missing == "pool"
            else path.lower().endswith(PRIMARY)
        ):
            raise HMCError("read unavailable")
        return await get(path, *args, **kwargs)

    b.hmc._get.side_effect = unavailable
    with pytest.raises(ValueError, match="prerequisites are unavailable"):
        await run(b)
    b.submit.assert_not_awaited()
    b.description.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["link", "containment"])
@pytest.mark.parametrize("change", ["unchanged", "redundancy", "device"])
async def test_partial_pool_after_attempt_keeps_observed_drift(
    attempt, missing, change
):
    b = attempt
    b.change = False
    b.error = HMCCLIError("unknown completion")
    b.partial_pool_response(missing, change, after_attempt=True)
    result = await run(b)
    drift = change != "unchanged"
    assert result.submission == "unknown" and result.observation == "unchanged"
    assert result.preservation == ("drifted" if drift else "unavailable")
    assert result.outcome == ("partial" if drift else "unverified")
    assert result.after.unavailable and result.workflow_completed is False
    assert result.after.uom_redundant is (change != "redundancy")
    assert result.after.unique_device_id == (
        "device-two" if change == "device" else "device-one"
    )
    b.submit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["link", "containment"])
async def test_partial_pool_before_attempt_remains_closed(attempt, missing):
    attempt.partial_pool_response(missing, "unchanged")
    with pytest.raises(ValueError, match="prerequisites are unavailable"):
        await run(attempt)
    attempt.submit.assert_not_awaited()
    attempt.description.assert_not_awaited()
