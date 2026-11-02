"""hmc_reconfigure_lpar over MCP under real access policies (#1226, ADR 0189, ADR 0314)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from unit.test_lpar_reconfigure import LPAR, FakeHMC
from unit.test_lpar_reconfigure import hmc as hmc  # noqa: PLC0414 - fixture import

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.server import PRIMARY_TOOLS, TOOL_SECURITY, create_mcp

TOOL = "hmc_reconfigure_lpar"
PROFILE_WRITE = "hmc_update_partition_profile"
MEM, PROC = "hmc_dlpar_mem", "hmc_dlpar_proc"
MODIFY, RENAME = "hmc_modify_lpar", "hmc_rename_lpar"
NEW = {"lpar_name_or_uuid": "web1", "system_name_or_uuid": "sysA"}
MEMORY = {**NEW, "patch": {"memory_mib": 1024}, "exclusive_writer_window": True}


@pytest.fixture(autouse=True)
def environment_only_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep build_config off a developer's own TOML profiles (AGENTS.md, HMC_* leaks)."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.delenv("HMC_AGENT_ID", raising=False)


def _app(*grants: dict) -> FastMCP:
    policy = compile_access_policy(
        {"policies": {"test": {"grants": list(grants)}}},
        "test",
        TOOL_SECURITY,
        "test-lpar-reconfigure.toml",
    )
    return create_mcp(policy)


def _grant(*tools: str, targets: Any = "all-targets") -> dict:
    return {"tools": list(tools), "connections": ["<default>"], "targets": targets}


def _call(app: FastMCP, arguments: dict) -> dict:
    async def go() -> dict:
        async with Client(app) as client:
            result = await client.call_tool(
                TOOL, {"wait_seconds": 10, "request_id": "r1", **arguments}
            )
            assert result.structured_content is not None
            return result.structured_content

    return asyncio.run(go())


def test_tool_is_listed_destructive_and_primary():
    security = TOOL_SECURITY[TOOL]
    assert (security.effect, security.target_kind) == ("destructive", "console")
    assert TOOL in PRIMARY_TOOLS

    async def listed() -> Any:
        async with Client(_app(_grant(TOOL))) as client:
            return {tool.name: tool for tool in await client.list_tools()}[TOOL]

    tool = asyncio.run(listed())
    assert tool.annotations is not None and tool.annotations.destructive_hint is True
    schema = tool.input_schema["properties"]
    assert {"patch", "allow_disruption", "exclusive_writer_window"} <= set(schema)


def test_memory_change_runs_with_its_delegated_authority(hmc: FakeHMC):
    record = _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), MEMORY)
    assert (record["state"], record["outcome"]) == ("terminal", "completed")
    assert record["result"]["changes"][0]["applied"] == "live"
    assert record["partition_uuid"] == LPAR


def test_memory_without_the_writer_window_is_refused_before_recording(hmc: FakeHMC):
    arguments = {**MEMORY, "exclusive_writer_window": False}
    with pytest.raises(ToolError, match="exclusive_writer_window"):
        _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), arguments)
    with pytest.raises(ToolError, match="no operation has request_id"):
        _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), {"continuation": "resume"})
    assert hmc.calls == []


@pytest.mark.parametrize(
    ("patch", "grants", "missing"),
    [
        ({"memory_mib": 1024}, (PROFILE_WRITE,), MEM),
        ({"memory_mib": 1024}, (MEM,), PROFILE_WRITE),
        ({"processors": 3}, (MEM, PROFILE_WRITE), PROC),
        ({"name": "web2"}, (MODIFY,), RENAME),
        ({"name": "web2"}, (RENAME,), MODIFY),
    ],
)
def test_each_field_needs_its_delegated_tools(hmc: FakeHMC, patch, grants, missing):
    arguments = {**NEW, "patch": patch, "exclusive_writer_window": True}
    with pytest.raises(ToolError, match=missing):
        _call(_app(_grant(TOOL, *grants)), arguments)
    assert hmc.calls == []


def test_rename_alone_needs_no_profile_authority_or_window(hmc: FakeHMC):
    record = _call(
        _app(_grant(TOOL, MODIFY, RENAME)), {**NEW, "patch": {"name": "web2"}}
    )
    assert record["outcome"] == "completed"
    assert hmc.name == "web2"


def test_a_target_the_specialist_grant_excludes_is_denied(hmc: FakeHMC):
    app = _app(
        _grant(TOOL, PROFILE_WRITE),
        _grant(MEM, targets={"lpar": ["other"], "managed_system": ["sysA"]}),
    )
    with pytest.raises(ToolError, match=MEM):
        _call(app, MEMORY)
    assert hmc.calls == []


def test_resume_is_authorized_from_the_record(hmc: FakeHMC):
    hmc.lag = True
    first = _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), MEMORY)
    assert first["outcome"] == "needs_attention"
    with pytest.raises(ToolError, match=MEM):
        _call(_app(_grant(TOOL, PROFILE_WRITE)), {"continuation": "resume"})
    hmc.lag, hmc.memory[1] = False, 1024
    done = _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), {"continuation": "resume"})
    assert (done["state"], done["outcome"]) == ("terminal", "completed")


def test_a_new_operation_needs_its_selectors_and_patch(hmc: FakeHMC):
    with pytest.raises(ToolError, match="patch"):
        _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), NEW)


def test_an_unknown_patch_field_is_refused(hmc: FakeHMC):
    arguments = {**MEMORY, "patch": {"memory_mib": 1024, "boot_mode": "Normal"}}
    with pytest.raises(ToolError):
        _call(_app(_grant(TOOL, MEM, PROFILE_WRITE)), arguments)
    assert hmc.calls == []
