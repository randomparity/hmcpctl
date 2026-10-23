"""C12 typed CLI/MCP forwarding and bounded input."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError
from typer.testing import CliRunner

from hmcpctl.cli import app
from hmcpctl.documents.lpar_io import LparPhysicalIO
from hmcpctl.operations.lpar.assignments import LparPcieWorkflowResult


def test_mcp_create_forwards_explicit_console(monkeypatch):
    from hmcpctl.server_tools.lpar import lifecycle_create

    call = AsyncMock(return_value="created")
    monkeypatch.setattr(lifecycle_create, "create_lpar", call)
    monkeypatch.setattr(
        lifecycle_create, "with_client", lambda f, **kw: asyncio.run(f("hmc"))
    )
    lifecycle_create.hmc_create_lpar(
        "s", "l", partition_type="OS400", physical_io=LparPhysicalIO(console_slot="hmc")
    )
    assert call.await_args.args[2].physical_io.console_slot == "hmc"


def test_cli_console_json_forwarding_and_invalid_file(monkeypatch, tmp_path):
    from hmcpctl.cli_commands.lpar import create

    call = AsyncMock(
        return_value=LparPcieWorkflowResult(True, True, {"UUID": "new"}, True, (), ())
    )
    monkeypatch.setattr(create, "create_lpar", call)
    monkeypatch.setattr(create, "with_client", lambda f: asyncio.run(f("hmc")))
    path = tmp_path / "io.json"
    path.write_text('{"console_slot":"hmc","virtual_opti_pool_id":0}')
    args = [
        "lpars",
        "create",
        "new",
        "--system",
        "s",
        "--type",
        "OS400",
        "--physical-io",
        str(path),
        "--yes",
    ]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert call.await_args.args[2].physical_io == LparPhysicalIO(
        console_slot="hmc", virtual_opti_pool_id=0
    )
    call.reset_mock()
    path.write_text('{"console_slot":"4"}')
    assert CliRunner().invoke(app, args).exit_code != 0
    call.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {"virtual_opti_pool_id": True},
        {"unknown": 1},
        {"io_slots": [{"drc_index": "21010010", "pool_id": None, "is_required": 1}]},
    ],
)
def test_schema_refuses_coercions_and_unknown_keys(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(LparPhysicalIO).validate_python(payload)
