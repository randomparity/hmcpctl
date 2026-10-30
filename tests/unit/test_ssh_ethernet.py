"""Exact one-attempt native Ethernet updates and both encoding boundaries (#1824)."""

import importlib
import importlib.util
import shlex
from unittest.mock import AsyncMock

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.errors import HMCError


def native():
    assert importlib.util.find_spec("hmcpctl.ssh.ethernet"), (
        "native Ethernet writer missing"
    )
    module = importlib.import_module("hmcpctl.ssh.ethernet")
    assert callable(getattr(module, "_set_ethernet_adapter", None))
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attributes,record",
    [
        ([("ieee_virtual_eth", 0)], "ieee_virtual_eth=0"),
        ([("qos_priority", 0)], "qos_priority=0"),
        ([("qos_priority", "none")], "qos_priority=none"),
        ([("vsi_manager_id", "none")], "vsi_manager_id=none"),
        ([("addl_vlan_ids", "101")], "addl_vlan_ids=101"),
        ([("addl_vlan_ids+", "101,102")], '"addl_vlan_ids+=101,102"'),
        ([("addl_vlan_ids-", "101,102")], '"addl_vlan_ids-=101,102"'),
        (
            [
                ("addl_vlan_ids", "101,102"),
                ("ieee_virtual_eth", 1),
                ("qos_priority", 0),
                ("vsi_manager_id", 0),
                ("vsi_type_id", 2),
                ("vsi_type_version", 3),
            ],
            (
                "ieee_virtual_eth=1,qos_priority=0,vsi_manager_id=0,vsi_type_id=2,"
                'vsi_type_version=3,"addl_vlan_ids=101,102"'
            ),
        ),
    ],
)
async def test_exact_one_native_command(monkeypatch, attributes, record):
    module = native()
    transport = AsyncMock(return_value="native-output")
    monkeypatch.setattr(module, "run_hmc_command", transport)
    config = HMCConfig.from_mapping({})
    assert await module._set_ethernet_adapter(
        config, "system one", 7, 4, attributes
    ) == ("native-output")
    transport.assert_awaited_once()
    assert transport.await_args.args[0] is config
    assert shlex.split(transport.await_args.args[1]) == [
        "chhwres",
        "-r",
        "virtualio",
        "--rsubtype",
        "eth",
        "-o",
        "s",
        "-m",
        "system one",
        "--id",
        "7",
        "-s",
        "4",
        "-a",
        record,
    ]
    assert transport.await_args.kwargs == {"max_output_bytes": 65536}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["system'one", "system $(not-a-command); two"])
async def test_system_name_stays_one_shell_word(monkeypatch, name):
    module = native()
    transport = AsyncMock(return_value="")
    monkeypatch.setattr(module, "run_hmc_command", transport)
    await module._set_ethernet_adapter(
        HMCConfig.from_mapping({}), name, 7, 4, [("qos_priority", 0)]
    )
    words = shlex.split(transport.await_args.args[1])
    assert words[words.index("-m") + 1] == name
    assert len(words) == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("partition_id", [0, -1, True, "7", 7.0])
async def test_invalid_partition_id_has_zero_transports(monkeypatch, partition_id):
    module = native()
    transport = AsyncMock()
    monkeypatch.setattr(module, "run_hmc_command", transport)
    with pytest.raises(ValueError):
        await module._set_ethernet_adapter(
            HMCConfig.from_mapping({}), "sys-R1", partition_id, 4, [("qos_priority", 0)]
        )
    transport.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", [-1, 65536, True, "4", 4.0])
async def test_invalid_slot_has_zero_transports(monkeypatch, slot):
    module = native()
    transport = AsyncMock()
    monkeypatch.setattr(module, "run_hmc_command", transport)
    with pytest.raises(ValueError):
        await module._set_ethernet_adapter(
            HMCConfig.from_mapping({}), "sys-R1", 7, slot, [("qos_priority", 0)]
        )
    transport.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attributes",
    [
        [],
        [("port_vlan_id", 1)],
        [("qos_priority", 0), ("qos_priority", 1)],
        [("addl_vlan_ids", "101"), ("addl_vlan_ids+", "102")],
        [("qos_priority", "0,injected=1")],
        [("addl_vlan_ids", '101,102"')],
        [("qos_priority", "0\nnext-command")],
        [("qos_priority", "0\x00tail")],
    ],
)
async def test_record_injection_and_conflicts_have_zero_transports(
    monkeypatch, attributes
):
    module = native()
    transport = AsyncMock()
    monkeypatch.setattr(module, "run_hmc_command", transport)
    with pytest.raises((ValueError, HMCError)):
        await module._set_ethernet_adapter(
            HMCConfig.from_mapping({}), "sys-R1", 7, 4, attributes
        )
    transport.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
async def test_invalid_timeout_has_zero_transports(monkeypatch, timeout):
    module = native()
    transport = AsyncMock()
    monkeypatch.setattr(module, "run_hmc_command", transport)
    with pytest.raises(ValueError, match="finite positive"):
        await module._set_ethernet_adapter(
            HMCConfig.from_mapping({"ssh_timeout": timeout}),
            "sys-R1",
            7,
            4,
            [("qos_priority", 0)],
        )
    transport.assert_not_awaited()
