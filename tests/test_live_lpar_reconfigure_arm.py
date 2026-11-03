"""ST70 arm sequencing and cleanup without hardware (#1226, #1846, ADRs 0314, 0322)."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from live_test import lpar_config
from live_test import lpar_reconfigure as arm


class Surface:
    """The MCP tools the arm calls, over one simulated scratch partition."""

    def __init__(self, *, persists=True, firmware_applied="pending_activation"):
        self.persists = persists
        self.firmware_applied = firmware_applied
        self.state, self.name = "not activated", None
        self.current = {"memory_mib": 2048, "processors": 1}
        self.saved = {"memory_mib": 2048, "processors": 1}
        self.calls: list[str] = []
        # Disk side: volumes in the group, the run disk's mapping, client adapters.
        self.volumes: set[str] = {"keep1"}
        self.mapped: str | None = None
        self.saved_vscsi = self.running_vscsi = False
        self.server_slots = {"5,vios-peer,2"}
        self.detach_leaves_mapping = False

    def __call__(self, _client, tool, **kwargs):
        self.calls.append(tool)
        return getattr(self, tool)(**kwargs)

    def hmc_create_lpar(self, name, **_kwargs):
        self.name = name
        return "PASS", {"lpar": {"UUID": "fixture"}, "workflow_completed": True}

    def hmc_reconfigure_lpar(self, patch=None, continuation="none", **_kwargs):
        if continuation == "abandon":
            return "PASS", {"outcome": "completed"}
        if "disks" in patch:
            for add in patch["disks"].get("add", []):
                self.volumes.add(add["name"])
                self.mapped, self.saved_vscsi = add["name"], True
                self.server_slots.add("6,scratch,2")
            if patch["disks"].get("detach") and not self.detach_leaves_mapping:
                self.mapped = None
            field = "disks.add" if "add" in patch["disks"] else "disks.detach"
            changes = [{"field": field, "applied": "profile"}]
            return "PASS", {"outcome": "completed", "result": {"changes": changes}}
        if "name" in patch:
            self.name = patch["name"]
            return "PASS", {"outcome": "completed", "result": {"changes": []}}
        self.saved |= patch
        applied = "profile" if self.state == "not activated" else self.firmware_applied
        if self.state == "not activated":
            self.current |= patch
        changes = [{"field": key, "applied": applied} for key in patch]
        return "PASS", {"outcome": "completed", "result": {"changes": changes}}

    def hmc_get_lpar(self, **_kwargs):
        return "PASS", {
            "PartitionName": self.name,
            "AssociatedPartitionProfile": {
                "href": "https://hmc.test/rest/api/uom/LogicalPartition/x/"
                "LogicalPartitionProfile/0000000a-0000-4000-c000-000000000000"
            },
            "PartitionMemoryConfiguration": {
                "CurrentMemory": str(self.current["memory_mib"])
            },
            "PartitionProcessorConfiguration": {
                "CurrentSharedProcessorConfiguration": {
                    "AllocatedVirtualProcessors": str(self.current["processors"])
                }
            },
        }

    def hmc_list_partition_profiles(self, **_kwargs):
        fields = {
            "ProfileMemory": {"DesiredMemory": str(self.saved["memory_mib"])},
            "ProcessorAttributes": {
                "SharedProcessorConfiguration": {
                    "DesiredVirtualProcessors": str(self.saved["processors"])
                }
            },
        }
        return "PASS", {"profiles": [{"fields": fields}]}

    def hmc_power_on_lpar(self, **_kwargs):
        self.state = "open firmware"
        if self.persists:
            self.current = dict(self.saved)
            self.running_vscsi = self.saved_vscsi
        return "PASS", {"job": {"status": "COMPLETED_OK"}}

    def hmc_power_off_lpar(self, **_kwargs):
        self.state = "not activated"
        return "PASS", {}

    def hmc_get_lpar_state(self, **_kwargs):
        return "PASS", self.state

    def hmc_list_vios(self, **_kwargs):
        return "PASS", [{"UUID": "vios-uuid", "PartitionID": "1"}]

    def hmc_list_volume_groups(self, **_kwargs):
        return "PASS", [{"name": "clientvg", "uuid": "vg-uuid", "free_space_gib": 50}]

    def hmc_list_storage_mappings(self, **_kwargs):
        if self.mapped is None:
            return "PASS", []
        return "PASS", [
            {
                "id": "vhost6/vtscsi0",
                "lpar_uuid": "FIXTURE",
                "backing_name": self.mapped,
            }
        ]

    def hmc_detach_storage_mapping(self, **_kwargs):
        self.mapped = None
        return "PASS", {}

    def hmc_delete_virtual_disk(self, disk_name, **_kwargs):
        self.volumes.discard(disk_name)
        return "PASS", {}

    def hmc_run_command(self, cmd, **_kwargs):
        if cmd.startswith("lssyscfg -r prof"):
            listing = '"2/client/1/vios/6/1"' if self.saved_vscsi else "none"
            return "PASS", f"default_profile,{listing}\nhmcprec-00ff,none"
        if "-F adapter_type" in cmd:
            return "PASS", "client" if self.running_vscsi else "No results were found."
        if "lsvg -lv" in cmd:
            names = "\n".join(sorted(self.volumes))
            return "PASS", f"clientvg:\nLV NAME TYPE\n{names}"
        if "remote_lpar_name" in cmd:
            return "PASS", "\n".join(sorted(self.server_slots))
        raise AssertionError(cmd)


def _state(surface):
    return SimpleNamespace(
        group=arm.GROUP,
        config=SimpleNamespace(
            system_name="system", vdisk_volume_group_name="clientvg"
        ),
        call=AsyncMock(side_effect=surface),
        skip=Mock(),
        record=Mock(),
        record_verified=Mock(),
    )


@pytest.fixture
def cleanup(monkeypatch):
    monkeypatch.setattr(
        arm, "_read_pools", AsyncMock(return_value=(SimpleNamespace(names=[]), 256))
    )

    async def teardown(_client, _state, run, **kwargs):
        assert kwargs == {"inactive_only": True}
        run.deleted = True

    teardown_mock = AsyncMock(side_effect=teardown)
    monkeypatch.setattr(arm, "_teardown", teardown_mock)
    monkeypatch.setattr(arm, "_compare", AsyncMock(return_value=True))
    monkeypatch.setattr(lpar_config, "_STATE_POLL_DELAY_S", 0)
    return teardown_mock


def _held(state):
    kwargs = state.record_verified.call_args.kwargs
    return {a.id: a.holds for a in kwargs["assertions"]}, kwargs["cleanup"]


@pytest.mark.asyncio
async def test_every_assertion_holds_and_the_partition_is_cleaned(cleanup):
    surface = Surface()
    state = _state(surface)
    await arm.exercise_lpar_reconfigure(object(), state)
    held, cleaned = _held(state)
    assert all(held.values()) and len(held) == 9
    assert cleaned == "passed"
    assert surface.volumes == {"keep1"} and surface.mapped is None
    manual = [c.args[1] for c in state.record.call_args_list if c.args[2] == "FAIL"]
    assert manual == ["VIOS server adapter teardown"]
    assert surface.name.endswith("-r")
    assert surface.state == "not activated"
    cleanup.assert_awaited_once()
    state.skip.assert_any_call(
        arm.SUBTASK, f"{arm.TOOL} (live DLPAR)", arm.LIVE_DLPAR_SKIP
    )


@pytest.mark.asyncio
async def test_a_change_lost_at_activation_fails_and_still_cleans(cleanup):
    surface = Surface(persists=False)
    state = _state(surface)
    await arm.exercise_lpar_reconfigure(object(), state)
    held, _ = _held(state)
    assert held["pending-change-survives-profile-activation"] is False
    assert surface.state == "not activated"
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_live_classification_at_firmware_fails(cleanup):
    state = _state(Surface(firmware_applied="live"))
    await arm.exercise_lpar_reconfigure(object(), state)
    held, _ = _held(state)
    assert held["firmware-change-classified-pending-activation"] is False


@pytest.mark.asyncio
async def test_a_failed_create_records_nothing_held_and_still_cleans(cleanup):
    surface = Surface()
    surface.hmc_create_lpar = lambda **_kwargs: ("FAIL", "refused")
    state = _state(surface)
    await arm.exercise_lpar_reconfigure(object(), state)
    held, _ = _held(state)
    assert not any(held.values())
    assert "hmc_reconfigure_lpar" not in surface.calls
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_outside_its_group_the_arm_only_skips(cleanup):
    surface = Surface()
    state = _state(surface)
    state.group = "all"
    await arm.exercise_lpar_reconfigure(object(), state)
    assert surface.calls == []
    state.record_verified.assert_not_called()


@pytest.mark.asyncio
async def test_an_added_disk_lost_at_activation_fails_and_is_still_deleted(cleanup):
    surface = Surface()
    surface.hmc_power_on_lpar = lambda **_k: (
        setattr(surface, "state", "open firmware") or ("PASS", {})
    )
    state = _state(surface)
    await arm.exercise_lpar_reconfigure(object(), state)
    held, _ = _held(state)
    assert held["added-disk-in-default-profile"] is True
    assert held["added-disk-survives-profile-activation"] is False
    assert surface.volumes == {"keep1"}


@pytest.mark.asyncio
async def test_a_detach_that_leaves_the_mapping_fails_and_teardown_detaches(cleanup):
    surface = Surface()
    surface.detach_leaves_mapping = True
    state = _state(surface)
    await arm.exercise_lpar_reconfigure(object(), state)
    held, _ = _held(state)
    assert held["detach-removes-only-the-mapping"] is False
    assert "hmc_detach_storage_mapping" in surface.calls
    assert surface.mapped is None and surface.volumes == {"keep1"}


@pytest.mark.asyncio
async def test_without_the_volume_group_the_disk_assertions_fail(cleanup):
    surface = Surface()
    surface.hmc_list_volume_groups = lambda **_k: ("PASS", [])
    state = _state(surface)
    await arm.exercise_lpar_reconfigure(object(), state)
    held, _ = _held(state)
    assert not any(held[name] for name in arm.DISK_ASSERTIONS)
    assert held["rename-persists-across-activations"] is True


def test_scratch_volumes_match_only_the_arm_prefix():
    assert arm.scratch_volumes(["lprcf0a1b2c3d", "lppwr1", "keep"]) == ["lprcf0a1b2c3d"]
