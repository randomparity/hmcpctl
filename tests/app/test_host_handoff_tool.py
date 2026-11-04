"""hmc_prepare_host_handoff over MCP under real access policies (#1852, ADR 0329)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.call_context import HOLD_EXEMPT_TOOLS
from hmcpctl.operations.logical import holds
from hmcpctl.server import PRIMARY_TOOLS, TOOL_SECURITY, create_mcp
from hmcpctl.tool_registry import hold_guarded

HANDOFF = "hmc_prepare_host_handoff"
SYS = "0000000a-0000-4000-8000-000000000000"
LPAR = "0000000a-0000-4000-8000-0000000000aa"
VIOS = "0000000a-0000-4000-9000-000000000001"
REQUIRED = ("hmc_get_lpar", "hmc_list_lpar_ownership", "hmc_get_vios_storage_detail")
SECTIONS = ("hmc_list_vios", "hmc_list_adapters")


@pytest.fixture(autouse=True)
def environment_only_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep build_config off a developer's own TOML profiles (AGENTS.md, HMC_* leaks)."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.delenv("HMC_AGENT_ID", raising=False)


class _HMC:
    def __init__(self) -> None:
        self.config = object()
        self.calls: list[str] = []

    async def list_system_summaries(self) -> list[dict]:
        return [{"UUID": SYS, "SystemName": "sys-R1"}]

    async def list_logical_partitions(self, uuid: str) -> list[dict]:
        self.calls.append(f"list_logical_partitions {uuid}")
        resource = {"PartitionName": "lpar-a", "PartitionID": "7"}
        return [{"UUID": LPAR, "Resource": resource}]

    async def list_vios(self, uuid: str) -> list[dict]:
        self.calls.append(f"list_vios {uuid}")
        return [{"UUID": VIOS}]

    async def get_vios_storage_detail(self, uuid: str) -> dict:
        self.calls.append(f"get_vios_storage_detail {uuid}")
        return {"Resource": {}}

    async def _network_adapter_creation_feed(
        self, uuid: str, *, allow_empty: bool = False
    ) -> list[dict]:
        self.calls.append(f"adapters {uuid}")
        return []


def _app(*grants: dict) -> FastMCP:
    policy = compile_access_policy(
        {"policies": {"test": {"grants": list(grants)}}},
        "test",
        TOOL_SECURITY,
        "test-host-handoff.toml",
    )
    return create_mcp(policy)


def _grant(*tools: str, targets: Any = "all-targets") -> dict:
    return {"tools": list(tools), "connections": ["<default>"], "targets": targets}


_ALL = _grant(HANDOFF, *REQUIRED, *SECTIONS)
_PREPARE = {
    "action": "prepare",
    "lpar_name_or_uuid": "lpar-a",
    "system_name_or_uuid": "sys-R1",
}


def _call(app: FastMCP, hmc: _HMC, arguments: dict) -> dict:
    @contextlib.asynccontextmanager
    async def client(*_args: Any, **_kwargs: Any) -> AsyncIterator[_HMC]:
        yield hmc

    async def go() -> dict:
        async with Client(app) as mcp_client:
            result = await mcp_client.call_tool(HANDOFF, arguments)
            assert result.structured_content is not None
            return result.structured_content

    with patch("hmcpctl._app.client_from_env", side_effect=client):
        return asyncio.run(go())


def test_handoff_tool_is_registered_primary_and_exempt():
    security = TOOL_SECURITY[HANDOFF]
    assert (security.effect, security.operation) == (
        "mutate",
        "lpar.prepare_host_handoff",
    )
    assert security.exhaustive_targets is False
    assert HANDOFF in PRIMARY_TOOLS and HANDOFF in HOLD_EXEMPT_TOOLS
    assert not hold_guarded(HANDOFF, security)

    async def listed() -> Any:
        async with Client(_app(_ALL)) as client:
            return {tool.name: tool for tool in await client.list_tools()}[HANDOFF]

    tool = asyncio.run(listed())
    assert set(tool.input_schema["required"]) == {
        "action",
        "lpar_name_or_uuid",
        "system_name_or_uuid",
    }
    assert "hold_id" in tool.input_schema["properties"]


def test_prepare_places_a_hold_and_release_drops_it():
    app = _app(_ALL)
    placed = _call(app, _HMC(), {**_PREPARE, "hold": True, "consumer_label": "kdive"})
    assert placed["action"] == "prepare" and placed["hold"]["hold_id"]
    assert placed["document"]["hold"]["hold_id"] is None
    assert placed["document"]["identity"]["partition_uuid"] == LPAR
    assert placed["document"]["observed"]["adapters"]["source"]["status"] == "ok"
    hold_id = placed["hold"]["hold_id"]
    released = _call(
        app,
        _HMC(),
        {**_PREPARE, "action": "release", "hold_id": hold_id},
    )
    assert released["document"] is None and released["hold"]["hold_id"] is None
    assert holds.find_hold(LPAR) is None


def test_unchecked_tools_match_the_registry():
    document = _call(_app(_ALL), _HMC(), _PREPARE)["document"]
    expected = sorted(
        name
        for name, security in TOOL_SECURITY.items()
        if security.effect != "read"
        and not hold_guarded(name, security)
        and name != HANDOFF
    )
    assert document["unchecked_tools"] == expected
    assert {"hmc_run_command", "hmc_restore_lpar_profiles"} <= set(expected)
    assert "hmc_power_lpar" not in expected


@pytest.mark.parametrize("withheld", REQUIRED)
def test_withheld_required_read_refuses_before_any_hmc_read(withheld: str):
    hmc = _HMC()
    granted = [tool for tool in (*REQUIRED, *SECTIONS) if tool != withheld]
    with pytest.raises(ToolError, match=withheld):
        _call(_app(_grant(HANDOFF, *granted)), hmc, _PREPARE)
    assert hmc.calls == []


def test_withheld_section_reads_report_denied():
    hmc = _HMC()
    observed = _call(_app(_grant(HANDOFF, *REQUIRED)), hmc, _PREPARE)["document"][
        "observed"
    ]
    assert observed["adapters"]["source"]["status"] == "denied"
    assert observed["resources"]["storage_source"]["status"] == "denied"
    assert not any(call.startswith(("adapters", "list_vios")) for call in hmc.calls)


def test_name_scoped_grant_admits_the_adapter_section():
    scoped = {"lpar": ["lpar-a"], "managed_system": ["sys-R1"]}
    app = _app(
        _grant(HANDOFF, "hmc_get_vios_storage_detail", "hmc_list_vios"),
        _grant(
            "hmc_get_lpar",
            "hmc_list_lpar_ownership",
            "hmc_list_adapters",
            targets=scoped,
        ),
    )
    adapters = _call(app, _HMC(), _PREPARE)["document"]["observed"]["adapters"]
    assert adapters["source"]["status"] == "ok"


@pytest.mark.parametrize(
    "arguments",
    [
        {**_PREPARE, "hold": True},
        {**_PREPARE, "consumer_label": "kdive"},
        {**_PREPARE, "hold_id": "x"},
        {**_PREPARE, "action": "release", "hold": True},
    ],
)
def test_undefined_argument_combinations_refuse_before_any_hmc_read(arguments):
    hmc = _HMC()
    with pytest.raises(ToolError):
        _call(_app(_ALL), hmc, arguments)
    assert hmc.calls == []
