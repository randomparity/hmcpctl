from __future__ import annotations

import csv
import shlex

import pytest

from hmcpctl.config import HMCConfig
from hmcpctl.ssh.transport import HMCCLIError
from hmcpctl.ssh.vnic import (
    add_vnic_backing,
    list_vnic_backing_rows,
    list_vnic_rows,
    list_vnics,
    read_vios_identity,
    remove_vnic_slot,
)


@pytest.fixture
def config() -> HMCConfig:
    return HMCConfig(host="hmc", user="user")


@pytest.mark.asyncio
async def test_list_vnic_rows_requests_exact_fields(monkeypatch, config) -> None:
    fields = (
        "lpar_name,lpar_id,slot_num,desired_mode,curr_mode,auto_priority_failover,"
        "port_vlan_id,pvid_priority,allowed_vlan_ids,mac_addr,allowed_os_mac_addrs,"
        "backing_devices,backing_device_states"
    )
    output = (
        fields
        + "\nclient,3,2,ded,ded,1,0,0,all,eaad,all,sriov/vios/100/1/1/27004003/2.0/2.0/50/100.0/100.0,sriov/27004003/1/Operational\n"
    )
    calls: list[str] = []

    async def fake_run(_config, command: str) -> str:
        calls.append(command)
        return output

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    rows = await list_vnic_rows(config, "system one", "client one")

    assert rows[0]["slot_num"] == "2"
    assert shlex.split(calls[0]) == [
        "lshwres",
        "-r",
        "virtualio",
        "--rsubtype",
        "vnic",
        "--level",
        "lpar",
        "-m",
        "system one",
        "--filter",
        "lpar_names=client one",
        "-F",
        fields,
        "--header",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply", ["No results were found.\n", "  No results were found.  \n\n"]
)
async def test_list_vnics_accepts_hmc_empty_result(
    monkeypatch, config, reply: str
) -> None:
    async def fake_run(_config, _command: str) -> str:
        return reply

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    assert await list_vnics(config, "system", "client") == []


@pytest.mark.asyncio
async def test_list_vnic_rows_accepts_hmc_empty_result(monkeypatch, config) -> None:
    async def fake_run(_config, _command: str) -> str:
        return "No results were found.\n"

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    assert await list_vnic_rows(config, "system", "client") == []


@pytest.mark.asyncio
async def test_list_vnic_backing_rows_accepts_hmc_empty_result(
    monkeypatch, config
) -> None:
    async def fake_run(_config, _command: str) -> str:
        return "No results were found.\n"

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    assert await list_vnic_backing_rows(config, "system") == []


@pytest.mark.asyncio
async def test_list_vnic_backing_rows_requests_exact_fields(
    monkeypatch, config
) -> None:
    fields = (
        "lpar_name,lpar_id,type,adapter_id,physical_port_id,logical_port_id,capacity,"
        "desired_capacity,max_capacity,desired_max_capacity,failover_priority,is_active,status"
    )
    calls: list[str] = []

    async def fake_run(_config, command: str) -> str:
        calls.append(command)
        return fields + "\n"

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    assert await list_vnic_backing_rows(config, "system one") == []
    assert shlex.split(calls[0]) == [
        "lshwres",
        "-r",
        "virtualio",
        "--rsubtype",
        "vnicbkdev",
        "-m",
        "system one",
        "-F",
        fields,
        "--header",
    ]


_HEADER_READS = {
    "vnic": (
        lambda config: list_vnic_rows(config, "system", "client"),
        "vNIC inventory",
        "lpar_name",
    ),
    "backing": (
        lambda config: list_vnic_backing_rows(config, "system"),
        "vNIC backing-device inventory",
        "adapter_id",
    ),
    "vios-identity": (
        lambda config: read_vios_identity(config, "system", "vios"),
        "VIOS identity read",
        "lpar_env",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("read", sorted(_HEADER_READS))
@pytest.mark.parametrize("output", ["", "bad=value\n", "lpar_name,lpar_id\nvios\n"])
async def test_header_reads_raise_hmc_cli_error_for_malformed_output(
    monkeypatch, config, read: str, output: str
) -> None:
    async def fake_run(_config, _command: str) -> str:
        return output

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    call, operation, field = _HEADER_READS[read]
    with pytest.raises(
        HMCCLIError, match=rf"^{operation} response did not match .*{field}"
    ):
        await call(config)


@pytest.mark.asyncio
async def test_read_vios_identity_treats_empty_result_as_zero_rows(
    monkeypatch, config
) -> None:
    async def fake_run(_config, _command: str) -> str:
        return "No results were found.\n"

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    with pytest.raises(ValueError, match="returned 0 rows; expected 1"):
        await read_vios_identity(config, "system", "vios")


@pytest.mark.asyncio
async def test_read_vios_identity_requires_exactly_one_row(monkeypatch, config) -> None:
    calls: list[str] = []

    async def fake_run(_config, _command: str) -> str:
        calls.append(_command)
        return "name,lpar_id,lpar_env\nvios,100,vioserver\n"

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    assert await read_vios_identity(config, "system", "vios") == {
        "name": "vios",
        "lpar_id": "100",
        "lpar_env": "vioserver",
    }
    assert shlex.split(calls[0]) == [
        "lssyscfg",
        "-r",
        "lpar",
        "-m",
        "system",
        "--filter",
        "lpar_names=vios",
        "-F",
        "name,lpar_id,lpar_env",
        "--header",
    ]


@pytest.mark.asyncio
async def test_add_vnic_uses_p_and_quotes_whole_payload(monkeypatch, config) -> None:
    calls: list[str] = []

    async def fake_run(_config, command: str) -> str:
        calls.append(command)
        return "added"

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    result = await add_vnic_backing(
        config, "system one", "client one", "sriov/vios name/100/1/1/2; touch nope", 7
    )
    assert result == "added"
    assert shlex.split(calls[0]) == [
        "chhwres",
        "-r",
        "virtualio",
        "--rsubtype",
        "vnic",
        "-o",
        "a",
        "-m",
        "system one",
        "-p",
        "client one",
        "-a",
        "port_vlan_id=7,backing_devices=sriov/vios name/100/1/1/2; touch nope",
    ]


@pytest.mark.asyncio
async def test_add_vnic_backing_quotes_a_multi_device_list(monkeypatch, config) -> None:
    """A comma-carrying device list renders as the IBM quoted pair (ADR 0061)."""
    calls: list[str] = []

    async def fake_run(_config, command: str) -> str:
        calls.append(command)
        return "added"

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    result = await add_vnic_backing(
        config, "system-a", "client-a", "sriov/vios1/1/0/2,sriov/vios2/2/0/2", 7
    )
    assert result == "added"
    # Every assertion here must be capable of failing.
    assert len(calls) == 1
    assert shlex.split(calls[0])[-2:] == [
        "-a",
        'port_vlan_id=7,"backing_devices=sriov/vios1/1/0/2,sriov/vios2/2/0/2"',
    ]


@pytest.mark.asyncio
async def test_add_vnic_backing_refuses_record_structure_in_a_device(
    monkeypatch, config
) -> None:
    """`=` inside a device value is refused; only the comma is quotable."""
    from hmcpctl.ssh.transport import HMCCLIError

    with pytest.raises(HMCCLIError, match="equals sign"):
        await add_vnic_backing(config, "system-a", "client-a", "sriov/dev=1", 7)


@pytest.mark.asyncio
async def test_remove_vnic_uses_p_and_s(monkeypatch, config) -> None:
    calls: list[str] = []

    async def fake_run(_config, command: str) -> str:
        calls.append(command)
        return "removed"

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", fake_run)
    assert (
        await remove_vnic_slot(config, "system one", "client one", "slot one")
        == "removed"
    )
    assert shlex.split(calls[0]) == [
        "chhwres",
        "-r",
        "virtualio",
        "--rsubtype",
        "vnic",
        "-o",
        "r",
        "-m",
        "system one",
        "-p",
        "client one",
        "-s",
        "slot one",
    ]


@pytest.mark.asyncio
async def test_two_policy_lists_keep_separate_hmc_attributes(monkeypatch, config):
    calls = []

    async def run(_config, command):
        calls.append(command)
        return "ok"

    monkeypatch.setattr("hmcpctl.ssh.vnic.run_hmc_command", run)
    await add_vnic_backing(
        config,
        "system",
        "client",
        "sriov/vios/1/1/1/2",
        0,
        policy={
            "pvid_priority": "0",
            "allowed_vlan_ids": "2,3",
            "allowed_os_mac_addrs": "aabbccddeeff,001122334455",
            "auto_priority_failover": "0",
        },
    )
    assert len(calls) == 1
    pairs = next(csv.reader([shlex.split(calls[0])[-1]], strict=True))
    assert pairs == [
        "port_vlan_id=0",
        "backing_devices=sriov/vios/1/1/1/2",
        "pvid_priority=0",
        "allowed_vlan_ids=2,3",
        "allowed_os_mac_addrs=aabbccddeeff,001122334455",
        "auto_priority_failover=0",
    ]


def test_policy_encoded_record_byte_boundary():
    from hmcpctl.ssh.vnic import build_vnic_add_attributes

    prefix = build_vnic_add_attributes("", 0, {"pvid_priority": "0"})
    backing = "x" * (65_536 - len(prefix.encode("utf-8")))
    assert (
        len(build_vnic_add_attributes(backing, 0, {"pvid_priority": "0"}).encode())
        == 65_536
    )
    with pytest.raises(ValueError, match="65536-byte"):
        build_vnic_add_attributes(backing + "é", 0, {"pvid_priority": "0"})
