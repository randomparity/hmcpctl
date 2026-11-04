"""The logical-workflow live arm (ST73, #1854) against a scripted HMC.

It reuses the lpar-power arm's scripted system and adds what the workflow touches:
an inactive provision, a reconfigure, an inspection and current figures after start.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import test_live_lpar_power_arm as arm
from live_test import config as live_config
from live_test import logical_workflow, runner
from test_live_lpar_power_arm import GROUP, SYSTEM, VLAN, FakeClient, World

from hmcpctl.operations.logical.models import OperationRecord
from hmcpctl.server_tools.catalog import PRIMARY_TOOLS

PATCH = logical_workflow.PATCH


@pytest.fixture(scope="module")
def schemas():
    return arm.schemas.__wrapped__()


@pytest.fixture(scope="module")
def output_schemas():
    return arm.output_schemas.__wrapped__()


@pytest.fixture(autouse=True)
def _typed_outputs(output_schemas):
    """Served results arrive typed, as the live client hands them to the arm."""
    arm._OUTPUTS.update(output_schemas)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    lpar_power = logical_workflow.lpar_power
    for name in (
        "_STATE_POLL_DELAY_S",
        "_OPERATION_POLL_DELAY_S",
        "_COMPARE_REREAD_DELAY_S",
    ):
        monkeypatch.setattr(lpar_power, name, 0)
    monkeypatch.setattr(lpar_power.dedicated_pcie, "_ABSENCE_REREAD_DELAY_S", 0)
    monkeypatch.setattr(lpar_power, "_STATE_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(logical_workflow, "_power_operations_authorized", lambda: True)


def _operation(tool: str, state: str, outcome: str | None, result: Any) -> dict:
    record = asdict(
        OperationRecord(
            "1" * 32,
            "r",
            tool,
            "<default>",
            None,
            None,
            state,
            "done",
            outcome,
            (),
            (),
            False,
            (),
            (),
            None,
            "t",
            "t",
        )
    )
    return {**record, "result": result}


class WorkflowWorld(World):
    """Answers the logical workflow as a healthy HMC would."""

    def __init__(self) -> None:
        super().__init__()
        self.applied = "profile"
        self.changed_fields = {"memory_mib", "processors"}
        self.reconfigure_state = "terminal"
        self.start_applies_change = True
        self.inspected_state = None
        self.inspected_memory = None

    def _hmc_provision_lpar(self, kwargs: dict[str, Any]) -> Any:
        answer = super()._hmc_provision_lpar(kwargs)
        if kwargs.get("continuation") is None:
            # power_on false: the partition stays as created.
            self.partitions[kwargs["name"]]["state"] = "Not Activated"
            answer["steps"] = [s for s in answer["steps"] if s["step"] != "power_on"]
        return answer

    def _hmc_reconfigure_lpar(self, kwargs: dict[str, Any]) -> Any:
        request_id = kwargs["request_id"]
        if kwargs.get("continuation") == "abandon":
            record = self.operations[request_id]
            record["state"] = "terminal"
            return record
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        assert entry is not None
        patch = kwargs["patch"]
        entry["resources"] = {
            **entry["resources"],
            "desired_memory": patch["memory_mib"],
            "desired_vcpus": patch["processors"],
        }
        changes = [
            {"field": key, "applied": self.applied, "after": value}
            for key, value in patch.items()
            if key in self.changed_fields
        ]
        outcome = "completed" if self.reconfigure_state == "terminal" else None
        record = _operation(
            "hmc_reconfigure_lpar",
            self.reconfigure_state,
            outcome,
            {"changes": changes},
        )
        record["request_id"] = request_id
        self.operations[request_id] = record
        return record

    def _hmc_inspect_lpar(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        assert entry is not None
        r = entry["resources"]
        return {
            "connection": "<default>",
            "id": "x",
            "system_uuid": "s",
            "uuid": entry["uuid"],
            "name": self.name_of(entry),
            "partition_id": 9,
            # PartitionState as the HMC spells it, not lower-cased.
            "state": self.inspected_state or entry["state"],
            "rmc": {"source": {"status": "ok", "tool": "hmc_get_lpar"}, "state": None},
            "resources": {
                "current_memory_mib": None,
                "desired_memory_mib": self.inspected_memory or r["desired_memory"],
                "current_proc_units": None,
                "desired_proc_units": r["desired_procs"],
                "desired_vcpus": r["desired_vcpus"],
                "has_dedicated_processors": False,
                "storage_source": {"status": "ok", "tool": "hmc_list_vios"},
                "vios": [{"uuid": arm.VIOS_UUID, "status": "ok"}],
                "mappings": [
                    {
                        "vios_uuid": arm.VIOS_UUID,
                        "type": "VirtualSCSIMapping",
                        "uuid": "m",
                        "backing_device": m["backing_name"],
                    }
                    for m in self.mappings
                    if m["lpar_uuid"] == entry["uuid"]
                ],
                "unresolved_mappings": 0,
            },
            "refcodes": None,
            "profile_drift": None,
            "next_actions": [],
        }

    def _hmc_get_lpar(self, kwargs: dict[str, Any]) -> Any:
        answer = super()._hmc_get_lpar(kwargs)
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        if entry is None or entry["state"] == "Not Activated":
            return answer
        r = entry["resources"]
        current = self.start_applies_change
        resource = answer["Resource"]
        resource["PartitionMemoryConfiguration"]["CurrentMemory"] = str(
            r["desired_memory"] if current else 2048
        )
        resource["PartitionProcessorConfiguration"][
            "CurrentSharedProcessorConfiguration"
        ] = {"AllocatedVirtualProcessors": str(r["desired_vcpus"] if current else 1)}
        return answer


def _run(schemas, world: World, group: str = logical_workflow.GROUP, **config: Any):
    state = runner.RunState(
        config=live_config.LiveTestConfig(
            system_name=SYSTEM,
            vdisk_volume_group_name=GROUP,
            provision_vlan_id=VLAN,
            **{"partition_profile_writer_window": True, **config},
        ),
        group=group,
    )
    state.schemas = schemas
    asyncio.run(
        logical_workflow.exercise_logical_workflow(
            FakeClient(world, arm._OUTPUTS), state
        )
    )
    return state


def _results(state) -> dict[str, str]:
    return {e["operation"]: e["observation"]["result"] for e in state.observations}


def _held(state, operation: str) -> list[str]:
    (entry,) = [e for e in state.observations if e["operation"] == operation]
    return entry["observation"]["assertions"]


_OPERATIONS = {
    "provision.lpar",
    "lpar.reconfigure",
    "lpar.inspect",
    "lpar.power",
    "lpar.decommission",
}


def test_the_workflow_tools_are_the_primary_catalogs():
    assert logical_workflow.WORKFLOW_TOOLS <= PRIMARY_TOOLS


def test_a_clean_workflow_passes_every_operation_and_leaves_nothing(schemas):
    world = WorkflowWorld()

    state = _run(schemas, world)

    assert _results(state) == dict.fromkeys(_OPERATIONS, "passed")
    for operation, names in logical_workflow._ASSERTIONS.items():
        assert sorted(_held(state, operation)) == sorted(names)
    assert world.partitions == {}
    assert world.volumes == {"hd5", "lv_op"}
    (sent,) = world.calls_to("hmc_provision_lpar")
    assert sent["power_on"] is False and sent["exclusive_writer_window"] is True
    (patched,) = world.calls_to("hmc_reconfigure_lpar")
    assert patched["patch"] == PATCH
    actions = [kwargs["action"] for kwargs in world.calls_to("hmc_power_lpar")]
    assert actions == ["start", "stop"]


#: The read-only tools the arm may call beside the workflow: its independent witnesses.
WITNESSES = frozenset(
    {
        "hmc_list_vios",
        "hmc_list_volume_groups",
        "hmc_list_virtual_networks",
        "hmc_list_storage_mappings",
        "hmc_get_vios_storage_detail",
        "hmc_get_lpar",
        "hmc_get_lpar_state",
        "hmc_get_lpar_description",
        "hmc_list_adapters",
        "hmc_run_command",
    }
)


def test_a_clean_run_calls_only_workflow_tools_and_witness_reads(schemas):
    """No specialist choreography stands in for a workflow step."""
    world = WorkflowWorld()

    _run(schemas, world)

    called = set(world.tools())
    assert called - logical_workflow.WORKFLOW_TOOLS <= WITNESSES
    # hmc_operation_status is polled only for an operation the call left running.
    assert logical_workflow.WORKFLOW_TOOLS - {"hmc_operation_status"} <= called
    commands = [kwargs["cmd"] for kwargs in world.calls_to("hmc_run_command")]
    assert all(
        cmd.split()[0] in {"lssyscfg", "lshwres", "viosvrcmd"} for cmd in commands
    )
    assert not any(
        verb in cmd for cmd in commands for verb in ("chsyscfg", "chhwres", "rmsyscfg")
    )


def test_outside_its_group_it_calls_nothing(schemas):
    world = WorkflowWorld()

    state = _run(schemas, world, group="lpar-power")

    assert world.calls == []
    assert state.observations == []


def test_without_the_writer_window_it_writes_nothing(schemas):
    world = WorkflowWorld()

    _run(schemas, world, partition_profile_writer_window=False)

    assert world.calls == []


def test_a_stranded_run_partition_stops_it_before_any_write(schemas):
    world = WorkflowWorld()
    world._new("hmcpctl-live-pwr-00000000", "lparpwr-00000000", {})

    _run(schemas, world)

    assert not set(world.tools()) & arm.MUTATIONS


def test_a_change_not_classified_profile_ends_the_workflow_and_cleans_up(schemas):
    world = WorkflowWorld()
    world.applied = "pending_activation"

    state = _run(schemas, world)

    assert _results(state) == {
        "provision.lpar": "passed",
        "lpar.reconfigure": "failed",
    }
    assert "hmc_power_lpar" not in world.tools()
    assert world.partitions == {} and world.volumes == {"hd5", "lv_op"}


def test_an_unsettled_reconfigure_is_abandoned_in_teardown(schemas):
    world = WorkflowWorld()
    world.reconfigure_state = "running"

    _run(schemas, world)

    abandons = [
        kwargs
        for kwargs in world.calls_to("hmc_reconfigure_lpar")
        if kwargs.get("continuation") == "abandon"
    ]
    assert len(abandons) == 1
    assert world.partitions == {}


def test_a_start_without_the_change_fails_that_assertion(schemas):
    world = WorkflowWorld()
    world.start_applies_change = False

    state = _run(schemas, world)

    assert "started-with-the-change" not in _held(state, "lpar.power")
    assert _results(state)["lpar.power"] == "failed"


def test_an_inspection_reading_another_state_fails_that_assertion(schemas):
    world = WorkflowWorld()
    world.inspected_state = "Running"

    state = _run(schemas, world)

    assert "reports-the-change-inactive" not in _held(state, "lpar.inspect")
    assert _results(state)["lpar.inspect"] == "failed"


def test_an_inspection_without_the_new_memory_fails_that_assertion(schemas):
    world = WorkflowWorld()
    world.inspected_memory = 2048

    state = _run(schemas, world)

    assert "reports-the-change-inactive" not in _held(state, "lpar.inspect")


def test_an_inspection_listing_only_a_foreign_mapping_fails_that_assertion(schemas):
    world = WorkflowWorld()
    real = world._hmc_inspect_lpar
    world.overrides["hmc_inspect_lpar"] = lambda kwargs: {
        **(answer := real(kwargs)),
        "resources": {**answer["resources"], "mappings": [{"backing_device": "lv_op"}]},
    }

    state = _run(schemas, world)

    assert "reports-the-owned-mapping" not in _held(state, "lpar.inspect")


def _changes(world: WorkflowWorld, tool: str, change) -> None:
    real = getattr(world, "_" + tool)
    world.overrides[tool] = lambda kwargs: change(kwargs, real(kwargs))


def _foreign_token(world: WorkflowWorld) -> None:
    _changes(
        world,
        "hmc_get_lpar_description",
        lambda kwargs, answer: answer.replace("lparpwr-", "lparcfg-"),
    )


def _left_running(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        world.partitions[kwargs["name"]]["state"] = "Open Firmware"
        return answer

    _changes(world, "hmc_provision_lpar", change)


def _mapping_unlisted(world: WorkflowWorld) -> None:
    world.overrides["hmc_list_storage_mappings"] = lambda kwargs: [
        dict(item) for item in world.mappings if item["backing_name"] == "lv_op"
    ]


def _needs_attention(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        if kwargs.get("continuation") is None:
            answer["operation"]["outcome"] = "needs_attention"
        return answer

    _changes(world, "hmc_provision_lpar", change)


def _storage_unavailable(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        answer["resources"]["storage_source"] = {
            "status": "unavailable",
            "tool": "hmc_list_vios",
        }
        return answer

    _changes(world, "hmc_inspect_lpar", change)


def _start_already_running(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        if kwargs.get("action") == "start":
            answer["result"]["already_in_state"] = True
        return answer

    _changes(world, "hmc_power_lpar", change)


def _inspected_vcpus(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        answer["resources"]["desired_vcpus"] = 1
        return answer

    _changes(world, "hmc_inspect_lpar", change)


def _current_vcpus(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        resource = answer.get("Resource") if isinstance(answer, dict) else None
        shared = (resource or {}).get("PartitionProcessorConfiguration", {})
        if "CurrentSharedProcessorConfiguration" in shared:
            shared["CurrentSharedProcessorConfiguration"] = {
                "AllocatedVirtualProcessors": "1"
            }
        return answer

    _changes(world, "hmc_get_lpar", change)


def _never_in_firmware(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        return "starting" if answer in {"running", "open firmware"} else answer

    _changes(world, "hmc_get_lpar_state", change)


def _stop_already_off(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        if kwargs.get("action") == "stop":
            answer["result"]["already_in_state"] = True
        return answer

    _changes(world, "hmc_power_lpar", change)


def _mixed_classification(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        if kwargs.get("continuation") is None:
            answer["result"]["changes"][0]["applied"] = "live"
        return answer

    _changes(world, "hmc_reconfigure_lpar", change)


def _processors_unplanned(world: WorkflowWorld) -> None:
    world.changed_fields = {"memory_mib"}


@pytest.mark.parametrize(
    ("change", "operation", "assertion"),
    [
        (_inspected_vcpus, "lpar.inspect", "reports-the-change-inactive"),
        (_current_vcpus, "lpar.power", "started-with-the-change"),
        (_never_in_firmware, "lpar.power", "started-with-the-change"),
        (_stop_already_off, "lpar.power", "stop-immediate-completed-not-activated"),
        (
            _mixed_classification,
            "lpar.reconfigure",
            "inactive-change-classified-profile",
        ),
        (
            _processors_unplanned,
            "lpar.reconfigure",
            "inactive-change-classified-profile",
        ),
        (_foreign_token, "provision.lpar", "ownership-stamped"),
        (_left_running, "provision.lpar", "left-not-activated"),
        (_mapping_unlisted, "provision.lpar", "storage-mapping-listed"),
        (_needs_attention, "provision.lpar", "workflow-completed"),
        (_storage_unavailable, "lpar.inspect", "reports-the-owned-mapping"),
        (_start_already_running, "lpar.power", "start-completed-activated"),
    ],
)
def test_each_unmet_postcondition_fails_its_assertion(
    schemas, change, operation, assertion
):
    world = WorkflowWorld()
    change(world)

    state = _run(schemas, world)

    assert assertion not in _held(state, operation)
    assert _results(state)[operation] == "failed"


def test_the_stop_is_immediate_and_the_start_states_no_mode(schemas):
    """A graceful stop needs RMC, which a partition with no OS never has."""
    world = WorkflowWorld()

    _run(schemas, world)

    modes = {k["action"]: k.get("mode") for k in world.calls_to("hmc_power_lpar")}
    assert modes == {"start": None, "stop": "immediate"}


def test_a_refused_reconfigure_is_looked_up_once_and_not_abandoned(schemas):
    world = WorkflowWorld()
    world.overrides["hmc_reconfigure_lpar"] = lambda kwargs: ValueError("refused")

    state = _run(schemas, world)

    assert len(world.calls_to("hmc_operation_status")) == 1
    assert len(world.calls_to("hmc_reconfigure_lpar")) == 1
    assert _results(state)["lpar.reconfigure"] == "failed"
    assert world.partitions == {}


def test_a_paused_provision_is_abandoned_with_its_own_tool(schemas):
    world = WorkflowWorld()
    world.provision_fails_after_storage = True

    _run(schemas, world)

    abandons = [
        kwargs
        for kwargs in world.calls_to("hmc_provision_lpar")
        if kwargs.get("continuation") == "abandon"
    ]
    assert len(abandons) == 1
    assert "hmc_reconfigure_lpar" not in world.tools()


def test_a_reconfigure_that_will_not_abandon_fails_the_cleanup(schemas):
    world = WorkflowWorld()
    world.reconfigure_state = "running"
    real = world._hmc_reconfigure_lpar

    def refuse_abandon(kwargs):
        if kwargs.get("continuation") == "abandon":
            return ValueError("operation is running; abandon is refused")
        return real(kwargs)

    world.overrides["hmc_reconfigure_lpar"] = refuse_abandon

    state = _run(schemas, world)

    (entry,) = [e for e in state.observations if e["operation"] == "provision.lpar"]
    assert entry["observation"]["cleanup"] == "failed"


def test_a_paused_stop_skips_the_decommission_and_teardown_removes_all(schemas):
    world = WorkflowWorld()
    world.composite_pauses = True

    state = _run(schemas, world)

    assert "lpar.decommission" not in _results(state)
    assert "hmc_decommission_lpar" not in world.tools()
    assert world.partitions == {} and world.volumes == {"hd5", "lv_op"}


def test_an_abandon_that_raises_still_tears_the_run_down(schemas, monkeypatch):
    async def raises(*_args):
        raise RuntimeError("abandon blew up")

    monkeypatch.setattr(logical_workflow, "_abandon_reconfigure", raises)
    world = WorkflowWorld()
    world.applied = "pending_activation"

    with pytest.raises(RuntimeError):
        _run(schemas, world)

    assert world.partitions == {} and world.volumes == {"hd5", "lv_op"}
