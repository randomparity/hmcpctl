"""Finite metadata effects, durable intent and independent baseline requirements."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import runner
from live_test import system_profiles as arm


def config():
    return runner.LiveTestConfig(
        system_profile_members_json=json.dumps(
            [
                {"partition": "a", "profile": "base"},
                {"partition": "b", "profile": "base"},
            ]
        ),
        system_profile_writer_window=True,
    )


def snapshot():
    return {
        "system_uuid": "system",
        "stable": ["system", "mtms", [], {}],
        "records": [],
        "selected": [{"partition_uuid": "a"}, {"partition_uuid": "b"}],
    }


def outcome(action, name, number):
    from dataclasses import asdict

    from hmcpctl.jobs import JobOutcome
    from hmcpctl.operations.systems.profiles import SystemProfileOutcome

    cli = action in {"delete", "validate", "validate-and-activate"}
    job = (
        None
        if cli
        else JobOutcome(
            str(number),
            "COMPLETED_OK",
            False,
            None,
            None,
            True,
            f"/rest/api/uom/jobs/{number}",
        )
    )
    result = SystemProfileOutcome(
        "system",
        name,
        action,
        "ssh-command" if cli else "rest-job",
        "completed",
        job,
        None if cli else "0",
        "completed" if cli else None,
        "complete" if action in {"activate", "validate-and-activate"} else None,
        "not-applicable" if action == "validate" else "verified",
        (),
        (),
        None,
    )
    return json.loads(json.dumps(asdict(result)))


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "checkpoint", "failed", "drift"])
async def test_system_profiles_arm(monkeypatch, fault):
    state = runner.RunState(config=config(), group=arm.GROUP, hmc_config=object())
    current = snapshot()
    saved = []
    calls = []

    def checkpoint():
        saved.append(deepcopy(state.artifacts))
        if fault == "checkpoint":
            raise OSError("private destination unwritable")

    state.checkpoint = checkpoint

    async def observe(*_):
        return deepcopy(current)

    async def call(_client, tool, **args):
        if tool == "hmc_list_system_profiles":
            return "PASS", {
                "system_uuid": "system",
                "source": "ssh-command",
                "job": None,
                "command_status": "completed",
                "error": None,
                "records": deepcopy(current["records"]),
            }
        assert saved[-1].system_profile_phase.startswith("pending:")
        calls.append(tool)
        name = args["name"]
        action = tool.removeprefix("hmc_").split("_system_profile")[0]
        rows = {r["name"]: r for r in current["records"]}
        if action == "create":
            rows[name] = {
                "name": name,
                "members": current["selected"]
                if len(calls) == 1
                else current["selected"][:1],
            }
        if action == "update":
            row = rows.pop(name)
            row["name"] = args.get("new_name") or name
            if args.get("members") is not None:
                row["members"] = (
                    current["selected"]
                    if len(args["members"]) == 2
                    else current["selected"][1:2]
                )
            rows[row["name"]] = row
        if action == "delete":
            rows.pop(name)
        current["records"] = [rows[n] for n in sorted(rows)]
        current["stable"][2] = sorted(rows)
        if fault == "drift":
            current["stable"][1] = "other"
        result = outcome(action, (args.get("new_name") or name), len(calls))
        if fault == "failed":
            result["state"] = "uncertain"
        return "PASS", result

    monkeypatch.setattr(arm, "snapshot", observe)
    state.call = call
    if fault in {"checkpoint", "drift"}:
        with pytest.raises((OSError, ValueError)):
            await arm.exercise_system_profiles(None, state)
    else:
        await arm.exercise_system_profiles(None, state)
    if fault == "checkpoint":
        assert not calls
    elif fault in {"failed", "drift"}:
        assert len(calls) == 1
    else:
        assert len(calls) == 7
        assert state.artifacts.system_profile_phase == "finished"
        assert (
            await arm.reconcile(object(), state.config, state.artifacts) == snapshot()
        )
        for fault_name in ("route", "system", "job", "phase", "listing", "baseline"):
            bad = deepcopy(state.artifacts)
            if fault_name == "route":
                bad.system_profile_jobs[0]["outcome"]["backend"] = "ssh-command"
            elif fault_name == "system":
                bad.system_profile_jobs[0]["outcome"]["system_uuid"] = "foreign"
            elif fault_name == "job":
                bad.system_profile_jobs[1]["outcome"]["job"] = deepcopy(
                    bad.system_profile_jobs[0]["outcome"]["job"]
                )
            elif fault_name == "phase":
                bad.system_profile_phase = "pending:delete-d"
            elif fault_name == "listing":
                bad.system_profile_baseline["listing"]["records"] = []
            else:
                bad.system_profile_baseline["before"]["stable"][1] = "foreign"
            with pytest.raises(ValueError):
                await arm.reconcile(object(), state.config, bad)
        state.artifacts.system_profile_jobs.pop()
        with pytest.raises(ValueError):
            await arm.reconcile(object(), state.config, state.artifacts)


@pytest.mark.asyncio
async def test_system_profiles_recovery_reads_before_partial_refusal(monkeypatch):
    state = runner.RunState(config=config())
    observed = AsyncMock(return_value=snapshot())
    monkeypatch.setattr(arm, "snapshot", observed)
    with pytest.raises(ValueError):
        await arm.reconcile(object(), state.config, state.artifacts)
    observed.assert_awaited_once()


def test_system_profiles_configuration_and_dispatch():
    assert runner.SUBTASK_GROUPS[arm.GROUP] == [44]
    assert 44 not in runner.SUBTASK_GROUPS["all"] + runner.SUBTASK_GROUPS["round2"]
    with pytest.raises((ValueError, json.JSONDecodeError)):
        arm.configured(runner.LiveTestConfig())


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_system_profiles_snapshot_normal_policy(monkeypatch, foreign):
    from types import SimpleNamespace

    from hmcpctl.config import HMCConfig
    from hmcpctl.operations.systems import profiles as domain

    system = "11111111-1111-4111-8111-111111111111"
    ids = [
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    profile_ids = [
        "44444444-4444-4444-8444-444444444444",
        "55555555-5555-4555-8555-555555555555",
    ]
    native = [
        {
            "UUID": uuid,
            "Resource": {
                "PartitionID": str(i + 1),
                "PartitionName": name,
                "PartitionState": "not activated",
                "Description": "[hmcpctl owner:foreign created:2026-10-19]"
                if foreign
                else "",
            },
        }
        for i, (uuid, name) in enumerate(zip(ids, ("a", "b"), strict=True))
    ]
    hmc_config = HMCConfig.from_mapping(
        {"host": "hmc.test", "user": "test", "agent_id": "test"}
    )
    hmc = SimpleNamespace(
        config=hmc_config,
        get_managed_system=AsyncMock(
            return_value={
                "UUID": system,
                "Resource": {
                    "MachineTypeModelAndSerialNumber": {
                        "MachineType": "9009",
                        "Model": "42A",
                        "SerialNumber": "TEST",
                    }
                },
            }
        ),
        list_logical_partitions=AsyncMock(return_value=native),
        list_vios=AsyncMock(return_value=[]),
    )

    async def profiles(kind, uuid, child):
        return [
            {
                "UUID": profile_ids[ids.index(uuid)],
                "Resource": {
                    "ProfileName": "base",
                    "AssociatedPartition": {
                        "link": {
                            "@attrs": {
                                "href": f"https://hmc.test/rest/api/uom/LogicalPartition/{uuid}"
                            }
                        }
                    },
                },
            }
        ]

    hmc.list_child = profiles

    class Session:
        def __init__(self, cfg):
            assert "port" in cfg.model_fields_set

        async def __aenter__(self):
            return hmc

        async def __aexit__(self, *_):
            return False

    monkeypatch.setattr(arm, "HMCClient", Session)
    monkeypatch.setattr(arm, "resolve_system_uuid", AsyncMock(return_value=system))
    monkeypatch.setattr(
        domain, "run_hmc_command", AsyncMock(return_value="No results were found.")
    )
    monkeypatch.setattr(
        "hmcpctl.ssh.profiles.run_hmc_command",
        AsyncMock(return_value="No results were found."),
    )
    if foreign:
        with pytest.raises(PermissionError):
            await arm.snapshot(hmc_config, config())
    else:
        actual = await arm.snapshot(hmc_config, config())
        assert [m["partition_uuid"] for m in actual["selected"]] == ids
        assert hmc.list_logical_partitions.await_count == 2
