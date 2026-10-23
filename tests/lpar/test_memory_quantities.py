"""D01 non-pool quantity grammar and guarded effective-memory outcomes."""

import shlex
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_memory import LparMemoryChange
from hmcpctl.ssh.memory import change_lpar_memory_quantity

CONFIG = HMCConfig.from_mapping({})


@pytest.mark.parametrize("operation", ["add", "remove", "set"])
@pytest.mark.parametrize("quantity", [0, 1, 512, 1024])
def test_quantity_request_preserves_integral_mib(operation, quantity):
    change = LparMemoryChange(operation=operation, quantity_mib=quantity)
    assert change.quantity_mib == quantity
    assert change.entitled_mib is None


@pytest.mark.parametrize("entitled", [1, 100, "auto"])
@pytest.mark.parametrize("operation", ["add", "remove", "set"])
def test_shared_entitlement_request_can_stand_alone_or_combine(operation, entitled):
    assert (
        LparMemoryChange(operation=operation, entitled_mib=entitled).entitled_mib
        == entitled
    )
    combined = LparMemoryChange(
        operation=operation, quantity_mib=512, entitled_mib=entitled
    )
    assert combined.quantity_mib == 512
    assert combined.entitled_mib == entitled


@pytest.mark.parametrize(
    "values",
    [
        {"operation": "add"},
        {"operation": "move", "quantity_mib": 512},
        {"operation": "add", "quantity_mib": -1},
        {"operation": "add", "quantity_mib": True},
        {"operation": "add", "quantity_mib": 1.0},
        {"operation": "add", "quantity_mib": "512"},
        {"operation": "add", "entitled_mib": 0},
        {"operation": "add", "entitled_mib": -1},
        {"operation": "add", "entitled_mib": True},
        {"operation": "add", "entitled_mib": 1.0},
        {"operation": "add", "entitled_mib": "100"},
        {"operation": "add", "entitled_mib": "AUTO"},
        {"operation": "set", "quantity_mib": 512, "force": 1},
        {"operation": "set", "quantity_mib": 512, "target": "other"},
    ],
)
def test_quantity_request_refuses_untyped_empty_or_foreign_scope(values):
    with pytest.raises(ValueError):
        LparMemoryChange(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,native", [("add", "a"), ("remove", "r"), ("set", "s")]
)
@pytest.mark.parametrize("entitled", [None, 100, "auto"])
async def test_fixed_native_quantity_grammar(monkeypatch, operation, native, entitled):
    command = AsyncMock(return_value="native output")
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    change = LparMemoryChange(
        operation=operation, quantity_mib=512, entitled_mib=entitled, force=True
    )
    assert (
        await change_lpar_memory_quantity(CONFIG, "system; quoted ' name", 7, change)
        == "native output"
    )
    words = shlex.split(command.call_args.args[1])
    assert words[:9] == [
        "chhwres",
        "-r",
        "mem",
        "-m",
        "system; quoted ' name",
        "-o",
        native,
        "--id",
        "7",
    ]
    assert words[9:] == [
        "-q",
        "512",
        *([] if entitled is None else ["--entitled", str(entitled)]),
        "--force",
    ]
    assert command.await_count == 1


@pytest.mark.asyncio
async def test_entitlement_only_omits_quantity_and_force(monkeypatch):
    command = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    await change_lpar_memory_quantity(
        CONFIG, "sys-R1", 2, LparMemoryChange(operation="set", entitled_mib="auto")
    )
    assert shlex.split(command.call_args.args[1]) == [
        "chhwres",
        "-r",
        "mem",
        "-m",
        "sys-R1",
        "-o",
        "s",
        "--id",
        "2",
        "--entitled",
        "auto",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("partition_id", [0, -1, True, "1", 1.0])
async def test_native_quantity_transport_refuses_invalid_partition_id(
    monkeypatch, partition_id
):
    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    with pytest.raises(ValueError, match="partition_id"):
        await change_lpar_memory_quantity(
            CONFIG,
            "sys-R1",
            partition_id,
            LparMemoryChange(operation="add", quantity_mib=512),
        )
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_zero_set_is_explicit_reset_not_omitted(monkeypatch):
    command = AsyncMock(return_value="")
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    await change_lpar_memory_quantity(
        CONFIG, "sys-R1", 2, LparMemoryChange(operation="set", quantity_mib=0)
    )
    assert shlex.split(command.call_args.args[1])[-2:] == ["-q", "0"]


@pytest.mark.asyncio
async def test_native_quantity_requires_validated_model(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.memory.run_hmc_command", command)
    with pytest.raises(TypeError, match="typed LparMemoryChange"):
        await change_lpar_memory_quantity(CONFIG, "sys-R1", 2, {"operation": "add"})
    command.assert_not_awaited()
