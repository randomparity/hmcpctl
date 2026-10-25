"""D05 native processor quantities; constructed fixtures use captured field shapes."""

import csv
import io
import shlex
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig

CONFIG = HMCConfig.from_mapping({})
FIELDS = [
    "lpar_id",
    "lpar_name",
    "curr_proc_mode",
    "curr_min_procs",
    "curr_procs",
    "curr_max_procs",
    "pend_proc_mode",
    "pend_min_procs",
    "pend_procs",
    "pend_max_procs",
    "run_procs",
    "curr_min_proc_units",
    "curr_proc_units",
    "curr_max_proc_units",
    "pend_min_proc_units",
    "pend_proc_units",
    "pend_max_proc_units",
    "run_proc_units",
]
ROW = [
    "7",
    "lpar-R1",
    "shared",
    "1",
    "2",
    "4",
    "shared",
    "1",
    "3",
    "4",
    "2",
    "0.10",
    "0.50",
    "1.00",
    "0.10",
    "0.75",
    "1.00",
    "0.50",
]


@pytest.mark.parametrize("operation", ["add", "remove", "set"])
@pytest.mark.parametrize("units", [0, 1, 0.1, "0.10", "1.000", Decimal("12.34")])
def test_request_units_and_values(operation, units):
    from hmcpctl.documents.lpar_processors import LparProcessorChange

    change = LparProcessorChange(
        operation=operation, processors=0, processing_units=units
    )
    assert change.processors == 0
    assert change.processing_units == Decimal(str(units))
    with pytest.raises(ValueError):
        change.processors = 2


@pytest.mark.parametrize(
    "values",
    [
        {"operation": "add"},
        {"operation": "move", "processors": 1},
        *({"operation": "add", "processors": value} for value in [-1, True, 1.5, "1"]),
        *(
            {"operation": "add", "processing_units": value}
            for value in [
                True,
                -1,
                "NaN",
                "Infinity",
                "-Infinity",
                "0.001",
                0.001,
                [],
                "no",
            ]
        ),
        {"operation": "set", "processors": 1, "force": 1},
        {"operation": "set", "processors": 1, "pool": 1},
    ],
)
def test_request_units_and_invalid_values(values):
    from hmcpctl.documents.lpar_processors import LparProcessorChange

    with pytest.raises(ValueError):
        LparProcessorChange(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,native", [("add", "a"), ("remove", "r"), ("set", "s")]
)
@pytest.mark.parametrize("counts,units", [(0, None), (None, "0"), (2, "0.10")])
async def test_processor_native_commands(monkeypatch, operation, native, counts, units):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.ssh.processors import change_lpar_processor_quantity

    command = AsyncMock(return_value="result")
    monkeypatch.setattr("hmcpctl.ssh.processors.run_hmc_command", command)
    change = LparProcessorChange(
        operation=operation, processors=counts, processing_units=units, force=True
    )
    assert (
        await change_lpar_processor_quantity(CONFIG, "system; quoted ' name", 7, change)
        == "result"
    )
    expected = [
        "chhwres",
        "-r",
        "proc",
        "-m",
        "system; quoted ' name",
        "-o",
        native,
        "--id",
        "7",
    ]
    if counts is not None:
        expected += ["--procs", str(counts)]
    if units is not None:
        expected += ["--procunits", f"{Decimal(units):.2f}"]
    assert shlex.split(command.call_args.args[1]) == expected + ["--force"]
    assert command.call_args.kwargs == {
        "include_stderr": True,
        "max_output_bytes": 65536,
    }
    assert command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("partition_id", [0, -1, True, "7", 1.5])
async def test_processor_native_commands_refuse_ids(monkeypatch, partition_id):
    from hmcpctl.documents.lpar_processors import LparProcessorChange
    from hmcpctl.ssh.processors import (
        change_lpar_processor_quantity,
        read_lpar_processor_quantities,
    )

    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.processors.run_hmc_command", command)
    with pytest.raises(ValueError, match="partition_id"):
        await change_lpar_processor_quantity(
            CONFIG,
            "sys-R1",
            partition_id,
            LparProcessorChange(operation="add", processors=1),
        )
    with pytest.raises(ValueError, match="partition_id"):
        await read_lpar_processor_quantities(CONFIG, "sys-R1", partition_id)
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_native_commands_require_typed_request(monkeypatch):
    from hmcpctl.ssh.processors import change_lpar_processor_quantity

    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.processors.run_hmc_command", command)
    with pytest.raises(TypeError):
        await change_lpar_processor_quantity(CONFIG, "sys-R1", 7, {"operation": "set"})
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_read_projection(monkeypatch):
    from hmcpctl.ssh.processors import read_lpar_processor_quantities

    row = [*ROW]
    row[1] = 'lpar, "quoted"'
    output = io.StringIO()
    csv.writer(output).writerow(row)
    command = AsyncMock(return_value=output.getvalue())
    monkeypatch.setattr("hmcpctl.ssh.processors.run_hmc_command", command)
    assert await read_lpar_processor_quantities(
        CONFIG, "system; quoted ' name", 7
    ) == dict(zip(FIELDS, row, strict=True))
    assert shlex.split(command.call_args.args[1]) == [
        "lshwres",
        "-r",
        "proc",
        "-m",
        "system; quoted ' name",
        "--level",
        "lpar",
        "--filter",
        "lpar_ids=7",
        "-F",
        ",".join(FIELDS),
    ]
    assert command.call_args.kwargs == {"max_output_bytes": 65536}
    assert command.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        "",
        ",".join(ROW[:-1]),
        ",".join(ROW + ["extra"]),
        ",".join(["8", *ROW[1:]]),
        ",".join(["07", *ROW[1:]]),
        ",".join(ROW) + "\n" + ",".join(ROW),
        '7,"unterminated',
        ",".join(["7", "", *ROW[2:]]),
    ],
)
async def test_processor_read_projection_refuses_malformed(monkeypatch, output):
    from hmcpctl.ssh.processors import read_lpar_processor_quantities
    from hmcpctl.ssh.transport import HMCCLIError

    monkeypatch.setattr(
        "hmcpctl.ssh.processors.run_hmc_command", AsyncMock(return_value=output)
    )
    with pytest.raises(HMCCLIError):
        await read_lpar_processor_quantities(CONFIG, "sys-R1", 7)
