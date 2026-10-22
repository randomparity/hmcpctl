"""Named lifecycle exercise, durable intent and independent read-only recovery proof."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from lpar.test_partition_profile_operations import F, G, NativeService, P, S
from lpar.test_partition_profiles import field

from hmcpctl.documents.partition_profile import ProfilePatch
from hmcpctl.operations.lpar import profiles as domain

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import partition_profiles as arm
from live_test import runner


def config():
    return runner.LiveTestConfig(
        system_name=S,
        partition_profile_target=P,
        partition_profile_kind="LogicalPartition",
        partition_profile_writer_window=True,
        partition_profile_memory_mib=512,
    )


@pytest.fixture(params=["LogicalPartition", "VirtualIOServer"])
def environment(monkeypatch, request):
    service = NativeService(request.param)
    service.parent["PartitionState"] = "Not Activated"
    service.hmc.__aenter__.return_value = service.hmc
    service.hmc.__aexit__.return_value = False
    monkeypatch.setattr(arm, "HMCClient", lambda *args, **kwargs: service.hmc)
    monkeypatch.setattr(
        domain, "run_hmc_command", AsyncMock(return_value="No results were found.\n")
    )
    state = runner.RunState(
        config=replace(config(), partition_profile_kind=request.param),
        group=arm.GROUP,
        hmc_config=service.hmc.config,
    )
    saved = []
    state.checkpoint = lambda: saved.append(deepcopy(asdict(state.artifacts)))

    async def call(_client, tool, **args):
        functions = {
            "hmc_list_partition_profiles": domain.list_partition_profiles,
            "hmc_read_partition_profile": domain.read_partition_profile,
            "hmc_update_partition_profile": domain.update_partition_profile,
            "hmc_save_current_partition_profile": domain.save_current_partition_profile,
            "hmc_set_default_partition_profile": domain.set_default_partition_profile,
            "hmc_delete_partition_profile": domain.delete_partition_profile,
        }
        tool = tool.replace("vios_partition_profile", "partition_profile")
        if "vios_name_or_uuid" in args:
            args["lpar_name_or_uuid"] = args.pop("vios_name_or_uuid")
            args["partition_type"] = "VirtualIOServer"
        args.pop("profile", None)
        if "patch" in args:
            args["patch"] = ProfilePatch.model_validate(args["patch"])
        try:
            result = await functions[tool](service.hmc, **args)
            return "PASS", asdict(result)
        except (ValueError, TypeError, PermissionError) as error:
            return "FAIL", str(error)

    state.call = call
    return service, state, saved


def document(state):
    return json.loads(
        json.dumps(
            {
                "run": {
                    "group": arm.GROUP,
                    "subtasks": [arm.SUBTASK],
                    "partial": False,
                },
                "hmc": runner._hmc_identity(state.hmc_config),
                "config": asdict(state.config),
                "artifacts": asdict(state.artifacts),
            }
        )
    )


@pytest.mark.asyncio
async def test_finite_profile_arm_restores_originals_and_independent_proof_requeries(
    environment,
):
    service, state, saved = environment
    await arm.exercise_partition_profiles(None, state)
    journal = state.artifacts.partition_profile_lifecycle
    assert journal["phase"] == "complete"
    assert [s["stage"] for s in journal["steps"]] == list(arm.STAGES)
    assert (
        set(service.docs) == {F, G} and service.parent["DefaultProfileName"] == "other"
    )
    assert (
        len([c for c in service.calls if c[0] in {"POST", "PUT", "DELETE", "JOB"}]) == 8
    )
    pending = [v["partition_profile_lifecycle"]["phase"] for v in saved]
    for stage in arm.STAGES:
        assert "pending:" + stage in pending
    assert len(state.observations) == 6
    service.hmc.get_job_entry.reset_mock()
    await arm.recover_document(state.hmc_config, document(state))
    assert service.hmc.get_job_entry.await_count == 4


@pytest.mark.asyncio
async def test_failed_patch_stops_without_cleanup_or_later_mutation(environment):
    service, state, _ = environment
    service.post_status = 500
    await arm.exercise_partition_profiles(None, state)
    journal = state.artifacts.partition_profile_lifecycle
    assert journal["phase"] != "complete"
    assert len([c for c in service.calls if c[0] == "JOB"]) == 1
    assert not [c for c in service.calls if c[0] in {"PUT", "DELETE"}]
    with pytest.raises(ValueError):
        await arm.recover_document(state.hmc_config, document(state))
    assert not state.observations


@pytest.mark.asyncio
async def test_checkpoint_failure_prevents_first_effect(environment):
    service, state, _ = environment
    state.checkpoint = lambda: (_ for _ in ()).throw(OSError("checkpoint refused"))
    with pytest.raises(OSError):
        await arm.exercise_partition_profiles(None, state)
    assert not [c for c in service.calls if c[0] != "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["original", "job", "partial", "identity"])
async def test_independent_proof_does_not_trust_saved_success_flags(environment, fault):
    service, state, _ = environment
    await arm.exercise_partition_profiles(None, state)
    doc = document(state)
    if fault == "original":
        field(service.docs[F], "ProfileMemory/DesiredMemory").text = "999"
    elif fault == "job":
        next(iter(service.jobs.values()))["Resource"]["Status"] = "RUNNING"
    elif fault == "partial":
        doc["run"]["partial"] = True
    else:
        doc["hmc"] = {"host": "different-target"}
    with pytest.raises((ValueError, TypeError)):
        await arm.recover_document(state.hmc_config, doc)


@pytest.mark.asyncio
async def test_arm_requires_explicit_group_config_and_checkpoint_before_io(environment):
    service, state, _ = environment
    state.group = "all"
    await arm.exercise_partition_profiles(None, state)
    assert not service.calls
    state.group = arm.GROUP
    state.checkpoint = None
    with pytest.raises(ValueError):
        await arm.exercise_partition_profiles(None, state)
    assert not service.calls
    with pytest.raises(ValueError):
        arm.configured(runner.LiveTestConfig())


@pytest.mark.asyncio
async def test_arm_refuses_noop_patch_and_retains_created_profile(environment):
    service, state, _ = environment
    state.config = replace(state.config, partition_profile_memory_mib=1024)
    with pytest.raises(ValueError, match="no-op"):
        await arm.exercise_partition_profiles(None, state)
    assert len([c for c in service.calls if c[0] == "JOB"]) == 1
    assert not [c for c in service.calls if c[0] in {"POST", "PUT", "DELETE"}]
    assert not state.observations


@pytest.mark.asyncio
async def test_arm_refuses_foreign_product_list_before_effects(environment):
    service, state, _ = environment
    state.call = AsyncMock(return_value=("PASS", {"partition_uuid": "foreign"}))
    with pytest.raises(ValueError, match="identity differs"):
        await arm.exercise_partition_profiles(None, state)
    assert not [c for c in service.calls if c[0] != "GET"]
