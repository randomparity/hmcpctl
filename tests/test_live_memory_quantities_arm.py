"""Owned inactive arm50 must establish outcomes through independent readback."""

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP
from lpar.test_memory_quantities import (
    LPAR_UUID,
    SYSTEM_UUID,
    quantity_boundary,  # noqa: F401 -- shared HMC boundary fixture
)

from hmcpctl.config import HMCConfig
from hmcpctl.operations.lpar.core import LparCreationResult
from hmcpctl.operations.lpar.workflow_contract import WorkflowStep

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import config, lpar_config, memory_quantities, runner
from live_test.observation import CallFailure

UUID = "aaaaaaaa-0000-4000-8000-000000000050"
OVER_MAXIMUM_MESSAGE = (
    "ToolError: Error calling tool 'hmc_dlpar_mem': "
    "Requested memory target exceeds the partition maximum"
)


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
    applies: int = 0

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
            token = (
                "other" if self.fault in {"foreign", "apply-foreign"} else self.token
            )
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
            if (
                self.fault
                in {
                    "uninitialized",
                    "setup",
                    "apply-refused",
                    "apply-foreign",
                    "apply-moved",
                }
                and not self.applies
            ):
                memory.update(
                    CurrentMemory="0",
                    CurrentMinimumMemory="0",
                    CurrentMaximumMemory="0",
                )
            if self.fault == "malformed":
                memory["CurrentMemory"] = "missing"
            if (self.fault == "post-read" and self.native_calls) or (
                self.fault == "refusal-post-unavailable" and self.native_calls == 3
            ):
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
            if self.fault == "refusal-unavailable":
                return "FAIL", None
            if self.fault == "refusal-post-read":
                self.current -= 256
            message = {
                "refusal-permission": "ToolError: ownership permission denied",
                "refusal-transport": "ToolError: Timeout connecting to HMC",
                "refusal-reason": "ToolError: Error calling tool 'hmc_dlpar_mem': Requested memory exceeds available system capacity",
                "refusal-message": OVER_MAXIMUM_MESSAGE + "; transport lost",
            }.get(self.fault, OVER_MAXIMUM_MESSAGE)
            return "FAIL", CallFailure(
                "ValueError" if self.fault == "refusal-type" else "ToolError",
                message,
                "",
                None,
                self.fault == "refusal-permission",
            )
        if self.fault == "native-rejected":
            return "PASS", {"outcome": "rejected", "workflow_completed": False}
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
        from types import SimpleNamespace

        yield SimpleNamespace(config=config)

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

    async def authorize(client, lpar, *, system_name_or_uuid):
        assert lpar == UUID and system_name_or_uuid == "sys-R1"
        if system.fault == "apply-moved":
            raise ValueError("Target left selected system")
        return "sys-R1", system.name

    async def apply(config, selected, name):
        assert system.creations[0].apply_profile is False
        assert selected == "sys-R1" and name == system.name
        assert any(tool == "hmc_get_lpar_description" for tool, _ in system.calls)
        if system.fault == "apply-refused":
            raise ValueError("Native setup refused")
        system.applies += 1
        if system.fault == "uninitialized":
            system.current = 0

    monkeypatch.setattr(
        memory_quantities, "resolve_and_authorize_lpar_names", authorize
    )
    monkeypatch.setattr(memory_quantities, "apply_lpar_profile_via_cli", apply)

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
async def test_inactive_removal_sequence_independently_reads_and_cleans(
    monkeypatch,
):
    system = ScriptedSystem()
    state = await run_arm(monkeypatch, system)
    assert system.deleted
    assert system.current == 1792
    assert system.applies == 0
    assert len(system.creations) == 1
    assert system.creations[0].apply_profile is False
    assert system.creations[0].stamp_policy == "required"
    changes = [
        kwargs["change"] for tool, kwargs in system.calls if tool == "hmc_dlpar_mem"
    ]
    assert [(item["operation"], item["quantity_mib"]) for item in changes] == [
        ("remove", 256),
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
        "native-rejected",
        "post-read",
        "delete",
        "refusal-read",
        "refusal-permission",
        "refusal-transport",
        "refusal-reason",
        "refusal-post-unavailable",
        "refusal-message",
        "refusal-type",
        "refusal-unavailable",
        "refusal-post-read",
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


def test_named_arm_is_registered_without_joining_default_run():
    assert runner.SUBTASK_GROUPS[memory_quantities.GROUP] == [50]
    assert runner.SUBTASKS[50] is memory_quantities.exercise_memory_quantities
    assert 50 not in runner.SUBTASK_GROUPS["all"]


@pytest.mark.asyncio
async def test_unconfigured_create_applies_only_after_owned_identity_read(monkeypatch):
    system = ScriptedSystem(fault="setup")
    state = await run_arm(monkeypatch, system)
    assert system.applies == 1
    assert system.native_calls == 3
    assert state.observations[0]["observation"]["result"] == "passed"
    assert system.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["apply-refused", "apply-foreign", "apply-moved"])
async def test_unsafe_or_failed_setup_never_submits_quantities(monkeypatch, fault):
    system = ScriptedSystem(fault=fault)
    state = await run_arm(monkeypatch, system)
    assert system.native_calls == 0
    assert system.applies == 0
    assert not any(
        item["observation"]["result"] == "passed" for item in state.observations
    )


@pytest.mark.asyncio
async def test_inactive_removal_never_crosses_minimum(monkeypatch):
    system = ScriptedSystem(current=1024)
    state = await run_arm(monkeypatch, system)
    assert system.native_calls == 0
    assert system.deleted
    assert not any(
        item["observation"]["result"] == "passed" for item in state.observations
    )


@pytest.mark.asyncio
async def test_native_refusal_stops_inactive_sequence_without_replay(monkeypatch):
    system = ScriptedSystem(fault="native-rejected")
    state = await run_arm(monkeypatch, system)
    changes = [
        kwargs["change"] for tool, kwargs in system.calls if tool == "hmc_dlpar_mem"
    ]
    assert changes == [{"operation": "remove", "quantity_mib": 256}]
    assert system.current == 2048 and system.deleted
    assert state.observations[0]["observation"]["result"] == "failed"


@pytest.mark.asyncio
async def test_inactive_removal_can_reach_minimum_without_reset(monkeypatch):
    system = ScriptedSystem(current=1280)
    state = await run_arm(monkeypatch, system)
    assert system.current == 1024 and system.deleted
    assert state.observations[0]["observation"]["result"] == "passed"


@pytest.mark.asyncio
async def test_overmax_oracle_through_actual_mcp_memory_admission(
    monkeypatch,
    quantity_boundary,  # noqa: F811 -- imported pytest boundary fixture
):
    from hmcpctl.operations.lpar import memory, ownership
    from hmcpctl.server_tools.lpar import lifecycle

    boundary = quantity_boundary
    run = lpar_config._Run(SYSTEM_UUID, "lpar-R1", "lparcfg-a1b2c3d4", 256, subtask=50)
    run.uuid = LPAR_UUID
    stamp = f"[hmcpctl owner:hmcpctl created:2026-10-23] [caller {run.token}]"
    monkeypatch.setattr(
        memory, "authorize_lpar_mutation", ownership.authorize_lpar_mutation
    )
    monkeypatch.setattr(
        ownership, "get_lpar_description", AsyncMock(return_value=stamp)
    )
    monkeypatch.setattr(
        lifecycle, "with_client", lambda fn, **kwargs: asyncio.run(fn(boundary.hmc))
    )
    server = FastMCP("memory admission boundary")
    server.tool(lifecycle.hmc_dlpar_mem)

    @server.tool
    async def hmc_get_lpar(lpar_name_or_uuid: str, system_name_or_uuid: str) -> dict:
        assert lpar_name_or_uuid == LPAR_UUID and system_name_or_uuid == SYSTEM_UUID
        return await boundary.hmc.get_logical_partition(lpar_name_or_uuid)

    @server.tool
    async def hmc_get_lpar_description(
        lpar_name_or_uuid: str, system_name_or_uuid: str
    ) -> str:
        assert lpar_name_or_uuid == LPAR_UUID and system_name_or_uuid == SYSTEM_UUID
        return await ownership.get_lpar_description(
            boundary.hmc.config, "sys-R1", run.name
        )

    state = runner.RunState()
    async with Client(server) as client:
        held = await memory_quantities._case(
            client, state, run, "add", 4352, (512, 1024, 4096), "refused"
        )
    assert held
    failure = state.results[-1]["data"]["result"]
    assert failure.exception_type == "ToolError"
    assert failure.message == OVER_MAXIMUM_MESSAGE
    assert boundary.hmc.get_managed_system.await_count == 1
    assert boundary.hmc.get_logical_partition.await_count >= 2
    boundary.command.assert_not_awaited()
