"""Bounded diagnostic probe against production RunState and a fake tool boundary."""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import live_test_runner as runner
import test_live_lpar_power_arm as arm
from live_test import config as live_config
from live_test import detach_probe, lpar_power
from test_live_lpar_power_arm import GROUP, SYSTEM, FakeClient, World

from hmcpctl.errors import HMCError
from hmcpctl.ssh.commands import HMC_NO_RESULTS


@pytest.fixture(scope="module")
def schemas():
    return arm.schemas.__wrapped__()


@pytest.fixture(scope="module")
def output_schemas():
    return arm.output_schemas.__wrapped__()


class ProbeWorld(World):
    def __init__(self):
        super().__init__()
        self.detach_states = []
        self.failure = None

    def _hmc_get_lpar(self, kwargs):
        result = super()._hmc_get_lpar(kwargs)
        if isinstance(result, dict):
            result["Resource"]["PartitionID"] = "9"
        return result

    def _hmc_run_command(self, kwargs):
        cmd = kwargs["cmd"]
        if cmd.endswith("lpar_id,name,lpar_env,state,rmc_state"):
            rows = [
                "1,vios-A,vioserver,Running,active",
                "7,lpar-A,aixlinux,Running,inactive",
            ]
            rows += [
                f"9,{name},aixlinux,{item['state']},inactive"
                for name, item in self.partitions.items()
            ]
            return "\n".join(rows)
        if "--rsubtype scsi" in cmd and "lpar_names" in cmd:
            owned = [
                row for row in self.mappings if row["backing_name"].startswith("lppwr")
            ]
            return "7,vios-A,3" if owned else HMC_NO_RESULTS
        return super()._hmc_run_command(kwargs)

    def _hmc_map_storage_to_lpar(self, kwargs):
        self.mappings.append(
            {
                "id": "vhost1/vtscsi1",
                "lpar_uuid": kwargs["lpar_name_or_uuid"],
                "backing_kind": "VirtualDisk",
                "backing_name": kwargs["storage_name"],
            }
        )
        name = self.name_of(self.by_selector(kwargs["lpar_name_or_uuid"]))
        self.adapters.add(f"3,{name},7")
        return {"mapped": True}

    def _hmc_detach_storage_mapping(self, kwargs):
        mapping = next(
            row for row in self.mappings if row["id"] == kwargs["mapping_id"]
        )
        self.detach_states.append(self.by_selector(mapping["lpar_uuid"])["state"])
        if self.failure == "no-effect":
            return HMCError("REST0126 HSCL2957 no RMC", 500)
        result = super()._hmc_detach_storage_mapping(kwargs)
        self.adapters = {row for row in self.adapters if not row.startswith("3,")}
        if self.failure == "effect":
            return HMCError("REST0126 HSCL2957 no RMC", 500)
        return result


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(lpar_power, "_STATE_POLL_DELAY_S", 0)
    monkeypatch.setattr(lpar_power, "_STATE_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(lpar_power, "_power_operations_authorized", lambda: True)


def run_probe(world, schemas, output_schemas):
    state = runner.RunState(
        config=live_config.LiveTestConfig(
            system_name=SYSTEM, vdisk_volume_group_name=GROUP
        ),
        group="lpar-power",
        schemas=schemas,
    )
    asyncio.run(
        detach_probe.exercise_detach_probe(FakeClient(world, output_schemas), state)
    )
    return state


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("wrong-group", "runs only"),
        ("power-refused", "HMC_AUTHORIZE_POWER_OPERATIONS"),
        ("no-vios", "exactly one is needed"),
        ("unreadable-baseline", "baseline did not read"),
        ("scratch-partition", "live_test_recovery.py"),
        ("scratch-volume", "live_test_recovery.py"),
    ],
)
def test_detach_probe_refuses_unadmitted_or_stranded_assets_without_writes(
    monkeypatch,
    schemas,
    output_schemas,
    fault,
    reason,
):
    world = ProbeWorld()
    if fault == "power-refused":
        monkeypatch.setattr(lpar_power, "_power_operations_authorized", lambda: False)
    elif fault == "no-vios":
        world.overrides["hmc_list_vios"] = lambda _: {
            "entries": [],
            "unreadable_systems": [],
        }
    elif fault == "unreadable-baseline":
        world.overrides["hmc_run_command"] = lambda _: HMCError(
            "baseline unavailable", 500
        )
    elif fault == "scratch-partition":
        world._new(
            lpar_power.NAME_PREFIX + "deadbeef-p", "foreign-owner", lpar_power.RESOURCES
        )
    elif fault == "scratch-volume":
        world.volumes.add(lpar_power.VOLUME_PREFIX + "deadbeef")
    before = (
        dict(world.partitions),
        set(world.volumes),
        list(world.mappings),
        set(world.adapters),
    )
    state = runner.RunState(
        config=live_config.LiveTestConfig(
            system_name=SYSTEM, vdisk_volume_group_name=GROUP
        ),
        group="all" if fault == "wrong-group" else "lpar-power",
        schemas=schemas,
    )
    asyncio.run(
        detach_probe.exercise_detach_probe(FakeClient(world, output_schemas), state)
    )
    assert not (arm.MUTATIONS | {"hmc_map_storage_to_lpar"}).intersection(world.tools())
    assert (world.partitions, world.volumes, world.mappings, world.adapters) == before
    assert state.results[-1]["tool"] == "detach probe"
    assert state.results[-1]["status"] == "SKIP"
    assert reason in state.results[-1]["note"]


@pytest.mark.parametrize("failure", [None, "effect"])
def test_remaining_cycle_preserves_baseline_and_original_status(
    failure,
    schemas,
    output_schemas,
):
    world = ProbeWorld()
    world.failure = failure
    before = (list(world.mappings), set(world.adapters), set(world.volumes))
    events = []
    real_map = world._hmc_map_storage_to_lpar
    real_power = world._hmc_power_on_lpar
    real_detach = world._hmc_detach_storage_mapping

    def map_na(kwargs):
        assert (
            world.by_selector(kwargs["lpar_name_or_uuid"])["state"] == "Not Activated"
        )
        events.append("attach NA")
        return real_map(kwargs)

    def activate_mapped(kwargs):
        assert any(row["backing_name"].startswith("lppwr") for row in world.mappings)
        assert kwargs["boot_mode"] == "of"
        events.append("activate OF")
        return real_power(kwargs)

    def detach_of(kwargs):
        events.append("detach OF")
        return real_detach(kwargs)

    world.overrides.update(
        {
            "hmc_map_storage_to_lpar": map_na,
            "hmc_power_on_lpar": activate_mapped,
            "hmc_detach_storage_mapping": detach_of,
        }
    )
    state = run_probe(world, schemas, output_schemas)
    assert events == ["attach NA", "activate OF", "detach OF"]
    assert world.detach_states == ["Open Firmware"]
    assert len(world.calls_to("hmc_map_storage_to_lpar")) == 1
    assert len(world.calls_to("hmc_power_on_lpar")) == 1
    assert len(world.calls_to("hmc_detach_storage_mapping")) == 1
    assert len(world.calls_to("hmc_power_off_lpar")) == 1
    assert len(world.calls_to("hmc_delete_virtual_disk")) == 1
    assert len(world.calls_to("hmc_delete_lpar")) == 1
    assert not (
        {"hmc_provision_lpar", "hmc_decommission_lpar", "hmc_power_lpar"}
        & set(world.tools())
    )
    assert not world.partitions and not state.observations
    assert (world.mappings, world.adapters, world.volumes) == before
    compared = [
        row for row in state.results if row["tool"] == "detach probe comparison 3"
    ]
    assert len(compared) == 1
    result = compared[0]
    assert result["status"] == ("FAIL" if failure else "PASS")
    assert result["data"]["before_attach_context"]["client"]["state"] == "Not Activated"
    assert (
        result["data"]["before_activation_context"]["client"]["state"]
        == "Not Activated"
    )
    assert result["data"]["before_context"]["client"]["state"] == "Open Firmware"
    assert result["data"]["after_context"]["client"]["state"] == "Open Firmware"
    assert result["data"]["mapping_absent"] == ("True" if failure else True)
    if failure:
        assert result["data"]["http_status"] == "500"
        assert result["data"]["codes"] == ["REST0126", "HSCL2957"]
    else:
        assert not [row for row in state.results if row["status"] == "FAIL"]


@pytest.mark.parametrize(
    "fault",
    [
        "refused",
        "effect",
        "mapping",
        "adapter",
        "duplicate",
        "uuid",
        "token",
        "state",
        "rmc",
        "read",
        "interrupt",
    ],
)
def test_activation_uncertainty_retains_without_detach(fault, schemas, output_schemas):
    world = ProbeWorld()
    real = world._hmc_power_on_lpar

    def injected(kwargs):
        if fault == "refused":
            return HMCError("activation refused", 500)
        if fault == "interrupt":
            raise KeyboardInterrupt
        result = real(kwargs)
        if fault == "effect":
            return HMCError("activation failed despite state change", 500)
        if fault == "mapping":
            world.mappings[0]["backing_name"] = "changed-protected-disk"
        if fault == "adapter":
            world.adapters.add("8,foreign,9")
        if fault in {"uuid", "token", "state"}:
            next(iter(world.partitions.values()))[fault] = (
                "Running" if fault == "state" else "foreign-value"
            )
        if fault == "read":
            world.overrides["hmc_list_storage_mappings"] = lambda _: HMCError(
                "unreadable"
            )
        if fault in {"duplicate", "rmc"}:
            original = world._hmc_run_command

            def listing(kw):
                text = original(kw)
                if (
                    fault == "duplicate"
                    and "lpar_names" in kw["cmd"]
                    and "--rsubtype scsi" in kw["cmd"]
                ):
                    return "7,vios-A,3\n7,vios-A,3"
                return (
                    text.replace(
                        "vioserver,Running,active", "vioserver,Running,inactive"
                    )
                    if fault == "rmc"
                    else text
                )

            world.overrides["hmc_run_command"] = listing
        return result

    world.overrides["hmc_power_on_lpar"] = injected
    if fault == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            run_probe(world, schemas, output_schemas)
    else:
        state = run_probe(world, schemas, output_schemas)
        assert any(row["tool"] == "detach probe stop" for row in state.results)
    assert len(world.calls_to("hmc_map_storage_to_lpar")) == 1
    assert len(world.calls_to("hmc_power_on_lpar")) == 1
    assert not world.calls_to("hmc_detach_storage_mapping")
    assert not world.calls_to("hmc_power_off_lpar")
    assert not world.calls_to("hmc_delete_virtual_disk")
    assert not world.calls_to("hmc_delete_lpar")
    assert world.partitions and any(name.startswith("lppwr") for name in world.volumes)


@pytest.mark.parametrize(
    "tool,later",
    [
        ("hmc_delete_virtual_disk", ["hmc_power_off_lpar", "hmc_delete_lpar"]),
        ("hmc_power_off_lpar", ["hmc_delete_lpar"]),
        ("hmc_delete_lpar", []),
    ],
)
@pytest.mark.parametrize("fault", ["refused", "effect", "interrupt"])
def test_cleanup_failure_never_dispatches_next_write(
    tool, later, fault, schemas, output_schemas
):
    world = ProbeWorld()
    real = getattr(world, "_" + tool)

    def injected(kwargs):
        if fault == "interrupt":
            raise KeyboardInterrupt
        if fault == "effect":
            real(kwargs)
        return HMCError("cleanup response failed", 500)

    world.overrides[tool] = injected
    if fault == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            run_probe(world, schemas, output_schemas)
    else:
        state = run_probe(world, schemas, output_schemas)
        assert any(row["tool"] == "detach probe stop" for row in state.results)
        assert any(
            row["tool"].startswith(tool + " (") and row["status"] == "FAIL"
            for row in state.results
        )
    assert len(world.calls_to(tool)) == 1
    assert all(not world.calls_to(next_tool) for next_tool in later)
    assert len(world.calls_to("hmc_map_storage_to_lpar")) == 1
    assert len(world.calls_to("hmc_detach_storage_mapping")) == 1
    if tool != "hmc_delete_lpar" or fault != "effect":
        assert world.partitions


@pytest.mark.parametrize(
    "phase,fault,detaches",
    [
        ("attach", "refused", 0),
        ("attach", "removed", 0),
        ("attach", "changed", 0),
        ("attach", "extra", 0),
        ("attach", "client-extra", 0),
        ("attach", "duplicate", 0),
        ("attach", "malformed", 0),
        ("detach", "no-effect", 1),
        ("detach", "read", 1),
        ("detach", "uuid", 1),
        ("detach", "token", 1),
        ("detach", "other", 1),
        ("detach", "adapter", 1),
        ("detach", "interrupt", 1),
    ],
)
def test_uncertainty_retains_assets_without_further_writes(
    phase,
    fault,
    detaches,
    schemas,
    output_schemas,
):
    world = ProbeWorld()
    protected = "4,other-client,5"
    world.adapters.add(protected)
    tool = (
        "hmc_map_storage_to_lpar" if phase == "attach" else "hmc_detach_storage_mapping"
    )
    real = getattr(world, "_" + tool)

    def injected(kwargs):
        if fault == "refused":
            return HMCError("attach refused", 500)
        if fault == "interrupt":
            raise KeyboardInterrupt
        if fault == "no-effect":
            world.failure = fault
        result = real(kwargs)
        if fault in {"removed", "changed"}:
            world.adapters.remove(protected)
        if fault == "changed":
            world.adapters.add("4,changed-client,5")
        if fault in {"extra", "adapter"}:
            world.adapters.add("8,foreign,9")
        if fault in {"client-extra", "duplicate", "malformed"}:
            original = world._hmc_run_command
            world.overrides["hmc_run_command"] = lambda kw: (
                {
                    "client-extra": "7,vios-A,3\n8,foreign,9",
                    "duplicate": "7,vios-A,3\n7,vios-A,3",
                    "malformed": "7,vios-A",
                }[fault]
                if "lpar_names" in kw["cmd"] and "--rsubtype scsi" in kw["cmd"]
                else original(kw)
            )
        if fault == "read":
            world.overrides["hmc_list_storage_mappings"] = lambda _: HMCError(
                "unreadable"
            )
        if fault in {"uuid", "token"}:
            next(iter(world.partitions.values()))[fault] = "foreign-value"
        if fault == "other":
            world.mappings[0]["backing_name"] = "changed-foreign-disk"
        return result

    world.overrides[tool] = injected
    if fault == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            run_probe(world, schemas, output_schemas)
    else:
        state = run_probe(world, schemas, output_schemas)
        assert any(row["tool"] == "detach probe stop" for row in state.results)
    assert len(world.calls_to("hmc_map_storage_to_lpar")) == 1
    assert len(world.calls_to("hmc_detach_storage_mapping")) == detaches
    if phase == "attach":
        assert not world.calls_to("hmc_power_on_lpar")
        assert not world.calls_to("hmc_power_off_lpar")
    assert not world.calls_to("hmc_delete_lpar")
    assert not world.calls_to("hmc_delete_virtual_disk")
    assert world.partitions and any(name.startswith("lppwr") for name in world.volumes)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [object()],
        [{"id": "x"}],
        [
            {"id": "x", "lpar_uuid": None, "backing_kind": None, "backing_name": None},
            {"id": "x", "lpar_uuid": None, "backing_kind": None, "backing_name": None},
        ],
    ],
)
def test_malformed_or_duplicate_mapping_refuses(value):
    with pytest.raises(detach_probe._Stop):
        detach_probe.mapping_rows(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "1,x",
        "1,,2",
        "x,peer,2",
        "1,peer,x",
        "1,peer,2\n1,peer,2",
        "1,peer,2\n1,other,3",
        "01,peer,2\n1,other,3",
        "x" * 1_048_577,
        "\n".join(f"{n},peer,2" for n in range(4097)),
    ],
    ids=[
        "none",
        "blank",
        "short",
        "empty-peer",
        "bad-slot",
        "bad-remote-slot",
        "identical",
        "conflicting",
        "numeric-alias",
        "bytes",
        "rows",
    ],
)
def test_raw_adapter_validation_precedes_set_conversion(value):
    with pytest.raises(detach_probe._Stop):
        detach_probe.adapter_rows(value)


def test_empty_sentinel_and_exact_adapter_fields():
    assert detach_probe.adapter_rows(HMC_NO_RESULTS) == frozenset()
    assert detach_probe.adapter_rows("3,peer,7") == frozenset({"3,peer,7"})


def test_typed_mapping_retains_exact_fields():
    @dataclass
    class Row:
        id: str
        lpar_uuid: str
        backing_kind: str
        backing_name: str

    row = Row("vhost1/vtscsi1", "client-uuid", "VirtualDisk", "run-disk")
    assert detach_probe.mapping_rows([row]) == frozenset({tuple(vars(row).values())})


def test_surviving_mapping_snapshot_stops_even_if_later_reads_would_converge(
    schemas, output_schemas
):
    world = ProbeWorld()
    world.failure = "no-effect"
    listing = world._hmc_list_storage_mappings

    def converging(kwargs):
        rows = listing(kwargs)
        if world.detach_states:
            world.mappings = [
                row
                for row in world.mappings
                if not row["backing_name"].startswith("lppwr")
            ]
            world.adapters = {row for row in world.adapters if not row.startswith("3,")}
        return rows

    world.overrides["hmc_list_storage_mappings"] = converging
    state = run_probe(world, schemas, output_schemas)
    assert len(world.calls_to("hmc_detach_storage_mapping")) == 1
    assert len(world.calls_to("hmc_power_on_lpar")) == 1
    assert not world.calls_to("hmc_delete_virtual_disk")
    assert not world.calls_to("hmc_delete_lpar")
    assert world.partitions
    assert any(row["tool"] == "detach probe stop" for row in state.results)
