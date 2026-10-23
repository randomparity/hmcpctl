"""Owned inactive arm50 must establish outcomes through independent readback."""

import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.operations.lpar.core import LparCreationResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import config, lpar_config, memory_quantities, runner
from live_test.observation import CallFailure

UUID = "aaaaaaaa-0000-4000-8000-000000000050"


@dataclass
class ScriptedSystem:
    fault: str = "none"
    name: str | None = None
    token: str | None = None
    current: int = 2048
    deleted: bool = False
    calls: list = field(default_factory=list)
    native_calls: int = 0
    creations: list = field(default_factory=list)

    def answer(self, tool, kwargs):
        self.calls.append((tool, kwargs))
        if tool == "hmc_run_command":
            command = kwargs["cmd"]
            if command.startswith("lssyscfg"):
                names = ["existing-R1"]
                if self.name and not self.deleted:
                    names.append(self.name)
                if self.fault == "stranded":
                    names.append("hmcpctl-live-lpar-00000000")
                return "PASS", "\n".join(names)
            return "PASS", "8.0" if "proc" in command else "16384,256"
        assert kwargs["lpar_name_or_uuid"] == UUID
        if tool == "hmc_get_lpar_description":
            token = "other" if self.fault == "foreign" else self.token
            return (
                "PASS",
                f"[hmcpctl owner:hmcpctl created:2026-10-23] [caller {token}]",
            )
        if tool == "hmc_get_lpar_state":
            return "PASS", "running" if self.fault == "active" else "not activated"
        if tool == "hmc_delete_lpar":
            if self.fault == "delete":
                return "FAIL", CallFailure("HMCError", "refused", "", None, False)
            self.deleted = True
            return "PASS", {}
        if tool == "hmc_get_lpar":
            memory = {
                "CurrentMemory": str(self.current),
                "CurrentMinimumMemory": "1024",
                "CurrentMaximumMemory": "4096",
            }
            if self.fault == "uninitialized":
                memory.update(
                    CurrentMemory="0",
                    CurrentMinimumMemory="0",
                    CurrentMaximumMemory="0",
                )
            if self.fault == "malformed":
                memory["CurrentMemory"] = "missing"
            if self.fault == "post-read" and self.native_calls:
                return "FAIL", CallFailure("HMCError", "lost read", "", None, False)
            return "PASS", {
                "UUID": UUID,
                "Resource": {
                    "PartitionName": self.name,
                    "PartitionState": "running"
                    if self.fault == "active"
                    else "not activated",
                    "PartitionType": "AIX/Linux",
                    "PartitionMemoryConfiguration": memory,
                },
            }
        assert tool == "hmc_dlpar_mem"
        self.native_calls += 1
        change = kwargs["change"]
        if change["quantity_mib"] > 4096:
            if self.fault == "refusal-read":
                return "FAIL", CallFailure("HMCError", "timeout", "", None, False)
            return "FAIL", CallFailure(
                "ValueError",
                "Requested memory target exceeds the partition maximum",
                "",
                None,
                False,
            )
        if self.fault != "ignored":
            self.current = {
                "set": change["quantity_mib"],
                "add": self.current + change["quantity_mib"],
                "remove": self.current - change["quantity_mib"],
            }[change["operation"]]
        outcome = (
            "partial"
            if self.fault == "partial"
            else "noop"
            if change["operation"] == "add" and change["quantity_mib"] == 0
            else "applied"
        )
        return "PASS", {
            "outcome": outcome,
            "workflow_completed": outcome in {"applied", "noop"},
        }


async def run_arm(monkeypatch, system, group="lpar-memory-quantities"):
    @asynccontextmanager
    async def client_context(config):
        yield object()

    async def create(client, selected, creation):
        assert selected == "sys-R1"
        system.creations.append(creation)
        system.name, system.token = creation.name, creation.caller_token
        return LparCreationResult(
            True,
            {"UUID": UUID},
            system.fault != "stamp",
            (),
            apply_step=WorkflowStep(
                "apply_profile", "error" if system.fault == "apply" else "ok"
            ),
        )

    async def call(state, client, tool, **kwargs):
        return system.answer(tool, kwargs)

    monkeypatch.setattr(memory_quantities, "HMCClient", client_context)
    monkeypatch.setattr(memory_quantities, "create_and_stamp_lpar", create)
    monkeypatch.setattr(runner.RunState, "call", call)
    monkeypatch.setattr(lpar_config, "_ABSENCE_REREAD_DELAY_S", 0)
    monkeypatch.setattr(lpar_config, "_COMPARE_REREAD_DELAY_S", 0)
    state = runner.RunState(
        config=config.LiveTestConfig(system_name="sys-R1"),
        group=group,
        hmc_config=HMCConfig.from_mapping({}),
    )
    await memory_quantities.exercise_memory_quantities(object(), state)
    return state


@pytest.mark.asyncio
async def test_full_inactive_quantity_sequence_independently_reads_and_cleans(
    monkeypatch,
):
    system = ScriptedSystem()
    state = await run_arm(monkeypatch, system)
    assert system.deleted
    assert system.current == 2048
    assert len(system.creations) == 1
    assert system.creations[0].apply_profile is True
    assert system.creations[0].stamp_policy == "required"
    changes = [
        kwargs["change"] for tool, kwargs in system.calls if tool == "hmc_dlpar_mem"
    ]
    assert [(item["operation"], item["quantity_mib"]) for item in changes] == [
        ("set", 2304),
        ("add", 256),
        ("remove", 512),
        ("add", 0),
        ("add", 4352),
    ]
    (observation,) = state.observations
    assert observation["observation"]["result"] == "passed"
    assert observation["observation"]["cleanup"] == "passed"
    assert not any("power" in tool or "profile" in tool for tool, _ in system.calls)
    assert not [row for row in state.results if row["status"] == "FAIL"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "stamp",
        "apply",
        "foreign",
        "active",
        "uninitialized",
        "malformed",
        "ignored",
        "partial",
        "post-read",
        "delete",
        "refusal-read",
    ],
)
async def test_failed_or_uncertain_arm_never_promotes_success_and_guards_cleanup(
    monkeypatch, fault
):
    system = ScriptedSystem(fault=fault)
    state = await run_arm(monkeypatch, system)
    assert not any(
        row["observation"]["result"] == "passed" for row in state.observations
    )
    if fault in {"foreign", "active", "delete"}:
        assert not system.deleted
        assert "MANUAL RECOVERY REQUIRED" in str(state.results)
    else:
        assert system.deleted
    if fault in {"stamp", "apply", "foreign", "active", "uninitialized", "malformed"}:
        assert system.native_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["all", "lpar-config"])
async def test_arm_is_opt_in_without_calls_outside_its_group(monkeypatch, group):
    system = ScriptedSystem()
    state = await run_arm(monkeypatch, system, group)
    assert not system.calls
    assert not system.creations
    assert not state.observations


@pytest.mark.asyncio
async def test_stranded_fixture_prevents_new_creation(monkeypatch):
    system = ScriptedSystem(fault="stranded")
    state = await run_arm(monkeypatch, system)
    assert not system.creations
    assert not state.observations
    assert state.results[-1]["status"] == "SKIP"
