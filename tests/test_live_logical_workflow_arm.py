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
from live_test import runner
from live_test.arms import logical_workflow
from test_live_lpar_power_arm import GROUP, SYSTEM, VLAN, FakeClient, World

from hmcpctl.operations.logical.models import OperationRecord
from hmcpctl.server_tools.catalog import PRIMARY_TOOLS

PATCH = logical_workflow.PATCH
SAVED = "hmcpctl_000000000000"
SYSTEM_UUID = "5A5A5A5A-0000-4000-8000-000000000386"


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
        # ADR 0331: provision applies its saved profile, so both name it.
        self.last_activated = SAVED
        #: partition UUID -> (consumer label, hold_id), as the ADR 0193 store keeps it.
        self.holds: dict[str, tuple[str, str]] = {}
        self.hold_enforced = True

    def _hmc_plan_lpar(self, kwargs: dict[str, Any]) -> Any:
        system = {"id": f"c/{SYSTEM_UUID}", "uuid": SYSTEM_UUID, "name": SYSTEM}
        targets = {"system": system, "vios": None, "volume_group": None}
        return {
            "connection": "<default>",
            "plan_digest": "d" * 64,
            "selected": targets,
            "blockers": [],
            "candidates": [
                {
                    "targets": targets,
                    "selector": SYSTEM,
                    "free_memory_mib": 100000,
                    "free_proc_units": 8.0,
                    "blockers": [],
                    "blockers_truncated": False,
                }
            ],
            "candidates_limit": 16,
            "candidates_truncated": False,
            "intended_changes": [],
            "unverified": [],
        }

    def _hmc_inventory(self, kwargs: dict[str, Any]) -> Any:
        ok = {"status": "ok", "tool": "hmc_list_systems", "detail": None}
        system_id = f"c/{SYSTEM_UUID}"
        return {
            "connection": "<default>",
            "systems_source": None,
            "systems": [
                {
                    "id": system_id,
                    "selector": SYSTEM,
                    "uuid": SYSTEM_UUID,
                    "name": SYSTEM,
                    "state": "operating",
                    "total_memory_mib": None,
                    "free_memory_mib": None,
                    "total_proc_units": None,
                    "free_proc_units": None,
                    "sources": {"capacity": ok, "partitions": ok, "ownership": ok},
                }
            ],
            "systems_limit": 16,
            "systems_truncated": False,
            "partitions": [
                {
                    "id": f"{system_id}/{entry['uuid']}",
                    "system_id": system_id,
                    "uuid": entry["uuid"].lower(),
                    "name": name,
                    "partition_id": 9,
                    "partition_type": "AIX/Linux",
                    "state": entry["state"].lower(),
                    "rmc_state": None,
                    "current_memory_mib": None,
                    "current_proc_units": None,
                    "has_dedicated_processors": False,
                    "owned": True,
                    "owner": "agent",
                }
                for name, entry in self.partitions.items()
            ],
            "limit": 50,
            "truncated": False,
            "next_cursor": None,
        }

    def _hmc_prepare_host_handoff(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        assert entry is not None
        lpar = entry["uuid"]
        if kwargs["action"] == "release":
            if lpar not in self.holds:
                return ValueError("no_hold: the partition is not held")
            label, _ = self.holds.pop(lpar)
            return {
                "action": "release",
                "hold": self._view(label, None),
                "document": None,
            }
        if kwargs.get("hold"):
            label = kwargs["consumer_label"]
            held = self.holds.setdefault(lpar, (label, f"{len(self.holds):032x}"))
            if held[0] != label:
                return ValueError("partition_held: another label holds it")
        hold = self.holds.get(lpar)
        view = None if hold is None else self._view(*hold)
        inspected = self._hmc_inspect_lpar(kwargs)
        return {
            "action": "prepare",
            "hold": view,
            "document": {
                "identity": {
                    "connection": "<default>",
                    "system_uuid": "s",
                    "partition_uuid": lpar.lower(),
                    "name": self.name_of(entry),
                    "partition_id": 9,
                },
                "references": {"connection": "<default>", "agent_id": "agent"},
                "observed": {
                    "state": entry["state"],
                    "rmc_state": None,
                    "resources": inspected["resources"],
                    "ownership": {"owned": True, "owner": "agent", "unparsed": False},
                    "adapters": {
                        "source": {"status": "ok", "tool": "hmc_list_adapters"},
                        "items": [],
                    },
                },
                "declared": {"source": "none", "detail": None},
                "unverified": [],
                "hold": None if view is None else {**view, "hold_id": None},
                "unfenced": ["library API"],
                "unchecked_tools": [],
            },
        }

    @staticmethod
    def _view(label: str, hold_id: str | None) -> dict[str, Any]:
        return {
            "consumer_label": label,
            "agent_id": "agent",
            "created_at": "t",
            "hold_id": hold_id,
        }

    def _hmc_invoke_tool(self, kwargs: dict[str, Any]) -> Any:
        result = self.respond(kwargs["name"], kwargs["arguments"])
        if isinstance(result, Exception):
            return result
        return {"name": kwargs["name"], "result": result}

    def _hmc_power_lpar(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs.get("lpar_name_or_uuid"))
        hold = None if entry is None else self.holds.get(entry["uuid"])
        if self.hold_enforced and hold and kwargs.get("hold_id") != hold[1]:
            record = _operation("hmc_power_lpar", "terminal", "failed", None)
            record["request_id"] = kwargs["request_id"]
            record["warnings"] = [{"code": "partition_held", "message": "held"}]
            self.operations[kwargs["request_id"]] = record
            return record
        return super()._hmc_power_lpar(kwargs)

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
        if entry is None:
            return answer
        answer["Resource"]["DefaultProfileName"] = SAVED
        answer["Resource"]["LastActivatedProfile"] = self.last_activated
        if entry["state"] == "Not Activated":
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
    "lpar.plan",
    "inventory.logical",
    "provision.lpar",
    "lpar.reconfigure",
    "lpar.inspect",
    "lpar.prepare_host_handoff",
    "tools.invoke",
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
    # The first start is the held one, dispatched through hmc_invoke_tool and refused.
    assert actions == ["start", "start", "stop"]


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
        "lpar.plan": "passed",
        "provision.lpar": "passed",
        "inventory.logical": "passed",
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


def _last_is_the_creation_profile(world: WorkflowWorld) -> None:
    world.last_activated = "default_profile"


def _vscsi_adapter_gone(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        return [] if kwargs["adapter_type"] == "VirtualSCSIClientAdapter" else answer

    _changes(world, "hmc_list_adapters", change)


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


def _still_on_after_stop(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        if kwargs.get("action") == "stop":
            world.by_selector(kwargs["lpar_name_or_uuid"])["state"] = "Open Firmware"
        return answer

    _changes(world, "hmc_power_lpar", change)


@pytest.mark.parametrize(
    ("change", "operation", "assertion"),
    [
        (
            _still_on_after_stop,
            "lpar.power",
            "stop-immediate-completed-not-activated",
        ),
        (_last_is_the_creation_profile, "provision.lpar", "last-activated-is-default"),
        (_vscsi_adapter_gone, "provision.lpar", "adapters-after-apply"),
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


def test_a_partition_still_on_after_the_stop_is_not_decommissioned(schemas):
    world = WorkflowWorld()
    _still_on_after_stop(world)

    _run(schemas, world)

    assert "hmc_decommission_lpar" not in world.tools()


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


def test_the_hold_is_placed_proved_through_the_gateway_and_released(schemas):
    world = WorkflowWorld()

    _run(schemas, world)

    handoffs = [
        (k["action"], k.get("hold", False))
        for k in world.calls_to("hmc_prepare_host_handoff")
    ]
    assert handoffs == [
        ("prepare", True),
        ("prepare", True),
        ("release", False),
        ("prepare", False),
    ]
    invoked = [k["name"] for k in world.calls_to("hmc_invoke_tool")]
    assert invoked == ["hmc_inspect_lpar", "hmc_power_lpar"]
    held_start = world.calls_to("hmc_power_lpar")[0]
    assert held_start["action"] == "start" and "hold_id" not in held_start
    assert world.holds == {}


def test_a_hold_that_lets_the_start_through_fails_both_assertions(schemas):
    world = WorkflowWorld()
    world.hold_enforced = False

    state = _run(schemas, world)

    assert "hold-refuses-a-mutation" not in _held(state, "lpar.prepare_host_handoff")
    assert "dispatched-mutation-meets-the-hold" not in _held(state, "tools.invoke")
    assert world.partitions == {} and world.volumes == {"hd5", "lv_op"}


def test_a_document_carrying_the_hold_id_fails_that_assertion(schemas):
    world = WorkflowWorld()

    def change(kwargs, answer):
        if isinstance(answer, dict) and answer.get("document") and answer["hold"]:
            answer["document"]["hold"] = dict(answer["hold"])
        return answer

    _changes(world, "hmc_prepare_host_handoff", change)

    state = _run(schemas, world)

    assert "document-withholds-hold-id" not in _held(state, "lpar.prepare_host_handoff")


def test_a_refused_release_stops_the_workflow_and_teardown_retries_it(schemas):
    world = WorkflowWorld()
    real = world._hmc_prepare_host_handoff
    refusals = iter([True])

    def refuse_first_release(kwargs):
        if kwargs["action"] == "release" and next(refusals, False):
            return ValueError("store unavailable")
        return real(kwargs)

    world.overrides["hmc_prepare_host_handoff"] = refuse_first_release

    state = _run(schemas, world)

    assert "release-drops-the-hold" not in _held(state, "lpar.prepare_host_handoff")
    assert [k["action"] for k in world.calls_to("hmc_power_lpar")] == ["start"]
    releases = [
        k
        for k in world.calls_to("hmc_prepare_host_handoff")
        if k["action"] == "release"
    ]
    assert len(releases) == 2
    assert world.holds == {} and world.partitions == {}


def test_a_hold_that_will_not_release_fails_the_cleanup(schemas):
    world = WorkflowWorld()
    real = world._hmc_prepare_host_handoff
    world.overrides["hmc_prepare_host_handoff"] = lambda kwargs: (
        ValueError("store unavailable")
        if kwargs["action"] == "release"
        else real(kwargs)
    )

    state = _run(schemas, world)

    (entry,) = [e for e in state.observations if e["operation"] == "provision.lpar"]
    assert entry["observation"]["cleanup"] == "failed"


def test_a_start_refused_for_another_reason_does_not_prove_the_hold(schemas):
    world = WorkflowWorld()
    world.hold_enforced = False
    real = world._hmc_power_lpar
    starts = iter([True])

    def fail_first_start(kwargs):
        if kwargs.get("action") == "start" and next(starts, False):
            return ValueError("HSCL1234 the start job failed")
        return real(kwargs)

    world.overrides["hmc_power_lpar"] = fail_first_start

    state = _run(schemas, world)

    assert "hold-refuses-a-mutation" not in _held(state, "lpar.prepare_host_handoff")
    assert "dispatched-mutation-meets-the-hold" not in _held(state, "tools.invoke")


def test_a_release_answering_with_a_document_fails_that_assertion(schemas):
    world = WorkflowWorld()

    def change(kwargs, answer):
        if kwargs["action"] == "release" and isinstance(answer, dict):
            answer["action"] = "prepare"
        return answer

    _changes(world, "hmc_prepare_host_handoff", change)

    state = _run(schemas, world)

    assert "release-drops-the-hold" not in _held(state, "lpar.prepare_host_handoff")
    assert world.holds == {} and world.partitions == {}


def test_the_plan_asks_about_provisions_own_inputs_before_it_runs(schemas):
    world = WorkflowWorld()

    _run(schemas, world)

    (plan,) = world.calls_to("hmc_plan_lpar")
    (provision,) = world.calls_to("hmc_provision_lpar")
    for key in ("name", "adapters", "storage", "resources", "system_name_or_uuid"):
        assert plan[key] == provision[key], key
    assert world.tools().index("hmc_plan_lpar") < world.tools().index(
        "hmc_provision_lpar"
    )
    assert world.tools().index("hmc_inventory") > world.tools().index(
        "hmc_provision_lpar"
    )


def _plan_digest_without_selection(world: WorkflowWorld) -> None:
    _changes(world, "hmc_plan_lpar", lambda k, a: {**a, "selected": None})


def _plan_for_another_system(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        for candidate in answer["candidates"]:
            candidate["targets"] = {
                **candidate["targets"],
                "system": {"id": "c/x", "uuid": "0" * 8, "name": "other"},
            }
        return answer

    _changes(world, "hmc_plan_lpar", change)


def _inventory_without_the_run_partition(world: WorkflowWorld) -> None:
    """Another partition of the system is listed in its place."""

    def change(kwargs, answer):
        for partition in answer["partitions"]:
            partition["uuid"] = "0" * 8 + partition["uuid"][8:]
            partition["name"] = "lpar-A"
        return answer

    _changes(world, "hmc_inventory", change)


def _inventory_partition_on_another_system(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        for partition in answer["partitions"]:
            partition["system_id"] = "c/other"
        return answer

    _changes(world, "hmc_inventory", change)


def _inventory_ownership_unavailable(world: WorkflowWorld) -> None:
    def change(kwargs, answer):
        sources = answer["systems"][0]["sources"]
        sources["ownership"] = {**sources["ownership"], "status": "unavailable"}
        return answer

    _changes(world, "hmc_inventory", change)


def _inventory_without_the_system(world: WorkflowWorld) -> None:
    _changes(world, "hmc_inventory", lambda k, a: {**a, "systems": []})


@pytest.mark.parametrize(
    ("change", "operation", "assertion"),
    [
        (_plan_digest_without_selection, "lpar.plan", "plan-outcome-consistent"),
        (_plan_for_another_system, "lpar.plan", "plan-targets-boundary-system"),
        (
            _inventory_without_the_run_partition,
            "inventory.logical",
            "run-partition-listed",
        ),
        (
            _inventory_partition_on_another_system,
            "inventory.logical",
            "partitions-belong-to-system",
        ),
        (
            _inventory_ownership_unavailable,
            "inventory.logical",
            "system-sources-read",
        ),
        (_inventory_without_the_system, "inventory.logical", "boundary-system-listed"),
        (_inventory_without_the_system, "lpar.plan", "plan-targets-boundary-system"),
    ],
)
def test_each_unmet_plan_or_inventory_postcondition_fails(
    schemas, change, operation, assertion
):
    world = WorkflowWorld()
    change(world)

    state = _run(schemas, world)

    assert assertion not in _held(state, operation)
    assert _results(state)[operation] == "failed"
