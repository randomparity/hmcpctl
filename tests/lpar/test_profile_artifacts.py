"""Backup artifact identity, bounded results and deletion postconditions."""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock

import pytest

from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.profile_artifacts import (
    delete_backup_profile_artifact,
    list_backup_profile_artifacts,
)

pytestmark = pytest.mark.asyncio

SYSTEM = "22222222-2222-4222-8222-222222222222"


def job(files=None, *, status="COMPLETED_OK", result=None, **resource):
    value = json.dumps(files if files is not None else []) if result is None else result
    return {
        "Resource": {
            "JobID": "job-1",
            "Status": status,
            "Results": {
                "JobParameter": [{"ParameterName": "result", "ParameterValue": value}]
            },
            **resource,
        }
    }


def client(*jobs):
    hmc = MagicMock()
    hmc.submit_job = AsyncMock(side_effect=jobs)
    hmc.wait_for_job_entry = AsyncMock(side_effect=jobs)
    return hmc


def artifact(name="backup-1"):
    return {"BackupFileName": name, "BackupTime": "opaque HMC time"}


async def test_native_json_inventory_and_projected_job():
    hmc = client(job([artifact()], extra="x" * 2_000_000))
    result = await list_backup_profile_artifacts(hmc, SYSTEM)
    assert result.system_uuid == SYSTEM
    assert result.artifacts[0].file_name == "backup-1"
    assert result.artifacts[0].backup_time == "opaque HMC time"
    assert result.job.job is None
    assert len(json.dumps(asdict(result))) < 1000
    path, xml = hmc.submit_job.call_args.args
    assert path == f"/rest/api/uom/ManagedSystem/{SYSTEM}/do/ListBackupProfileData"
    assert "<OperationName" in xml and ">ListBackupProfileData<" in xml
    assert ">ManagedSystem<" in xml
    assert "<JobParameter " not in xml


@pytest.mark.parametrize(
    "status", ["COMPLETED_WITH_WARNINGS", "FAILED_BEFORE_COMPLETION", "RUNNING"]
)
async def test_unavailable_inventory_is_not_empty_and_diagnostics_are_bounded(status):
    hmc = client(job(status=status, result="x" * 2_000_000))
    result = await list_backup_profile_artifacts(hmc, SYSTEM)
    assert result.artifacts is None
    assert result.job.status == status
    assert result.job.job is None
    assert len(json.dumps(asdict(result))) < 10_000
    if result.job.error:
        assert result.job.error.endswith("[truncated]")
        assert len(result.job.error) <= 2048


@pytest.mark.parametrize(
    "result",
    [
        "no results",
        "{}",
        "null",
        "[1]",
        '[{"BackupFileName":1,"BackupTime":"t"}]',
        '[{"BackupFileName":"a"}]',
        '[{"BackupFileName":"a","BackupTime":null}]',
        '[{"BackupFileName":"a","BackupTime":"t"},{"BackupFileName":"a","BackupTime":"t"}]',
        '[{"BackupFileName":"a","BackupFileName":"b","BackupTime":"t"}]',
        "x" * (1048576 + 1),
    ],
    ids=lambda value: str(value)[:40],
)
async def test_malformed_or_oversized_inventory_fails_closed(result):
    with pytest.raises(HMCError, match="inventory"):
        await list_backup_profile_artifacts(client(job(result=result)), SYSTEM)


async def test_empty_inventory_and_entry_limit():
    result = await list_backup_profile_artifacts(client(job()), SYSTEM)
    assert result.artifacts == ()
    with pytest.raises(HMCError, match="inventory"):
        await list_backup_profile_artifacts(
            client(job([artifact(str(n)) for n in range(1001)])), SYSTEM
        )


@pytest.mark.parametrize(
    "results",
    [
        None,
        {},
        {"JobParameter": []},
        {"JobParameter": [{"ParameterName": "Result", "ParameterValue": "[]"}]},
        {"JobParameter": [{"ParameterName": "result", "ParameterValue": "[]"}] * 2},
    ],
)
async def test_missing_or_duplicate_result_does_not_admit_deletion(results):
    hmc = client(job(Results=results))
    with pytest.raises(HMCError, match="inventory"):
        await delete_backup_profile_artifact(hmc, SYSTEM, "backup-1")
    assert hmc.submit_job.await_count == 1


@pytest.mark.parametrize(
    "name",
    ["", " ", " a", "a ", ".", "..", "-a", "a/b", "a\\b", "a\n", "a\x00", "a\x7f"],
)
async def test_invalid_file_refused_before_any_hmc_call(name):
    hmc = client()
    with pytest.raises(ValueError, match="file_name"):
        await delete_backup_profile_artifact(hmc, SYSTEM, name)
    hmc.submit_job.assert_not_awaited()


async def test_missing_file_or_failed_prelist_never_deletes():
    for before in (job(), job(status="FAILED_TO_START")):
        hmc = client(before)
        with pytest.raises(HMCError):
            await delete_backup_profile_artifact(hmc, SYSTEM, "backup-1")
        assert hmc.submit_job.await_count == 1


@pytest.mark.parametrize(
    "after,postcondition",
    [
        (job(), "absent"),
        (job([artifact()]), "present"),
        (job(status="COMPLETED_WITH_WARNINGS"), "unknown"),
    ],
)
async def test_delete_checks_exact_identity_and_observes_readback(after, postcondition):
    hmc = client(job([artifact(), artifact("other")]), job(), after)
    result = await delete_backup_profile_artifact(hmc, SYSTEM, "backup-1")
    assert result.postcondition == postcondition
    assert result.job.job is None
    calls = hmc.submit_job.call_args_list
    assert len(calls) == 3
    assert calls[1].args[0].endswith("/do/DeleteBackupProfileData")
    assert ">file<" in calls[1].args[1] and ">backup-1<" in calls[1].args[1]
    assert all(f"/ManagedSystem/{SYSTEM}/" in c.args[0] for c in calls)


async def test_delete_failure_is_not_retried_or_reported_absent():
    hmc = client(job([artifact()]), job(status="FAILED_BEFORE_COMPLETION"))
    result = await delete_backup_profile_artifact(hmc, SYSTEM, "backup-1")
    assert result.postcondition == "not-checked"
    assert result.job.status == "FAILED_BEFORE_COMPLETION"
    assert hmc.submit_job.await_count == 2


async def test_readback_error_retains_original_job_and_bounded_uncertainty():
    hmc = client(job([artifact()]), job(), HMCError("x" * 10_000))
    result = await delete_backup_profile_artifact(hmc, SYSTEM, "backup-1")
    assert result.postcondition == "unknown"
    assert result.job.status == "COMPLETED_OK"
    assert result.postcondition_error.endswith("[truncated]")
    assert len(result.postcondition_error) <= 2048
    assert hmc.submit_job.await_count == 3


async def test_no_submission_handle_does_not_imply_success():
    hmc = client(None)
    with pytest.raises(HMCError, match="job"):
        await list_backup_profile_artifacts(hmc, SYSTEM)


@pytest.mark.parametrize("selector", ["", " "])
async def test_empty_system_refused_before_inventory(selector):
    hmc = client()
    with pytest.raises(ValueError, match="system"):
        await list_backup_profile_artifacts(hmc, selector)
    hmc.list_system_summaries.assert_not_called()


async def test_oversized_job_handle_refused_before_polling():
    hmc = client(job(JobID="j" * 4097))
    with pytest.raises(HMCError, match="identity exceeds"):
        await list_backup_profile_artifacts(hmc, SYSTEM)
    hmc.wait_for_job_entry.assert_not_awaited()


async def test_file_identity_is_xml_escaped_without_changing_its_matching_name():
    name = 'backup<&"'
    hmc = client(job([artifact(name)]), job(), job())
    result = await delete_backup_profile_artifact(hmc, SYSTEM, name)
    assert result.file_name == name and result.postcondition == "absent"
    xml = hmc.submit_job.call_args_list[1].args[1]
    assert "backup&lt;&amp;&quot;" in xml


async def test_missing_poll_preserves_the_submission_polling_link():
    submitted = {
        "UUID": "entry-id",
        "link": "/rest/api/uom/jobs/stable-job",
        "Resource": {"Status": "RUNNING"},
    }
    hmc = client(submitted)
    hmc.wait_for_job_entry = AsyncMock(return_value=None)
    result = await list_backup_profile_artifacts(hmc, SYSTEM)
    assert result.artifacts is None and result.job.found is False
    assert result.job.job_href == "/rest/api/uom/jobs/stable-job"


async def test_missing_delete_poll_retains_handle_without_retry():
    submitted = {
        "UUID": "entry-id",
        "link": "/rest/api/uom/jobs/stable-job",
        "Resource": {"Status": "RUNNING"},
    }
    before = job([artifact()])
    hmc = client(before, submitted)
    hmc.wait_for_job_entry = AsyncMock(side_effect=[before, None])
    result = await delete_backup_profile_artifact(hmc, SYSTEM, "backup-1")
    assert result.postcondition == "not-checked"
    assert result.job.job_href == "/rest/api/uom/jobs/stable-job"
    assert hmc.submit_job.await_count == 2
