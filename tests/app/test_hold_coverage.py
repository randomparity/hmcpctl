"""Registry-driven proof that every hold-guarded tool reaches the ADR 0193 hook (#1851).

Every non-``read`` tool that selects a partition, plus the tools that derive one at run
time, is called through the composed server on a held partition: by lower-case UUID,
upper-case UUID and name. Each must be refused ``partition_held`` before any HMC write;
with the matching ``hold_id`` the hook must run and pass, directly and through
``hmc_invoke_tool``. A spy on the installed guard records each check and its outcome.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

import httpx
import pytest
from conftest import (  # noqa: F401 - mock_hmc is a fixture
    assert_no_mutating_requests,
    mock_hmc,
    mock_system_summary,
)
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
from hmcpctl.client.core import HMCClient
from hmcpctl.operations.logical import holds
from hmcpctl.operations.logical.holds import HoldRefused
from hmcpctl.operations.storage import resources as storage_resources
from hmcpctl.resource_identity import PartitionGuard, install_partition_guard
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.ssh import transport
from hmcpctl.tool_registry import RUNTIME_PARTITION_TOOLS, hold_guarded

SYS = "0b3f9a4e-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
LPAR = "7d1e2f3a-4b5c-4d6e-8f70-819a2b3c4d5e"
VIOS = "2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d"
NAME = "lpar-a"

GUARDED = sorted(
    name for name, security in TOOL_SECURITY.items() if hold_guarded(name, security)
)
# Non-read VIOS tools with no partition selector: any of them that reaches the hook through
# a partition it derives must be in RUNTIME_PARTITION_TOOLS, or its holder could not pass.
VIOS_ONLY = sorted(
    name
    for name, security in TOOL_SECURITY.items()
    if security.effect not in ("read", "arbitrary-command")
    and name not in GUARDED
    and any(target.kind == "vios" for target in security.targets)
)

# Arguments a tool needs to get as far as resolving its partition, beyond those
# `_arguments` derives from the schema.
_WINDOW = {"exclusive_writer_window": True}
OVERRIDES: dict[str, dict[str, Any]] = {
    "hmc_assign_dedicated_pcie_slot": {"drc_index": "21010010"},
    "hmc_unassign_dedicated_pcie_slot": {"drc_index": "21010010"},
    "hmc_attach_disk_to_lpar": {"capacity_mib": 1024},
    "hmc_configure_lpar": {"settings": {"allow_perf_collection": True}},
    "hmc_configure_lpar_reporting": {"settings": {"ipl_source": "a"}},
    "hmc_configure_lpar_security_policy": {"settings": {"secure_boot": 0}},
    "hmc_configure_lpar_vtpm": {"settings": {"vtpm_enabled": True}},
    "hmc_delete_partition_profile": _WINDOW,
    "hmc_dlpar_mem": {"resources": {"desired_memory": 1024}},
    "hmc_dlpar_proc": {"change": {"operation": "add", "processors": 1}},
    "hmc_dump_restart_lpar": {"allow_dump_restart": True},
    "hmc_migrate_lpar_with_affinity_preflight": {
        "affinity_preflight": {
            "source_current_score": 1,
            "destination_estimated_score": 1,
            "destination_check_basis": "calculated",
            "configured_minimum": 1,
            "capability": "available",
            "capability_limits": ["none"],
            "response": "warn",
        }
    },
    "hmc_move_lpar_memory": {"move": {"quantity_mib": 256}},
    "hmc_move_lpar_processors": {"move": {"processors": 1}},
    "hmc_power_lpar": {"action": "start"},
    "hmc_reconfigure_lpar": {"patch": {"processors": 1}, **_WINDOW},
    "hmc_save_current_partition_profile": _WINDOW,
    "hmc_set_default_partition_profile": _WINDOW,
    "hmc_set_lpar_accelerator_qos": {"change": {"qos": 1}},
    "hmc_set_lpar_boot_order": {"boot_string": "x"},
    "hmc_set_minimum_affinity_policy": {
        "policy": {"min_affinity_score": 1, "min_affinity_score_action": "warn"}
    },
    "hmc_switch_lpar_paging_vios": _WINDOW,
    "hmc_detach_storage_mapping": {"mapping_id": "vhost0/vtscsi0"},
    "hmc_update_network_adapter": {"adapter_uuid": VIOS, "tagged": True, **_WINDOW},
    "hmc_update_partition_profile": _WINDOW,
}


class SSHReached(Exception):
    """An SSH command ran; the hold hook should have refused first."""


class Spy:
    """Wraps the installed hold guard and records each check's outcome."""

    def __init__(self) -> None:
        self.passed: list[str] = []
        self.refused: list[str] = []

    def check(self, partition_uuid: str) -> None:
        try:
            holds.enforce_hold(partition_uuid)
        except HoldRefused:
            self.refused.append(partition_uuid.lower())
            raise
        self.passed.append(partition_uuid.lower())


_ENTRY = """<entry xmlns="http://www.w3.org/2005/Atom"><id>urn:uuid:{uuid}</id>
<content type="application/vnd.ibm.powervm.uom+xml">
<{kind} xmlns="http://www.ibm.com/xmlns/systems/power/firmware/uom/mc/2012_10/">
<PartitionName>{name}</PartitionName><PartitionID>{number}</PartitionID>
<PartitionType>{environment}</PartitionType><PartitionState>not activated</PartitionState>
</{kind}></content></entry>"""


def _mock_partition(router, kind: str, uuid: str, name: str, number: int) -> None:
    environment = "Virtual IO Server" if kind == "VirtualIOServer" else "AIX/Linux"
    entry = _ENTRY.format(
        uuid=uuid, kind=kind, name=name, number=number, environment=environment
    )
    for path in (
        f"/rest/api/uom/{kind}/{uuid}",
        f"/rest/api/uom/ManagedSystem/{SYS}/{kind}/{uuid}",
    ):
        router.get(path).mock(return_value=httpx.Response(200, text=entry))
    router.get(f"/rest/api/uom/ManagedSystem/{SYS}/{kind}").mock(
        return_value=httpx.Response(
            200, text=f'<feed xmlns="http://www.w3.org/2005/Atom">{entry}</feed>'
        )
    )


@pytest.fixture
def environment(monkeypatch, mock_hmc):  # noqa: F811 - the conftest fixture
    monkeypatch.setenv("HMC_HOST", "hmc.test")
    monkeypatch.setenv("HMC_USER", "hscroot")
    monkeypatch.setenv("HMC_PASSWORD", "abc123")

    def no_ssh(*_args: Any, **_kwargs: Any) -> Any:
        raise SSHReached("ssh reached")

    async def console_info(_self: Any) -> dict[str, Any]:
        return {
            "Resource": {
                "VersionInfo": {
                    "Version": "10",
                    "Release": "3",
                    "ServicePackName": "1060",
                }
            }
        }

    async def storage_mappings(_self: Any, _vios_uuid: str) -> list[dict[str, Any]]:
        return [{"mapping": "vhost0/vtscsi0"}]

    monkeypatch.setattr(transport.asyncssh, "connect", no_ssh)
    monkeypatch.setattr(HMCClient, "get_console_info", console_info)
    monkeypatch.setattr(HMCClient, "list_storage_mappings", storage_mappings)
    monkeypatch.setattr(
        storage_resources, "storage_mapping_id", lambda item: item["mapping"]
    )
    monkeypatch.setattr(storage_resources, "mapping_lpar_uuid", lambda _item: LPAR)
    mock_system_summary(mock_hmc, SYS, "sys-R1")
    _mock_partition(mock_hmc, "LogicalPartition", LPAR, NAME, 3)
    _mock_partition(mock_hmc, "VirtualIOServer", VIOS, "vios-a", 1)
    mock_hmc.route(method__in=["POST", "PUT", "DELETE"]).mock(
        return_value=httpx.Response(500, text="write reached")
    )
    return mock_hmc


@pytest.fixture
def app(environment) -> tuple[FastMCP, Spy, Any]:
    application = create_mcp(
        compile_legacy_policy(
            TOOL_SECURITY, ("<default>",), include_arbitrary_command=False
        )
    )
    spy = Spy()
    install_partition_guard(
        PartitionGuard(spy.check, holds.HOLD_GUARD.names_need_resolution)
    )
    return application, spy, environment


_BY_NAME = {
    "lpar_name_or_uuid": LPAR,
    "lpar_uuid": LPAR,
    "target_lpar_name_or_uuid": LPAR,
    "system_name_or_uuid": SYS,
    "target_system_name_or_uuid": SYS,
    "vios_name_or_uuid": VIOS,
    "vios_uuid": VIOS,
}


def _value(name: str, schema: dict[str, Any]) -> Any:
    if name in _BY_NAME:
        return _BY_NAME[name]
    options = schema.get("anyOf") or [schema]
    for option in options:
        if "enum" in option:
            return option["enum"][0]
        if "const" in option:
            return option["const"]
    kind = next((o.get("type") for o in options if o.get("type") != "null"), "string")
    if kind is None and any("$ref" in option for option in options):
        kind = "object"
    defaults = {"string": "x", "integer": 1, "number": 1, "boolean": False}
    return {"object": {}, "array": []}.get(kind, defaults.get(kind))


async def _arguments(client: Client, tool: str, lpar: str) -> dict[str, Any]:
    listed = {t.name: t for t in await client.list_tools()}[tool]
    schema = listed.input_schema
    arguments = {
        name: _value(name, schema["properties"][name])
        for name in schema.get("required", [])
    }
    if "system_name_or_uuid" in schema["properties"]:
        arguments["system_name_or_uuid"] = SYS
    for selector in ("lpar_name_or_uuid", "lpar_uuid"):
        if selector in schema["properties"]:
            arguments[selector] = lpar
    arguments.update(OVERRIDES.get(tool, {}))
    if "request_id" in schema["properties"]:
        arguments["request_id"] = secrets.token_hex(8)
    return arguments


def _call(app: FastMCP, tool: str, lpar: str, **extra: Any) -> str:
    async def go() -> str:
        async with Client(app) as client:
            arguments = {**await _arguments(client, tool, lpar), **extra}
            try:
                result = await client.call_tool(tool, arguments)
            except ToolError as error:
                return str(error)
        # A logical tool reports a refusal in its operation record, not as an error.
        return json.dumps(result.structured_content)

    return asyncio.run(go())


def _invoke(app: FastMCP, tool: str, lpar: str, **extra: Any) -> str:
    async def go() -> str:
        async with Client(app) as client:
            arguments = {**await _arguments(client, tool, lpar), **extra}
            try:
                result = await client.call_tool(
                    "hmc_invoke_tool", {"name": tool, "arguments": arguments}
                )
            except ToolError as error:
                return str(error)
        return json.dumps(result.structured_content)

    return asyncio.run(go())


def test_guarded_set_is_the_partition_mutations():
    assert "hmc_capture_lpar_console" not in GUARDED
    assert "hmc_power_lpar" in GUARDED
    assert RUNTIME_PARTITION_TOOLS <= set(GUARDED)
    assert all(TOOL_SECURITY[name].effect != "read" for name in GUARDED)


def test_every_guarded_tool_publishes_hold_id_and_no_other_does(app):
    application, _, _ = app

    async def listed() -> dict[str, Any]:
        async with Client(application) as client:
            return {tool.name: tool for tool in await client.list_tools()}

    tools = asyncio.run(listed())
    for name, tool in tools.items():
        has = "hold_id" in tool.input_schema["properties"]
        assert has == (name in GUARDED), name


# Tools whose partition selector names a VIOS: holds are placed on client partitions, and
# these refuse any partition that is not a VIOS before their first write.
VIOS_TARGETS = frozenset({"hmc_set_lpar_msp"})
CLIENT_TARGETS = [name for name in GUARDED if name not in VIOS_TARGETS]


@pytest.mark.parametrize("tool", sorted(VIOS_TARGETS))
def test_vios_target_tool_refuses_a_client_partition(app, tool):
    application, _, router = app
    holds.place_hold(SYS, LPAR, consumer_label="kdive", agent_id="a", connection="c")
    outcome = _call(application, tool, LPAR)
    assert "No Virtual I/O Server" in outcome, outcome
    assert_no_mutating_requests(router)


@pytest.mark.parametrize("tool", CLIENT_TARGETS)
def test_guarded_tool_refuses_a_held_partition(app, tool):
    application, spy, router = app
    holds.place_hold(SYS, LPAR, consumer_label="kdive", agent_id="a", connection="c")
    for lpar in (LPAR, LPAR.upper(), NAME):
        spy.refused.clear()
        outcome = _call(application, tool, lpar)
        assert "partition_held" in outcome, (tool, lpar, outcome)
        assert spy.refused == [LPAR], (tool, lpar)
    assert_no_mutating_requests(router)


@pytest.mark.parametrize("tool", CLIENT_TARGETS)
def test_matching_hold_id_passes_the_hook(app, tool):
    application, spy, _ = app
    placed = holds.place_hold(
        SYS, LPAR, consumer_label="kdive", agent_id="a", connection="c"
    )
    for call in (_call, _invoke):
        spy.passed.clear()
        outcome = call(application, tool, LPAR, hold_id=placed.hold_id)
        assert "partition_held" not in outcome and "hold_id" not in outcome, (
            tool,
            call.__name__,
            outcome,
        )
        assert LPAR in spy.passed, (tool, call.__name__, outcome)


@pytest.mark.parametrize("tool", VIOS_ONLY)
def test_vios_tools_outside_the_guarded_set_never_reach_the_hook(app, tool):
    application, spy, _ = app
    holds.place_hold(SYS, LPAR, consumer_label="kdive", agent_id="a", connection="c")
    _call(application, tool, LPAR)
    assert spy.passed == spy.refused == [], tool


def test_hold_id_stays_outside_a_logical_request_digest(app):
    application, _, _ = app
    request_id = secrets.token_hex(8)

    async def go() -> list[str]:
        async with Client(application) as client:
            arguments = {
                **await _arguments(client, "hmc_power_lpar", LPAR),
                "request_id": request_id,
                "wait_seconds": 0,
            }
            outcomes = []
            for hold_id in ("a" * 32, None, "b" * 32):
                result = await client.call_tool(
                    "hmc_power_lpar", {**arguments, "hold_id": hold_id}
                )
                outcomes.append(result.structured_content["operation_id"])
            return outcomes

    first, *repeats = asyncio.run(go())
    assert repeats == [first, first]


@pytest.mark.parametrize("tool", ["hmc_get_lpar", "hmc_capture_lpar_console"])
def test_reads_and_exempt_tools_pass_a_held_partition(app, tool):
    application, spy, _ = app
    holds.place_hold(SYS, LPAR, consumer_label="kdive", agent_id="a", connection="c")
    outcome = _call(application, tool, LPAR)
    assert "partition_held" not in outcome, outcome
    assert spy.refused == [] and spy.passed == [LPAR], (spy.passed, outcome)
