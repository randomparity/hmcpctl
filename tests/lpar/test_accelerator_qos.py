"""Strict accelerator requests, fixed native commands, and source bindings."""

import asyncio
import json
import shlex
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncssh
import pytest
from pydantic import ValidationError

from hmcpctl.client.core import HMCClient
from hmcpctl.config import HMCConfig
from hmcpctl.documents.lpar_accelerator import LparAcceleratorQos
from hmcpctl.operations.lpar.accelerator import set_lpar_accelerator_qos
from hmcpctl.operations.partition_state import PARTITION_STATES
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


# The domain tests exercise the real admission, ownership, command construction and
# native parser. Only REST requests and SSH execution cross mocked boundaries.
SYSTEM_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PARTITION_UUID = "11111111-2222-3333-aaaa-bbbbbbbbbbbb"


@pytest.fixture
def accelerator_boundary(monkeypatch):
    class Boundary:
        def __init__(self):
            self.hmc = MagicMock(spec=HMCClient)
            self.hmc.config = HMCConfig.from_mapping({"agent_id": "test-agent"})
            self.partition = {
                "UUID": PARTITION_UUID,
                "Resource": {
                    "PartitionName": "part-test",
                    "PartitionID": "7",
                    "PartitionType": "AIX/Linux",
                    "PartitionState": "running",
                    "ResourceMonitoringControlState": "active",
                    "IsOperationInProgress": "false",
                    "HardwareAcceleratorQoS": {"Metadata": {}},
                },
            }
            self.system = {
                "UUID": SYSTEM_UUID,
                "Resource": {
                    "SystemName": "sys-test",
                    "State": "operating",
                    "AssociatedSystemCapabilities": {
                        "HardwareAcceleratorCapable": "true",
                        "IBMiHardwareAcceleratorCapable": "true",
                    },
                    "SupportedHardwareAcceleratorTypes": {
                        "HardwareAcceleratorType": {"Type": "gzip"}
                    },
                },
            }
            self.lpars = [deepcopy(self.partition)]
            self.vios = []
            self.system_rows = [
                {
                    "type": "gzip",
                    "max_qos": "128",
                    "curr_avail_qos": "128",
                    "pend_avail_qos": "128",
                }
            ]
            self.partition_rows = [
                {"type": "gzip", "lpar_id": "7", "curr_qos": "0", "pend_qos": "0"}
            ]
            self.after_rows = [
                {"type": "gzip", "lpar_id": "7", "curr_qos": "3", "pend_qos": "3"}
            ]
            self.sent = []
            self.submitted = False
            self.failure = None
            self.description = ""
            self.counts = {}
            self.hook = lambda event, count: None
            for name, event in (
                ("list_system_summaries", "summaries"),
                ("list_logical_partitions", "lpars"),
                ("list_vios", "vios"),
                ("get_logical_partition", "partition"),
                ("get_vios", "partition"),
                ("get_managed_system", "system"),
            ):

                async def read(*args, _event=event, **kwargs):
                    self.event(_event)
                    return deepcopy(
                        {
                            "summaries": [
                                {"UUID": SYSTEM_UUID, "SystemName": "sys-test"}
                            ],
                            "lpars": self.lpars,
                            "vios": self.vios,
                            "partition": self.partition,
                            "system": self.system,
                        }[_event]
                    )

                setattr(self.hmc, name, AsyncMock(side_effect=read))

        def event(self, name):
            self.counts[name] = self.counts.get(name, 0) + 1
            self.hook(name, self.counts[name])

        async def execute(self, config, command, **kwargs):
            if command.startswith("lssyscfg"):
                self.event("ownership")
                if isinstance(self.description, Exception):
                    raise self.description
                return self.description
            if command.startswith("chhwres"):
                self.sent.append(command)
                self.submitted = True
                if self.failure is not None:
                    raise self.failure
                return ""
            self.event("native")
            rows = (
                self.system_rows
                if "--level sys" in command
                else (self.after_rows if self.submitted else self.partition_rows)
            )
            return "\n".join(
                ",".join(f"{key}={value}" for key, value in row.items()) for row in rows
            )

        async def invoke(self, selector="part-test", change=None, **kwargs):
            return await set_lpar_accelerator_qos(
                self.hmc,
                selector,
                change or LparAcceleratorQos(qos=3),
                system_name_or_uuid=SYSTEM_UUID,
                **kwargs,
            )

    boundary = Boundary()
    monkeypatch.setattr("hmcpctl.ssh.accelerator.run_hmc_command", boundary.execute)
    monkeypatch.setattr("hmcpctl.ssh.profiles.run_hmc_command", boundary.execute)
    return boundary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "uuid",
        "vios",
        "duplicate",
        "malformed",
        "foreign",
        "id0",
        "name",
        "type",
        "kind",
    ],
)
async def test_identity_containment(accelerator_boundary, case):
    b = accelerator_boundary
    selector = "part-test"
    if case == "uuid":
        selector = PARTITION_UUID.upper()
    elif case == "vios":
        b.partition["Resource"]["PartitionType"] = "Virtual IO Server"
        b.vios, b.lpars = [deepcopy(b.partition)], []
    elif case == "duplicate":
        b.vios = deepcopy(b.lpars)
    elif case == "malformed":
        b.lpars[0]["UUID"] = "bad"
    elif case == "foreign":
        b.lpars = []
    else:
        field, value = {
            "id0": ("PartitionID", "0"),
            "name": ("PartitionName", "recycled"),
            "type": ("PartitionType", "novel"),
            "kind": ("PartitionType", "Virtual IO Server"),
        }[case]
        b.partition["Resource"][field] = value
    if case in ("uuid", "vios"):
        result = await b.invoke(selector)
        assert result["UUID"].casefold() == PARTITION_UUID
        assert len(b.sent) == 1
        b.hmc.list_logical_partitions.assert_called_with(SYSTEM_UUID)
        b.hmc.list_vios.assert_called_with(SYSTEM_UUID)
        getter = b.hmc.get_vios if case == "vios" else b.hmc.get_logical_partition
        getter.assert_called_with(PARTITION_UUID)
    else:
        with pytest.raises(ValueError):
            await b.invoke(selector)
        assert b.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "false",
        "malformed",
        "absent-gzip",
        "duplicate-gzip",
        "list",
        "ibmi-false",
        "ibmi-true",
    ],
)
async def test_capability_admission(accelerator_boundary, case):
    b = accelerator_boundary
    resource = b.system["Resource"]
    caps = resource["AssociatedSystemCapabilities"]
    if case == "missing":
        caps.pop("HardwareAcceleratorCapable")
    elif case in ("false", "malformed"):
        caps["HardwareAcceleratorCapable"] = "false" if case == "false" else True
    elif case == "absent-gzip":
        resource["SupportedHardwareAcceleratorTypes"] = {}
    elif case in ("duplicate-gzip", "list"):
        resource["SupportedHardwareAcceleratorTypes"]["HardwareAcceleratorType"] = [
            {"Type": "gzip"},
            {"Type": "gzip" if case == "duplicate-gzip" else "other"},
        ]
    else:
        b.partition["Resource"]["PartitionType"] = "OS400"
        caps["IBMiHardwareAcceleratorCapable"] = (
            "false" if case == "ibmi-false" else "true"
        )
    if case in ("list", "ibmi-true"):
        assert (await b.invoke())["workflow_completed"] is True
        assert len(b.sent) == 1
    else:
        with pytest.raises(ValueError):
            await b.invoke()
        assert b.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        *sorted(PARTITION_STATES),
        None,
        "novel",
        "busy",
        "malformed-busy",
        "missing-busy",
    ],
)
async def test_state_admission(accelerator_boundary, state):
    b = accelerator_boundary
    resource = b.partition["Resource"]
    resource["PartitionState"] = state
    if state in ("busy", "malformed-busy", "missing-busy"):
        resource["PartitionState"] = "running"
        resource["IsOperationInProgress"] = {
            "busy": "true",
            "malformed-busy": False,
            "missing-busy": None,
        }[state]
    if state in PARTITION_STATES and state != "Unknown":
        assert (await b.invoke())["submission"] == "completed"
        assert len(b.sent) == 1
    else:
        with pytest.raises(ValueError):
            await b.invoke()
        assert b.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["AIX/Linux", "Virtual IO Server", "OS400"])
@pytest.mark.parametrize("rmc", ["active", "inactive", "none", "unknown", None])
@pytest.mark.parametrize(
    "options", [{}, {"force": True}, {"wait_minutes": 0}, {"detail_level": 1}]
)
async def test_rmc_force_options(accelerator_boundary, kind, rmc, options):
    b = accelerator_boundary
    b.partition["Resource"].update(
        PartitionType=kind, ResourceMonitoringControlState=rmc
    )
    if kind == "Virtual IO Server":
        b.vios, b.lpars = [deepcopy(b.partition)], []
    allowed = (
        not options
        if kind == "OS400"
        else (
            rmc in ("active", "inactive", "none")
            and (rmc == "active" or options.get("force"))
        )
    )
    if allowed:
        assert (await b.invoke(change=LparAcceleratorQos(qos=3, **options)))[
            "submission"
        ] == "completed"
        assert len(b.sent) == 1
    else:
        with pytest.raises(ValueError):
            await b.invoke(change=LparAcceleratorQos(qos=3, **options))
        assert b.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "max_qos",
        "curr_avail_qos",
        "pend_avail_qos",
        "curr_qos",
        "pend_qos",
        "lpar_id",
        "missing",
        "duplicate",
        "type",
        "wrong-id",
        "large",
        "zero",
    ],
)
@pytest.mark.parametrize("invalid", ["-1", "1.5", "NaN", "", "True"])
async def test_native_baseline(accelerator_boundary, field, invalid):
    b = accelerator_boundary
    if field in ("large", "zero"):
        quantity = 10000 if field == "large" else 0
        await b.invoke(change=LparAcceleratorQos(qos=quantity))
        assert len(b.sent) == 1
        assert f" -q {quantity}" in b.sent[0]
        return
    if field == "missing":
        b.partition_rows = []
    elif field == "duplicate":
        b.system_rows *= 2
    elif field == "type":
        b.partition_rows[0]["type"] = "other"
    elif field == "wrong-id":
        b.partition_rows[0]["lpar_id"] = "8"
    else:
        rows = b.system_rows if field in b.system_rows[0] else b.partition_rows
        rows[0][field] = invalid
    with pytest.raises(ValueError):
        await b.invoke()
    assert b.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign", "malformed", "failed", "override"])
async def test_ownership_audit(accelerator_boundary, monkeypatch, case):
    b = accelerator_boundary
    b.description = {
        "foreign": "[hmcpctl owner:foreign created:2026-10-26]",
        "malformed": "[hmcpctl owner:]",
        "failed": HMCCLIError("ownership unavailable"),
        "override": "[hmcpctl owner:foreign created:2026-10-26]",
    }[case]
    audit = MagicMock()
    monkeypatch.setattr("hmcpctl.audit.records.record_ownership_override", audit)
    if case == "override":
        await b.invoke(ownership_override=True)
        assert audit.call_count == 2
        audit.assert_called_with(
            system="sys-test", lpar="part-test", host="", agent_id="test-agent"
        )
        assert len(b.sent) == 1
    else:
        with pytest.raises((PermissionError, HMCCLIError)):
            await b.invoke()
        assert b.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "containment",
        "UUID",
        "PartitionName",
        "PartitionID",
        "PartitionType",
        "PartitionState",
        "ResourceMonitoringControlState",
        "IsOperationInProgress",
        "system-uuid",
        "SystemName",
        "State",
        "capability",
        "gzip",
        "system-native",
        "partition-native",
        "ownership",
    ],
)
async def test_presubmit_drift(accelerator_boundary, field):
    b = accelerator_boundary

    def drift(event, count):
        if event == "ownership" and count == 2 and field == "ownership":
            b.description = "[hmcpctl owner:foreign created:2026-10-26]"
        if event != "lpars" or count != 2:
            return
        if field == "containment":
            b.lpars = []
        elif field == "UUID":
            b.partition["UUID"] = SYSTEM_UUID
        elif field.startswith("system-"):
            if field == "system-uuid":
                b.system["UUID"] = PARTITION_UUID
            else:
                b.system_rows[0]["max_qos"] = "127"
        elif field == "partition-native":
            b.partition_rows[0]["pend_qos"] = "1"
        elif field in ("SystemName", "State"):
            b.system["Resource"][field] = "changed"
        elif field == "capability":
            b.system["Resource"]["AssociatedSystemCapabilities"][
                "HardwareAcceleratorCapable"
            ] = "false"
        elif field == "gzip":
            b.system["Resource"]["SupportedHardwareAcceleratorTypes"] = {}
        elif field != "ownership":
            b.partition["Resource"][field] = (
                "inactive" if field == "ResourceMonitoringControlState" else "changed"
            )

    b.hook = drift
    with pytest.raises((ValueError, PermissionError)):
        await b.invoke()
    assert b.sent == []


def native_failure(status=None, signal=None):
    cause = asyncssh.ProcessError(
        {}, "chhwres", None, status, signal, status, "", "native refusal"
    )
    error = HMCCLIError("submission diagnostic " + "x" * 3000)
    error.__cause__ = cause
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (None, "completed"),
        (native_failure(4), "rejected"),
        (native_failure(None), "unknown"),
        (native_failure(-1), "unknown"),
        (native_failure(4, ("TERM", False, "", "")), "unknown"),
        (HMCCLIError("exit status 4"), "unknown"),
        (HMCCLIError("timeout"), "unknown"),
    ],
)
async def test_submission_classification(accelerator_boundary, failure, expected):
    b = accelerator_boundary
    b.failure = failure
    result = await b.invoke()
    assert result["submission"] == expected
    assert result["workflow_completed"] is (expected == "completed")
    assert len(b.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "pending", "outcome"),
    [
        ("3", "3", "observed"),
        ("3", "0", "partial"),
        ("0", "3", "partial"),
        ("0", "0", "unverified"),
        ("bad", "3", "unverified"),
        ("3", None, "unverified"),
        (None, None, "unverified"),
    ],
)
async def test_independent_postconditions(
    accelerator_boundary, current, pending, outcome
):
    b = accelerator_boundary
    b.after_rows[0].update(curr_qos=current, pend_qos=pending)
    for field in ("curr_qos", "pend_qos"):
        if b.after_rows[0][field] is None:
            del b.after_rows[0][field]
    result = await b.invoke()
    assert result["outcome"] == outcome
    assert result["after"]["native"] == {
        "curr_qos": int(current) if current not in (None, "bad") else None,
        "pend_qos": int(pending) if pending is not None else None,
    }
    assert bool(result["observation_error"]) is (
        current in (None, "bad") or pending is None
    )
    assert result["workflow_completed"] is (outcome == "observed")
    assert len(b.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "xml",
        "rejected-match",
        "rejected-unchanged",
        "restart",
        "identity",
        "containment",
        "wrong-id",
        "read-failure",
        "system",
        "cancel",
        "unexpected",
    ],
)
async def test_truthful_observations(accelerator_boundary, case):
    b = accelerator_boundary
    change = LparAcceleratorQos(qos=3)
    if case.startswith("rejected"):
        b.failure = native_failure(4)
        if case == "rejected-unchanged":
            b.after_rows = deepcopy(b.partition_rows)
    elif case == "restart":
        b.partition["Resource"]["ResourceMonitoringControlState"] = "inactive"
        change = LparAcceleratorQos(qos=3, force=True)
    elif case in ("cancel", "unexpected"):
        b.failure = (
            asyncio.CancelledError() if case == "cancel" else RuntimeError("unexpected")
        )

    def post_change(event, count):
        if not b.submitted:
            return
        if case == "identity":
            b.partition["Resource"]["PartitionID"] = "8"
        elif case == "containment":
            b.lpars = []
        elif case == "wrong-id":
            b.after_rows[0]["lpar_id"] = "8"
        elif case == "read-failure" and event == "partition":
            raise HMCCLIError("observation " + "x" * 3000)
        elif case == "system":
            b.system["UUID"] = PARTITION_UUID

    b.hook = post_change
    if case in ("cancel", "unexpected"):
        with pytest.raises(
            asyncio.CancelledError if case == "cancel" else RuntimeError
        ):
            await b.invoke(change=change)
        assert len(b.sent) == 1
        return
    result = await b.invoke(change=change)
    assert result["before"]["xml_collection"] == {"Metadata": {}}
    assert result["before"]["native"] == {"curr_qos": 0, "pend_qos": 0}
    assert result["before"]["system_native"] == {
        "max_qos": 128,
        "curr_avail_qos": 128,
        "pend_avail_qos": 128,
    }
    assert result["effective_runtime"] == "unavailable"
    assert result["profile_persistence"] == "unverified"
    assert result["completion_scope"] == "native-current-and-pending"
    assert result["restart_required"] is (True if case == "restart" else None)
    assert any("replay" in warning.lower() for warning in result["warnings"])
    if case in ("identity", "containment", "wrong-id", "read-failure", "system"):
        assert result["outcome"] == "unverified"
        assert result["after"] is None
        assert 0 < len(result["observation_error"]) <= 2048
    elif case.startswith("rejected"):
        assert result["outcome"] == (
            "observed" if case == "rejected-match" else "rejected"
        )
        assert result["workflow_completed"] is False
        assert len(result["submission_error"]) == 2048
    else:
        assert result["workflow_completed"] is True
        assert "system_native" not in result["after"]
    assert len(b.sent) == 1
