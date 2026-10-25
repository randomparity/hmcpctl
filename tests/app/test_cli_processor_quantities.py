"""Native processor presentations preserve units, scope and old resources mode."""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.documents.lpar import LparResources
from hmcpctl.documents.lpar_processors import LparProcessorChange
from hmcpctl.server_tools.lpar import lifecycle


def test_mcp_modes_preserve_profile_scope_and_ownership(monkeypatch):
    hmc = MagicMock()
    native, absolute = (
        AsyncMock(return_value={"outcome": "applied"}),
        AsyncMock(return_value={"legacy": True}),
    )
    monkeypatch.setattr(lifecycle, "change_lpar_processors", native, raising=False)
    monkeypatch.setattr(lifecycle, "set_lpar_processors", absolute)
    seen = []

    def client(fn, **kwargs):
        seen.append(kwargs)
        return asyncio.run(fn(hmc))

    monkeypatch.setattr(lifecycle, "with_client", client)
    change = LparProcessorChange(operation="add", processors=1, processing_units="0.10")
    assert lifecycle.hmc_dlpar_proc(
        "lpar-R1",
        profile="profile-R1",
        system_name_or_uuid="sys-R1",
        ownership_override=True,
        change=change,
    ) == {"outcome": "applied"}
    native.assert_awaited_once_with(
        hmc, "lpar-R1", change, system_name_or_uuid="sys-R1", ownership_override=True
    )
    resources = LparResources(desired_procs=0.5, desired_vcpus=2)
    assert lifecycle.hmc_dlpar_proc(
        "lpar-R1", resources, "profile-R1", "sys-R1", True
    ) == {"legacy": True}
    absolute.assert_awaited_once_with(
        hmc, "lpar-R1", resources, system_name_or_uuid="sys-R1", ownership_override=True
    )
    assert seen == [{"profile": "profile-R1"}] * 2


@pytest.mark.parametrize(
    "system,resources",
    [
        (None, LparResources()),
        (" ", LparResources()),
        ("sys-R1", LparResources(desired_procs=0.5)),
        ("sys-R1", LparResources(desired_memory=1024)),
    ],
)
def test_mcp_invalid_mode_refuses_before_client(monkeypatch, system, resources):
    client = MagicMock()
    monkeypatch.setattr(lifecycle, "with_client", client)
    with pytest.raises(ValueError):
        lifecycle.hmc_dlpar_proc(
            "lpar-R1",
            resources=resources,
            system_name_or_uuid=system,
            change=LparProcessorChange(operation="add", processors=1),
        )
    client.assert_not_called()


def test_mcp_modes_and_security():
    from hmcpctl.authorization.access_policy import DEFAULT_CONNECTION_TOKEN
    from hmcpctl.cli_commands.legacy_policy import compile_legacy_policy
    from hmcpctl.server import TOOL_SECURITY, create_mcp

    mcp = create_mcp(compile_legacy_policy(TOOL_SECURITY, (DEFAULT_CONNECTION_TOKEN,)))
    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "hmc_dlpar_proc")
    model = next(
        s
        for s in tool.parameters["properties"]["change"]["anyOf"]
        if s.get("type") == "object"
    )
    assert model["additionalProperties"] is False
    assert model["properties"]["operation"]["enum"] == ["add", "remove", "set"]
    assert set(model["properties"]) == {
        "operation",
        "processors",
        "processing_units",
        "force",
    }
    assert TOOL_SECURITY["hmc_dlpar_proc"].operation == "lpar.dlpar_proc"
    assert TOOL_SECURITY["hmc_dlpar_proc"].effect == "mutate"
    assert TOOL_SECURITY["hmc_dlpar_proc"].target_kind == "lpar"


@pytest.mark.parametrize(
    "args",
    [
        ["--operation", "move", "--procs", "1"],
        ["--operation", "add"],
        ["--operation", "add", "--procunits", "0.001"],
        ["--operation", "set", "--procs", "1.5"],
        ["--operation", "set", "--procunits", "NaN"],
        ["--operation", "set", "--procs", "-1"],
    ],
)
def test_cli_processor_invalid_inputs_before_client(monkeypatch, args):
    from hmcpctl.cli_commands.lpar import processors

    client = MagicMock()
    monkeypatch.setattr(processors, "with_client", client)
    result = CliRunner().invoke(
        app, ["lpars", "processors", "lpar-R1", "--system", "sys-R1", "--yes", *args]
    )
    assert result.exit_code == 2
    client.assert_not_called()


@pytest.mark.parametrize(
    "outcome,code",
    [("applied", 0), ("noop", 0), ("rejected", 1), ("partial", 1), ("unverified", 1)],
)
def test_cli_processor_arguments_and_outcomes(monkeypatch, outcome, code):
    from hmcpctl.cli_commands.lpar import processors

    hmc = MagicMock()
    operation = AsyncMock(
        return_value={"outcome": outcome, "workflow_completed": code == 0}
    )
    monkeypatch.setattr(processors, "change_lpar_processors", operation)
    monkeypatch.setattr(processors, "with_client", lambda fn: asyncio.run(fn(hmc)))
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "processors",
            "lpar-R1",
            "--system",
            "sys-R1",
            "--operation",
            "add",
            "--procs",
            "1",
            "--procunits",
            "0.10",
            "--force",
            "--yes",
            "--ownership-override",
        ],
    )
    assert result.exit_code == code, result.output
    assert outcome in result.output
    operation.assert_awaited_once_with(
        hmc,
        "lpar-R1",
        LparProcessorChange(
            operation="add", processors=1, processing_units=Decimal("0.10"), force=True
        ),
        system_name_or_uuid="sys-R1",
        ownership_override=True,
    )


def test_cli_confirmation_names_coupled_reset_and_decline_stops(monkeypatch):
    from hmcpctl.cli_commands.lpar import processors

    client = MagicMock()
    monkeypatch.setattr(processors, "with_client", client)
    result = CliRunner().invoke(
        app,
        [
            "lpars",
            "processors",
            "lpar-R1",
            "--system",
            "sys-R1",
            "--operation",
            "set",
            "--procs",
            "0",
        ],
        input="n\n",
    )
    assert result.exit_code == 0
    assert (
        "memory" in result.output
        and "processor" in result.output
        and "restart" in result.output
    )
    client.assert_not_called()


def test_processor_quantity_metadata_binding():
    import inspect
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    operations = json.loads((root / "docs/capabilities/operations.json").read_text())[
        "operations"
    ]
    operation = next(row for row in operations if row["operation"] == "lpar.dlpar_proc")
    assert set(operation["row_ids"]) == {
        "rest:managed-system/logical-partition",
        "cli:commands/chhwres",
        "cli:commands/lshwres",
    }
    assert operation["signature"] == str(inspect.signature(lifecycle.hmc_dlpar_proc))
    assert {
        "tests/lpar/test_processor_quantities.py",
        "tests/app/test_cli_processor_quantities.py",
    } <= set(operation["tests"])
    maturity = json.loads((root / "docs/capabilities/maturity.json").read_text())[
        "operations"
    ]
    entry = next(row for row in maturity if row["operation"] == "lpar.dlpar_proc")
    variants = {
        item["variant"]: item for item in entry["implementation"]["implemented_scope"]
    }
    assert set(variants) == {
        "processor-read-modify-write",
        "native-current-processor-quantities",
    }
    parameters = variants["native-current-processor-quantities"]["parameters"]
    assert {item["name"] for item in parameters} == {
        "operation",
        "processors",
        "processing_units",
        "force",
    }
    assert all(item["constraint"].strip() for item in parameters)
    assert {item["scenario"] for item in entry["evidence"]} == {"st39-lpar-config"}
    projection = json.loads(
        (root / "src/hmcpctl/_operation_maturity.json").read_text()
    )["operations"]
    current = next(row for row in projection if row["operation"] == "lpar.dlpar_proc")
    assert current["implementation"] == "implemented"
    assert current["verification"] == "stale" and current["reason"] == "closure-changed"
