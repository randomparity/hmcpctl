"""Validation-first LPM operation contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from hmcpctl.errors import HMCError
from hmcpctl.jobs import JobOutcome
from hmcpctl.operations.lpar.migration import LpmMigrationRequest, migrate_lpar


@pytest.fixture(autouse=True)
def _authorize_lpar_mutations(monkeypatch):
    async def authorize(hmc, lpar, *, system_name_or_uuid=None, **_kwargs):
        from hmcpctl.resource_identity import resolve_lpar_uuid

        return await resolve_lpar_uuid(
            hmc, lpar, system_name_or_uuid=system_name_or_uuid
        )

    monkeypatch.setattr(
        "hmcpctl.operations.lpar.migration.resolve_and_authorize_lpar_mutation",
        authorize,
    )


def _job(status: str, *, error: str | None = None) -> dict:
    resource: dict[str, object] = {"JobID": status.lower(), "Status": status}
    if error is not None:
        resource["Results"] = {
            "JobParameter": {"ParameterName": "ErrorData", "ParameterValue": error}
        }
    return {"UUID": status.lower(), "Resource": resource}


def _client(validation: dict, migration: dict | None = None) -> AsyncMock:
    client = AsyncMock()
    client.find_partition_by_name.return_value = {"UUID": "lpar-1"}
    client.lpar_migrate_validate.return_value = _job("RUNNING")
    client.wait_for_job_entry.return_value = validation
    client.lpar_migrate.return_value = migration or _job("RUNNING")
    return client


@pytest.mark.parametrize("status", ["COMPLETED_OK"])
@pytest.mark.asyncio
async def test_default_waits_for_validation_then_submits_migration(status: str) -> None:
    client = _client(_job(status))
    events: list[str] = []

    async def submit_validation(*_args, **_kwargs):
        events.append("validate")
        return _job("RUNNING")

    async def wait_for_validation(*_args, **_kwargs):
        events.append("wait")
        return _job(status)

    async def submit_migration(*_args, **_kwargs):
        events.append("migrate")
        return _job("RUNNING")

    client.lpar_migrate_validate.side_effect = submit_validation
    client.wait_for_job_entry.side_effect = wait_for_validation
    client.lpar_migrate.side_effect = submit_migration

    result = await migrate_lpar(
        client, "lpar", LpmMigrationRequest("target"), wait=False
    )

    assert isinstance(result.job, JobOutcome)
    assert result.job.status == "RUNNING"
    assert events == ["validate", "wait", "migrate"]
    assert (
        call.lpar_migrate_validate(
            "lpar-1", "target", target_profile_name=None, wait_time=None
        )
        in client.method_calls
    )
    assert (
        call.lpar_migrate("lpar-1", "target", target_profile_name=None, wait_time=None)
        in client.method_calls
    )
    client.wait_for_job_entry.assert_awaited_once()


@pytest.mark.parametrize(
    "status",
    ["COMPLETED_WITH_ERROR", "FAILED_BEFORE_COMPLETION", "COMPLETED_WITH_WARNINGS"],
)
@pytest.mark.asyncio
async def test_failed_validation_blocks_migration_and_surfaces_detail(
    status: str,
) -> None:
    client = _client(_job(status, error="validation detail"))

    with pytest.raises(HMCError, match=f"status={status!r}.*validation detail"):
        await migrate_lpar(client, "lpar", LpmMigrationRequest("target"))

    client.lpar_migrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_canceled_validation_blocks_migration() -> None:
    client = _client(_job("CANCELED_WHILE_RUNNING"))

    with pytest.raises(HMCError, match=r"status='CANCELED_WHILE_RUNNING'"):
        await migrate_lpar(client, "lpar", LpmMigrationRequest("target"))

    client.lpar_migrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_validation_blocks_migration() -> None:
    client = _client(_job("RUNNING"))

    with pytest.raises(HMCError, match="status='RUNNING'"):
        await migrate_lpar(client, "lpar", LpmMigrationRequest("target"))

    client.lpar_migrate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["submit", "poll"])
async def test_validation_exception_blocks_migration(failure_point: str) -> None:
    client = _client(_job("COMPLETED_OK"))
    error = HMCError(f"validation {failure_point} failed")
    if failure_point == "submit":
        client.lpar_migrate_validate.side_effect = error
    else:
        client.wait_for_job_entry.side_effect = error

    with pytest.raises(HMCError) as exc_info:
        await migrate_lpar(client, "lpar", LpmMigrationRequest("target"))

    assert exc_info.value is error
    client.lpar_migrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_first_false_preserves_direct_submission() -> None:
    client = _client(_job("COMPLETED_WITH_ERROR"))

    result = await migrate_lpar(
        client, "lpar", LpmMigrationRequest("target"), validate_first=False
    )

    assert isinstance(result.job, JobOutcome)
    client.lpar_migrate_validate.assert_not_awaited()
    client.wait_for_job_entry.assert_not_awaited()
    client.lpar_migrate.assert_awaited_once()


@pytest.mark.asyncio
async def test_effective_validation_timing_fails_before_resolution() -> None:
    client = _client(_job("COMPLETED_OK"))

    with pytest.raises(ValueError, match="poll_interval"):
        await migrate_lpar(
            client, "lpar", LpmMigrationRequest("target"), poll_interval=0
        )

    client.find_partition_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_validation_message_is_repr_quoted() -> None:
    """HMC-supplied validation text cannot carry control characters into str()."""
    hostile = "boom\n\x1b[31moverridden\u2028mid"
    client = _client(_job("COMPLETED_WITH_ERROR", error=hostile))

    with pytest.raises(HMCError) as exc_info:
        await migrate_lpar(client, "lpar", LpmMigrationRequest("target"))

    message = str(exc_info.value)
    assert repr(hostile) in message
    assert not any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or ch in "\u2028\u2029" for ch in message
    )


@pytest.mark.asyncio
async def test_unreachable_destination_prevents_validation_and_migration() -> None:
    client = _client(_job("COMPLETED_OK"))
    client.list_system_summaries.side_effect = HMCError("destination unreachable")

    with pytest.raises(HMCError, match="destination unreachable"):
        await migrate_lpar(
            client,
            "00000000-0000-0000-0000-000000000002",
            LpmMigrationRequest("00000000-0000-0000-0000-000000000003"),
        )

    client.lpar_migrate_validate.assert_not_awaited()
    client.lpar_migrate.assert_not_awaited()
    client.wait_for_job_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["CANCELED_WHILE_RUNNING", "FAILED_BEFORE_COMPLETION"]
)
async def test_waited_migration_preserves_source_and_failed_job(status: str) -> None:
    client = _client(_job("COMPLETED_OK"))
    submitted = {"UUID": "migration-1", "link": "/jobs/migration-1"}
    terminal = {
        "Resource": {
            "JobID": "migration-1",
            "Status": status,
            "Results": {
                "JobParameter": {
                    "ParameterName": "detailedStatus",
                    "ParameterValue": "Partial migration requires operator recovery",
                }
            },
        }
    }
    client.lpar_migrate.return_value = submitted
    client.wait_for_job_entry.side_effect = [_job("COMPLETED_OK"), terminal]

    result = await migrate_lpar(
        client, "lpar", LpmMigrationRequest("destination"), wait=True
    )

    assert result.lpar_uuid == "lpar-1"
    assert result.job.job_id == "migration-1"
    assert result.job.status == status
    assert result.job.error == "Partial migration requires operator recovery"
    assert result.job.timed_out is False
    assert result.job.job is terminal
    assert client.wait_for_job_entry.await_args_list[-1] == call(
        "migration-1", 300, 5, job_href="/jobs/migration-1"
    )
    client.lpar_migrate_abort.assert_not_awaited()
    client.lpar_migrate_recover.assert_not_awaited()
