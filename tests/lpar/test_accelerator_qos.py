"""Strict accelerator requests, fixed native commands, and source bindings."""

import asyncio
import json
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_accelerator import LparAcceleratorQos
from hmcpctl.ssh.accelerator import read_accelerator_qos, set_accelerator_qos
from hmcpctl.ssh.transport import HMCCLIError

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "accelerator_qos"


@pytest.mark.parametrize(
    "values",
    [
        {},
        *({"qos": value} for value in (True, False, "1", 1.0, -1, None)),
        *({"qos": 1, "wait_minutes": value} for value in (-1, True, "0", 0.0)),
        *({"qos": 1, "detail_level": value} for value in (0, 5, True, "1", 1.0)),
        *({"qos": 1, "force": value} for value in (0, 1, "true", None)),
        {"qos": 1, "operation": "add"},
    ],
)
def test_strict_request(values):
    with pytest.raises(ValidationError):
        LparAcceleratorQos.model_validate(values)


def test_strict_request_defaults_frozen_and_schema():
    change = LparAcceleratorQos(qos=0)
    assert change.model_dump() == {
        "qos": 0,
        "wait_minutes": None,
        "detail_level": None,
        "force": False,
    }
    with pytest.raises(ValidationError, match="frozen"):
        change.qos = 1
    schema = LparAcceleratorQos.model_json_schema()
    assert schema["required"] == ["qos"]
    assert schema["additionalProperties"] is False
    assert all(prop["description"] for prop in schema["properties"].values())
    for detail in (1, 2, 3, 4):
        assert LparAcceleratorQos(qos=1, detail_level=detail).detail_level == detail


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [0, 129, 10**100])
async def test_explicit_quantities(monkeypatch, quantity):
    run = AsyncMock(return_value="submitted")
    monkeypatch.setattr("hmcpctl.ssh.accelerator.run_hmc_command", run)
    config = HMCConfig.from_mapping({})
    assert (
        await set_accelerator_qos(
            config, "sys-test", 7, LparAcceleratorQos(qos=quantity)
        )
        == "submitted"
    )
    run.assert_awaited_once_with(
        config,
        f"chhwres -r accel -m sys-test --rsubtype gzip -o s --id 7 -q {quantity}",
        include_stderr=True,
        max_output_bytes=65536,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "suffix"),
    [
        ({}, ""),
        ({"wait_minutes": 0}, " -w 0"),
        ({"wait_minutes": 17}, " -w 17"),
        ({"detail_level": 1}, " -d 1"),
        ({"detail_level": 4}, " -d 4"),
        ({"force": False}, ""),
        ({"force": True}, " --force"),
        (
            {"wait_minutes": 0, "detail_level": 4, "force": True},
            " -w 0 -d 4 --force",
        ),
    ],
)
async def test_fixed_native_commands_set(monkeypatch, options, suffix):
    run = AsyncMock(return_value="accepted\n[stderr]\ndeferred")
    monkeypatch.setattr("hmcpctl.ssh.accelerator.run_hmc_command", run)
    config = HMCConfig.from_mapping({"ssh_timeout": 17})
    system = "sys-test'; $(exit 7) & next"
    result = await set_accelerator_qos(
        config, system, 7, LparAcceleratorQos(qos=3, **options)
    )
    command = (
        f"chhwres -r accel -m {shlex.quote(system)} "
        f"--rsubtype gzip -o s --id 7 -q 3{suffix}"
    )
    run.assert_awaited_once_with(
        config, command, include_stderr=True, max_output_bytes=65536
    )
    assert shlex.split(command) == [
        "chhwres",
        "-r",
        "accel",
        "-m",
        system,
        "--rsubtype",
        "gzip",
        "-o",
        "s",
        "--id",
        "7",
        "-q",
        "3",
        *shlex.split(suffix),
    ]
    assert config.ssh_timeout == 17
    assert result == "accepted\n[stderr]\ndeferred"


@pytest.mark.asyncio
@pytest.mark.parametrize("partition_id", [None, 7])
async def test_fixed_native_commands_read(monkeypatch, partition_id):
    observations = json.loads((FIXTURES / "observations.json").read_text())
    row = observations["system" if partition_id is None else "partition"]
    run = AsyncMock(
        return_value=",".join(f"{key}={value}" for key, value in row.items())
    )
    monkeypatch.setattr("hmcpctl.ssh.accelerator.run_hmc_command", run)
    config = HMCConfig.from_mapping({})
    system = "sys-test'; $(exit 7) & next"
    result = await read_accelerator_qos(config, system, partition_id)
    suffix = "sys" if partition_id is None else "lpar --filter lpar_ids=7"
    run.assert_awaited_once_with(
        config,
        f"lshwres -r accel -m {shlex.quote(system)} --rsubtype gzip --level {suffix}",
        max_output_bytes=65536,
    )
    assert result == [{key: str(value) for key, value in row.items()}]


@pytest.mark.asyncio
@pytest.mark.parametrize("partition_id", [0, -1, True, False, "7", 7.0])
async def test_fixed_native_commands_reject_id(monkeypatch, partition_id):
    run = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.accelerator.run_hmc_command", run)
    config = HMCConfig.from_mapping({})
    with pytest.raises(ValueError, match="partition_id.*positive integer"):
        await read_accelerator_qos(config, "sys-test", partition_id)
    with pytest.raises(ValueError, match="partition_id.*positive integer"):
        await set_accelerator_qos(
            config, "sys-test", partition_id, LparAcceleratorQos(qos=1)
        )
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("system", ["", " \t\n"])
async def test_fixed_native_commands_reject_blank_system(monkeypatch, system):
    run = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.accelerator.run_hmc_command", run)
    config = HMCConfig.from_mapping({})
    with pytest.raises(ValueError, match="system.*blank"):
        await read_accelerator_qos(config, system, None)
    with pytest.raises(ValueError, match="system.*blank"):
        await set_accelerator_qos(config, system, 7, LparAcceleratorQos(qos=1))
    run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"qos": 1}, None, 1])
async def test_fixed_native_commands_reject_untyped_change(monkeypatch, change):
    run = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.accelerator.run_hmc_command", run)
    with pytest.raises(TypeError, match="change.*LparAcceleratorQos"):
        await set_accelerator_qos(HMCConfig.from_mapping({}), "sys-test", 7, change)
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixed_native_commands_set_requires_partition(monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr("hmcpctl.ssh.accelerator.run_hmc_command", run)
    with pytest.raises(ValueError, match="partition_id.*positive integer"):
        await set_accelerator_qos(
            HMCConfig.from_mapping({}), "sys-test", None, LparAcceleratorQos(qos=1)
        )
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixed_native_commands_wait_zero_keeps_ssh_deadline(monkeypatch):
    connection = AsyncMock()
    connection.__aenter__.return_value = connection
    connection.abort = MagicMock()
    finished = asyncio.Event()

    async def blocked(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            finished.set()

    connection.create_process.side_effect = blocked
    monkeypatch.setattr(
        "hmcpctl.ssh.transport.asyncssh.connect", MagicMock(return_value=connection)
    )
    config = HMCConfig.from_mapping(
        {
            "host": "hmc.test",
            "user": "test-user",
            "password": "p",
            "ssh_timeout": 0.05,
        }
    )
    with pytest.raises(HMCCLIError, match="completion is unknown"):
        await set_accelerator_qos(
            config, "sys-test", 7, LparAcceleratorQos(qos=1, wait_minutes=0)
        )
    connection.create_process.assert_awaited_once_with(
        "chhwres -r accel -m sys-test --rsubtype gzip -o s --id 7 -q 1 -w 0",
        encoding=None,
        window=4096,
        max_pktsize=4096,
    )
    assert finished.is_set()
    connection.abort.assert_called_once()


@pytest.mark.asyncio
async def test_fixed_native_commands_no_retry(monkeypatch):
    run = AsyncMock(side_effect=HMCCLIError("completion unknown"))
    monkeypatch.setattr("hmcpctl.ssh.accelerator.run_hmc_command", run)
    with pytest.raises(HMCCLIError, match="completion unknown"):
        await set_accelerator_qos(
            HMCConfig.from_mapping({}), "sys-test", 7, LparAcceleratorQos(qos=0)
        )
    assert run.await_count == 1


def test_two_corpus_contract():
    contracts = json.loads((FIXTURES / "reference-contracts.json").read_text())
    allocated_hash = (
        "1569925f-b684fa57-af026870-da88d243-275cd489-8eec6d12-aaf9d5aa-d54617c0"
    )
    supporting_hash = (
        "ceabd96b-d6bb26b7-0d91a475-f6705819-d9807eae-e8040952-b26c5988-b52ce444"
    )
    assert contracts == [
        {
            "selector": f"commands-{corpus}:commands/chhwres:L{line:05}:command-synopsis",
            "sha256": digest,
            "role": role,
            "tokens": tokens,
        }
        for corpus, line, role, tokens, digest in (
            ("p10", 197, "allocated", "-r -m --rsubtype -o -q -p --id", allocated_hash),
            ("p11", 199, "allocated", "-r -m --rsubtype -o -q -p --id", allocated_hash),
            ("p10", 198, "supporting", "-w -d --force", supporting_hash),
            ("p11", 200, "supporting", "-w -d --force", supporting_hash),
        )
    ]
    corpora = json.loads((ROOT / "docs" / "capabilities" / "corpora.json").read_text())
    units = {unit["id"]: unit for unit in corpora["source_units"]}
    topics = {topic["id"]: topic for topic in corpora["topics"]}
    corpus_ids = {corpus["id"] for corpus in corpora["corpora"]}
    for contract in contracts:
        unit = units[contract["selector"]]
        corpus_id = contract["selector"].split(":", 1)[0]
        assert corpus_id in corpus_ids
        assert unit["topic"] == f"{corpus_id}:commands/chhwres"
        assert topics[unit["topic"]]["corpus"] == corpus_id
        assert topics[unit["topic"]]["path"] == "commands/chhwres.md"
        assert unit["kind"] == "command-synopsis"
        assert unit["sha256"] == contract["sha256"]
        assert unit["text"] == contract["tokens"]
        assert unit["accounting"] == {"id": "cli:commands/chhwres", "kind": "row"}
    observations = json.loads((FIXTURES / "observations.json").read_text())
    assert observations == {
        "provenance": {
            "capture_date": "2026-10-26",
            "source": "retained source capture",
            "partition_id": "synthetic",
        },
        "system": {
            "type": "gzip",
            "max_qos": 128,
            "curr_avail_qos": 128,
            "pend_avail_qos": 128,
        },
        "partition": {"lpar_id": 7, "type": "gzip", "curr_qos": 0, "pend_qos": 0},
        "capabilities": {"general": True, "gzip_supported": True, "ibmi": False},
        "xml_qos_collection": {"metadata_only": True, "runtime_evidence": False},
    }
