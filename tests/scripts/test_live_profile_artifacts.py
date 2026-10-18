"""Focused artifact-only arm, preflight and independent recovery contracts."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
import live_profile_artifacts as wrapper
import live_test_preflight as preflight
import live_test_recovery as recovery
from live_test import config as live_config
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
@pytest.mark.parametrize("suffix_hex_length", [12, 16])
async def test_incomplete_or_existing_scratch_baseline_prevents_creation(
    before, suffix_hex_length
):
    state = runner.RunState(
        group="profile-artifacts",
        config=runner.LiveTestConfig(
            profile_artifact_suffix_hex_length=suffix_hex_length
        ),
    )
    state.call = AsyncMock(return_value=("PASS", before))
    await profiles.exercise_backup_artifacts(None, state)
    assert [c.args[1] for c in state.call.call_args_list] == [
        "hmc_list_backup_lpar_profiles"
    ]
    assert not state.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix_hex_length", [12, 16])
async def test_artifact_arm_creates_lists_deletes_only_its_scratch(
    monkeypatch, suffix_hex_length
):
    requested_bytes = []

    def token_hex(count):
        requested_bytes.append(count)
        return "a" * (count * 2)

    monkeypatch.setattr(profiles.secrets, "token_hex", token_hex)
    name = "hmcpctl-live-prof-" + "a" * suffix_hex_length
    state = runner.RunState(
        group="profile-artifacts",
        config=runner.LiveTestConfig(
            profile_artifact_suffix_hex_length=suffix_hex_length
        ),
    )
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
    assert requested_bytes == [suffix_hex_length // 2]
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
@pytest.mark.parametrize("suffix_hex_length", [12, 16])
async def test_ambiguous_creation_never_deletes_and_failed_delete_is_not_retried(
    monkeypatch, suffix_hex_length
):
    monkeypatch.setattr(profiles.secrets, "token_hex", lambda count: "a" * (count * 2))
    name = "hmcpctl-live-prof-" + "a" * suffix_hex_length
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
        state = runner.RunState(
            group="profile-artifacts",
            config=runner.LiveTestConfig(
                profile_artifact_suffix_hex_length=suffix_hex_length
            ),
        )
        state.call = AsyncMock(side_effect=responses)
        await profiles.exercise_backup_artifacts(None, state)
        assert state.call.await_count == expected_count
        assert not any("restore" in c.args[1] for c in state.call.call_args_list)
        assert any(x["status"] == "FAIL" for x in state.results)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix_hex_length", [12, 16])
async def test_independent_recovery_reads_exact_system_and_reports_residue(
    suffix_hex_length,
):
    name = "hmcpctl-live-prof-" + "a" * suffix_hex_length
    call = AsyncMock(return_value=("PASS", listing("unrelated", name)))
    findings = await recovery.check_profile_artifacts(call, "sys-R1")
    call.assert_awaited_once_with(
        "hmc_list_backup_lpar_profiles", system_name_or_uuid="sys-R1"
    )
    assert len(findings) == 1
    assert name in findings[0].what
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


def test_null_artifact_system_is_unreadable_in_main(tmp_path, monkeypatch, capsys):
    path = tmp_path / "results.json"
    path.write_text(
        json.dumps(
            {
                "run": {
                    "subtasks": [43],
                    "group": "profile-artifacts",
                    "partial": False,
                },
                "config": {"system_name": None},
                "results": [],
                "artifacts": {},
            }
        ),
        encoding="utf-8",
    )
    call = AsyncMock()

    async def run_checks(checks, *, users=False):
        return await recovery.check_run(call, checks, users=users)

    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    monkeypatch.setattr(recovery, "_run_checks", run_checks)

    assert recovery.main(["--results", str(path)]) == 2
    captured = capsys.readouterr()
    assert "CLEAN" not in captured.out
    assert "profile-artifacts managed-system identity" in captured.out
    assert "NOT confirmed clean" in captured.err
    call.assert_not_awaited()


@pytest.fixture
def artifact_env(tmp_path):
    path = tmp_path / ".env"
    path.write_text((Path(__file__).parents[2] / ".env.example").read_text())
    return path


@pytest.mark.parametrize("value,expected", [(None, 16), ("12", 12), ("16", 16)])
def test_artifact_suffix_is_optional_dotenv_only_and_printed_in_preflight(
    artifact_env, monkeypatch, value, expected
):
    # This setting name matches the IAM-key detector pattern.
    key = "LIVE_TEST_PROFILE_ARTIFACT_SUFFIX_HEX_LENGTH"  # pragma: allowlist secret
    monkeypatch.setenv(key, "0")
    if value is not None:
        with artifact_env.open("a") as stream:
            stream.write(f"\n{key}={value}\n")
    config = live_config.LiveTestConfig.from_env_file(artifact_env)
    assert config.profile_artifact_suffix_hex_length == expected
    assert asdict(config)["profile_artifact_suffix_hex_length"] == expected
    verdict = preflight.arm_verdicts(config, "profile-artifacts")[0]
    details = " ".join(verdict.mutates)
    assert f"<{expected} hex digits>" in details
    assert f"({18 + expected} characters)" in details


@pytest.mark.parametrize("value", ["", "0", "8", "14", "32", "12.0", "True", "12x"])
def test_artifact_suffix_rejects_other_configuration_values(artifact_env, value):
    with artifact_env.open("a") as stream:
        stream.write(f"\nLIVE_TEST_PROFILE_ARTIFACT_SUFFIX_HEX_LENGTH={value}\n")
    with pytest.raises(
        ValueError, match="LIVE_TEST_PROFILE_ARTIFACT_SUFFIX_HEX_LENGTH"
    ):
        live_config.LiveTestConfig.from_env_file(artifact_env)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, 14, True, "12", None])
async def test_invalid_direct_artifact_suffix_cannot_contact_hmc(value):
    state = runner.RunState(
        group="profile-artifacts",
        config=runner.LiveTestConfig(profile_artifact_suffix_hex_length=value),
    )
    state.call = AsyncMock()
    with pytest.raises(ValueError, match="suffix.*12 or 16"):
        await profiles.exercise_backup_artifacts(None, state)
    state.call.assert_not_awaited()


def test_old_saved_config_defaults_artifact_suffix_to_sixteen():
    saved = json.loads(json.dumps(asdict(live_config.LiveTestConfig())))
    saved.pop("profile_artifact_suffix_hex_length", None)
    assert (
        live_config.decode_saved_config(saved).profile_artifact_suffix_hex_length == 16
    )


@pytest.mark.parametrize("value", [12, 16])
def test_saved_artifact_suffix_round_trips(value):
    config = live_config.LiveTestConfig(profile_artifact_suffix_hex_length=value)
    assert (
        live_config.decode_saved_config(json.loads(json.dumps(asdict(config))))
        == config
    )


@pytest.mark.parametrize("value", [True, 12.0, "12", None, 14])
def test_saved_artifact_suffix_rejects_wrong_type_or_value(value):
    saved = json.loads(json.dumps(asdict(live_config.LiveTestConfig())))
    saved["profile_artifact_suffix_hex_length"] = value
    with pytest.raises(
        (TypeError, ValueError), match="profile_artifact_suffix_hex_length"
    ):
        live_config.decode_saved_config(saved)
