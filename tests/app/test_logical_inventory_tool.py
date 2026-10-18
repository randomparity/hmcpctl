"""hmc_inventory over MCP under real access policies (#1220, ADR 0196)."""

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

from hmcpctl.audit import sink as audit_sink
from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.errors import HMCTransportError
from hmcpctl.operations.inventory.logical import DELEGATED_TOOLS
from hmcpctl.server import TOOL_SECURITY, create_mcp

INVENTORY = "hmc_inventory"
SYS_A = "0000000a-0000-4000-8000-000000000000"
SYS_B = "0000000b-0000-4000-8000-000000000000"


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
                "ConfigurableSystemMemory": "1024",
                "CurrentAvailableSystemMemory": "512",
            },
            "AssociatedSystemProcessorConfiguration": {
                "ConfigurableSystemProcessorUnits": "4",
                "CurrentAvailableSystemProcessorUnits": "2",
            },
        },
    }


def _lpar(system_uuid: str, name: str) -> dict:
    return {
        "UUID": system_uuid[:-1] + "1",
        "Resource": {
            "PartitionName": name,
            "PartitionState": "running",
            "Description": "[hmcpctl owner:agent-a created:2026-10-01]",
        },
    }


class _HMC:
    def __init__(self) -> None:
        self.systems = {SYS_A: _system(SYS_A, "sysA"), SYS_B: _system(SYS_B, "sysB")}
        self.calls: list[str] = []
        self.stalled: set[str] = set()

    async def list_system_summaries(self) -> list[dict]:
        self.calls.append("list_system_summaries")
        return [
            {
                "UUID": uuid,
                "SystemName": entry["Resource"]["SystemName"],
                "State": "operating",
            }
            for uuid, entry in self.systems.items()
        ]

    async def get_managed_system(self, uuid: str) -> dict | None:
        self.calls.append(f"get_managed_system {uuid}")
        return self.systems.get(uuid)

    async def list_logical_partitions(self, uuid: str) -> list[dict]:
        self.calls.append(f"list_logical_partitions {uuid}")
        if uuid in self.stalled:
            raise HMCTransportError("GET LogicalPartition timed out")
        return [_lpar(uuid, f"web-{uuid[7]}")]


def _app(*grants: dict) -> FastMCP:
    policy = compile_access_policy(
        {"policies": {"test": {"grants": list(grants)}}},
        "test",
        TOOL_SECURITY,
        "test-logical-inventory.toml",
    )
    return create_mcp(policy)


def _grant(*tools: str, targets: Any = "all-targets") -> dict:
    return {"tools": list(tools), "connections": ["<default>"], "targets": targets}


def _call(
    app: FastMCP, hmc: _HMC, arguments: dict, *, logoff: Exception | None = None
) -> dict:
    @contextlib.asynccontextmanager
    async def client(*_args: Any, **_kwargs: Any) -> AsyncIterator[_HMC]:
        yield hmc
        # HMCClient.__aexit__ raises a logoff failure when the body exited cleanly.
        if logoff is not None:
            raise logoff

    async def go() -> dict:
        async with Client(app) as mcp_client:
            result = await mcp_client.call_tool(INVENTORY, arguments)
            assert result.structured_content is not None
            return result.structured_content

    with patch("hmcpctl._app.client_from_env", side_effect=client):
        return asyncio.run(go())


def _listed(app: FastMCP) -> dict:
    async def go() -> dict:
        async with Client(app) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    return asyncio.run(go())


_ALL = _grant(INVENTORY, *DELEGATED_TOOLS)
# The inventory's own grant is all-targets (ADR 0196 Decision 4); the partition and
# ownership tools carry the target bound.
_SCOPED = (
    _grant(INVENTORY),
    _grant(
        "hmc_list_lpars",
        "hmc_list_lpar_ownership",
        targets={"managed_system": ["sysA"]},
    ),
)


def test_inventory_is_read_console_and_not_exhaustive():
    security = TOOL_SECURITY[INVENTORY]
    assert (security.effect, security.target_kind) == ("read", "console")
    assert security.exhaustive_targets is False
    assert security.connection_argument == "profile"


def test_delegated_tools_are_registered_names():
    assert set(DELEGATED_TOOLS) <= set(TOOL_SECURITY)


def test_all_targets_policy_returns_every_source_ok():
    page = _call(_app(_ALL), _HMC(), {})
    assert page["systems_source"]["status"] == "ok"
    assert [s["id"] for s in page["systems"]] == [
        f"<default>/{SYS_A}",
        f"<default>/{SYS_B}",
    ]
    for system in page["systems"]:
        assert {v["status"] for v in system["sources"].values()} == {"ok"}
    assert {p["owner"] for p in page["partitions"]} == {"agent-a"}


def test_targets_table_without_selectors_reads_nothing():
    hmc = _HMC()
    page = _call(_app(*_SCOPED), hmc, {})
    assert page["systems_source"]["status"] == "denied"
    assert "Pass systems selectors" in page["systems_source"]["detail"]
    assert page["systems"] == [] and page["partitions"] == []
    assert hmc.calls == []


def test_targets_table_admits_each_selector_separately():
    hmc = _HMC()
    page = _call(_app(*_SCOPED), hmc, {"systems": ["sysA", "sysB"]})
    by_selector = {s["selector"]: s for s in page["systems"]}
    allowed, denied = by_selector["sysA"], by_selector["sysB"]
    assert allowed["sources"]["partitions"]["status"] == "ok"
    assert allowed["sources"]["ownership"]["status"] == "ok"
    # hmc_capacity_report is not granted, so capacity is withheld everywhere.
    assert allowed["sources"]["capacity"]["status"] == "denied"
    assert allowed["total_memory_mib"] is None
    assert denied["sources"]["partitions"]["status"] == "denied"
    assert (denied["id"], denied["uuid"], denied["name"], denied["state"]) == (
        None,
        None,
        None,
        None,
    )
    assert hmc.calls.count("list_system_summaries") == 1
    assert [p["system_id"] for p in page["partitions"]] == [f"<default>/{SYS_A}"]


def test_withheld_ownership_tool_is_denied_and_owner_null():
    app = _app(_grant(INVENTORY, "hmc_list_systems", "hmc_list_lpars"))
    page = _call(app, _HMC(), {})
    for system in page["systems"]:
        assert system["sources"]["ownership"]["status"] == "denied"
        assert "hmc_list_lpar_ownership" in system["sources"]["ownership"]["detail"]
    assert {(p["owned"], p["owner"]) for p in page["partitions"]} == {(None, None)}


def test_each_delegated_decision_is_recorded_under_its_tool(records):
    _call(_app(*_SCOPED), _HMC(), {"systems": ["sysA", "sysB"]})
    decisions = {(r["tool"], r["decision"]) for r in records}
    assert (INVENTORY, "allow") in decisions
    assert ("hmc_list_lpars", "allow") in decisions
    assert ("hmc_list_lpars", "deny") in decisions
    assert ("hmc_list_lpar_ownership", "allow") in decisions


def test_inventory_is_listed_as_primary():
    listed = _listed(_app(_ALL))
    meta = listed[INVENTORY].meta or {}
    assert meta.get("io.github.randomparity.hmcpctl/catalog-tier") == "primary"


def test_partition_stall_returns_the_partial_page_with_a_cursor():
    hmc = _HMC()
    hmc.stalled.add(SYS_A)
    page = _call(_app(_ALL), hmc, {})
    assert page["systems"][0]["sources"]["partitions"]["status"] == "unavailable"
    assert page["truncated"] is True and page["next_cursor"]
    assert f"list_logical_partitions {SYS_B}" not in hmc.calls


def test_whole_hmc_stall_that_also_times_out_logoff_is_a_tool_error():
    """The failure model's bound: one read timeout plus the logoff, then a tool error."""
    from fastmcp.exceptions import ToolError

    hmc = _HMC()
    hmc.stalled.add(SYS_A)
    logoff = HMCTransportError("DELETE /rest/api/web/Logon timed out")
    with pytest.raises(ToolError, match="Logon timed out"):
        _call(_app(_ALL), hmc, {}, logoff=logoff)


def test_bad_arguments_fail_before_the_hmc_session_opens():
    from fastmcp.exceptions import ToolError

    async def go() -> None:
        async with Client(_app(_ALL)) as mcp_client:
            await mcp_client.call_tool(INVENTORY, {"limit": 0})

    with (
        patch("hmcpctl._app.client_from_env") as client,
        pytest.raises(ToolError, match="limit"),
    ):
        asyncio.run(go())
    client.assert_not_called()


def test_blank_owner_and_cursor_read_as_absent():
    """ADR 0094: an MCP client may send an unset optional string as ""."""
    page = _call(_app(_ALL), _HMC(), {"owner": " ", "cursor": ""})
    assert len(page["partitions"]) == 2


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """A config.toml with ``lab`` and ``prod``, and no HMC_HOST to collapse them."""
    from hmcpctl.config import config_dir

    for name in ("XDG_CONFIG_HOME", "APPDATA", "HMC_HOST", "HMC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = config_dir() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[profiles.lab]\nhost = 'lab-hmc.test'\nuser = 'u'\npassword = 'p'\n\n"
        "[profiles.prod]\nhost = 'prod-hmc.test'\nuser = 'u'\npassword = 'p'\n",
        encoding="utf-8",
    )


def test_profile_reaches_every_delegated_connection_check(two_profiles, records):
    app = _app(
        {
            "tools": [INVENTORY, "hmc_list_systems", "hmc_capacity_report"],
            "connections": ["lab", "prod"],
            "targets": "all-targets",
        },
        {
            "tools": ["hmc_list_lpars", "hmc_list_lpar_ownership"],
            "connections": ["lab"],
            "targets": "all-targets",
        },
    )
    hmc = _HMC()
    page = _call(app, hmc, {"profile": "prod"})
    assert page["systems_source"]["status"] == "ok"
    for system in page["systems"]:
        assert system["sources"]["partitions"]["status"] == "denied"
        assert system["sources"]["ownership"]["status"] == "denied"
    assert not [
        call for call in hmc.calls if call.startswith("list_logical_partitions")
    ]
    assert ("hmc_list_lpars", "deny") in {(r["tool"], r["decision"]) for r in records}
    lab = _call(app, _HMC(), {"profile": "lab"})
    assert {s["sources"]["partitions"]["status"] for s in lab["systems"]} == {"ok"}


@pytest.mark.parametrize(
    ("mode", "expected"), [("true", True), ("false", False), (None, None)]
)
def test_processor_mode_wire_name_preserves_three_values(monkeypatch, mode, expected):
    hmc = _HMC()
    original = hmc.list_logical_partitions

    async def partitions(uuid):
        rows = await original(uuid)
        for row in rows:
            row["Resource"]["PartitionProcessorConfiguration"] = {
                "HasDedicatedProcessors": mode,
                "CurrentDedicatedProcessorConfiguration": {"CurrentProcessors": "2"},
                "CurrentSharedProcessorConfiguration": {
                    "CurrentProcessingUnits": "0.5"
                },
            }
        return rows

    monkeypatch.setattr(hmc, "list_logical_partitions", partitions)
    result = _call(_app(_ALL), hmc, {})
    serialized = result["partitions"][0]
    assert serialized["has_dedicated_processors"] is expected
    assert "dedicated_procs" not in serialized
    assert serialized["current_proc_units"] == (2.0 if expected is True else 0.5)
