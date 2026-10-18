"""Focused artifact-only arm, preflight and independent recovery contracts."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_profile_artifacts as wrapper
import live_test_preflight as preflight
import live_test_recovery as recovery
from live_test import profiles, runner


def listing(*names, status="COMPLETED_OK"):
    return {
        "job": {"status": status},
        "artifacts": [{"file_name": n, "backup_time": "time"} for n in names],
    }


def test_wrapper_is_isolated_and_never_dispatches_on_help(monkeypatch):
    called = []
    monkeypatch.setattr(
        runner, "_run_from_arguments", lambda args: called.append(args) or 0
    )
    assert wrapper.main([]) == 0
    assert called == [["--group", "profile-artifacts"]]
    with pytest.raises(SystemExit) as stopped:
        wrapper.main(["--help"])
    assert stopped.value.code == 0
    with pytest.raises(SystemExit):
        wrapper.main(["--unknown"])
    assert len(called) == 1
    assert runner.SUBTASK_GROUPS[wrapper.GROUP] == [43]
    assert 43 not in runner.SUBTASK_GROUPS["all"]
    assert 43 not in runner.SUBTASK_GROUPS["round2"]
    verdict = preflight.arm_verdicts(runner.LiveTestConfig(), wrapper.GROUP)[0]
    assert "restore" in " ".join(verdict.mutates)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before",
    [
        listing(status="FAILED_TO_START"),
        {"artifacts": None},
        listing("hmcpctl-live-prof-old"),
    ],
)
async def test_incomplete_or_existing_scratch_baseline_prevents_creation(before):
    state = runner.RunState(group="profile-artifacts")
    state.call = AsyncMock(return_value=("PASS", before))
    await profiles.exercise_backup_artifacts(None, state)
    assert [c.args[1] for c in state.call.call_args_list] == [
        "hmc_list_backup_lpar_profiles"
    ]
    assert not state.observations


@pytest.mark.asyncio
async def test_artifact_arm_creates_lists_deletes_only_its_scratch(monkeypatch):
    monkeypatch.setattr(profiles.secrets, "token_hex", lambda count: "a" * 16)
    name = "hmcpctl-live-prof-" + "a" * 16
    state = runner.RunState(group="profile-artifacts")
    state.call = AsyncMock(
        side_effect=[
            ("PASS", listing()),
            ("PASS", "backup created"),
            ("PASS", listing(name)),
            ("PASS", {"postcondition": "absent"}),
        ]
    )
    await profiles.exercise_backup_artifacts(None, state)
    calls = state.call.call_args_list
    assert [c.args[1] for c in calls] == [
        "hmc_list_backup_lpar_profiles",
        "hmc_backup_lpar_profiles",
        "hmc_list_backup_lpar_profiles",
        "hmc_delete_backup_lpar_profile",
    ]
    assert calls[1].kwargs["force"] is False
    assert calls[1].kwargs["file_path"] == name == calls[3].kwargs["file_name"]
    assert all("restore" not in c.args[1] for c in calls)
    assert len(state.observations) == 2
    assert all(x["observation"]["result"] == "passed" for x in state.observations)


@pytest.mark.asyncio
async def test_ambiguous_creation_never_deletes_and_failed_delete_is_not_retried(
    monkeypatch,
):
    monkeypatch.setattr(profiles.secrets, "token_hex", lambda count: "a" * 16)
    name = "hmcpctl-live-prof-" + "a" * 16
    for responses, expected_count in [
        ([("PASS", listing()), ("FAIL", "create uncertain")], 2),
        (
            [
                ("PASS", listing()),
                ("PASS", "created"),
                ("PASS", listing(name)),
                ("PASS", {"postcondition": "unknown"}),
            ],
            4,
        ),
    ]:
        state = runner.RunState(group="profile-artifacts")
        state.call = AsyncMock(side_effect=responses)
        await profiles.exercise_backup_artifacts(None, state)
        assert state.call.await_count == expected_count
        assert not any("restore" in c.args[1] for c in state.call.call_args_list)
        assert any(x["status"] == "FAIL" for x in state.results)


@pytest.mark.asyncio
async def test_independent_recovery_reads_exact_system_and_reports_residue():
    call = AsyncMock(
        return_value=("PASS", listing("unrelated", "hmcpctl-live-prof-stranded"))
    )
    findings = await recovery.check_profile_artifacts(call, "sys-R1")
    call.assert_awaited_once_with(
        "hmc_list_backup_lpar_profiles", system_name_or_uuid="sys-R1"
    )
    assert len(findings) == 1
    assert "hmcpctl-live-prof-stranded" in findings[0].what
    assert "unrelated" not in findings[0].what
    recovery.guard_read_only(
        "hmc_list_backup_lpar_profiles", {"system_name_or_uuid": "sys-R1"}
    )
    with pytest.raises(recovery.MutatingCallRefused):
        recovery.guard_read_only("hmc_delete_backup_lpar_profile", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {"artifacts": None},
        listing(status="FAILED_TO_START"),
        {"job": {"status": "COMPLETED_OK"}, "artifacts": [1]},
    ],
)
async def test_unreadable_recovery_never_claims_clean(data):
    with pytest.raises(recovery.StateUnreadable):
        await recovery.check_profile_artifacts(
            AsyncMock(return_value=("PASS", data)), "sys-R1"
        )


@pytest.mark.asyncio
async def test_wrong_group_cannot_start_artifact_mutation():
    state = runner.RunState(group="all")
    state.call = AsyncMock(return_value=("PASS", listing()))
    await profiles.exercise_backup_artifacts(None, state)
    state.call.assert_not_awaited()
