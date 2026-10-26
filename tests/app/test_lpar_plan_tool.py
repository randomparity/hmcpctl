"""hmc_plan_lpar over MCP under real access policies (#1221, ADR 0198)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from hmcpctl.audit import sink as audit_sink
from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.operations.lpar.plan import PLAN_TOOLS
from hmcpctl.server import PRIMARY_TOOLS, TOOL_SECURITY, create_mcp

PLAN = "hmc_plan_lpar"
SYS_A = "0000000a-0000-4000-8000-000000000000"
SYS_B = "0000000b-0000-4000-8000-000000000000"
VIOS_A = "0000000a-0000-4000-9000-000000000000"
VIOS_B = "0000000b-0000-4000-9000-000000000000"
VG = "0000000a-0000-4000-a000-000000000000"
ADR_0198_ROW = (
    "hmc_list_systems",
    "hmc_list_lpars",
    "hmc_capacity_report",
    "hmc_list_virtual_networks",
    "hmc_list_vios",
    "hmc_list_volume_groups",
    "hmc_get_vios_storage_detail",
)


@pytest.fixture(autouse=True)
def environment_only_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep build_config off a developer's own TOML profiles (AGENTS.md, HMC_* leaks)."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.delenv("HMC_AGENT_ID", raising=False)


@pytest.fixture
def records() -> Iterator[list[dict]]:
    """Every authorization audit record emitted during the test, parsed."""
    parsed: list[dict] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            parsed.append(json.loads(record.getMessage()))

    logger = logging.getLogger(audit_sink.AUDIT_LOGGER_NAME)
    handler, level, propagate = _Collect(), logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield parsed
    logger.removeHandler(handler)
    logger.setLevel(level)
    logger.propagate = propagate


def _system(uuid: str, name: str) -> dict:
    return {
        "UUID": uuid,
        "Resource": {
            "SystemName": name,
            "State": "operating",
            "AssociatedSystemMemoryConfiguration": {
                "ConfigurableSystemMemory": "1048576",
                "CurrentAvailableSystemMemory": "65536",
            },
            "AssociatedSystemProcessorConfiguration": {
                "ConfigurableSystemProcessorUnits": "32",
                "CurrentAvailableSystemProcessorUnits": "8",
            },
        },
    }


class _HMC:
    def __init__(self) -> None:
        self.systems = {SYS_A: _system(SYS_A, "sysA"), SYS_B: _system(SYS_B, "sysB")}
        self.vios = {SYS_A: VIOS_A, SYS_B: VIOS_B}
        self.calls: list[str] = []

    async def list_system_summaries(self) -> list[dict]:
        self.calls.append("list_system_summaries")
        return [
            {
                "UUID": uuid,
                "SystemName": entry["Resource"]["SystemName"],
                "State": entry["Resource"].get("State", "operating"),
            }
            for uuid, entry in self.systems.items()
        ]

    async def get_managed_system(self, uuid: str) -> dict | None:
        self.calls.append(f"get_managed_system {uuid}")
        return self.systems.get(uuid.lower())

    async def list_logical_partitions(self, uuid: str) -> list[dict]:
        self.calls.append(f"list_logical_partitions {uuid}")
        return []

    async def list_virtual_networks(self, uuid: str) -> list[dict]:
        self.calls.append(f"list_virtual_networks {uuid}")
        return [{"Resource": {"NetworkVLANID": "100"}}]

    async def list_vios(self, uuid: str) -> list[dict]:
        self.calls.append(f"list_vios {uuid}")
        return [
            {
                "UUID": self.vios[uuid],
                "Resource": {"PartitionName": "vios1", "PartitionState": "running"},
            }
        ]

    async def list_volume_groups(self, vios_uuid: str) -> list[dict]:
        self.calls.append(f"list_volume_groups {vios_uuid}")
        return [
            {
                "UUID": VG if vios_uuid == VIOS_A else VG.replace("a", "b", 1),
                "Resource": {
                    "GroupName": "rootvg",
                    "GroupCapacity": "200",
                    "FreeSpace": "100",
                },
            }
        ]


def _app(*grants: dict) -> FastMCP:
    policy = compile_access_policy(
        {"policies": {"test": {"grants": list(grants)}}},
        "test",
        TOOL_SECURITY,
        "test-lpar-plan.toml",
    )
    return create_mcp(policy)


def _grant(*tools: str, targets: Any = "all-targets") -> dict:
    return {"tools": list(tools), "connections": ["<default>"], "targets": targets}


_ARGUMENTS = {
    "name": "web1",
    "adapters": {"port_vlan_id": 100},
    "storage": {"storage_name": "web1_root", "capacity_mib": 20480},
    "system_name_or_uuid": "sysA",
}


def _call(app: FastMCP, hmc: _HMC, arguments: dict) -> dict:
    @contextlib.asynccontextmanager
    async def client(*_args: Any, **_kwargs: Any) -> AsyncIterator[_HMC]:
        yield hmc

    async def go() -> dict:
        async with Client(app) as mcp_client:
            result = await mcp_client.call_tool(PLAN, arguments)
            assert result.structured_content is not None
            return result.structured_content

    with patch("hmcpctl._app.client_from_env", side_effect=client):
        return asyncio.run(go())


def _listed(app: FastMCP) -> dict:
    async def go() -> dict:
        async with Client(app) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    return asyncio.run(go())


_ALL = _grant(PLAN, *PLAN_TOOLS)


def test_plan_tool_is_listed_read_and_primary():
    security = TOOL_SECURITY[PLAN]
    assert (security.effect, security.target_kind) == ("read", "console")
    assert security.exhaustive_targets is False
    assert security.connection_argument == "profile"
    assert PLAN in PRIMARY_TOOLS
    tool = _listed(_app(_ALL))[PLAN]
    assert tool.annotations is not None and tool.annotations.read_only_hint is True
    assert {"install", "placement", "storage"} <= set(tool.input_schema["properties"])
    meta = tool.meta or {}
    assert meta.get("io.github.randomparity.hmcpctl/catalog-tier") == "primary"


def test_plan_row_names_registered_tools():
    assert PLAN_TOOLS == ADR_0198_ROW
    assert set(PLAN_TOOLS) <= set(TOOL_SECURITY)


def test_all_targets_policy_plans_a_digest():
    plan = _call(_app(_ALL), _HMC(), _ARGUMENTS)
    assert plan["blockers"] == []
    assert plan["selected"]["system"]["uuid"] == SYS_A
    assert plan["selected"]["vios"]["uuid"] == VIOS_A
    assert len(plan["plan_digest"]) == 64
    assert plan["connection"] == "<default>"


@pytest.mark.parametrize(
    ("withheld", "arguments"),
    [
        *(
            pytest.param(tool, _ARGUMENTS, id=tool)
            for tool in PLAN_TOOLS
            if tool not in ("hmc_list_systems", "hmc_get_vios_storage_detail")
        ),
        pytest.param(
            "hmc_list_systems",
            {**_ARGUMENTS, "system_name_or_uuid": None, "placement": {}},
            id="hmc_list_systems when enumerating",
        ),
        pytest.param(
            "hmc_get_vios_storage_detail",
            {**_ARGUMENTS, "storage": {"storage_name": "web1_root"}},
            id="hmc_get_vios_storage_detail for existing storage",
        ),
    ],
)
def test_withheld_delegated_tool_refuses(withheld: str, arguments: dict):
    hmc = _HMC()
    app = _app(_grant(PLAN, *(tool for tool in PLAN_TOOLS if tool != withheld)))
    with pytest.raises(ToolError, match=withheld):
        _call(app, hmc, arguments)
    assert hmc.calls == []


@pytest.mark.parametrize(
    "withheld", ["hmc_list_systems", "hmc_get_vios_storage_detail"]
)
def test_tools_the_request_does_not_need_are_not_required(withheld: str):
    app = _app(_grant(PLAN, *(tool for tool in PLAN_TOOLS if tool != withheld)))
    plan = _call(app, _HMC(), _ARGUMENTS)
    assert plan["plan_digest"] is not None


# The plan's own grant is all-targets; the system and VIOS tools carry the target
# bound in a table that lists the system by name and the VIOS by UUID, and capacity
# is a console tool that only all-targets admits (ADR 0198 Consequences).
_SCOPED = (
    _grant(PLAN, "hmc_capacity_report"),
    _grant(
        "hmc_list_lpars",
        "hmc_list_virtual_networks",
        "hmc_list_vios",
        "hmc_list_volume_groups",
        "hmc_get_vios_storage_detail",
        targets={"managed_system": ["sysA"], "vios": [VIOS_A]},
    ),
)


def test_target_denial_is_a_blocker_with_audit_record(records):
    hmc = _HMC()
    arguments = {
        **_ARGUMENTS,
        "system_name_or_uuid": None,
        "placement": {"systems": ["sysA", "sysB"]},
    }
    plan = _call(_app(*_SCOPED), hmc, arguments)
    assert plan["selected"]["system"]["uuid"] == SYS_A
    denied = next(c for c in plan["candidates"] if c["selector"] == "sysB")
    assert [(b["code"], b["tool"]) for b in denied["blockers"]] == [
        ("denied", "hmc_list_lpars")
    ]
    assert hmc.calls.count("list_system_summaries") == 1
    decisions = {(r["tool"], r["decision"]) for r in records}
    assert ("hmc_list_lpars", "deny") in decisions
    assert ("hmc_list_volume_groups", "allow") in decisions
    assert (PLAN, "allow") in decisions


def test_targets_table_keyed_by_name_admits_named_system():
    plan = _call(_app(*_SCOPED), _HMC(), _ARGUMENTS)
    assert plan["plan_digest"] is not None


def test_targets_table_listing_the_vios_by_name_denies_it():
    app = _app(
        _grant(PLAN, "hmc_capacity_report"),
        _grant(
            "hmc_list_lpars",
            "hmc_list_virtual_networks",
            "hmc_list_vios",
            "hmc_list_volume_groups",
            "hmc_get_vios_storage_detail",
            targets={"managed_system": ["sysA"], "vios": ["vios1"]},
        ),
    )
    hmc = _HMC()
    plan = _call(app, hmc, _ARGUMENTS)
    (candidate,) = plan["candidates"]
    assert [(b["code"], b["tool"], b["target"]) for b in candidate["blockers"]] == [
        ("denied", "hmc_list_volume_groups", VIOS_A)
    ]
    assert f"list_volume_groups {VIOS_A}" not in hmc.calls


def test_bad_arguments_fail_before_the_hmc_session_opens():
    async def go() -> None:
        async with Client(_app(_ALL)) as mcp_client:
            await mcp_client.call_tool(
                PLAN, {**_ARGUMENTS, "adapters": {"port_vlan_id": 0}}
            )

    with (
        patch("hmcpctl._app.client_from_env") as client,
        pytest.raises(ToolError, match="port_vlan_id"),
    ):
        asyncio.run(go())
    client.assert_not_called()


def test_blank_selectors_read_as_absent():
    """ADR 0094: an MCP client may send an unset optional string as ""."""
    arguments = {
        **_ARGUMENTS,
        "storage": {
            "storage_name": "web1_root",
            "capacity_mib": 20480,
            "vios_uuid": "",
            "vg_uuid": "",
        },
    }
    plan = _call(_app(_ALL), _HMC(), arguments)
    assert plan["plan_digest"] is not None


@pytest.mark.parametrize("weight", [0, 64])
def test_update_weight_fails_before_planner_reads(weight):
    hmc = _HMC()
    with pytest.raises(ToolError, match="uncapped_weight.*only.*update"):
        _call(_app(_ALL), hmc, {**_ARGUMENTS, "resources": {"uncapped_weight": weight}})
    assert hmc.calls == []
