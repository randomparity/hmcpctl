"""Existing partition creation adapters preserve exact memory-layout inputs."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from typer.testing import CliRunner

from hmcpctl.authorization.access_policy import compile_access_policy
from hmcpctl.cli import app
from hmcpctl.cli_commands.lpar import create
from hmcpctl.cli_commands.vios import core as vios_cli
from hmcpctl.documents.lpar_memory_layout import LparMemoryLayout
from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult
from hmcpctl.server import TOOL_SECURITY, create_mcp
from hmcpctl.server_tools.lpar import lifecycle_create
from hmcpctl.server_tools.vios import core as vios_tools


@pytest.fixture(params=("lpar", "vios"))
def adapter(request, monkeypatch):
    kind = request.param
    tools = lifecycle_create if kind == "lpar" else vios_tools
    cli = create if kind == "lpar" else vios_cli
    result = (
        LparPcieWorkflowResult(True, True, {}, True, (), ()) if kind == "lpar" else {}
    )
    operation = AsyncMock(return_value=result)
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
        group="lpars" if kind == "lpar" else "vios",
    )


def forwarded(adapter):
    call = adapter.operation.await_args
    return (
        call.args[2].memory_layout
        if adapter.kind == "lpar"
        else call.kwargs["memory_layout"]
    )


def test_presenters_forward_all_six_values(adapter):
    layout = LparMemoryLayout(hpt_ratio="1:128", ppt_ratio="1:256")
    if adapter.kind == "lpar":
        layout = LparMemoryLayout(
            0, 1, 2, "1:128", "1:256", Decimal("12345678901234567890.12")
        )
    adapter.handler("sys-R1", "part-R1", memory_layout=layout)
    assert forwarded(adapter) is layout
    assert adapter.connections == [{"profile": None}]


@pytest.mark.parametrize("os400", [False, True])
def test_cli_layout_exact_forwarding(adapter, os400):
    args = [
        adapter.group,
        "create",
        "part-R1",
        "--system",
        "sys-R1",
        "--yes",
        "--hpt-ratio",
        "1:128",
        "--ppt-ratio",
        "1:256",
    ]
    if adapter.kind == "lpar":
        if os400:
            args += ["--type", "OS400"]
        else:
            args += [
                "--min-num-huge-pages",
                "0",
                "--desired-num-huge-pages",
                "1",
                "--max-num-huge-pages",
                "2",
                "--kvm-reserved-mem-percent",
                "12345678901234567890.12",
            ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    layout = forwarded(adapter)
    assert layout.hpt_ratio == "1:128" and layout.ppt_ratio == "1:256"
    if adapter.kind == "lpar" and not os400:
        assert layout.min_num_huge_pages == 0
        assert layout.kvm_reserved_mem_percent == Decimal("12345678901234567890.12")
    else:
        assert layout.kvm_reserved_mem_percent is None


@pytest.mark.parametrize(
    "value",
    ["0.0099999999999999999", "1.00000000000000001", "NaN", "Infinity", "1E+65536"],
)
def test_cli_invalid_layout_has_no_client_effect(adapter, value):
    if adapter.kind != "lpar":
        pytest.skip("LPAR reservation input only")
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "create",
            "part-R1",
            "--system",
            "sys-R1",
            "--kvm-reserved-mem-percent",
            value,
        ],
        input="y\n",
    )
    assert result.exit_code != 0
    assert "Create LPAR" not in result.output
    assert adapter.connections == []
    adapter.operation.assert_not_awaited()


@pytest.mark.parametrize("kind", ["lpar", "vios"])
@pytest.mark.asyncio
async def test_typed_mcp_schema_validates_original_precision(adapter, kind):
    if adapter.kind != kind:
        pytest.skip("Other adapter parameterization")
    name = f"hmc_create_{kind}"
    policy = compile_access_policy(
        {
            "policies": {
                "test": {
                    "grants": [
                        {
                            "tools": [name],
                            "connections": ["<default>"],
                            "targets": "all-targets",
                        }
                    ]
                }
            }
        },
        "test",
        TOOL_SECURITY,
        "test-policy.toml",
    )
    async with Client(create_mcp(policy)) as client:
        for value in ["0.0099999999999999999", "1.00000000000000001", "NaN"]:
            result = await client.call_tool(
                name,
                {
                    "system_name_or_uuid": "sys-R1",
                    "name": "part-R1",
                    "memory_layout": {"kvm_reserved_mem_percent": value},
                },
                raise_on_error=False,
            )
            assert result.is_error
            assert adapter.connections == []
        for invalid in [{"unknown_layout_field": 1}, {"min_num_huge_pages": True}]:
            result = await client.call_tool(
                name,
                {
                    "system_name_or_uuid": "sys-R1",
                    "name": "part-R1",
                    "memory_layout": invalid,
                },
                raise_on_error=False,
            )
            assert result.is_error
            assert adapter.connections == []
        if kind == "vios":
            result = await client.call_tool(
                name,
                {
                    "system_name_or_uuid": "sys-R1",
                    "name": "part-R1",
                    "memory_layout": {"kvm_reserved_mem_percent": "0"},
                },
                raise_on_error=False,
            )
            assert result.is_error
            assert adapter.connections == []
        layout = {"hpt_ratio": "1:128", "ppt_ratio": "1:256"}
        if kind == "lpar":
            layout.update(
                min_num_huge_pages=0, kvm_reserved_mem_percent="12345678901234567890.12"
            )
        result = await client.call_tool(
            name,
            {
                "system_name_or_uuid": "sys-R1",
                "name": "part-R1",
                "memory_layout": layout,
            },
        )
        assert not result.is_error
        assert forwarded(adapter).hpt_ratio == "1:128"
        if kind == "lpar":
            assert forwarded(adapter).kvm_reserved_mem_percent == Decimal(
                "12345678901234567890.12"
            )


def test_cli_layout_validation_precedes_confirmation(adapter):
    args = [
        adapter.group,
        "create",
        "part-R1",
        "--system",
        "sys-R1",
        "--hpt-ratio",
        "not,a,token",
    ]
    result = CliRunner().invoke(app, args, input="y\n")
    assert result.exit_code != 0
    assert "Create " not in result.output
    assert adapter.connections == []


def test_presenter_empty_layout_preserves_defaults(adapter):
    adapter.handler("sys-R1", "part-R1")
    layout = forwarded(adapter)
    assert layout is None if adapter.kind == "vios" else layout.pairs() == []


def test_cli_zero_reservation_and_os400_exclusion(adapter):
    if adapter.kind != "lpar":
        pytest.skip("LPAR reservation and OS400 input only")
    args = [
        "lpars",
        "create",
        "part-R1",
        "--system",
        "sys-R1",
        "--kvm-reserved-mem-percent",
        "0",
    ]
    rejected = CliRunner().invoke(app, args + ["--type", "OS400"], input="y\n")
    assert rejected.exit_code != 0 and "Create LPAR" not in rejected.output
    assert adapter.connections == []
    declined = CliRunner().invoke(app, args, input="n\n")
    assert declined.exit_code != 0
    assert adapter.connections == []
    accepted = CliRunner().invoke(app, args + ["--yes"])
    assert accepted.exit_code == 0, accepted.output
    assert forwarded(adapter).pairs() == [("kvm_reserved_mem_percent", "0")]
