"""The lpar-config live arm (ST39, #1345) against a scripted HMC."""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

_RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "live_test_runner.py"
sys.path.insert(0, str(_RUNNER_PATH.parent))
from live_test import config as live_config  # noqa: E402
from live_test import (  # noqa: E402
    evidence,
    lpar_config,
    runner,
)
from live_test.observation import CallFailure  # noqa: E402

SYSTEM = "sys-A"
OTHER = "lpar-A"
SCRATCH_UUID = "0A1B2C3D-0000-4000-8000-000000000039"
PROFILE_UUID = "0A1B2C3D-0000-4000-8000-0000000000F1"
REGION = 256
MUTATIONS = frozenset(
    {
        "hmc_modify_lpar",
        "hmc_dlpar_mem",
        "hmc_dlpar_proc",
        "hmc_rename_lpar",
        "hmc_set_lpar_boot_order",
        "hmc_clear_lpar_boot_order",
        "hmc_power_on_lpar",
        "hmc_power_off_lpar",
        "hmc_delete_lpar",
    }
)
_MEMORY = {"min_memory": "MinimumMemory", "desired_memory": "DesiredMemory"}
_MEMORY |= {"max_memory": "MaximumMemory"}
_SHARED = {
    "desired_procs": "DesiredProcessingUnits",
    "max_procs": "MaximumProcessingUnits",
    "desired_vcpus": "DesiredVirtualProcessors",
    "max_vcpus": "MaximumVirtualProcessors",
}


def _failure(message: str, status: int | None = None) -> CallFailure:
    return CallFailure("HMCError", message, "", status, False)


@dataclass
class FakeHMC:
    """One managed system holding OTHER and, once created, the scratch partition."""

    existing_scratch: bool = False
    ignore_memory_write: bool = False
    modify_406: bool = False
    foreign_token: bool = False
    pool_drift: bool = False
    pool_lag: bool = False
    activated_dlpar_refused: bool = True
    never_activates: bool = False
    create_without_uuid: bool = False
    power_off_refused: bool = False
    stale_absence: bool = False
    delete_refused: bool = False
    activated_failure: str = "HSCL294C no RMC connection"
    set_refused: bool = False
    rename_back_lies: bool = False
    renames: int = 0
    name: str | None = None
    token: str | None = None
    values: dict[str, float] = field(default_factory=dict)
    state: str = "Not Activated"
    pending: str | None = None
    deleted: bool = False
    pool_reads: int = 0
    stale_served: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def answer(self, tool: str, kwargs: dict[str, Any]) -> tuple[str, Any]:
        self.calls.append((tool, kwargs))
        return getattr(self, "_" + tool)(kwargs)

    def exists(self) -> bool:
        return self.name is not None and not self.deleted

    def is_scratch(self, selector: object) -> bool:
        return self.exists() and selector in {SCRATCH_UUID, self.name}

    def _hmc_run_command(self, kwargs):
        cmd = kwargs["cmd"]
        if cmd.startswith("lssyscfg"):
            names = [OTHER] + (
                ["hmcpctl-live-lpar-00000000"] if self.existing_scratch else []
            )
            names += [self.name] if self.exists() else []
            return "PASS", "\n".join(names) + "\n"
        self.pool_reads += 1 if "proc" in cmd else 0
        drift = self.deleted and (
            self.pool_drift or (self.pool_lag and self.pool_reads == 2)
        )
        if "proc" in cmd:
            return "PASS", "7.5\n" if drift else "8.0\n"
        return "PASS", f"100000,{REGION}\n"

    def _hmc_create_lpar(self, kwargs):
        self.name, self.token = kwargs["name"], kwargs["caller_token"]
        resources = kwargs["resources"]
        self.values = {key: float(resources[key]) for key in _MEMORY | _SHARED}
        lpar = {} if self.create_without_uuid else {"UUID": SCRATCH_UUID}
        return "PASS", {"lpar": lpar, "steps": []}

    def _resource(self) -> dict[str, Any]:
        memory = {_MEMORY[k]: str(self.values[k]) for k in _MEMORY}
        shared = {_SHARED[k]: str(self.values[k]) for k in _SHARED}
        return {
            "PartitionMemoryConfiguration": memory,
            "PartitionProcessorConfiguration": {"SharedProcessorConfiguration": shared},
            "AssociatedPartitionProfile": {
                "href": f"https://hmc.example.test/rest/api/uom/x/{PROFILE_UUID}"
            },
        }

    def _hmc_get_lpar(self, kwargs):
        if not self.is_scratch(kwargs["lpar_name_or_uuid"]):
            return "FAIL", _failure("LPAR not found")
        return "PASS", {"UUID": SCRATCH_UUID, "Resource": self._resource()}

    def _hmc_get_lpar_description(self, kwargs):
        if not self.is_scratch(kwargs["lpar_name_or_uuid"]):
            if self.stale_absence and not self.stale_served:
                self.stale_served = True
                return "FAIL", _failure("Connection reset")
            return "FAIL", _failure("HSCL8012 The partition named x was not found")
        token = "lparcfg-ffffffff" if self.foreign_token else self.token
        return "PASS", f"[hmcpctl owner:agent created:2026-10-06] [caller {token}]"

    def _hmc_get_lpar_state(self, kwargs):
        return "PASS", self.state

    def _change(self, resources: dict[str, Any]) -> tuple[str, Any]:
        if not resources:
            return "FAIL", _failure("Nothing to change: pass at least one field")
        if self.state != "Not Activated" and self.activated_dlpar_refused:
            return "FAIL", _failure(self.activated_failure)
        for key, value in resources.items():
            self.values[key] = float(value)
        return "PASS", {"change_location": {"lives_in": "current-configuration"}}

    def _hmc_modify_lpar(self, kwargs):
        if self.modify_406:
            return "FAIL", _failure("HTTP 406 Not Acceptable", 406)
        return self._change(kwargs["resources"])

    def _hmc_dlpar_mem(self, kwargs):
        if self.ignore_memory_write and kwargs["resources"]:
            return "PASS", {}
        return self._change(kwargs["resources"])

    def _hmc_dlpar_proc(self, kwargs):
        return self._change(kwargs["resources"])

    def _hmc_rename_lpar(self, kwargs):
        self.renames += 1
        self.name = kwargs["new_name"]
        if self.rename_back_lies and self.renames == 2:
            return "FAIL", _failure("Read timed out")
        return "PASS", {}

    def _hmc_read_lpar_boot_order(self, kwargs):
        return "PASS", {"pending_boot_string": self.pending}

    def _hmc_set_lpar_boot_order(self, kwargs):
        if self.set_refused:
            return "FAIL", _failure("REST0126 refused")
        self.pending = " ".join(kwargs["devices"])
        return "PASS", {}

    def _hmc_clear_lpar_boot_order(self, kwargs):
        return "FAIL", _failure(
            "Refusing to clear the pending boot order: no value tried on a V10R3 HMC"
        )

    def _hmc_power_on_lpar(self, kwargs):
        if not self.never_activates:
            self.state = "Open Firmware"
        return "PASS", {"job": {"status": "COMPLETED_OK"}}

    def _hmc_power_off_lpar(self, kwargs):
        if self.power_off_refused:
            return "FAIL", _failure("HSCL0001 refused")
        self.state = "Not Activated"
        return "PASS", {}

    def _hmc_delete_lpar(self, kwargs):
        if self.delete_refused:
            return "FAIL", _failure("HSCL0001 refused")
        self.deleted = True
        return "PASS", "deleted"


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch):
    for name in (
        "_ABSENCE_REREAD_DELAY_S",
        "_STATE_POLL_DELAY_S",
        "_COMPARE_REREAD_DELAY_S",
    ):
        monkeypatch.setattr(lpar_config, name, 0)
    monkeypatch.setattr(lpar_config, "_STATE_POLL_ATTEMPTS", 2)


async def _run(monkeypatch, hmc: FakeHMC, group: str | None = "lpar-config"):
    async def scripted(_state, _client, tool, **kwargs):
        return hmc.answer(tool, kwargs)

    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = runner.RunState(
        config=live_config.LiveTestConfig(system_name=SYSTEM), group=group
    )
    await lpar_config.exercise_lpar_config(object(), state)
    return state


def _results(state) -> dict[str, str]:
    return {e["operation"]: e["observation"]["result"] for e in state.observations}


def _held(state, operation: str) -> list[str]:
    (entry,) = [e for e in state.observations if e["operation"] == operation]
    return entry["observation"]["assertions"]


def _rows(state, tool: str) -> list[dict[str, Any]]:
    return [row for row in state.results if row["tool"] == tool]


_ALL_PASSED = {
    "lpar.modify": "passed",
    "lpar.dlpar_mem": "passed",
    "lpar.dlpar_proc": "passed",
    "lpar.rename": "passed",
    "boot_order.set": "passed",
    "boot_order.clear": "passed",
}


@pytest.mark.asyncio
async def test_clean_run_passes_every_observation(monkeypatch):
    hmc = FakeHMC()
    state = await _run(monkeypatch, hmc)

    assert _results(state) == _ALL_PASSED
    assert {e["observation"]["cleanup"] for e in state.observations} == {"passed"}
    assert hmc.deleted
    assert hmc.name.startswith("hmcpctl-live-lpar-")
    assert hmc.token == "lparcfg-" + hmc.name.removeprefix("hmcpctl-live-lpar-")


@pytest.mark.asyncio
async def test_clean_run_records_no_failed_row(monkeypatch):
    """Expected refusals are PASS rows: a FAIL row must mean something went wrong."""
    state = await _run(monkeypatch, FakeHMC(activated_dlpar_refused=False))

    assert [row["tool"] for row in state.results if row["status"] == "FAIL"] == []


@pytest.mark.asyncio
async def test_refused_delete_leaves_recovery_row(monkeypatch):
    hmc = FakeHMC(delete_refused=True)
    state = await _run(monkeypatch, hmc)

    (row,) = _rows(state, "scratch partition teardown")
    assert "the delete was refused" in row["data"]
    assert {e["observation"]["cleanup"] for e in state.observations} == {"failed"}


@pytest.mark.asyncio
async def test_rename_reported_failed_does_not_hide_a_refused_delete(monkeypatch):
    """The partition went back to its name though the call failed; the listing sees it."""
    hmc = FakeHMC(rename_back_lies=True, delete_refused=True)
    state = await _run(monkeypatch, hmc)

    assert hmc.exists()
    assert _rows(state, "scratch partition teardown")


@pytest.mark.asyncio
async def test_outside_its_group_it_calls_nothing(monkeypatch):
    hmc = FakeHMC()
    state = await _run(monkeypatch, hmc, group="round2")

    assert hmc.calls == []
    assert state.results[-1]["status"] == "SKIP"
    assert state.observations == []


@pytest.mark.asyncio
async def test_existing_scratch_partition_blocks_the_run(monkeypatch):
    hmc = FakeHMC(existing_scratch=True)
    state = await _run(monkeypatch, hmc)

    assert not any(tool == "hmc_create_lpar" for tool, _ in hmc.calls)
    assert "live_test_recovery.py" in state.results[-1]["note"]


@pytest.mark.asyncio
async def test_every_mutation_targets_the_scratch_partition(monkeypatch):
    hmc = FakeHMC()
    await _run(monkeypatch, hmc)

    mutations = [kwargs for tool, kwargs in hmc.calls if tool in MUTATIONS]
    assert mutations
    assert {kwargs["lpar_name_or_uuid"] for kwargs in mutations} == {SCRATCH_UUID}


@pytest.mark.asyncio
async def test_unapplied_memory_change_fails_dlpar_mem(monkeypatch):
    state = await _run(monkeypatch, FakeHMC(ignore_memory_write=True))

    assert _results(state)["lpar.dlpar_mem"] == "failed"
    assert "small-change-read-back" not in _held(state, "lpar.dlpar_mem")
    assert "empty-request-refused" in _held(state, "lpar.dlpar_mem")


@pytest.mark.asyncio
async def test_modify_refusal_is_a_failed_observation(monkeypatch):
    state = await _run(monkeypatch, FakeHMC(modify_406=True))

    assert _results(state)["lpar.modify"] == "failed"
    assert state.gaps == []
    assert _rows(state, "hmc_modify_lpar (memory and units)")[0]["status"] == "FAIL"


@pytest.mark.asyncio
async def test_foreign_token_is_not_deleted(monkeypatch):
    hmc = FakeHMC(foreign_token=True)
    state = await _run(monkeypatch, hmc)

    assert not any(tool == "hmc_delete_lpar" for tool, _ in hmc.calls)
    assert "MANUAL RECOVERY REQUIRED" in str(_rows(state, "scratch partition teardown"))
    assert "rmsyscfg -r lpar -m sys-A -n" in str(state.results)


@pytest.mark.asyncio
async def test_pool_drift_fails_cleanup(monkeypatch):
    state = await _run(monkeypatch, FakeHMC(pool_drift=True))

    assert {e["observation"]["cleanup"] for e in state.observations} == {"failed"}
    assert set(_results(state).values()) == {"failed"}


@pytest.mark.asyncio
async def test_pool_lag_is_reread(monkeypatch):
    state = await _run(monkeypatch, FakeHMC(pool_lag=True))

    (compare,) = _rows(state, "system baseline compare")
    assert compare["status"] == "PASS"
    assert len(compare["data"]["reads"]) == 2
    assert _results(state) == _ALL_PASSED


@pytest.mark.asyncio
async def test_activated_refusal_is_a_gap_row(monkeypatch):
    state = await _run(monkeypatch, FakeHMC())

    (row,) = _rows(state, "hmc_dlpar_mem (activated)")
    assert row["status"] == "SKIP"
    assert "active RMC connection" in row["note"]
    assert "HSCL294C" in row["data"]
    # A failure nested inside a row's data would skip the runner's redaction.
    assert row["data"] == "HSCL294C no RMC connection"


@pytest.mark.asyncio
async def test_activated_transport_failure_is_not_the_gap(monkeypatch):
    """Only an HMC refusal is the documented gap; a lost session stays a FAIL."""
    hmc = FakeHMC(activated_failure="Connection reset by peer")
    state = await _run(monkeypatch, hmc)

    (row,) = _rows(state, "hmc_dlpar_mem (activated)")
    assert row["status"] == "FAIL"
    assert "RMC" not in row["note"]
    assert "Traceback" not in json.dumps(state.results, default=str)
    assert not any(
        e["operation"] == "lpar.dlpar_mem" and "activated" in str(e)
        for e in state.observations
    )


@pytest.mark.asyncio
async def test_activated_acceptance_makes_no_rmc_claim(monkeypatch):
    state = await _run(monkeypatch, FakeHMC(activated_dlpar_refused=False))

    for tool in ("hmc_dlpar_mem (activated)", "hmc_dlpar_proc (activated)"):
        (row,) = _rows(state, tool)
        assert row["status"] == "PASS"
        assert "RMC" not in row["note"]
        (read_back,) = _rows(state, tool.replace(")", " read-back)"))
        assert read_back["data"]["read_back"]["memory"]
    assert _results(state) == _ALL_PASSED


@pytest.mark.asyncio
async def test_activation_timeout_skips_dlpar(monkeypatch):
    hmc = FakeHMC(never_activates=True)
    state = await _run(monkeypatch, hmc)

    assert _rows(state, "hmc_dlpar_mem (activated)") == []
    assert _rows(state, "activated DLPAR")[0]["status"] == "SKIP"
    assert hmc.deleted


@pytest.mark.asyncio
async def test_create_without_uuid_is_found_by_token(monkeypatch):
    hmc = FakeHMC(create_without_uuid=True)
    state = await _run(monkeypatch, hmc)

    assert _results(state) == _ALL_PASSED
    assert hmc.deleted


@pytest.mark.asyncio
async def test_power_off_refused_leaves_recovery_row(monkeypatch):
    hmc = FakeHMC(power_off_refused=True)
    state = await _run(monkeypatch, hmc)

    assert not hmc.deleted
    (row,) = _rows(state, "scratch partition teardown")
    assert "did not reach Not Activated" in row["data"]
    assert "chsysstate -m sys-A -r lpar -n" in row["data"]
    assert {e["observation"]["cleanup"] for e in state.observations} == {"failed"}


@pytest.mark.asyncio
async def test_absence_rereads_once(monkeypatch):
    hmc = FakeHMC(stale_absence=True)
    state = await _run(monkeypatch, hmc)

    assert hmc.stale_served
    assert "old-name-absent" in _held(state, "lpar.rename")
    assert _results(state) == _ALL_PASSED


@pytest.mark.asyncio
async def test_clear_is_not_proven_without_a_pending_string(monkeypatch):
    state = await _run(monkeypatch, FakeHMC(set_refused=True))

    assert _results(state)["boot_order.set"] == "failed"
    assert "pending-boot-string-unchanged" not in _held(state, "boot_order.clear")


def test_scratch_partitions_selects_only_the_reserved_prefix():
    names = [
        "hmcpctl-live-lpar-0a1b2c3d",
        "lpar-A",
        "hmcpctl-live-x",
        "hmcpctl-live-lpar-1-rn",
    ]

    assert lpar_config.scratch_partitions(names) == [
        "hmcpctl-live-lpar-0a1b2c3d",
        "hmcpctl-live-lpar-1-rn",
    ]


@dataclass
class DedicatedHMC(FakeHMC):
    mode: str = "ok"
    slots: str = "none"
    other_slots: str = "21010030/none/0"
    profile_reads: int = 0
    inventory_reads: int = 0

    def _hmc_run_command(self, kwargs):
        cmd = kwargs["cmd"]
        if cmd == "lshmc -V":
            if self.mode == "release-failure":
                return "FAIL", _failure("release read refused")
            if self.mode == "release-nontext":
                return "PASS", {"unexpected": "shape"}
            if self.mode == "release-malformed":
                return "PASS", "Version: 10 Release: 3"
            if self.mode == "release-duplicate":
                return "PASS", "Version: 10 Version: 10 Release: 3 Service Pack: 1060"
            return "PASS", "Version: 10 Release: 3 Service Pack: 1060"
        if "-F type_model" in cmd:
            if self.mode == "model-failure":
                return "FAIL", _failure("model read refused")
            return "PASS", {
                "model-nontext": None,
                "model-malformed": "garbage",
                "unsupported": "9009-22A",
            }.get(self.mode, "8375-42A")
        if "-r prof" in cmd:
            self.profile_reads += 1
            if self.mode == "profile-failure":
                return "FAIL", _failure("profile read refused")
            if self.mode == "profile-nontext":
                return "PASS", []
            if self.mode == "profile-malformed":
                return "PASS", "not a profile table"
            if self.mode == "profile-held":
                self.other_slots = "21010020/none/0"
            rows = [
                "lpar_name,name,io_slots",
                f'{OTHER},default_profile,"{self.other_slots}"',
            ]
            if self.exists():
                rows += [f'{self.name},default_profile,"{self.slots}"']
            return "PASS", "\n".join(rows)
        return super()._hmc_run_command(kwargs)

    def _hmc_list_dedicated_pcie_slots(self, kwargs):
        self.inventory_reads += 1
        if self.mode == "fresh-inventory-malformed" and self.inventory_reads > 1:
            return "PASS", {
                "items": [{"drc_index": "21010020", "owner_lpar": None}, None]
            }
        if self.mode == "inventory-missing-owner" or (
            self.mode == "fresh-inventory-missing-owner" and self.inventory_reads > 1
        ):
            return "PASS", {"items": [{"drc_index": "21010020"}]}
        if self.mode == "inventory-failure":
            return "FAIL", _failure("inventory read refused")
        if self.mode == "inventory-malformed":
            return "PASS", {"items": None}
        owner = OTHER if self.mode == "owned" else None
        return "PASS", {"items": [{"drc_index": "21010020", "owner_lpar": owner}]}

    def _hmc_modify_lpar(self, kwargs):
        if "assignments" not in kwargs:
            return super()._hmc_modify_lpar(kwargs)
        self.slots = "21010020/none/0"
        if self.mode == "wrong-triple":
            self.slots = "21010020/none/1"
        if self.mode == "other-drift":
            self.other_slots = "21010040/none/0"
        if self.mode == "lost-response":
            return "FAIL", _failure("assignment response lost")
        if self.mode == "foreign-after-write":
            self.foreign_token = True
        if self.mode == "typed":
            return "PASS", LparPcieWorkflowResult(
                False, True, None, None, (WorkflowStep("dedicated[0]", "ok"),), ()
            )
        data = {
            "workflow_completed": self.mode != "partial",
            "steps": [{"step": "dedicated[0]", "status": "ok"}],
            "lpar": None,
        }
        if self.mode == "wrong-step":
            data["steps"][0]["status"] = "error"
        if self.mode == "warning":
            data["warnings"] = ["final LPAR read failed"]
        return "PASS", data

    def _hmc_unassign_dedicated_pcie_slot(self, kwargs):
        if self.mode == "unassign-refused":
            return "FAIL", _failure("unassign refused")
        self.slots = "none"
        return "PASS", {"changed": True}


async def _run_dedicated(monkeypatch, hmc, *, system=SYSTEM, drc="21010020"):
    async def scripted(_state, _client, tool, **kwargs):
        return hmc.answer(tool, kwargs)

    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = runner.RunState(
        config=live_config.LiveTestConfig(
            system_name=SYSTEM,
            dedicated_pcie_system_name=system,
            dedicated_pcie_lpar_prefix="hmcpctl-dedicated-",
            dedicated_pcie_drc_index=drc,
        ),
        group="lpar-config",
    )
    await lpar_config.exercise_lpar_config(object(), state)
    return state


def _dedicated_observation(state):
    return next(
        entry["observation"]
        for entry in state.observations
        if entry["observation"]["id"] == "st39-hmc-modify-lpar-dedicated"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "warning", "typed"])
async def test_dedicated_assignment_uses_modify_and_scratch_uuid(monkeypatch, mode):
    hmc = DedicatedHMC(mode=mode)
    state = await _run_dedicated(monkeypatch, hmc)
    observation = _dedicated_observation(state)
    assert observation["result"] == "passed"
    assignments = [
        kw
        for tool, kw in hmc.calls
        if tool == "hmc_modify_lpar" and "assignments" in kw
    ]
    assert assignments == [
        {
            "lpar_name_or_uuid": SCRATCH_UUID,
            "system_name_or_uuid": SYSTEM,
            "assignments": {
                "dedicated": [
                    {"profile_name": "default_profile", "drc_index": "21010020"}
                ]
            },
        }
    ]
    tools = [tool for tool, _ in hmc.calls]
    assert tools.count("hmc_unassign_dedicated_pcie_slot") == 1
    assert tools.index("hmc_unassign_dedicated_pcie_slot") < tools.index(
        "hmc_power_on_lpar"
    )
    assert hmc.slots == "none" and hmc.deleted
    assert hmc.other_slots == "21010030/none/0"
    assert not any("sriov" in tool or "vnic" in tool for tool in tools)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "release-failure",
        "model-failure",
        "release-nontext",
        "model-nontext",
        "release-malformed",
        "release-duplicate",
        "model-malformed",
        "profile-failure",
        "profile-nontext",
        "profile-malformed",
        "inventory-failure",
        "inventory-malformed",
    ],
)
async def test_dedicated_precreate_read_failures_stay_failed(monkeypatch, mode):
    hmc = DedicatedHMC(mode=mode)
    state = await _run_dedicated(monkeypatch, hmc)
    assert any(row["status"] == "FAIL" for row in state.results)
    if mode.endswith("failure"):
        assert any("read refused" in str(row["data"]) for row in state.results)
    assert not any("assignments" in kw for _, kw in hmc.calls)
    assert not any(
        e["observation"]["id"].endswith("-dedicated") for e in state.observations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["owned", "unsupported", "profile-held"])
async def test_dedicated_unavailable_is_skip_without_fail(monkeypatch, mode):
    state = await _run_dedicated(monkeypatch, DedicatedHMC(mode=mode))
    assert not any(row["status"] == "FAIL" for row in state.results)
    assert any(
        "dedicated" in row["tool"] and row["status"] == "SKIP" for row in state.results
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["partial", "lost-response", "wrong-step"])
async def test_partial_assignment_is_failed_and_restores_once(monkeypatch, mode):
    hmc = DedicatedHMC(mode=mode)
    state = await _run_dedicated(monkeypatch, hmc)
    assert _dedicated_observation(state)["result"] == "failed"
    assert hmc.slots == "none" and hmc.deleted
    assert sum(tool == "hmc_unassign_dedicated_pcie_slot" for tool, _ in hmc.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["wrong-triple", "other-drift", "foreign-after-write", "unassign-refused"]
)
async def test_remaining_slot_never_activates_or_deletes(monkeypatch, mode):
    hmc = DedicatedHMC(mode=mode)
    state = await _run_dedicated(monkeypatch, hmc)
    assert _dedicated_observation(state)["result"] == "failed"
    assert hmc.exists()
    assert not any(
        tool in {"hmc_power_on_lpar", "hmc_delete_lpar"} for tool, _ in hmc.calls
    )
    assert sum(tool == "hmc_unassign_dedicated_pcie_slot" for tool, _ in hmc.calls) <= 1
    assert any("MANUAL RECOVERY REQUIRED" in str(row) for row in state.results)


@pytest.mark.asyncio
async def test_dedicated_auto_select_and_mismatched_system(monkeypatch):
    hmc = DedicatedHMC()
    state = await _run_dedicated(monkeypatch, hmc, drc="")
    assert _dedicated_observation(state)["result"] == "passed"
    hmc = DedicatedHMC()
    state = await _run_dedicated(monkeypatch, hmc, system="sys-B")
    assert not any("assignments" in kw for _, kw in hmc.calls)
    assert not any(row["status"] == "FAIL" for row in state.results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "release-failure",
        "model-nontext",
        "model-malformed",
        "profile-failure",
        "inventory-failure",
        "inventory-missing-owner",
        "ok",
        "other-drift",
    ],
)
async def test_actual_runner_preserves_exit_and_recovery_baseline(
    monkeypatch, tmp_path, mode
):
    hmc = DedicatedHMC(mode=mode)

    class Client:
        async def call_tool(self, tool, kwargs):
            status, data = hmc.answer(tool, kwargs)
            if status == "FAIL":
                raise RuntimeError(data.message)
            return SimpleNamespace(data=data)

    @asynccontextmanager
    async def served():
        yield Client()

    async def schemas(_client):
        return {}

    monkeypatch.setattr(runner, "served_client", served)
    monkeypatch.setattr(runner, "served_schemas", schemas)
    monkeypatch.setattr(evidence, "repository_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    config = live_config.LiveTestConfig(
        system_name=SYSTEM,
        dedicated_pcie_system_name=SYSTEM,
        dedicated_pcie_lpar_prefix="scratch-",
        dedicated_pcie_drc_index="21010020",
    )
    result_path = tmp_path / "test-results-lpar-config.json"
    code = await runner.main(
        group="lpar-config",
        config=config,
        hmc_config=HMCConfig.from_mapping({}),
        results_path=str(result_path),
    )
    assert code == (0 if mode == "ok" else 1)
    rows = json.loads(result_path.read_text())["results"]
    if mode != "ok":
        assert any(
            row["status"] == "FAIL" and "dedicated" in row["tool"] for row in rows
        )
    if mode in {"ok", "other-drift"}:
        baseline_index = next(
            index
            for index, row in enumerate(rows)
            if row["tool"] == "dedicated recovery baseline"
        )
        baseline = rows[baseline_index]["data"]
        assert baseline == {
            "drc_index": "21010020",
            "profile_name": "default_profile",
            "profiles": [
                {
                    "lpar_name": OTHER,
                    "profile_name": "default_profile",
                    "io_slots": [
                        {"drc_index": "21010030", "pool_id": None, "is_required": False}
                    ],
                }
            ],
        }
        assert baseline_index < next(
            index
            for index, row in enumerate(rows)
            if row["tool"] == "hmc_create_lpar (scratch)"
        )
        if mode == "other-drift":
            failed_read = next(
                row for row in rows if row["tool"] == "dedicated profile read-back"
            )
            assert failed_read["status"] == "FAIL"
            observed = failed_read["data"]["profiles"]
            assert isinstance(observed, list)
            assert any(
                slot["drc_index"] == "21010040"
                for profile in observed
                for slot in profile["io_slots"]
            )
    if mode != "other-drift":
        assert any(
            row["tool"] == "hmc_modify_lpar" and row["status"] == "PASS" for row in rows
        )


@pytest.mark.asyncio
async def test_fresh_inventory_malformed_row_fails_before_assignment(monkeypatch):
    hmc = DedicatedHMC(mode="fresh-inventory-malformed")
    state = await _run_dedicated(monkeypatch, hmc)
    assert any(row["status"] == "FAIL" for row in state.results)
    assert not any("assignments" in kw for _, kw in hmc.calls)
    assert not any(tool == "hmc_power_on_lpar" for tool, _ in hmc.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["inventory-missing-owner", "fresh-inventory-missing-owner"]
)
async def test_missing_inventory_owner_fails_closed(monkeypatch, mode):
    hmc = DedicatedHMC(mode=mode)
    state = await _run_dedicated(monkeypatch, hmc)
    assert any(
        row["tool"] == "dedicated inventory shape" and row["status"] == "FAIL"
        for row in state.results
    )
    assert not any("assignments" in kwargs for _, kwargs in hmc.calls)
    if mode.startswith("fresh-"):
        assert not any(tool == "hmc_power_on_lpar" for tool, _ in hmc.calls)
