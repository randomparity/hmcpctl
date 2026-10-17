"""hmc_inspect_lpar over MCP under real access policies (#1224, ADR 0200)."""

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
from hmcpctl.operations.lpar import inspect as inspect_module
from hmcpctl.server import PRIMARY_TOOLS, TOOL_SECURITY, create_mcp

INSPECT = "hmc_inspect_lpar"
SYS = "0000000a-0000-4000-8000-000000000000"
LPAR = "0000000a-0000-4000-8000-0000000000aa"
VIOS = "0000000a-0000-4000-9000-000000000001"
DELEGATED = (
    "hmc_get_lpar",
    "hmc_read_lpar_refcodes",
    "hmc_list_vios",
    "hmc_get_vios_storage_detail",
)


@pytest.fixture(autouse=True)
def environment_only_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep build_config off a developer's own TOML profiles (AGENTS.md, HMC_* leaks)."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.delenv("HMC_AGENT_ID", raising=False)


@pytest.fixture(autouse=True)
def ssh_refcodes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Read refcodes without SSH, recording each read."""
    reads: list[str] = []

    async def names(_config: Any, system: str, lpar: str) -> tuple[str, str]:
        return system, lpar

    async def refcodes(_config: Any, _system: str, lpar: str, _count: int) -> list:
        reads.append(lpar)
        return [{"lpar_name": lpar, "time_stamp": "t", "refcode": "CA00E105"}]

    monkeypatch.setattr(inspect_module, "resolve_ssh_names", names)
    monkeypatch.setattr(inspect_module, "list_lpar_refcodes", refcodes)
    return reads


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


class _HMC:
    def __init__(self, state: str = "error") -> None:
        self.config = object()
        self.state = state
        self.calls: list[str] = []

    async def list_system_summaries(self) -> list[dict]:
        self.calls.append("list_system_summaries")
        return [{"UUID": SYS, "SystemName": "sysA"}]

    async def list_logical_partitions(self, uuid: str) -> list[dict]:
        self.calls.append(f"list_logical_partitions {uuid}")
        resource = {
            "PartitionName": "web1",
            "PartitionID": "7",
            "PartitionState": self.state,
            "ResourceMonitoringControlState": "inactive",
        }
        return [{"UUID": LPAR, "Resource": resource}]

    async def list_vios(self, uuid: str) -> list[dict]:
        self.calls.append(f"list_vios {uuid}")
        return [{"UUID": VIOS}]

    async def get_vios_storage_detail(self, uuid: str) -> dict | None:
        self.calls.append(f"get_vios_storage_detail {uuid}")
        return {"Resource": {}}


def _app(*grants: dict) -> FastMCP:
    policy = compile_access_policy(
        {"policies": {"test": {"grants": list(grants)}}},
        "test",
        TOOL_SECURITY,
        "test-lpar-inspect.toml",
    )
    return create_mcp(policy)


def _grant(*tools: str, targets: Any = "all-targets") -> dict:
    return {"tools": list(tools), "connections": ["<default>"], "targets": targets}


_ARGUMENTS = {"lpar_name_or_uuid": "web1", "system_name_or_uuid": "sysA"}
_ALL = _grant(INSPECT, *DELEGATED, "hmc_capture_lpar_console", "hmc_power_lpar")


def _call(app: FastMCP, hmc: _HMC, arguments: dict) -> dict:
    @contextlib.asynccontextmanager
    async def client(*_args: Any, **_kwargs: Any) -> AsyncIterator[_HMC]:
        yield hmc

    async def go() -> dict:
        async with Client(app) as mcp_client:
            result = await mcp_client.call_tool(INSPECT, arguments)
            assert result.structured_content is not None
            return result.structured_content

    with patch("hmcpctl._app.client_from_env", side_effect=client):
        return asyncio.run(go())


def _listed(app: FastMCP) -> dict:
    async def go() -> dict:
        async with Client(app) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    return asyncio.run(go())


def test_inspect_tool_is_listed_read_and_primary():
    security = TOOL_SECURITY[INSPECT]
    assert (security.effect, security.operation) == ("read", "lpar.inspect")
    assert security.target_kind == "lpar"
    assert security.exhaustive_targets is False
    assert INSPECT in PRIMARY_TOOLS
    tool = _listed(_app(_ALL))[INSPECT]
    assert tool.annotations is not None and tool.annotations.read_only_hint is True
    assert set(tool.input_schema["required"]) == {
        "lpar_name_or_uuid",
        "system_name_or_uuid",
    }
    meta = tool.meta or {}
    assert meta.get("io.github.randomparity.hmcpctl/catalog-tier") == "primary"


def test_delegated_tools_are_registered():
    assert set(DELEGATED) <= set(TOOL_SECURITY)
    assert {"hmc_capture_lpar_console", "hmc_power_lpar"} <= set(TOOL_SECURITY)


def test_default_inspection_reads_rmc_and_refcodes(ssh_refcodes: list[str]):
    result = _call(_app(_ALL), _HMC(), _ARGUMENTS)
    assert result["id"] == f"<default>/{SYS}/{LPAR}"
    assert result["state"] == "error"
    assert result["rmc"]["source"]["status"] == "ok"
    assert result["refcodes"]["codes"][0]["refcode"] == "CA00E105"
    assert (result["resources"], result["profile_drift"]) == (None, None)
    assert result["next_actions"] == ["hmc_capture_lpar_console"]
    assert ssh_refcodes == ["web1"]


def test_withheld_base_read_refuses_before_any_hmc_read():
    hmc = _HMC()
    app = _app(_grant(INSPECT, *DELEGATED[1:]))
    with pytest.raises(ToolError, match="hmc_get_lpar"):
        _call(app, hmc, _ARGUMENTS)
    assert hmc.calls == []


def test_denied_partition_target_refuses(records: list[dict]):
    hmc = _HMC()
    app = _app(
        _grant(INSPECT),
        _grant("hmc_get_lpar", targets={"lpar": ["other"], "managed_system": ["sysA"]}),
    )
    with pytest.raises(ToolError):
        _call(app, hmc, _ARGUMENTS)
    assert hmc.calls == []
    assert ("hmc_get_lpar", "deny") in {(r["tool"], r["decision"]) for r in records}


def test_withheld_section_tools_report_denied(ssh_refcodes: list[str]):
    hmc = _HMC()
    app = _app(_grant(INSPECT, "hmc_get_lpar"))
    result = _call(
        app, hmc, {**_ARGUMENTS, "include": ["resources", "refcodes", "rmc"]}
    )
    assert result["refcodes"]["source"]["status"] == "denied"
    assert result["resources"]["storage_source"]["status"] == "denied"
    assert result["rmc"]["source"]["status"] == "ok"
    assert ssh_refcodes == []
    assert not any(call.startswith(("list_vios", "get_vios")) for call in hmc.calls)


def test_vios_granted_by_uuid_is_read(records: list[dict]):
    app = _app(
        _grant(INSPECT, "hmc_get_lpar", "hmc_list_vios"),
        _grant(
            "hmc_get_vios_storage_detail",
            targets={"vios": [VIOS], "managed_system": ["sysA"]},
        ),
    )
    result = _call(app, _HMC(), {**_ARGUMENTS, "include": ["resources"]})
    assert result["resources"]["storage_source"]["status"] == "ok"
    decisions = {(r["tool"], r["decision"]) for r in records}
    assert ("hmc_get_vios_storage_detail", "allow") in decisions


def test_next_actions_name_only_permitted_tools():
    app = _app(_grant(INSPECT, *DELEGATED))
    result = _call(app, _HMC(), _ARGUMENTS)
    assert result["next_actions"] == []


def test_profile_drift_is_unavailable():
    result = _call(_app(_ALL), _HMC(), {**_ARGUMENTS, "include": ["profile_drift"]})
    assert result["profile_drift"]["status"] == "unavailable"
    assert result["profile_drift"]["tool"] is None


def test_mcp_inspection_serializes_definitions_without_changing_state(monkeypatch):
    from hmcpctl.ssh import refcodes

    async def transport(*_args):
        return (
            "lpar_name,time_stamp,refcode\nweb1,t,CA00E105 LP=00002\nweb1,t,B2001230\n"
        )

    monkeypatch.setattr(refcodes, "run_hmc_command", transport)
    monkeypatch.setattr(
        inspect_module, "list_lpar_refcodes", refcodes.list_lpar_refcodes
    )
    result = _call(_app(_ALL), _HMC(), _ARGUMENTS)
    rows = result["refcodes"]["codes"]
    assert rows[0]["refcode"] == "CA00E105 LP=00002"
    assert "operating system" in rows[0]["definition"].lower()
    assert rows[0]["definition_source"]["notation"] == "CA00E105"
    assert rows[1]["definition"] is None
    assert rows[1]["definition_source"] is None
    assert result["state"] == "error"
