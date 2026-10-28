"""C07 presentation boundaries preserve strict, creation-only configured values."""

import asyncio
import inspect
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from lpar.test_creation_memory_workflow import memory_target as provider_target  # noqa: F401
from pydantic import TypeAdapter
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import AccessPolicyError, compile_access_policy
from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import create
from hmcpctl.cli_commands.vios import core as vios_cli
from hmcpctl.documents.lpar_creation_memory import LparCreationMemory
from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
from hmcpctl.operations.lpar.creation_memory import (
    CreationMemoryObservation,
    CreationMemoryVerification,
)
from hmcpctl.operations.lpar.workflows import create_lpar
from hmcpctl.operations.vios.core import create_vios
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle_create
from hmcpctl.server_tools.vios import core as vios_tools


@pytest.fixture(params=("lpar", "vios"))
def adapter(request, monkeypatch):
    kind = request.param
    tools = lifecycle_create if kind == "lpar" else vios_tools
    cli = create if kind == "lpar" else vios_cli
    outcome = (
        LparPcieWorkflowResult(True, True, {}, True, (), ()) if kind == "lpar" else {}
    )
    operation = AsyncMock(return_value=outcome)
    connections = []

    def run(fn, **kwargs):
        connections.append(kwargs)
        return asyncio.run(fn(object()))

    for module in (tools, cli):
        monkeypatch.setattr(module, "with_client", run)
        monkeypatch.setattr(module, f"create_{kind}", operation)
    return SimpleNamespace(
        kind=kind,
        operation=operation,
        connections=connections,
        handler=getattr(tools, f"hmc_create_{kind}"),
        cli=cli.lpars_create if kind == "lpar" else cli.vios_create,
        tool=f"hmc_create_{kind}",
        group="lpars" if kind == "lpar" else "vios",
    )


def forwarded(adapter):
    call = adapter.operation.await_args
    return (
        call.args[2].creation_memory
        if adapter.kind == "lpar"
        else call.kwargs["creation_memory"]
    )


def policy(adapter, targets="all-targets"):
    return compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": [adapter.tool],
                            "connections": ["<default>"],
                            "targets": targets,
                        }
                    ]
                }
            }
        },
        "test",
        TOOL_SECURITY,
        "test-policy.toml",
    )


def cli_args(adapter):
    return [adapter.group, "create", "part-R1", "--system", "sys-R1"]


def test_direct_handler_appends_and_forwards_typed_memory(adapter):
    parameters = inspect.signature(adapter.handler).parameters
    assert list(parameters)[-3:] == [
        "creation_memory",
        "affinity_group_id",
        "start_settings",
    ]
    assert parameters["creation_memory"].default is None
    memory = LparCreationMemory(
        mem_mode="shared",
        desired_io_entitled_mem="auto",
        mem_weight=0,
        primary_paging_vios_id=7,
        secondary_paging_vios_id="none",
        mem_expansion=Decimal("1.10"),
    )
    adapter.handler("sys-R1", "part-R1", creation_memory=memory)
    assert forwarded(adapter) is memory
    assert adapter.connections == [{"profile": None}]


def test_cli_python_defaults_preserve_omission_for_direct_callers(adapter):
    parameters = inspect.signature(adapter.cli).parameters
    fields = (
        "mem_mode",
        "desired_io_entitled_mem",
        "mem_weight",
        "primary_paging_vios_name",
        "primary_paging_vios_id",
        "secondary_paging_vios_name",
        "secondary_paging_vios_id",
        "mem_expansion",
    )
    assert all(parameters[name].default is None for name in fields)


def test_direct_omission_preserves_defaults(adapter):
    adapter.handler("sys-R1", "part-R1")
    memory = forwarded(adapter)
    assert memory is None if adapter.kind == "vios" else memory.pairs() == []


@pytest.mark.parametrize("bad", [{"mem_expansion": 1.1}, "1.1", False])
def test_direct_untyped_input_rejected_before_client(adapter, bad):
    with pytest.raises(TypeError, match="creation_memory"):
        adapter.handler("sys-R1", "part-R1", creation_memory=bad)
    assert adapter.connections == []


@pytest.mark.parametrize("names", [True, False])
def test_cli_exact_all_eight_fields(adapter, names):
    args = cli_args(adapter) + [
        "--yes",
        "--mem-mode",
        "shared",
        "--desired-io-entitled-mem",
        "auto" if names else "128",
        "--mem-weight",
        "0",
        "--mem-expansion",
        "1.123456789012345678901",
    ]
    args += (
        [
            "--primary-paging-vios-name",
            "paging-a",
            "--secondary-paging-vios-name",
            "paging-b",
        ]
        if names
        else ["--primary-paging-vios-id", "7", "--secondary-paging-vios-id", "none"]
    )
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    memory = forwarded(adapter)
    assert memory.mem_mode == "shared" and memory.mem_weight == 0
    assert memory.mem_expansion == Decimal("1.123456789012345678901")
    assert memory.desired_io_entitled_mem == ("auto" if names else 128)
    assert memory.primary_paging_vios_name == ("paging-a" if names else None)
    assert memory.secondary_paging_vios_name == ("paging-b" if names else None)
    assert memory.primary_paging_vios_id == (None if names else 7)
    assert memory.secondary_paging_vios_id == (None if names else "none")


def test_cli_omission_and_zero_independent_of_os_intent(adapter):
    args = cli_args(adapter) + ["--yes"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert forwarded(adapter).pairs() == []
    if adapter.kind == "lpar":
        args += ["--type", "OS400"]
    result = CliRunner().invoke(app, args + ["--mem-expansion", "0"])
    assert result.exit_code == 0, result.output
    assert forwarded(adapter).pairs() == [("mem_expansion", "0")]


@pytest.mark.parametrize(
    "flags",
    [
        ["--mem-weight", "0"],
        ["--mem-mode", "SHARED"],
        ["--mem-mode", "shared", "--mem-weight", "256"],
        ["--mem-mode", "shared", "--primary-paging-vios-id", "none"],
        ["--mem-mode", "shared", "--desired-io-entitled-mem", "Auto"],
        ["--mem-mode", "shared", "--desired-io-entitled-mem", "1.0"],
        ["--mem-mode", "shared", "--primary-paging-vios-name", "a,b"],
        [
            "--mem-mode",
            "shared",
            "--primary-paging-vios-name",
            "a",
            "--primary-paging-vios-id",
            "7",
        ],
        ["--mem-expansion", "NaN"],
        ["--mem-expansion", "Infinity"],
        ["--mem-expansion", "0.99999999999999999999"],
        ["--mem-expansion", "10.00000000000000001"],
    ],
)
def test_invalid_cli_input_precedes_confirmation_and_client(adapter, flags):
    result = CliRunner().invoke(app, cli_args(adapter) + flags, input="y\n")
    assert result.exit_code != 0
    assert "Create LPAR" not in result.output and "Create VIOS" not in result.output
    assert adapter.connections == []
    adapter.operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_served_numeric_schema_and_strict_runtime(adapter):
    async with Client(create_mcp(policy(adapter))) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        schema = tools[adapter.tool].input_schema
        memory_schema = schema["properties"]["creation_memory"]
        description = memory_schema["description"].lower()
        assert all(
            word in description for word in ("saved", "current", "runtime", "aix")
        )
        fields = next(
            branch["properties"]
            for branch in memory_schema["anyOf"]
            if "properties" in branch
        )
        assert set(fields) == set(LparCreationMemory.__dataclass_fields__)
        expansion = fields["mem_expansion"]
        assert {branch["type"] for branch in expansion["anyOf"]} <= {
            "integer",
            "number",
            "null",
        }
        direct = TypeAdapter(LparCreationMemory).json_schema(mode="validation")
        assert expansion["anyOf"] == direct["properties"]["mem_expansion"]["anyOf"]
        base = {"system_name_or_uuid": "sys-R1", "name": "part-R1"}
        for memory in (
            {"mem_expansion": "1.1"},
            {"mem_expansion": True},
            {"mem_weight": 0},
            {"mem_mode": "shared", "primary_paging_vios_id": "7"},
            {"mem_mode": "shared", "mem_weight": False},
            {"unknown": 1},
        ):
            result = await client.call_tool(
                adapter.tool, {**base, "creation_memory": memory}, raise_on_error=False
            )
            assert result.is_error
            assert adapter.connections == []
        for value in (0, 1.1):
            result = await client.call_tool(
                adapter.tool, {**base, "creation_memory": {"mem_expansion": value}}
            )
            assert not result.is_error
            assert forwarded(adapter).mem_expansion == Decimal(str(value))


def test_target_table_does_not_authorize_nested_providers(adapter):
    assert TOOL_SECURITY[adapter.tool].exhaustive_targets is False
    with pytest.raises(AccessPolicyError, match="all-targets"):
        policy(adapter, {"managed_system": ["sys-R1"]})
    assert adapter.connections == []


@pytest.mark.asyncio
async def test_all_targets_policy_retains_independent_owner_denial(
    adapter, request, monkeypatch
):
    b = request.getfixturevalue("provider_target")
    b.providers[0]["Resource"]["Description"] = (
        "[hmcpctl owner:foreign created:2026-10-27]"
    )
    if adapter.kind == "vios":
        b.local["ResourceType"] = "VirtualIOServer"
        b.local["Resource"]["PartitionType"] = "Virtual IO Server"
    tools = lifecycle_create if adapter.kind == "lpar" else vios_tools
    monkeypatch.setattr(
        tools,
        f"create_{adapter.kind}",
        create_lpar if adapter.kind == "lpar" else create_vios,
    )

    def run(fn, **kwargs):
        adapter.connections.append(kwargs)
        return asyncio.run(fn(b.hmc))

    monkeypatch.setattr(tools, "with_client", run)
    async with Client(create_mcp(policy(adapter))) as client:
        result = await client.call_tool(
            adapter.tool,
            {
                "system_name_or_uuid": "system",
                "name": "new",
                "creation_memory": {
                    "mem_mode": "shared",
                    "primary_paging_vios_id": 7,
                    "secondary_paging_vios_id": "none",
                },
            },
            raise_on_error=False,
        )
        assert result.is_error
        assert "owned by" in str(result.content)
    assert adapter.connections == [{"profile": None}]
    assert not b.created


def test_vios_cli_creation_memory_verification_is_structured_json(monkeypatch):
    verification = CreationMemoryVerification(
        LparCreationMemory(mem_mode="ded", mem_expansion=Decimal("1.1")),
        CreationMemoryObservation("verified", {"mem_expansion": Decimal("1.1")}, ()),
        None,
    )
    outcome = {
        "UUID": "vios-R1",
        "creation_memory_verification": verification,
        "serial_verification": {"status": "verified"},
    }
    monkeypatch.setattr(vios_cli, "with_client", lambda fn: outcome)
    result = CliRunner().invoke(
        app,
        [
            "vios",
            "create",
            "part-R1",
            "--system",
            "sys-R1",
            "--yes",
            "--mem-mode",
            "ded",
            "--mem-expansion",
            "1.1",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    proof = payload["creation_memory_verification"]
    assert proof["saved"]["status"] == "verified"
    assert proof["saved"]["observed"] == {"mem_expansion": "1.1"}
    assert proof["current"] is None
    assert payload["serial_verification"] == {"status": "verified"}
    assert payload["UUID"] == "vios-R1"
    assert outcome["creation_memory_verification"] is verification


@pytest.mark.parametrize(
    "outcome",
    [
        None,
        {"UUID": "vios-R1", "serial_verification": {"status": "verified"}},
        {"creation_memory_verification": None},
        {
            "creation_memory_verification": {
                "saved": {"status": "verified", "observed": {"mem_weight": 0}},
                "current": None,
            },
        },
    ],
)
def test_vios_cli_preserves_existing_plain_or_absent_verification(monkeypatch, outcome):
    monkeypatch.setattr(vios_cli, "with_client", lambda fn: outcome)
    result = CliRunner().invoke(
        app, ["vios", "create", "part-R1", "--system", "sys-R1", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == outcome
