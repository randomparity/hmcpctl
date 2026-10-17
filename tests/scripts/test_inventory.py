"""Behavioral tests for the live-runner inventory scenarios."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

LIVE_TEST_ROOT = Path(__file__).parents[2] / "scripts"
sys.path.insert(0, str(LIVE_TEST_ROOT))
from live_test import inventory, network, storage  # noqa: E402


class ScenarioState:
    """Small state seam that records inventory calls and artifact mutations."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.group = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: list[tuple[int, str, str, Any]] = []
        self.config = SimpleNamespace(
            system_name="system one",
            lp3_name="lp three",
            vlan_range_start=3000,
            vlan_range_end=3099,
            vdisk_name="disk-one",
            vdisk_volume_group_name="rootvg",
        )
        self.artifacts = SimpleNamespace(
            lp3_uuid=None,
            lp3_baseline={},
            vios_uuid=None,
            vios_partition_id=None,
            test_vswitch_id=None,
            test_vlan_id=None,
            vg_uuid=None,
            vdisk_vg_name=None,
        )

    async def call(self, _client: object, tool: str, **kwargs: Any) -> tuple[str, Any]:
        self.calls.append((tool, kwargs))
        response = self.responses.get(tool, {})
        if callable(response):
            response = response(kwargs)
        return "PASS", response

    def record(self, stage: int, tool: str, status: str, data: Any) -> None:
        self.results.append((stage, tool, status, data))

    def skip(self, stage: int, tool: str, reason: str) -> None:
        self.results.append((stage, tool, "SKIP", reason))

    def record_verified(self, stage: int, tool: str, **kwargs: Any) -> None:
        self.results.append((stage, tool, "VERIFIED", kwargs["operation"]))


@pytest.mark.asyncio
async def test_baseline_capture_preserves_identity_and_adapter_topology() -> None:
    def adapters(kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        if kwargs["adapter_type"] == "ClientNetworkAdapter":
            return [{"Resource": {"PortVLANID": "42", "VirtualSwitchID": "7"}}]
        return [
            {
                "Resource": {
                    "RemoteLogicalPartitionID": "2",
                    "ServerAdapter": {"VirtualSlotNumber": "11"},
                }
            }
        ]

    state = ScenarioState(
        {
            "hmc_get_lpar": {"UUID": "lpar-uuid"},
            "hmc_lpar_summary": {"state": "running"},
            "hmc_get_lpar_description": {"description": "baseline"},
            "hmc_get_lpar_msp": True,
            "hmc_get_lpar_proc_compat": "POWER10",
            "hmc_list_adapters": adapters,
            # A FleetListing result arrives as a generated dataclass (ADR 0197).
            "hmc_list_vios": SimpleNamespace(
                entries=[{"UUID": "vios-uuid", "Resource": {"PartitionID": "2"}}],
                unreadable_systems=[],
            ),
            "hmc_run_command": "name=lp three",
        }
    )

    await inventory.capture_lpar_baseline(None, state)

    assert state.artifacts.lp3_uuid == "lpar-uuid"
    assert state.artifacts.vios_uuid == "vios-uuid"
    assert state.artifacts.vios_partition_id == 2
    assert state.artifacts.lp3_baseline["description"] == "baseline"
    assert state.artifacts.lp3_baseline["pvid"] == 42
    assert state.artifacts.lp3_baseline["vswitch_id"] == 7
    assert "vios_partition_id" not in state.artifacts.lp3_baseline
    assert "vios_slot" not in state.artifacts.lp3_baseline
    command = next(
        kwargs["cmd"] for tool, kwargs in state.calls if tool == "hmc_run_command"
    )
    assert "-m 'system one'" in command
    assert "lpar_names=lp three" in command


def test_listed_vlans_parses_each_form_and_keeps_malformed_values() -> None:
    listing = [
        {"Resource": {"NetworkVLANID": "1"}},
        {"Resource": {"VLANId": 20}},
        {"vlan_id": 300},
        {"Resource": {"NetworkVLANID": "trunk"}},
        {"Resource": {}},
    ]

    assert network.listed_vlans(listing) == ({1, 20, 300}, ["trunk"])


@pytest.mark.asyncio
async def test_storage_inventory_resolves_the_owning_group() -> None:
    state = ScenarioState(
        {
            "hmc_list_volume_groups": [
                {
                    "UUID": "vg-uuid",
                    "Resource": {
                        "GroupName": "rootvg",
                        "VirtualDisks": {
                            "VirtualDisk": {
                                "DiskName": "disk-one",
                                "DiskCapacity": "not-a-size",
                            }
                        },
                    },
                }
            ]
        }
    )
    state.artifacts.vios_uuid = "vios-uuid"

    await storage.inventory_storage(None, state)

    assert state.artifacts.vg_uuid == "vg-uuid"
    assert state.artifacts.vdisk_vg_name == "rootvg"
    assert not any(
        tool == "parse virtual disk capacity" for _, tool, _, _ in state.results
    )
    assert [tool for tool, _ in state.calls] == [
        "hmc_list_volume_groups",
        "hmc_list_clusters",
        "hmc_list_shared_storage_pools",
        "hmc_get_shared_storage_pool",
        "hmc_list_io_slots",
        "hmc_list_memory_pools",
    ]


def test_resolver_selects_configured_group_listed_last() -> None:
    state = ScenarioState({})
    listing = [
        {"uuid": "vg-a", "name": "datavg", "free_space_gib": 900},
        {"uuid": "vg-b", "name": "rootvg", "free_space_gib": 1.5},
    ]

    group = storage.resolve_configured_volume_group(state, 16, listing, ("create",))

    assert group is not None
    assert group.uuid == "vg-b"
    assert group.free_space_mib == 1536
    assert state.artifacts.vg_uuid == "vg-b"
    assert state.artifacts.vdisk_vg_name == "rootvg"
    assert state.results == []


def test_resolver_miss_skips_dependents_and_clears_stale_uuid() -> None:
    state = ScenarioState({})
    state.artifacts.vg_uuid = "stale"
    state.artifacts.vdisk_vg_name = "rootvg"

    group = storage.resolve_configured_volume_group(
        state, 16, [{"uuid": "vg-a", "name": "datavg"}], ("create", "get")
    )

    assert group is None
    assert state.artifacts.vg_uuid is None
    assert state.artifacts.vdisk_vg_name is None
    assert [(stage, tool, status) for stage, tool, status, _ in state.results] == [
        (16, "create", "SKIP"),
        (16, "get", "SKIP"),
    ]
    assert "'rootvg' not listed" in state.results[0][3]
