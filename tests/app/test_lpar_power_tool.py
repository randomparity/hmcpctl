"""hmc_power_lpar over MCP under real access policies (#1223, ADR 0189, ADR 0199)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from unit.test_lpar_power import LPAR, FakeHMC

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.operations.lpar import power
from hmcpctl.server import PRIMARY_TOOLS, TOOL_SECURITY, create_mcp

POWER = "hmc_power_lpar"
ON, OFF = "hmc_power_on_lpar", "hmc_power_off_lpar"
NEW = {"lpar_name_or_uuid": "web1", "system_name_or_uuid": "sysA"}


@pytest.fixture(autouse=True)
def environment_only_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep build_config off a developer's own TOML profiles (AGENTS.md, HMC_* leaks)."""
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.delenv("HMC_AGENT_ID", raising=False)


@pytest.fixture
def hmc(monkeypatch: pytest.MonkeyPatch) -> FakeHMC:
    fake = FakeHMC()
    monkeypatch.setattr(power, "open_client", lambda _connection, _host: fake)
    monkeypatch.setattr(power, "POLL_SECONDS", 0)
    return fake


def _app(*grants: dict) -> FastMCP:
    policy = compile_access_policy(
        {"policies": {"test": {"grants": list(grants)}}},
        "test",
        TOOL_SECURITY,
        "test-lpar-power.toml",
    )
    return create_mcp(policy)


def _grant(*tools: str, targets: Any = "all-targets") -> dict:
    return {"tools": list(tools), "connections": ["<default>"], "targets": targets}


def _call(app: FastMCP, arguments: dict) -> dict:
    async def go() -> dict:
        async with Client(app) as client:
            result = await client.call_tool(POWER, {"wait_seconds": 10, **arguments})
            assert result.structured_content is not None
            return result.structured_content

    return asyncio.run(go())


def _listed(app: FastMCP) -> dict:
    async def go() -> dict:
        async with Client(app) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    return asyncio.run(go())


def test_power_tool_is_listed_destructive_and_primary():
    security = TOOL_SECURITY[POWER]
    assert (security.effect, security.target_kind) == ("destructive", "console")
    assert security.exhaustive_targets is False
    assert POWER in PRIMARY_TOOLS
    tool = _listed(_app(_grant(POWER, ON, OFF)))[POWER]
    assert tool.annotations is not None and tool.annotations.destructive_hint is True
    schema = tool.input_schema["properties"]
    assert {"request_id", "action", "mode", "continuation"} <= set(schema)
    assert "boot" not in str(schema["continuation"])
    meta = tool.meta or {}
    assert meta.get("io.github.randomparity.hmcpctl/catalog-tier") == "primary"


def test_start_runs_with_power_on_authority_alone(hmc: FakeHMC):
    hmc.after = "running"
    record = _call(
        _app(_grant(POWER, ON)), {**NEW, "request_id": "r1", "action": "start"}
    )
    assert (record["state"], record["outcome"]) == ("terminal", "completed")
    assert record["result"]["observed_state"] == "running"


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_stop_and_restart_need_power_off_authority(hmc: FakeHMC, action: str):
    hmc.state = "running"
    with pytest.raises(ToolError, match=OFF):
        _call(_app(_grant(POWER, ON)), {**NEW, "request_id": "r1", "action": action})
    assert hmc.submits == [] and hmc.listings == 0


def test_start_needs_power_on_authority(hmc: FakeHMC):
    with pytest.raises(ToolError, match=ON):
        _call(_app(_grant(POWER, OFF)), {**NEW, "request_id": "r1", "action": "start"})
    assert hmc.listings == 0


def test_a_target_the_specialist_grant_excludes_is_denied(hmc: FakeHMC):
    app = _app(
        _grant(POWER),
        _grant(ON, targets={"lpar": ["other"], "managed_system": ["sysA"]}),
    )
    with pytest.raises(ToolError, match=ON):
        _call(app, {**NEW, "request_id": "r1", "action": "start"})
    assert hmc.listings == 0


def test_resume_is_authorized_from_the_record(hmc: FakeHMC):
    hmc.state, hmc.status = "running", "RUNNING"
    first = _call(
        _app(_grant(POWER, ON, OFF)),
        {**NEW, "request_id": "r1", "action": "stop", "mode": "immediate"},
    )
    assert first["outcome"] == "needs_attention"
    with pytest.raises(ToolError, match=OFF):
        _call(_app(_grant(POWER, ON)), {"request_id": "r1", "continuation": "resume"})
    hmc.status, hmc.after = "COMPLETED_OK", "not activated"
    done = _call(
        _app(_grant(POWER, ON, OFF)), {"request_id": "r1", "continuation": "resume"}
    )
    assert (done["state"], done["outcome"]) == ("terminal", "completed")
    assert len(hmc.submits) == 1
    assert done["partition_uuid"] == LPAR


def test_a_new_operation_needs_its_selectors_and_action(hmc: FakeHMC):
    with pytest.raises(ToolError, match="system_name_or_uuid"):
        _call(
            _app(_grant(POWER, ON, OFF)),
            {"lpar_name_or_uuid": "web1", "request_id": "r1", "action": "start"},
        )


def test_start_with_immediate_is_refused(hmc: FakeHMC):
    with pytest.raises(ToolError, match="stop and restart only"):
        _call(
            _app(_grant(POWER, ON, OFF)),
            {**NEW, "request_id": "r1", "action": "start", "mode": "immediate"},
        )


def test_continuing_an_unknown_request_is_not_found(hmc: FakeHMC):
    with pytest.raises(ToolError, match="no operation has request_id"):
        _call(
            _app(_grant(POWER, ON, OFF)), {"request_id": "r9", "continuation": "resume"}
        )


def test_an_omitted_mode_is_recorded_as_graceful(hmc: FakeHMC):
    hmc.state, hmc.status = "running", "RUNNING"
    app = _app(_grant(POWER, ON, OFF))
    first = _call(app, {**NEW, "request_id": "r1", "action": "stop"})
    again = _call(
        app, {**NEW, "request_id": "r1", "action": "stop", "mode": "graceful"}
    )
    assert again["operation_id"] == first["operation_id"]
    resumed = _call(
        app, {"request_id": "r1", "continuation": "resume", "mode": "graceful"}
    )
    assert resumed["operation_id"] == first["operation_id"]
    assert len(hmc.submits) == 1


def test_an_unknown_continuation_is_refused_before_admission(
    hmc: FakeHMC, monkeypatch: pytest.MonkeyPatch
):
    from hmcpctl.operations.logical import engine

    def admitted(*_args, **_kwargs):
        raise AssertionError("admitted without the delegated check")

    monkeypatch.setattr(engine, "submit", admitted)
    with pytest.raises(ToolError, match="no operation has request_id"):
        _call(
            _app(_grant(POWER, ON, OFF)),
            {"request_id": "r9", "continuation": "abandon"},
        )
