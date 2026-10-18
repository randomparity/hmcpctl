"""The lpar-power live arm (ST41, #1346) against a scripted HMC.

The arm runs against the production `RunState` with only the MCP client replaced,
so a dispatch the served tool schemas would reject fails here as `InvalidDispatch`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SCRIPTS_ROOT = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))
from fastmcp.utilities.json_schema_type import json_schema_to_type  # noqa: E402
from live_test import config as live_config  # noqa: E402
from live_test import (  # noqa: E402
    dedicated_pcie,
    lpar_config,
    lpar_power,
    runner,
)
from pydantic import TypeAdapter  # noqa: E402

from hmcpctl.errors import HMCError  # noqa: E402
from hmcpctl.operations.lpar.assignments import LparPcieAssignments  # noqa: E402
from hmcpctl.operations.lpar.provision import (  # noqa: E402
    validate_provision_assignments,
)

SYSTEM = "sys-A"
OTHER = "lpar-A"
OTHER_UUID = "0A1B2C3D-0000-4000-8000-0000000000A1"
VIOS_UUID = "0A1B2C3D-0000-4000-8000-0000000000B1"
VG_UUID = "0A1B2C3D-0000-4000-8000-0000000000C1"
PROFILE_UUID = "0A1B2C3D-0000-4000-8000-0000000000F1"
GROUP = "datavg"
DRC = "21010020"
VLAN = 7
MUTATIONS = frozenset(
    {
        "hmc_create_lpar",
        "hmc_power_on_lpar",
        "hmc_power_off_lpar",
        "hmc_delete_lpar",
        "hmc_power_lpar",
        "hmc_provision_lpar",
        "hmc_decommission_lpar",
        "hmc_create_virtual_disk",
        "hmc_delete_virtual_disk",
        "hmc_detach_storage_mapping",
        "hmc_dump_restart_lpar",
        "hmc_power_on_system",
        "hmc_power_off_system",
    }
)
_JOB = {"UUID": "4711", "Resource": {"Status": "COMPLETED_OK", "Results": {}}}
_NOT_MEASURED = {
    "measured": False,
    "status": "skipped",
    "reason": "x",
    "assessment": None,
}


def _created(uuid: str | None) -> dict[str, Any]:
    """A complete `hmc_create_lpar` result."""
    return {
        "resource_created": True,
        "workflow_completed": True,
        "lpar": {"UUID": uuid} if uuid else None,
        "ownership_stamped": True,
        "steps": [],
        "warnings": [],
    }


def _power_on(already: bool, job: Any, message: str | None) -> dict[str, Any]:
    """A complete `hmc_power_on_lpar` result."""
    return {
        "already_running": already,
        "job": job,
        "message": message,
        "affinity_assessment": _NOT_MEASURED,
        "warnings": [],
    }


_MEMORY = ("MinimumMemory", "DesiredMemory", "MaximumMemory")
_SHARED = ("DesiredProcessingUnits", "MaximumProcessingUnits")
_SHARED += ("DesiredVirtualProcessors", "MaximumVirtualProcessors")


class World:
    """One managed system: OTHER, a VIOS with one group, one slot, run partitions."""

    def __init__(self) -> None:
        self.partitions: dict[str, dict[str, Any]] = {}
        self.volumes = {"hd5", "lv_op"}
        self.mappings = [
            {
                "id": "vhost0/vtscsi0",
                "lpar_uuid": OTHER_UUID,
                "backing_kind": "VirtualDisk",
                "backing_name": "lv_op",
            }
        ]
        self.adapters = {"2,lpar-A,3"}
        self.operations: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.overrides: dict[str, Any] = {}
        self.composite_pauses = False
        self.detach_keeps_adapter = False
        self.provision_fails_after_storage = False
        self.provision_without_uuid = False
        self.refusals_create = False

    # -- views ---------------------------------------------------------------

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    def calls_to(self, tool: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == tool]

    def by_selector(self, selector: object) -> dict[str, Any] | None:
        for name, entry in self.partitions.items():
            if selector in {name, entry["uuid"]}:
                return entry
        return None

    def name_of(self, entry: dict[str, Any]) -> str:
        return next(n for n, e in self.partitions.items() if e is entry)

    # -- dispatch ------------------------------------------------------------

    def respond(self, tool: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((tool, kwargs))
        override = self.overrides.get(tool)
        if override is not None:
            return override(kwargs)
        return getattr(self, "_" + tool)(kwargs)

    def _new(self, name: str, token: str | None, resources: dict[str, Any]) -> str:
        uuid = f"0A1B2C3D-0000-4000-8000-{len(self.partitions) + 1:012d}"
        self.partitions[name] = {
            "uuid": uuid,
            "state": "Not Activated",
            "token": token,
            "resources": resources,
        }
        return uuid

    # -- CLI -----------------------------------------------------------------

    def _hmc_run_command(self, kwargs: dict[str, Any]) -> Any:
        cmd = kwargs["cmd"]
        if cmd.startswith("lssyscfg -r lpar") and cmd.endswith("name,state"):
            rows = [f"{OTHER},Running"]
            rows += [f"{n},{e['state']}" for n, e in self.partitions.items()]
            return "\n".join(rows) + "\n"
        if cmd.startswith("lssyscfg -r lpar"):
            return "\n".join([OTHER, *self.partitions]) + "\n"
        if "-r proc" in cmd:
            return "8.0\n"
        if "-r mem" in cmd:
            return "100000,256\n"
        if "--rsubtype slot" in cmd:
            return f"{DRC},null\n"
        if "virtualio" in cmd:
            return "\n".join(sorted(self.adapters)) + "\n"
        if cmd.startswith("viosvrcmd") and f"lsvg -lv {GROUP}" in cmd:
            rows = [f"{GROUP}:", "LV NAME TYPE LPs PPs PVs LV STATE MOUNT POINT"]
            rows += [f"{v} jfs2 1 1 1 open/syncd N/A" for v in sorted(self.volumes)]
            return "\n".join(rows) + "\n"
        return HMCError(f"unexpected command {cmd!r}")

    # -- partitions ----------------------------------------------------------

    def _hmc_create_lpar(self, kwargs: dict[str, Any]) -> Any:
        resources = kwargs["resources"]
        if resources.get("desired_memory", 0) > 1_000_000:
            return ValueError("desired_memory exceeds the configurable memory")
        if resources.get("desired_procs", 0) > resources.get("desired_vcpus", 1):
            return ValueError("a virtual processor uses at most 1.0 processing unit")
        if kwargs["name"] in self.partitions:
            return ValueError(f"An LPAR named {kwargs['name']!r} already exists")
        uuid = self._new(kwargs["name"], kwargs.get("caller_token"), resources)
        return _created(uuid)

    def _resource(self, entry: dict[str, Any]) -> dict[str, Any]:
        r = entry["resources"]
        keys = ("min_memory", "desired_memory", "max_memory")
        memory = dict(zip(_MEMORY, (str(r[k]) for k in keys), strict=True))
        keys = ("desired_procs", "max_procs", "desired_vcpus", "max_vcpus")
        shared = dict(zip(_SHARED, (str(r[k]) for k in keys), strict=True))
        return {
            "PartitionMemoryConfiguration": memory,
            "PartitionProcessorConfiguration": {"SharedProcessorConfiguration": shared},
            "AssociatedPartitionProfile": {
                "href": f"https://hmc.example.test/rest/api/uom/x/{PROFILE_UUID}"
            },
        }

    def _hmc_get_lpar(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        if entry is None:
            return HMCError("LPAR not found")
        return {"UUID": entry["uuid"], "Resource": self._resource(entry)}

    def _hmc_get_lpar_description(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        if entry is None:
            return HMCError("HSCL8012 The partition was not found")
        token = entry["token"]
        return f"[hmcpctl owner:agent created:2026-10-06] [caller {token}]"

    def _hmc_get_lpar_state(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        return entry["state"].lower() if entry else HMCError("LPAR not found")

    def _hmc_power_on_lpar(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        assert entry is not None
        if entry["state"] != "Not Activated":
            return _power_on(
                True,
                None,
                "LPAR x is already active (Open Firmware). No PowerOn job was "
                "submitted. The requested boot mode was not applied; power the "
                "partition off first.",
            )
        entry["state"] = "Open Firmware"
        return _power_on(False, _JOB, None)

    def _hmc_power_off_lpar(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        assert entry is not None
        entry["state"] = "Not Activated"
        return _JOB

    def _hmc_delete_lpar(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        if entry is None:
            return HMCError("LPAR not found")
        if entry["state"] != "Not Activated":
            return HMCError(
                "Cannot delete LPAR x — current state is 'open firmware'; it must be "
                "'not activated' to delete.",
                status_code=409,
            )
        del self.partitions[self.name_of(entry)]
        return f"Deleted LPAR {entry['uuid']}"

    def _hmc_capture_lpar_console(self, kwargs: dict[str, Any]) -> Any:
        return {"stop_reason": "duration", "released": True, "bytes_captured": 3}

    # -- composite -----------------------------------------------------------

    def _hmc_power_lpar(self, kwargs: dict[str, Any]) -> Any:
        request_id = kwargs["request_id"]
        if kwargs.get("continuation") == "abandon":
            self.operations[request_id]["state"] = "terminal"
            return self.operations[request_id]
        if request_id in self.operations:
            return self.operations[request_id]
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        assert entry is not None
        action = kwargs["action"]
        wanted = "Not Activated" if action == "stop" else "Running"
        already = (entry["state"] == "Not Activated") == (action == "stop")
        entry["state"] = wanted
        record = {
            "operation_id": f"{len(self.operations):032x}",
            "request_id": request_id,
            "tool": "hmc_power_lpar",
            "connection": "<default>",
            "system_uuid": None,
            "partition_uuid": entry["uuid"],
            "phase": "done",
            "effects": [],
            "events": [],
            "events_truncated": False,
            "next_actions": [],
            "created_at": "2026-10-06T00:00:00Z",
            "updated_at": "2026-10-06T00:00:00Z",
            "state": "paused" if self.composite_pauses else "terminal",
            "outcome": None if self.composite_pauses else "completed",
            "result": {
                "already_in_state": already,
                "observed_state": wanted.lower(),
                "job_id": None if already else "4711",
            },
            "warnings": [],
        }
        self.operations[request_id] = record
        return record

    def _hmc_operation_status(self, kwargs: dict[str, Any]) -> Any:
        found = self.operations.get(kwargs["request_id"])
        return {
            "operations": [found] if found else [],
            "limit": 50,
            "truncated": False,
            "next_cursor": None,
        }

    # -- storage and provisioning -------------------------------------------

    def _hmc_list_vios(self, kwargs: dict[str, Any]) -> Any:
        return {
            "entries": [
                {"UUID": VIOS_UUID, "Resource": {"PartitionID": "1"}},
            ],
            "unreadable_systems": [],
        }

    def _hmc_list_volume_groups(self, kwargs: dict[str, Any]) -> Any:
        return [{"uuid": VG_UUID, "name": GROUP, "free_space_gib": 50.0}]

    def _hmc_list_virtual_networks(self, kwargs: dict[str, Any]) -> Any:
        return [{"Resource": {"NetworkVLANID": str(VLAN)}}]

    def _hmc_list_storage_mappings(self, kwargs: dict[str, Any]) -> Any:
        return [dict(item) for item in self.mappings]

    def _hmc_get_vios_storage_detail(self, kwargs: dict[str, Any]) -> Any:
        mappings = []
        for item in self.mappings:
            identity = item["id"]
            server, target = identity.split("/") if identity else ("", "")
            mappings.append(
                {
                    "AssociatedLogicalPartition": {
                        "href": "/rest/api/uom/LogicalPartition/" + item["lpar_uuid"]
                    },
                    "ServerAdapter": {"AdapterName": server},
                    "TargetDevice": {"VirtualSCSITargetDevice": {"TargetName": target}},
                    "Storage": {
                        item["backing_kind"]: {"DiskName": item["backing_name"]}
                    },
                }
            )
        return {
            "UUID": VIOS_UUID,
            "Resource": {"VirtualSCSIMappings": {"VirtualSCSIMapping": mappings}},
        }

    def _hmc_create_virtual_disk(self, kwargs: dict[str, Any]) -> Any:
        self.volumes.add(kwargs["disk_name"])
        return {"disk_name": kwargs["disk_name"]}

    def _hmc_delete_virtual_disk(self, kwargs: dict[str, Any]) -> Any:
        if any(m["backing_name"] == kwargs["disk_name"] for m in self.mappings):
            return ValueError("the disk is mapped")
        self.volumes.discard(kwargs["disk_name"])
        return {"deleted": kwargs["disk_name"]}

    def _hmc_detach_storage_mapping(self, kwargs: dict[str, Any]) -> Any:
        (mapping,) = [m for m in self.mappings if m["id"] == kwargs["mapping_id"]]
        if not any(e["uuid"] == mapping["lpar_uuid"] for e in self.partitions.values()):
            return ValueError("could not resolve the mapped LPAR")
        self.mappings.remove(mapping)
        if not self.detach_keeps_adapter:
            self.adapters.discard("3,run,2")
        return {"mapping_id": kwargs["mapping_id"]}

    def _hmc_provision_lpar(self, kwargs: dict[str, Any]) -> Any:
        assignments = kwargs.get("assignments", {})
        try:
            validate_provision_assignments(
                TypeAdapter(LparPcieAssignments).validate_python(assignments)
            )
        except ValueError as exc:
            return exc
        uuid = self._new(kwargs["name"], kwargs["caller_token"], kwargs["resources"])
        self.mappings.append(
            {
                "id": "vhost1/vtscsi1",
                "lpar_uuid": uuid,
                "backing_kind": "VirtualDisk",
                "backing_name": kwargs["storage"]["storage_name"],
            }
        )
        self.adapters.add("3,run,2")
        # Live V10R3: a REST create reports the profile apply as skipped (#1164).
        steps = [
            {"step": "create", "status": "ok"},
            {"step": "apply_profile", "status": "skipped", "result": "not needed"},
            {"step": "storage", "status": "ok"},
        ]
        if self.provision_fails_after_storage:
            steps.append({"step": "power_on", "status": "error", "result": "x"})
        else:
            self.partitions[kwargs["name"]]["state"] = "Open Firmware"
            steps.append({"step": "power_on", "status": "ok"})
        return {
            "resource_created": True,
            "workflow_completed": not self.provision_fails_after_storage,
            "lpar_uuid": None if self.provision_without_uuid else uuid,
            "dry_run": False,
            "ownership_stamped": True,
            "steps": steps,
            "warnings": [],
        }

    def _hmc_list_adapters(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        if entry is None:
            return []
        kind = kwargs["adapter_type"]
        if kind == "ClientNetworkAdapter":
            return [{"UUID": "net-1", "Resource": {"PortVLANID": str(VLAN)}}]
        if kind == "VirtualSCSIClientAdapter":
            return [{"UUID": "scsi-1", "Resource": {}}]
        return []

    def _hmc_decommission_lpar(self, kwargs: dict[str, Any]) -> Any:
        entry = self.by_selector(kwargs["lpar_name_or_uuid"])
        assert entry is not None
        radius = {
            "lpar_uuid": entry["uuid"],
            "lpar_name": self.name_of(entry),
            "partition_id": 9,
            "state": entry["state"].lower(),
            "owner": None,
            "unresolved_storage_mapping_count": 0,
            "unavailable_storage_source_count": 0,
            "adapters": [
                {"type": "ClientNetworkAdapter", "uuid": "net-1"},
                {"type": "VirtualSCSIClientAdapter", "uuid": "scsi-1"},
            ],
            "storage_mappings": [
                {
                    "vios_uuid": VIOS_UUID,
                    "type": "VirtualSCSIMapping",
                    "uuid": "m",
                    "backing_device": m["backing_name"],
                }
                for m in self.mappings
                if m["lpar_uuid"] == entry["uuid"]
            ],
        }
        if kwargs.get("dry_run"):
            steps = [
                {"step": n, "status": "dry_run"}
                for n in (
                    "power_off",
                    "detach_storage_mappings",
                    "detach_adapters",
                    "delete_lpar",
                )
            ]
            steps[1]["result"] = {
                "mappings": [
                    {"vios_uuid": VIOS_UUID, "mapping_id": m["id"]}
                    for m in self.mappings
                    if m["lpar_uuid"] == entry["uuid"]
                ]
            }
            return {
                "resource_deleted": False,
                "workflow_completed": False,
                "dry_run": True,
                "lpar_uuid": entry["uuid"],
                "warnings": [],
                "steps": steps,
                "blast_radius": radius,
            }
        for mapping in list(self.mappings):
            if mapping["lpar_uuid"] == entry["uuid"]:
                self._hmc_detach_storage_mapping({"mapping_id": mapping["id"]})
        del self.partitions[self.name_of(entry)]
        return {
            "resource_deleted": True,
            "workflow_completed": True,
            "dry_run": False,
            "lpar_uuid": entry["uuid"],
            "warnings": [],
            "steps": [{"step": "delete_lpar", "status": "ok"}],
            "blast_radius": radius,
        }


class FakeClient:
    """Answers from the World, typed the way FastMCP types a served result.

    A tool whose output schema has properties comes back as a generated dataclass,
    never a dict, so the arm is judged against what the live client hands it.
    """

    def __init__(self, world: World, output_schemas: dict[str, Any]) -> None:
        self.world = world
        self.output_schemas = output_schemas

    async def call_tool(self, tool: str, kwargs: dict[str, Any]) -> Any:
        value = self.world.respond(tool, kwargs)
        if isinstance(value, Exception):
            raise value
        schema = self.output_schemas.get(tool)
        if schema:
            if schema.get("x-fastmcp-wrap-result"):
                schema = schema.get("properties", {}).get("result", schema)
            value = TypeAdapter(json_schema_to_type(schema)).validate_python(value)
        return SimpleNamespace(data=value)


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict[str, Any]]:
    """The input schemas the live runner's server really serves."""

    async def served() -> dict[str, dict[str, Any]]:
        async with runner.served_client() as client:
            return await runner.served_schemas(client)

    return asyncio.run(served())


@pytest.fixture(scope="module")
def output_schemas() -> dict[str, Any]:
    """The output schemas the live runner's server really serves."""

    async def served() -> dict[str, Any]:
        async with runner.served_client() as client:
            return {tool.name: tool.output_schema for tool in await client.list_tools()}

    return asyncio.run(served())


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    for module, name in (
        (lpar_power, "_STATE_POLL_DELAY_S"),
        (lpar_power, "_OPERATION_POLL_DELAY_S"),
        (lpar_power, "_COMPARE_REREAD_DELAY_S"),
        (dedicated_pcie, "_ABSENCE_REREAD_DELAY_S"),
    ):
        monkeypatch.setattr(module, name, 0)
    monkeypatch.setattr(lpar_power, "_STATE_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(lpar_power, "_power_operations_authorized", lambda: True)


_OUTPUTS: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _typed_outputs(output_schemas):
    _OUTPUTS.update(output_schemas)


def _run(schemas, world: World, group: str = "lpar-power", **config: Any):
    state = runner.RunState(
        config=live_config.LiveTestConfig(
            system_name=SYSTEM,
            vdisk_volume_group_name=GROUP,
            provision_vlan_id=VLAN,
            **config,
        ),
        group=group,
    )
    state.schemas = schemas
    asyncio.run(lpar_power.exercise_lpar_power(FakeClient(world, _OUTPUTS), state))
    return state


def _results(state) -> dict[str, str]:
    return {e["operation"]: e["observation"]["result"] for e in state.observations}


def _held(state, operation: str) -> list[str]:
    (entry,) = [e for e in state.observations if e["operation"] == operation]
    return entry["observation"]["assertions"]


def _rows(state, tool: str) -> list[dict[str, Any]]:
    return [row for row in state.results if row["tool"] == tool]


_OBSERVED = {
    "lpar.create",
    "lpar.power_on",
    "lpar.capture_console",
    "lpar.power_off",
    "lpar.power",
    "lpar.delete",
    "provision.lpar",
    "lpar.decommission",
}


def test_clean_run_passes_every_observation_and_leaves_the_baseline(schemas):
    world = World()
    state = _run(schemas, world)

    assert [row["tool"] for row in state.results if row["status"] == "FAIL"] == []
    assert set(_results(state)) == _OBSERVED
    assert set(_results(state).values()) == {"passed"}
    assert {e["observation"]["cleanup"] for e in state.observations} == {"passed"}
    assert world.partitions == {}
    assert world.volumes == {"hd5", "lv_op"}
    assert _rows(state, "system baseline compare")[0]["status"] == "PASS"


def test_names_and_token_share_the_run_suffix(schemas):
    world = World()
    _run(schemas, world)

    (create, *_) = [k for k in world.calls_to("hmc_create_lpar")]
    name = world.calls_to("hmc_provision_lpar")[0]["name"]
    suffix = name.removeprefix(lpar_power.NAME_PREFIX).removesuffix("-p")
    assert create["caller_token"] == lpar_power.TOKEN_PREFIX + suffix
    disk = world.calls_to("hmc_create_virtual_disk")[0]["disk_name"]
    assert disk == lpar_power.VOLUME_PREFIX + suffix and len(disk) <= 15


def test_every_mutation_names_a_run_object(schemas):
    world = World()
    _run(schemas, world)

    run_uuids = {e for e in _uuids_created(world)}
    for tool, kwargs in world.calls:
        if tool not in MUTATIONS or tool in {"hmc_create_lpar", "hmc_provision_lpar"}:
            continue
        if tool in {"hmc_create_virtual_disk", "hmc_delete_virtual_disk"}:
            assert kwargs["disk_name"].startswith(lpar_power.VOLUME_PREFIX)
        elif tool == "hmc_detach_storage_mapping":
            assert kwargs["mapping_id"] == "vhost1/vtscsi1"
        elif tool == "hmc_power_lpar" and "continuation" in kwargs:
            assert kwargs["request_id"].startswith(lpar_power.TOKEN_PREFIX)
        else:
            assert kwargs["lpar_name_or_uuid"] in run_uuids, (tool, kwargs)
    assert all(
        k["name"].startswith(lpar_power.NAME_PREFIX)
        for k in world.calls_to("hmc_create_lpar")
        + world.calls_to("hmc_provision_lpar")
    )


def _uuids_created(world: World) -> set[str]:
    return {
        f"0A1B2C3D-0000-4000-8000-{n:012d}"
        for n in range(1, 1 + len(world.calls_to("hmc_create_lpar")) + 1)
    }


def test_outside_its_group_it_calls_nothing(schemas):
    world = World()
    state = _run(schemas, world, group="round2")

    assert world.calls == []
    assert state.observations == []


def test_without_the_power_guard_it_calls_nothing(schemas, monkeypatch):
    monkeypatch.setattr(lpar_power, "_power_operations_authorized", lambda: False)
    world = World()
    state = _run(schemas, world)

    assert world.calls == []
    assert "HMC_AUTHORIZE_POWER_OPERATIONS" in _rows(state, "lpar-power arm")[0]["note"]


def test_a_stranded_run_partition_stops_the_arm_before_any_write(schemas):
    world = World()
    world.partitions["hmcpctl-live-pwr-00000000"] = {
        "uuid": "x",
        "state": "Not Activated",
        "token": None,
        "resources": {},
    }
    state = _run(schemas, world)

    assert not set(world.tools()) & MUTATIONS
    assert "already exist" in _rows(state, "lpar-power arm")[0]["note"]


def test_gap_rows_name_their_prerequisite_and_make_no_call(schemas):
    world = World()
    state = _run(schemas, world)

    for tool, _ in lpar_power.GAPS:
        (row,) = _rows(state, tool)
        assert row["status"] == "SKIP" and row["note"].startswith("gap: needs")
    assert not {"hmc_dump_restart_lpar", "hmc_power_on_system"} & set(world.tools())
    assert "hmc_power_off_system" not in world.tools()


def test_a_create_guard_that_lets_a_request_through_fails_its_assertion(schemas):
    world = World()
    real = world._hmc_create_lpar
    world.overrides["hmc_create_lpar"] = lambda k: (
        world._new(k["name"] + "-leak", k["caller_token"], k["resources"])
        and _created(None)
        if k["resources"]["desired_memory"] > 1_000_000
        else real(k)
    )
    state = _run(schemas, world)

    assert "memory-over-configurable-refused" not in _held(state, "lpar.create")
    assert _results(state)["lpar.create"] == "failed"


def test_a_duplicate_create_that_succeeds_fails_its_assertion(schemas):
    world = World()
    real = world._hmc_create_lpar
    world.overrides["hmc_create_lpar"] = lambda k: (
        _created(world.partitions[k["name"]]["uuid"])
        if k["name"] in world.partitions
        else real(k)
    )
    state = _run(schemas, world)

    assert "duplicate-name-refused" not in _held(state, "lpar.create")


def test_a_power_on_of_a_running_partition_that_submits_fails(schemas):
    world = World()
    real = world._hmc_power_on_lpar
    world.overrides["hmc_power_on_lpar"] = lambda k: (
        _power_on(False, _JOB, None)
        if world.by_selector(k["lpar_name_or_uuid"])["state"] != "Not Activated"
        else real(k)
    )
    state = _run(schemas, world)

    assert "running-reported-without-job" not in _held(state, "lpar.power_on")


def test_an_activated_delete_that_is_accepted_fails(schemas):
    world = World()
    real = world._hmc_delete_lpar

    def accepts(kwargs: dict[str, Any]) -> Any:
        entry = world.by_selector(kwargs["lpar_name_or_uuid"])
        if entry is not None:
            entry["state"] = "Not Activated"
        return real(kwargs)

    world.overrides["hmc_delete_lpar"] = accepts
    state = _run(schemas, world)

    assert "activated-delete-refused" not in _held(state, "lpar.delete")


def test_a_paused_composite_fails_and_is_abandoned_in_teardown(schemas):
    world = World()
    world.composite_pauses = True
    state = _run(schemas, world)

    assert _results(state)["lpar.power"] == "failed"
    abandoned = [k for k in world.calls_to("hmc_power_lpar") if "continuation" in k]
    assert {k["request_id"] for k in abandoned} == set(world.operations)


def test_a_repeat_stop_that_submits_fails_its_assertion(schemas):
    world = World()
    real = world._hmc_power_lpar

    def submits(kwargs: dict[str, Any]) -> Any:
        record = real(kwargs)
        if kwargs["request_id"].endswith("-4"):
            record["result"]["already_in_state"] = False
        return record

    world.overrides["hmc_power_lpar"] = submits
    state = _run(schemas, world)

    assert "repeat-stop-already-in-state" not in _held(state, "lpar.power")


def test_without_a_vios_provision_is_skipped_with_its_reason(schemas):
    world = World()
    world.overrides["hmc_list_volume_groups"] = lambda _k: []
    state = _run(schemas, world)

    assert "hmc_provision_lpar" not in world.tools()
    (row,) = _rows(state, "hmc_provision_lpar / hmc_decommission_lpar")
    assert "exactly one is needed" in row["note"]
    assert "provision.lpar" not in _results(state)


def test_without_the_vlan_provision_is_skipped(schemas):
    world = World()
    world.overrides["hmc_list_virtual_networks"] = lambda _k: []
    state = _run(schemas, world)

    assert "hmc_create_virtual_disk" not in world.tools()
    assert (
        "no virtual network"
        in _rows(state, "hmc_provision_lpar / hmc_decommission_lpar")[0]["note"]
    )


def test_a_dry_run_dedicated_slot_is_refused_and_never_sent_for_real(schemas):
    world = World()
    state = _run(schemas, world)

    probe, real = world.calls_to("hmc_provision_lpar")
    assert probe["dry_run"] is True and probe["assignments"]["dedicated"]
    assert "assignments" not in real and "dry_run" not in real
    (row,) = _rows(state, "hmc_provision_lpar (dedicated PCIe argument)")
    assert row["status"] == "PASS"
    assert "dedicated-slot-refused" in _held(state, "provision.lpar")
    assert "pcie-slot-owned" not in _held(state, "provision.lpar")


def test_product_decommission_runs_without_prior_detach(schemas):
    world = World()
    _run(schemas, world)
    assert not world.calls_to("hmc_detach_storage_mapping")
    tools = world.tools()
    real = [
        i
        for i, (tool, k) in enumerate(world.calls)
        if tool == "hmc_decommission_lpar" and not k.get("dry_run")
    ]
    assert real and tools.index("hmc_delete_virtual_disk") > real[0]


def test_failed_decommission_preserves_mapping_and_backing_volume(schemas):
    world = World()
    original = world._hmc_decommission_lpar
    world.overrides["hmc_decommission_lpar"] = lambda k: (
        original(k) if k["dry_run"] else HMCError("busy")
    )
    state = _run(schemas, world)
    assert _rows(state, "decommission recovery")
    assert world.volumes > {"hd5", "lv_op"}
    assert [name for name in world.partitions if name.endswith("-p")]
    assert not world.calls_to("hmc_detach_storage_mapping")
    assert not world.calls_to("hmc_delete_virtual_disk")


def test_teardown_detaches_a_run_mapping_before_deleting_its_partition(schemas):
    """Without a decommission attempt, teardown detaches before deleting P."""
    world = World()
    world.provision_fails_after_storage = True
    world.provision_without_uuid = True
    get_lpar = world._hmc_get_lpar
    unavailable = True

    def read_lpar(kwargs):
        nonlocal unavailable
        if str(kwargs["lpar_name_or_uuid"]).endswith("-p") and unavailable:
            unavailable = False
            return HMCError("post-provision partition read unavailable")
        return get_lpar(kwargs)

    world.overrides["hmc_get_lpar"] = read_lpar
    state = _run(schemas, world)

    tools = world.tools()
    last = {tool: len(tools) - 1 - tools[::-1].index(tool) for tool in set(tools)}
    assert last["hmc_detach_storage_mapping"] < last["hmc_delete_lpar"]
    assert world.partitions == {}
    assert world.volumes == {"hd5", "lv_op"}
    assert _results(state)["provision.lpar"] == "failed"


def test_provision_without_a_uuid_adopts_the_partition_by_token(schemas):
    world = World()
    world.provision_without_uuid = True
    state = _run(schemas, world)

    assert world.partitions == {}
    assert _results(state)["lpar.decommission"] == "passed"


def test_a_server_adapter_left_by_the_detach_fails_the_compare(schemas):
    world = World()
    world.detach_keeps_adapter = True
    state = _run(schemas, world)

    (row,) = _rows(state, "VIOS server adapter teardown")
    assert (
        "chhwres -r virtualio --rsubtype scsi" in row["data"] and "-s 3" in row["data"]
    )
    assert _rows(state, "system baseline compare")[0]["status"] == "FAIL"
    assert {e["observation"]["cleanup"] for e in state.observations} == {"failed"}


def test_a_changed_partition_state_fails_the_compare(schemas):
    world = World()
    real = world._hmc_run_command
    reads = []

    def drifts(kwargs: dict[str, Any]) -> Any:
        text = real(kwargs)
        if kwargs["cmd"].endswith("name,state"):
            reads.append(text)
            if world.calls_to("hmc_decommission_lpar"):
                return text.replace(f"{OTHER},Running", f"{OTHER},Not Activated")
        return text

    world.overrides["hmc_run_command"] = drifts
    state = _run(schemas, world)

    assert _rows(state, "system baseline compare")[0]["status"] == "FAIL"


def test_a_partition_that_will_not_delete_leaves_recovery_commands(schemas):
    world = World()
    real = world._hmc_delete_lpar
    world.overrides["hmc_delete_lpar"] = lambda k: (
        HMCError("HSCL0001 refused")
        if world.by_selector(k["lpar_name_or_uuid"])
        and world.by_selector(k["lpar_name_or_uuid"])["state"] == "Not Activated"
        else real(k)
    )
    state = _run(schemas, world)

    (row, *_) = _rows(state, "run partition teardown")
    assert "rmsyscfg" in row["data"] and "MANUAL RECOVERY REQUIRED" in row["data"]


def test_scratch_name_helpers_match_only_the_reserved_prefixes():
    assert lpar_power.scratch_partitions(["hmcpctl-live-pwr-1", "lpar-A"]) == [
        "hmcpctl-live-pwr-1"
    ]
    assert lpar_power.scratch_volumes(["lppwrabc", "lv_op"]) == ["lppwrabc"]
    assert lpar_config.NAME_PREFIX != lpar_power.NAME_PREFIX


@pytest.mark.parametrize("listing", [None, 42, "", "  ", "datavg:", "not a listing"])
def test_unreadable_volume_inventory_blocks_power_mutations(schemas, listing):
    world = World()
    real = world._hmc_run_command
    world.overrides["hmc_run_command"] = lambda k: (
        listing if "lsvg -lv" in k["cmd"] else real(k)
    )

    state = _run(schemas, world)

    assert not any(world.calls_to(tool) for tool in MUTATIONS)
    assert "the system baseline did not read" in str(state.results)


@pytest.mark.parametrize(
    "listing", [None, "", "datavg:", "not a listing", HMCError("read failed")]
)
def test_unreadable_volume_inventory_never_authorizes_disk_teardown(schemas, listing):
    world = World()
    real = world._hmc_run_command
    world.overrides["hmc_run_command"] = lambda k: (
        listing if "lsvg -lv" in k["cmd"] else real(k)
    )
    state = runner.RunState(config=live_config.LiveTestConfig(system_name=SYSTEM))
    state.schemas = schemas
    run = lpar_power.Run(
        SYSTEM,
        "0a1b2c3d",
        vios=lpar_power.Vios(VIOS_UUID, 2, GROUP, VG_UUID),
        volume_attempted=True,
    )

    clean = asyncio.run(
        lpar_power._teardown_storage(FakeClient(world, _OUTPUTS), state, run)
    )

    assert not world.calls_to("hmc_delete_virtual_disk")
    assert not clean
    assert "MANUAL RECOVERY REQUIRED" in str(state.results)


def test_header_only_volume_inventory_allows_normal_power_cleanup(schemas):
    world = World()
    world.volumes.clear()

    state = _run(schemas, world)

    assert world.calls_to("hmc_create_virtual_disk")
    assert world.calls_to("hmc_delete_virtual_disk")
    assert not world.volumes
    assert _rows(state, "system baseline compare")[0]["status"] == "PASS"


# ---------------------------------------------------------------------------
# Each assertion can fail: one scripted deviation per assertion id
# ---------------------------------------------------------------------------

_FAILED_JOB = {"UUID": "4712", "Resource": {"Status": "COMPLETED_WITH_ERROR"}}


def _wrap(world: World, tool: str, change) -> None:
    """Answer *tool* from the model, then let *change* rewrite the answer."""
    real = getattr(world, "_" + tool)
    world.overrides[tool] = lambda kwargs: change(kwargs, real)


def _units_accepted(w: World) -> None:
    _wrap(
        w,
        "hmc_create_lpar",
        lambda k, real: (
            _created(None) if k["resources"]["desired_procs"] > 1 else real(k)
        ),
    )


def _skewed_read(w: World) -> None:
    def change(k, real):
        answer = real(k)
        if isinstance(answer, dict):
            answer["Resource"]["PartitionMemoryConfiguration"]["MinimumMemory"] = "1"
        return answer

    _wrap(w, "hmc_get_lpar", change)


def _foreign_token(w: World) -> None:
    _wrap(
        w,
        "hmc_get_lpar_description",
        lambda k, real: (
            "[caller lparpwr-ffffffff]"
            if w.by_selector(k["lpar_name_or_uuid"])
            else real(k)
        ),
    )


def _job_fails_when(predicate):
    def apply(w: World) -> None:
        def change(k, real):
            answer = real(k)
            if not predicate(k):
                return answer
            if "job" in answer:
                return {**answer, "job": _FAILED_JOB}
            return _FAILED_JOB

        _wrap(w, "hmc_power_on_lpar", change)
        _wrap(w, "hmc_power_off_lpar", change)

    return apply


def _console(**answer: Any):
    return lambda w: w.overrides.__setitem__(
        "hmc_capture_lpar_console", lambda _k: answer
    )


def _composite_fails(action: str):
    def apply(w: World) -> None:
        def change(k, real):
            record = real(k)
            if k.get("action") == action and "continuation" not in k:
                record["outcome"] = "failed"
            return record

        _wrap(w, "hmc_power_lpar", change)

    return apply


def _replay_is_new(w: World) -> None:
    seen: set[str] = set()

    def change(k, real):
        record = real(k)
        if k["request_id"] in seen:
            return {**record, "operation_id": "f" * 32}
        seen.add(k["request_id"])
        return record

    _wrap(w, "hmc_power_lpar", change)


def _lost_after_refusal(w: World) -> None:
    deletes: list[Any] = []
    _wrap(w, "hmc_delete_lpar", lambda k, real: deletes.append(1) or real(k))
    _wrap(
        w,
        "hmc_get_lpar",
        lambda k, real: HMCError("LPAR not found") if len(deletes) == 1 else real(k),
    )


def _a_delete_lost(w: World) -> None:
    """A's real delete takes effect but its answer is lost."""

    def change(k, real):
        answer = real(k)
        return (
            HMCError("Read timed out")
            if len(w.calls_to("hmc_delete_lpar")) == 2
            else answer
        )

    _wrap(w, "hmc_delete_lpar", change)


def _a_delete_ignored(w: World) -> None:
    """The second delete (A's real one) answers success and deletes nothing."""

    def change(k, real):
        if len(w.calls_to("hmc_delete_lpar")) == 2:
            return "Deleted LPAR"
        return real(k)

    _wrap(w, "hmc_delete_lpar", change)


def _other_vlan(w: World) -> None:
    w.overrides["hmc_list_adapters"] = lambda _k: [
        {"UUID": "net-1", "Resource": {"PortVLANID": "99"}}
    ]


def _mapping_elsewhere(w: World) -> None:
    def change(k, real):
        answer = real(k)
        for mapping in w.mappings:
            if mapping["id"] == "vhost1/vtscsi1":
                mapping["lpar_uuid"] = OTHER_UUID
        return answer

    _wrap(w, "hmc_provision_lpar", change)


def _p_not_activated(w: World) -> None:
    def change(k, real):
        answer = real(k)
        if k.get("dry_run"):
            return answer
        w.partitions[k["name"]]["state"] = "Not Activated"
        return answer

    _wrap(w, "hmc_provision_lpar", change)


def _dedicated_dry_run_accepted(w: World) -> None:
    _wrap(
        w,
        "hmc_provision_lpar",
        lambda k, real: (
            {"dry_run": True, "workflow_completed": False, "steps": []}
            if k.get("dry_run")
            else real(k)
        ),
    )


def _decommission_answer(dry_run: bool, **fields: Any):
    def apply(w: World) -> None:
        def change(k, real):
            answer = real(k)
            return {**answer, **fields} if bool(k["dry_run"]) == dry_run else answer

        _wrap(w, "hmc_decommission_lpar", change)

    return apply


def _dry_run_powers_off(w: World) -> None:
    def change(k, real):
        if k["dry_run"]:
            w.by_selector(k["lpar_name_or_uuid"])["state"] = "Not Activated"
        return real(k)

    _wrap(w, "hmc_decommission_lpar", change)


def _decommission_keeps_partition(w: World) -> None:
    def change(k, real):
        entry = w.by_selector(k["lpar_name_or_uuid"])
        name = w.name_of(entry) if entry else None
        answer = real(k)
        if name and not k["dry_run"]:
            w.partitions[name] = entry
        return answer

    _wrap(w, "hmc_decommission_lpar", change)


_DEVIATIONS = [
    ("lpar.create", "units-over-vcpus-refused", _units_accepted),
    ("lpar.create", "resources-read-back", _skewed_read),
    ("lpar.create", "ownership-stamped", _foreign_token),
    (
        "lpar.power_on",
        "profile-activation-reached-firmware",
        _job_fails_when(lambda k: k.get("partition_profile_uuid")),
    ),
    (
        "lpar.power_on",
        "current-configuration-reached-firmware",
        _job_fails_when(lambda k: k.get("operation_type")),
    ),
    ("lpar.capture_console", "console-captured", _console(stop_reason="error")),
    ("lpar.capture_console", "console-released", _console(released=False)),
    (
        "lpar.power_off",
        "delayed-shutdown-not-activated",
        _job_fails_when(lambda k: k.get("immediate") is False),
    ),
    (
        "lpar.power_off",
        "immediate-shutdown-not-activated",
        _job_fails_when(lambda k: k.get("immediate") is True),
    ),
    ("lpar.power", "start-completed-activated", _composite_fails("start")),
    (
        "lpar.power",
        "restart-immediate-completed-activated",
        _composite_fails("restart"),
    ),
    ("lpar.power", "stop-immediate-completed-not-activated", _composite_fails("stop")),
    ("lpar.power", "same-request-replays", _replay_is_new),
    ("lpar.delete", "partition-kept", _lost_after_refusal),
    ("lpar.delete", "delete-call-succeeded", _a_delete_lost),
    ("lpar.delete", "lpar-name-absent", _a_delete_ignored),
    ("provision.lpar", "ownership-stamped", _foreign_token),
    ("provision.lpar", "network-adapter-on-vlan", _other_vlan),
    ("provision.lpar", "storage-mapping-listed", _mapping_elsewhere),
    ("provision.lpar", "partition-activated", _p_not_activated),
    ("provision.lpar", "dedicated-slot-refused", _dedicated_dry_run_accepted),
    (
        "lpar.decommission",
        "dry-run-inventoried",
        _decommission_answer(True, resource_deleted=True),
    ),
    ("lpar.decommission", "dry-run-changed-nothing", _dry_run_powers_off),
    (
        "lpar.decommission",
        "resource-deleted",
        _decommission_answer(False, resource_deleted=False),
    ),
    (
        "lpar.decommission",
        "workflow-completed",
        _decommission_answer(False, workflow_completed=False),
    ),
    ("lpar.decommission", "lpar-name-absent", _decommission_keeps_partition),
]


@pytest.mark.parametrize(
    ("operation", "assertion", "deviate"),
    _DEVIATIONS,
    ids=[f"{op}:{assertion}" for op, assertion, _ in _DEVIATIONS],
)
def test_each_assertion_fails_on_its_deviation(schemas, operation, assertion, deviate):
    world = World()
    deviate(world)
    state = _run(schemas, world)

    assert assertion not in _held(state, operation)
    assert _results(state)[operation] == "failed"


def test_an_otherwise_empty_volume_group_still_cleans_up(schemas):
    world = World()
    world.volumes = set()
    state = _run(schemas, world)

    assert world.volumes == set()
    assert not _rows(state, "run volume teardown")
    assert {e["observation"]["cleanup"] for e in state.observations} == {"passed"}


def test_a_run_mapping_without_an_id_is_never_read_as_detached(schemas):
    world = World()

    def provision(k, real):
        answer = real(k)
        if k.get("dry_run"):
            return answer
        world.mappings[-1]["id"] = None
        return answer

    _wrap(world, "hmc_provision_lpar", provision)
    state = _run(schemas, world)

    real = [k for k in world.calls_to("hmc_decommission_lpar") if not k["dry_run"]]
    assert real == []
    assert [name for name in world.partitions if name.endswith("-p")]
    assert _rows(state, "decommission recovery")


def test_a_refused_current_configuration_activation_does_not_stop_the_sequence(schemas):
    """V10R3 answered FAILED_TO_START and left the partition Not Activated (#1346).

    The arm activates once with the current configuration and then goes on by
    profile; it no longer retries without `operation_type`, which is never sent
    on the wire (#1392).
    """
    world = World()
    real = world._hmc_power_on_lpar
    failed = {"UUID": "4713", "Resource": {"Status": "FAILED_TO_START"}}

    def refuses(kwargs: dict[str, Any]) -> Any:
        if kwargs.get("keylock"):
            return _power_on(False, failed, None)
        return real(kwargs)

    world.overrides["hmc_power_on_lpar"] = refuses
    state = _run(schemas, world)

    (current,) = [k for k in world.calls_to("hmc_power_on_lpar") if k.get("keylock")]
    assert current["boot_mode"] == "of"
    assert current["operation_type"] == "activate"
    assert not _rows(
        state, "hmc_power_on_lpar (current configuration, no operation type)"
    )
    assert _rows(state, "hmc_power_on_lpar (profile, to continue)")
    assert "current-configuration-reached-firmware" not in _held(state, "lpar.power_on")
    assert "immediate-shutdown-not-activated" in _held(state, "lpar.power_off")
    assert _results(state)["lpar.power"] == "passed"
    assert _results(state)["lpar.delete"] == "passed"


def test_a_refused_detach_records_the_vios_rmc_state(schemas):
    """HSCL2957 names RMC; the arm reads the VIOS's rmc_state beside the refusal."""
    world = World()
    real = world._hmc_run_command
    reads: list[str] = []

    def answer(kwargs: dict[str, Any]) -> Any:
        if kwargs["cmd"].endswith("name,lpar_env,rmc_state"):
            reads.append(kwargs["cmd"])
            return f"{OTHER},aixlinux,inactive\nvios-A,vioserver,active\n"
        return real(kwargs)

    world.overrides["hmc_run_command"] = answer
    world.provision_fails_after_storage = True
    world.provision_without_uuid = True
    get_lpar = world._hmc_get_lpar
    unavailable = True

    def read_lpar(kwargs):
        nonlocal unavailable
        if str(kwargs["lpar_name_or_uuid"]).endswith("-p") and unavailable:
            unavailable = False
            return HMCError("post-provision partition read unavailable")
        return get_lpar(kwargs)

    world.overrides["hmc_get_lpar"] = read_lpar
    world.overrides["hmc_detach_storage_mapping"] = lambda _k: HMCError(
        "REST0126 HSCL2957 no RMC connection"
    )
    state = _run(schemas, world)

    assert reads
    rows = _rows(state, "VIOS rmc_state (after a refused detach)")
    assert rows and {row["data"] for row in rows} == {"vios-A,vioserver,active"}


def test_decommission_cancellation_retains_mapping_partition_and_volume(schemas):
    world = World()

    def cancel(kwargs):
        if not kwargs["dry_run"]:
            raise asyncio.CancelledError()
        return world._hmc_decommission_lpar(kwargs)

    world.overrides["hmc_decommission_lpar"] = cancel
    with pytest.raises(asyncio.CancelledError):
        _run(schemas, world)
    assert any(name.endswith("-p") for name in world.partitions)
    assert world.volumes > {"hd5", "lv_op"}
    assert not world.calls_to("hmc_delete_virtual_disk")


def test_failed_partition_absence_never_releases_decommission_cleanup(schemas):
    world = World()
    _decommission_keeps_partition(world)
    state = _run(schemas, world)
    assert _results(state)["lpar.decommission"] == "failed"
    assert any(name.endswith("-p") for name in world.partitions)
    assert not world.calls_to("hmc_delete_virtual_disk")


@pytest.mark.parametrize(
    "failure",
    [
        "omitted-collection",
        "invalid-metadata",
        "failed-detail",
        "failed-partition-read",
        "missing-volume",
        "changed-foreign",
    ],
)
def test_uncertain_post_decommission_proof_retains_manual_cleanup(schemas, failure):
    world = World()
    completed = False
    original = world._hmc_decommission_lpar

    def decommission(kwargs):
        nonlocal completed
        result = original(kwargs)
        if not kwargs["dry_run"]:
            completed = True
            if failure == "missing-volume":
                world.volumes = {"hd5", "lv_op"}
            elif failure == "changed-foreign":
                world.mappings.append(
                    {
                        "id": "vhost9/vtscsi9",
                        "lpar_uuid": OTHER_UUID,
                        "backing_kind": "VirtualDisk",
                        "backing_name": "operator-volume",
                    }
                )
        return result

    def detail(kwargs):
        if completed and failure == "failed-detail":
            return HMCError("VIOS detail unavailable")
        result = world._hmc_get_vios_storage_detail(kwargs)
        if completed and failure == "omitted-collection":
            result["Resource"].pop("VirtualSCSIMappings")
        if completed and failure == "invalid-metadata":
            result["Resource"]["VirtualSCSIMappings"]["Metadata"] = {
                "Atom": "unexpected"
            }
        return result

    def read_partitions(kwargs):
        if (
            completed
            and failure == "failed-partition-read"
            and kwargs["cmd"].endswith("-F name,state")
        ):
            return HMCError("partition listing unavailable")
        return world._hmc_run_command(kwargs)

    world.overrides.update(
        hmc_decommission_lpar=decommission,
        hmc_get_vios_storage_detail=detail,
        hmc_run_command=read_partitions,
    )
    state = _run(schemas, world)
    assert _results(state)["lpar.decommission"] == "failed"
    assert _rows(state, "decommission recovery")
    assert not world.calls_to("hmc_delete_virtual_disk")
    assert not world.calls_to("hmc_detach_storage_mapping")


@pytest.mark.parametrize("boundary", ["preview", "inventory", "post-inventory"])
def test_cancellation_during_decommission_proof_preserves_remaining_scratch(
    schemas, boundary
):
    world = World()

    def cancel(kwargs):
        if boundary == "post-inventory" and any(
            name.endswith("-p") for name in world.partitions
        ):
            return world._hmc_get_vios_storage_detail(kwargs)
        raise asyncio.CancelledError()

    tool = (
        "hmc_decommission_lpar"
        if boundary == "preview"
        else "hmc_get_vios_storage_detail"
    )
    world.overrides[tool] = cancel
    with pytest.raises(asyncio.CancelledError):
        _run(schemas, world)
    assert any(name.endswith("-p") for name in world.partitions) == (
        boundary != "post-inventory"
    )
    assert world.volumes > {"hd5", "lv_op"}
    assert not world.calls_to("hmc_delete_virtual_disk")
    assert not world.calls_to("hmc_detach_storage_mapping")


@pytest.mark.parametrize("flag", ["resource_deleted", "workflow_completed"])
def test_unsuccessful_typed_decommission_flag_retains_manual_cleanup(schemas, flag):
    world = World()
    original = world._hmc_decommission_lpar

    def incomplete(kwargs):
        result = original(kwargs)
        if not kwargs["dry_run"]:
            result[flag] = False
        return result

    world.overrides["hmc_decommission_lpar"] = incomplete
    state = _run(schemas, world)
    assert _results(state)["lpar.decommission"] == "failed"
    assert _rows(state, "decommission recovery")
    assert not world.calls_to("hmc_delete_virtual_disk")
    assert not world.calls_to("hmc_detach_storage_mapping")


@pytest.mark.parametrize(
    "preview",
    [
        "substituted",
        "missing",
        "duplicate",
        "case-duplicate",
        "empty",
        "missing-uuid",
        "malformed",
    ],
)
def test_wrong_preview_adapter_identity_retains_decommission_resources(
    schemas, preview
):
    world = World()

    def change(kwargs, real):
        data = real(kwargs)
        if kwargs["dry_run"]:
            adapters = data["blast_radius"]["adapters"]
            if preview == "substituted":
                adapters[0]["uuid"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
            elif preview == "missing":
                adapters.pop()
            elif preview == "duplicate":
                adapters.append(dict(adapters[0]))
            elif preview == "case-duplicate":
                adapters.append({**adapters[0], "uuid": adapters[0]["uuid"].upper()})
            elif preview == "empty":
                adapters[0]["uuid"] = ""
            elif preview == "missing-uuid":
                adapters[0].pop("uuid")
            else:
                data["blast_radius"]["adapters"] = [None]
        return data

    _wrap(world, "hmc_decommission_lpar", change)
    state = _run(schemas, world)
    assert _results(state)["lpar.decommission"] == "failed"
    assert not [k for k in world.calls_to("hmc_decommission_lpar") if not k["dry_run"]]
    assert any(name.endswith("-p") for name in world.partitions)
    assert not world.calls_to("hmc_delete_virtual_disk")


def test_preview_adapter_uuid_case_matches_snapshot(schemas):
    world = World()

    def uppercase(kwargs, real):
        data = real(kwargs)
        if kwargs["dry_run"]:
            for item in data["blast_radius"]["adapters"]:
                item["uuid"] = item["uuid"].upper()
        return data

    _wrap(world, "hmc_decommission_lpar", uppercase)
    state = _run(schemas, world)
    assert _results(state)["lpar.decommission"] == "passed"
    assert (
        len([k for k in world.calls_to("hmc_decommission_lpar") if not k["dry_run"]])
        == 1
    )
    assert len(world.calls_to("hmc_delete_virtual_disk")) == 1
