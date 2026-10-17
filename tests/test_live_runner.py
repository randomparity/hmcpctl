"""Lifecycle tests for the executable live integration runner."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from conftest import live_fixture
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.json_schema_type import json_schema_to_type
from pydantic import TypeAdapter

from hmcpctl.authorization import target_scope
from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.config import ConfigError, HMCConfig
from hmcpctl.documents.storage import VIRTUAL_DISK_NAME_MAX
from hmcpctl.jobs import JobOutcome
from hmcpctl.operations.virtualization.pcie import (
    InventorySelector,
    SriovLogicalPortChangeResult,
)
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.ssh import affinity as ssh_affinity
from hmcpctl.xmlutil import parse_feed

_RUNNER_PATH = Path(__file__).parents[1] / "scripts" / "live_test_runner.py"
sys.path.insert(0, str(_RUNNER_PATH.parent))
import scenario_gap_report  # noqa: E402
from live_test import (  # noqa: E402
    bare_cec,
    connectivity,
    escape_hatch,
    inventory,
    lpar,
    lpar_config,
    lpar_power,
    metrics,
    network,
    observation,
    pcie,
    profiles,
    provisioning,
    results,
    storage,
    storage_lifecycle,
    users,
    vios_backup,
    vmedia,
)

LIVE_WORKFLOW_MODULES = (
    bare_cec,
    connectivity,
    escape_hatch,
    inventory,
    lpar,
    lpar_config,
    lpar_power,
    metrics,
    network,
    pcie,
    profiles,
    provisioning,
    storage,
    storage_lifecycle,
    users,
    vios_backup,
    vmedia,
)

_SPEC = importlib.util.spec_from_file_location("hmc_live_test_runner", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = runner
_SPEC.loader.exec_module(runner)


class _FakeClient:
    def __init__(self, _server):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def list_tools(self):
        # An empty schema map leaves the runtime dispatch guard inert, which is
        # what these isolation tests want: they script their own tool responses.
        return []


class _ToolResult:
    def __init__(self, *, data=None, content=None):
        self.data = data
        self.content = content or []


class _TextBlock:
    def __init__(self, text):
        self.text = text


class _ScriptedClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def call_tool(self, _tool, _kwargs):
        self.calls.append((_tool, _kwargs))
        if self.error is not None:
            raise self.error
        return self.result


def _failure(text: str) -> observation.CallFailure:
    """The shape `RunState.call` really returns on failure, for a scripted stub."""
    return observation.classify_failure(RuntimeError(text))


class _ScriptedSriovState(runner.RunState):
    """Run SR-IOV phases against an ordered in-memory tool transcript."""

    def __init__(self, responses: list[tuple[str, str, object]]):
        super().__init__()
        self._responses = iter(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, _client, tool, *, expected=(), reuse_gaps=True, **kwargs):
        self.calls.append((tool, kwargs))
        expected_tool, status, data = next(self._responses)
        assert tool == expected_tool
        return status, data


def _logical_port_state(
    state: _ScriptedSriovState, *, owner: str | None = None
) -> dict[str, object]:
    items: list[dict[str, object]] = []
    if owner is not None:
        items.append(
            {
                "logical_port_id": str(state.config.sriov_logical_port_id),
                "availability": "1",
                "owner_lpar": owner,
                "capacity_percent": state.config.sriov_capacity_percent,
            }
        )
    return {"items": items}


def _profile_state(value: str = "none") -> tuple[str, str, object]:
    return ("hmc_run_command", "PASS", value)


def test_sriov_baseline_helpers_require_healthy_adapter() -> None:
    """Baseline predicates reject wrong mode/availability and accept healthy data."""
    assert pcie._adapter_is_healthy(
        {"items": [{"adapter_id": "17", "mode": "sriov", "availability": "1"}]}, "17"
    )
    assert not pcie._adapter_is_healthy(
        {"items": [{"adapter_id": "17", "mode": "ded", "availability": "1"}]}, "17"
    )


def test_sriov_baseline_helpers_compute_capacity_and_configuration() -> None:
    """Capacity and clean-port predicates handle unconfigured rows and reject malformed data."""
    data = {
        "items": [
            {"capacity_percent": "25", "availability": "1"},
            {"capacity_percent": "bad", "availability": "1"},
            {"capacity_percent": "50", "availability": "unconfigured"},
        ]
    }
    with pytest.raises(ValueError, match="row 1.*capacity_percent"):
        pcie._available_capacity(data)
    assert not pcie._logical_port_is_configured({"items": []}, "917003")
    assert pcie._logical_port_is_configured(
        {"items": [{"logical_port_id": "917003", "availability": "1"}]},
        "917003",
    )
    # The rows are projected as str; comparing the numeric config value is the
    # #763 defect and must not read as "no such port".
    assert not pcie._logical_port_is_configured(
        {"items": [{"logical_port_id": "917003", "availability": "1"}]},
        917003,
    )


@pytest.mark.asyncio
async def test_sriov_phases_assign_verify_unassign_and_reassign() -> None:
    state = _ScriptedSriovState([])
    owned = _logical_port_state(state, owner=state.config.lp3_name)
    state._responses = iter(
        [
            ("hmc_assign_sriov_logical_port", "PASS", {"changed": True}),
            ("hmc_list_sriov_logical_ports", "PASS", owned),
            _profile_state(),
            ("hmc_unassign_sriov_logical_port", "PASS", {"changed": True}),
            ("hmc_list_sriov_logical_ports", "PASS", owned),
            _profile_state(),
            ("hmc_assign_sriov_logical_port", "PASS", {"changed": True}),
            ("hmc_list_sriov_logical_ports", "PASS", owned),
            _profile_state(),
        ]
    )

    evidence = pcie._SriovEvidence()
    assert await pcie.assign_sriov_to_lp3(object(), state, evidence)
    assert await pcie.verify_sriov_assigned(object(), state, True, evidence)
    assert await pcie.unassign_sriov_from_lp3(object(), state)
    assert await pcie.reassign_sriov_to_lp3(object(), state, evidence)

    assign_tool, assign_args = state.calls[0]
    assert assign_tool == "hmc_assign_sriov_logical_port"
    assert assign_args == {
        "system_name_or_uuid": state.config.system_name,
        "lpar_name_or_uuid": state.config.lp3_name,
        "adapter_id": str(state.config.sriov_adapter_id),
        "physical_port_id": str(state.config.sriov_physical_port_id),
        "logical_port_id": str(state.config.sriov_logical_port_id),
        "capacity_percent": state.config.sriov_capacity_percent,
        "profile_name": state.config.sriov_profile_name,
        "ownership_override": True,
    }
    assert state.calls[3][0] == "hmc_unassign_sriov_logical_port"
    assert state.calls[3][1]["ownership_override"] is True
    assert [entry["status"] for entry in state.results if entry["subtask"] == 26] == [
        "PASS",
        "PASS",
        "PASS",
        "PASS",
    ]


@pytest.mark.asyncio
async def test_sriov_verify_records_wrong_owner_without_mutation() -> None:
    state = _ScriptedSriovState([])
    state._responses = iter(
        [
            (
                "hmc_list_sriov_logical_ports",
                "PASS",
                _logical_port_state(state, owner="another-lpar"),
            ),
            _profile_state(),
        ]
    )

    assert not await pcie.verify_sriov_assigned(
        object(), state, True, pcie._SriovEvidence()
    )

    assert [tool for tool, _ in state.calls] == [
        "hmc_list_sriov_logical_ports",
        "hmc_run_command",
    ]
    assert any(
        entry["tool"] == "sriov owner check" and entry["status"] == "FAIL"
        for entry in state.results
    )


@pytest.mark.asyncio
async def test_sriov_cleanup_refuses_unowned_or_unconfigured_ports() -> None:
    unconfigured = _ScriptedSriovState([])
    unconfigured._responses = iter(
        [
            ("hmc_list_sriov_logical_ports", "PASS", _logical_port_state(unconfigured)),
            _profile_state(),
            ("hmc_list_sriov_logical_ports", "PASS", _logical_port_state(unconfigured)),
            ("hmc_list_sriov_logical_ports", "PASS", _logical_port_state(unconfigured)),
            _profile_state(),
        ]
    )
    await pcie.cleanup_sriov(object(), unconfigured)
    assert "hmc_unassign_sriov_logical_port" not in [
        tool for tool, _ in unconfigured.calls
    ]

    foreign = _ScriptedSriovState([])
    foreign._responses = iter(
        [
            (
                "hmc_list_sriov_logical_ports",
                "PASS",
                _logical_port_state(foreign, owner="another-lpar"),
            ),
            _profile_state(),
        ]
    )
    await pcie.cleanup_sriov(object(), foreign)
    assert [tool for tool, _ in foreign.calls] == [
        "hmc_list_sriov_logical_ports",
        "hmc_run_command",
    ]
    assert any(
        entry["tool"] == "sriov cleanup: owner mismatch"
        and "MANUAL RECOVERY REQUIRED" in str(entry["data"])
        for entry in foreign.results
    )


@pytest.mark.asyncio
async def test_sriov_cleanup_removes_owned_port_and_verifies_baseline() -> None:
    state = _ScriptedSriovState([])
    state._responses = iter(
        [
            (
                "hmc_list_sriov_logical_ports",
                "PASS",
                _logical_port_state(state, owner=state.config.lp3_name),
            ),
            _profile_state("configured-port"),
            ("hmc_unassign_sriov_logical_port", "PASS", {"changed": True}),
            ("hmc_run_command", "PASS", "removed"),
            ("hmc_list_sriov_logical_ports", "PASS", _logical_port_state(state)),
            ("hmc_list_sriov_logical_ports", "PASS", _logical_port_state(state)),
            _profile_state(),
        ]
    )

    await pcie.cleanup_sriov(object(), state)

    assert [tool for tool, _ in state.calls] == [
        "hmc_list_sriov_logical_ports",
        "hmc_run_command",
        "hmc_unassign_sriov_logical_port",
        "hmc_run_command",
        "hmc_list_sriov_logical_ports",
        "hmc_list_sriov_logical_ports",
        "hmc_run_command",
    ]
    cleanup_command = state.calls[3][1]["cmd"]
    assert "chhwres -r sriov --rsubtype logport" in cleanup_command
    assert " -o r -p " in cleanup_command
    assert all(entry["status"] == "PASS" for entry in state.results)


def _sriov_arm_transcript(
    state: _ScriptedSriovState,
    *,
    assign_status: str = "PASS",
    chhwres_status: str = "PASS",
    profile_prepopulated: bool = True,
    unassign_profile_read: tuple[str, str, object] | None = None,
) -> list[tuple[str, str, object]]:
    """Every call `exercise_sriov_assignment` makes after its baseline, in order.

    Answers as the operations do. *profile_prepopulated* is baseline (b): the
    profile lists the port, so the first profile unassign dispatches; from `none`
    it is the idempotent `changed=False`. The dynamic assign never touches the
    profile, and the profile-only unassign leaves the effective port in place,
    so the reassign always finds it assigned as asked and changes nothing.
    """
    owned = _logical_port_state(state, owner=state.config.lp3_name)
    free = _logical_port_state(state)
    profile = "configured-port" if profile_prepopulated else "none"
    transcript = [
        ("hmc_assign_sriov_logical_port", assign_status, {"changed": True}),
        ("hmc_list_sriov_logical_ports", "PASS", owned),
        _profile_state(profile),
    ]
    if assign_status == "PASS":
        transcript += [
            ("hmc_unassign_sriov_logical_port", "PASS", {"changed": profile != "none"}),
            ("hmc_list_sriov_logical_ports", "PASS", owned),
            unassign_profile_read or _profile_state(),
            ("hmc_assign_sriov_logical_port", "PASS", {"changed": False}),
            ("hmc_list_sriov_logical_ports", "PASS", owned),
            _profile_state(),
        ]
        profile = "none"
    transcript += [
        ("hmc_list_sriov_logical_ports", "PASS", owned),
        _profile_state(profile),
        ("hmc_unassign_sriov_logical_port", "PASS", {"changed": profile != "none"}),
        ("hmc_run_command", chhwres_status, "removed"),
    ]
    if chhwres_status == "PASS":
        transcript += [
            ("hmc_list_sriov_logical_ports", "PASS", free),
            ("hmc_list_sriov_logical_ports", "PASS", free),
            _profile_state(),
        ]
    return transcript


async def _run_sriov_arm(monkeypatch, **faults) -> _ScriptedSriovState:
    state = _ScriptedSriovState([])
    state._responses = iter(_sriov_arm_transcript(state, **faults))

    async def baseline_ok(_client, _state) -> bool:
        return True

    monkeypatch.setattr(pcie, "capture_sriov_baseline", baseline_ok)
    await pcie.exercise_sriov_assignment(object(), state)
    assert next(state._responses, None) is None, "transcript not consumed"
    return state


def _sriov_observations(state) -> dict[str, dict]:
    return {item["observation"]["id"]: item for item in state.observations}


@pytest.mark.asyncio
async def test_sriov_arm_emits_verified_observations(monkeypatch) -> None:
    state = await _run_sriov_arm(monkeypatch)

    emitted = _sriov_observations(state)
    assert len(emitted) == len(state.observations), "duplicate observation id"
    assert {
        key: (
            item["operation"],
            item["observation"]["result"],
            item["observation"]["cleanup"],
            sorted(item["observation"]["assertions"]),
        )
        for key, item in emitted.items()
    } == {
        "st25-hmc-assign-sriov-logical-port": (
            "sriov.assign_logical_port",
            "passed",
            "passed",
            sorted(
                [
                    "assign-call-succeeded",
                    "logical-port-configured",
                    "owner-is-target-lpar",
                    "capacity-matches",
                ]
            ),
        ),
        "st26-hmc-unassign-sriov-logical-port": (
            "sriov.unassign_logical_port",
            "passed",
            "not-required",
            ["profile-ports-cleared", "unassign-call-succeeded"],
        ),
    }
    assert {item["observation"]["scenario"] for item in emitted.values()} == {
        "st23-sriov-logical-port"
    }


@pytest.mark.asyncio
async def test_sriov_failed_cleanup_fails_the_assign_observations(monkeypatch) -> None:
    """A port cleanup left assigned cannot back a passed assign observation."""
    state = await _run_sriov_arm(monkeypatch, chhwres_status="FAIL")

    emitted = _sriov_observations(state)
    assign = emitted["st25-hmc-assign-sriov-logical-port"]["observation"]
    assert (assign["cleanup"], assign["result"]) == ("failed", "failed")


@pytest.mark.asyncio
async def test_sriov_idempotent_calls_back_no_observation(monkeypatch) -> None:
    """From a `none` profile the unassigns and the reassign dispatch nothing."""
    state = await _run_sriov_arm(monkeypatch, profile_prepopulated=False)

    assert set(_sriov_observations(state)) == {"st25-hmc-assign-sriov-logical-port"}


@pytest.mark.asyncio
async def test_sriov_unreadable_profile_is_not_a_cleared_profile(monkeypatch) -> None:
    state = await _run_sriov_arm(
        monkeypatch, unassign_profile_read=("hmc_run_command", "FAIL", "ssh lost")
    )

    unassign = _sriov_observations(state)["st26-hmc-unassign-sriov-logical-port"]
    assert unassign["observation"]["result"] == "failed"
    assert "profile-ports-cleared" not in unassign["observation"]["assertions"]


@pytest.mark.asyncio
async def test_sriov_failed_assign_records_no_assign_observation(monkeypatch) -> None:
    """With ST26 skipped the profile still lists the port, so cleanup's unassign dispatches."""
    state = await _run_sriov_arm(monkeypatch, assign_status="FAIL")

    assert set(_sriov_observations(state)) == {"st28-hmc-unassign-sriov-logical-port"}


@pytest.mark.asyncio
async def test_profile_inventory_records_all_selector_scoped_probes() -> None:
    tools = [
        "hmc_get_lpar_description",
        "hmc_get_lpar_msp",
        "hmc_get_proc_compat_modes",
        "hmc_get_lpar_proc_compat",
        "hmc_list_memory_pools",
        "hmc_list_vnics",
        "hmc_get_lpar_memopt_score",
        "hmc_list_lpar_memopt_scores",
        "hmc_get_system_memopt_score",
        "hmc_plan_lpar_memopt_scores",
        "hmc_plan_system_memopt_score",
        "hmc_list_resource_group_memopt_scores",
        "hmc_plan_resource_group_memopt_scores",
        "hmc_get_minimum_affinity_policy",
    ]
    state = _ScriptedSriovState([(tool, "PASS", {}) for tool in tools])

    await profiles.inventory_lpar_profiles(object(), state)

    assert [tool for tool, _ in state.calls] == tools
    assert all(entry["subtask"] == 4 for entry in state.results)
    for tool, kwargs in state.calls:
        if tool in {
            "hmc_get_proc_compat_modes",
            "hmc_list_memory_pools",
            "hmc_list_lpar_memopt_scores",
            "hmc_get_system_memopt_score",
            "hmc_plan_lpar_memopt_scores",
            "hmc_plan_system_memopt_score",
            "hmc_list_resource_group_memopt_scores",
            "hmc_plan_resource_group_memopt_scores",
        }:
            assert kwargs == {"system_name_or_uuid": state.config.system_name}
        else:
            assert kwargs["system_name_or_uuid"] == state.config.system_name
            if tool != "hmc_get_proc_compat_modes":
                assert kwargs.get("lpar_name_or_uuid") == state.config.lp3_name


@pytest.mark.asyncio
async def test_connectivity_inventory_discovers_context_and_records_probes() -> None:
    state = _ScriptedSriovState(
        [
            ("hmc_get_console_info", "PASS", {"UUID": "console-uuid"}),
            ("hmc_list_resources", "PASS", []),
            ("hmc_list_systems", "PASS", {"entries": []}),
            ("hmc_get_system", "PASS", {"UUID": "system-uuid"}),
            ("hmc_list_lpars", "PASS", {"entries": "malformed"}),
            ("hmc_get_lpar", "PASS", {"UUID": "lp3-uuid"}),
            (
                "hmc_list_vios",
                "PASS",
                {"entries": [{"UUID": "vios-uuid", "Resource": {"PartitionID": "3"}}]},
            ),
            ("hmc_capacity_report", "PASS", {}),
            ("hmc_find_placement", "PASS", {}),
            ("hmc_list_resources", "PASS", {}),
            ("hmc_system_summary", "PASS", {}),
            ("hmc_lpar_summary", "PASS", {}),
            ("hmc_list_lpar_ownership", "PASS", {}),
            ("hmc_read_lpar_boot_order", "PASS", {}),
            ("hmc_inspect_lpar", "PASS", {}),
            ("hmc_get_lpar_proc_compat", "PASS", {"profile": "default_profile"}),
            ("hmc_snapshot_capture", "PASS", {}),
            ("hmc_inventory", "PASS", {}),
            ("hmc_fleet_health", "PASS", {}),
            ("hmc_plan_lpar", "PASS", {}),
        ]
    )

    await connectivity.inventory_connectivity(object(), state)

    assert (state.artifacts.console_uuid, state.artifacts.system_uuid) == (
        "console-uuid",
        "system-uuid",
    )
    assert (
        state.artifacts.lp3_uuid,
        state.artifacts.vios_uuid,
        state.artifacts.vios_partition_id,
    ) == (
        "lp3-uuid",
        "vios-uuid",
        3,
    )
    assert state.artifacts.job_uuid_sample is None
    assert state.calls[8] == (
        "hmc_find_placement",
        {
            "desired_memory_mib": state.config.placement_memory_mib,
            "desired_proc_units": 0.5,
        },
    )
    assert all(entry["subtask"] == 1 for entry in state.results)


def _console_at(version: str, release: str, service_pack: str) -> dict[str, object]:
    return {
        "UUID": "console-uuid",
        "Resource": {
            "VersionInfo": {
                "Version": version,
                "Release": release,
                "ServicePackName": service_pack,
            }
        },
    }


_GATE_REFUSAL = (
    "PlatformUpdate requires HMC 11.1.1111 or later; the connected HMC version "
    "is below the minimum. Upgrade the HMC before retrying."
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "data", "expected"),
    [
        ("FAIL", _failure(_GATE_REFUSAL), "PASS"),
        ("PASS", {"UUID": "job-uuid"}, "FAIL"),
        (
            "FAIL",
            _failure("No managed system named 'hmcpctl-live-absent-system' found."),
            "FAIL",
        ),
    ],
)
async def test_platform_update_check_passes_only_on_the_version_refusal(
    status, data, expected
) -> None:
    state = _ScriptedSriovState([("hmc_update_firmware", status, data)])

    await connectivity._check_platform_update_refusal(
        object(), state, _console_at("10", "3", "1060")
    )

    (_, kwargs) = state.calls[0]
    assert kwargs["system_name_or_uuid"] == "hmcpctl-live-absent-system"
    [row] = state.results
    assert (row["subtask"], row["status"]) == (1, expected)
    assert not state.observations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "console",
    [_console_at("11", "1", "1111"), {"UUID": "console-uuid"}, None],
)
async def test_platform_update_check_skips_without_a_pre_minimum_version(
    console,
) -> None:
    state = _ScriptedSriovState([])

    await connectivity._check_platform_update_refusal(object(), state, console)

    assert state.calls == []
    [row] = state.results
    assert row["status"] == "SKIP"


_SYS_UUID = "00000000-0000-0000-0000-00000000000A"
_PREFS = {
    "EnergyMonitoringCapable": True,
    "LongTermMonitorEnabled": True,
    "AggregationEnabled": True,
    "ShortTermMonitorEnabled": False,
    "ComputeLTMEnabled": False,
    "EnergyMonitorEnabled": True,
}


def _metric_links(kind: str) -> list[dict[str, str]]:
    return [
        {
            "link": f"/rest/api/pcm/{kind}/ManagedSystem_{_SYS_UUID.lower()}_x_y_30.json",
            "updated": "2026-10-06T10:00:00Z",
            "title": kind,
        }
    ]


def _metric_document() -> dict[str, object]:
    return {
        "systemUtil": {
            "utilInfo": {"uuid": _SYS_UUID.lower(), "name": "sys-A"},
            "utilSamples": [{"sampleType": "ManagedSystem"}],
        }
    }


def _captured_templates() -> list[dict[str, object]]:
    """The template library feed a V10R3 HMC returned (#1202), as the client parses it."""
    return parse_feed(live_fixture("rest-templates-feed")["body"])


def _st5_transcript(
    templates: list[dict[str, object]],
) -> list[tuple[str, str, object]]:
    first = templates[0]["UUID"]
    return [
        ("hmc_get_pcm_preferences", "PASS", dict(_PREFS)),
        ("hmc_processed_metric_links", "PASS", _metric_links("ProcessedMetrics")),
        ("hmc_processed_metrics", "PASS", _metric_document()),
        ("hmc_aggregated_metric_links", "PASS", _metric_links("AggregatedMetrics")),
        ("hmc_aggregated_metrics", "PASS", _metric_document()),
        ("hmc_list_partition_templates", "PASS", templates),
        ("hmc_get_partition_template", "PASS", {"UUID": str(first).upper()}),
    ]


@pytest.mark.asyncio
async def test_st5_records_verified_pcm_and_template_reads() -> None:
    templates = _captured_templates()
    state = _ScriptedSriovState(_st5_transcript(templates))
    state.artifacts.system_uuid = _SYS_UUID

    await metrics.inspect_metrics_templates(object(), state)

    assert [row["result"] for row in state.results] == ["passed"] * 7
    assert [entry["operation"] for entry in state.observations] == [
        "pcm.get_preferences",
        "metrics.processed_links",
        "metrics.processed",
        "metrics.aggregated_links",
        "metrics.aggregated",
        "template.list",
        "template.get",
    ]
    ids = [entry["observation"]["id"] for entry in state.observations]
    assert len(set(ids)) == len(ids)
    assert state.calls[6][1] == {"template_uuid": templates[0]["UUID"]}
    # A two-hour window: processed metrics are retained for about that long.
    start = datetime.strptime(
        state.calls[1][1]["start_ts"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)
    assert 7100 < (datetime.now(UTC) - start).total_seconds() < 7300


@pytest.mark.asyncio
async def test_st5_fails_reads_whose_data_names_another_system() -> None:
    state = _ScriptedSriovState(_st5_transcript(_captured_templates()))
    state.artifacts.system_uuid = "00000000-0000-0000-0000-0000000000ff"

    await metrics.inspect_metrics_templates(object(), state)

    failed = [row["tool"] for row in state.results if row["result"] == "failed"]
    assert failed == [
        "hmc_processed_metric_links",
        "hmc_processed_metrics",
        "hmc_aggregated_metric_links",
        "hmc_aggregated_metrics",
    ]


@pytest.mark.asyncio
async def test_st5_empty_metric_feed_skips_links_and_data() -> None:
    state = _ScriptedSriovState(
        [
            ("hmc_get_pcm_preferences", "PASS", dict(_PREFS)),
            ("hmc_processed_metric_links", "PASS", []),
            ("hmc_aggregated_metric_links", "PASS", []),
            ("hmc_list_partition_templates", "PASS", []),
        ]
    )
    state.artifacts.system_uuid = _SYS_UUID

    await metrics.inspect_metrics_templates(object(), state)

    rows = {row["tool"]: row for row in state.results}
    for tool in (
        "hmc_processed_metric_links",
        "hmc_processed_metrics",
        "hmc_aggregated_metric_links",
        "hmc_aggregated_metrics",
        "hmc_get_partition_template",
    ):
        assert rows[tool]["status"] == "SKIP"
    assert "AggregationEnabled" in rows["hmc_aggregated_metric_links"]["note"]
    # An empty library is not evidence that listing works.
    assert rows["hmc_list_partition_templates"]["result"] == "failed"
    assert [entry["operation"] for entry in state.observations] == [
        "pcm.get_preferences",
        "template.list",
    ]


@pytest.mark.asyncio
async def test_st5_empty_metric_document_skips() -> None:
    transcript = _st5_transcript(_captured_templates())
    transcript[2] = ("hmc_processed_metrics", "PASS", {})
    state = _ScriptedSriovState(transcript)
    state.artifacts.system_uuid = _SYS_UUID

    await metrics.inspect_metrics_templates(object(), state)

    [row] = [r for r in state.results if r["tool"] == "hmc_processed_metrics"]
    assert row["status"] == "SKIP"
    assert "metrics.processed" not in [e["operation"] for e in state.observations]


@pytest.mark.asyncio
async def test_st5_reads_the_system_uuid_when_st1_did_not_run() -> None:
    transcript = _st5_transcript(_captured_templates())
    transcript.insert(1, ("hmc_get_system", "PASS", {"UUID": _SYS_UUID}))
    state = _ScriptedSriovState(transcript)

    await metrics.inspect_metrics_templates(object(), state)

    assert state.artifacts.system_uuid == _SYS_UUID
    assert state.results[1]["result"] == "observed"
    assert len(state.observations) == 7


@pytest.mark.asyncio
async def test_pcm_declarations_match_only_authority() -> None:
    """A 403 is a prerequisite gap; a 406 or a message naming PCM is a failure."""
    state = _ScriptedSriovState(
        [
            ("hmc_get_pcm_preferences", "FAIL", _failure("HMCError: no (HTTP 403)")),
            ("hmc_processed_metric_links", "FAIL", _failure("HMCError: x (HTTP 406)")),
            ("hmc_aggregated_metric_links", "FAIL", _failure("PCM unavailable")),
            ("hmc_list_partition_templates", "FAIL", _failure("templates (HTTP 406)")),
        ]
    )
    state.artifacts.system_uuid = _SYS_UUID

    await metrics.inspect_metrics_templates(object(), state)

    rows = {row["tool"]: row["status"] for row in state.results}
    assert rows["hmc_get_pcm_preferences"] == "SKIP"
    assert rows["hmc_processed_metric_links"] == "FAIL"
    assert rows["hmc_aggregated_metric_links"] == "FAIL"
    assert rows["hmc_list_partition_templates"] == "FAIL"
    assert [
        (gap["operation"], gap["missing_scope"]["variant"]) for gap in state.gaps
    ] == [("pcm.get_preferences", "pcm-authority")]
    runner._validate_declared_outcomes()


@pytest.mark.asyncio
async def test_st12_never_sets_pcm_preferences() -> None:
    state = _ScriptedSriovState(
        [("hmc_get_job", "PASS", {}), ("hmc_wait_for_job", "PASS", {})]
    )
    state.artifacts.job_uuid_sample = "job-uuid"

    await metrics.inspect_metrics_jobs(object(), state)

    assert [tool for tool, _ in state.calls] == ["hmc_get_job", "hmc_wait_for_job"]
    assert state.calls[1][1] == {
        "job_id": "job-uuid",
        "timeout_seconds": 10,
        "poll_interval": 2,
    }


def _flags(**overrides: bool) -> dict[str, bool]:
    return {**_PREFS, **overrides}


#: With aggregation on, the HMC holds these on (the reference, and the 2026-10-06 run).
_HELD = {"LongTermMonitorEnabled", "EnergyMonitorEnabled"}


def _round_trip(
    final: dict[str, bool], baseline: dict[str, bool] | None = None
) -> list[tuple[str, str, object]]:
    """Each flag toggled and read back as the HMC answers, then a restore.

    A flag the baseline's aggregation holds reads back unchanged; every other
    flag reads back flipped. `final` is the last read.
    """
    base = baseline or _flags()
    held = _HELD if base["AggregationEnabled"] else set()
    transcript: list[tuple[str, str, object]] = [
        ("hmc_get_pcm_preferences", "PASS", base)
    ]
    for name, _ in metrics.PCM_FLAGS:
        after = base if name in held else {**base, name: not base[name]}
        transcript += [
            ("hmc_set_pcm_preferences", "PASS", {}),
            ("hmc_get_pcm_preferences", "PASS", after),
            ("hmc_set_pcm_preferences", "PASS", {}),
            ("hmc_get_pcm_preferences", "PASS", base),
        ]
    return [*transcript, ("hmc_get_pcm_preferences", "PASS", final)]


@pytest.mark.asyncio
async def test_st38_round_trip_restores_snapshot() -> None:
    state = _ScriptedSriovState(_round_trip(_flags()))
    state.group = "pcm"

    await metrics.exercise_pcm_preferences(object(), state)

    sets = [kwargs for tool, kwargs in state.calls if tool == "hmc_set_pcm_preferences"]
    restore = {kw: _PREFS[name] for name, kw in metrics.PCM_FLAGS}
    for index, (name, keyword) in enumerate(metrics.PCM_FLAGS):
        assert sets[2 * index][keyword] is (not _PREFS[name])
        assert {k: v for k, v in sets[2 * index + 1].items() if k in restore} == restore
    # The snapshot is a results row before the first write: an interrupted run's
    # document still carries the values to restore.
    assert state.results[0]["tool"] == "hmc_get_pcm_preferences (snapshot)"
    assert state.calls[1][0] == "hmc_set_pcm_preferences"
    [observation_entry] = state.observations
    recorded = observation_entry["observation"]
    assert observation_entry["operation"] == "pcm.set_preferences"
    assert recorded["result"] == "passed"
    assert recorded["cleanup"] == "passed"
    assert recorded["assertions"] == [
        "long-term-monitor-held-by-aggregation",
        "aggregation-toggled",
        "short-term-monitor-toggled",
        "compute-ltm-toggled",
        "energy-monitor-held-by-aggregation",
        "snapshot-restored",
    ]


@pytest.mark.asyncio
async def test_st38_toggles_every_flag_when_aggregation_is_off() -> None:
    baseline = _flags(
        AggregationEnabled=False,
        LongTermMonitorEnabled=False,
        EnergyMonitorEnabled=False,
    )
    state = _ScriptedSriovState(_round_trip(baseline, baseline))
    state.group = "pcm"

    await metrics.exercise_pcm_preferences(object(), state)

    recorded = state.observations[0]["observation"]
    assert recorded["result"] == "passed"
    assert recorded["assertions"][:5] == [
        "long-term-monitor-toggled",
        "aggregation-toggled",
        "short-term-monitor-toggled",
        "compute-ltm-toggled",
        "energy-monitor-toggled",
    ]


class _CoupledPcmState(runner.RunState):
    """Apply writes to PCM state, holding monitoring while aggregation was on."""

    def __init__(self, failure: tuple[str, int] | None = None):
        super().__init__()
        self.group = "pcm"
        self.preferences = _flags(
            AggregationEnabled=False,
            LongTermMonitorEnabled=False,
            EnergyMonitorEnabled=False,
        )
        self.baseline = dict(self.preferences)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.counts = {"hmc_get_pcm_preferences": 0, "hmc_set_pcm_preferences": 0}
        self.failure = failure

    async def call(self, _client, tool, *, expected=(), reuse_gaps=True, **kwargs):
        self.calls.append((tool, kwargs))
        self.counts[tool] += 1
        if self.failure == (tool, self.counts[tool]):
            return "FAIL", _failure("HMCError: simulated refusal")
        if tool == "hmc_get_pcm_preferences":
            return "PASS", dict(self.preferences)
        aggregation_was_on = self.preferences["AggregationEnabled"]
        for name, keyword in metrics.PCM_FLAGS:
            if kwargs.get(keyword) is not None:
                self.preferences[name] = kwargs[keyword]
        if aggregation_was_on or self.preferences["AggregationEnabled"]:
            self.preferences["LongTermMonitorEnabled"] = True
            self.preferences["EnergyMonitorEnabled"] = True
        return "PASS", {}


@pytest.mark.asyncio
async def test_st38_restores_held_flags_after_disabling_aggregation() -> None:
    state = _CoupledPcmState()

    await metrics.exercise_pcm_preferences(object(), state)

    assert state.preferences == state.baseline
    recorded = state.observations[0]["observation"]
    assert (recorded["result"], recorded["cleanup"]) == ("passed", "passed")
    sets = [kwargs for tool, kwargs in state.calls if tool == "hmc_set_pcm_preferences"]
    assert len(sets) == 12  # five toggles/restores, plus one ordered fallback
    assert sets[4]["aggregation"] is False
    assert all(
        sets[4][kw] is None for _, kw in metrics.PCM_FLAGS if kw != "aggregation"
    )
    assert {kw: sets[5][kw] for _, kw in metrics.PCM_FLAGS} == {
        kw: state.baseline[name] for name, kw in metrics.PCM_FLAGS
    }
    held_read = next(
        row for row in state.results if row["tool"].endswith("(aggregation restored)")
    )
    assert held_read["data"]["LongTermMonitorEnabled"] is True
    assert held_read["data"]["EnergyMonitorEnabled"] is True
    assert state.results[0]["data"] == state.baseline
    assert len(state.observations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "ordinal", "write_count"),
    [
        ("hmc_set_pcm_preferences", 4, 4),
        ("hmc_get_pcm_preferences", 5, 4),
        ("hmc_set_pcm_preferences", 5, 5),
        ("hmc_get_pcm_preferences", 6, 5),
        ("hmc_set_pcm_preferences", 6, 6),
        ("hmc_get_pcm_preferences", 7, 6),
    ],
)
async def test_st38_restore_failures_stop_the_ordered_fallback(
    tool, ordinal, write_count
) -> None:
    state = _CoupledPcmState((tool, ordinal))

    await metrics.exercise_pcm_preferences(object(), state)

    assert state.counts["hmc_set_pcm_preferences"] == write_count
    recorded = state.observations[0]["observation"]
    assert (recorded["result"], recorded["cleanup"]) == ("failed", "failed")
    assert state.results[-1]["tool"].endswith("(MANUAL RECOVERY REQUIRED)")
    assert any(row["status"] == "FAIL" for row in state.results[:-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["AggregationEnabled", "ShortTermMonitorEnabled"])
async def test_st38_does_not_retry_a_restore_with_unrelated_mismatch(field) -> None:
    baseline = _flags(AggregationEnabled=False)
    changed = {**baseline, field: not baseline[field]}
    state = _ScriptedSriovState(
        [
            ("hmc_get_pcm_preferences", "PASS", baseline),
            ("hmc_set_pcm_preferences", "PASS", {}),
            ("hmc_get_pcm_preferences", "PASS", baseline),
            ("hmc_set_pcm_preferences", "PASS", {}),
            ("hmc_get_pcm_preferences", "PASS", changed),
            ("hmc_get_pcm_preferences", "PASS", changed),
        ]
    )
    state.group = "pcm"

    await metrics.exercise_pcm_preferences(object(), state)

    assert sum(tool == "hmc_set_pcm_preferences" for tool, _ in state.calls) == 2
    assert state.observations[0]["observation"]["cleanup"] == "failed"


@pytest.mark.asyncio
async def test_st38_ordered_restore_is_bounded_when_monitoring_stays_held() -> None:
    baseline = _flags(AggregationEnabled=False, LongTermMonitorEnabled=False)
    held = {**baseline, "LongTermMonitorEnabled": True}
    state = _ScriptedSriovState(
        [
            ("hmc_get_pcm_preferences", "PASS", baseline),
            ("hmc_set_pcm_preferences", "PASS", {}),
            ("hmc_get_pcm_preferences", "PASS", held),
            ("hmc_set_pcm_preferences", "PASS", {}),
            ("hmc_get_pcm_preferences", "PASS", held),
            ("hmc_set_pcm_preferences", "PASS", {}),
            ("hmc_get_pcm_preferences", "PASS", held),
            ("hmc_set_pcm_preferences", "PASS", {}),
            ("hmc_get_pcm_preferences", "PASS", held),
            ("hmc_get_pcm_preferences", "PASS", held),
        ]
    )
    state.group = "pcm"

    await metrics.exercise_pcm_preferences(object(), state)

    assert sum(tool == "hmc_set_pcm_preferences" for tool, _ in state.calls) == 4
    assert state.observations[0]["observation"]["cleanup"] == "failed"
    assert state.results[-1]["tool"].endswith("(MANUAL RECOVERY REQUIRED)")


@pytest.mark.asyncio
async def test_st38_a_refused_held_toggle_fails_its_assertion() -> None:
    """Held means accepted and unchanged; a refused request is not evidence."""
    transcript = _round_trip(_flags())
    transcript[1] = (
        "hmc_set_pcm_preferences",
        "FAIL",
        _failure("HMCError: (HTTP 500)"),
    )
    state = _ScriptedSriovState(transcript)
    state.group = "pcm"

    await metrics.exercise_pcm_preferences(object(), state)

    recorded = state.observations[0]["observation"]
    assert recorded["result"] == "failed"
    assert "long-term-monitor-held-by-aggregation" not in recorded["assertions"]


@pytest.mark.asyncio
async def test_st38_a_held_flag_that_flips_fails_its_assertion() -> None:
    transcript = _round_trip(_flags())
    # The coupling says LTM stays on while aggregation is enabled; it did not.
    transcript[2] = (
        "hmc_get_pcm_preferences",
        "PASS",
        _flags(LongTermMonitorEnabled=False),
    )
    state = _ScriptedSriovState(transcript)
    state.group = "pcm"

    await metrics.exercise_pcm_preferences(object(), state)

    recorded = state.observations[0]["observation"]
    assert recorded["result"] == "failed"
    assert recorded["cleanup"] == "passed"
    assert "long-term-monitor-held-by-aggregation" not in recorded["assertions"]


@pytest.mark.asyncio
async def test_st38_mismatched_final_read_fails_cleanup() -> None:
    state = _ScriptedSriovState(_round_trip(_flags(EnergyMonitorEnabled=False)))
    state.group = "pcm"

    await metrics.exercise_pcm_preferences(object(), state)

    recorded = state.observations[0]["observation"]
    assert (recorded["result"], recorded["cleanup"]) == ("failed", "failed")
    manual = state.results[-1]
    assert manual["tool"] == "hmc_set_pcm_preferences (MANUAL RECOVERY REQUIRED)"
    assert manual["status"] == "FAIL"
    assert "EnergyMonitorEnabled=True" in manual["note"]
    assert "EnergyMonitoringCapable" not in manual["note"]


@pytest.mark.asyncio
async def test_st38_stops_toggling_after_a_restore_that_did_not_hold() -> None:
    ltm_off = _flags(LongTermMonitorEnabled=False)
    state = _ScriptedSriovState(
        [
            ("hmc_get_pcm_preferences", "PASS", _flags()),
            ("hmc_set_pcm_preferences", "PASS", {}),
            ("hmc_get_pcm_preferences", "PASS", ltm_off),
            ("hmc_set_pcm_preferences", "FAIL", _failure("HMCError: x (HTTP 400)")),
            ("hmc_get_pcm_preferences", "PASS", ltm_off),
            ("hmc_get_pcm_preferences", "PASS", ltm_off),
        ]
    )
    state.group = "pcm"

    await metrics.exercise_pcm_preferences(object(), state)

    sets = [tool for tool, _ in state.calls if tool == "hmc_set_pcm_preferences"]
    assert len(sets) == 2
    recorded = state.observations[0]["observation"]
    assert (recorded["result"], recorded["cleanup"]) == ("failed", "failed")
    assert state.results[-1]["tool"].endswith("(MANUAL RECOVERY REQUIRED)")


@pytest.mark.asyncio
async def test_st38_skips_without_a_boolean_snapshot() -> None:
    state = _ScriptedSriovState(
        [("hmc_get_pcm_preferences", "FAIL", _failure("HMCError: x (HTTP 403)"))]
    )
    state.group = "pcm"

    await metrics.exercise_pcm_preferences(object(), state)

    assert [tool for tool, _ in state.calls] == ["hmc_get_pcm_preferences"]
    assert state.results[-1]["status"] == "SKIP"
    assert not state.observations


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [None, "all", "round2"])
async def test_st38_runs_only_in_the_pcm_group(group) -> None:
    state = _ScriptedSriovState([])
    state.group = group

    await metrics.exercise_pcm_preferences(object(), state)

    assert state.calls == []
    assert state.results[-1]["status"] == "SKIP"


def test_pcm_group_is_opt_in() -> None:
    assert runner.SUBTASK_GROUPS["pcm"] == [38]
    assert 38 not in runner.SUBTASK_GROUPS["all"]


def test_lpar_config_group_is_opt_in() -> None:
    assert runner.SUBTASK_GROUPS["lpar-config"] == [39]
    assert 39 not in runner.SUBTASK_GROUPS["all"]
    assert runner.SUBTASKS[39] is lpar_config.exercise_lpar_config


def test_lpar_power_group_is_opt_in() -> None:
    assert runner.SUBTASK_GROUPS["lpar-power"] == [41]
    assert 41 not in runner.SUBTASK_GROUPS["all"]
    assert runner.SUBTASKS[41] is lpar_power.exercise_lpar_power


@pytest.mark.asyncio
async def test_escape_hatch_uses_only_bounded_commands() -> None:
    state = _ScriptedSriovState(
        [
            ("hmc_run_command", "PASS", "version"),
            ("hmc_run_command", "PASS", "systems"),
        ]
    )

    await escape_hatch.exercise_cli_escape_hatch(object(), state)

    assert state.calls == [
        ("hmc_run_command", {"cmd": "lshmc -V"}),
        ("hmc_run_command", {"cmd": "lssyscfg -r sys"}),
    ]
    assert [(entry["subtask"], entry["status"]) for entry in state.results] == [
        (7, "PASS"),
        (7, "PASS"),
    ]


@pytest.mark.asyncio
async def test_provision_dry_run_requires_vios_and_uses_configured_vlan() -> None:
    missing = _ScriptedSriovState([])
    await provisioning.validate_provisioning_dry_run(object(), missing)
    assert missing.calls == []
    assert missing.results[0]["status"] == "SKIP"

    state = _ScriptedSriovState(
        [("hmc_provision_lpar", "PASS", {"steps": [{"status": "dry_run"}]})]
    )
    state.artifacts.vios_uuid = "vios-uuid"
    state.artifacts.test_vlan_id = 3100
    await provisioning.validate_provisioning_dry_run(object(), state)
    assert state.calls == [
        (
            "hmc_provision_lpar",
            {
                "dry_run": True,
                "system_name_or_uuid": state.config.system_name,
                "name": state.config.dry_run_lpar_name,
                "adapters": {"port_vlan_id": state.config.provision_vlan_id},
                "storage": {
                    "vios_uuid": "vios-uuid",
                    "storage_name": state.config.dry_run_storage_name,
                },
                "resources": {"desired_memory": state.config.dry_run_memory_mib},
            },
        )
    ]


@pytest.mark.asyncio
async def test_lpar_lifecycle_captures_jobs_and_clears_scratch_identity() -> None:
    state = _ScriptedSriovState(
        [
            ("hmc_run_command", "PASS", "name\n"),
            ("hmc_get_system", "PASS", {"UUID": "system-uuid"}),
            ("hmc_create_lpar", "PASS", {"lpar": {"UUID": "scratch-uuid"}}),
            ("hmc_get_lpar", "PASS", {"UUID": "scratch-uuid"}),
            ("hmc_modify_lpar", "PASS", {}),
            ("hmc_lpar_summary", "PASS", {}),
            ("hmc_power_on_lpar", "PASS", {"job_uuid": "boot-job"}),
            ("hmc_power_off_lpar", "PASS", {}),
            ("hmc_delete_lpar", "PASS", {}),
            ("hmc_list_lpars", "PASS", {"entries": []}),
        ]
    )

    await lpar.exercise_lpar_lifecycle(object(), state)

    assert state.artifacts.system_uuid == "system-uuid"
    assert state.artifacts.scratch_uuid is None
    assert state.artifacts.job_uuid_sample == "boot-job"
    assert state.calls[2][1]["resources"] == {
        "desired_memory": state.config.scratch_create_desired_memory_mib,
        "max_memory": state.config.scratch_create_max_memory_mib,
        "desired_vcpus": state.config.scratch_create_desired_vcpus,
        "max_vcpus": state.config.scratch_create_max_vcpus,
        "desired_procs": state.config.scratch_create_desired_procs,
        "max_procs": state.config.scratch_create_max_procs,
    }
    assert [entry["subtask"] for entry in state.results] == [8] * 8


@pytest.mark.asyncio
async def test_scratch_create_with_failed_apply_step_is_not_recorded_pass() -> None:
    """A create whose apply_profile step errored must not read as a clean PASS (#997)."""
    state = _ScriptedSriovState(
        [
            (
                "hmc_create_lpar",
                "PASS",
                {
                    "lpar": {"UUID": "scratch-uuid"},
                    "workflow_completed": False,
                    "steps": [
                        {"step": "create", "status": "ok"},
                        {
                            "step": "apply_profile",
                            "status": "error",
                            "result": "HMCCLIError: mksyscfg refused",
                        },
                    ],
                },
            ),
            ("hmc_get_lpar", "PASS", {"UUID": "scratch-uuid"}),
        ]
    )

    await lpar._create_and_confirm_scratch_lpar(object(), state)

    create_row = state.results[0]
    assert create_row["status"] == "FAIL"
    assert "apply_profile failed" in create_row["note"]
    assert "mksyscfg refused" in create_row["note"]
    # The partition really was created — identity tracking is unaffected by
    # the recorded status downgrade.
    assert state.artifacts.scratch_uuid == "scratch-uuid"


@pytest.mark.asyncio
async def test_provision_dry_run_prints_the_steps_of_a_served_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typed dry-run result is a generated dataclass, not a dict (#1410)."""
    planned = await _served_result(
        "hmc_provision_lpar",
        {
            "resource_created": False,
            "workflow_completed": False,
            "lpar_uuid": None,
            "dry_run": True,
            "ownership_stamped": None,
            "steps": [{"step": "create", "status": "dry_run"}],
            "warnings": [],
            "change_location": None,
        },
    )
    assert dataclasses.is_dataclass(planned)
    state = _ScriptedSriovState([("hmc_provision_lpar", "PASS", planned)])
    state.artifacts.vios_uuid = "vios-A-uuid"

    await provisioning.validate_provisioning_dry_run(object(), state)

    assert "all status=dry_run: True" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_scratch_create_reads_the_uuid_from_a_served_dataclass_result() -> None:
    """A typed create result is a generated dataclass, not a dict (#1410)."""
    created = await _served_result(
        "hmc_create_lpar",
        {
            "resource_created": True,
            "workflow_completed": True,
            "lpar": {"UUID": "scratch-uuid"},
            "ownership_stamped": True,
            "steps": [{"step": "create", "status": "ok"}],
            "warnings": [],
        },
    )
    assert dataclasses.is_dataclass(created)
    state = _ScriptedSriovState(
        [("hmc_create_lpar", "PASS", created), ("hmc_get_lpar", "PASS", {})]
    )

    await lpar._create_and_confirm_scratch_lpar(object(), state)

    assert state.artifacts.scratch_uuid == "scratch-uuid"


@pytest.mark.asyncio
async def test_live_provision_is_judged_by_the_steps_of_a_served_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typed provision result's failed step is a FAIL row and a printed line (#1410)."""
    provisioned = await _served_result(
        "hmc_provision_lpar",
        {
            "resource_created": True,
            "workflow_completed": False,
            "lpar_uuid": "lpar-A-uuid",
            "dry_run": False,
            "ownership_stamped": True,
            "steps": [
                {"step": "create", "status": "ok"},
                {"step": "storage", "status": "error", "result": "mapping refused"},
            ],
            "warnings": [],
            "change_location": None,
        },
    )
    assert dataclasses.is_dataclass(provisioned)
    state = _ScriptedSriovState([("hmc_provision_lpar", "PASS", provisioned)])

    await provisioning._provision_from_baseline(
        object(),
        state,
        {
            "name": "fixture",
            "disk": "hp14deadbeef",
            "token": "round2-deadbeef",
            "vios_uuid": "vios-A-uuid",
            "vg_uuid": "vg-A-uuid",
        },
    )

    (row,) = state.results
    assert row["status"] == "FAIL"
    assert "storage failed: mapping refused" in row["note"]
    printed = capsys.readouterr().out
    assert "provision step [create]: ok" in printed
    assert "provision step [storage]: error" in printed


def _answer(answers: dict[str, object]):
    """A scripted RunState.call: a tool's answer, or a callable of its kwargs."""
    calls: list[tuple[str, dict[str, object]]] = []

    async def scripted_call(_state, _client, tool, **kwargs):
        calls.append((tool, kwargs))
        answer = answers.get(tool, ("PASS", {}))
        return answer(kwargs) if callable(answer) else answer

    return calls, scripted_call


def _verified(state) -> dict[str, dict[str, Any]]:
    return {entry["operation"]: entry["observation"] for entry in state.observations}


def _held(observation: dict[str, Any]) -> set[str]:
    return set(observation["assertions"])


def test_profiles_group_selects_only_property_subtasks() -> None:
    """The profiles arm never selects lifecycle, networking, users or storage."""
    assert runner.SUBTASK_GROUPS["profiles"] == [0, 4, 10, 15]


def test_vios_backup_group_is_subtask_37_and_outside_all() -> None:
    """The ST37 restore needs its own authorization, so `all` never reaches it."""
    assert runner.SUBTASK_GROUPS["vios-backup"] == [37]
    assert 37 not in runner.SUBTASK_GROUPS["all"]
    assert runner.SUBTASKS[37].__module__ == "live_test.vios_backup"


@pytest.mark.asyncio
async def test_main_records_the_dispatched_group(monkeypatch, tmp_path) -> None:
    _isolated_environ(monkeypatch)
    _isolate_runner(monkeypatch)
    seen: list[str | None] = []

    async def capture(_client, state):
        seen.append(state.group)

    monkeypatch.setitem(runner.SUBTASKS, 998, capture)
    monkeypatch.setitem(runner.SUBTASK_GROUPS, "profiles", [998])

    await runner.main(
        results_path=str(tmp_path / "results.json"),
        group="profiles",
        config=runner.LiveTestConfig(),
    )

    assert seen == ["profiles"]


_ST4_ANSWERS: dict[str, object] = {
    "hmc_get_lpar_description": ("PASS", "[hmcpctl owner:a created:2026-10-01]\n"),
    "hmc_get_lpar_msp": ("PASS", False),
    "hmc_get_proc_compat_modes": ("PASS", ["default", "POWER9", "POWER9_base"]),
    "hmc_get_lpar_proc_compat": (
        "PASS",
        {"curr": "POWER9_base", "profile_mode": "default"},
    ),
    "hmc_list_memory_pools": ("PASS", []),
    "hmc_get_lpar_memopt_score": (
        "PASS",
        {"lpar_name": "lpar-name", "curr_lpar_score": "100"},
    ),
    "hmc_list_lpar_memopt_scores": (
        "PASS",
        [{"lpar_name": "lpar-name", "curr_lpar_score": "100"}],
    ),
    "hmc_get_system_memopt_score": ("PASS", {"curr_sys_score": "86"}),
    "hmc_plan_lpar_memopt_scores": (
        "PASS",
        [{"predicted_lpar_score": "100", "prediction_guaranteed": False}],
    ),
    "hmc_plan_system_memopt_score": (
        "PASS",
        {"predicted_sys_score": "86", "prediction_guaranteed": False},
    ),
    "hmc_list_resource_group_memopt_scores": (
        "PASS",
        {"capability": "capability-unavailable", "unavailable_reason": "needs V11R1"},
    ),
    "hmc_plan_resource_group_memopt_scores": (
        "PASS",
        {"capability": "available", "items": [{"predicted_score": "100"}]},
    ),
    "hmc_get_minimum_affinity_policy": (
        "PASS",
        SimpleNamespace(
            capability="available",
            min_affinity_score=0,
            min_affinity_score_action="none",
            unavailable_reason=None,
        ),
    ),
}


@pytest.mark.asyncio
async def test_profile_inventory_promotes_reads_with_assertions(monkeypatch) -> None:
    _calls, scripted = _answer(_ST4_ANSWERS)
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = runner.RunState()
    state.config = dataclasses.replace(state.config, lp3_name="lpar-name")

    await profiles.inventory_lpar_profiles(None, state)

    observations = _verified(state)
    assert len(observations) == 13
    assert {o["result"] for o in observations.values()} == {"passed"}
    assert _held(observations["memory_pool.list"]) == {"memory-pools-empty-branch"}
    assert _held(observations["resource_group.list_memopt_scores"]) == {
        "capability-unavailable-reason"
    }
    assert _held(observations["resource_group.plan_memopt_scores"]) == {
        "capability-available-rows"
    }
    assert _held(observations["lpar.get_minimum_affinity_policy"]) == {
        "capability-available-policy"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "answer", "operation"),
    [
        (
            "hmc_get_lpar_memopt_score",
            ("PASS", {"lpar_name": "lpar-name", "curr_lpar_score": "101"}),
            "lpar.get_memopt_score",
        ),
        ("hmc_get_lpar_msp", ("FAIL", "ssh lost"), "lpar.get_msp"),
        (
            "hmc_list_memory_pools",
            ("PASS", [{}]),
            "memory_pool.list",
        ),
        (
            "hmc_list_resource_group_memopt_scores",
            ("PASS", {"capability": "capability-unavailable"}),
            "resource_group.list_memopt_scores",
        ),
    ],
)
async def test_a_wrong_read_shape_fails_its_observation(
    monkeypatch, tool, answer, operation
) -> None:
    _calls, scripted = _answer({**_ST4_ANSWERS, tool: answer})
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = runner.RunState()
    state.config = dataclasses.replace(state.config, lp3_name="lpar-name")

    await profiles.inventory_lpar_profiles(None, state)

    assert _verified(state)[operation]["result"] == "failed"


def _st10_answers(
    sync_reads: tuple[str, ...] = ("1,Not Activated", "0,Not Activated"),
    **overrides: object,
) -> dict[str, object]:
    """A not-activated test partition, one VIOS, and a default-mode profile."""
    msp = iter([True, False, True])
    profile_mode = iter(["default", "POWER9_base", "default"])
    sync = iter(sync_reads)

    def run_command(kwargs):
        cmd = kwargs["cmd"]
        if "-F name,lpar_env" in cmd:
            return "PASS", "lpar-name,aixlinux\nvios-b,vioserver\nvios-a,vioserver\n"
        if "sync_curr_profile" in cmd:
            return "PASS", next(sync) + "\n"
        return "PASS", "name=default_profile,lpar_name=lpar-name\n"

    answers: dict[str, object] = {
        "hmc_get_lpar_description": ("PASS", "baseline\n"),
        "hmc_run_command": run_command,
        "hmc_set_lpar_msp": lambda kwargs: (
            ("FAIL", "lpar_env='aixlinux', not vioserver")
            if kwargs["lpar_name_or_uuid"] == "lpar-name"
            else ("PASS", "")
        ),
        "hmc_get_lpar_msp": lambda _kwargs: ("PASS", next(msp)),
        "hmc_get_proc_compat_modes": ("PASS", ["default", "POWER9", "POWER9_base"]),
        "hmc_get_lpar_proc_compat": lambda _kwargs: (
            "PASS",
            {"profile": "default_profile", "profile_mode": next(profile_mode)},
        ),
        "hmc_list_memory_pools": ("PASS", []),
        "hmc_remove_memory_pool": (
            "FAIL",
            "Cannot remove the shared memory pool — the system has no shared memory pool",
        ),
    }
    answers.update(overrides)
    return answers


def _st10_state(group: str | None = "profiles"):
    state = runner.RunState(group=group)
    state.config = dataclasses.replace(state.config, lp3_name="lpar-name")
    state.artifacts.lp3_baseline.update(
        description="baseline", sync_curr_profile="0", state="Not Activated"
    )
    return state


def _tool_calls(calls, tool: str) -> list[dict[str, object]]:
    return [kwargs for name, kwargs in calls if name == tool]


@pytest.mark.asyncio
async def test_st10_round_trips_pass_and_restore_each_value(monkeypatch) -> None:
    calls, scripted = _answer(_st10_answers())
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    observations = _verified(state)
    for operation in (
        "lpar.set_msp",
        "lpar.set_proc_compat",
        "lpar_profile.sync",
        "lpar_profile.backup",
        "lpar_profile.restore",
    ):
        assert observations[operation]["result"] == "passed", operation
    msp = _tool_calls(calls, "hmc_set_lpar_msp")
    # The non-VIOS refusal, then the first VIOS by name toggled and restored.
    assert [(m["lpar_name_or_uuid"], m["enabled"]) for m in msp] == [
        ("lpar-name", True),
        ("vios-a", False),
        ("vios-a", True),
    ]
    assert all("ownership_override" not in m for m in msp)
    assert [m["mode"] for m in _tool_calls(calls, "hmc_set_lpar_proc_compat")] == [
        "POWER9_base",
        "default",
    ]
    assert [m["mode"] for m in _tool_calls(calls, "hmc_sync_lpar_profile")] == [
        "enable",
        "disable",
    ]
    restore = _tool_calls(calls, "hmc_restore_lpar_profiles")
    assert restore == [
        {
            "system_name_or_uuid": state.config.system_name,
            "file_path": "hmcpctl-live-st10",
            "restore_type": 3,
            "system_wide_restore_approved": True,
            "ownership_override": True,
        }
    ]
    assert _tool_calls(calls, "hmc_backup_lpar_profiles")[0]["force"] is True
    refusal = next(
        row for row in state.results if row["tool"].startswith("hmc_remove_memory_pool")
    )
    assert refusal["status"] == "PASS"
    assert "memory_pool.remove" not in observations


@pytest.mark.asyncio
async def test_st10_never_removes_a_pool_the_system_has(monkeypatch) -> None:
    """The remove names no pool, so a listed pool skips the refusal probe."""
    calls, scripted = _answer(
        _st10_answers(hmc_list_memory_pools=("PASS", [{"pool_mem": "4096"}]))
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    assert _tool_calls(calls, "hmc_remove_memory_pool") == []
    probe = next(
        row for row in state.results if row["tool"].startswith("hmc_remove_memory_pool")
    )
    assert probe["status"] == "SKIP"


@pytest.mark.asyncio
async def test_description_round_trip_fails_when_the_restore_reads_back_wrong(
    monkeypatch,
) -> None:
    reads = iter(
        [
            "[hmcpctl owner:a created:2026-10-01] MCP live-test probe R2 safe to clear\n",
            "something else\n",
        ]
    )
    _calls, scripted = _answer(
        _st10_answers(
            hmc_get_lpar_description=lambda _kwargs: (
                "PASS",
                next(reads),
            )
        )
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    observation = _verified(state)["lpar.set_description"]
    assert observation["result"] == "failed"
    assert observation["cleanup"] == "failed"
    assert observation["assertions"] == ["probe-description-read-back"]


@pytest.mark.asyncio
async def test_a_refused_msp_toggle_that_changed_nothing_is_a_clean_failure(
    monkeypatch,
) -> None:
    """#1318: both writes refused; the read-back still shows the original value."""
    _calls, scripted = _answer(
        _st10_answers(
            hmc_set_lpar_msp=("FAIL", "No LPAR named 'vios-a' found."),
            hmc_get_lpar_msp=("PASS", True),
        )
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    observation = _verified(state)["lpar.set_msp"]
    assert observation["result"] == "failed"
    assert observation["assertions"] == ["vios-msp-restored"]
    assert observation["cleanup"] == "passed"


@pytest.mark.asyncio
async def test_msp_pre_read_failure_skips_the_toggle(monkeypatch) -> None:
    calls, scripted = _answer(_st10_answers(hmc_get_lpar_msp=("FAIL", "ssh lost")))
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    vios_sets = [
        m
        for m in _tool_calls(calls, "hmc_set_lpar_msp")
        if m["lpar_name_or_uuid"] != "lpar-name"
    ]
    assert vios_sets == []
    assert "lpar.set_msp" not in _verified(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "baseline",
    [
        {"sync_curr_profile": None},
        {"sync_curr_profile": "7"},
        {"state": "Running"},
    ],
)
async def test_sync_round_trip_skips_without_a_safe_baseline(
    monkeypatch, baseline
) -> None:
    calls, scripted = _answer(_st10_answers())
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()
    state.artifacts.lp3_baseline.update(baseline)

    await lpar.mutate_lpar_properties(None, state)

    assert _tool_calls(calls, "hmc_sync_lpar_profile") == []
    assert "lpar_profile.sync" not in _verified(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original", "reads", "modes", "probe_assertion"),
    [
        (
            "0",
            ("1,Not Activated", "0,Not Activated"),
            ["enable", "disable"],
            "sync-enable-read-1",
        ),
        (
            "1",
            ("0,Not Activated", "1,Not Activated"),
            ["disable", "enable"],
            "sync-disable-read-0",
        ),
        (
            "2",
            ("1,Not Activated", "2,Not Activated"),
            ["enable", "suspend"],
            "sync-enable-read-1",
        ),
    ],
)
async def test_sync_round_trip_probes_a_value_other_than_the_baseline(
    monkeypatch, original, reads, modes, probe_assertion
) -> None:
    """#1323: an `enable` probe over a baseline of 1 changes nothing and proves nothing."""
    calls, scripted = _answer(_st10_answers(sync_reads=reads))
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()
    state.artifacts.lp3_baseline["sync_curr_profile"] = original

    await lpar.mutate_lpar_properties(None, state)

    assert [m["mode"] for m in _tool_calls(calls, "hmc_sync_lpar_profile")] == modes
    observation = _verified(state)["lpar_profile.sync"]
    assert observation["result"] == "passed"
    assert observation["assertions"] == [probe_assertion, "sync-restored-baseline"]


@pytest.mark.asyncio
async def test_sync_probe_that_reads_back_the_baseline_fails(monkeypatch) -> None:
    """An accepted probe with no state change behind it is never a passed observation."""
    _calls, scripted = _answer(
        _st10_answers(sync_reads=("1,Not Activated", "1,Not Activated"))
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()
    state.artifacts.lp3_baseline["sync_curr_profile"] = "1"

    await lpar.mutate_lpar_properties(None, state)

    observation = _verified(state)["lpar_profile.sync"]
    assert observation["result"] == "failed"
    assert observation["assertions"] == ["sync-restored-baseline"]


@pytest.mark.asyncio
async def test_a_refused_disable_probe_skips_without_an_observation(
    monkeypatch,
) -> None:
    calls, scripted = _answer(
        _st10_answers(
            sync_reads=("1,Not Activated",),
            hmc_sync_lpar_profile=(
                "FAIL",
                observation.CallFailure(
                    "ToolError", "HSCL refused on hmc.example.test", "", None, False
                ),
            ),
        )
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()
    state.artifacts.lp3_baseline["sync_curr_profile"] = "1"

    await lpar.mutate_lpar_properties(None, state)

    assert [m["mode"] for m in _tool_calls(calls, "hmc_sync_lpar_profile")] == [
        "disable"
    ]
    assert "lpar_profile.sync" not in _verified(state)
    skip = next(
        row for row in state.results if row["tool"].startswith("hmc_sync_lpar_profile")
    )
    assert skip["status"] == "SKIP"
    assert "disable" in skip["note"]
    assert "sync_curr_profile=<0|1|2>" in skip["note"]
    assert skip["data"] == "HSCL refused on <REDACTED-HOST>"


@pytest.mark.asyncio
async def test_a_refused_disable_probe_that_changed_state_still_restores(
    monkeypatch,
) -> None:
    """A refusal is a SKIP only while the read-back still shows the baseline."""
    calls, scripted = _answer(
        _st10_answers(
            sync_reads=("0,Not Activated", "1,Not Activated"),
            hmc_sync_lpar_profile=lambda kwargs: (
                ("FAIL", _REFUSED) if kwargs["mode"] == "disable" else ("PASS", "")
            ),
        )
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()
    state.artifacts.lp3_baseline["sync_curr_profile"] = "1"

    await lpar.mutate_lpar_properties(None, state)

    assert [m["mode"] for m in _tool_calls(calls, "hmc_sync_lpar_profile")] == [
        "disable",
        "enable",
    ]
    observation = _verified(state)["lpar_profile.sync"]
    assert observation["result"] == "failed"
    assert observation["cleanup"] == "passed"


@pytest.mark.asyncio
async def test_a_harness_defect_in_the_disable_probe_is_not_a_skip(monkeypatch) -> None:
    """An `InvalidDispatch` never reached the HMC, so it fails rather than skips."""
    defect = observation.CallFailure("InvalidDispatch", "bad mode", "", None, False)
    _calls, scripted = _answer(
        _st10_answers(
            sync_reads=("1,Not Activated", "1,Not Activated"),
            hmc_sync_lpar_profile=lambda kwargs: (
                ("FAIL", defect) if kwargs["mode"] == "disable" else ("PASS", "")
            ),
        )
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()
    state.artifacts.lp3_baseline["sync_curr_profile"] = "1"

    await lpar.mutate_lpar_properties(None, state)

    assert _verified(state)["lpar_profile.sync"]["result"] == "failed"


@pytest.mark.asyncio
async def test_backup_failure_skips_the_restore(monkeypatch) -> None:
    calls, scripted = _answer(
        _st10_answers(hmc_backup_lpar_profiles=("FAIL", "HSCL disk full"))
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    assert _tool_calls(calls, "hmc_restore_lpar_profiles") == []
    assert _verified(state)["lpar_profile.backup"]["result"] == "failed"
    assert "lpar_profile.restore" not in _verified(state)


@pytest.mark.asyncio
async def test_changed_profiles_fail_the_restore_observation(monkeypatch) -> None:
    dumps = iter(["profile-a\n", "profile-a\nprofile-b\n"])
    base = _st10_answers()
    run_command = base["hmc_run_command"]

    def answer(kwargs):
        if kwargs["cmd"].startswith("lssyscfg -r prof "):
            return "PASS", next(dumps)
        return run_command(kwargs)  # type: ignore[operator]  # the scripted answer is callable

    _calls, scripted = _answer({**base, "hmc_run_command": answer})
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    observation = _verified(state)["lpar_profile.restore"]
    assert observation["result"] == "failed"
    assert (
        "profiles-unchanged-after-merge-current-wins" not in observation["assertions"]
    )


_CONFIGURED = (
    "name=lpar-name,state=Not Activated,resource_config=1,curr_profile=default_profile"
)


@pytest.mark.asyncio
async def test_restore_side_effect_fails_the_observation_and_is_reapplied(
    monkeypatch,
) -> None:
    """rstprofdata -l 3 unconfigures a not-activated partition (#627, observed live)."""
    partitions = iter(
        [_CONFIGURED, _CONFIGURED.replace("config=1", "config=0"), _CONFIGURED]
    )
    base = _st10_answers()
    run_command = base["hmc_run_command"]

    def answer(kwargs):
        if (
            kwargs["cmd"].startswith("lssyscfg -r lpar -m ")
            and "-F" not in kwargs["cmd"]
        ):
            return "PASS", next(partitions) + "\n"
        return run_command(kwargs)  # type: ignore[operator]  # the scripted answer is callable

    calls, scripted = _answer({**base, "hmc_run_command": answer})
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    observation = _verified(state)["lpar_profile.restore"]
    assert observation["result"] == "failed"
    assert (
        "partitions-unchanged-after-merge-current-wins" not in observation["assertions"]
    )
    assert observation["cleanup"] == "passed"
    applies = [
        kwargs["cmd"]
        for tool, kwargs in calls
        if tool == "hmc_run_command" and " -o apply " in kwargs["cmd"]
    ]
    assert applies == [
        f"chsyscfg -r lpar -m {state.config.system_name} -o apply -p lpar-name -n default_profile"
    ]


def _st10_with_system_reads(lpar_reads, prof_read=("PASS", "profile-a\n"), apply=None):
    """ST10 answers whose full ``lssyscfg`` reads and ``-o apply`` are scripted."""
    base = _st10_answers()
    run_command = base["hmc_run_command"]
    partitions = iter(lpar_reads)

    def answer(kwargs):
        cmd = kwargs["cmd"]
        if cmd.startswith("lssyscfg -r prof "):
            return prof_read
        if cmd.startswith("lssyscfg -r lpar -m ") and "-F" not in cmd:
            return next(partitions)
        if apply is not None and " -o apply " in cmd:
            return apply
        return run_command(kwargs)  # type: ignore[operator]  # the scripted answer is callable

    return _answer({**base, "hmc_run_command": answer})


def _manual_recovery_rows(state) -> list[dict[str, Any]]:
    return [
        row
        for row in state.results
        if row["tool"] == "chsyscfg -o apply (re-apply after restore)"
        and "MANUAL RECOVERY REQUIRED" in row["note"]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["prof", "lpar"])
async def test_a_failed_pre_restore_read_skips_the_restore(monkeypatch, failed) -> None:
    """Without both baselines the restore can be neither compared nor compensated."""
    lost = ("FAIL", "ssh lost")
    calls, scripted = _st10_with_system_reads(
        [lost if failed == "lpar" else ("PASS", _CONFIGURED + "\n")],
        prof_read=lost if failed == "prof" else ("PASS", "profile-a\n"),
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    assert _tool_calls(calls, "hmc_restore_lpar_profiles") == []
    observations = _verified(state)
    assert "lpar_profile.restore" not in observations
    assert observations["lpar_profile.backup"]["assertions"] == ["backup-accepted"]
    skip = next(
        row for row in state.results if row["tool"] == "hmc_restore_lpar_profiles"
    )
    assert skip["status"] == "SKIP"
    assert "pre-restore" in skip["note"]


_UNCONFIGURED = _CONFIGURED.replace("config=1", "config=0")
_NO_PROFILE = "curr_profile=default_profile", "curr_profile="
_REFUSED = observation.CallFailure(
    "ToolError", "HSCL partition busy", "Traceback\n" + "frame\n" * 400, None, False
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "after", "apply", "profile"),
    [
        (
            _CONFIGURED.replace(*_NO_PROFILE),
            ("PASS", _UNCONFIGURED.replace(*_NO_PROFILE) + "\n"),
            None,
            "<profile>",
        ),
        (
            _CONFIGURED,
            ("PASS", _UNCONFIGURED + "\n"),
            ("FAIL", _REFUSED),
            "default_profile",
        ),
        (_CONFIGURED, ("FAIL", "ssh lost"), None, "default_profile"),
        (
            _CONFIGURED,
            ("PASS", "name=other,resource_config=1\n"),
            None,
            "default_profile",
        ),
    ],
    ids=["empty-curr-profile", "apply-refused", "post-read-failed", "absent-after"],
)
async def test_a_partition_that_cannot_be_reapplied_needs_manual_recovery(
    monkeypatch, before, after, apply, profile
) -> None:
    calls, scripted = _st10_with_system_reads(
        [("PASS", before + "\n"), after, ("PASS", before + "\n")], apply=apply
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    [row] = _manual_recovery_rows(state)
    assert row["status"] == "FAIL"
    assert (
        f"partition 'lpar-name' — if its resource_config is 0, run chsyscfg -r lpar "
        f"-m {state.config.system_name} -o apply -p lpar-name -n {profile}"
        in row["note"]
    )
    assert "frame" not in row["data"]
    applies = [
        kwargs for tool, kwargs in calls if " -o apply " in str(kwargs.get("cmd", ""))
    ]
    assert len(applies) == (1 if apply is not None else 0)


@pytest.mark.asyncio
async def test_each_unconfigured_partition_is_reapplied_or_reported(
    monkeypatch,
) -> None:
    kept = _CONFIGURED.replace("lpar-name", "kept")
    # A dotted name is one the FAIL data's hostname redaction would mask.
    other = _CONFIGURED.replace("lpar-name", "lp.other").replace(*_NO_PROFILE)
    before = f"{kept}\n{_CONFIGURED}\n{other}\n"
    after = f"{kept}\n{_UNCONFIGURED}\n{other.replace('config=1', 'config=0')}\n"
    calls, scripted = _st10_with_system_reads(
        [("PASS", before), ("PASS", after), ("PASS", before)]
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    applies = [
        kwargs["cmd"]
        for tool, kwargs in calls
        if " -o apply " in str(kwargs.get("cmd", ""))
    ]
    assert applies == [
        f"chsyscfg -r lpar -m {state.config.system_name} -o apply -p lpar-name -n default_profile"
    ]
    [row] = _manual_recovery_rows(state)
    assert "partition 'lp.other'" in row["note"]
    assert "-p lp.other -n <profile>" in row["note"]
    assert "-F lpar_name,name)" in row["note"]


@pytest.mark.asyncio
async def test_a_reordered_profile_listing_is_not_a_change(monkeypatch) -> None:
    dumps = iter(["profile-a\nprofile-b\n", "profile-b\nprofile-a\n"])
    base = _st10_answers()
    run_command = base["hmc_run_command"]

    def answer(kwargs):
        if kwargs["cmd"].startswith("lssyscfg -r prof "):
            return "PASS", next(dumps)
        return run_command(kwargs)  # type: ignore[operator]  # the scripted answer is callable

    _calls, scripted = _answer({**base, "hmc_run_command": answer})
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    assert _verified(state)["lpar_profile.restore"]["result"] == "passed"


@pytest.mark.asyncio
async def test_proc_compat_round_trip_skips_an_original_the_tool_cannot_write(
    monkeypatch,
) -> None:
    calls, scripted = _answer(
        _st10_answers(
            hmc_get_lpar_proc_compat=(
                "PASS",
                {"profile": "default_profile", "profile_mode": "unsupported_mode"},
            )
        )
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    assert _tool_calls(calls, "hmc_set_lpar_proc_compat") == []
    assert "lpar.set_proc_compat" not in _verified(state)


@pytest.mark.asyncio
async def test_st10_round_trips_and_restores_power9_base_profile(monkeypatch) -> None:
    """#1319: a profile in POWER9_base mode can be changed and restored."""
    profile_mode = iter(["POWER9_base", "POWER9", "POWER9_base"])
    calls, scripted = _answer(
        _st10_answers(
            hmc_get_lpar_proc_compat=lambda _kwargs: (
                "PASS",
                {"profile": "default_profile", "profile_mode": next(profile_mode)},
            )
        )
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()

    await lpar.mutate_lpar_properties(None, state)

    assert _verified(state)["lpar.set_proc_compat"]["result"] == "passed"
    assert [m["mode"] for m in _tool_calls(calls, "hmc_set_lpar_proc_compat")] == [
        "POWER9",
        "POWER9_base",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["round2", "all", None])
async def test_other_arms_never_restore_profiles(monkeypatch, group) -> None:
    calls, scripted = _answer(_st10_answers())
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state(group)

    await lpar.mutate_lpar_properties(None, state)

    assert _tool_calls(calls, "hmc_backup_lpar_profiles") == []
    assert _tool_calls(calls, "hmc_restore_lpar_profiles") == []
    # Only the profiles arm touches the VIOS, the profile mode or the sync setting.
    assert [m["lpar_name_or_uuid"] for m in _tool_calls(calls, "hmc_set_lpar_msp")] == [
        "lpar-name"
    ]
    assert _tool_calls(calls, "hmc_set_lpar_proc_compat") == []
    assert _tool_calls(calls, "hmc_sync_lpar_profile") == []


@pytest.mark.asyncio
async def test_st15_leaves_a_baseline_mode_the_tool_cannot_write(monkeypatch) -> None:
    calls, scripted = _answer({})
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = runner.RunState()
    state.artifacts.lp3_baseline.update(
        description="baseline",
        proc_compat={"profile": "default_profile", "profile_mode": "unsupported_mode"},
    )

    await runner.restore_lpar_baseline(None, state)

    assert _tool_calls(calls, "hmc_set_lpar_proc_compat") == []
    row = next(
        r for r in state.results if r["tool"] == "hmc_set_lpar_proc_compat (restore)"
    )
    assert row["status"] == "SKIP"


@pytest.mark.asyncio
async def test_st15_restores_a_power9_base_baseline_mode(monkeypatch) -> None:
    """#1319: ST15 restores a POWER9_base baseline profile mode."""
    calls, scripted = _answer(
        {
            "hmc_lpar_summary": ("PASS", {}),
            "hmc_set_lpar_proc_compat": ("PASS", ""),
            "hmc_get_lpar_description": ("PASS", "baseline\n"),
            "hmc_get_lpar_msp": ("PASS", True),
            "hmc_run_command": ("PASS", "0,Not Activated\n"),
        }
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = runner.RunState()
    state.config = dataclasses.replace(state.config, lp3_name="lpar-name")
    state.artifacts.lp3_baseline.update(
        description="baseline",
        sync_curr_profile="0",
        state="Not Activated",
        proc_compat={"profile": "default_profile", "profile_mode": "POWER9_base"},
    )

    await runner.restore_lpar_baseline(None, state)

    assert [m["mode"] for m in _tool_calls(calls, "hmc_set_lpar_proc_compat")] == [
        "POWER9_base"
    ]
    row = next(
        r for r in state.results if r["tool"] == "hmc_set_lpar_proc_compat (restore)"
    )
    assert row["status"] == "PASS"


@pytest.mark.asyncio
async def test_a_synchronized_profile_skips_the_proc_compat_round_trip(
    monkeypatch,
) -> None:
    """#1333: the HMC refuses a profile change while sync_curr_profile is 1."""
    calls, scripted = _answer(
        _st10_answers(sync_reads=("0,Not Activated", "1,Not Activated"))
    )
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = _st10_state()
    state.artifacts.lp3_baseline["sync_curr_profile"] = "1"

    await lpar.mutate_lpar_properties(None, state)

    assert _tool_calls(calls, "hmc_set_lpar_proc_compat") == []
    observations = _verified(state)
    assert "lpar.set_proc_compat" not in observations
    assert observations["lpar_profile.sync"]["result"] == "passed"
    skip = next(
        r for r in state.results if r["tool"] == "hmc_set_lpar_proc_compat (round trip)"
    )
    assert skip["status"] == "SKIP"
    assert "sync_curr_profile" in skip["note"]


@pytest.mark.asyncio
async def test_st15_leaves_the_mode_of_a_synchronized_profile(monkeypatch) -> None:
    """#1333: ST10 never changes a synchronized profile, and the HMC refuses the write."""
    calls, scripted = _answer({})
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = runner.RunState()
    state.artifacts.lp3_baseline.update(
        description="baseline",
        sync_curr_profile="1",
        proc_compat={"profile": "default_profile", "profile_mode": "default"},
    )

    await runner.restore_lpar_baseline(None, state)

    assert _tool_calls(calls, "hmc_set_lpar_proc_compat") == []
    row = next(
        r for r in state.results if r["tool"] == "hmc_set_lpar_proc_compat (restore)"
    )
    assert row["status"] == "SKIP"
    assert "sync_curr_profile" in row["note"]


@pytest.mark.asyncio
async def test_st15_without_a_baseline_profile_mode_asks_for_a_manual_restore(
    monkeypatch,
) -> None:
    calls, scripted = _answer({})
    monkeypatch.setattr(runner.RunState, "call", scripted)
    state = runner.RunState()
    state.artifacts.lp3_baseline["description"] = "baseline"

    await runner.restore_lpar_baseline(None, state)

    assert _tool_calls(calls, "hmc_set_lpar_proc_compat") == []
    row = next(
        r for r in state.results if r["tool"] == "hmc_set_lpar_proc_compat (restore)"
    )
    assert row["status"] == "FAIL"
    assert "MANUAL RECOVERY REQUIRED" in row["data"]


class TestDescriptionBaselineRestore:
    """#968: the ownership stamp survives ST10, or its loss fails the run."""

    _STAMP = "[hmcpctl owner:hmcpctl created:2026-09-23]"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminator", ["\n", "\r\n"])
    async def test_a_cli_line_terminator_is_not_part_of_the_restored_baseline(
        self, terminator: str
    ) -> None:
        state = _ScriptedSriovState(
            [
                ("hmc_get_lpar", "PASS", {"uuid": "lp3-uuid"}),
                ("hmc_lpar_summary", "PASS", {}),
                ("hmc_get_lpar_description", "PASS", self._STAMP + terminator),
                ("hmc_get_lpar_msp", "PASS", {}),
                ("hmc_get_lpar_proc_compat", "PASS", {}),
                ("hmc_set_lpar_description", "PASS", {}),
                ("hmc_get_lpar_description", "PASS", self._STAMP + terminator),
            ]
        )

        await inventory._capture_lpar_properties(object(), state)
        restored = await lpar._restore_description(object(), state, 10)

        assert state.artifacts.lp3_baseline["description"] == self._STAMP
        assert restored is True
        assert state.calls[-2] == (
            "hmc_set_lpar_description",
            {
                "system_name_or_uuid": state.config.system_name,
                "lpar_name_or_uuid": state.config.lp3_name,
                "description": self._STAMP,
            },
        )
        assert state.results[-1]["status"] == "PASS"

    @pytest.mark.asyncio
    async def test_an_unrestorable_baseline_fails_with_a_manual_recovery_row(
        self,
    ) -> None:
        state = _ScriptedSriovState([])
        state.artifacts.lp3_baseline["description"] = "café stamp"

        await lpar._restore_description(object(), state, 10)

        assert state.calls == []
        row = state.results[-1]
        assert (row["tool"], row["status"]) == (
            "hmc_set_lpar_description (restore)",
            "FAIL",
        )
        assert "MANUAL RECOVERY REQUIRED" in row["data"]
        assert "chsyscfg -r lpar" in row["data"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("baseline", [None, "café stamp"])
    async def test_manual_recovery_text_quotes_the_system_name(self, baseline) -> None:
        """#1113: the pasteable chsyscfg line quotes system_name on both paths."""
        state = _ScriptedSriovState([])
        state.config = dataclasses.replace(state.config, system_name="sys; reboot")
        if baseline is not None:
            state.artifacts.lp3_baseline["description"] = baseline

        await lpar._restore_description(object(), state, 10)

        assert "chsyscfg -r lpar -m 'sys; reboot' " in state.results[-1]["data"]

    @pytest.mark.asyncio
    async def test_an_absent_baseline_key_fails_with_a_manual_recovery_row(
        self,
    ) -> None:
        """No ST0 baseline was ever recorded for this key (e.g. a resumed results
        file that never captured it) — distinct from a baseline that was really
        empty, which restores normally (#1038)."""
        state = _ScriptedSriovState([])
        assert "description" not in state.artifacts.lp3_baseline

        await lpar._restore_description(object(), state, 10)

        assert state.calls == []
        row = state.results[-1]
        assert (row["tool"], row["status"]) == (
            "hmc_set_lpar_description (restore)",
            "FAIL",
        )
        assert "MANUAL RECOVERY REQUIRED" in row["data"]
        assert "no baseline description was captured" in row["data"]

    @pytest.mark.asyncio
    async def test_a_failed_st0_read_leaves_the_key_absent_and_fails_restore(
        self,
    ) -> None:
        state = _ScriptedSriovState(
            [
                ("hmc_get_lpar", "PASS", {"uuid": "lp3-uuid"}),
                ("hmc_lpar_summary", "PASS", {}),
                ("hmc_get_lpar_description", "FAIL", {"error": "timeout"}),
                ("hmc_get_lpar_msp", "PASS", {}),
                ("hmc_get_lpar_proc_compat", "PASS", {}),
            ]
        )

        await inventory._capture_lpar_properties(object(), state)
        assert "description" not in state.artifacts.lp3_baseline

        calls_before = len(state.calls)
        await lpar._restore_description(object(), state, 15)

        assert len(state.calls) == calls_before
        row = state.results[-1]
        assert (row["tool"], row["status"]) == (
            "hmc_set_lpar_description (restore)",
            "FAIL",
        )
        assert "MANUAL RECOVERY REQUIRED" in row["data"]
        assert "no baseline description was captured" in row["data"]


@pytest.mark.asyncio
async def test_sriov_orchestrator_runs_phases_in_order_and_cleans_up() -> None:
    """A successful round trip invokes every phase and always reaches cleanup."""
    calls: list[str] = []

    def phase(name: str):
        def run(*_args) -> bool:
            calls.append(name)
            return True

        return run

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            pcie, "capture_sriov_baseline", AsyncMock(side_effect=phase("baseline"))
        )
        monkeypatch.setattr(
            pcie, "assign_sriov_to_lp3", AsyncMock(side_effect=phase("assign"))
        )
        monkeypatch.setattr(
            pcie, "verify_sriov_assigned", AsyncMock(side_effect=phase("verify"))
        )
        monkeypatch.setattr(
            pcie, "unassign_sriov_from_lp3", AsyncMock(side_effect=phase("unassign"))
        )
        monkeypatch.setattr(
            pcie, "reassign_sriov_to_lp3", AsyncMock(side_effect=phase("reassign"))
        )
        monkeypatch.setattr(
            pcie, "cleanup_sriov", AsyncMock(side_effect=phase("cleanup"))
        )

        class State:
            def skip(self, *_args) -> None:
                raise AssertionError("successful orchestration must not skip a phase")

        await pcie.exercise_sriov_assignment(object(), State())
    finally:
        monkeypatch.undo()

    assert calls == ["baseline", "assign", "verify", "unassign", "reassign", "cleanup"]


@pytest.mark.asyncio
async def test_sriov_orchestrator_stops_after_baseline_but_runs_cleanup() -> None:
    """A failed baseline prevents mutations while preserving the cleanup arm."""
    calls: list[str] = []

    async def baseline(*_args) -> bool:
        calls.append("baseline")
        return False

    async def cleanup(*_args) -> None:
        calls.append("cleanup")

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(pcie, "capture_sriov_baseline", baseline)
        monkeypatch.setattr(pcie, "cleanup_sriov", cleanup)
        for name in (
            "assign_sriov_to_lp3",
            "verify_sriov_assigned",
            "unassign_sriov_from_lp3",
            "reassign_sriov_to_lp3",
        ):
            monkeypatch.setattr(pcie, name, AsyncMock(side_effect=AssertionError(name)))

        class State:
            pass

        await pcie.exercise_sriov_assignment(object(), State())
    finally:
        monkeypatch.undo()

    assert calls == ["baseline", "cleanup"]


@pytest.mark.asyncio
async def test_sriov_orchestrator_skips_mutations_after_assign_failure() -> None:
    """Assignment failure still verifies state, skips later mutations, and cleans up."""
    calls: list[str] = []

    async def baseline(*_args) -> bool:
        calls.append("baseline")
        return True

    async def assign(*_args) -> bool:
        calls.append("assign")
        return False

    async def verify(*_args) -> bool:
        calls.append("verify")
        return True

    async def cleanup(*_args) -> None:
        calls.append("cleanup")

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(pcie, "capture_sriov_baseline", baseline)
        monkeypatch.setattr(pcie, "assign_sriov_to_lp3", assign)
        monkeypatch.setattr(pcie, "verify_sriov_assigned", verify)
        monkeypatch.setattr(pcie, "cleanup_sriov", cleanup)
        monkeypatch.setattr(
            pcie, "unassign_sriov_from_lp3", AsyncMock(side_effect=AssertionError)
        )
        monkeypatch.setattr(
            pcie, "reassign_sriov_to_lp3", AsyncMock(side_effect=AssertionError)
        )

        class State:
            def skip(self, *_args) -> None:
                calls.append("skip")

        await pcie.exercise_sriov_assignment(object(), State())
    finally:
        monkeypatch.undo()

    assert calls == ["baseline", "assign", "verify", "skip", "skip", "cleanup"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing_phase", "expected_calls"),
    [
        ("assign", ["baseline", "assign", "cleanup"]),
        ("verify", ["baseline", "assign", "verify", "cleanup"]),
        ("unassign", ["baseline", "assign", "verify", "unassign", "cleanup"]),
        (
            "reassign",
            ["baseline", "assign", "verify", "unassign", "reassign", "cleanup"],
        ),
    ],
)
async def test_sriov_orchestrator_cleans_up_after_post_baseline_error(
    monkeypatch, failing_phase: str, expected_calls: list[str]
) -> None:
    """Every mutation failure leaves cleanup as the final, single operation."""
    calls: list[str] = []

    def phase(name: str):
        async def run(*_args) -> bool:
            calls.append(name)
            if name == failing_phase:
                raise RuntimeError(f"{name} failed")
            return True

        return run

    monkeypatch.setattr(pcie, "capture_sriov_baseline", phase("baseline"))
    monkeypatch.setattr(pcie, "assign_sriov_to_lp3", phase("assign"))
    monkeypatch.setattr(pcie, "verify_sriov_assigned", phase("verify"))
    monkeypatch.setattr(pcie, "unassign_sriov_from_lp3", phase("unassign"))
    monkeypatch.setattr(pcie, "reassign_sriov_to_lp3", phase("reassign"))
    monkeypatch.setattr(pcie, "cleanup_sriov", phase("cleanup"))

    with pytest.raises(RuntimeError, match=f"{failing_phase} failed"):
        await pcie.exercise_sriov_assignment(object(), object())

    assert calls == expected_calls


@pytest.mark.asyncio
async def test_sriov_orchestrator_propagates_cleanup_error(monkeypatch) -> None:
    """Cleanup remains observable when it is the only failure."""
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))

    monkeypatch.setattr(pcie, "capture_sriov_baseline", AsyncMock(return_value=True))
    monkeypatch.setattr(pcie, "assign_sriov_to_lp3", AsyncMock(return_value=True))
    monkeypatch.setattr(pcie, "verify_sriov_assigned", AsyncMock(return_value=True))
    monkeypatch.setattr(pcie, "unassign_sriov_from_lp3", AsyncMock(return_value=True))
    monkeypatch.setattr(pcie, "reassign_sriov_to_lp3", AsyncMock(return_value=True))
    monkeypatch.setattr(pcie, "cleanup_sriov", cleanup)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await pcie.exercise_sriov_assignment(object(), object())

    assert cleanup.await_count == 1


@pytest.mark.asyncio
async def test_sriov_orchestrator_preserves_mutation_error_when_cleanup_fails(
    monkeypatch,
) -> None:
    """A cleanup failure adds recovery context without replacing the mutation error."""
    cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))

    monkeypatch.setattr(pcie, "capture_sriov_baseline", AsyncMock(return_value=True))
    monkeypatch.setattr(
        pcie,
        "assign_sriov_to_lp3",
        AsyncMock(side_effect=RuntimeError("assign failed")),
    )
    monkeypatch.setattr(pcie, "cleanup_sriov", cleanup)

    with pytest.raises(RuntimeError, match="assign failed") as exc_info:
        await pcie.exercise_sriov_assignment(object(), object())

    assert cleanup.await_count == 1
    assert exc_info.value.__notes__ == ["SR-IOV cleanup failed: cleanup failed"]


def _isolate_runner(monkeypatch) -> None:
    monkeypatch.setattr(runner, "Client", _FakeClient)

    async def configure(enabled, application, *, permits=None, authorize=None) -> None:
        assert enabled is True
        # The old assertion here was `application is runner.mcp`. ADR 0041 removed that
        # module-level object, and the identity check had nothing left to compare
        # against — so this asserts the property the runner actually needs instead:
        # the toggle is handed the gates of the policy the runner composed, and that
        # policy grants the escape hatch. Called with neither, `permits=None` would
        # register the tool whatever the policy said and `authorize=None` would leave
        # its handler unwrapped, which is how a live run stops being evidence.
        assert permits is not None and authorize is not None
        assert permits("hmc_run_command") is True

    monkeypatch.setattr(runner, "configure_arbitrary_command_tool", configure)


def _clear(monkeypatch, name: str) -> None:
    """Delete every casing of *name*, not just the canonical one.

    The runner reads its `HMC_*` variables the way `HMCConfig` does, so a test
    that isolates one spelling does not isolate the variable: an ambient
    `hmc_schema_version` in a developer's shell or on a CI runner reaches the
    code under test and the assertion fails saying nothing about casing (#543).
    """
    for spelling in [k for k in list(os.environ) if k.lower() == name.lower()]:
        monkeypatch.delenv(spelling, raising=False)


def _isolated_environ(monkeypatch) -> None:
    """Give the test its own ``os.environ``, restored on teardown.

    The runner mutates the process environment directly — it injects, and now
    deletes — so a key the code under test creates is not one ``monkeypatch``
    recorded, and teardown cannot take it back. Swapping the mapping itself is
    what keeps a runner test from leaking an `HMC_*` variable into every test
    that runs after it.
    """
    monkeypatch.setattr(os, "environ", dict(os.environ))


def _startup_gate(monkeypatch, tmp_path) -> list[dict[str, object]]:
    """Stub everything `_run_from_arguments` touches except the run itself.

    Returns the list the stubbed `main` appends to, so a caller asserts on
    whether the run started rather than on a return code the stub chose.
    """
    repo = _live_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        runner.LiveTestConfig,
        "from_env_file",
        classmethod(lambda _cls: runner.LiveTestConfig()),
    )
    monkeypatch.setattr(runner, "_read_environment", lambda *_a: ("V10R3", "POWER10"))
    monkeypatch.setattr(runner, "_bootstrap_config", lambda: True)
    started: list[dict[str, object]] = []

    async def _record(**kwargs: object) -> int:
        started.append(kwargs)
        return 0

    monkeypatch.setattr(runner, "main", _record)
    return started


def test_the_startup_gate_runs_with_the_schema_version_absent(monkeypatch, tmp_path):
    """#875. The variable is opt-in, so its absence cannot refuse to start a run."""
    _clear(monkeypatch, "HMC_SCHEMA_VERSION")
    _isolated_environ(monkeypatch)
    started = _startup_gate(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_load_dotenv", lambda: None)

    assert runner._run_from_arguments([]) == 0
    assert started, "the gate refused to start a run with HMC_SCHEMA_VERSION absent"


def test_the_startup_gate_resolves_a_dotenv_only_schema_version(monkeypatch, tmp_path):
    """#875. `_bootstrap_config` reads `.env` only when the TOML profile fails.

    Deleting the old hard gate deletes the one unconditional `_load_dotenv()`
    on the startup path, so a `.env`-only `HMC_*` value would stop resolving
    whenever a profile loaded — and the run header below would then report a
    request environment the run did not use.
    """
    _clear(monkeypatch, "HMC_SCHEMA_VERSION")
    _isolated_environ(monkeypatch)
    started = _startup_gate(monkeypatch, tmp_path)
    dotenv = tmp_path / ".env"
    dotenv.write_text("HMC_SCHEMA_VERSION=V1_0\n", encoding="utf-8")
    monkeypatch.setattr(runner, "_ENV_FILE", dotenv)

    assert runner._run_from_arguments([]) == 0

    assert started
    assert runner.env_var_value("HMC_SCHEMA_VERSION") == "V1_0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spelling", "reported"),
    [
        (None, "HMC_SCHEMA_VERSION=(not set)"),
        # #543: `HMCConfig` reads the name case-blind, so a lower-case export
        # reaches the client. A header that reported `(not set)` for one would
        # describe the wrong request environment.
        ("hmc_schema_version", "HMC_SCHEMA_VERSION=V1_0"),
        ("HMC_SCHEMA_VERSION", "HMC_SCHEMA_VERSION=V1_0"),
    ],
)
async def test_the_run_header_records_which_way_the_run_went(
    monkeypatch, tmp_path, capsys, spelling, reported
):
    """#875 / ADR 0162. The variable is optional, so the evidence must say.

    ADR 0162's recorded rows were gathered with the header pinned. A later run
    may now legitimately execute without it, and the run header is the only
    place a reader of `test-results-*.json` can tell the two apart.
    """
    _clear(monkeypatch, "HMC_SCHEMA_VERSION")
    _isolated_environ(monkeypatch)
    _isolate_runner(monkeypatch)
    if spelling is not None:
        monkeypatch.setenv(spelling, "V1_0")
        assert HMCConfig(host="h", user="u", password="p").schema_version == "V1_0"

    await runner.main(
        999, str(tmp_path / "results.json"), config=runner.LiveTestConfig()
    )

    assert reported in capsys.readouterr().out


def test_the_iso_allowlist_merge_reaches_the_field_and_is_idempotent(monkeypatch):
    """#543 / ADR 0050. The merged value has to be the one the loader resolves.

    Three post-conditions, because the runner cannot diagnose their absence: it
    prints the allowlist it believes it set, and if a case variant still outranks
    the canonical spelling, ADR 0050 refuses every upload in the run while the
    printed evidence says the host is permitted.
    """
    name = "HMC_ISO_URL_ALLOWLIST"
    _isolated_environ(monkeypatch)
    _clear(monkeypatch, name)
    # Two casings, two *different* values: identical ones would leave an
    # exact-case read and a folded one returning the same string, and the test
    # could not tell which one it was running against.
    monkeypatch.setenv(name, "canonical.example.com")
    monkeypatch.setenv("hmc_iso_url_allowlist", "variant.example.com")

    config = runner.LiveTestConfig()
    vmedia._allow_iso_host(config)

    merged = os.environ[name]
    assert [k for k in os.environ if k.lower() == name.lower()] == [name]
    assert merged.split(",") == ["variant.example.com", config.iso_host]
    assert HMCConfig(host="h", user="u", password="p").iso_url_allowlist == merged

    vmedia._allow_iso_host(config)
    assert os.environ[name] == merged


def test_the_iso_allowlist_merge_keeps_a_variant_only_operator_entry(monkeypatch):
    """#543 / ADR 0050. The deletion loop must never run on an empty merge.

    This is the case the deletion makes destructive: with only a variant set, an
    exact-case read yields nothing, `entries` becomes the runner's own host
    alone, and the loop then deletes the key that held the operator's ISO
    servers. They would be gone from the run with no diagnostic — the banner
    prints a one-entry allowlist that looks deliberate.
    """
    name = "HMC_ISO_URL_ALLOWLIST"
    _isolated_environ(monkeypatch)
    _clear(monkeypatch, name)
    monkeypatch.setenv("hmc_iso_url_allowlist", "operator.example.com")

    config = runner.LiveTestConfig()
    vmedia._allow_iso_host(config)

    assert os.environ[name].split(",") == ["operator.example.com", config.iso_host]


def test_live_config_is_frozen() -> None:
    config = runner.LiveTestConfig()

    with pytest.raises(FrozenInstanceError):
        config.system_name = "changed"


def test_live_config_reads_the_complete_example_and_ignores_exports(
    monkeypatch, tmp_path
) -> None:
    """The checked-in example is a complete, authoritative live-test mapping."""
    example = Path(__file__).parents[1] / ".env.example"
    config_path = tmp_path / ".env"
    config_path.write_text(example.read_text())
    monkeypatch.setenv("LIVE_TEST_SYSTEM_NAME", "ambient-target")

    config = runner.LiveTestConfig.from_env_file(config_path)

    assert config.system_name == "example-lt-609-system"
    assert config.sriov_logical_port_id == "917003"
    assert config.scratch_create_desired_procs == 0.3
    assert config.scratch_create_max_procs == 0.6
    assert config.iso_url == "http://iso.example.test:18090/example-lt-609.iso"
    assert config.protected_lpar_names == (
        "example-lt-609-protected-a",
        "example-lt-609-protected-b",
    )


def _example_env_with(tmp_path: Path, key: str, value: str) -> Path:
    """Write the checked-in example with one key overridden."""
    example = Path(__file__).parents[1] / ".env.example"
    lines = [
        f"{key}={value}" if line.startswith(f"{key}=") else line
        for line in example.read_text().splitlines()
    ]
    config_path = tmp_path / ".env"
    config_path.write_text("\n".join(lines) + "\n")
    return config_path


def test_live_config_reads_the_bare_cec_dump_opt_in_from_the_example(tmp_path) -> None:
    """The example documents the key commented out; uncommented, the runner accepts it."""
    example = Path(__file__).parents[1] / ".env.example"
    text = example.read_text().replace(
        "#LIVE_TEST_ACCEPT_PLATFORM_DUMP=false", "LIVE_TEST_ACCEPT_PLATFORM_DUMP=true"
    )
    assert "LIVE_TEST_ACCEPT_PLATFORM_DUMP=true" in text
    config_path = tmp_path / ".env"
    config_path.write_text(text)

    assert (
        runner.LiveTestConfig.from_env_file(config_path).accept_platform_dump == "true"
    )
    assert runner.LiveTestConfig().accept_platform_dump == ""


def test_live_config_keeps_a_hex_sriov_logical_port_id_as_a_string(tmp_path) -> None:
    """The HMC reports logical port ids such as 2700400a; `_ID` keys parse as int."""
    config_path = _example_env_with(
        tmp_path, "LIVE_TEST_SRIOV_LOGICAL_PORT_ID", "2700400a"
    )

    config = runner.LiveTestConfig.from_env_file(config_path)

    assert config.sriov_logical_port_id == "2700400a"


@pytest.mark.parametrize(
    "value", ["", "27004 00a", "2700400a; reboot", "xyz", "2700400A"]
)
def test_live_config_rejects_a_non_hex_sriov_logical_port_id(tmp_path, value) -> None:
    """The id reaches recovery shell commands unquoted, so only lowercase hex digits (as the HMC reports them) load."""
    config_path = _example_env_with(tmp_path, "LIVE_TEST_SRIOV_LOGICAL_PORT_ID", value)

    with pytest.raises(ValueError, match="LIVE_TEST_SRIOV_LOGICAL_PORT_ID"):
        runner.LiveTestConfig.from_env_file(config_path)


def test_live_config_accepts_zero_sriov_physical_port_id(tmp_path) -> None:
    """Physical port IDs are zero-indexed on Power, so port 0 is the first port.

    A `> 0` check here aborts every SR-IOV run configured against port 0
    before the runner reaches the HMC.
    """
    config_path = _example_env_with(tmp_path, "LIVE_TEST_SRIOV_PHYSICAL_PORT_ID", "0")

    config = runner.LiveTestConfig.from_env_file(config_path)

    assert config.sriov_physical_port_id == 0


def test_default_sriov_capacity_is_a_multiple_of_every_recorded_port_granularity() -> (
    None
):
    """#1082: the assign path refuses a capacity that is not a granularity multiple.

    The recorded HMC ports report 1.0 (roce) and 2.0 (ethc); a default that fails
    either stops the SR-IOV arm at its own pre-check instead of exercising assign.
    """
    fixture = (
        Path(__file__).parent / "fixtures/sriov/sriov-physport-granularity-v10r3.json"
    )
    granularities = {
        Decimal(value)
        for value in re.findall(
            r'"min_eth_capacity_granularity": "([^"]+)"', fixture.read_text()
        )
    }
    example = runner.LiveTestConfig.from_env_file(
        Path(__file__).parents[1] / ".env.example"
    )

    assert granularities == {Decimal("1.0"), Decimal("2.0")}
    defaults = (
        runner.LiveTestConfig().sriov_capacity_percent,
        example.sriov_capacity_percent,
    )
    for capacity in defaults:
        assert all(Decimal(str(capacity)) % step == 0 for step in granularities), (
            capacity
        )


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("LIVE_TEST_SCRATCH_CREATE_DESIRED_PROCS", "", "SCRATCH_CREATE_DESIRED_PROCS"),
        ("LIVE_TEST_SCRATCH_CREATE_MAX_PROCS", "", "SCRATCH_CREATE_MAX_PROCS"),
        ("LIVE_TEST_SCRATCH_CREATE_DESIRED_PROCS", "half", "could not convert"),
        ("LIVE_TEST_SCRATCH_CREATE_DESIRED_PROCS", "0", "scratch_create_desired_procs"),
        ("LIVE_TEST_SCRATCH_CREATE_MAX_PROCS", "-0.5", "scratch_create_max_procs"),
        (
            "LIVE_TEST_SCRATCH_CREATE_DESIRED_PROCS",
            "nan",
            "scratch_create_desired_procs",
        ),
        ("LIVE_TEST_SCRATCH_CREATE_MAX_PROCS", "inf", "scratch_create_max_procs"),
        (
            "LIVE_TEST_SCRATCH_CREATE_DESIRED_PROCS",
            "0.7",
            "inconsistent resource limits",
        ),
    ],
)
def test_live_config_rejects_unusable_scratch_processing_units(
    tmp_path, key, value, match
) -> None:
    """#947: units are required, finite, positive, and desired <= max."""
    config_path = _example_env_with(tmp_path, key, value)

    with pytest.raises(ValueError, match=match):
        runner.LiveTestConfig.from_env_file(config_path)


@pytest.mark.parametrize(
    "key",
    [
        "LIVE_TEST_VMEDIA_REPOSITORY_SIZE_MIB",
        "LIVE_TEST_VMEDIA_SHORT_REPOSITORY_SIZE_MIB",
    ],
)
def test_live_config_rejects_a_media_repository_size_that_is_not_whole_gib(
    tmp_path, key
) -> None:
    """#963: the HMC takes whole GiB, so a stale 1536 fails at load, not mid-arm."""
    config_path = _example_env_with(tmp_path, key, "1536")

    with pytest.raises(ValueError, match=f"{key} must be a multiple of 1024"):
        runner.LiveTestConfig.from_env_file(config_path)


def test_live_config_default_and_example_vdisk_name_fit_the_vios_limit() -> None:
    """#1027: the shipped disk name must not fail ST14 before any HMC write."""
    example = Path(__file__).parents[1] / ".env.example"
    example_value = dict(
        line.split("=", 1) for line in example.read_text().splitlines() if "=" in line
    )["LIVE_TEST_VDISK_NAME"]

    assert len(runner.LiveTestConfig().vdisk_name) <= VIRTUAL_DISK_NAME_MAX
    assert len(example_value) <= VIRTUAL_DISK_NAME_MAX


def test_live_config_rejects_an_over_length_vdisk_name(tmp_path) -> None:
    """#1027: fail at load, naming the variable, not as an ST14 step failure."""
    name = "x" * (VIRTUAL_DISK_NAME_MAX + 1)
    config_path = _example_env_with(tmp_path, "LIVE_TEST_VDISK_NAME", name)

    with pytest.raises(ValueError, match="LIVE_TEST_VDISK_NAME must be at most 15"):
        runner.LiveTestConfig.from_env_file(config_path)

    exact = _example_env_with(tmp_path, "LIVE_TEST_VDISK_NAME", "x" * 15)
    assert runner.LiveTestConfig.from_env_file(exact).vdisk_name == "x" * 15


@pytest.mark.parametrize(
    "key", ["LIVE_TEST_VDISK_VOLUME_GROUP_NAME", "LIVE_TEST_VDISK_NAME"]
)
@pytest.mark.parametrize(
    "value",
    ["a b", "a'b", 'a"b', "a;b", "a$b", "a`b", "-a", "a|b", "a&b", "a\\b"],
)
def test_live_config_rejects_shell_unsafe_vios_names(tmp_path, key, value) -> None:
    """#1033: a name that could alter a VIOS storage command fails at load."""
    config_path = _example_env_with(tmp_path, key, value)

    with pytest.raises(ValueError, match=f"{key} may contain only"):
        runner.LiveTestConfig.from_env_file(config_path)


def test_live_config_accepts_allowlisted_vios_names(tmp_path) -> None:
    config_path = _example_env_with(tmp_path, "LIVE_TEST_VDISK_NAME", "lt_1.a-b")

    assert runner.LiveTestConfig.from_env_file(config_path).vdisk_name == "lt_1.a-b"


def test_live_config_reads_the_provision_fixture_settings(tmp_path) -> None:
    """#970: ST13/ST14's VLAN and disk size come from `.env`, not lab fixtures."""
    config_path = _example_env_with(tmp_path, "LIVE_TEST_PROVISION_VLAN_ID", "4094")

    config = runner.LiveTestConfig.from_env_file(config_path)

    assert config.provision_vlan_id == 4094
    assert config.provision_disk_mib == 10240


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("LIVE_TEST_PROVISION_VLAN_ID", "", "LIVE_TEST_PROVISION_VLAN_ID"),
        ("LIVE_TEST_PROVISION_VLAN_ID", "0", "provision_vlan_id"),
        ("LIVE_TEST_PROVISION_VLAN_ID", "4095", "LIVE_TEST_PROVISION_VLAN_ID"),
        ("LIVE_TEST_PROVISION_DISK_MIB", "", "LIVE_TEST_PROVISION_DISK_MIB"),
        ("LIVE_TEST_PROVISION_DISK_MIB", "0", "provision_disk_mib"),
        (
            "LIVE_TEST_PROVISION_DISK_MIB",
            "1536",
            "LIVE_TEST_PROVISION_DISK_MIB must be a multiple of 1024",
        ),
    ],
)
def test_live_config_rejects_unusable_provision_fixture_settings(
    tmp_path, key, value, match
) -> None:
    config_path = _example_env_with(tmp_path, key, value)

    with pytest.raises(ValueError, match=match):
        runner.LiveTestConfig.from_env_file(config_path)


def test_live_config_rejects_negative_sriov_physical_port_id(tmp_path) -> None:
    config_path = _example_env_with(tmp_path, "LIVE_TEST_SRIOV_PHYSICAL_PORT_ID", "-1")

    with pytest.raises(ValueError, match="sriov_physical_port_id"):
        runner.LiveTestConfig.from_env_file(config_path)


@pytest.mark.asyncio
async def test_main_rejects_missing_live_test_file_before_creating_mcp(
    monkeypatch, tmp_path, capsys
) -> None:
    """Programmatic invocation cannot bypass required local live-test settings."""
    monkeypatch.setattr(runner, "_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(
        runner, "create_mcp", lambda *_args, **_kwargs: pytest.fail("created MCP")
    )

    assert await runner.main(results_path=str(tmp_path / "results.json")) == 1
    output = capsys.readouterr().out
    assert str(tmp_path / ".env") not in output
    assert "live-test configuration file not found" in output


def test_a_dotenv_entry_never_outranks_a_case_variant_export(monkeypatch, tmp_path):
    """#543. `_bootstrap_config`'s priority 1 beats its priority 3 in any casing.

    The exact-case membership test injected the canonical spelling beside an
    exported variant; a newly created key lands last in `os.environ` order, so
    the committed `.env` won and an operator who exported a lab host ran the
    destructive suite against the one `.env` names.
    """
    _isolated_environ(monkeypatch)
    for name in ("HMC_HOST", "HMC_PASSWORD", "HMC_SCHEMA_VERSION"):
        _clear(monkeypatch, name)
    monkeypatch.setenv("hmc_host", "lab-hmc.example.com")
    env_file = tmp_path / ".env"
    env_file.write_text("HMC_HOST=prod-hmc.example.com\nHMC_SCHEMA_VERSION=V1_0\n")
    monkeypatch.setattr(runner, "_ENV_FILE", env_file)

    runner._load_dotenv()

    # `host` is left to the environment deliberately: a constructor argument
    # outranks every environment source, which is the one precedence this test
    # must not use.
    config = HMCConfig(user="u", password="p")
    assert config.host == "lab-hmc.example.com"
    # A name the environment does not carry in any casing is still injected.
    assert config.schema_version == "V1_0"


def test_bootstrap_propagates_unexpected_profile_loader_failure(monkeypatch):
    """Only a configuration rejection authorizes the legacy dotenv fallback."""

    def fail_to_load_profile():
        raise RuntimeError("profile loader defect")

    fallback = pytest.fail
    monkeypatch.setattr("hmcpctl.config.load_profile", fail_to_load_profile)
    monkeypatch.setattr(runner, "_load_dotenv", fallback)

    with pytest.raises(RuntimeError, match="profile loader defect"):
        runner._bootstrap_config()


def test_bootstrap_redacts_config_error_before_dotenv_fallback(monkeypatch, capsys):
    secret_path = "/home/operator/private/config.toml"

    def fail_to_load_profile():
        raise ConfigError(f"{secret_path}: password=runner-secret")

    monkeypatch.setattr("hmcpctl.config.load_profile", fail_to_load_profile)
    monkeypatch.setattr(runner, "_load_dotenv", lambda: None)
    monkeypatch.delenv("HMC_PASSWORD", raising=False)

    assert not runner._bootstrap_config()

    output = capsys.readouterr().out
    assert secret_path not in output
    assert "runner-secret" not in output
    assert "<REDACTED-PATH>" in output
    assert "<REDACTED-SECRET>" in output


@pytest.mark.parametrize(
    "filename",
    [
        "config.toml",
        "test-results-round2.json",
        "CONFIG.TOML",
        "settings.yaml",
        "lab_run.log",
    ],
)
def test_failure_redaction_keeps_a_named_file_readable(filename):
    """#914: `config.toml: no default_profile set` printed as `<REDACTED-HOST>: …`."""
    message = f"{filename}: no default_profile set"

    assert runner._redact_failure_text(message) == message


@pytest.mark.parametrize(
    "hostname",
    [
        "hmc01.lab.example.com",
        "lab.example.toml",
        "hmc.lab.json",
        "example.com",
        "example.py",
        "example.md",
        "results.py",
        "lab_hmc01.example.com",
        "hmc01.lab_net.example.com",
        "_hmc.lab.example.com",
        "hmc_.lab.example.com",
        "hmc-01.lab-a.example.com",
    ],
)
def test_failure_redaction_still_hides_a_hostname(hostname):
    """Only a single-dot name with a non-TLD file extension escapes; `.py` and
    `.md` are country-code TLDs, and any multi-label name stays a hostname.
    An underscore or hyphen in a label must not leave a leading label readable (#927).
    """
    redacted = runner._redact_failure_text(f"connect to {hostname} failed")

    assert redacted == "connect to <REDACTED-HOST> failed"


def test_the_no_credentials_message_names_this_platforms_config_directory(
    monkeypatch, capsys
):
    """It printed a Linux literal, which on macOS names a directory the
    resolver never reads — so the operator it is instructing cannot follow it.
    """
    from hmcpctl.config import ConfigError, config_dir

    def fail_to_load_profile():
        raise ConfigError("no profile")

    monkeypatch.setattr("hmcpctl.config.load_profile", fail_to_load_profile)
    monkeypatch.setattr(runner, "_load_dotenv", lambda: None)
    monkeypatch.delenv("HMC_PASSWORD", raising=False)

    assert not runner._bootstrap_config()

    assert str(config_dir() / "config.toml") in capsys.readouterr().out


def test_a_case_variant_of_an_exact_case_reader_does_not_suppress_its_dotenv_line(
    monkeypatch, tmp_path
):
    """#543. Only the names `HMCConfig` folds may be matched case-blind here.

    `HMC_PROFILE` carries the prefix but is not an `HMCConfig` field:
    `load_profile()` looks it up in `os.environ` directly, so a `hmc_profile`
    export selects no profile. Folding it would let that inert variant suppress
    the `.env` line spelling it canonically — the same silent misrouting this
    sweep closes, running the other way.
    """
    _isolated_environ(monkeypatch)
    for name in ("HMC_PROFILE", "HMC_HOST"):
        _clear(monkeypatch, name)
    monkeypatch.setenv("hmc_profile", "read-by-nothing")
    monkeypatch.setenv("hmc_host", "lab-hmc.example.com")
    env_file = tmp_path / ".env"
    env_file.write_text("HMC_PROFILE=lab\nHMC_HOST=prod-hmc.example.com\n")
    monkeypatch.setattr(runner, "_ENV_FILE", env_file)

    runner._load_dotenv()

    assert os.environ["HMC_PROFILE"] == "lab"
    assert HMCConfig(user="u", password="p").host == "lab-hmc.example.com"


def test_the_already_set_gate_folds_down_like_the_loader(monkeypatch, tmp_path):
    """#543. `_already_set` must decide with the relation the lookup uses.

    Over Unicode `str.lower()` and `str.upper()` are different relations, so the
    direction is load-bearing rather than cosmetic. `hmc_ssh_\u212aey_file`
    (Kelvin sign) lowers onto `ssh_key_file` and is therefore a name `HMCConfig`
    reads, while its upper-fold is not `HMC_SSH_KEY_FILE`. Spelled that way in a
    `.env`, an upper-folding gate calls it a name nothing folds, falls through to
    the exact-case test and injects it — and a newly created key lands last in
    `os.environ` order, so the `.env` value takes the field from the operator's
    export. That is the priority inversion this sweep exists to close, re-opened
    for exactly the names the gate covers.
    """
    _isolated_environ(monkeypatch)
    kelvin = "hmc_ssh_\u212aey_file"
    # The premise, asserted rather than assumed: the loader reads that spelling
    # and an upper-fold does not recognise it.
    assert kelvin.lower() == "hmc_ssh_key_file"
    assert kelvin.upper() != "HMC_SSH_KEY_FILE"

    _clear(monkeypatch, "HMC_SSH_KEY_FILE")
    monkeypatch.setenv("HMC_SSH_KEY_FILE", "/home/op/exported")
    env_file = tmp_path / ".env"
    env_file.write_text(f"{kelvin}=/home/op/from-dotenv\n")
    monkeypatch.setattr(runner, "_ENV_FILE", env_file)

    runner._load_dotenv()

    config = HMCConfig(host="h", user="u", password="p")
    assert config.ssh_key_file == "/home/op/exported"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_ToolResult(data={"UUID": "one"}), {"UUID": "one"}),
        (_ToolResult(content=[_TextBlock('[{"UUID": "two"}]')]), [{"UUID": "two"}]),
        (_ToolResult(content=[_TextBlock("plain text")]), "plain text"),
    ],
)
async def test_call_normalizes_fastmcp_result_shapes(result, expected):
    state = runner.RunState()
    assert await state.call(_ScriptedClient(result=result), "tool") == (
        "PASS",
        expected,
    )


@pytest.mark.asyncio
async def test_call_failure_is_redacted_when_recorded(capsys):
    suffix = "-secret"
    url_credential = "url" + suffix
    credential = "runner" + suffix
    sensitive = (
        f"transport failed password={credential} "
        f"https://operator:{url_credential}@hmc.lab.example.test/api "
        "/home/operator/live-test.toml"
    )
    state = runner.RunState()
    status, data = await state.call(
        _ScriptedClient(error=RuntimeError(sensitive)), "tool"
    )

    assert status == "FAIL"
    state.record(0, "tool", status, data)

    output = capsys.readouterr().out
    recorded = str(state.results[0]["data"])
    for value in (
        credential,
        f"operator:{url_credential}",
        "hmc.lab.example.test",
        "/home/operator/live-test.toml",
    ):
        assert value not in output
        assert value not in recorded
    assert "RuntimeError: transport failed" in recorded
    # The traceback stays on the CallFailure for the caller that classifies it and
    # is not persisted: the results document keeps only the redacted message.
    assert "Traceback" not in recorded
    assert "Traceback" in data.traceback_text


@pytest.mark.asyncio
async def test_call_reports_unexpected_result_parser_failure(monkeypatch):
    def fail_to_parse(_text):
        raise TypeError("parser bug")

    monkeypatch.setattr(runner.json, "loads", fail_to_parse)

    status, data = await runner.RunState().call(
        _ScriptedClient(result=_ToolResult(content=[_TextBlock("plain text")])), "tool"
    )

    assert status == "FAIL"
    assert "TypeError: parser bug" in data.message


def test_expected_hmc_limitation_is_classified_as_skip():
    state = runner.RunState()

    state.record_with_expected(
        5,
        "optional_tool",
        "FAIL",
        observation.classify_failure(RuntimeError("HTTP 406 Not Acceptable")),
        [
            observation.ExpectedOutcome(
                operation="pcm.get_preferences",
                variant="managed-system-pcm",
                reason="feature unavailable",
                error_codes=frozenset({"406"}),
            )
        ],
    )

    assert state.results[0]["status"] == "SKIP"
    assert state.results[0]["note"] == "feature unavailable"


def test_declared_limitations_have_registered_gap_identities():
    declarations = [
        value
        for module in LIVE_WORKFLOW_MODULES
        for value in vars(module).values()
        if isinstance(value, observation.ExpectedOutcome)
    ]
    assert declarations
    registered = {security.operation for security in TOOL_SECURITY.values()}
    assert all(
        getattr(value, "operation", None) in registered for value in declarations
    )
    assert all(
        re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value.variant) for value in declarations
    )


@pytest.mark.parametrize(
    "field,value", [("operation", "bad"), ("variant", ""), ("variant", "x y")]
)
def test_gap_identity_rejects_malformed_tokens(field, value):
    arguments = {
        "operation": "pcm.get_preferences",
        "variant": "managed-system-pcm",
        "reason": "unavailable",
        "error_codes": frozenset({"406"}),
    }
    arguments[field] = value
    with pytest.raises(ValueError, match="operation|variant"):
        observation.ExpectedOutcome(**arguments)


@pytest.mark.asyncio
async def test_gap_identity_rejected_before_call():
    state = runner.RunState()
    client = _ScriptedClient(result={})
    with pytest.raises(ValueError, match="operation"):
        await state.call(
            client, "hmc_list_users", expected=[metrics._PREFERENCES_AUTHORITY]
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_later_invalid_declaration_stops_run_before_client(monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(
        metrics,
        "_PREFERENCES_AUTHORITY",
        replace(metrics._PREFERENCES_AUTHORITY, operation="user.list"),
    )
    monkeypatch.setattr(runner, "create_mcp", lambda *_a: pytest.fail("created client"))
    assert (
        await runner.main(config=runner.LiveTestConfig(), hmc_config=_live_hmc_config())
        == 1
    )


@pytest.mark.parametrize("transient", [False, True])
def test_matching_gap_is_separate_from_evidence(transient):
    from dataclasses import replace

    state = runner.RunState()
    expected = replace(metrics._PREFERENCES_AUTHORITY, transient=transient)
    for _ in range(2):
        state.record_with_expected(
            5,
            "hmc_get_pcm_preferences",
            "FAIL",
            observation.classify_failure(RuntimeError("HTTP 403")),
            [expected],
        )
    assert state.observations == []
    assert {row["result"] for row in state.results} == {"skipped"}
    assert len(state.gaps) == (0 if transient else 1)
    if not transient:
        assert state.gaps[0]["operation"] == expected.operation
        assert state.gaps[0]["missing_scope"]["variant"] == expected.variant


@pytest.mark.asyncio
@pytest.mark.parametrize("same_operation", [False, True])
async def test_swapped_declared_results_fail_before_client(monkeypatch, same_operation):
    from dataclasses import replace
    from types import ModuleType

    module = ModuleType("swapped_declarations")
    module.A = metrics._PREFERENCES_AUTHORITY
    module.B = (
        replace(module.A, variant="other-pcm")
        if same_operation
        else metrics._PROCESSED_LINKS_AUTHORITY
    )
    second_tool = (
        "hmc_get_pcm_preferences" if same_operation else "hmc_processed_metric_links"
    )
    source = f'''
async def scenario(client, state):
    st_a, data_a = await state.call(client, "hmc_get_pcm_preferences", expected=[A])
    st_b, data_b = await state.call(client, "{second_tool}", expected=[B])
    state.record_with_expected(5, "hmc_get_pcm_preferences", st_a, data_a, [B])
    state.record_with_expected(5, "{second_tool}", st_b, data_b, [A])
'''
    monkeypatch.setattr(runner, "_SCENARIO_MODULES", [module])
    monkeypatch.setattr(runner.inspect, "getsource", lambda _: source)
    monkeypatch.setattr(runner, "create_mcp", lambda *_a: pytest.fail("created client"))
    assert (
        await runner.main(config=runner.LiveTestConfig(), hmc_config=_live_hmc_config())
        == 1
    )


@pytest.mark.parametrize(
    "tail",
    [
        "",
        'st, data = await state.call(client, "hmc_list_users")',
        'state.record_with_expected(5, "pcm", st, None, [A])',
        'state.record_with_expected(5, "pcm", st, data, [])',
        'st = "PASS"\n    state.record_with_expected(5, "pcm", st, data, [A])',
        'data = None\n    state.record_with_expected(5, "pcm", st, data, [A])',
    ],
)
def test_declared_results_require_one_matching_record(tail):
    source = (
        "async def scenario(client, state):\n"
        '    st, data = await state.call(client, "hmc_get_pcm_preferences", expected=[A])\n'
        f"    {tail}\n"
    )
    with pytest.raises(ValueError, match="declared|pair"):
        runner._validate_declared_function(
            ast.parse(source).body[0], {"A": metrics._PREFERENCES_AUTHORITY}
        )


def test_declared_calls_require_assigned_status_and_data():
    source = (
        "async def scenario(client, state):\n"
        '    await state.call(client, "hmc_get_pcm_preferences", expected=[A])\n'
    )
    with pytest.raises(ValueError, match="assigned"):
        runner._validate_declared_function(
            ast.parse(source).body[0], {"A": metrics._PREFERENCES_AUTHORITY}
        )


@pytest.mark.asyncio
async def test_current_gap_skips_call_without_refreshing_confirmation():
    expected = metrics._PREFERENCES_AUTHORITY
    state = runner.RunState(known_gaps={(expected.operation, expected.variant)})
    client = _ScriptedClient(result={})
    status, data = await state.call(
        client, "hmc_get_pcm_preferences", expected=[expected]
    )
    state.record_with_expected(5, "hmc_get_pcm_preferences", status, data, [expected])
    assert client.calls == []
    assert state.results[0]["status"] == "SKIP"
    assert "known gap" in state.results[0]["note"]
    assert state.observations == state.gaps == []


@pytest.mark.asyncio
async def test_invalid_dispatch_precedes_known_gap():
    expected = metrics._PREFERENCES_AUTHORITY
    state = runner.RunState(
        known_gaps={(expected.operation, expected.variant)},
        schemas={"hmc_get_pcm_preferences": {"properties": {}}},
    )
    status, data = await state.call(
        _ScriptedClient(result={}),
        "hmc_get_pcm_preferences",
        expected=[expected],
        invalid_argument="406",
    )
    state.record_with_expected(5, "hmc_get_pcm_preferences", status, data, [expected])
    assert state.results[0]["status"] == "FAIL"
    assert data.exception_type == "InvalidDispatch"
    assert state.observations == state.gaps == []


def test_gap_output_can_be_copied_and_loaded_for_next_run(tmp_path):
    repo = _live_repo(tmp_path)
    expected = metrics._PREFERENCES_AUTHORITY
    state = runner.RunState()
    state.record_with_expected(
        5,
        "hmc_get_pcm_preferences",
        "FAIL",
        observation.classify_failure(RuntimeError("HTTP 403")),
        [expected],
    )
    destination = repo / "test-results-gaps-observations.json"
    assert runner._emit_observations(
        state, destination, ("V10R3", "POWER10"), "(not set)", repo
    )
    emitted = json.loads(destination.read_text())
    assert len(emitted) == 1 and set(emitted[0]) == {"operation", "missing_scope"}
    catalog = repo / "maturity.json"
    catalog.write_text(
        json.dumps(
            {
                "format_version": runner.check_capability_inventory.MATURITY_FORMAT_VERSION,
                "admission_policy": "existing-runtime-guards",
                "operations": [
                    {
                        "operation": expected.operation,
                        "implementation": {
                            "state": "absent",
                            "implemented_scope": [],
                            "missing_scope": [emitted[0]["missing_scope"]],
                        },
                        "evidence": [],
                    }
                ],
            }
        )
    )
    assert runner._load_known_gaps(("V10R3", "POWER10"), repo, catalog) == {
        (expected.operation, expected.variant)
    }
    assert runner._load_known_gaps(("V10R4", "POWER10"), repo, catalog) == set()
    assert runner._load_known_gaps(None, repo, catalog) == set()
    document = json.loads(catalog.read_text())
    scope = document["operations"][0]["implementation"]["missing_scope"][0]
    scope["confirmation"]["observed_at"] = "1970-01-01T00:00:00Z"
    catalog.write_text(json.dumps(document))
    assert runner._load_known_gaps(("V10R3", "POWER10"), repo, catalog) == set()
    del scope["confirmation"]
    catalog.write_text(json.dumps(document))
    assert runner._load_known_gaps(("V10R3", "POWER10"), repo, catalog) == set()


@pytest.mark.parametrize(
    "records", [[None], [{"operation": "unknown.operation"}], "invalid"]
)
def test_invalid_gap_catalog_fails_even_without_environment(tmp_path, records):
    catalog = tmp_path / "maturity.json"
    catalog.write_text(
        json.dumps(
            {
                "format_version": (
                    runner.check_capability_inventory.MATURITY_FORMAT_VERSION
                ),
                "admission_policy": "existing-runtime-guards",
                "operations": records,
            }
        )
    )
    with pytest.raises(ValueError, match="invalid gap catalog"):
        runner._load_known_gaps(None, tmp_path, catalog)


def test_classify_failure_reads_the_message_not_the_traceback():
    """A status quoted by an unrelated frame must not reclassify the failure."""
    try:
        try:
            raise RuntimeError("inner frame mentions HTTP 500")
        except RuntimeError as inner:
            raise RuntimeError("transport returned HTTP 400") from inner
    except RuntimeError as exc:
        failure = observation.classify_failure(exc)

    assert failure.http_status == 400
    assert "HTTP 500" in failure.traceback_text


@pytest.mark.parametrize(
    "status,data,expected_status,note_contains",
    [
        (
            "FAIL",
            {"steps": [{"step": "apply_profile", "status": "error"}]},
            "FAIL",
            None,
        ),
        ("PASS", "not a dict", "PASS", None),
        ("PASS", {"lpar": {"UUID": "u"}}, "PASS", None),
        (
            "PASS",
            {"workflow_completed": False, "steps": []},
            "FAIL",
            "workflow_completed is false",
        ),
        (
            "PASS",
            {
                "workflow_completed": False,
                "steps": [
                    {"step": "create", "status": "ok"},
                    {"step": "apply_profile", "status": "error", "result": "boom"},
                ],
            },
            "FAIL",
            "apply_profile failed: boom",
        ),
        (
            "PASS",
            {
                "workflow_completed": False,
                "steps": [{"step": "assign[0]", "status": "error"}],
            },
            "FAIL",
            "assign[0] failed",
        ),
    ],
)
def test_judge_create_result_reads_steps_not_call_status(
    status, data, expected_status, note_contains
):
    """A failed step or workflow_completed=False downgrades PASS to FAIL (#997)."""
    result_status, note = observation.judge_create_result(status, data)
    assert result_status == expected_status
    if note_contains is None:
        assert note == ""
    else:
        assert note_contains in note


async def _served_result(tool: str, payload: dict[str, Any]) -> Any:
    """*payload* typed the way the live client hands back *tool*'s served result."""
    async with runner.served_client() as client:
        (schema,) = [
            t.output_schema for t in await client.list_tools() if t.name == tool
        ]
    if schema.get("x-fastmcp-wrap-result"):
        schema = schema["properties"]["result"]
    return TypeAdapter(json_schema_to_type(schema)).validate_python(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,payload,expected_status,note_contains",
    [
        (
            "hmc_create_lpar",
            {
                "resource_created": True,
                "workflow_completed": True,
                "lpar": {"UUID": "u"},
                "ownership_stamped": True,
                "steps": [
                    {"step": "create", "status": "ok"},
                    {"step": "apply_profile", "status": "error", "result": "boom"},
                ],
                "warnings": [],
            },
            "FAIL",
            "apply_profile failed: boom",
        ),
        (
            "hmc_provision_lpar",
            {
                "resource_created": True,
                "workflow_completed": False,
                "lpar_uuid": "u",
                "dry_run": False,
                "ownership_stamped": True,
                "steps": [{"step": "create", "status": "ok"}],
                "warnings": [],
            },
            "FAIL",
            "workflow_completed is false",
        ),
        (
            "hmc_create_lpar",
            {
                "resource_created": True,
                "workflow_completed": True,
                "lpar": {"UUID": "u"},
                "ownership_stamped": True,
                "steps": [{"step": "create", "status": "ok"}],
                "warnings": [],
            },
            "PASS",
            None,
        ),
    ],
)
async def test_judge_create_result_reads_a_served_dataclass_result(
    tool, payload, expected_status, note_contains
):
    """A served typed result is a generated dataclass, judged like a dict (#1369)."""
    data = await _served_result(tool, payload)
    assert dataclasses.is_dataclass(data)

    result_status, note = observation.judge_create_result("PASS", data)

    assert result_status == expected_status
    if note_contains is None:
        assert note == ""
    else:
        assert note_contains in note


@pytest.mark.asyncio
async def test_a_real_access_policy_denial_classifies_as_denied():
    """The denial pattern is coupled to the message the application really renders."""
    policy = compile_legacy_policy(
        TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,), include_arbitrary_command=False
    )
    async with Client(create_mcp(policy)) as client:
        with pytest.raises(ToolError) as raised:
            await client.call_tool("hmc_list_systems", {"profile": "not-granted"})

    assert observation.classify_failure(raised.value).denied is True


def test_a_target_scope_denial_classifies_as_denied():
    """Three of the four target-scope templates omit the ``on <targets>`` segment."""
    rendered = target_scope._UNREADABLE_VALUE.format(
        tool="hmc_get_lpar",
        policy="'legacy-equivalent'",
        argument="lpar_name_or_uuid",
        kind="lpar",
    )

    assert observation.classify_failure(RuntimeError(rendered)).denied is True


def test_expected_outcome_matches_whole_tokens_in_the_message():
    outcome = observation.ExpectedOutcome(
        operation="pcm.get_preferences",
        variant="managed-system-pcm",
        reason="job REST type unsupported",
        error_codes=frozenset({"REST000E"}),
    )

    assert outcome.matches(
        observation.classify_failure(RuntimeError("saw REST000E here"))
    )
    assert not outcome.matches(
        observation.classify_failure(RuntimeError("saw REST000EX here"))
    )


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        (
            "metrics._PREFERENCES_AUTHORITY",
            "HMCError: The connecting user does not have PCM authority (HTTP 403)",
        ),
        ("vmedia._ALREADY_POWERED_OFF", "HMCError: partition is Not Running"),
    ],
)
def test_declared_outcomes_match_the_message_forms_the_hmc_really_renders(
    outcome, message
):
    """The substring match this replaced was case-insensitive; so is this one.

    The HMC renders `Not Acceptable`, `No Such` and `Not Running` in title case,
    and `templates` in the plural. A case-sensitive whole-token pattern misses
    all four, recording a known limitation as a real failure on live hardware —
    a regression no `tmp_path` fixture would show.
    """
    module, _, name = outcome.partition(".")
    declared = getattr(globals()[module], name)

    assert declared.matches(observation.classify_failure(RuntimeError(message)))


def test_a_declared_outcome_does_not_match_an_unrelated_failure():
    assert not metrics._PREFERENCES_AUTHORITY.matches(
        observation.classify_failure(RuntimeError("HMCError: HTTP 500 internal"))
    )


def test_expected_outcome_requires_a_code_or_a_denial():
    with pytest.raises(ValueError, match="error code or a denial"):
        observation.ExpectedOutcome(
            operation="pcm.get_preferences",
            variant="managed-system-pcm",
            reason="nothing to match on",
        )


def test_an_unmatched_failure_is_recorded_as_failed():
    """An undeclared failure is never laundered into a skip."""
    state = runner.RunState()

    state.record_with_expected(
        12,
        "hmc_get_job",
        "FAIL",
        observation.classify_failure(RuntimeError("HTTP 500 Internal Server Error")),
        [
            observation.ExpectedOutcome(
                operation="pcm.get_preferences",
                variant="managed-system-pcm",
                reason="job REST type unsupported",
                error_codes=frozenset({"REST000E"}),
            )
        ],
    )

    assert state.results[0]["status"] == "FAIL"
    assert state.results[0]["result"] == "failed"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("PASS", "observed"), ("FAIL", "failed"), ("SKIP", "skipped")],
)
def test_record_always_yields_a_non_promoting_result(status, expected):
    state = runner.RunState()

    state.record(1, "hmc_get_console_info", status, {"uuid": "c"})

    assert state.results[0]["result"] == expected
    assert state.results[0]["result"] != "passed"


def test_record_verified_yields_failed_on_a_false_assertion():
    state = runner.RunState()

    state.record_verified(
        12,
        "hmc_get_job",
        operation="job.get",
        scenario="st12-job-inspection",
        assertions=[
            observation.Assertion("job-found", True),
            observation.Assertion("job-status-successful", False),
        ],
        cleanup="not-required",
        data={"UUID": "j"},
    )

    assert state.results[0]["result"] == "failed"
    assert state.observations[0]["observation"]["result"] == "failed"
    assert state.observations[0]["observation"]["assertions"] == ["job-found"]


def test_record_verified_yields_failed_on_failed_cleanup():
    state = runner.RunState()

    state.record_verified(
        12,
        "hmc_get_job",
        operation="job.get",
        scenario="st12-job-inspection",
        assertions=[observation.Assertion("job-found", True)],
        cleanup="failed",
        data={"UUID": "j"},
    )

    assert state.results[0]["result"] == "failed"
    assert state.observations[0]["observation"]["cleanup"] == "failed"


def test_record_verified_writes_a_catalog_shaped_observation():
    state = runner.RunState()

    state.record_verified(
        1,
        "hmc_get_console_info",
        operation="console.info",
        scenario="st1-console-identity",
        assertions=[observation.Assertion("console-uuid-present", True)],
        cleanup="not-required",
        data={"uuid": "c"},
    )

    recorded = state.observations[0]
    assert recorded["operation"] == "console.info"
    assert recorded["observation"]["id"] == "st1-hmc-get-console-info"
    assert recorded["observation"]["channel"] == "live"
    assert recorded["observation"]["result"] == "passed"
    assert set(recorded["observation"]) == {
        "id",
        "channel",
        "result",
        "scenario",
        "observed_at",
        "cleanup",
        "assertions",
    }


@pytest.mark.parametrize(
    ("assertions", "cleanup", "scenario", "match"),
    [
        ([], "not-required", "st1-console-identity", "at least one condition"),
        (
            [("console-uuid-present", True)],
            "unknown",
            "st1-console-identity",
            "cleanup disposition",
        ),
        ([("console-uuid-present", True)], "not-required", "console", "scenario id"),
    ],
)
def test_record_verified_rejects_a_malformed_observation(
    assertions, cleanup, scenario, match
):
    state = runner.RunState()

    with pytest.raises(ValueError, match=match):
        state.record_verified(
            1,
            "hmc_get_console_info",
            operation="console.info",
            scenario=scenario,
            assertions=[observation.Assertion(*item) for item in assertions],
            cleanup=cleanup,
            data={},
        )


def test_assertion_id_must_be_a_closed_shape_token():
    with pytest.raises(ValueError, match="closed-shape token"):
        observation.Assertion("entry UUID equals job id", True)


def test_record_keeps_a_dataclass_result_as_a_mapping(capsys):
    """A FleetListing tool result arrives as a generated dataclass (ADR 0197)."""

    @dataclasses.dataclass
    class Listing:
        entries: list
        unreadable_systems: list

    state = runner.RunState()
    state.record(0, "hmc_list_vios", "PASS", Listing([{"UUID": "v-1"}], []))
    state.record(0, "hmc_list_vios", "FAIL", Listing([{"UUID": "v-1"}], []))

    for row in state.results:
        assert row["data"] == {"entries": [{"UUID": "v-1"}], "unreadable_systems": []}


def test_result_helpers_filter_malformed_entries_and_resource_shapes():
    raw_entries = [
        {"Resource": {"UUID": "nested"}},
        "not-a-mapping",
        {"UUID": "flat"},
    ]

    assert results.entries(raw_entries) == [raw_entries[0], raw_entries[2]]
    assert results.entries({"entries": raw_entries}) == [raw_entries[0], raw_entries[2]]
    assert results.entries({"entries": {"UUID": "not-a-list"}}) == []
    assert results.entries("invalid") == []
    # FastMCP hands a dataclass result (ADR 0197's FleetListing) to a client as a
    # generated model rather than a mapping.
    model = SimpleNamespace(entries=raw_entries, unreadable_systems=[])
    assert results.entries(model) == [raw_entries[0], raw_entries[2]]
    assert results.resource(raw_entries[0]) == {"UUID": "nested"}
    assert results.resource({"UUID": "flat"}) == {"UUID": "flat"}
    assert results.resource({"Resource": "not-a-mapping"}) == {
        "Resource": "not-a-mapping"
    }


def test_module_docstring_prescribes_no_sync_and_the_real_subtask_range():
    """The docstring is argparse's description, so a stale one misdirects a run."""
    assert "uv run --no-sync python scripts/live_test_runner.py" in runner.__doc__
    assert not re.search(r"uv run (?!--no-sync)", runner.__doc__)
    assert f"0 through {max(runner.SUBTASKS)}" in runner.__doc__


def test_help_renders_the_docstring_unwrapped_with_every_group(capsys):
    with pytest.raises(SystemExit) as exit_info:
        runner._parse_arguments(["--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    for group in runner.SUBTASK_GROUPS:
        assert group in help_text
    # The default formatter reflows the description into one paragraph, which
    # would swallow the usage line and the --no-sync requirement with it.
    assert "Usage:\n    uv run --no-sync" in help_text


def test_run_provenance_stamps_the_commit_and_a_clean_tree(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]

    block = runner._run_provenance([24], "dedicated", repo_root, "V1_0", False)

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    assert block["tested_commit"] == head.stdout.strip()
    assert block["group"] == "dedicated"
    assert block["subtasks"] == [24]
    assert block["schema_version"] == "V1_0"
    assert isinstance(block["tree_clean"], bool)
    assert datetime.fromisoformat(block["finished"]).tzinfo is not None


def test_run_provenance_outside_a_repository_reports_no_commit():
    """A run that cannot be attributed says so; it does not omit the block."""
    block = runner._run_provenance([0, 1], None, None, "(not set)", True)

    assert block == {
        "tested_commit": None,
        "tree_clean": None,
        "group": None,
        "subtasks": [0, 1],
        "schema_version": "(not set)",
        "finished": block["finished"],
        "partial": True,
    }


def test_run_provenance_reports_a_dirty_tree(tmp_path):
    """A sha naming a tree that was not exercised must not read as attribution."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    for args in (
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-qm", "seed"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    (tmp_path / "scripts" / "thing.py").write_text("x = 2\n", encoding="utf-8")

    block = runner._run_provenance([24], "dedicated", tmp_path, "(not set)", False)

    assert block["tested_commit"] is not None
    assert block["tree_clean"] is False


def _live_hmc_config() -> HMCConfig:
    return HMCConfig.from_mapping(
        {"host": "hmc.test", "port": 12443, "user": "operator", "verify_ssl": False}
    )


def _result_document(
    config: runner.LiveTestConfig,
    hmc_config: HMCConfig,
    artifacts: runner.LiveTestArtifacts | None = None,
) -> dict:
    return {
        "config": asdict(config),
        "hmc": runner._hmc_identity(hmc_config),
        "artifacts": asdict(artifacts or runner.LiveTestArtifacts()),
        "results": [{"status": "PASS"}],
    }


def test_restore_artifacts_round_trips_config_and_preserves_result_rows(tmp_path):
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    artifacts = runner.LiveTestArtifacts(
        system_uuid="system-1",
        vios_uuid="vios-1",
        lp3_baseline={"description": "original"},
    )
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(_result_document(config, hmc_config, artifacts)))
    state = runner.RunState(config=config)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts.system_uuid == "system-1"
    assert state.artifacts.vios_uuid == "vios-1"
    assert state.artifacts.lp3_baseline == {"description": "original"}
    assert json.loads(results_path.read_text())["results"] == [{"status": "PASS"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_subset_preserves_destination_pending_scratch_recovery(
    monkeypatch, tmp_path, fallback
):
    """A read-only subset cannot erase the only evidence of unconfirmed restoration."""
    import live_test_recovery as recovery

    _isolate_runner(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    artifacts = runner.LiveTestArtifacts(
        vios_uuid="vios-1", storage_volume_group_name="hpvg1234abcd"
    )
    destination = tmp_path / "test-results-storage.json"
    destination.write_text(json.dumps(_result_document(config, hmc_config, artifacts)))
    if fallback:
        (tmp_path / "test-results-vmedia.json").write_text(
            json.dumps(_result_document(config, hmc_config))
        )

    async def read_only(_client, state):
        state.record(3, "hmc_list_volume_groups", "PASS", [])

    monkeypatch.setitem(runner.SUBTASKS, 3, read_only)
    assert (
        await runner.main(3, str(destination), config=config, hmc_config=hmc_config)
        == 0
    )
    saved = json.loads(destination.read_text())
    assert saved["run"]["subtasks"] == [3]
    assert saved["artifacts"]["storage_volume_group_name"] == "hpvg1234abcd"
    inputs = recovery.lpar_inputs_from_document(saved, [3])
    assert inputs is not None and inputs.scratch_vg_ran
    calls = []

    async def no_group(tool, **kwargs):
        calls.append((tool, kwargs))
        return "PASS", []

    finding = await recovery._run_volume_group_left(no_group, inputs)
    assert (
        finding is not None and finding.what == "scratch group restoration unconfirmed"
    )
    assert [tool for tool, _ in calls] == ["hmc_list_volume_groups"]


@pytest.mark.parametrize("mismatch", ["settings", "hmc"])
def test_subset_rejection_preserves_destination_for_recovery(
    monkeypatch, tmp_path, capsys, mismatch
):
    import asyncio

    import live_test_recovery as recovery

    _isolate_runner(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = runner.LiveTestConfig(
        scratch_pv_name="hdisk9", scratch_vg_name="hpvg1234abcd"
    )
    hmc_config = _live_hmc_config()
    document = _result_document(
        config,
        hmc_config,
        runner.LiveTestArtifacts(
            vios_uuid="vios-1", storage_volume_group_name="hpvg1234abcd"
        ),
    )
    document["run"] = {"subtasks": [42]}
    if mismatch == "settings":
        config = dataclasses.replace(config, scratch_pv_name="", scratch_vg_name="")
    else:
        document["hmc"]["user"] = "other"
    destination = tmp_path / "test-results-storage.json"
    destination.write_text(json.dumps(document))
    original = destination.read_bytes()
    dispatched = []

    async def read_only(_client, _state):
        dispatched.append(3)

    monkeypatch.setitem(runner.SUBTASKS, 3, read_only)
    assert (
        asyncio.run(
            runner.main(3, str(destination), config=config, hmc_config=hmc_config)
        )
        == 1
    )
    assert dispatched == []
    assert destination.read_bytes() == original
    capsys.readouterr()
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: False)
    assert recovery.main(["--results", str(destination)]) == 2
    assert "CLEAN" not in capsys.readouterr().out


@pytest.mark.parametrize("existing", [True, False])
def test_default_interruption_then_real_subset_preserves_recovery(
    monkeypatch, tmp_path, capsys, existing
):
    import asyncio

    import live_test_recovery as recovery

    monkeypatch.chdir(tmp_path)
    config = runner.LiveTestConfig(
        system_name="sys-A",
        vdisk_volume_group_name="data-A",
        scratch_pv_name="hdisk9",
        scratch_vg_name="hpvg1234abcd",
    )
    hmc_config = _live_hmc_config()
    document = _result_document(
        config,
        hmc_config,
        runner.LiveTestArtifacts(
            vios_uuid="vios-A",
            vios_partition_id=1,
            storage_volume_group_name="hpvg1234abcd",
        ),
    )
    document["run"] = {"subtasks": [42], "group": "storage", "partial": False}
    destination = tmp_path / "test-results-storage.json"
    if existing:
        destination.write_text(json.dumps(document))
    (tmp_path / "test-results-vmedia.json").write_text(json.dumps(document))
    parsed = runner._parse_arguments(["--results-file", str(destination)])
    assert parsed.subtask is None and parsed.group is None
    interrupted = True
    recovering = False
    calls = []

    class BoundaryClient(Client):
        async def list_tools(self):
            if interrupted:
                raise RuntimeError("schema transport interrupted")
            return await super().list_tools()

        async def call_tool(self, name, arguments=None, **kwargs):
            calls.append(name)
            assert name in {
                "hmc_list_clusters",
                "hmc_list_shared_storage_pools",
                "hmc_list_io_slots",
                "hmc_list_memory_pools",
                "hmc_get_shared_storage_pool",
                "hmc_list_volume_groups",
                "hmc_run_command",
            }
            if name == "hmc_list_volume_groups":
                data = [] if recovering else [{"name": "data-A", "uuid": "vg-A"}]
            elif name == "hmc_run_command":
                assert arguments["cmd"].endswith("-c lsvg")
                data = "data-A\n"
            else:
                data = "" if name == "hmc_get_shared_storage_pool" else []
            return SimpleNamespace(data=data)

    monkeypatch.setattr(runner, "Client", BoundaryClient)
    monkeypatch.setattr(recovery.runner, "Client", BoundaryClient)
    with pytest.raises(RuntimeError, match="schema transport interrupted"):
        asyncio.run(
            runner.main(
                results_path=str(destination), config=config, hmc_config=hmc_config
            )
        )
    partial = json.loads(destination.read_text())
    assert partial["run"]["partial"] is True and calls == []
    marker = "hpvg1234abcd" if existing else None
    assert partial["artifacts"]["storage_volume_group_name"] == marker
    assert partial["artifacts"]["vios_uuid"] == ("vios-A" if existing else None)
    interrupted = False
    assert (
        asyncio.run(
            runner.main(3, str(destination), config=config, hmc_config=hmc_config)
        )
        == 0
    )
    saved = json.loads(destination.read_text())
    assert saved["run"]["subtasks"] == [3] and saved["run"]["partial"] is False
    assert saved["artifacts"]["storage_volume_group_name"] == marker
    capsys.readouterr()
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: True)
    recovering = True
    assert recovery.main(["--results", str(destination)]) == (1 if existing else 0)
    output = capsys.readouterr().out
    if existing:
        assert (
            "scratch group restoration unconfirmed" in output and "CLEAN" not in output
        )
        assert calls[-1] == "hmc_list_volume_groups"
    else:
        assert "CLEAN" in output


@pytest.mark.parametrize("mismatch", ["settings", "hmc", "json", "artifact"])
def test_default_rejection_preserves_destination_before_client(
    monkeypatch, tmp_path, capsys, mismatch
):
    import asyncio

    import live_test_recovery as recovery

    monkeypatch.chdir(tmp_path)
    config = runner.LiveTestConfig(
        scratch_pv_name="hdisk9", scratch_vg_name="hpvg1234abcd"
    )
    hmc_config = _live_hmc_config()
    document = _result_document(
        config,
        hmc_config,
        runner.LiveTestArtifacts(
            vios_uuid="vios-A", storage_volume_group_name="hpvg1234abcd"
        ),
    )
    document["run"] = {"subtasks": [42]}
    if mismatch == "settings":
        config = dataclasses.replace(config, scratch_pv_name="", scratch_vg_name="")
    elif mismatch == "hmc":
        document["hmc"]["user"] = "other"
    elif mismatch == "artifact":
        document["artifacts"]["storage_volume_group_name"] = 42
    destination = tmp_path / "test-results-storage.json"
    destination.write_text("{" if mismatch == "json" else json.dumps(document))
    original = destination.read_bytes()
    entered = []

    def no_client(_server):
        entered.append(True)
        raise AssertionError("incompatible destination reached client setup")

    monkeypatch.setattr(runner, "Client", no_client)
    assert (
        asyncio.run(
            runner.main(
                results_path=str(destination), config=config, hmc_config=hmc_config
            )
        )
        == 1
    )
    assert entered == [] and destination.read_bytes() == original
    capsys.readouterr()
    monkeypatch.setattr(recovery.runner, "_bootstrap_config", lambda: False)
    assert recovery.main(["--results", str(destination)]) == 2
    assert "CLEAN" not in capsys.readouterr().out


def test_restore_artifacts_accepts_a_document_carrying_the_run_block(tmp_path):
    """The runner writes `run`; the guard that reads its own output must admit it."""
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    document["run"] = {"tested_commit": "a" * 40, "tree_clean": True, "subtasks": [24]}
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))
    state = runner.RunState(config=config)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts == runner.LiveTestArtifacts()


def test_restore_drops_the_configured_test_user_from_before_632(tmp_path):
    """A pre-#632 report still restores, and its user UUID is not carried forward.

    That UUID named a configured user, never a users-arm scratch user, so no
    later step may act on it.
    """
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    del document["artifacts"]["test_user_name"]
    document["artifacts"]["test_user_uuid"] = "configured-user-uuid"
    document["config"]["test_user"] = "configured-user"
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))
    state = runner.RunState(config=config)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts.test_user_name is None
    assert not hasattr(state.artifacts, "test_user_uuid")


def test_restore_drops_the_network_test_partition_from_before_1377(tmp_path):
    """A report written while the runner still carried the #1361-retired partition restores."""
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    document["config"]["nettest_name"] = "lpar-A"
    document["artifacts"]["nettest_uuid"] = "nettest-uuid"
    document["artifacts"]["system_uuid"] = "system-uuid"
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))
    state = runner.RunState(config=config)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts == runner.LiveTestArtifacts(system_uuid="system-uuid")


@pytest.mark.parametrize(
    "key",
    [
        "LIVE_TEST_TEST_USER_NAME",  # #632
        "LIVE_TEST_NETWORK_TEST_LPAR_NAME",  # #1377
    ],
)
def test_a_retired_setting_still_loads_with_a_notice(tmp_path, capsys, key):
    """A retired setting in an existing .env must not stop every arm."""
    example = Path(__file__).parents[1] / ".env.example"
    env = tmp_path / ".env"
    env.write_text(example.read_text() + f"{key}=someone\n")

    config = runner.LiveTestConfig.from_env_file(env)

    assert config == runner.LiveTestConfig()
    assert f"{key} (line" in capsys.readouterr().out


def test_users_group_is_opt_in():
    """ST11 creates an HMC user, so round2 no longer dispatches it (#632)."""
    assert 11 not in runner.SUBTASK_GROUPS["round2"]
    assert runner.SUBTASK_GROUPS["users"] == [11]


def test_restore_artifacts_tolerates_a_document_from_before_the_vios_backup_arm(
    tmp_path,
):
    """A report written before #1349 added the ST37 fields is still a restore source."""
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    for name in runner._VIOS_BACKUP_ARTIFACTS:
        del document["artifacts"][name]
    document["artifacts"]["vios_uuid"] = "vios-1"
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))
    state = runner.RunState(config=config)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts.vios_uuid == "vios-1"
    assert state.artifacts.vios_backup_name is None


def test_restore_artifacts_tolerates_a_document_from_before_the_vmedia_artifacts(
    tmp_path,
):
    """A report written before #1347 tracked the repository group still restores."""
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    for name in runner._VMEDIA_REPOSITORY_ARTIFACTS:
        del document["artifacts"][name]
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))
    state = runner.RunState(config=config)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts.vmedia_vg_uuid is None
    assert state.artifacts.vmedia_blank_name is None


def test_decode_artifacts_refuses_a_non_string_vmedia_field():
    artifacts = asdict(runner.LiveTestArtifacts())
    artifacts["vmedia_blank_name"] = 3

    with pytest.raises(TypeError, match="vmedia_blank_name"):
        runner._decode_artifacts(artifacts)


def test_decode_artifacts_refuses_a_non_string_vios_backup_field():
    artifacts = asdict(runner.LiveTestArtifacts())
    artifacts["vios_backup_mapping"] = 3

    with pytest.raises(TypeError, match="vios_backup_mapping"):
        runner._decode_artifacts(artifacts)


def test_restore_artifacts_tolerates_a_config_with_the_removed_vios_slot_settings(
    tmp_path,
):
    """A report written before #1030 dropped the dry-run VIOS settings still restores."""
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    document["config"]["dry_run_vios_slot"] = 17
    document["config"]["dry_run_vios_partition_id"] = 307
    document["artifacts"]["vios_uuid"] = "vios-1"
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))
    state = runner.RunState(config=config)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts.vios_uuid == "vios-1"


@pytest.mark.parametrize("document", ["not JSON", "[]", '{"context": []}'])
def test_restore_artifacts_reports_expected_results_file_failures(
    tmp_path, capsys, document
):
    results_path = tmp_path / "previous.json"
    results_path.write_text(document)

    runner._restore_artifacts_from_results(
        runner.RunState(), _live_hmc_config(), str(results_path)
    )

    assert "Could not restore artifacts" in capsys.readouterr().out


def test_restore_artifacts_rejects_wrong_types_without_partial_mutation(
    tmp_path, capsys
):
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    document["artifacts"]["system_uuid"] = "would-be-installed"
    document["artifacts"]["vios_partition_id"] = True
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))
    original = runner.LiveTestArtifacts(system_uuid="original")
    state = runner.RunState(config=config, artifacts=original)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts is original
    assert state.artifacts.system_uuid == "original"
    assert "must be an integer or null" in capsys.readouterr().out


def test_restore_artifacts_rejects_config_mismatch_without_mutation(tmp_path, capsys):
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    document["config"]["system_name"] = "other-system"
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))
    original = runner.LiveTestArtifacts(system_uuid="original")
    state = runner.RunState(config=config, artifacts=original)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts is original
    assert "configuration does not match this run" in capsys.readouterr().out


def test_restore_artifacts_rejects_unknown_fields_without_mutation(tmp_path, capsys):
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    document["artifacts"]["unknown"] = "value"
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))
    original = runner.LiveTestArtifacts(system_uuid="original")
    state = runner.RunState(config=config, artifacts=original)

    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts is original
    assert "fields do not match" in capsys.readouterr().out


@pytest.mark.parametrize("field", ["host", "port", "user", "verify_ssl"])
def test_restore_artifacts_rejects_each_hmc_identity_mismatch(tmp_path, capsys, field):
    config = runner.LiveTestConfig()
    hmc_config = _live_hmc_config()
    document = _result_document(config, hmc_config)
    document["hmc"][field] = {
        "host": "other.test",
        "port": 443,
        "user": "other",
        "verify_ssl": True,
    }[field]
    results_path = tmp_path / "previous.json"
    results_path.write_text(json.dumps(document))

    state = runner.RunState(config=config)
    runner._restore_artifacts_from_results(state, hmc_config, str(results_path))

    assert state.artifacts == runner.LiveTestArtifacts()
    assert "does not match this run" in capsys.readouterr().out


def test_restore_artifacts_propagates_unexpected_restoration_defects(
    tmp_path, monkeypatch
):
    results_path = tmp_path / "previous.json"
    results_path.write_text(
        json.dumps(_result_document(runner.LiveTestConfig(), _live_hmc_config()))
    )
    monkeypatch.setattr(
        runner, "asdict", lambda _context: (_ for _ in ()).throw(RuntimeError("defect"))
    )

    with pytest.raises(RuntimeError, match="defect"):
        runner._restore_artifacts_from_results(
            runner.RunState(), _live_hmc_config(), str(results_path)
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["--unknown"],
        ["--group"],
        ["--results-file"],
        ["10", "--group", "round2"],
        ["999"],
    ],
)
def test_live_runner_rejects_malformed_arguments(argv):
    with pytest.raises(SystemExit) as error:
        runner._parse_arguments(argv)

    assert error.value.code == 2


def test_live_runner_rejects_arguments_before_bootstrap(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_bootstrap_config",
        lambda: (_ for _ in ()).throw(AssertionError("bootstrap reached")),
    )

    with pytest.raises(SystemExit) as error:
        runner._run_from_arguments(["--unknown"])

    assert error.value.code == 2


def test_live_runner_parses_selection_and_result_defaults():
    assert runner._parse_arguments([]) == runner.RunnerArguments(
        subtask=None,
        group=None,
        results_path="test-results-round2.json",
    )
    assert runner._parse_arguments(["10"]) == runner.RunnerArguments(
        subtask=10,
        group=None,
        results_path="test-results-round2.json",
    )
    assert runner._parse_arguments(["--group", "vmedia"]) == runner.RunnerArguments(
        subtask=None,
        group="vmedia",
        results_path="test-results-vmedia.json",
    )
    assert runner._parse_arguments(
        ["--group", "all", "--results-file", "custom.json"]
    ) == runner.RunnerArguments(
        subtask=None,
        group="all",
        results_path="custom.json",
    )


def test_live_config_has_no_mapping_facade():
    config = runner.LiveTestConfig()

    assert not hasattr(config, "__getitem__")
    assert not hasattr(config, "get")


def test_numeric_dispatch_uses_intent_revealing_workflow_names():
    assert runner.SUBTASKS[0] is runner.capture_lpar_baseline
    assert runner.SUBTASKS[2] is runner.inventory_network
    assert runner.SUBTASKS[9] is runner.mutate_virtual_networking
    assert runner.SUBTASKS[15] is runner.restore_lpar_baseline


@pytest.mark.asyncio
async def test_baseline_capture_runs_cohesive_phases_in_order(monkeypatch):
    events: list[str] = []

    def phase(name):
        async def run(_client, _state):
            events.append(name)

        return run

    monkeypatch.setattr(inventory, "_capture_lpar_properties", phase("properties"))
    monkeypatch.setattr(inventory, "_capture_adapter_topology", phase("adapters"))
    monkeypatch.setattr(inventory, "_capture_vios_identity", phase("vios"))
    monkeypatch.setattr(inventory, "_capture_lpar_cli_dump", phase("cli"))
    monkeypatch.setattr(inventory, "_capture_sync_state", phase("sync"))
    monkeypatch.setattr(
        inventory, "_print_baseline_summary", lambda _state: events.append("summary")
    )

    await inventory.capture_lpar_baseline(object(), runner.RunState())

    assert events == ["properties", "adapters", "vios", "cli", "sync", "summary"]


@pytest.mark.asyncio
async def test_lpar_inventory_calls_all_read_only_affinity_operations(monkeypatch):
    calls = []

    async def scripted_call(_state, _client, tool, **kwargs):
        calls.append((tool, kwargs))
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    await runner.inventory_lpar_profiles(None, state)

    affinity_calls = [
        call
        for call in calls
        if "memopt" in call[0] or "minimum_affinity_policy" in call[0]
    ]
    assert affinity_calls == [
        (
            "hmc_get_lpar_memopt_score",
            {
                "system_name_or_uuid": "example-lt-609-system",
                "lpar_name_or_uuid": "example-lt-609-lpar",
            },
        ),
        (
            "hmc_list_lpar_memopt_scores",
            {"system_name_or_uuid": "example-lt-609-system"},
        ),
        (
            "hmc_get_system_memopt_score",
            {"system_name_or_uuid": "example-lt-609-system"},
        ),
        (
            "hmc_plan_lpar_memopt_scores",
            {"system_name_or_uuid": "example-lt-609-system"},
        ),
        (
            "hmc_plan_system_memopt_score",
            {"system_name_or_uuid": "example-lt-609-system"},
        ),
        (
            "hmc_list_resource_group_memopt_scores",
            {"system_name_or_uuid": "example-lt-609-system"},
        ),
        (
            "hmc_plan_resource_group_memopt_scores",
            {"system_name_or_uuid": "example-lt-609-system"},
        ),
        (
            "hmc_get_minimum_affinity_policy",
            {
                "system_name_or_uuid": "example-lt-609-system",
                "lpar_name_or_uuid": "example-lt-609-lpar",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_affinity_live_paths_use_only_current_and_calcscore_commands(monkeypatch):
    commands = []
    outputs = iter(
        [
            "curr_sys_score=70",
            "lpar_name=web,lpar_id=3,curr_lpar_score=60,predicted_lpar_score=80",
            "curr_sys_score=70,predicted_sys_score=85",
        ]
    )

    async def capture(_config, command):
        commands.append(command)
        return next(outputs)

    monkeypatch.setattr(ssh_affinity, "run_hmc_command", capture)
    config = HMCConfig(host="h", user="u")
    await ssh_affinity.get_system_memopt_score(config, "sys1")
    await ssh_affinity.plan_lpar_memopt_scores(config, "sys1")
    await ssh_affinity.plan_system_memopt_score(config, "sys1")

    assert commands == [
        "lsmemopt -m sys1 -r sys -o currscore",
        "lsmemopt -m sys1 -r lpar -o calcscore",
        "lsmemopt -m sys1 -r sys -o calcscore",
    ]


def test_live_runner_contains_no_executable_optmem_command():
    source = _RUNNER_PATH.read_text(encoding="utf-8")
    assert re.search(r"(?<![\w-])optmem(?![\w-])", source) is None


def _dispatch_sites(source: str) -> list[scenario_gap_report.DispatchSite]:
    """Every ``call`` dispatch in ``source``, from the gap report's enumerator.

    Each site pairs its keyword names with the expression nodes supplying them, so
    a caller can check the *type* a site passes and not only the name. A site the
    enumerator marks unreadable is kept, never skipped: skipping it would silently
    shrink the guard's coverage.
    """
    return scenario_gap_report.dispatch_sites(ast.parse(source))


def _dispatched_tool_names(source: str) -> set[str]:
    """Every tool name ``source`` hands to the runner's ``call`` dispatcher."""
    sites = _dispatch_sites(source)
    unreadable = [f"line {site.lineno}" for site in sites if site.tool is None]
    assert unreadable == [], (
        f"{', '.join(unreadable)}: call() dispatches a tool name this guard "
        "cannot read — pass a string literal"
    )
    return {site.tool for site in sites if site.tool is not None}


#: The holder a dispatch site reads statically-typed arguments from. A default
#: instance is enough: the guard compares the *type* a field carries, and a
#: config field's default has its declared type because the runner validates
#: every one at startup. Run artifacts are deliberately absent — they default to
#: None and are filled in mid-run, so their defaults describe no dispatch.
_ARGUMENT_SOURCES = {"config": runner.LiveTestConfig()}

_UNRESOLVED = runner.UNRESOLVED_ARGUMENT

#: A dispatch site reading a config field that does not exist. It is not an
#: unknowable type, it is a typo that would `AttributeError` against live
#: hardware, so it is reported rather than passed over.
_NO_SUCH_FIELD = object()

#: A conversion whose result type is known even though its argument is not.
#: `str(whatever)` is a `str`; that is the whole question the guard asks, and
#: it is the spelling every converted dispatch site uses.
_CONVERSIONS: dict[str, object] = {"str": "", "int": 0, "float": 0.0, "bool": False}


def _resolved_argument(node: ast.expr) -> object:
    """The value a dispatch site passes, or ``_UNRESOLVED`` when it is dynamic.

    Four shapes are statically knowable: a literal; an attribute read off the run
    config (``config.x`` or ``state.config.x``); a builtin conversion such as
    ``str(...)``, whose result type is fixed regardless of its argument; and an
    f-string, which is always a ``str``. Anything else is a local discovered
    mid-run, which carries no static type and is passed over rather than guessed
    at.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ""
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in _CONVERSIONS:
            return _CONVERSIONS[node.func.id]
        return _UNRESOLVED
    if isinstance(node, ast.Attribute):
        holder = node.value
        if (
            isinstance(holder, ast.Attribute)
            and isinstance(holder.value, ast.Name)
            and holder.value.id == "state"
        ):
            # `state.config.x`, but not any chain that merely ends in `.config`:
            # matching those would resolve an unrelated object's field against
            # LiveTestConfig and report a spurious missing-field problem.
            holder = ast.Name(id=holder.attr)
        if isinstance(holder, ast.Name) and holder.id in _ARGUMENT_SOURCES:
            source = _ARGUMENT_SOURCES[holder.id]
            if not hasattr(source, node.attr):
                return _NO_SUCH_FIELD
            return getattr(source, node.attr)
    return _UNRESOLVED


async def _served_schemas() -> dict[str, dict[str, object]]:
    """The input schema the composed application actually serves for each tool."""
    async with runner.served_client() as client:
        return await runner.served_schemas(client)


@pytest.mark.asyncio
async def test_served_client_serves_every_registered_tool():
    """The runner's one composition serves the whole registry, escape hatch included."""
    async with runner.served_client() as client:
        schemas = await runner.served_schemas(client)

    assert set(schemas) == set(TOOL_SECURITY)
    assert all(isinstance(schema, dict) for schema in schemas.values())


def _dispatch_argument_report(
    sources: dict[str, str], schemas
) -> tuple[list[str], int, int]:
    """Every disagreement with the served schema, plus how much was actually read.

    The counts are the guard's own coverage. Without them a change that stops
    resolving a whole class of arguments — turning checked dispatches into
    unchecked ones — reads exactly like a clean run.
    """
    problems: list[str] = []
    checked = total = 0
    for path, source in sources.items():
        for site in _dispatch_sites(source):
            lineno, tool = site.lineno, site.tool
            if site.unreadable is not None or tool is None:
                problems.append(
                    f"{path}:{lineno} a dispatch this guard cannot read: "
                    f"{site.unreadable}"
                )
                continue
            supplied: dict[str, object] = {}
            for name, node in site.arguments:
                if name is None:
                    continue
                total += 1
                value = _resolved_argument(node)
                if value is _NO_SUCH_FIELD:
                    problems.append(
                        f"{path}:{lineno} {tool}: {name} reads a field that does "
                        "not exist on the run config"
                    )
                    continue
                if value is not _UNRESOLVED:
                    checked += 1
                supplied[name] = value
            problems += [
                f"{path}:{lineno} {problem}"
                for problem in runner._dispatch_problems(tool, supplied, schemas)
            ]
    return problems, checked, total


def _assert_dispatch_arguments(sources: dict[str, str], schemas) -> None:
    """Fail naming every dispatch whose arguments disagree with the served schema."""
    problems, _, _ = _dispatch_argument_report(sources, schemas)
    assert problems == [], "\n".join(problems)


def test_every_dispatched_tool_name_is_registered():
    """The runner is a mirror of the tool registry; a removed tool must not linger."""
    dispatched = set().union(
        *(
            _dispatched_tool_names(Path(module.__file__).read_text(encoding="utf-8"))
            for module in LIVE_WORKFLOW_MODULES
        )
    )

    assert dispatched, "no dispatches found — the guard would pass vacuously"
    assert sorted(dispatched - set(TOOL_SECURITY)) == []


def test_dispatch_guard_reports_a_tool_missing_from_the_registry():
    """The guard bites: a dispatch of an unregistered name is reported, not ignored."""
    source = (
        "async def workflow(client):\n"
        '    await state.call(client, "hmc_list_lpars")\n'
        '    await state.call(client, "hmc_list_password_policies")\n'
    )

    assert _dispatched_tool_names(source) - set(TOOL_SECURITY) == {
        "hmc_list_password_policies"
    }


def test_dispatch_guard_refuses_a_tool_name_it_cannot_read():
    source = "async def workflow(client, tool):\n    await state.call(client, tool)\n"

    with pytest.raises(AssertionError, match="cannot read"):
        _dispatched_tool_names(source)


_SRIOV_SCHEMA = {
    "hmc_list_sriov_logical_ports": {
        "properties": {
            "system_name_or_uuid": {"type": "string"},
            "adapter_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "logical_port_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["system_name_or_uuid"],
    },
    "hmc_assign_sriov_logical_port": {
        "properties": {
            "adapter_id": {"type": "string"},
            "capacity_percent": {"type": "number"},
            "ownership_override": {"type": "boolean"},
        },
        "required": [],
    },
}


def test_dispatch_guard_reports_an_argument_of_the_wrong_type():
    """An int where the tool declares a string is the #763 defect, caught statically.

    The name is right and the tool is registered, so the pre-#763 guard passed it
    through and the mismatch only surfaced as a pydantic error against a live HMC.
    """
    problems = runner._dispatch_problems(
        "hmc_list_sriov_logical_ports",
        {"system_name_or_uuid": "sys", "adapter_id": 17, "logical_port_id": 917003},
        _SRIOV_SCHEMA,
    )

    assert sorted(problems) == [
        "hmc_list_sriov_logical_ports: adapter_id expects string or null, got int",
        "hmc_list_sriov_logical_ports: logical_port_id expects string or null, got int",
    ]


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("adapter_id", "17"),
        ("capacity_percent", 7.5),
        ("capacity_percent", 7),  # PEP 484 numeric tower: int satisfies number
        ("ownership_override", True),
    ],
)
def test_dispatch_guard_accepts_an_argument_of_the_declared_type(argument, value):
    assert (
        runner._dispatch_problems(
            "hmc_assign_sriov_logical_port", {argument: value}, _SRIOV_SCHEMA
        )
        == ()
    )


def test_dispatch_guard_rejects_a_bool_where_a_number_is_declared():
    """`bool` is an `int` subclass, so a naive isinstance check would admit it."""
    assert runner._dispatch_problems(
        "hmc_assign_sriov_logical_port", {"capacity_percent": True}, _SRIOV_SCHEMA
    ) == ("hmc_assign_sriov_logical_port: capacity_percent expects number, got bool",)


def test_dispatch_guard_accepts_none_for_an_optional_argument():
    """`str | None` serves as `anyOf[string, null]`; passing None is a valid dispatch."""
    assert (
        runner._dispatch_problems(
            "hmc_list_sriov_logical_ports",
            {"system_name_or_uuid": "sys", "adapter_id": None},
            _SRIOV_SCHEMA,
        )
        == ()
    )


@pytest.mark.parametrize(
    ("property_schema", "unreadable"),
    [
        ({"$ref": "#/$defs/Selector"}, ["$ref"]),
        ({"allOf": [{"type": "string"}]}, ["allOf"]),
        ({"oneOf": [{"type": "string"}]}, ["oneOf"]),
        ({"const": "sriov"}, ["const"]),
        ({"enum": ["sriov", "ded"], "description": "mode"}, ["enum"]),
    ],
)
def test_dispatch_guard_reports_a_schema_shape_it_cannot_read(
    property_schema, unreadable
):
    """A shape the guard cannot read must be loud, not a silent skip.

    None of these is served today, but FastMCP emits `$ref` as soon as a tool
    parameter is annotated with a nested model rather than a scalar. Returning
    "no problem" would drop that argument from type checking with nothing to
    show for it.
    """
    problems = runner._dispatch_problems(
        "hmc_list_sriov_adapters",
        {"adapter_id": 17},
        {"hmc_list_sriov_adapters": {"properties": {"adapter_id": property_schema}}},
    )

    assert problems == (
        (
            "hmc_list_sriov_adapters: adapter_id has a schema shape this guard "
            f"cannot read: {unreadable}"
        ),
    )


@pytest.mark.parametrize("property_schema", [{}, {"description": "anything"}])
def test_dispatch_guard_accepts_a_property_that_constrains_nothing(property_schema):
    """A property with no type and no constraint admits any value; that is not a gap."""
    assert (
        runner._dispatch_problems(
            "hmc_list_sriov_adapters",
            {"adapter_id": 17},
            {
                "hmc_list_sriov_adapters": {
                    "properties": {"adapter_id": property_schema}
                }
            },
        )
        == ()
    )


@pytest.mark.parametrize(
    "property_schema",
    [
        {"type": ["string", "null"]},
        {"anyOf": [{"anyOf": [{"type": "string"}]}, {"type": "null"}]},
    ],
)
def test_dispatch_guard_reads_type_lists_and_nested_any_of(property_schema):
    """Neither shape is served today; both are read rather than passed over."""
    assert runner._dispatch_problems(
        "hmc_list_sriov_adapters",
        {"adapter_id": 17},
        {"hmc_list_sriov_adapters": {"properties": {"adapter_id": property_schema}}},
    ) == ("hmc_list_sriov_adapters: adapter_id expects string or null, got int",)


def test_dispatch_guard_passes_over_a_statically_unknowable_argument():
    """A value discovered mid-run has no static type; the guard must not guess."""
    assert (
        runner._dispatch_problems(
            "hmc_list_sriov_logical_ports",
            {
                "system_name_or_uuid": "sys",
                "adapter_id": runner.UNRESOLVED_ARGUMENT,
            },
            _SRIOV_SCHEMA,
        )
        == ()
    )


def test_static_argument_resolution_reads_config_field_types():
    """`config.x` and `state.config.x` both resolve, so both spellings are guarded."""
    source = (
        "async def workflow(client):\n"
        '    await state.call(client, "hmc_list_sriov_adapters",\n'
        "        adapter_id=config.sriov_adapter_id,\n"
        "        system_name_or_uuid=state.config.system_name,\n"
        "        physical_port_id=discovered_at_runtime,\n"
        '        logical_port_id="917003")\n'
    )

    site, *rest = _dispatch_sites(source)
    resolved = {name: _resolved_argument(node) for name, node in site.arguments}

    assert rest == []
    assert resolved["adapter_id"] == runner.LiveTestConfig().sriov_adapter_id
    assert resolved["system_name_or_uuid"] == runner.LiveTestConfig().system_name
    assert resolved["physical_port_id"] is _UNRESOLVED
    assert resolved["logical_port_id"] == "917003"


@pytest.mark.parametrize(
    ("expression", "expected_type"),
    [
        ("str(config.sriov_adapter_id)", str),
        ("int(discovered)", int),
        ("float(discovered)", float),
        ("bool(discovered)", bool),
        ('f"{config.sriov_adapter_id}"', str),
    ],
)
def test_static_argument_resolution_reads_conversions_and_f_strings(
    expression, expected_type
):
    """A conversion's result type is known even when its argument is not.

    Every site #763 fixed is spelled `str(config.x)`. Passing over an `ast.Call`
    would leave all 23 of them unchecked while the guard still reported a clean
    run — the guard would bite only on a literal revert, never on a wrong
    conversion such as `int(...)` where the tool declares a string.
    """
    source = (
        "async def workflow(client):\n"
        f'    await state.call(client, "hmc_list_sriov_adapters", adapter_id={expression})\n'
    )

    (site,) = _dispatch_sites(source)
    ((_, node),) = site.arguments

    assert type(_resolved_argument(node)) is expected_type


def test_every_sriov_identifier_argument_is_actually_type_checked():
    """The 23 arguments #763 fixed, and #630's 3, must stay resolvable, not just the budget.

    The coverage floor in the schema test is a whole-tree total, so a later change
    could stop resolving every SR-IOV identifier and stay under it by resolving
    something else. These are the arguments the guard was extended for, so they
    are asserted by name.
    """
    source = Path(pcie.__file__).read_text(encoding="utf-8")
    identifiers = {"adapter_id", "physical_port_id", "logical_port_id"}

    passed_over = [
        f"pcie.py:{site.lineno} {site.tool}: {name}"
        for site in _dispatch_sites(source)
        if "sriov" in (site.tool or "")
        for name, node in site.arguments
        if name in identifiers and _resolved_argument(node) is _UNRESOLVED
    ]

    assert passed_over == [], "\n".join(passed_over)
    assert (
        sum(
            name in identifiers
            for site in _dispatch_sites(source)
            if "sriov" in (site.tool or "")
            for name, _ in site.arguments
        )
        == 26
    )


def test_static_argument_resolution_reports_a_config_field_that_does_not_exist():
    """A misspelled config field would `AttributeError` live; it must not read as dynamic."""
    source = (
        "async def workflow(client):\n"
        '    await state.call(client, "hmc_list_sriov_adapters",\n'
        "        adapter_id=config.sriov_adapter_idd)\n"
    )

    (site,) = _dispatch_sites(source)
    ((_, node),) = site.arguments

    assert _resolved_argument(node) is _NO_SUCH_FIELD

    problems, _, _ = _dispatch_argument_report(
        {"pcie.py": source},
        {"hmc_list_sriov_adapters": {"properties": {"adapter_id": {"type": "string"}}}},
    )
    assert problems == [
        (
            "pcie.py:2 hmc_list_sriov_adapters: adapter_id reads a field that "
            "does not exist on the run config"
        )
    ]


def test_argument_guard_fails_a_malformed_positional_dispatch():
    """A `*args` or third positional would raise `TypeError` live; it is not a clean call."""
    source = (
        "async def workflow(client, extra):\n"
        '    await state.call(client, "hmc_get_lpar", *extra)\n'
        '    await state.call(client, "hmc_get_lpar", "lpar-1")\n'
    )

    problems, checked, total = _dispatch_argument_report({"m.py": source}, {})

    reason = "a dispatch this guard cannot read: "
    reason += "dispatch with a * splat or extra positional arguments"
    assert problems == [f"m.py:2 {reason}", f"m.py:3 {reason}"]
    assert (checked, total) == (0, 0)


@pytest.mark.asyncio
async def test_every_dispatched_argument_matches_the_served_schema():
    """A dispatch the served schema rejects is a defect the harness ships blind."""
    schemas = await _served_schemas()
    sources = {
        Path(module.__file__).name: Path(module.__file__).read_text(encoding="utf-8")
        for module in LIVE_WORKFLOW_MODULES
    }
    # The runner itself dispatches nothing today; covering it keeps a dispatch
    # added there from being the one the guard never reads.
    sources[_RUNNER_PATH.name] = _RUNNER_PATH.read_text(encoding="utf-8")

    problems, checked, total = _dispatch_argument_report(sources, schemas)

    assert problems == [], "\n".join(problems)
    # A floor, not the live number, so adding an argument the guard cannot read
    # is allowed while losing a class of arguments it used to read is not. #763
    # converted 23 checked arguments to unchecked ones and every test stayed
    # green; that is precisely what this catches.
    assert checked >= 230, (
        f"the dispatch guard now type-checks only {checked} of {total} arguments; "
        "a resolvable argument shape stopped resolving and the guard is quietly "
        "covering less than it did"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "async def workflow(client, tool):\n    await state.call(client, tool)\n",
        (
            "async def workflow(client):\n"
            '    await state.call(client, "hmc_list_lpars", **overrides)\n'
        ),
        (
            "if True:\n"
            "    async def workflow(client):\n"
            '        await state.call(client, "hmc_list_lpars")\n'
        ),
    ],
    ids=["non-literal-tool", "splat", "outside-top-level-function"],
)
async def test_argument_guard_refuses_a_dispatch_it_cannot_read(source):
    """Each shape the shared enumerator cannot read fails the guard, never drops out."""
    with pytest.raises(AssertionError, match="cannot read"):
        _assert_dispatch_arguments({"synthetic.py": source}, await _served_schemas())


@pytest.mark.asyncio
async def test_argument_guard_reports_an_unknown_keyword():
    source = (
        "async def workflow(client):\n"
        '    await state.call(client, "hmc_get_job", job_uuid="x")\n'
    )

    with pytest.raises(AssertionError) as raised:
        _assert_dispatch_arguments({"synthetic.py": source}, await _served_schemas())

    assert "unknown argument job_uuid" in str(raised.value)
    assert "missing required argument job_id" in str(raised.value)


@pytest.mark.asyncio
async def test_an_invalid_dispatch_never_reaches_the_client():
    class _RefusingClient:
        async def call_tool(self, _tool, _kwargs):
            raise AssertionError("an invalid dispatch reached the client")

    state = runner.RunState()
    state.schemas = await _served_schemas()

    status, data = await state.call(_RefusingClient(), "hmc_get_job", job_uuid="x")
    state.record(12, "hmc_get_job", status, data)

    assert status == "FAIL"
    assert data.exception_type == "InvalidDispatch"
    assert state.results[0]["result"] == "failed"


def test_every_live_workflow_dispatch_has_exactly_client_and_tool_arguments():
    """A duplicated client argument turns a live stage into an immediate TypeError."""
    invalid: list[str] = []
    for module in LIVE_WORKFLOW_MODULES:
        source = Path(module.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "call"
                and len(node.args) != 2
            ):
                invalid.append(f"{Path(module.__file__).name}:{node.lineno}")

    assert invalid == []


@pytest.mark.asyncio
async def test_main_uses_fresh_state_for_repeated_runs(monkeypatch, tmp_path):
    _isolate_runner(monkeypatch)
    seen_states = []
    closed_servers = []
    initial_system_uuids = []

    async def fake_subtask(_client, state):
        seen_states.append(state)
        state.iso_http_server.close = lambda: closed_servers.append(
            state.iso_http_server
        )
        initial_system_uuids.append(state.artifacts.system_uuid)
        state.artifacts.system_uuid = "first-run-only"
        state.record(0, "fake", "PASS", {})

    monkeypatch.setattr(runner, "SUBTASKS", {0: fake_subtask})
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"

    assert (
        await runner.main(results_path=str(first_path), config=runner.LiveTestConfig())
        == 0
    )
    seen_states[0].artifacts.system_uuid = "mutated-after-run"
    assert (
        await runner.main(results_path=str(second_path), config=runner.LiveTestConfig())
        == 0
    )

    assert seen_states[0] is not seen_states[1]
    assert initial_system_uuids == [None, None]
    assert len(seen_states[1].results) == 1
    assert closed_servers == [
        seen_states[0].iso_http_server,
        seen_states[1].iso_http_server,
    ]
    assert (
        json.loads(second_path.read_text())["artifacts"]["system_uuid"]
        == "first-run-only"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_value", "recorded"), [(None, "(not set)"), ("V1_0", "V1_0")]
)
async def test_main_stamps_run_provenance_into_the_results_document(
    monkeypatch, tmp_path, env_value, recorded
):
    """A matrix taken from this document can be dated and its header state read."""
    _isolate_runner(monkeypatch)
    _clear(monkeypatch, "HMC_SCHEMA_VERSION")
    if env_value is not None:
        monkeypatch.setenv("HMC_SCHEMA_VERSION", env_value)

    async def fake_subtask(_client, state):
        state.record(24, "fake", "PASS", {})

    monkeypatch.setattr(runner, "SUBTASKS", {24: fake_subtask})
    emitted: list[str] = []
    monkeypatch.setattr(
        runner,
        "_emit_observations",
        lambda _state, _path, _environment, schema_version, _root: emitted.append(
            schema_version
        ),
    )
    results_path = tmp_path / "results.json"

    assert (
        await runner.main(
            results_path=str(results_path),
            config=runner.LiveTestConfig(),
            environment=("V10R3", "POWER10"),
        )
        == 0
    )

    block = json.loads(results_path.read_text())["run"]
    assert block["subtasks"] == [24]
    assert block["schema_version"] == recorded
    # The observations carry the string the header printed (ADR 0186).
    assert emitted == [recorded]
    assert set(block) == {
        "tested_commit",
        "tree_clean",
        "group",
        "subtasks",
        "schema_version",
        "finished",
        "partial",
    }
    assert block["partial"] is False


@pytest.mark.asyncio
async def test_main_redacts_direct_failure_before_persisting(
    monkeypatch, tmp_path, capsys
):
    _isolate_runner(monkeypatch)
    suffix = "-secret"
    url_credential = "url" + suffix
    credential = "runner" + suffix
    sensitive = (
        f"direct failure password={credential} "
        f"https://operator:{url_credential}@hmc.lab.example.test/api "
        "/home/operator/live-test.toml"
    )

    async def failing_subtask(_client, state):
        state.record(0, "fake", "FAIL", {"detail": sensitive})

    monkeypatch.setattr(runner, "SUBTASKS", {0: failing_subtask})
    results_path = tmp_path / "results.json"

    assert (
        await runner.main(
            results_path=str(results_path), config=runner.LiveTestConfig()
        )
        == 1
    )
    saved = json.loads(results_path.read_text())
    assert saved["results"][0]["status"] == "FAIL"
    assert isinstance(saved["results"][0]["data"], dict)
    output = capsys.readouterr().out
    persisted = json.dumps(saved)
    for value in (
        credential,
        f"operator:{url_credential}",
        "hmc.lab.example.test",
        "/home/operator/live-test.toml",
    ):
        assert value not in output
        assert value not in persisted


@pytest.mark.asyncio
async def test_main_rejects_unknown_numeric_workflow(monkeypatch, tmp_path):
    _isolate_runner(monkeypatch)
    results_path = tmp_path / "unknown.json"

    assert (
        await runner.main(999, str(results_path), config=runner.LiveTestConfig()) == 1
    )

    saved = json.loads(results_path.read_text())
    assert saved["results"][0]["tool"] == "runner"
    assert saved["results"][0]["data"] == "Unknown sub-task 999"


def test_result_write_preserves_existing_report_when_replacement_fails(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "results.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def fail_replace(_temporary, _path):
        raise OSError("replacement failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failed"):
        runner._write_results(path, '{"new": true}')

    assert path.read_text(encoding="utf-8") == '{"old": true}'
    assert not list(tmp_path.glob(".results.json.*.tmp"))


@pytest.mark.asyncio
async def test_connectivity_inventory_forwards_selectors_and_captures_context(
    monkeypatch,
):
    calls = []

    async def scripted_call(_state, _client, tool, *, expected=(), **kwargs):
        calls.append((tool, kwargs))
        responses = {
            "hmc_get_console_info": {"uuid": "console-uuid"},
            "hmc_list_systems": [
                {
                    "UUID": "system-uuid",
                    "Resource": {"SystemName": "example-lt-609-system"},
                }
            ],
            "hmc_get_lpar": {"UUID": "lpar-uuid"},
            "hmc_list_vios": SimpleNamespace(
                entries=[{"UUID": "vios-uuid", "Resource": {"PartitionID": "7"}}],
                unreadable_systems=[],
            ),
        }
        return "PASS", responses.get(tool, {})

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()

    await connectivity.inventory_connectivity(None, state)

    assert [tool for tool, _ in calls] == [
        "hmc_get_console_info",
        "hmc_list_resources",
        "hmc_list_systems",
        "hmc_get_system",
        "hmc_list_lpars",
        "hmc_get_lpar",
        "hmc_list_vios",
        "hmc_capacity_report",
        "hmc_find_placement",
        "hmc_list_resources",
        "hmc_system_summary",
        "hmc_lpar_summary",
        "hmc_list_lpar_ownership",
        "hmc_read_lpar_boot_order",
        "hmc_inspect_lpar",
        "hmc_get_lpar_proc_compat",
        "hmc_inventory",
        "hmc_fleet_health",
        "hmc_plan_lpar",
    ]
    assert calls[1][1] == {"resource_type": "ManagedSystem"}
    assert calls[3][1] == {"system_name_or_uuid": "example-lt-609-system"}
    assert calls[5][1] == {"lpar_name_or_uuid": "example-lt-609-lpar"}
    assert calls[8][1] == {"desired_memory_mib": 3072, "desired_proc_units": 0.5}
    assert calls[9][1] == {"resource_type": "LogicalPartition"}
    assert calls[14][1] == {
        "lpar_name_or_uuid": "example-lt-609-lpar",
        "system_name_or_uuid": "example-lt-609-system",
        "include": ["resources", "rmc", "refcodes"],
    }
    assert calls[15][1] == {
        "system_name_or_uuid": "example-lt-609-system",
        "lpar_name_or_uuid": "example-lt-609-lpar",
    }
    assert calls[16][1] == {"systems": ["example-lt-609-system"]}
    assert calls[18][1] == {
        "name": "example-lt-609-dry-run",
        "adapters": {"port_vlan_id": 1},
        "storage": {"storage_name": "hmcpctl-st1", "capacity_mib": 10240},
        "system_name_or_uuid": "example-lt-609-system",
    }
    assert state.artifacts.console_uuid == "console-uuid"
    assert state.artifacts.system_uuid == "system-uuid"
    assert state.artifacts.lp3_uuid == "lpar-uuid"
    assert state.artifacts.vios_uuid == "vios-uuid"
    assert state.artifacts.vios_partition_id == 7
    assert state.artifacts.job_uuid_sample is None


@pytest.mark.asyncio
async def test_discover_vios_refuses_a_partition_id_neither_str_nor_int(monkeypatch):
    async def scripted_call(_state, _client, tool, *, expected=(), **kwargs):
        return "PASS", {
            "entries": [{"UUID": "vios-uuid", "Resource": {"PartitionID": 7.0}}]
        }

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()

    with pytest.raises(TypeError, match="PartitionID of type float"):
        await connectivity._discover_vios(None, state)

    assert state.artifacts.vios_uuid is None


_ST1_SYSTEM = "example-lt-609-system"
_ST1_LPAR = "example-lt-609-lpar"
_ST1_SYSTEM_UUID = "11111111-2222-3333-4444-555555555555"
_ST1_LPAR_UUID = "66666666-7777-8888-9999-000000000000"
_ST1_PROFILE = "default_profile"
_ST1_NULL_PROPERTY_500 = (
    "HMCError: Managed-system inventory is unavailable (HTTP 500): Nested path "
    "contains null property currentProperty=Uuid "
    "nestedPath=VirtualPersistentMemoryVolume/Uuid/Value/Value"
)


def _st1_capacity_row(**overrides: object) -> SimpleNamespace:
    """One CapacitySummary as FastMCP delivers it: a generated model, not a mapping."""
    row = {
        "system_uuid": _ST1_SYSTEM_UUID,
        "system_name": _ST1_SYSTEM,
        "total_memory_mib": 65536,
        "assigned_memory_mib": 40960,
        "free_memory_mib": 24576,
        "total_proc_units": 8.0,
        "assigned_proc_units": 5.5,
        "free_proc_units": 2.5,
    }
    return SimpleNamespace(**(row | overrides))


def _st1_inventory_system(system_id: str, capacity: str = "ok") -> SimpleNamespace:
    sources = {
        part: SimpleNamespace(status=status)
        for part, status in (
            ("capacity", capacity),
            ("partitions", "ok"),
            ("ownership", "ok"),
        )
    }
    return SimpleNamespace(
        id=system_id, name=_ST1_SYSTEM, sources=SimpleNamespace(**sources)
    )


def _st1_inspection(**statuses: str) -> SimpleNamespace:
    status = {"uuid": _ST1_LPAR_UUID, "storage": "ok", "rmc": "ok", "refcodes": "ok"}
    status |= statuses
    return SimpleNamespace(
        uuid=status["uuid"],
        resources=SimpleNamespace(
            storage_source=SimpleNamespace(status=status["storage"])
        ),
        rmc=SimpleNamespace(source=SimpleNamespace(status=status["rmc"])),
        refcodes=SimpleNamespace(source=SimpleNamespace(status=status["refcodes"])),
    )


def _st1_ownership(**overrides: object) -> SimpleNamespace:
    row = {
        "lpar_name": _ST1_LPAR,
        "description": "[hmcpctl owner:agent-a]",
        "owned": True,
        "owner": "agent-a",
        "unparsed": False,
    }
    return SimpleNamespace(entries=[row | overrides])


def _st1_plan(candidate_uuid: str) -> SimpleNamespace:
    return SimpleNamespace(
        plan_digest="digest",
        selected=SimpleNamespace(system=SimpleNamespace(uuid=candidate_uuid)),
        blockers=[],
        candidates=[
            SimpleNamespace(
                targets=SimpleNamespace(system=SimpleNamespace(uuid=candidate_uuid))
            )
        ],
    )


def _st1_snapshot(**overrides: object) -> dict[str, object]:
    """A captured snapshot, reduced to the fields ST1 asserts on."""
    parts: dict[str, object] = {
        "lpar_uuid": _ST1_LPAR_UUID,
        "lpar_name": _ST1_LPAR,
        "system_uuid": _ST1_SYSTEM_UUID,
        "profile_name": _ST1_PROFILE,
        "native": {"name": _ST1_PROFILE, "lpar_name": _ST1_LPAR},
        "score_lpar": _ST1_LPAR,
    } | overrides
    return {
        "format": "hmcpctl.lpar-snapshot",
        "version": 1,
        "source": {
            "lpar": {"uuid": parts["lpar_uuid"], "name": parts["lpar_name"]},
            "system": {"uuid": parts["system_uuid"]},
        },
        "configuration": {
            "profile_name": parts["profile_name"],
            "native": {"data": parts["native"]},
        },
        "observations": {
            "scores": {
                "data": {"current": {"lpar": {"lpar_name": parts["score_lpar"]}}}
            }
        },
    }


def _st1_responses() -> dict[str, object]:
    """Conforming ST1 results, shaped as the served tools return them."""
    system = {"UUID": _ST1_SYSTEM_UUID, "Resource": {"SystemName": _ST1_SYSTEM}}
    return {
        "hmc_get_console_info": {"UUID": "console-uuid"},
        "hmc_list_resources": [system],
        "hmc_list_systems": [system],
        "hmc_get_system": {
            "UUID": _ST1_SYSTEM_UUID,
            "Resource": {"SystemName": _ST1_SYSTEM, "State": "operating"},
        },
        "hmc_get_lpar": {"UUID": _ST1_LPAR_UUID},
        "hmc_capacity_report": [_st1_capacity_row()],
        "hmc_find_placement": [_st1_capacity_row()],
        "hmc_list_lpar_ownership": SimpleNamespace(
            entries=[
                {
                    "lpar_name": _ST1_LPAR,
                    "lpar_uuid": _ST1_LPAR_UUID,
                    "description": "[hmcpctl owner:agent-a]",
                    "owned": True,
                    "owner": "agent-a",
                    "unparsed": False,
                },
                {
                    "lpar_name": "other",
                    "lpar_uuid": "other-uuid",
                    "description": None,
                    "owned": False,
                    "owner": None,
                    "unparsed": False,
                },
            ],
            unreadable_systems=[],
        ),
        "hmc_read_lpar_boot_order": {"lpar_uuid": _ST1_LPAR_UUID},
        "hmc_inspect_lpar": SimpleNamespace(
            uuid=_ST1_LPAR_UUID,
            resources=SimpleNamespace(storage_source=SimpleNamespace(status="ok")),
            rmc=SimpleNamespace(source=SimpleNamespace(status="ok")),
            refcodes=SimpleNamespace(source=SimpleNamespace(status="ok")),
        ),
        "hmc_inventory": SimpleNamespace(
            systems=[_st1_inventory_system(f"c/{_ST1_SYSTEM_UUID}")],
            partitions=[
                SimpleNamespace(name=_ST1_LPAR, system_id=f"c/{_ST1_SYSTEM_UUID}")
            ],
        ),
        "hmc_fleet_health": {"systems": [], "vios": [], "lpars": [], "warnings": []},
        "hmc_get_lpar_proc_compat": {"profile": _ST1_PROFILE},
        "hmc_snapshot_capture": _st1_snapshot(),
        "hmc_plan_lpar": SimpleNamespace(
            plan_digest="digest",
            selected=SimpleNamespace(system=SimpleNamespace(uuid=_ST1_SYSTEM_UUID)),
            blockers=[],
            candidates=[
                SimpleNamespace(
                    targets=SimpleNamespace(
                        system=SimpleNamespace(uuid=_ST1_SYSTEM_UUID.upper())
                    )
                )
            ],
        ),
    }


_ST1_OPERATIONS = {
    "system.list",
    "capacity.report",
    "placement.find",
    "lpar.list_ownership",
    "boot_order.read",
    "lpar.inspect",
    "inventory.logical",
    "health.fleet",
    "lpar.plan",
    "snapshot.capture",
}


async def _run_st1(monkeypatch, overrides: dict[str, tuple[str, object]] | None = None):
    responses = {tool: ("PASS", data) for tool, data in _st1_responses().items()}
    responses |= overrides or {}

    async def scripted_call(_state, _client, tool, *, expected=(), **kwargs):
        if (
            tool == "hmc_list_resources"
            and kwargs["resource_type"] == "LogicalPartition"
        ):
            return "PASS", [{"UUID": _ST1_LPAR_UUID}]
        return responses.get(tool, ("PASS", {}))

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    await connectivity.inventory_connectivity(None, state)
    return state, {
        row["operation"]: row["observation"]
        for row in state.observations
        if row["operation"] in _ST1_OPERATIONS
    }


@pytest.mark.asyncio
async def test_st1_records_each_scoped_read_as_a_passed_observation(monkeypatch):
    state, observed = await _run_st1(monkeypatch)

    assert set(observed) == _ST1_OPERATIONS
    assert {op for op, obs in observed.items() if obs["result"] != "passed"} == set()
    assert observed["system.list"]["assertions"] == [
        "system-list-non-empty",
        "boundary-system-listed",
        "entries-carry-uuid",
        "feed-served-directly",
    ]
    assert observed["lpar.plan"]["scenario"] == "st1-lpar-plan"
    assert state.artifacts.system_uuid == _ST1_SYSTEM_UUID
    assert not state.gaps


@pytest.mark.parametrize(
    ("tool", "data", "operation", "unmet"),
    [
        (
            "hmc_list_systems",
            [{"UUID": "x", "Resource": {"SystemName": "example-lt-609-system-2"}}],
            "system.list",
            {"boundary-system-listed", "feed-served-directly"},
        ),
        (
            "hmc_capacity_report",
            [_st1_capacity_row(assigned_memory_mib=1)],
            "capacity.report",
            {"capacity-figures-consistent"},
        ),
        (
            "hmc_capacity_report",
            [_st1_capacity_row(free_proc_units=9.0, assigned_proc_units=-1.0)],
            "capacity.report",
            {"capacity-figures-consistent"},
        ),
        (
            "hmc_find_placement",
            [_st1_capacity_row(free_memory_mib=1024)],
            "placement.find",
            {"candidates-fit"},
        ),
        (
            "hmc_find_placement",
            [],
            "placement.find",
            {"boundary-candidate-when-it-fits"},
        ),
        (
            "hmc_find_placement",
            [
                _st1_capacity_row(system_name="b", free_memory_mib=9000),
                _st1_capacity_row(system_name="a", free_memory_mib=5000),
            ],
            "placement.find",
            {"candidates-best-fit-first", "boundary-candidate-when-it-fits"},
        ),
        (
            "hmc_list_lpar_ownership",
            SimpleNamespace(
                entries=[
                    {
                        "lpar_name": _ST1_LPAR,
                        "owned": True,
                        "owner": None,
                        "unparsed": False,
                    }
                ]
            ),
            "lpar.list_ownership",
            {"ownership-facts-consistent"},
        ),
        (
            "hmc_read_lpar_boot_order",
            {"lpar_uuid": "another-uuid"},
            "boot_order.read",
            {"boot-order-names-partition"},
        ),
        (
            "hmc_inspect_lpar",
            SimpleNamespace(
                uuid=_ST1_LPAR_UUID,
                resources=SimpleNamespace(
                    storage_source=SimpleNamespace(status="unavailable")
                ),
                rmc=SimpleNamespace(source=SimpleNamespace(status="ok")),
                refcodes=SimpleNamespace(source=SimpleNamespace(status="denied")),
            ),
            "lpar.inspect",
            {"resources-read", "refcodes-read"},
        ),
        (
            "hmc_list_systems",
            [{"Resource": {"SystemName": _ST1_SYSTEM}}],
            "system.list",
            {"entries-carry-uuid", "feed-served-directly"},
        ),
        (
            "hmc_find_placement",
            [_st1_capacity_row(free_proc_units=0.25, assigned_proc_units=7.75)],
            "placement.find",
            {"candidates-fit"},
        ),
        (
            "hmc_list_lpar_ownership",
            _st1_ownership(lpar_name="other"),
            "lpar.list_ownership",
            {"test-partition-listed"},
        ),
        (
            "hmc_list_lpar_ownership",
            _st1_ownership(unparsed=True),
            "lpar.list_ownership",
            {"ownership-facts-consistent"},
        ),
        (
            "hmc_list_lpar_ownership",
            _st1_ownership(owned=False, owner=None, unparsed=True, description=None),
            "lpar.list_ownership",
            {"ownership-facts-consistent"},
        ),
        (
            "hmc_inspect_lpar",
            _st1_inspection(uuid="another-uuid"),
            "lpar.inspect",
            {"inspection-names-partition"},
        ),
        (
            "hmc_inspect_lpar",
            _st1_inspection(rmc="unavailable"),
            "lpar.inspect",
            {"rmc-read"},
        ),
        (
            "hmc_inspect_lpar",
            _st1_inspection(refcodes="denied"),
            "lpar.inspect",
            {"refcodes-read"},
        ),
        (
            "hmc_plan_lpar",
            _st1_plan("99999999-0000-0000-0000-000000000000"),
            "lpar.plan",
            {"plan-targets-boundary-system"},
        ),
        (
            "hmc_inventory",
            SimpleNamespace(
                systems=[_st1_inventory_system("c/s", capacity="denied")],
                partitions=[SimpleNamespace(name=_ST1_LPAR, system_id="c/s")],
            ),
            "inventory.logical",
            {"system-sources-read"},
        ),
        (
            "hmc_inventory",
            SimpleNamespace(
                systems=[_st1_inventory_system("c/s")],
                partitions=[SimpleNamespace(name="other", system_id="c/elsewhere")],
            ),
            "inventory.logical",
            {"test-partition-listed", "partitions-belong-to-system"},
        ),
        (
            "hmc_fleet_health",
            {
                "systems": [{"uuid": _ST1_SYSTEM_UUID}],
                "vios": [],
                "lpars": [],
                "warnings": [],
            },
            "health.fleet",
            {"boundary-system-flag-matches-state"},
        ),
        (
            "hmc_fleet_health",
            {"systems": [], "vios": []},
            "health.fleet",
            {"health-sections-present"},
        ),
        (
            "hmc_plan_lpar",
            SimpleNamespace(
                plan_digest="digest",
                selected=None,
                blockers=[SimpleNamespace(code="no_candidate")],
                candidates=[],
            ),
            "lpar.plan",
            {"plan-targets-boundary-system", "plan-outcome-consistent"},
        ),
        (
            "hmc_snapshot_capture",
            _st1_snapshot(lpar_uuid="another-uuid"),
            "snapshot.capture",
            {"snapshot-names-partition"},
        ),
        (
            "hmc_snapshot_capture",
            _st1_snapshot(lpar_name="other"),
            "snapshot.capture",
            {"snapshot-names-partition"},
        ),
        (
            "hmc_snapshot_capture",
            _st1_snapshot(system_uuid="another-uuid"),
            "snapshot.capture",
            {"snapshot-names-system"},
        ),
        (
            "hmc_snapshot_capture",
            _st1_snapshot(profile_name="other_profile"),
            "snapshot.capture",
            {"profile-captured"},
        ),
        (
            "hmc_snapshot_capture",
            _st1_snapshot(native={}),
            "snapshot.capture",
            {"profile-captured"},
        ),
        (
            "hmc_snapshot_capture",
            _st1_snapshot(score_lpar="other"),
            "snapshot.capture",
            {"scores-name-partition"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_st1_assertions_fail_on_violating_results(
    monkeypatch, tool, data, operation, unmet
):
    _, observed = await _run_st1(monkeypatch, {tool: ("PASS", data)})

    observation = observed[operation]
    assert observation["result"] == "failed"
    held = set(observation["assertions"])
    assert unmet.isdisjoint(held)
    others = {op for op, obs in observed.items() if obs["result"] != "passed"}
    assert others == {operation}


@pytest.mark.asyncio
async def test_st1_captures_the_partition_default_profile(monkeypatch):
    calls: list[tuple[str, dict[str, object]]] = []
    responses = {tool: ("PASS", data) for tool, data in _st1_responses().items()}

    async def scripted_call(_state, _client, tool, *, expected=(), **kwargs):
        calls.append((tool, kwargs))
        if (
            tool == "hmc_list_resources"
            and kwargs["resource_type"] == "LogicalPartition"
        ):
            return "PASS", [{"UUID": _ST1_LPAR_UUID}]
        return responses.get(tool, ("PASS", {}))

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    await connectivity.inventory_connectivity(None, runner.RunState())

    captures = [kwargs for tool, kwargs in calls if tool == "hmc_snapshot_capture"]
    assert captures == [
        {
            "system_name_or_uuid": _ST1_SYSTEM,
            "lpar_name_or_uuid": _ST1_LPAR,
            "profile_name": _ST1_PROFILE,
        }
    ]


@pytest.mark.parametrize(
    "proc_compat", [("PASS", {"profile": ""}), ("FAIL", _failure("HMCError: refused"))]
)
@pytest.mark.asyncio
async def test_st1_capture_without_a_profile_name_fails_without_promoting(
    monkeypatch, proc_compat
):
    state, observed = await _run_st1(
        monkeypatch, {"hmc_get_lpar_proc_compat": proc_compat}
    )

    assert "snapshot.capture" not in observed
    rows = {row["tool"]: row["status"] for row in state.results}
    assert rows["hmc_snapshot_capture"] == "FAIL"


@pytest.mark.asyncio
async def test_st1_blocked_plan_without_a_digest_still_passes(monkeypatch):
    blocked = SimpleNamespace(
        plan_digest=None,
        selected=None,
        blockers=[SimpleNamespace(code="no_candidate")],
        candidates=[
            SimpleNamespace(
                targets=SimpleNamespace(system=SimpleNamespace(uuid=_ST1_SYSTEM_UUID))
            )
        ],
    )

    _, observed = await _run_st1(monkeypatch, {"hmc_plan_lpar": ("PASS", blocked)})

    assert observed["lpar.plan"]["result"] == "passed"


@pytest.mark.asyncio
async def test_st1_fallback_served_feed_reads_fail_rather_than_promote(monkeypatch):
    """The raw feed 500s, so list, capacity, placement and health answered via ADR 0138."""
    _, observed = await _run_st1(
        monkeypatch, {"hmc_list_resources": ("FAIL", _failure(_ST1_NULL_PROPERTY_500))}
    )

    feed_reads = {"system.list", "capacity.report", "placement.find", "health.fleet"}
    assert {op for op, obs in observed.items() if obs["result"] == "failed"} == (
        feed_reads
    )
    assert all(
        "feed-served-directly" not in observed[op]["assertions"] for op in feed_reads
    )


@pytest.mark.asyncio
async def test_st1_null_property_500_on_a_system_read_fails_closed(monkeypatch):
    """No reachable system 500s on these reads (#1531), so one that does is a FAIL."""
    reads = {
        "hmc_list_systems": "system.list",
        "hmc_capacity_report": "capacity.report",
        "hmc_find_placement": "placement.find",
    }
    dispatched = {}
    responses = {tool: ("PASS", data) for tool, data in _st1_responses().items()}
    responses |= {tool: ("FAIL", _failure(_ST1_NULL_PROPERTY_500)) for tool in reads}

    async def scripted_call(_state, _client, tool, *, expected=(), **kwargs):
        dispatched.setdefault(tool, list(expected))
        if (
            tool == "hmc_list_resources"
            and kwargs["resource_type"] == "LogicalPartition"
        ):
            return "PASS", [{"UUID": _ST1_LPAR_UUID}]
        return responses.get(tool, ("PASS", {}))

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    await connectivity.inventory_connectivity(None, state)

    observed = {row["operation"]: row["observation"] for row in state.observations}
    assert {observed[op]["result"] for op in reads.values()} == {"failed"}
    assert all(dispatched[tool] == [] for tool in reads)
    rows = {row["tool"]: row["status"] for row in state.results}
    assert {rows[tool] for tool in reads} == {"FAIL"}
    assert not state.gaps


@pytest.mark.asyncio
async def test_st1_undeclared_failure_is_a_failed_observation(monkeypatch):
    state, observed = await _run_st1(
        monkeypatch,
        {
            "hmc_list_systems": ("FAIL", _failure("HMCError: Unauthorized (HTTP 401)")),
            "hmc_plan_lpar": ("FAIL", _failure("ToolError: plan refused")),
        },
    )

    assert observed["system.list"]["result"] == "failed"
    assert observed["system.list"]["assertions"] == []
    assert observed["lpar.plan"]["result"] == "failed"
    assert not state.gaps


@pytest.mark.asyncio
async def test_metrics_template_inventory_records_expected_limitation_and_continues(
    monkeypatch,
):
    calls = []

    async def scripted_call(_state, _client, tool, *, expected=(), **kwargs):
        calls.append((tool, kwargs))
        if tool == "hmc_get_pcm_preferences":
            return "PASS", {"LongTermMonitorEnabled": True}
        if tool == "hmc_processed_metric_links":
            return "FAIL", _failure("HMCError: no PCM authority (HTTP 403)")
        return "PASS", []

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    state.artifacts.system_uuid = "sys-uuid"

    await metrics.inspect_metrics_templates(None, state)

    assert [tool for tool, _ in calls] == [
        "hmc_get_pcm_preferences",
        "hmc_processed_metric_links",
        "hmc_aggregated_metric_links",
        "hmc_list_partition_templates",
    ]
    assert calls[0][1] == {
        "category": "ManagedSystem",
        "resource_name_or_uuid": state.config.system_name,
    }
    assert state.artifacts.lp3_baseline["pcm_prefs"] == {"LongTermMonitorEnabled": True}
    assert state.results[1]["status"] == "SKIP"


@pytest.mark.asyncio
async def test_user_inventory_records_a_refusal_as_a_failure(monkeypatch):
    """REST000E was the pre-ADR-0076 HmcUser refusal; on UserProfile it is a defect."""

    async def scripted_call(_state, _client, tool, **kwargs):
        assert "expected" not in kwargs
        return "FAIL", _failure("REST000E: endpoint unavailable")

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    state.artifacts.console_uuid = "console-uuid"

    await users.inventory_users(None, state)

    assert [row["status"] for row in state.results] == ["FAIL"]
    assert state.gaps == []


@pytest.mark.asyncio
async def test_user_inventory_skips_without_a_console_uuid(monkeypatch):
    """Every user tool addresses the console by UUID, so ST1 gates the whole path."""

    async def scripted_call(_state, _client, _tool, **_kwargs):
        raise AssertionError("a user tool ran without a console UUID")

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()

    await users.inventory_users(None, state)

    assert state.results[0]["status"] == "SKIP"


@pytest.mark.asyncio
async def test_cli_escape_hatch_runs_both_bounded_commands_after_failure(monkeypatch):
    calls = []

    async def scripted_call(_state, _client, tool, **kwargs):
        calls.append((tool, kwargs))
        if len(calls) == 1:
            return "FAIL", "first command failed"
        return "PASS", "systems"

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()

    await escape_hatch.exercise_cli_escape_hatch(None, state)

    assert calls == [
        ("hmc_run_command", {"cmd": "lshmc -V"}),
        ("hmc_run_command", {"cmd": "lssyscfg -r sys"}),
    ]
    assert [result["status"] for result in state.results] == ["FAIL", "PASS"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow", "configure", "expected_tool"),
    [
        (
            runner.validate_provisioning_dry_run,
            lambda _context: None,
            "hmc_provision_lpar (dry_run)",
        ),
        (
            runner.exercise_storage_provisioning,
            lambda _context: None,
            "pre-flight check",
        ),
    ],
)
async def test_mutating_workflows_stop_when_inventory_context_is_missing(
    monkeypatch, workflow, configure, expected_tool
):
    calls = []

    async def unexpected_call(_state, _client, tool, **kwargs):
        calls.append((tool, kwargs))
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", unexpected_call)
    state = runner.RunState()
    configure(state.artifacts)

    await workflow(None, state)

    assert calls == []
    matching = [result for result in state.results if result["tool"] == expected_tool]
    assert matching
    assert matching[0]["status"] in {"FAIL", "SKIP"}


@pytest.mark.asyncio
async def test_storage_provisioning_preserves_baseline_resource_sizes():
    state = _ScriptedSriovState([("hmc_provision_lpar", "PASS", {"steps": []})])
    state.artifacts.lp3_baseline = {
        "lpars": {
            "Resource": {
                "MinimumMemory": "1024",
                "DesiredMemory": "2048",
                "MaximumMemory": "4096",
                "DesiredVirtualProcessors": "2",
                "MaximumVirtualProcessors": "4",
            }
        }
    }
    await provisioning._provision_from_baseline(
        None,
        state,
        {
            "name": "fixture",
            "disk": "hp14deadbeef",
            "token": "round2-deadbeef",
            "vios_uuid": "vios-uuid",
            "vg_uuid": "vg-uuid",
        },
    )
    assert state.calls[0][1]["resources"] == {
        "min_memory": 1024,
        "desired_memory": 2048,
        "max_memory": 4096,
        "desired_vcpus": 2,
        "max_vcpus": 4,
    }
    assert state.calls[0][1]["name"] == "fixture"
    assert state.artifacts.lp3_uuid is None


def _listed_networks(*vlans: object) -> list[dict[str, object]]:
    return [{"Resource": {"NetworkVLANID": vlan}} for vlan in vlans]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listing", "reason"),
    [
        (("PASS", _listed_networks(2, 3)), "no virtual network on VLAN 1"),
        (("FAIL", "HMC unavailable"), "hmc_list_virtual_networks returned FAIL"),
        (("PASS", _listed_networks("trunk")), "unparsable VLAN identifiers: 'trunk'"),
    ],
    ids=["unlisted", "listing-failed", "malformed"],
)
async def test_storage_provisioning_refuses_an_unlisted_vlan_before_deleting(
    monkeypatch, listing, reason
):
    """#970: a configured VLAN is not the partition's own, so check it before any delete."""
    calls = []

    async def scripted_call(_state, _client, tool, **kwargs):
        calls.append(tool)
        return listing if tool == "hmc_list_virtual_networks" else ("PASS", {})

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    state.artifacts.vios_uuid = "vios-uuid"
    state.artifacts.vios_partition_id = 7
    state.artifacts.vg_uuid = "vg-uuid"
    state.artifacts.vdisk_vg_name = state.config.vdisk_volume_group_name
    assert state.config.provision_vlan_id == 1

    await runner.exercise_storage_provisioning(None, state)

    assert calls == ["hmc_list_virtual_networks"]
    preflight = next(r for r in state.results if r["tool"] == "pre-flight check")
    assert preflight["status"] == "FAIL"
    assert reason in str(preflight)
    # Only a listing that answered can blame the setting.
    assert ("LIVE_TEST_PROVISION_VLAN_ID" in str(preflight)) == (listing[0] == "PASS")


@pytest.mark.asyncio
async def test_storage_provisioning_refuses_untrusted_volume_group(monkeypatch):
    """A restored vg_uuid not recorded for the configured group writes nothing."""
    calls = []

    async def scripted_call(_state, _client, tool, **kwargs):
        calls.append(tool)
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    state.artifacts.vios_uuid = "vios-uuid"
    state.artifacts.vg_uuid = "first-listed-vg"
    state.artifacts.vdisk_vg_name = ""

    await runner.exercise_storage_provisioning(None, state)

    assert calls == []
    preflight = next(r for r in state.results if r["tool"] == "pre-flight check")
    assert preflight["status"] == "FAIL"
    assert "vg_uuid" in str(preflight)


@pytest.mark.asyncio
async def test_lpar_lifecycle_sequences_create_power_and_cleanup(monkeypatch):
    calls = []

    async def scripted_call(_state, _client, tool, **kwargs):
        calls.append((tool, kwargs))
        if tool == "hmc_run_command":
            return "PASS", "name\n"
        if tool == "hmc_create_lpar":
            return "PASS", {"UUID": "scratch-uuid"}
        if tool in {"hmc_power_on_lpar", "hmc_power_off_lpar"}:
            return "PASS", {"UUID": "job-uuid"}
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    state.artifacts.system_uuid = "system-uuid"

    await runner.exercise_lpar_lifecycle(None, state)

    assert [tool for tool, _ in calls] == [
        "hmc_run_command",
        "hmc_create_lpar",
        "hmc_get_lpar",
        "hmc_modify_lpar",
        "hmc_lpar_summary",
        "hmc_power_on_lpar",
        "hmc_power_off_lpar",
        "hmc_delete_lpar",
        "hmc_list_lpars",
    ]
    assert state.artifacts.scratch_uuid is None
    assert state.artifacts.job_uuid_sample == "job-uuid"


@pytest.mark.parametrize(
    "baseline",
    ["web tier, prod", "owner=alice", "em—dash", "desc\x00bad"],
)
def test_unrestorable_description_names_the_reason(baseline):
    """A baseline the CLI cannot round-trip is reported, not silently retried.

    The runner defers to the server's validator, so the ``-i`` record grammar
    of ADR 0045 refuses the restore before any write is attempted.
    """
    reason = lpar._unrestorable_description(baseline)
    assert isinstance(reason, str) and reason


@pytest.mark.parametrize("baseline", ["", "plain text", "[hmcpctl owner:a created:x]"])
def test_restorable_description_is_not_blocked(baseline):
    """An ordinary baseline description is restored, not refused."""
    assert lpar._unrestorable_description(baseline) is None


def _st14_volumes(state, *names):
    return (
        f"{state.config.vdisk_volume_group_name}:\n"
        "LV NAME TYPE LPs PPs PVs LV STATE MOUNT POINT\n"
        + "".join(f"{name} jfs2 1 1 1 open/syncd N/A\n" for name in names)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [False, True])
async def test_st14_cleanup_uses_scoped_delete_and_independent_exact_inventory(
    monkeypatch, present
):
    state = runner.RunState(
        config=dataclasses.replace(runner.LiveTestConfig(), system_name="sys; reboot")
    )
    state.artifacts.vios_uuid = "unused-stale-uuid"
    state.artifacts.vios_partition_id = 7
    calls = []
    listed = ["hp14deadbeef"] if present else ["hp14deadbeef" + "-other"]

    async def call(_state, _client, tool, **kwargs):
        calls.append((tool, kwargs))
        if tool == "hmc_get_lpar":
            return "FAIL", observation.CallFailure(
                "HMCCLIError", "HSCL8012 partition not found", "", None, False
            )
        if tool == "hmc_get_vios_storage_detail":
            return "PASS", {"Resource": {"VirtualSCSIMappings": {}}}
        if tool == "hmc_run_command":
            return "PASS", _st14_volumes(state, *listed)
        if tool == "hmc_delete_virtual_disk":
            listed.clear()
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", call)
    await provisioning._cleanup_fixture(
        None,
        state,
        {
            "name": "fixture",
            "disk": "hp14deadbeef",
            "token": "round2-deadbeef",
            "vios_uuid": "selected-vios-uuid",
            "vg_uuid": "configured-vg-uuid",
        },
    )
    deletes = [kw for tool, kw in calls if tool == "hmc_delete_virtual_disk"]
    assert deletes == (
        [
            {
                "vios_name_or_uuid": "selected-vios-uuid",
                "system_name_or_uuid": "sys; reboot",
                "vg_uuid": "configured-vg-uuid",
                "disk_name": "hp14deadbeef",
            }
        ]
        if present
        else []
    )
    reads = [kw for tool, kw in calls if tool == "hmc_run_command"]
    assert reads == [
        {
            "cmd": f"viosvrcmd -m 'sys; reboot' --id 7 -c 'lsvg -lv {state.config.vdisk_volume_group_name}'"
        }
    ] * (2 if present else 1)
    assert [
        kw["disk_name"] for tool, kw in calls if tool == "hmc_create_virtual_disk"
    ] == []
    removals = [
        row
        for row in state.results
        if row["tool"] == "hmc_delete_virtual_disk (fixture)"
    ]
    assert [row["status"] for row in removals] == (["PASS"] if present else [])
    assert not state.gaps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "does not exist",
        "not found",
        "No such",
        "0516-306",
        "0516-404",
        "permission denied",
        "transport timeout",
    ],
)
async def test_st14_cleanup_delete_failures_are_fail_and_stop(monkeypatch, failure):
    state = runner.RunState()
    state.artifacts.vios_partition_id = 7
    calls = []

    async def call(_state, _client, tool, **kwargs):
        calls.append(tool)
        if tool == "hmc_get_lpar":
            return "FAIL", observation.CallFailure(
                "HMCCLIError", "HSCL8012 partition not found", "", None, False
            )
        if tool == "hmc_get_vios_storage_detail":
            return "PASS", {"Resource": {"VirtualSCSIMappings": {}}}
        if tool == "hmc_run_command":
            return "PASS", _st14_volumes(state, "hp14deadbeef")
        if tool == "hmc_delete_virtual_disk":
            return "FAIL", observation.classify_failure(RuntimeError(failure))
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", call)
    await provisioning._cleanup_fixture(
        None,
        state,
        {
            "name": "fixture",
            "disk": "hp14deadbeef",
            "token": "round2-deadbeef",
            "vios_uuid": "vios-uuid",
            "vg_uuid": "vg-uuid",
        },
    )
    assert "hmc_create_virtual_disk" not in calls
    removal = next(
        row
        for row in state.results
        if row["tool"] == "hmc_delete_virtual_disk (fixture)"
    )
    assert removal["status"] == "FAIL"
    assert not state.gaps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "read-error",
        "empty",
        "wrong-group",
        "bad-header",
        "bad-row",
        "bad-number",
        "post-read-error",
        "survives",
    ],
)
async def test_st14_cleanup_refuses_unproven_absence(monkeypatch, fault):
    state = runner.RunState()
    state.artifacts.vios_partition_id = 7
    calls = []
    reads = 0

    async def call(_state, _client, tool, **kwargs):
        nonlocal reads
        calls.append(tool)
        if tool == "hmc_get_lpar":
            return "FAIL", observation.CallFailure(
                "HMCCLIError", "HSCL8012 partition not found", "", None, False
            )
        if tool == "hmc_get_vios_storage_detail":
            return "PASS", {"Resource": {"VirtualSCSIMappings": {}}}
        if tool != "hmc_run_command":
            return "PASS", {}
        reads += 1
        data = _st14_volumes(state, "hp14deadbeef")
        if fault == "read-error" or (fault == "post-read-error" and reads == 2):
            return "FAIL", observation.classify_failure(
                RuntimeError("managed system not found")
            )
        if fault == "empty":
            data = ""
        elif fault == "wrong-group":
            data = data.replace(state.config.vdisk_volume_group_name, "other-group")
        elif fault == "bad-header":
            data = data.replace("LV NAME TYPE", "LV NAME")
        elif fault == "bad-row":
            data += "unrecognized diagnostic\n"
        elif fault == "bad-number":
            data = data.replace("1 1 1", "x 1 1")
        return "PASS", data

    monkeypatch.setattr(runner.RunState, "call", call)
    await provisioning._cleanup_fixture(
        None,
        state,
        {
            "name": "fixture",
            "disk": "hp14deadbeef",
            "token": "round2-deadbeef",
            "vios_uuid": "vios-uuid",
            "vg_uuid": "vg-uuid",
        },
    )
    assert "hmc_create_virtual_disk" not in calls
    assert any(row["status"] == "FAIL" for row in state.results)
    assert not any(row["status"] == "SKIP" for row in state.results)
    assert ("hmc_delete_virtual_disk" in calls) == (
        fault in {"post-read-error", "survives"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("vios_id", [None, "7", True, 0, -1])
async def test_st14_cleanup_requires_partition_id_before_destructive_work(
    monkeypatch, vios_id
):
    state = runner.RunState()
    state.artifacts.vios_uuid = "vios-uuid"
    state.artifacts.vg_uuid = "vg-uuid"
    state.artifacts.vdisk_vg_name = state.config.vdisk_volume_group_name
    state.artifacts.vios_partition_id = vios_id
    calls = []

    async def call(_state, _client, tool, **kwargs):
        calls.append(tool)
        if tool == "hmc_list_virtual_networks":
            return "PASS", _listed_networks(state.config.provision_vlan_id)
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", call)
    await runner.exercise_storage_provisioning(None, state)
    assert calls == []
    assert any(row["status"] == "FAIL" for row in state.results)


@pytest.mark.asyncio
async def test_st14_cleanup_failure_stops_dependent_provision(monkeypatch):
    state = runner.RunState()
    state.artifacts.vios_uuid = "vios-uuid"
    state.artifacts.vg_uuid = "vg-uuid"
    state.artifacts.vdisk_vg_name = state.config.vdisk_volume_group_name
    state.artifacts.vios_partition_id = 7
    calls = []

    async def call(_state, _client, tool, **kwargs):
        calls.append(tool)
        if tool == "hmc_list_virtual_networks":
            return "PASS", _listed_networks(state.config.provision_vlan_id)
        if tool == "hmc_run_command":
            return "FAIL", observation.classify_failure(
                RuntimeError("volume group not found")
            )
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", call)
    await runner.exercise_storage_provisioning(None, state)
    assert "hmc_create_virtual_disk" not in calls
    assert "hmc_provision_lpar" not in calls
    assert any(row["status"] == "FAIL" for row in state.results)


@pytest.mark.asyncio
async def test_lpar_property_workflow_refuses_an_unrestorable_description(monkeypatch):
    """A baseline carrying a record delimiter is refused rather than rewritten."""
    calls = []

    async def scripted_call(_state, _client, tool, **kwargs):
        calls.append((tool, kwargs))
        if tool == "hmc_run_command":
            return "PASS", "aixlinux"
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    state.artifacts.lp3_baseline["description"] = "web tier, prod"

    await runner.mutate_lpar_properties(None, state)

    descriptions = [
        kwargs["description"]
        for tool, kwargs in calls
        if tool == "hmc_set_lpar_description"
    ]
    assert descriptions == ["MCP live-test probe R2 safe to clear"]
    restore = [row for row in state.results if row["tool"].endswith("(restore)")]
    assert [row["status"] for row in restore if "description" in row["tool"]] == [
        "FAIL"
    ]


@pytest.mark.asyncio
async def test_lpar_property_workflow_restores_description(monkeypatch):
    calls = []

    async def scripted_call(_state, _client, tool, **kwargs):
        calls.append((tool, kwargs))
        if tool == "hmc_run_command":
            return "PASS", "aixlinux"
        if tool == "hmc_set_lpar_msp":
            return "FAIL", "only valid for a VIOS partition"
        if tool == "hmc_get_proc_compat_modes":
            return "PASS", ["default", "POWER9", "POWER10"]
        if tool == "hmc_get_lpar_proc_compat":
            return "PASS", {"profile": "default_profile", "profile_mode": "default"}
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState(group="profiles")
    state.artifacts.lp3_baseline["description"] = "original description"

    await runner.mutate_lpar_properties(None, state)

    descriptions = [
        kwargs["description"]
        for tool, kwargs in calls
        if tool == "hmc_set_lpar_description"
    ]
    assert descriptions == [
        "MCP live-test probe R2 safe to clear",
        "original description",
    ]
    proc_sets = [
        (kwargs["mode"], kwargs["profile_name"])
        for tool, kwargs in calls
        if tool == "hmc_set_lpar_proc_compat"
    ]
    assert proc_sets == [("POWER10", "default_profile"), ("default", "default_profile")]


@pytest.mark.asyncio
async def test_final_restore_replays_baseline_and_audits(monkeypatch):
    calls = []

    async def scripted_call(_state, _client, tool, **kwargs):
        calls.append((tool, kwargs))
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    state.artifacts.lp3_baseline["description"] = "baseline"
    state.artifacts.lp3_baseline["proc_compat"] = {
        "profile": "default_profile",
        "profile_mode": "POWER9",
    }

    await runner.restore_lpar_baseline(None, state)

    assert (
        next(kwargs for tool, kwargs in calls if tool == "hmc_set_lpar_description")[
            "description"
        ]
        == "baseline"
    )
    assert (
        next(kwargs for tool, kwargs in calls if tool == "hmc_set_lpar_proc_compat")[
            "mode"
        ]
        == "POWER9"
    )
    assert "hmc_sync_lpar_profile" not in [tool for tool, _ in calls]
    assert [tool for tool, _ in calls][-2:] == [
        "hmc_run_command",
        "hmc_lpar_summary",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "response"),
    [
        ("hmc_get_job", {"UUID": "job-uuid", "Status": "FAILED_BEFORE_COMPLETION"}),
        (
            "hmc_wait_for_job",
            {
                "job_id": "job-uuid",
                "found": True,
                "timed_out": False,
                "status": "FAILED_BEFORE_COMPLETION",
                "error": "the job did not complete",
                "job": {"UUID": "job-uuid"},
                "job_href": None,
            },
        ),
    ],
)
async def test_job_scenarios_fail_on_a_non_successful_status(
    monkeypatch, tool, response
):
    """`PASS` meant the call returned; a job that came back failed must not promote."""

    async def scripted_call(_state, _client, dispatched, **_kwargs):
        if dispatched == tool:
            return "PASS", response
        return "PASS", {}

    monkeypatch.setattr(runner.RunState, "call", scripted_call)
    state = runner.RunState()
    state.artifacts.job_uuid_sample = "job-uuid"

    await metrics.inspect_metrics_jobs(None, state)

    row = next(entry for entry in state.results if entry["tool"] == tool)
    assert row["result"] == "failed"
    held = next(
        item["observation"]["assertions"]
        for item in state.observations
        if item["observation"]["id"] == f"st12-{tool.replace('_', '-')}"
    )
    assert "job-status-successful" not in held


@pytest.mark.asyncio
async def test_wait_for_job_outcome_normalizes_from_the_served_shape():
    """FastMCP serves `hmc_wait_for_job` unwrapped, so `result.data` is not a dict.

    The scripted stubs above hand `_as_outcome` a mapping, so only this arm can
    catch a normalizer that handles nothing else.
    """
    application = FastMCP("job-shape-probe")

    @application.tool
    async def probe() -> JobOutcome:
        return JobOutcome(
            job_id="job-uuid",
            status="COMPLETED_OK",
            timed_out=False,
            error=None,
            job={"UUID": "job-uuid"},
            found=True,
            job_href=None,
        )

    async with Client(application) as client:
        result = await client.call_tool("probe", {})

    assert not isinstance(result.data, dict)
    outcome = metrics._as_outcome(result.data)
    assert outcome is not None
    assert outcome.status == "COMPLETED_OK"
    assert outcome.job_id == "job-uuid"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [True, False])
# FastMCP's client cannot enforce the served schema's Decimal pattern and says so;
# the field it concerns is not the one this test reads.
@pytest.mark.filterwarnings(
    "ignore:Pattern .* is not supported by Pydantic:UserWarning"
)
async def test_sriov_changed_reads_the_served_result_shape(changed: bool):
    """The SR-IOV transcripts script mappings; the live client serves a model."""
    application = FastMCP("sriov-shape-probe")

    @application.tool
    async def probe() -> SriovLogicalPortChangeResult:
        return SriovLogicalPortChangeResult(
            operation="unassign",
            path="profile",
            changed=changed,
            selector=InventorySelector("1", "0", "3"),
            effective_before=None,
            effective_after=None,
            profile_before="none",
            profile_after="none",
            output="",
        )

    async with Client(application) as client:
        result = await client.call_tool("probe", {})

    assert not isinstance(result.data, dict)
    assert pcie._sriov_changed(result.data) is changed


def _recorded_scenarios() -> dict[str, set[str]]:
    """Every scenario's declared assertion ids, read from the workflow sources."""
    declared: dict[str, set[str]] = {}
    for module in LIVE_WORKFLOW_MODULES:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name) and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record_verified"
            ):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            scenario = keywords["scenario"]
            if isinstance(scenario, ast.Constant):
                name = scenario.value
            else:
                name = constants[scenario.id]
            declared.setdefault(name, set()).update(
                _assertion_ids(keywords["assertions"], module)
            )
    return declared


def _assertion_ids(node: ast.expr, module) -> set[str]:
    """The ids of the `Assertion(...)` values a `record_verified` call declares."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        # A helper builds the list; read the ids from the helper's own body.
        helper = next(
            item
            for item in ast.parse(
                Path(module.__file__).read_text(encoding="utf-8")
            ).body
            if isinstance(item, ast.FunctionDef) and item.name == node.func.id
        )
        return _assertion_ids_in(helper)
    return _assertion_ids_in(node)


def _assertion_ids_in(node: ast.AST) -> set[str]:
    return {
        item.args[0].value
        for item in ast.walk(node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == "Assertion"
        and item.args
        and isinstance(item.args[0], ast.Constant)
    }


def _recorded_operations() -> set[str]:
    """Every `operation=` literal a `record_verified` call names."""
    declared: set[str] = set()
    for module in LIVE_WORKFLOW_MODULES:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record_verified"
            ):
                continue
            operation = {keyword.arg: keyword.value for keyword in node.keywords}[
                "operation"
            ]
            assert isinstance(operation, ast.Constant), (
                f"{Path(module.__file__).name}:{node.lineno} names an operation "
                "this guard cannot read — pass a string literal"
            )
            declared.add(operation.value)
    return declared


def test_verified_scenarios_name_registered_operations():
    """An observation whose operation is not in the catalog can never be filed.

    `_emit_observations` resolves the closure fingerprint through the registry, so
    a mistyped id yields an observation nothing can fingerprint. Caught here, in
    the pull request, rather than after an expensive run against real hardware.
    """
    registered = {
        tool.operation for tool in runner.check_capability_inventory.discover_registry()
    }
    declared = _recorded_operations()

    assert declared, (
        "no record_verified operations found — the guard would pass vacuously"
    )
    assert sorted(declared - registered) == []


def test_scenarios_declare_their_expected_assertion_ids():
    """Deleting an assertion must fail here, not go unnoticed in a stale observation.

    The closure fingerprint covers `src/hmcpctl/` only, so removing an assertion
    from a harness module leaves every committed observation still listing its id,
    still matching its recomputed hash, and still reported `current` — a reader
    concludes a postcondition was checked that nothing checks any more.
    """
    assert _recorded_scenarios() == {
        "st16-repository-read": {"repository-named", "repository-size-positive"},
        "st18-iso-upload": {"upload-accepted", "media-listed", "reupload-refused"},
        "st19-optical-round-trip": {
            "media-entries-named",
            "create-accepted",
            "media-listed",
            "size-matches",
            "baseline-media-kept",
            "mount-accepted",
            "mapping-listed",
            "baseline-mappings-kept",
            "unmount-accepted",
            "mapping-absent",
            "mappings-equal-baseline",
            "adapters-equal-baseline",
            "refused-while-mounted",
            "delete-accepted",
            "media-absent",
            "media-equal-baseline",
        },
        "st3-storage-inventory": {
            "configured-group-listed",
            "groups-match-vios",
            "no-free-space-diagnostic",
            "clusters-identified",
            "pools-identified",
        },
        "st3-pool-read": {"absent-pool-not-returned", "listed-pool-returned"},
        "st40-disk-lifecycle": {
            "create-accepted",
            "volume-listed",
            "free-space-reduced",
            "baseline-volumes-kept",
            "map-accepted",
            "mapping-listed",
            "baseline-mappings-kept",
            "detach-accepted",
            "mapping-absent",
            "mappings-equal-baseline",
            "adapters-equal-baseline",
            "volume-survives",
            "refused-while-mapped",
            "delete-accepted",
            "volume-absent",
            "volumes-equal-baseline",
            "free-space-restored",
        },
        "st40-attach-disk": {"workflow-completed", "volume-listed", "mapping-listed"},
        "st21-mapping-inventory": {
            "mapping-ids-identified",
            "lpar-scope-subset",
            "optical-backing-agrees",
            "optical-entries-named",
        },
        "st12-job-inspection": {
            "job-found",
            "job-identity-matches",
            "job-status-successful",
        },
        "st5-pcm-template-reads": {
            "five-flags-boolean",
            "links-name-system",
            "document-names-system",
            "samples-present",
            "entries-are-template-summaries",
            "template-identity-matches",
            "read-succeeded",
        },
        "st38-pcm-preference-round-trip": {
            "long-term-monitor-toggled",
            "aggregation-toggled",
            "short-term-monitor-toggled",
            "compute-ltm-toggled",
            "energy-monitor-toggled",
            "long-term-monitor-held-by-aggregation",
            "energy-monitor-held-by-aggregation",
            "snapshot-restored",
        },
        "st4-profile-reads": {
            "description-is-text",
            "msp-is-bool",
            "modes-include-default",
            "current-mode-supported",
            "profile-mode-supported",
            "memory-pools-empty-branch",
            "memory-pool-rows-parsed",
        },
        "st4-affinity-reads": {
            "score-names-partition",
            "score-in-range",
            "partition-listed",
            "scores-in-range",
            "predictions-in-range",
            "prediction-in-range",
            "prediction-not-guaranteed",
            "capability-available-rows",
            "capability-available-policy",
            "capability-unavailable-reason",
        },
        "st10-description-round-trip": {
            "probe-description-read-back",
            "baseline-description-restored",
        },
        "st10-msp-round-trip": {"vios-msp-toggled", "vios-msp-restored"},
        "st10-proc-compat-round-trip": {
            "profile-mode-changed",
            "profile-mode-restored",
        },
        "st10-sync-round-trip": {
            "sync-enable-read-1",
            "sync-disable-read-0",
            "sync-restored-baseline",
        },
        "st10-profile-backup-restore": {
            "backup-accepted",
            "backup-file-restorable",
            "merge-current-wins-accepted",
            "profiles-unchanged-after-merge-current-wins",
            "partitions-unchanged-after-merge-current-wins",
        },
        "st2-network-inventory": {
            "switch-ids-integral",
            "vlan-ids-in-range",
            "bridge-entries-identified",
            "eth-rows-parsed",
            "fc-rows-parsed",
            "fc-port-rows-parsed",
            "group-rows-parsed",
            "adapter-entries-identified",
        },
        "st9-virtual-network-round-trip": {
            "create-accepted",
            "network-listed",
            "duplicate-vlan-refused",
            "delete-accepted",
            "networks-equal-baseline",
        },
        "st9-client-network-adapter": {
            "adapter-added",
            "pvid-matches",
            "delete-accepted",
            "unknown-uuid-refused",
            "adapters-equal-baseline",
        },
        "st9-vscsi-client-adapter": {
            "adapter-added",
            "pairing-matches",
            "slot-collision-refused",
            "adapters-equal-baseline",
            "vios-side-unchanged",
        },
        "st9-vfc-client-adapter": {
            "adapter-added",
            "pairing-matches",
            "slot-collision-refused",
            "adapters-equal-baseline",
            "vios-side-unchanged",
        },
        "st9-fc-port-label": {
            "label-set",
            "unknown-port-refused",
            "labels-equal-baseline",
            "label-removed",
        },
        "st9-vfc-group-label": {
            "group-created",
            "duplicate-refused",
            "group-renamed",
            "group-removed",
            "groups-equal-baseline",
        },
        "st1-console-identity": {"console-uuid-present"},
        "st11-user-reads": {
            "profiles-listed",
            "profiles-carry-uuid-and-user-id",
            "passwords-not-disclosed",
            "task-roles-listed",
            "viewer-role-present",
            "resource-role-rows-named",
            "resource-roles-empty-branch",
            "remote-access-group-read",
            "bind-password-not-disclosed",
        },
        "st11-user-lifecycle": {
            "create-accepted",
            "scratch-profile-listed",
            "password-not-echoed",
            "verify-session-timeout-read-back",
            "user-id-matches",
            "task-role-is-viewer",
            "password-not-disclosed",
            "not-predefined",
            "remote-access-disabled",
            "description-updated",
            "verify-session-timeout-updated",
            "description-cleared",
            "user-id-unchanged",
            "profile-uuid-unchanged",
            "task-role-unchanged",
            "remote-access-unchanged",
            "scratch-profile-absent",
            "pre-existing-profiles-unchanged",
        },
        "st1-system-inventory": {
            "system-list-non-empty",
            "boundary-system-listed",
            "entries-carry-uuid",
            "feed-served-directly",
            "system-uuid-present",
            "system-summary-returned",
        },
        "st1-lpar-inventory": {
            "lpar-list-non-empty",
            "lpar-uuid-present",
            "lpar-summary-returned",
            "ownership-entries-non-empty",
            "test-partition-listed",
            "ownership-facts-consistent",
            "boot-order-names-partition",
            "inspection-names-partition",
            "resources-read",
            "rmc-read",
            "refcodes-read",
        },
        "st1-capacity": {
            "boundary-system-reported",
            "capacity-figures-consistent",
            "feed-served-directly",
            "candidates-fit",
            "candidates-best-fit-first",
            "boundary-candidate-when-it-fits",
        },
        "st1-logical-inventory": {
            "boundary-system-listed",
            "test-partition-listed",
            "partitions-belong-to-system",
            "system-sources-read",
        },
        "st1-fleet-health": {
            "health-sections-present",
            "boundary-system-flag-matches-state",
            "feed-served-directly",
        },
        "st1-lpar-plan": {
            "plan-targets-boundary-system",
            "plan-outcome-consistent",
        },
        "st1-vios-inventory": {
            "vios-list-non-empty",
            "vios-uuid-present",
        },
        "st1-resource-inventory": {"resource-list-non-empty"},
        "st1-lpar-snapshot": {
            "snapshot-names-partition",
            "snapshot-names-system",
            "profile-captured",
            "scores-name-partition",
        },
        "st29-dedicated-pcie": {
            "profile-lists-slot",
            "assign-call-succeeded",
            "remove-command-succeeded",
            "profile-restored-to-baseline",
            "add-command-succeeded",
        },
        "st36-io-slots": {
            "zero-suffix-add-accepted",
            "added-slot-renders-none-pool",
            "other-slots-stable-on-add",
            "zero-suffix-remove-accepted",
            "other-slots-stable-on-remove",
            "required-slot-removed-by-zero-suffix",
            "remaining-slot-stable",
            "remove-command-succeeded",
            "empty-profile-reads-none",
        },
        "st29-pcie-inventory": {
            "slot-rows-identified",
            "owners-normalized",
            "matches-dedicated-inventory",
            "class-filter-exact",
            "capability-available",
            "adapter-rows-parsed",
            "adapter-filter-selects-one",
            "ports-listed",
            "granularity-positive",
            "adapter-required-refused",
            "ports-belong-to-adapter",
            "parents-are-listed-ports",
            "configured-capacity-bounded",
            "vnic-rows-parsed",
        },
        "st23-sriov-logical-port": {
            "assign-call-succeeded",
            "logical-port-configured",
            "owner-is-target-lpar",
            "capacity-matches",
            "unassign-call-succeeded",
            "profile-ports-cleared",
        },
        "st35-bare-cec": {
            "assign-call-succeeded",
            "profile-lists-slot",
            "job-found",
            "job-identity-matches",
            "job-status-successful",
            "refcodes-returned",
            "refcodes-name-the-fixture",
            "unassign-call-succeeded",
            "profile-restored-to-baseline",
            "state-read-returned-a-state",
            "list-call-succeeded",
            "fixture-slot-listed",
            "fixture-slot-unowned",
        },
        "st41-lpar-power": {
            "units-over-vcpus-refused",
            "memory-over-configurable-refused",
            "resources-read-back",
            "ownership-stamped",
            "duplicate-name-refused",
            "profile-activation-reached-firmware",
            "running-reported-without-job",
            "current-configuration-reached-firmware",
            "console-captured",
            "console-released",
            "delayed-shutdown-not-activated",
            "immediate-shutdown-not-activated",
            "start-completed-activated",
            "restart-immediate-completed-activated",
            "stop-immediate-completed-not-activated",
            "repeat-stop-already-in-state",
            "same-request-replays",
            "activated-delete-refused",
            "partition-kept",
            "delete-call-succeeded",
            "lpar-name-absent",
            "workflow-completed",
            "network-adapter-on-vlan",
            "storage-mapping-listed",
            "partition-activated",
            "dedicated-slot-refused",
            "dry-run-inventoried",
            "dry-run-changed-nothing",
            "resource-deleted",
            "storage-mapping-absent",
            "backing-volume-retained",
        },
        "st37-vios-io-backup-restore": {
            "listing-parsed",
            "listing-names-run-backup",
            "backup-accepted",
            "backup-newly-listed",
            "backup-type-viosioconfig",
            "restore-accepted",
            "mapping-restored",
            "baseline-restored",
        },
        "st39-lpar-config": {
            "assignment-workflow-completed",
            "dedicated-profile-read-back",
            "other-profile-slots-unchanged",
            "dedicated-profile-restored",
            "dedicated-slot-unowned",
            "dedicated-baseline-restored",
            "memory-read-back",
            "processing-units-read-back",
            "other-values-unchanged",
            "small-change-read-back",
            "large-change-read-back",
            "no-op-accepted-unchanged",
            "empty-request-refused",
            "renamed-same-uuid",
            "old-name-absent",
            "ownership-stamp-kept",
            "original-name-restored",
            "pending-boot-string-read-back",
            "clear-refused-after-authorization",
            "pending-boot-string-unchanged",
        },
    }


def _live_repo(tmp_path: Path) -> Path:
    """A throwaway git repository whose ignore rules match the real one's."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / ".gitignore").write_text(
        "test-results*.json\n.test-results*.tmp\n", encoding="utf-8"
    )
    package = tmp_path / "src" / "hmcpctl"
    package.mkdir(parents=True)
    # A real module, so the validated `closure_fingerprint` is a hash of files
    # rather than the empty-input digest, which would say nothing about the walk.
    (package / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "placeholder.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.email=t@example.test",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    return tmp_path


def _state_with_one_observation():
    state = runner.RunState()
    state.record_verified(
        1,
        "hmc_get_console_info",
        operation="console.info",
        scenario="st1-console-identity",
        assertions=[observation.Assertion("console-uuid-present", True)],
        cleanup="not-required",
        data={"uuid": "console"},
    )
    return state


def test_a_lone_environment_key_is_rejected(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LIVE_TEST_ENV_HMC_RELEASE=V10R3\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be set together"):
        runner._read_environment(env_file)


def test_both_environment_keys_are_read(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TEST_ENV_HMC_RELEASE=V10R3M1060\nLIVE_TEST_ENV_HARDWARE_FAMILY=POWER10\n",
        encoding="utf-8",
    )

    assert runner._read_environment(env_file) == ("V10R3M1060", "POWER10")


def test_a_release_without_its_maintenance_level_is_rejected(tmp_path):
    """#1335: one HMC must not be recorded as both `V10R3` and `V10R3M1060`.

    The catalog grammar still admits the bare rows recorded before this rule.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LIVE_TEST_ENV_HMC_RELEASE=V10R3\nLIVE_TEST_ENV_HARDWARE_FAMILY=POWER10\n",
        encoding="utf-8",
    )

    assert runner.check_capability_inventory.HMC_RELEASE.fullmatch("V10R3")
    with pytest.raises(ValueError, match=r"expected V<n>R<n>M<n>, e\.g\. V10R3M1060"):
        runner._read_environment(env_file)


def test_the_example_environment_is_accepted():
    """Copying `.env.example` must not fail the release rule at startup."""
    example = Path(__file__).parents[1] / ".env.example"

    assert runner._read_environment(example) is not None


def test_observations_are_not_emitted_without_environment(tmp_path, capsys):
    repo = _live_repo(tmp_path)
    destination = repo / "test-results-round2-observations.json"

    assert not runner._emit_observations(
        _state_with_one_observation(), destination, None, "(not set)", repo
    )
    assert "no LIVE_TEST_ENV_* settings" in capsys.readouterr().out
    assert not destination.exists()


def test_observations_are_not_emitted_from_a_dirty_tree(tmp_path, capsys, monkeypatch):
    repo = _live_repo(tmp_path)
    monkeypatch.setattr(runner, "_tree_is_clean", lambda _root: False)
    destination = repo / "test-results-round2-observations.json"

    assert not runner._emit_observations(
        _state_with_one_observation(),
        destination,
        ("V10R3", "POWER10"),
        "(not set)",
        repo,
    )
    assert "is modified" in capsys.readouterr().out
    assert not destination.exists()


def test_emission_refuses_a_path_git_does_not_ignore(tmp_path, capsys):
    """`--results-file` accepts any stem, so no fixed pattern can cover it."""
    repo = _live_repo(tmp_path)
    destination = repo / "evidence-observations.json"

    assert not runner._emit_observations(
        _state_with_one_observation(),
        destination,
        ("V10R3", "POWER10"),
        "(not set)",
        repo,
    )
    assert "is not ignored by git" in capsys.readouterr().out
    assert not destination.exists()


@pytest.mark.parametrize("schema_version", ["(not set)", "V1_0", "V1_17_0"])
def test_emitted_observations_validate_against_the_catalog_shape(
    tmp_path, schema_version
):
    repo = _live_repo(tmp_path)
    destination = repo / "test-results-round2-observations.json"

    assert runner._emit_observations(
        _state_with_one_observation(),
        destination,
        ("V10R3", "POWER10"),
        schema_version,
        repo,
    )

    document = json.loads(destination.read_text())
    errors: list[str] = []
    # One shared id set across the document, so a duplicate id is caught here
    # exactly as `just capability-inventory` would catch it after a copy-in.
    evidence_ids: set[str] = set()
    for entry in document:
        runner.check_capability_inventory._validate_observation(
            entry["observation"], entry["operation"], evidence_ids, errors
        )
    assert errors == []
    assert document[0]["operation"] == "console.info"
    assert document[0]["observation"]["result"] == "passed"
    assert document[0]["observation"]["schema_version"] == schema_version
    assert (
        document[0]["observation"]["closure_fingerprint"]
        != hashlib.sha256(b"").hexdigest()
    )


def test_emission_refuses_an_unrecordable_schema_version(tmp_path, capsys):
    """The observation's narrow grammar is applied on the way out, too (ADR 0186)."""
    repo = _live_repo(tmp_path)
    destination = repo / "test-results-round2-observations.json"

    assert not runner._emit_observations(
        _state_with_one_observation(),
        destination,
        ("V10R3", "POWER10"),
        "V1_0;x",
        repo,
    )
    assert (
        "HMC_SCHEMA_VERSION is not V<n>_<n>[_<n>...] or unset — observations not written"
        in capsys.readouterr().out
    )
    assert not destination.exists()


@pytest.mark.asyncio
async def test_an_unrecordable_schema_version_is_warned_before_the_run(
    monkeypatch, tmp_path, capsys
):
    """A value no observation can hold is named before the hardware run, which still starts."""
    _isolate_runner(monkeypatch)
    _clear(monkeypatch, "HMC_SCHEMA_VERSION")
    monkeypatch.setenv("HMC_SCHEMA_VERSION", "v1_0")

    async def fake_subtask(_client, state):
        print("subtask ran")
        state.record(24, "fake", "PASS", {})

    monkeypatch.setattr(runner, "SUBTASKS", {24: fake_subtask})
    monkeypatch.setattr(runner, "_emit_observations", lambda *_args: False)

    assert (
        await runner.main(
            results_path=str(tmp_path / "results.json"),
            config=runner.LiveTestConfig(),
            environment=("V10R3", "POWER10"),
        )
        == 0
    )
    out = capsys.readouterr().out
    warning = "HMC_SCHEMA_VERSION is not V<n>_<n>[_<n>...] or unset — this run's"
    assert warning in out
    assert out.index(warning) < out.index("subtask ran")


def test_a_lone_environment_key_exits_before_the_run(monkeypatch, tmp_path, capsys):
    """A one-line `.env` typo costs a startup exit, not a hardware run's output."""
    monkeypatch.setattr(
        runner,
        "_read_environment",
        lambda *_a: (_ for _ in ()).throw(ValueError("lone key")),
    )
    monkeypatch.setattr(
        runner.LiveTestConfig,
        "from_env_file",
        classmethod(lambda _cls: runner.LiveTestConfig()),
    )
    monkeypatch.setattr(
        runner, "create_mcp", lambda *_a, **_k: pytest.fail("created MCP")
    )

    assert runner._run_from_arguments([]) == 1
    assert "lone key" in capsys.readouterr().out


def test_an_invalid_dispatch_is_never_laundered_into_a_skip():
    """The harness's own defect must not be recorded as a known HMC limitation.

    An `InvalidDispatch` message names the offending argument, so it can contain
    a token a declared `ExpectedOutcome` matches; consulting declarations first
    would turn the substitution the old substring match allowed back on.
    """
    state = runner.RunState()

    state.record_with_expected(
        12,
        "hmc_get_job",
        "FAIL",
        observation.CallFailure(
            "InvalidDispatch", "hmc_get_job: unknown argument 406", "", None, False
        ),
        [
            observation.ExpectedOutcome(
                operation="pcm.get_preferences",
                variant="managed-system-pcm",
                reason="not licensed",
                error_codes=frozenset({"406"}),
            )
        ],
    )

    assert state.results[0]["status"] == "FAIL"
    assert state.results[0]["result"] == "failed"


def test_emission_skips_an_unknown_operation_and_keeps_the_rest(tmp_path, capsys):
    """One unresolvable row must not discard an expensive run's other evidence."""
    repo = _live_repo(tmp_path)
    state = _state_with_one_observation()
    state.observations.insert(
        0,
        {
            "operation": "not.an.operation",
            "observation": dict(state.observations[0]["observation"], id="st0-bogus"),
        },
    )
    destination = repo / "test-results-round2-observations.json"

    assert runner._emit_observations(
        state, destination, ("V10R3", "POWER10"), "(not set)", repo
    )

    document = json.loads(destination.read_text())
    assert [entry["operation"] for entry in document] == ["console.info"]
    assert "unknown operation not.an.operation" in capsys.readouterr().out


def test_gitignore_covers_live_test_results():
    """Both the results document and the atomic write's stranded temp file."""
    root = Path(__file__).parents[1]
    for name in ("test-results-round2.json", ".test-results-round2.json.abc123.tmp"):
        assert (
            subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-q", name], check=False
            ).returncode
            == 0
        ), name


@pytest.mark.parametrize(
    "value",
    [
        "hmc01.lab.example.com",
        "0644C7T",
        "U78CB.001.WZS0044-P1-C2",
        "lab-hmc-3",
        "10.1.2.3",
    ],
)
def test_an_environment_value_outside_its_grammar_is_rejected(tmp_path, value):
    """These two strings are the only free text an observation carries.

    Rejecting them at read time keeps a hostname or serial from reaching disk at
    all, rather than surfacing when a human pastes it into `maturity.json`.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LIVE_TEST_ENV_HMC_RELEASE={value}\nLIVE_TEST_ENV_HARDWARE_FAMILY=POWER10\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match its grammar") as raised:
        runner._read_environment(env_file)
    assert value not in str(raised.value)


def test_the_repository_root_is_resolved_from_git_not_the_working_directory(
    tmp_path, monkeypatch
):
    """From a subdirectory, `Path.cwd()` would fingerprint no files at all."""
    repo = _live_repo(tmp_path)
    subdirectory = repo / "scripts"
    monkeypatch.chdir(subdirectory)

    assert runner._repository_root() == repo


def test_the_repository_root_refuses_a_checkout_without_the_package(
    tmp_path, monkeypatch
):
    """A repository that is not this one must not silently fingerprint nothing."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    monkeypatch.chdir(tmp_path)

    assert runner._repository_root() is None


def test_an_unignored_results_path_exits_before_the_run(monkeypatch, tmp_path, capsys):
    """The results document is the larger, more sensitive of the two writes.

    Guarding only the observations file would refuse the small write and let the
    verbatim HMC responses land unignored beside it.
    """
    repo = _live_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        runner.LiveTestConfig,
        "from_env_file",
        classmethod(lambda _cls: runner.LiveTestConfig()),
    )
    monkeypatch.setattr(runner, "_read_environment", lambda *_a: ("V10R3", "POWER10"))
    monkeypatch.setattr(
        runner, "create_mcp", lambda *_a, **_k: pytest.fail("created MCP")
    )

    assert runner._run_from_arguments(["--results-file", "evidence.json"]) == 1

    output = capsys.readouterr().out
    assert "git does not ignore evidence.json" in output
    assert "evidence-observations.json" in output
    assert not (repo / "evidence.json").exists()


def test_an_ignored_results_path_passes_the_startup_gate(monkeypatch, tmp_path):
    repo = _live_repo(tmp_path)
    monkeypatch.chdir(repo)

    assert runner._destination_is_ignored(Path("test-results-round2.json"))
    assert runner._destination_is_ignored(
        runner._observations_path("test-results-round2.json")
    )
    assert not runner._destination_is_ignored(Path("evidence.json"))


def test_record_honours_an_explicit_result():
    """`record_verified` supplies its verdict here rather than patching it after."""
    state = runner.RunState()

    state.record(1, "hmc_get_console_info", "PASS", {}, result="passed")

    assert state.results[0]["result"] == "passed"


def test_record_verified_writes_its_verdict_with_the_row():
    """The verdict must not be patched onto `results[-1]` after the append."""
    state = runner.RunState()
    state.record(0, "unrelated", "PASS", {})
    state.record_verified(
        1,
        "hmc_get_console_info",
        operation="console.info",
        scenario="st1-console-identity",
        assertions=[observation.Assertion("console-uuid-present", True)],
        cleanup="not-required",
        data={"uuid": "c"},
    )

    assert [row["result"] for row in state.results] == ["observed", "passed"]


@pytest.mark.parametrize(
    "identity", ["job-", "j", "ab", "Job-found", "-job", "job_found"]
)
def test_assertion_id_rejects_a_truncated_or_malformed_token(identity):
    """A trailing hyphen is a truncated token, not a closed-shape one."""
    with pytest.raises(ValueError, match="closed-shape token"):
        observation.Assertion(identity, True)


def test_the_two_assertion_id_patterns_agree():
    """The runner bounds ids on the way out; the validator bounds them on the way in."""
    anchored = observation.ASSERTION_ID.pattern

    assert anchored.startswith("\\A") and anchored.endswith("\\Z")
    assert (
        anchored.removeprefix("\\A").removesuffix("\\Z")
        == runner.check_capability_inventory.ASSERTION_ID.pattern
    )


# --- #1332: an interrupted run still writes its results document -------------


def _baseline_then_raise(monkeypatch, error: BaseException) -> None:
    """ST0 captures a baseline and records a row; the next subtask raises *error*."""

    async def capture_baseline(_client, state):
        state.artifacts.lp3_baseline = {"description": "original", "msp": False}
        state.record(0, "hmc_get_lpar_description", "PASS", "original")

    async def interrupted(_client, _state):
        raise error

    monkeypatch.setattr(runner, "SUBTASKS", {0: capture_baseline, 4: interrupted})


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt()])
async def test_main_writes_partial_results_when_a_subtask_raises(
    monkeypatch, tmp_path, error
):
    """The ST0 baseline survives the interruption the operator must recover from."""
    _isolate_runner(monkeypatch)
    _baseline_then_raise(monkeypatch, error)
    closed: list[bool] = []
    monkeypatch.setattr(
        runner.IsoHttpServer, "close", lambda _self: closed.append(True)
    )
    results_path = tmp_path / "results.json"

    with pytest.raises(type(error)) as raised:
        await runner.main(
            results_path=str(results_path), config=runner.LiveTestConfig()
        )

    assert raised.value is error
    assert closed == [True]
    saved = json.loads(results_path.read_text())
    assert saved["artifacts"]["lp3_baseline"] == {
        "description": "original",
        "msp": False,
    }
    assert saved["run"]["partial"] is True
    assert saved["run"]["subtasks"] == [0, 4]
    assert [row["tool"] for row in saved["results"]] == ["hmc_get_lpar_description"]


@pytest.mark.asyncio
async def test_partial_results_write_failure_does_not_mask_the_run_failure(
    monkeypatch, tmp_path, capsys
):
    """The run's own exception is what propagates, whatever the write does."""
    _isolate_runner(monkeypatch)
    error = RuntimeError("boom")
    _baseline_then_raise(monkeypatch, error)

    def refuse(_path, _document):
        raise OSError("disk full")

    monkeypatch.setattr(runner, "_write_results", refuse)

    with pytest.raises(RuntimeError) as raised:
        await runner.main(
            results_path=str(tmp_path / "results.json"),
            config=runner.LiveTestConfig(),
        )

    assert raised.value is error
    assert "Could not write partial results: disk full" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--detach-probe"],
        ["--group", "storage", "--detach-probe"],
        ["41", "--detach-probe"],
    ],
)
def test_detach_probe_requires_explicit_lpar_power_group(argv):
    with pytest.raises(SystemExit):
        runner._parse_arguments(argv)


def test_detach_probe_selection_and_provenance():
    args = runner._parse_arguments(["--group", "lpar-power", "--detach-probe"])
    assert args.detach_probe and args.group == "lpar-power"
    assert (
        runner._run_provenance(
            [41], args.group, None, "(not set)", False, detach_probe=True
        )["detach_probe"]
        is True
    )
    assert "detach_probe" not in runner._run_provenance(
        [41], args.group, None, "(not set)", False
    )


@pytest.mark.asyncio
async def test_direct_detach_probe_refuses_bad_selection_before_configuration(
    monkeypatch,
):
    def forbidden():
        raise AssertionError("configuration accessed for invalid probe selection")

    monkeypatch.setattr(runner.LiveTestConfig, "from_env_file", forbidden)
    assert await runner.main(group="storage", detach_probe=True) == 1
    assert (
        await runner.main(subtask_filter=41, group="lpar-power", detach_probe=True) == 1
    )


@pytest.mark.asyncio
async def test_detach_probe_dispatches_only_the_probe_and_stamps_private_results(
    monkeypatch, tmp_path
):
    _isolate_runner(monkeypatch)
    seen = []

    async def probe(_client, state):
        seen.append("probe")
        state.record(41, "bounded probe", "PASS", {})

    async def broad(_client, _state):
        raise AssertionError("broad ST41 arm dispatched")

    monkeypatch.setattr(runner, "exercise_detach_probe", probe, raising=False)
    monkeypatch.setattr(runner, "SUBTASKS", {41: broad})
    path = tmp_path / "results.json"
    assert (
        await runner.main(
            group="lpar-power",
            detach_probe=True,
            results_path=str(path),
            config=runner.LiveTestConfig(),
        )
        == 0
    )
    assert seen == ["probe"]
    assert json.loads(path.read_text())["run"]["detach_probe"] is True


def test_scratch_settings_are_optional_and_dotenv_only(tmp_path, monkeypatch):
    example = Path(__file__).parents[1] / ".env.example"
    text = example.read_text()
    path = tmp_path / ".env"
    path.write_text(text)
    monkeypatch.setenv("LIVE_TEST_SCRATCH_PV_NAME", "hdisk8")
    monkeypatch.setenv("LIVE_TEST_SCRATCH_VG_NAME", "hpvg88888888")
    config = runner.LiveTestConfig.from_env_file(path)
    assert (config.scratch_pv_name, config.scratch_vg_name) == ("", "")
    path.write_text(
        text
        + "\nLIVE_TEST_SCRATCH_PV_NAME=hdisk9\n"
        + "LIVE_TEST_SCRATCH_VG_NAME=hpvg00000009\n"
    )
    config = runner.LiveTestConfig.from_env_file(path)
    assert (config.scratch_pv_name, config.scratch_vg_name) == (
        "hdisk9",
        "hpvg00000009",
    )


@pytest.mark.parametrize("value", ["7", b"7", 7.9])
def test_baseline_numeric_fields_keep_direct_int_conversion(value):
    baseline = {}
    inventory._capture_cna_identifiers([{"PortVLANID": value}], baseline)
    assert baseline == {"pvid": 7, "vswitch_id": 0}
    state = runner.RunState()
    state.artifacts.lp3_baseline = {"lpars": {"MinimumMemory": value}}
    assert provisioning._baseline_provision_resources(state)["min_memory"] == 7


def test_baseline_numeric_fields_keep_truthy_fallbacks_and_conversion_errors():
    baseline = {}
    inventory._capture_cna_identifiers(
        [{"PortVLANID": 0, "port_vlan_id": "7", "VirtualSwitchID": "2"}], baseline
    )
    assert baseline == {"pvid": 7, "vswitch_id": 2}
    state = runner.RunState()
    state.artifacts.lp3_baseline = {
        "lpars": {"MinimumMemory": 0, "minimum_memory": "7"}
    }
    assert provisioning._baseline_provision_resources(state)["min_memory"] == 7
    state.artifacts.lp3_baseline = {"lpars": {"MinimumMemory": "invalid"}}
    with pytest.raises(ValueError):
        provisioning._baseline_provision_resources(state)
    with pytest.raises(ValueError):
        inventory._capture_cna_identifiers([{"PortVLANID": "invalid"}], {})
