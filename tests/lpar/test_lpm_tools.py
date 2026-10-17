"""Tool-layer tests for the Live Partition Mobility MCP tools.

The job XML builders and client methods are covered in test_lpm.py; these
tests call the actual ``@mcp.tool`` functions in ``server_tools.lpar.migration`` against the
respx ``mock_hmc`` router so the argument->URL and argument->XML mapping in
the tool bodies is exercised — the layer the client tests skip.
"""

from dataclasses import asdict
from unittest.mock import ANY, AsyncMock, patch

import httpx
import pytest
from conftest import (
    COMPLETED_JOB_ENTRY,
    JOB_ENTRY,
    JOB_ID,
    RUNNING_JOB_ENTRY,
    captured_lpar_entry,
)

from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.migration import (
    RemoteRestartRequest,
    abort_lpar_migration,
    recover_lpar_migration,
    remote_restart_lpar,
)
from hmcpctl.server_tools.lpar.migration import (
    hmc_migrate_abort_lpar,
    hmc_migrate_lpar,
    hmc_migrate_recover_lpar,
    hmc_migrate_validate_lpar,
    hmc_remote_restart_lpar,
)

LPAR_UUID = "00000000-0000-0000-0000-000000000002"
TARGET_SYSTEM_UUID = "00000000-0000-0000-0000-000000000001"
JOB_OUTCOME_KEYS = {
    "job_id",
    "status",
    "timed_out",
    "error",
    "job",
    "found",
    "job_href",
}


@pytest.fixture(autouse=True)
def _authorize_lpar_mutations(monkeypatch, mock_hmc):
    mock_hmc.get("/rest/api/uom/ManagedSystem/quick/All").mock(
        return_value=httpx.Response(
            200, json=[{"UUID": TARGET_SYSTEM_UUID, "SystemName": "source-system"}]
        )
    )
    mock_hmc.get(
        f"/rest/api/uom/ManagedSystem/{TARGET_SYSTEM_UUID}/LogicalPartition"
    ).mock(
        return_value=httpx.Response(
            200,
            text='<feed xmlns="http://www.w3.org/2005/Atom">'
            + captured_lpar_entry(LPAR_UUID, "lpar1")
            + "</feed>",
        )
    )

    async def authorize(hmc, system, lpar, **_kwargs):
        from hmcpctl.resource_identity import resolve_lpar_uuid

        return await resolve_lpar_uuid(hmc, lpar, system_name_or_uuid=system)

    monkeypatch.setattr(
        "hmcpctl.operations.lpar.migration.resolve_and_authorize_lpar_mutation",
        authorize,
    )


LPM_RECOVERY_TOOL_CASES = [
    (hmc_migrate_abort_lpar, "MigrateAbort", (LPAR_UUID,)),
    (hmc_migrate_recover_lpar, "MigrateRecover", (LPAR_UUID,)),
    (
        hmc_remote_restart_lpar,
        "RemoteRestart",
        (LPAR_UUID, "restart", "source-system", "vrml12-fsp"),
    ),
]
LPM_RECOVERY_OPERATION_CASES = [
    (abort_lpar_migration, "lpar_migrate_abort", (None, LPAR_UUID)),
    (recover_lpar_migration, "lpar_migrate_recover", (None, LPAR_UUID)),
    (
        remote_restart_lpar,
        "lpar_remote_restart",
        ("source-system", LPAR_UUID, RemoteRestartRequest("restart")),
    ),
]


def _hmc_env(monkeypatch) -> None:
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")


def _job_route(router, operation: str):
    return router.put(
        f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/{operation}"
    ).mock(return_value=httpx.Response(202, text=JOB_ENTRY))


def test_migrate_lpar_submits_job(monkeypatch, mock_hmc):
    """hmc_migrate_lpar PUTs a Migrate job with the target system."""
    _hmc_env(monkeypatch)
    route = _job_route(mock_hmc, "Migrate")
    result = hmc_migrate_lpar(
        LPAR_UUID,
        "vrml12-fsp",
        target_profile_name="prof1",
        wait_time=60,
        validate_first=False,
    )
    body = route.calls.last.request.content.decode()
    assert "Migrate</OperationName>" in body
    assert "TargetManagedSystemName" in body and "vrml12-fsp" in body
    assert "TargetProfileName" in body and "prof1" in body
    assert "WaitTime" in body and "60" in body
    assert result.job_id == JOB_ID


def test_migrate_lpar_resolves_target_system_uuid(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    route = _job_route(mock_hmc, "Migrate")
    resolver = AsyncMock(return_value="vrml12-fsp")

    with patch("hmcpctl.operations.lpar.migration.resolve_system_name", new=resolver):
        hmc_migrate_lpar(LPAR_UUID, TARGET_SYSTEM_UUID, validate_first=False)

    resolver.assert_awaited_once_with(ANY, TARGET_SYSTEM_UUID)
    body = route.calls.last.request.content.decode()
    assert "TargetManagedSystemName" in body and "vrml12-fsp" in body


def test_migrate_validate_lpar_submits_job(monkeypatch, mock_hmc):
    """hmc_migrate_validate_lpar PUTs a MigrateValidate job."""
    _hmc_env(monkeypatch)
    route = _job_route(mock_hmc, "MigrateValidate")
    hmc_migrate_validate_lpar(LPAR_UUID, "vrml12-fsp")
    body = route.calls.last.request.content.decode()
    assert "MigrateValidate</OperationName>" in body
    assert "vrml12-fsp" in body


def test_migrate_abort_lpar_submits_job(monkeypatch, mock_hmc):
    """hmc_migrate_abort_lpar PUTs a MigrateAbort job."""
    _hmc_env(monkeypatch)
    route = _job_route(mock_hmc, "MigrateAbort")
    hmc_migrate_abort_lpar(LPAR_UUID)
    assert "MigrateAbort</OperationName>" in route.calls.last.request.content.decode()


def test_migrate_recover_lpar_submits_job(monkeypatch, mock_hmc):
    """hmc_migrate_recover_lpar PUTs a MigrateRecover job."""
    _hmc_env(monkeypatch)
    route = _job_route(mock_hmc, "MigrateRecover")
    hmc_migrate_recover_lpar(LPAR_UUID)
    assert "MigrateRecover</OperationName>" in route.calls.last.request.content.decode()


def test_remote_restart_lpar_submits_job(monkeypatch, mock_hmc):
    """hmc_remote_restart_lpar PUTs a RemoteRestart job with the target."""
    _hmc_env(monkeypatch)
    route = _job_route(mock_hmc, "RemoteRestart")
    hmc_remote_restart_lpar(LPAR_UUID, "restart", "source-system", "vrml12-fsp")
    body = route.calls.last.request.content.decode()
    assert "RemoteRestart</OperationName>" in body
    assert "restart" in body
    assert "source-system" in body
    assert "vrml12-fsp" in body


@pytest.mark.parametrize(
    ("tool_fn", "operation", "args"),
    LPM_RECOVERY_TOOL_CASES,
)
def test_lpm_recovery_tools_wait_for_terminal_outcome(
    monkeypatch, mock_hmc, tool_fn, operation, args
):
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_VERIFY_SSL", "true")
    _job_route(mock_hmc, operation)
    poll_route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )

    result = tool_fn(*args, wait=True, timeout_seconds=60, poll_interval=1)

    assert poll_route.called
    assert set(asdict(result)) == JOB_OUTCOME_KEYS
    assert result.job_id == JOB_ID
    assert result.status == "COMPLETED_OK"
    assert result.timed_out is False
    assert result.error is None


@pytest.mark.parametrize(
    ("tool_fn", "operation", "args"),
    LPM_RECOVERY_TOOL_CASES,
)
def test_lpm_recovery_tools_return_explicit_timeout(
    monkeypatch, mock_hmc, tool_fn, operation, args
):
    _hmc_env(monkeypatch)
    monkeypatch.setenv("HMC_VERIFY_SSL", "true")
    _job_route(mock_hmc, operation)
    # The corpus holds a running read of another job only; this test pins the
    # timeout fields, not which job the read named.
    mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=RUNNING_JOB_ENTRY)
    )

    result = tool_fn(*args, wait=True, timeout_seconds=0, poll_interval=1)

    assert set(asdict(result)) == JOB_OUTCOME_KEYS
    assert result.status == "RUNNING"
    assert result.timed_out is True
    assert result.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "submit_method", "args"),
    LPM_RECOVERY_OPERATION_CASES,
)
async def test_lpm_recovery_operations_return_stable_submission_outcome(
    operation, submit_method, args
):
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": TARGET_SYSTEM_UUID, "SystemName": "source-system"}
    ]
    hmc.list_logical_partitions.return_value = [{"UUID": LPAR_UUID}]
    setattr(hmc, submit_method, AsyncMock(return_value={"UUID": "job-1"}))

    result = await operation(hmc, *args)

    assert set(asdict(result.job)) == JOB_OUTCOME_KEYS
    assert result.job.job_id == "job-1"
    assert result.job.status is None
    assert result.job.timed_out is False
    assert result.job.error is None
    hmc.wait_for_job_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "submit_method", "args"),
    LPM_RECOVERY_OPERATION_CASES,
)
async def test_lpm_recovery_operations_wait_for_terminal_outcome(
    operation, submit_method, args
):
    submitted = {"UUID": "job-1", "link": "/jobs/job-1"}
    completed = {"Resource": {"JobID": "job-1", "Status": "COMPLETED_OK"}}
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": TARGET_SYSTEM_UUID, "SystemName": "source-system"}
    ]
    hmc.list_logical_partitions.return_value = [{"UUID": LPAR_UUID}]
    setattr(hmc, submit_method, AsyncMock(return_value=submitted))
    hmc.wait_for_job_entry.return_value = completed

    result = await operation(hmc, *args, wait=True, timeout_seconds=60, poll_interval=2)

    assert set(asdict(result.job)) == JOB_OUTCOME_KEYS
    assert result.job.status == "COMPLETED_OK"
    assert result.job.timed_out is False
    hmc.wait_for_job_entry.assert_awaited_once_with(
        "job-1", 60, 2, job_href="/jobs/job-1"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "submit_method", "args"),
    LPM_RECOVERY_OPERATION_CASES,
)
async def test_lpm_recovery_operations_surface_terminal_failure(
    operation, submit_method, args
):
    submitted = {"UUID": "job-1", "link": "/jobs/job-1"}
    failed = {
        "Resource": {
            "JobID": "job-1",
            "Status": "COMPLETED_WITH_ERROR",
            "Results": {
                "JobParameter": {
                    "ParameterName": "ErrorData",
                    "ParameterValue": "Migration recovery failed",
                }
            },
        }
    }
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": TARGET_SYSTEM_UUID, "SystemName": "source-system"}
    ]
    hmc.list_logical_partitions.return_value = [{"UUID": LPAR_UUID}]
    setattr(hmc, submit_method, AsyncMock(return_value=submitted))
    hmc.wait_for_job_entry.return_value = failed

    result = await operation(hmc, *args, wait=True)

    assert set(asdict(result.job)) == JOB_OUTCOME_KEYS
    assert result.job.job_id == "job-1"
    assert result.job.status == "COMPLETED_WITH_ERROR"
    assert result.job.timed_out is False
    assert result.job.error == "Migration recovery failed"
    assert result.job.job is failed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "submit_method", "args"),
    LPM_RECOVERY_OPERATION_CASES,
)
async def test_lpm_recovery_operations_reject_invalid_active_timing_before_work(
    operation, submit_method, args
):
    hmc = AsyncMock()
    hmc.list_system_summaries.return_value = [
        {"UUID": TARGET_SYSTEM_UUID, "SystemName": "source-system"}
    ]
    hmc.list_logical_partitions.return_value = [{"UUID": LPAR_UUID}]

    with pytest.raises(ValueError, match="timeout_seconds"):
        await operation(hmc, *args, wait=True, timeout_seconds=-1)

    getattr(hmc, submit_method).assert_not_awaited()


def test_migrate_lpar_error_propagates(monkeypatch, mock_hmc):
    """A non-2xx job submission surfaces as HMCError naming the failing PUT."""
    _hmc_env(monkeypatch)
    mock_hmc.put(f"/rest/api/uom/LogicalPartition/{LPAR_UUID}/do/Migrate").mock(
        return_value=httpx.Response(500)
    )
    with pytest.raises(HMCError) as exc_info:
        hmc_migrate_lpar(LPAR_UUID, "vrml12-fsp", validate_first=False)
    assert exc_info.value.status_code == 500
    assert "do/Migrate" in str(exc_info.value)


# ---------------------------------------------------------------------- #
# wait=True path: migrate_lpar blocks until job reaches terminal state
# ---------------------------------------------------------------------- #


def test_migrate_lpar_wait_true_polls_to_completion(monkeypatch, mock_hmc):
    """hmc_migrate_lpar(wait=True) submits the job then polls until COMPLETED_OK."""
    _hmc_env(monkeypatch)
    submit_route = _job_route(mock_hmc, "Migrate")
    poll_route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    result = hmc_migrate_lpar(
        LPAR_UUID,
        "vrml12-fsp",
        wait=True,
        timeout_seconds=60,
        poll_interval=1,
        validate_first=False,
    )
    assert submit_route.called
    assert poll_route.called
    assert result.status == "COMPLETED_OK"


def test_migrate_lpar_wait_false_returns_submitted_job(monkeypatch, mock_hmc):
    """hmc_migrate_lpar(wait=False) returns the submitted job entry without polling."""
    _hmc_env(monkeypatch)
    submit_route = _job_route(mock_hmc, "Migrate")
    poll_route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )
    result = hmc_migrate_lpar(LPAR_UUID, "vrml12-fsp", wait=False, validate_first=False)
    assert submit_route.called
    assert not poll_route.called
    assert result.job_id == JOB_ID


def test_migrate_validate_wait_true_polls_to_completion(monkeypatch, mock_hmc):
    _hmc_env(monkeypatch)
    submit_route = _job_route(mock_hmc, "MigrateValidate")
    poll_route = mock_hmc.get(f"/rest/api/uom/jobs/{JOB_ID}").mock(
        return_value=httpx.Response(200, text=COMPLETED_JOB_ENTRY)
    )

    result = hmc_migrate_validate_lpar(
        LPAR_UUID, "vrml12-fsp", wait=True, timeout_seconds=60, poll_interval=1
    )

    assert submit_route.called
    assert poll_route.called
    assert result.status == "COMPLETED_OK"
