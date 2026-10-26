"""Arm56 offline proof: owned fixture, independent REST/native oracle, cleanup."""

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lpar.test_processor_quantities import (
    processor_boundary,  # noqa: F401 -- shared network boundary fixture
    processor_entry,
    processor_row,
    processor_system,
)

from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError
from hmcpctl.operations.lpar.core import LparCreationResult

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import config, lpar_config, runner
from live_test.observation import CallFailure

UUID = "aaaaaaaa-0000-4000-8000-000000000056"
REFUSAL = "ToolError: Error calling tool 'hmc_dlpar_proc': Requested processor target exceeds partition maximum"


@dataclass
class ScriptedSystem:
    fault: str = "none"
    name: str | None = None
    token: str | None = None
    units: Decimal = Decimal("0.50")
    deleted: bool = False
    applies: int = 0
    creations: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    reads: int = 0

    def entry(self):
        entry = processor_entry()
        entry["UUID"] = UUID
        value = entry["Resource"]
        value.update(
            PartitionName=self.name,
            PartitionState="running" if self.fault == "active" else "not activated",
        )
        cpu = value["PartitionProcessorConfiguration"][
            "CurrentSharedProcessorConfiguration"
        ]
        cpu["CurrentProcessingUnits"] = str(self.units)
        if self.fault == "rest-tail" and self.changes:
            cpu["CurrentProcessingUnits"] = str(self.units) + "0" * 28 + "1"
        if self.fault in {"setup", "mixed", "setup-refused"} and not self.applies:
            value["PartitionMemoryConfiguration"].update(
                CurrentMemory="0", CurrentMinimumMemory="0", CurrentMaximumMemory="0"
            )
            if self.fault != "mixed":
                cpu.update({key: "0" for key in cpu})
        if self.fault == "memory-drift" and self.changes:
            value["PartitionMemoryConfiguration"]["CurrentMemory"] = "1024"
        return entry

    def answer(self, tool, kwargs):
        if tool == "hmc_run_command":
            cmd = kwargs["cmd"]
            if cmd.startswith("lssyscfg"):
                names = ["existing-R1"] + (
                    [self.name] if self.name and not self.deleted else []
                )
                if self.fault == "stranded":
                    names.append("hmcpctl-live-lpar-00000000")
                return "PASS", "\n".join(names)
            return "PASS", "8.0" if "proc" in cmd else "16384,256"
        assert kwargs["lpar_name_or_uuid"] == UUID
        if tool == "hmc_get_lpar_description":
            token = "foreign" if self.fault == "foreign" else self.token
            return (
                "PASS",
                f"[hmcpctl owner:hmcpctl created:2026-01-01] [caller {token}]",
            )
        if tool == "hmc_get_lpar_state":
            return "PASS", "running" if self.fault == "active" else "not activated"
        if tool == "hmc_get_lpar":
            return "PASS", self.entry()
        if tool == "hmc_delete_lpar":
            if self.fault == "delete":
                return "FAIL", CallFailure("HMCError", "refused", "", None, False)
            self.deleted = True
            return "PASS", {}
        assert tool == "hmc_dlpar_proc"
        change = kwargs["change"]
        self.changes.append(change)
        amount = Decimal(str(change["processing_units"]))
        if amount > 1:
            return "FAIL", CallFailure(
                "ToolError",
                REFUSAL if self.fault != "wrong-refusal" else "ToolError: timeout",
                "",
                None,
                False,
            )
        if self.fault == "rejected" or change["operation"] == "set":
            return "PASS", {"outcome": "rejected", "workflow_completed": False}
        if self.fault == "exception":
            raise HMCError("transport lost")
        if self.fault != "ignored":
            self.units = {
                "add": self.units + amount,
                "remove": self.units - amount,
            }[change["operation"]]
        outcome = (
            "partial"
            if self.fault == "partial"
            else "noop"
            if amount == 0
            else "applied"
        )
        return "PASS", {"outcome": outcome, "workflow_completed": outcome != "partial"}


async def run_arm(monkeypatch, system, group="lpar-processor-quantities"):
    from live_test import processor_quantities as arm

    @asynccontextmanager
    async def client_context(config):
        system_entry = processor_system()
        if system.fault == "floor":
            system_entry["Resource"]["AssociatedSystemProcessorConfiguration"][
                "MinimumProcessorUnitsPerVirtualProcessor"
            ] = "0.20"
        yield SimpleNamespace(
            config=config,
            get_managed_system=AsyncMock(return_value=system_entry),
            list_system_summaries=AsyncMock(
                return_value=[{"UUID": system_entry["UUID"], "SystemName": "sys-R1"}]
            ),
        )

    async def create(client, selected, creation):
        system.creations.append(creation)
        system.name, system.token = creation.name, creation.caller_token
        return LparCreationResult(True, {"UUID": UUID}, system.fault != "stamp", ())

    async def authorize(client, lpar, *, system_name_or_uuid):
        assert lpar == UUID and system_name_or_uuid == "sys-R1"
        return "sys-R1", system.name

    async def apply(*args):
        if system.fault == "setup-refused":
            raise HMCError("setup refused")
        system.applies += 1

    async def native(*args):
        system.reads += 1
        if system.fault == "native-read":
            raise HMCError("native output overflow")
        row = processor_row()
        row.update(lpar_name=system.name, curr_proc_units=str(system.units))
        if system.fault in {"setup", "setup-refused"} and not system.applies:
            row.update(
                {
                    key: "0"
                    for key in row
                    if key.startswith("curr_") and key != "curr_proc_mode"
                }
            )
        if system.fault == "native-tail" and system.changes:
            row["curr_proc_units"] = str(system.units) + "0" * 28 + "1"
        if system.fault == "native-drift" and system.changes:
            row["curr_proc_units"] = "0.55"
        return row

    async def call(state, client, tool, **kwargs):
        return system.answer(tool, kwargs)

    for name, value in {
        "HMCClient": client_context,
        "create_and_stamp_lpar": create,
        "resolve_and_authorize_lpar_names": authorize,
        "apply_lpar_profile_via_cli": apply,
        "read_lpar_processor_quantities": native,
    }.items():
        monkeypatch.setattr(arm, name, value)
    monkeypatch.setattr(runner.RunState, "call", call)
    monkeypatch.setattr(lpar_config, "_ABSENCE_REREAD_DELAY_S", 0)
    monkeypatch.setattr(lpar_config, "_COMPARE_REREAD_DELAY_S", 0)
    state = runner.RunState(
        config=config.LiveTestConfig(system_name="sys-R1"),
        group=group,
        hmc_config=HMCConfig.from_mapping({}),
    )
    await arm.exercise_processor_quantities(object(), state)
    return state


@pytest.mark.asyncio
async def test_owned_processor_sequence_and_cleanup(monkeypatch):
    system = ScriptedSystem()
    state = await run_arm(monkeypatch, system)
    assert [
        (c["operation"], Decimal(str(c["processing_units"]))) for c in system.changes
    ] == [
        ("remove", Decimal("0.10")),
        ("add", 0),
        ("add", Decimal("1.10")),
    ]
    assert system.units == Decimal("0.40") and system.deleted and system.reads >= 5
    assert (
        system.creations[0].stamp_policy == "required"
        and system.creations[0].apply_profile is False
    )
    assert (
        system.creations[0].resources.desired_vcpus == 2
        and system.creations[0].resources.max_vcpus == 4
    )
    observation = state.observations[0]["observation"]
    assert observation["result"] == observation["cleanup"] == "passed"
    assert state.results[-1]["data"]["positive_add"] == "not-run"
    assert state.results[-1]["data"]["set"] == "not-run"


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_units", ["0.50", "0.40"])
async def test_inactive_set_rejection_never_becomes_positive_evidence(
    monkeypatch, expected_units
):
    from live_test import processor_quantities as arm

    # The first native arm rejected set0.50 and independently remained at0.40.
    current = (*arm._INITIAL[:4], "0.40", *arm._INITIAL[5:])
    expected = (*arm._INITIAL[:4], expected_units, *arm._INITIAL[5:])
    run = SimpleNamespace(uuid=UUID, token="caller-R1", system="sys-R1")
    data = {"outcome": "rejected", "workflow_completed": False}
    state = runner.RunState(hmc_config=HMCConfig.from_mapping({}))
    call = AsyncMock(return_value=("PASS", data))
    read = AsyncMock(return_value=current)
    monkeypatch.setattr(state, "call", call)
    monkeypatch.setattr(arm.fixture, "_token_of", AsyncMock(return_value=run.token))
    monkeypatch.setattr(arm, "_read_current", read)

    assert not await arm._case(None, state, run, "set", "0.50", expected, "applied")
    assert state.results[-1]["status"] == "FAIL"
    assert call.await_count == 1 and read.await_count == 2
    assert data == {"outcome": "rejected", "workflow_completed": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "foreign",
        "active",
        "stamp",
        "mixed",
        "setup-refused",
        "native-read",
        "ignored",
        "partial",
        "rejected",
        "exception",
        "memory-drift",
        "native-drift",
        "rest-tail",
        "native-tail",
        "wrong-refusal",
        "delete",
    ],
)
async def test_processor_faults_never_pass_and_cleanup_is_guarded(monkeypatch, fault):
    system = ScriptedSystem(fault=fault)
    state = await run_arm(monkeypatch, system)
    assert not any(
        row["observation"]["result"] == "passed" for row in state.observations
    )
    if fault in {"foreign", "active", "delete"}:
        assert not system.deleted
    else:
        assert system.deleted
    if fault in {"foreign", "active", "stamp", "mixed", "setup-refused", "native-read"}:
        assert not system.changes
    elif fault not in {"wrong-refusal", "delete"}:
        assert len(system.changes) == 1


@pytest.mark.asyncio
async def test_processor_setup_is_once_and_requires_all_zero_cpu_and_memory(
    monkeypatch,
):
    system = ScriptedSystem(fault="setup")
    state = await run_arm(monkeypatch, system)
    assert system.applies == 1 and system.deleted
    assert state.observations[0]["observation"]["result"] == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault,group",
    [
        ("stranded", "lpar-processor-quantities"),
        ("floor", "lpar-processor-quantities"),
        ("none", "all"),
    ],
)
async def test_processor_preconditions_prevent_creation(monkeypatch, fault, group):
    system = ScriptedSystem(fault=fault)
    await run_arm(monkeypatch, system, group)
    assert not system.creations and not system.changes


def test_processor_group_is_named_only():
    from live_test import processor_quantities as arm

    assert runner.SUBTASK_GROUPS[arm.GROUP] == [56]
    assert runner.SUBTASKS[56] is arm.exercise_processor_quantities
    assert 56 not in runner.SUBTASK_GROUPS["all"]


@pytest.mark.asyncio
async def test_processor_overmaximum_oracle_through_actual_mcp(
    monkeypatch,
    processor_boundary,  # noqa: F811 -- imported pytest fixture
):
    from fastmcp import Client, FastMCP
    from live_test import processor_quantities as arm
    from lpar.test_processor_quantities import LPAR_UUID, SYSTEM_UUID

    from hmcpctl.server_tools.lpar import lifecycle

    b = processor_boundary
    run = lpar_config._Run("sys-R1", "lpar-R1", "lparcfg-a1b2c3d4", 256, subtask=56)
    run.uuid = LPAR_UUID
    stamp = f"[hmcpctl owner:hmcpctl created:2026-01-01] [caller {run.token}]"
    b.description.return_value = stamp
    monkeypatch.setattr(arm, "read_lpar_processor_quantities", b.read)
    monkeypatch.setattr(
        lifecycle, "with_client", lambda fn, **kwargs: asyncio.run(fn(b.hmc))
    )
    server = FastMCP("processor admission boundary")
    server.tool(lifecycle.hmc_dlpar_proc)

    @server.tool
    async def hmc_get_lpar(lpar_name_or_uuid: str, system_name_or_uuid: str) -> dict:
        assert lpar_name_or_uuid == LPAR_UUID and system_name_or_uuid == "sys-R1"
        return await b.hmc.get_logical_partition(lpar_name_or_uuid)

    @server.tool
    async def hmc_get_lpar_description(
        lpar_name_or_uuid: str, system_name_or_uuid: str
    ) -> str:
        assert lpar_name_or_uuid == LPAR_UUID and system_name_or_uuid == "sys-R1"
        return stamp

    state = runner.RunState(hmc_config=HMCConfig.from_mapping({}))
    async with Client(server) as client:
        assert await arm._case(
            client, state, run, "add", "1.10", arm._INITIAL, "refused"
        )
    assert state.results[-1]["data"]["result"].message == REFUSAL
    b.write.assert_not_awaited()
    b.hmc.get_managed_system.assert_awaited_once_with(SYSTEM_UUID)


@pytest.mark.parametrize(
    "value,units",
    [
        ("NaN", True),
        ("Infinity", True),
        ("-Infinity", True),
        ("sNaN", True),
        ("1e-1000", True),
        ("-1", True),
        ("0.001", True),
        (None, True),
        ("0.5", False),
    ],
)
def test_processor_oracle_rejects_malformed_quantities(value, units):
    from live_test import processor_quantities as arm

    with pytest.raises(ValueError):
        arm._number(value, units=units)


@pytest.mark.asyncio
async def test_processor_arm_is_not_selected_by_default(monkeypatch, tmp_path):
    from test_live_runner import _isolate_runner

    _isolate_runner(monkeypatch)

    async def ordinary(client, state):
        state.record(0, "ordinary", "PASS", {})

    forbidden = AsyncMock(
        side_effect=AssertionError("processor arm dispatched without named group")
    )
    monkeypatch.setattr(runner, "SUBTASKS", {0: ordinary, 56: forbidden})
    assert (
        await runner.main(
            results_path=str(tmp_path / "results.json"),
            config=config.LiveTestConfig(system_name="sys-R1"),
            hmc_config=HMCConfig.from_mapping({}),
        )
        == 0
    )
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("precision", [1, 28, 60])
@pytest.mark.parametrize(
    "value,units",
    [
        ("0.400000000000000000000000000001", True),
        ("1.000000000000000000000000000001", False),
    ],
)
def test_processor_oracle_rejects_exact_fractional_tails(precision, value, units):
    from live_test import processor_quantities as arm

    with localcontext() as context:
        context.prec = precision
        with pytest.raises(ValueError, match="precision"):
            arm._number(value, units=units)


@pytest.mark.parametrize("precision", [1, 28])
@pytest.mark.parametrize(
    "value,units,expected",
    [
        ("0.400000000000000000000000000000", True, "0.40"),
        ("1.000000000000000000000000000000", False, "1"),
        ("4e-1", True, "0.40"),
        ("1.23e2", False, "123"),
        ("123.45", True, "123.45"),
        ("0e1058", True, "0.00"),
        ("-0e-1058", True, "-0.00"),
        ("0e1058", False, "0"),
    ],
)
def test_processor_oracle_preserves_exact_representations(
    precision, value, units, expected
):
    from live_test import processor_quantities as arm

    with localcontext() as context:
        context.prec = precision
        assert arm._number(value, units=units) == expected


@pytest.mark.parametrize("units,digits", [(False, 1055), (True, 1052)])
def test_processor_oracle_bounds_rendered_numeric_strings(units, digits):
    from live_test import processor_quantities as arm

    expected = "1" + "0" * (digits - 1) + (".00" if units else "")
    assert arm._number(f"1e{digits - 1}", units=units) == expected
    assert len(expected.encode()) + 2 == 1057  # JSON string quotes count too.
    with pytest.raises(ValueError, match="budget"):
        arm._number(f"1e{digits}", units=units)


@pytest.mark.parametrize("units", [False, True])
@pytest.mark.parametrize("oversized", ["0" * 1058, "٠" * 529])
def test_processor_oracle_bounds_input_before_decimal_parse(units, oversized):
    from live_test import processor_quantities as arm

    assert arm._number("0" * 1057, units=units) == ("0.00" if units else "0")
    with pytest.raises(ValueError, match="budget"):
        arm._number(oversized, units=units)
