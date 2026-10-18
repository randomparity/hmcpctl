"""Ownership inference for template-deployed LPARs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree.ElementTree import ParseError

import httpx
import pytest

from hmcpctl.errors import HMCError, HMCTransportError
from hmcpctl.operations.templates import core as operations_templates
from hmcpctl.operations.templates.core import (
    _new_lpar_from_snapshots,
    deploy_partition_template,
)

SYSTEM_UUID = "system-uuid"
RUNNING_JOB = {"Resource": {"Status": "RUNNING"}}
COMPLETED_JOB = {"Resource": {"Status": "COMPLETED_OK"}}


def _lpar(uuid: object, name: str) -> dict:
    return {"UUID": uuid, "Resource": {"PartitionName": name}}


def test_snapshot_diff_returns_the_only_new_lpar():
    before = [_lpar("old-uuid", "old")]
    created = _lpar("new-uuid", "new")

    candidate, warning = _new_lpar_from_snapshots(before, [*before, created])

    assert candidate == created
    assert warning is None


@pytest.mark.parametrize(
    ("after", "warning_fragment"),
    [
        ([_lpar("old-uuid", "old")], "no new LPAR"),
        (
            [_lpar("old-uuid", "old"), _lpar("new-1", "one"), _lpar("new-2", "two")],
            "2 new LPARs",
        ),
    ],
)
def test_snapshot_diff_rejects_non_unique_candidates(after, warning_fragment):
    candidate, warning = _new_lpar_from_snapshots([_lpar("old-uuid", "old")], after)

    assert candidate is None
    assert warning is not None
    assert warning_fragment in warning


@pytest.mark.parametrize("bad_uuid", [None, "", "  ", 7])
def test_snapshot_diff_rejects_malformed_uuid_in_either_snapshot(bad_uuid):
    valid_old = _lpar("old-uuid", "old")
    valid_new = _lpar("new-uuid", "new")

    for before, after in (
        ([_lpar(bad_uuid, "bad")], [valid_new]),
        ([valid_old], [valid_old, valid_new, _lpar(bad_uuid, "bad")]),
    ):
        candidate, warning = _new_lpar_from_snapshots(before, after)

        assert candidate is None
        assert warning is not None
        assert "missing a usable UUID" in warning


def _client(*, snapshots=()):
    return SimpleNamespace(
        config=SimpleNamespace(agent_id="alice"),
        list_logical_partitions=AsyncMock(side_effect=snapshots),
        deploy_partition_template=AsyncMock(return_value=RUNNING_JOB),
    )


@pytest.mark.asyncio
async def test_waited_deploy_stamps_after_ordered_snapshots(monkeypatch):
    old = _lpar("old-uuid", "old")
    created = _lpar("new-uuid", "new")
    events: list[str] = []
    hmc = _client()

    async def list_lpars(_system_uuid):
        events.append("baseline" if "submit" not in events else "post")
        return [old] if events[-1] == "baseline" else [old, created]

    async def submit(*_args):
        events.append("submit")
        return RUNNING_JOB

    async def wait(*_args):
        events.append("wait")
        return COMPLETED_JOB

    async def stamp(*args):
        events.append("stamp")
        assert args[1:] == (SYSTEM_UUID, "system-name", created)
        return True, []

    hmc.list_logical_partitions.side_effect = list_lpars
    hmc.deploy_partition_template.side_effect = submit
    monkeypatch.setattr(
        operations_templates, "resolve_system_uuid", AsyncMock(return_value=SYSTEM_UUID)
    )
    monkeypatch.setattr(operations_templates, "wait_for_submitted_job", wait)
    monkeypatch.setattr(operations_templates, "stamp_created_lpar_ownership", stamp)

    result = await deploy_partition_template(
        hmc,
        "draft-uuid",
        "system-name",
        wait=True,
        timeout_seconds=60,
        poll_interval=1,
    )

    assert events == ["baseline", "submit", "wait", "post", "stamp"]
    assert result == {
        "job": COMPLETED_JOB,
        "ownership_stamped": True,
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_waited_deploy_reports_stamp_failure(monkeypatch):
    old = _lpar("old-uuid", "old")
    created = _lpar("new-uuid", "new")
    hmc = _client(snapshots=([old], [old, created]))
    stamp = AsyncMock(return_value=(False, ["ownership stamp failed for LPAR 'new'"]))
    monkeypatch.setattr(
        operations_templates, "resolve_system_uuid", AsyncMock(return_value=SYSTEM_UUID)
    )
    monkeypatch.setattr(
        operations_templates,
        "wait_for_submitted_job",
        AsyncMock(return_value=COMPLETED_JOB),
    )
    monkeypatch.setattr(operations_templates, "stamp_created_lpar_ownership", stamp)

    result = await deploy_partition_template(
        hmc,
        "draft-uuid",
        "system-name",
        wait=True,
        timeout_seconds=60,
        poll_interval=1,
    )

    assert result["ownership_stamped"] is False
    assert result["warnings"] == ["ownership stamp failed for LPAR 'new'"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_snapshot", ["baseline", "post"])
@pytest.mark.parametrize(
    ("error_type", "cause_type", "status"),
    [
        (HMCTransportError, httpx.ReadTimeout, None),
        (HMCTransportError, httpx.ConnectError, None),
        (HMCError, ParseError, None),
        (HMCError, None, 500),
    ],
)
async def test_waited_deploy_keeps_success_when_snapshot_fails(
    monkeypatch, caplog, failed_snapshot, error_type, cause_type, status
):
    old = _lpar("old-uuid", "old")
    sensitive_body = "SENSITIVE-HMC-RESPONSE-BODY"
    sensitive_message = "SENSITIVE-ERROR-MESSAGE"
    sensitive_cause = "SENSITIVE-CAUSE-MESSAGE"
    snapshot_error = error_type(sensitive_message, status, sensitive_body)
    if cause_type is not None:
        snapshot_error.__cause__ = cause_type(sensitive_cause)
    snapshots = (
        [snapshot_error] if failed_snapshot == "baseline" else [[old], snapshot_error]
    )
    hmc = _client(snapshots=snapshots)
    stamp = AsyncMock()
    monkeypatch.setattr(
        operations_templates, "resolve_system_uuid", AsyncMock(return_value=SYSTEM_UUID)
    )
    monkeypatch.setattr(
        operations_templates,
        "wait_for_submitted_job",
        AsyncMock(return_value=COMPLETED_JOB),
    )
    monkeypatch.setattr(operations_templates, "stamp_created_lpar_ownership", stamp)

    result = await deploy_partition_template(
        hmc,
        "draft-uuid",
        "system-name",
        wait=True,
        timeout_seconds=60,
        poll_interval=1,
    )

    expected_warning = (
        "ownership stamp not attempted: baseline LPAR snapshot is unavailable"
        if failed_snapshot == "baseline"
        else "ownership stamp not attempted: post-deployment LPAR snapshot is unavailable"
    )
    assert result == {
        "job": COMPLETED_JOB,
        "ownership_stamped": None,
        "warnings": [expected_warning],
    }
    hmc.deploy_partition_template.assert_awaited_once_with("draft-uuid", SYSTEM_UUID)
    assert hmc.list_logical_partitions.await_count == (
        1 if failed_snapshot == "baseline" else 2
    )
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert SYSTEM_UUID in record.getMessage()
    assert (
        "baseline" if failed_snapshot == "baseline" else "post-deployment"
    ) in record.message
    assert f"HTTP status {status}" in record.message
    assert f"exception {error_type.__name__}" in record.message
    assert f"cause {cause_type.__name__ if cause_type else None}" in record.message
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    for sensitive in (sensitive_body, sensitive_message, sensitive_cause):
        assert sensitive not in caplog.text
        assert sensitive not in repr(record.__dict__)
    stamp.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_completed_waited_deploy_does_not_post_list_or_stamp(monkeypatch):
    hmc = _client(snapshots=([_lpar("old-uuid", "old")],))
    stamp = AsyncMock()
    monkeypatch.setattr(
        operations_templates, "resolve_system_uuid", AsyncMock(return_value=SYSTEM_UUID)
    )
    monkeypatch.setattr(
        operations_templates,
        "wait_for_submitted_job",
        # A documented, captured failed status (016-job-status.md:16-27).
        AsyncMock(return_value={"Resource": {"Status": "COMPLETED_WITH_ERROR"}}),
    )
    monkeypatch.setattr(operations_templates, "stamp_created_lpar_ownership", stamp)

    result = await deploy_partition_template(
        hmc,
        "draft-uuid",
        "system-name",
        wait=True,
        timeout_seconds=60,
        poll_interval=1,
    )

    assert hmc.list_logical_partitions.await_count == 1
    assert result["ownership_stamped"] is None
    assert "COMPLETED_WITH_ERROR" in result["warnings"][0]
    stamp.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_waited_deploy_does_not_list_or_stamp(monkeypatch):
    hmc = _client()
    stamp = AsyncMock()
    monkeypatch.setattr(
        operations_templates, "resolve_system_uuid", AsyncMock(return_value=SYSTEM_UUID)
    )
    monkeypatch.setattr(operations_templates, "stamp_created_lpar_ownership", stamp)

    result = await deploy_partition_template(
        hmc,
        "draft-uuid",
        "system-name",
        wait=False,
        timeout_seconds=60,
        poll_interval=1,
    )

    hmc.list_logical_partitions.assert_not_awaited()
    assert result["ownership_stamped"] is None
    stamp.assert_not_awaited()
