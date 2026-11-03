"""ST70 arm sequencing and cleanup without hardware (#1226, ADR 0314)."""

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

    def __call__(self, _client, tool, **kwargs):
        self.calls.append(tool)
        return getattr(self, tool)(**kwargs)

    def hmc_create_lpar(self, name, **_kwargs):
        self.name = name
        return "PASS", {"lpar": {"UUID": "fixture"}, "workflow_completed": True}

    def hmc_reconfigure_lpar(self, patch=None, continuation="none", **_kwargs):
        if continuation == "abandon":
            return "PASS", {"outcome": "completed"}
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
        return "PASS", {"job": {"status": "COMPLETED_OK"}}

    def hmc_power_off_lpar(self, **_kwargs):
        self.state = "not activated"
        return "PASS", {}

    def hmc_get_lpar_state(self, **_kwargs):
        return "PASS", self.state


def _state(surface):
    return SimpleNamespace(
        group=arm.GROUP,
        config=SimpleNamespace(system_name="system"),
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
    assert all(held.values()) and len(held) == 5
    assert cleaned == "passed"
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
